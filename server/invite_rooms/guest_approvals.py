"""Bounded canonical guest approvals. No generic RPC, legacy fallback or ID derivation.

The authenticated local operator trusts the core's unique Group namespace, not
an invented cryptographic grant -> session binding. See the API restriction.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import threading
import time
from collections import OrderedDict
from pathlib import Path

import aiohttp

from invite_rooms.guest import GuestServiceError
from invite_rooms.native_rpc import MAX_REQUEST, MAX_RESPONSE, _unique


class ApprovalHandles:
    """Process-local opaque, expiring references; restart requires a new snapshot."""
    def __init__(self, *, ttl=300, limit=2048):
        self.ttl, self.limit = ttl, limit
        self._items = OrderedDict()
        self._lock = threading.Lock()

    def _expire(self):
        now = time.monotonic()
        for key, (expires, _) in list(self._items.items()):
            if expires <= now:
                del self._items[key]

    def issue(self, value):
        with self._lock:
            self._expire()
            for key, (_, existing) in self._items.items():
                if existing == value:
                    return key
            while len(self._items) >= self.limit:
                self._items.popitem(last=False)
            key = 'approval-' + secrets.token_hex(24)
            self._items[key] = (time.monotonic() + self.ttl, value)
            return key

    def get(self, key):
        with self._lock:
            self._expire()
            item = self._items.get(key)
            if item is None:
                raise GuestServiceError('request_expired')
            return item[1]

    def discard(self, key):
        with self._lock:
            self._items.pop(key, None)


HANDLES = ApprovalHandles()


def scope_key(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     allow_nan=False).encode()).hexdigest()


class _Socket:
    """Private fixed verbs only; there is deliberately no public RPC callable."""
    def __init__(self, ws, profile, validate):
        self.ws, self.profile, self.validate = ws, profile, validate

    async def _frame(self):
        frame = await self.ws.receive()
        self.validate()
        if frame.type != aiohttp.WSMsgType.TEXT or len(frame.data.encode()) > MAX_RESPONSE:
            raise GuestServiceError('native_unavailable')
        value = json.loads(frame.data, object_pairs_hook=_unique,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if not isinstance(value, dict) or value.get('jsonrpc') != '2.0':
            raise GuestServiceError('native_unavailable')
        return value

    async def ready(self):
        budget = MAX_RESPONSE
        for _ in range(32):
            value = await self._frame()
            budget -= len(json.dumps(value).encode())
            if budget < 0 or set(value) != {'jsonrpc', 'method', 'params'}:
                raise GuestServiceError('native_unavailable')
            params = value.get('params')
            if value.get('method') == 'event' and isinstance(params, dict) and params.get('type') == 'gateway.ready':
                payload = params.get('payload')
                if not isinstance(payload, dict) or payload.get('session_authority') is not True:
                    raise GuestServiceError('not_supported')
                return
        raise GuestServiceError('native_unavailable')

    async def _call(self, method, params):
        # Not a general-purpose allowlist: only fixed calls below reach here.
        if method not in {'session.list', 'session.resume', 'session.detach', 'approval.respond'}:
            raise GuestServiceError('forbidden')
        rid = secrets.token_hex(16)
        raw = json.dumps({'jsonrpc': '2.0', 'id': rid, 'method': method,
                          'params': dict(params, profile=self.profile)}, allow_nan=False)
        if len(raw.encode()) > MAX_REQUEST:
            raise GuestServiceError('native_unavailable')
        # No await between final durable scope check and the native write.
        self.validate()
        await self.ws.send_str(raw)
        self.validate()
        budget = MAX_RESPONSE
        for _ in range(256):
            value = await self._frame()
            budget -= len(json.dumps(value).encode())
            if budget < 0:
                break
            if set(value) == {'jsonrpc', 'method', 'params'} and isinstance(value['method'], str):
                continue
            if value.get('id') != rid:
                break
            if set(value) == {'jsonrpc', 'id', 'error'}:
                error = value['error']
                if not isinstance(error, dict) or type(error.get('code')) is not int:
                    break
                reason = error.get('data', {}).get('reason') if isinstance(error.get('data'), dict) else None
                if error['code'] == -32601:
                    raise GuestServiceError('not_supported')
                if reason in {'permission_denied', 'profile_mismatch'}:
                    raise GuestServiceError('forbidden')
                if reason in {'stale_generation', 'generation_mismatch', 'not_found'}:
                    raise GuestServiceError('request_expired')
                raise GuestServiceError('native_unavailable')
            if set(value) == {'jsonrpc', 'id', 'result'} and isinstance(value['result'], dict):
                return value['result']
            break
        raise GuestServiceError('native_unavailable')

    async def discover(self, room):
        result = await self._call('session.list', {'include_hidden': True, 'limit': 200})
        rows = result.get('sessions')
        if result.get('scope') != 'live' or not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
            raise GuestServiceError('not_supported')
        if len(rows) >= 200:
            raise GuestServiceError('discovery_inconclusive')
        matches = [r for r in rows if r.get('title') == 'Group: ' + room and r.get('source') == 'bot_room']
        if len(matches) != 1:
            raise GuestServiceError('discovery_inconclusive' if matches else 'session_unavailable')
        row = matches[0]
        sid = row.get('session_id')
        if (not isinstance(sid, str) or not sid or len(sid) > 256 or
                ('profile' in row and row['profile'] != self.profile)):
            raise GuestServiceError('native_unavailable')
        return sid

    async def resume(self, sid):
        result = await self._call('session.resume', {'session_id': sid})
        if (result.get('session_id') != sid or not isinstance(result.get('subscription_id'), str)
                or not result['subscription_id'] or type(result.get('execution_generation')) is not int
                or not isinstance(result.get('prompts'), list) or len(result['prompts']) > 200):
            raise GuestServiceError('not_supported')
        return result


class GuestApprovalAdapter:
    def __init__(self, rpc_config, *, handles=HANDLES):
        self.config, self.handles = rpc_config, handles

    async def run(self, context, validate, *, request_id=None, choice=None):
        if self.config is None:
            raise GuestServiceError('native_unavailable')
        expected = self.handles.get(request_id) if request_id is not None else None
        try:
            token = Path(self.config['token_path']).read_text(encoding='utf-8').strip()
        except (OSError, UnicodeError):
            raise GuestServiceError('native_unavailable') from None
        if not token:
            raise GuestServiceError('native_unavailable')
        key = scope_key({'confirmed': context, 'endpoint': self.config['endpoint'],
                         'credential': hashlib.sha256(token.encode()).hexdigest()})
        if expected is not None and expected[0] != key:
            raise GuestServiceError('request_expired')
        snapshot = None
        sid = None
        try:
            async with asyncio.timeout(10):
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as client:
                    async with client.ws_connect(self.config['endpoint'], max_msg_size=MAX_RESPONSE,
                            headers={'Authorization': 'Bearer ' + token}) as ws:
                        validate()
                        socket = _Socket(ws, context['profile'], validate)
                        await socket.ready()
                        try:
                            sid = await socket.discover(context['room_id'])
                            snapshot = await socket.resume(sid)
                            prompts = []
                            identities = set()
                            for prompt in snapshot['prompts']:
                                if not isinstance(prompt, dict):
                                    raise GuestServiceError('native_unavailable')
                                if prompt.get('kind') != 'approval':
                                    continue
                                pid, generation = prompt.get('prompt_id'), prompt.get('execution_generation')
                                choices = prompt.get('choices')
                                if (not isinstance(pid, str) or not pid or len(pid) > 256
                                        or type(generation) is not int or generation < 1
                                        or generation != snapshot['execution_generation']
                                        or not isinstance(choices, list) or not {'once', 'deny'} <= set(choices)
                                        or pid in identities or prompt.get('session_id', sid) != sid):
                                    raise GuestServiceError('native_unavailable')
                                identities.add(pid)
                                identity = (key, sid, pid, generation)
                                prompts.append((identity, prompt))
                            validate()
                            if expected is None:
                                return [{'request_id': self.handles.issue(identity),
                                         'title': p.get('command', ''), 'detail': p.get('description', ''),
                                         'command': p.get('command', ''), 'description': p.get('description', ''),
                                         'choices': ['once', 'deny'], 'allow_permanent': False}
                                        for identity, p in prompts]
                            if expected not in [identity for identity, _ in prompts]:
                                raise GuestServiceError('request_expired')
                            if choice not in {'once', 'deny'}:
                                raise GuestServiceError('invalid_choice')
                            result = await socket._call('approval.respond', {
                                'session_id': sid, 'prompt_id': expected[2],
                                'execution_generation': expected[3], 'choice': choice})
                            if (set(result) != {'status', 'prompt_id'} or result['prompt_id'] != expected[2]
                                    or result['status'] not in {'resolved', 'already_resolved'}):
                                raise GuestServiceError('native_unavailable')
                            self.handles.discard(request_id)
                            return result
                        finally:
                            if snapshot is not None:
                                # A failed scope check skips explicit detach; closing the socket
                                # removes only its subscriptions. Never stamp end_reason or stop.
                                try:
                                    async with asyncio.timeout(1):
                                        await socket._call('session.detach', {
                                            'session_id': sid, 'subscription_id': snapshot['subscription_id']})
                                except Exception:
                                    pass
        except GuestServiceError:
            raise
        except aiohttp.WSServerHandshakeError as exc:
            raise GuestServiceError('forbidden' if exc.status in {401, 403} else 'native_unavailable') from None
        except Exception:
            raise GuestServiceError('native_unavailable') from None
