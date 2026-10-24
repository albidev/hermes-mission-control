"""Canonical bounded native groups RPC on an owner-authenticated fixed WebSocket.

This is the ONE implementation. No listener, remote RPC ingress or
request-selected URL.
The owner supplies an already authenticated fixed local socket.  No retries; any
protocol ambiguity poisons the adapter so a late response can never be treated as
another result.
"""
from __future__ import annotations

import asyncio
import json
import math
import time
import uuid

import aiohttp

from room_lobby_service import AdmissionDenied, NativeRpcRejected, REGISTER_ADMISSION  # noqa: F401 — re-exported for canonical surface

MAX_REQUEST = 24 * 1024
MAX_RESPONSE = 256 * 1024
_IDLE_POLL = 0.001  # buffered idle frames return at once; an empty queue ends the drain
# The idle drain has its own budget: a normal idle backlog is tens of frames (29 after a
# 10 min gate wait), so these bounds only trip on a flood, which then fails closed.
_DRAIN_MAX_FRAMES = 256
_DRAIN_MAX_BYTES = 4 * MAX_RESPONSE
# Buffered receive() completes without suspending, so asyncio.timeout cannot fire
# inside a run of buffered frames: yield explicitly and check our own deadline.
_DRAIN_YIELD_EVERY = 16
# Fixed native Room methods only. Never exposed as a remote control operation.
_FIELDS = {
    'groups.capabilities': (set(), set()),
    'groups.state': ({'room_id'}, {'room_id'}),
    'groups.log': ({'room_id'}, {'room_id', 'since_seq', 'limit'}),
    'groups.create': ({'room_id', 'name', 'members'}, {'room_id', 'name', 'members'}),
    'groups.peer.invite': ({'room_id', 'home_install_id', 'authority_gateway_id', 'authority_epoch', 'member_id'},
                         {'profile', 'room_id', 'home_install_id', 'authority_gateway_id', 'authority_epoch', 'member_id', 'grant_id', 'ttl_seconds'}),
    'groups.peer.register': ({'room_id', 'member_id', 'target_url', 'target_profile', 'grant', 'catalog'},
                           {'room_id', 'member_id', 'target_url', 'target_profile', 'grant', 'catalog', 'cancellation_scope_id', 'trace_id'}),
    'groups.peer.revoke': ({'grant'}, {'grant', 'profile'}),
    'groups.send': ({'room_id', 'event_id', 'payload'}, {'room_id', 'event_id', 'payload'}),
    # Owner-explicit orphan reconciliation only (lobby_reconcile): exact room id, nothing else.
    'groups.disband': ({'room_id'}, {'room_id'}),
}


class NativeRpcAmbiguous(RuntimeError):
    def __init__(self):
        super().__init__('native_rpc_ambiguous')


class NativeRpcDenied(ValueError):
    def __init__(self):
        super().__init__('native_rpc_denied')


def _unique(pairs):
    """Reject duplicate keys in JSON objects (object_pairs_hook)."""
    result = {}
    for k, v in pairs:
        if k in result:
            raise ValueError()
        result[k] = v
    return result


class BoundedNativeRpc:
    def __init__(self, ws, *, timeout: float = 10, require_register_admission: bool = False):
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 10:
            raise NativeRpcDenied()
        self.ws = ws
        # Production transports set this: a groups.peer.register with no admission seam is never sent.
        self._require_register_admission = bool(require_register_admission)
        self.timeout = timeout
        self._lock = asyncio.Lock()
        self._poisoned = False
        self.drained_notifications = 0

    @staticmethod
    def _notification(message):
        """Parsed native frame, or None when it is a well-formed notification."""
        if message.type != aiohttp.WSMsgType.TEXT or len(message.data.encode()) > MAX_RESPONSE:
            raise ValueError()
        value = json.loads(message.data, object_pairs_hook=_unique,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if not isinstance(value, dict) or value.get('jsonrpc') != '2.0':
            raise ValueError()
        if set(value) <= {'jsonrpc', 'method', 'params'} and isinstance(value.get('method'), str):
            return None
        return value

    async def _drain_idle(self, deadline):
        # The native socket broadcasts surface-global notifications to every client.
        # Those queued while the adapter sat idle predate this request, so they cannot
        # spend its response budget. Only already-buffered frames are read (no wait);
        # a response or malformed frame before a request is a protocol violation.
        frames = size = 0
        while True:
            try:
                message = await self.ws.receive(timeout=_IDLE_POLL)
            except TimeoutError:
                return
            try:
                if self._notification(message) is not None:
                    raise ValueError()
                frames += 1
                size += len(message.data.encode())
                if frames > _DRAIN_MAX_FRAMES or size > _DRAIN_MAX_BYTES or time.monotonic() >= deadline:
                    raise ValueError()
            except Exception:
                self._poisoned = True  # the stream can no longer be trusted
                raise
            self.drained_notifications += 1
            if frames % _DRAIN_YIELD_EVERY == 0:
                await asyncio.sleep(0)

    async def __call__(self, method, params):
        if not isinstance(method, str) or method not in _FIELDS or not isinstance(params, dict):
            raise NativeRpcDenied()
        required, allowed = _FIELDS[method]
        if not required <= set(params) <= allowed:
            raise NativeRpcDenied()
        admission = REGISTER_ADMISSION.get() if method == 'groups.peer.register' else None
        if method == 'groups.peer.register' and admission is None and self._require_register_admission:
            raise NativeRpcDenied()
        rid = uuid.uuid4().hex
        try:
            raw = json.dumps({'jsonrpc': '2.0', 'id': rid, 'method': method, 'params': params},
                             allow_nan=False, separators=(',', ':'))
            if len(raw.encode()) > MAX_REQUEST:
                raise ValueError()
        except (ValueError, TypeError, RecursionError):
            raise NativeRpcDenied() from None
        acquired = False
        sent = False
        deadline = time.monotonic() + self.timeout
        try:
            async with asyncio.timeout(self.timeout):
                await self._lock.acquire()
                acquired = True
                if self._poisoned:
                    raise NativeRpcAmbiguous()
                await self._drain_idle(deadline)
                if admission is not None:
                    # R1-05: after the queue lock and the idle drain, immediately before the frame is
                    # written. Synchronous and with NO await between this check and the send below.
                    try:
                        admission()
                    except Exception:
                        raise AdmissionDenied() from None
                sent = True  # send failure can follow partial native admission
                await self.ws.send_str(raw)
                # Native notifications are permitted, but cannot reset time/byte budgets.
                budget = MAX_RESPONSE
                for _ in range(16):
                    message = await self.ws.receive()
                    if message.type != aiohttp.WSMsgType.TEXT:
                        raise ValueError()
                    budget -= len(message.data.encode())
                    if budget < 0:
                        raise ValueError()
                    value = self._notification(message)
                    if value is None:
                        continue
                    if value.get('id') != rid:
                        raise ValueError()
                    if set(value) == {'jsonrpc', 'id', 'error'}:
                        error = value['error']
                        if (not isinstance(error, dict) or not {'code', 'message'} <= set(error) <= {'code', 'message', 'data'}
                                or type(error.get('code')) is not int or not isinstance(error.get('message'), str)):
                            raise ValueError()
                        # Numeric native code/message/data are intentionally never echoed.
                        raise NativeRpcRejected()
                    if set(value) != {'jsonrpc', 'id', 'result'} or not isinstance(value['result'], dict):
                        raise ValueError()
                    return value['result']
                raise ValueError()
        except (NativeRpcRejected, AdmissionDenied):
            raise
        except asyncio.CancelledError:
            if sent:
                self._poisoned = True
            raise
        except Exception:
            if sent:
                self._poisoned = True
            raise NativeRpcAmbiguous() from None
        finally:
            if acquired:
                self._lock.release()