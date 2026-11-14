"""Ephemeral loopback-only S2 owner surface. Never imported by live MC."""
from __future__ import annotations
import hmac
import os
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from lobby_control import ControlError, decode, encode, MAX_PAYLOAD
from room_invite_store import InviteError


class HostOwner:
    """Fixed provisioned lobby/peer, callable only behind local OwnerSurface."""
    def __init__(self, store, lobby_id, binding):
        store._validate_binding(binding)
        self.store, self.lobby_id, self.binding = store, lobby_id, dict(binding)

    def approve(self, payload):
        if (set(payload) != {'lobby_id', 'invite_id'} or payload['lobby_id'] != self.lobby_id
                or not isinstance(payload['invite_id'], str)):
            raise ControlError('invalid_owner_scope')
        invite_id = payload['invite_id']
        with self.store._write() as db:
            self.store._open_lobby(db, self.lobby_id)
            row = db.execute("SELECT r.*,i.state FROM invitations i JOIN redemptions r USING(invite_id) WHERE i.lobby_id=? AND i.invite_id=?", (self.lobby_id, invite_id)).fetchone()
            if (row is None or row['state'] != 'redeemed'
                    or any(row[k] != v for k, v in self.binding.items())
                    or row['host_decision'] not in {'pending', 'approved'}):
                raise ControlError('owner_operation_blocked')
            db.execute("UPDATE redemptions SET host_decision='approved',decided_at=? WHERE invite_id=? AND host_decision='pending'", (self.store.clock(), invite_id))
        return {'status': self.store.get_redemption(self.lobby_id, invite_id)['status']}


class JoiningOwner:
    """Private local-owner workflow. Delivers only durable, signed consent."""
    def __init__(self, root, control, lobby_id, binding, host_id):
        import sqlite3
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat, PrivateFormat, NoEncryption
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        keyfile = root / 'consent.key'
        try:
            fd = os.open(keyfile, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            fd = os.open(keyfile, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, 'rb') as f:
                self._key = Ed25519PrivateKey.from_private_bytes(f.read(33))
        else:
            self._key = Ed25519PrivateKey.generate()
            with os.fdopen(fd, 'wb') as f:
                f.write(self._key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption()))
                f.flush()
                os.fsync(f.fileno())
        self.public_key = self._key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        self.path = root / 'consents.sqlite'
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        with self._db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS consents(invite_id TEXT PRIMARY KEY, receipt TEXT NOT NULL, signature TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS confirmed_scope(singleton INTEGER PRIMARY KEY CHECK(singleton=1), scope TEXT NOT NULL)')
        self.control, self.lobby_id, self.binding, self.host_id = control, lobby_id, dict(binding), host_id

    def confirm_scope(self, room_id, member_id):
        """Owner-private manual provisioning, never an HTTP/control operation."""
        import re
        from lobby_consent import canonical
        if (not isinstance(room_id, str) or not re.fullmatch(r'invite-room-[A-Za-z0-9-]{1,64}', room_id)
                or not isinstance(member_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}', member_id)):
            raise ControlError('invalid_owner_scope')
        encoded = canonical({'room_id': room_id, 'member_id': member_id, 'lobby_id': self.lobby_id,
                             'host_id': self.host_id, 'binding': self.binding}).decode()
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT scope FROM confirmed_scope WHERE singleton=1').fetchone()
            if row is None:
                db.execute('INSERT INTO confirmed_scope VALUES(1,?)', (encoded,))
            elif row[0] != encoded:
                raise ControlError('consent_scope_conflict')
            db.commit()

    def _db(self):
        from contextlib import closing
        import sqlite3
        return closing(sqlite3.connect(self.path, isolation_level=None, timeout=5))

    def has_consent(self, invite_id):
        with self._db() as db:
            return db.execute('SELECT 1 FROM consents WHERE invite_id=?', (invite_id,)).fetchone() is not None

    def consent(self, payload):
        import json
        import base64
        from lobby_consent import canonical
        if (set(payload) != {'lobby_id', 'invite_id'} or payload['lobby_id'] != self.lobby_id
                or not isinstance(payload['invite_id'], str)):
            raise ControlError('invalid_owner_scope')
        invite_id = payload['invite_id']
        with self._db() as db:
            confirmed = db.execute('SELECT scope FROM confirmed_scope WHERE singleton=1').fetchone()
        if confirmed is None:
            raise ControlError('owner_scope_required')
        expected_scope = dict(json.loads(confirmed[0]), invite_id=invite_id)
        status = self.control.call({'op': 'status', 'invite_id': invite_id})
        scope = status.get('scope')
        if (status.get('status') not in {'consent_required', 'ready'} or scope != expected_scope):
            raise ControlError('host_approval_required')
        receipt = dict(scope, purpose='joining-owner-consent-v1')
        encoded = canonical(receipt).decode()
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT receipt,signature FROM consents WHERE invite_id=?', (invite_id,)).fetchone()
            if row is None:
                signature = base64.b64encode(self._key.sign(encoded.encode())).decode()
                db.execute('INSERT INTO consents VALUES(?,?,?)', (invite_id, encoded, signature))
            elif row[0] != encoded:
                raise ControlError('consent_scope_conflict')
            else:
                signature = row[1]
            db.commit()  # FULL SQLite commit before any consent leaves this owner.
        return self.control.call({'op': 'joining_consent', 'invite_id': invite_id,
                                  'receipt': json.loads(encoded), 'signature': signature})


class OwnerSurface:
    def __init__(self, root, operations):
        if not operations or not set(operations) <= {'approve', 'consent', 'mint', 'activate', 'send',
                                                       'inventory', 'quarantine', 'reconcile'}:
            raise ControlError('invalid_owner_operations')
        self.operations = dict(operations)
        root.mkdir(parents=True, exist_ok=True)
        self.token = secrets.token_urlsafe(32)
        fd = os.open(root / 'owner.token', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as f:
            f.write(self.token)
            f.flush()
            os.fsync(f.fileno())
        surface = self
        class Handler(BaseHTTPRequestHandler):
            def setup(self):
                super().setup()
                self.connection.settimeout(10)

            def log_message(self, *args):
                pass

            def reply(self, status, value):
                raw = encode(value)
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Referrer-Policy', 'no-referrer')
                self.send_header('Connection', 'close')
                self.end_headers()
                self.wfile.write(raw)
                self.close_connection = True

            def do_POST(self):
                auth = self.headers.get_all('Authorization', [])
                if (self.path != '/owner' or self.headers.get('Origin') is not None
                        or len(auth) != 1 or not auth[0].isascii()
                        or not hmac.compare_digest(auth[0], 'Bearer ' + surface.token)):
                    self.reply(403, {'error': 'owner_required'})
                    return
                lengths = self.headers.get_all('Content-Length', [])
                try:
                    if (len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit()
                            or not 0 < int(lengths[0]) <= MAX_PAYLOAD or self.headers.get('Transfer-Encoding')
                            or self.headers.get('Content-Type') != 'application/json'):
                        raise ControlError('invalid_control')
                    body = self.rfile.read(int(lengths[0]))
                    if len(body) != int(lengths[0]):
                        raise ControlError('invalid_control')
                    value = decode(body)
                    op = value.pop('op', None)
                    if not isinstance(op, str) or op not in surface.operations:
                        raise ControlError('invalid_control')
                    result = surface.operations[op](value)
                    self.reply(200, result)
                except ControlError as exc:
                    from lobby_material import MintAmbiguous
                    from lobby_activation import OperationAmbiguous
                    from lobby_reconcile import OwnerConfirmationRequired, ReconcileBlocked, ScopeQuarantined
                    if isinstance(exc, OwnerConfirmationRequired):
                        self.reply(403, {'error': 'owner_confirmation_required'})
                        return
                    code = ('mint_ambiguous' if isinstance(exc, MintAmbiguous) else
                            str(exc) if isinstance(exc, (OperationAmbiguous, ScopeQuarantined)) else
                            exc.code if isinstance(exc, ReconcileBlocked) else
                            str(exc) if str(exc) in {'unknown_scope', 'scope_not_orphan', 'scope_not_quarantined'}
                            else 'owner_operation_blocked')
                    self.reply(409, {'error': code})
                except InviteError:
                    self.reply(409, {'error': 'owner_operation_blocked'})
                except Exception:
                    self.reply(502, {'error': 'owner_operation_ambiguous'})

            def do_GET(self):
                self.reply(403, {'error': 'owner_required'})
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = False
        self.thread = threading.Thread(target=self.server.serve_forever, name='s2-local-owner', daemon=True)
        self.base = f'http://127.0.0.1:{self.server.server_address[1]}'

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(15)
        if self.thread.is_alive():
            raise ControlError('owner_teardown_failed')
