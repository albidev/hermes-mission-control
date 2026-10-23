"""Unimported isolated-lab lobby protocol; no native/admin forwarding."""
from __future__ import annotations

import re
from urllib.parse import urlsplit


class ControlError(ValueError):
    """Machine code only."""


def parse_invitation(url):
    if not isinstance(url, str) or len(url) > 512:
        raise ControlError('invalid_invitation')
    try:
        p = urlsplit(url)
        match = re.fullmatch(r'/invite/(lobby-[a-zA-Z0-9-]{1,64})/(invite-[a-zA-Z0-9-]{1,64})', p.path)
        valid = (p.scheme == 'https' and p.netloc == 'rooms.lab' and not p.query
                 and match and re.fullmatch(r'[A-Za-z0-9_-]{43}', p.fragment)
                 and not any(ord(ch) < 33 for ch in url))
    except ValueError:
        valid = False
    if not valid:
        raise ControlError('invalid_invitation')
    assert match is not None
    return match[1], match[2], p.fragment


import json
import time
from room_invite_store import InviteError
from tls_stream import SequencedTLS, client_context, server_context, wrap_client, wrap_server

MAX_PAYLOAD = 24 * 1024


def _unique(pairs):
    result = {}
    for k, v in pairs:
        if k in result:
            raise ControlError('invalid_control')
        result[k] = v
    return result


def decode(raw):
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_PAYLOAD:
        raise ControlError('invalid_control')
    try:
        value = json.loads(raw, object_pairs_hook=_unique,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError):
        raise ControlError('invalid_control') from None
    if not isinstance(value, dict):
        raise ControlError('invalid_control')
    return value


def encode(value):
    try:
        raw = json.dumps(value, separators=(',', ':'), allow_nan=False).encode()
    except (ValueError, TypeError):
        raise ControlError('invalid_control') from None
    if not 0 < len(raw) <= MAX_PAYLOAD:
        raise ControlError('invalid_control')
    return raw


class PinnedControl:
    """One fixed route, one verified peer; identity never comes from input JSON."""
    def __init__(self, client, inner):
        self.client = client
        import threading
        self._slots = threading.BoundedSemaphore(2)
        self.pin = inner['peer_fingerprint']
        self.client_tls = client_context(ca_file=inner['peer_ca_file'], cert_file=inner['cert_file'], key_file=inner['key_file'])
        self.server_tls = server_context(ca_file=inner['peer_ca_file'], cert_file=inner['cert_file'], key_file=inner['key_file'])

    def serve(self, handler):
        if (not isinstance(handler, HostLobbyControl)
                or handler.binding['fingerprint'] != self.pin
                or handler.binding['target_install_id'] != self.client.peer_identity):
            raise ControlError('peer_binding_mismatch')
        def receive(sock, stream_id):
            sock.settimeout(10)
            with wrap_server(sock, self.server_tls, expected_fingerprint=self.pin) as tls:
                frames = SequencedTLS(tls, deadline=time.monotonic() + 10)
                try:
                    request = decode(frames.receive()[1])
                    result = {'result': handler(request)}
                except (InviteError, ControlError):
                    # One generic unavailable code prevents invite/state diagnostics oracles.
                    result = {'error': 'control_unavailable'}
                except Exception:
                    result = {'error': 'control_ambiguous'}
                frames.send(encode(result))
        self.client.on_new_stream = receive

    def call(self, request):
        deadline = time.monotonic() + 10
        if not self._slots.acquire(timeout=10):
            raise ControlError('control_busy')
        try:
            return self._call(request, deadline)
        finally:
            self._slots.release()

    def _call(self, request, deadline):
        raw = encode(request)
        sock = None
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError()
            sock = self.client.open_stream(timeout=remaining)
            sock.settimeout(max(.001, deadline - time.monotonic()))
            with wrap_client(sock, self.client_tls, expected_fingerprint=self.pin) as tls:
                frames = SequencedTLS(tls, deadline=deadline)
                frames.send(raw)
                response = decode(frames.receive()[1])
        except Exception:
            raise ControlError('control_ambiguous') from None
        finally:
            if sock is not None:
                sock.close()
        if set(response) == {'error'}:
            raise ControlError('control_unavailable' if response['error'] == 'control_unavailable' else 'control_ambiguous')
        if set(response) != {'result'} or not isinstance(response['result'], dict):
            raise ControlError('control_ambiguous')
        return response['result']


class HostLobbyControl:
    def __init__(self, store, lobby_id, binding, *, joining_owner_key=None, host_id=None, material_registry=None):
        store._validate_binding(binding)
        self.store, self.lobby_id, self.binding = store, lobby_id, dict(binding)
        self.joining_owner_key, self.host_id = joining_owner_key, host_id
        self.material_registry = material_registry

    def _consent_control(self, request):
        from lobby_consent import verify
        op = request['op']
        expected_fields = {'op', 'invite_id'} if op == 'status' else {'op', 'invite_id', 'receipt', 'signature'}
        if (set(request) != expected_fields or self.joining_owner_key is None or self.host_id is None
                or not isinstance(request['invite_id'], str)):
            raise ControlError('invalid_control')
        invite_id = request['invite_id']
        with self.store._write() as db:
            lobby = self.store._open_lobby(db, self.lobby_id)
            row = db.execute("SELECT r.*,i.state,i.member_id FROM invitations i JOIN redemptions r USING(invite_id) WHERE i.lobby_id=? AND i.invite_id=?", (self.lobby_id, invite_id)).fetchone()
            if (row is None or row['state'] != 'redeemed' or any(row[k] != v for k, v in self.binding.items())):
                raise ControlError('invalid_control')
            scope = {'lobby_id': self.lobby_id, 'invite_id': invite_id, 'room_id': lobby['planned_room_id'],
                     'member_id': row['member_id'], 'binding': self.binding, 'host_id': self.host_id}
            if op == 'status':
                return {'status': self.store._consent_status(row), 'scope': scope}
            verify(self.joining_owner_key, request['receipt'], request['signature'],
                   dict(scope, purpose='joining-owner-consent-v1'))
            if row['host_decision'] != 'approved' or row['joining_consent'] not in {'pending', 'given'}:
                raise ControlError('host_approval_required')
            db.execute("UPDATE redemptions SET joining_consent='given',decided_at=? WHERE invite_id=? AND joining_consent='pending'", (self.store.clock(), invite_id))
        return {'status': 'ready'}

    def __call__(self, request):
        if not isinstance(request, dict):
            raise ControlError('invalid_control')
        op = request.get('op')
        if op == 'material':
            from lobby_consent import verify
            if set(request) != {'op', 'invite_id', 'envelope'} or self.material_registry is None:
                raise ControlError('invalid_control')
            status = self._consent_control({'op': 'status', 'invite_id': request['invite_id']})
            if status['status'] != 'ready':
                raise ControlError('both_consents_required')
            envelope = request['envelope']
            if not isinstance(envelope, dict) or set(envelope) != {'scope', 'grant', 'catalog', 'signature'}:
                raise ControlError('invalid_control')
            unsigned = {k: v for k, v in envelope.items() if k != 'signature'}
            signed = dict(unsigned, purpose='joining-owner-material-v1')
            if envelope['scope'] != status['scope']:
                raise ControlError('invalid_control')
            verify(self.joining_owner_key, signed, envelope['signature'], signed)
            return self.material_registry.accept(status['scope'], envelope)
        if isinstance(op, str) and op in {'status', 'joining_consent'}:
            return self._consent_control(request)
        fields = {'op', 'invite_id', 'secret'} if op == 'redeem' else {'op', 'invite_id'}
        if set(request) != fields or op not in {'preview', 'redeem'}:
            raise ControlError('invalid_control')
        invite_id = request['invite_id']
        if not isinstance(invite_id, str) or not re.fullmatch(r'invite-[a-zA-Z0-9-]{1,64}', invite_id):
            raise ControlError('invalid_control')
        if op == 'redeem':
            result = self.store.redeem(self.lobby_id, invite_id, request['secret'], binding=self.binding)
            return {'status': result['status']}
        with self.store._connection() as db:
            row = db.execute("SELECT l.name,i.expires_at FROM invitations i JOIN lobbies l USING(lobby_id) WHERE i.lobby_id=? AND invite_id=? AND i.state='open' AND i.expires_at>?", (self.lobby_id, invite_id, self.store.clock())).fetchone()
        if row is None:
            raise ControlError('invite_unavailable')
        return dict(row)
