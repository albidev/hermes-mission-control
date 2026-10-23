"""Owner-private criterion-4 material. No production imports or activation.

A journal intent is a one-shot permission to ATTEMPT a native mint, not success.
No supported native API recovers a lost bearer; interrupted intents stay blocked.
"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import stat

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from lobby_control import ControlError, encode, decode
from lobby_consent import canonical


class MintAmbiguous(ControlError):
    def __init__(self):
        super().__init__('mint_ambiguous')


class MintJournal:
    """Private 0600 encrypted journal; SQLite serializes cross-process claims."""
    def __init__(self, root):
        import fcntl
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / 'material.sqlite'
        lockfd = os.open(self.root / 'initialize.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(lockfd, 'rb') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            keyfile = self.root / 'material.key'
            try:
                fd = os.open(keyfile, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            except FileExistsError:
                pass
            else:
                with os.fdopen(fd, 'wb') as out:
                    out.write(AESGCM.generate_key(bit_length=256))
                    out.flush()
                    os.fsync(out.fileno())
            fd = os.open(keyfile, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, 'rb') as inp:
                if stat.S_IMODE(os.fstat(inp.fileno()).st_mode) != 0o600:
                    raise ControlError('private_storage_required')
                key = inp.read(33)
            if len(key) != 32:
                raise ControlError('private_storage_required')
            self._cipher = AESGCM(key)
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            if stat.S_IMODE(os.fstat(fd).st_mode) != 0o600:
                os.close(fd)
                raise ControlError('private_storage_required')
            os.close(fd)
            with self._db() as db:
                db.execute('CREATE TABLE IF NOT EXISTS material(scope TEXT PRIMARY KEY, grant_id TEXT NOT NULL, state TEXT NOT NULL, ciphertext BLOB)')
            dfd = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, isolation_level=None, timeout=5)
        try:
            db.execute('PRAGMA synchronous=FULL')
            db.execute('PRAGMA fullfsync=1')
            yield db
        finally:
            db.close()

    @staticmethod
    def _scope(scope):
        return canonical(scope).decode()

    def begin(self, scope):
        encoded = self._scope(scope)
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM material WHERE scope=?', (encoded,)).fetchone():
                raise MintAmbiguous()
            grant_id = 'grant-' + secrets.token_hex(16)
            db.execute('INSERT INTO material VALUES(?,?,?,NULL)', (encoded, grant_id, 'intent'))
            db.commit()
        return grant_id

    def state(self, scope):
        with self._db() as db:
            row = db.execute('SELECT state FROM material WHERE scope=?', (self._scope(scope),)).fetchone()
        return row[0] if row else None

    def complete(self, scope, value):
        encoded = self._scope(scope)
        nonce = os.urandom(12)
        ciphertext = nonce + self._cipher.encrypt(nonce, encode(value), encoded.encode())
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute("UPDATE material SET state='ready',ciphertext=? WHERE scope=? AND state='intent'", (ciphertext, encoded)).rowcount != 1:
                raise ControlError('material_unavailable')
            db.commit()

    def load(self, scope):
        encoded = self._scope(scope)
        with self._db() as db:
            row = db.execute("SELECT ciphertext FROM material WHERE scope=? AND state='ready'", (encoded,)).fetchone()
        if row is None:
            raise ControlError('material_unavailable')
        try:
            blob = row[0]
            return decode(self._cipher.decrypt(blob[:12], blob[12:], encoded.encode()))
        except Exception:
            raise ControlError('material_unavailable') from None


def validate_material(scope, value):
    """Scope hygiene only; native target alone can verify S4's HMAC authority."""
    import base64
    import sys
    import time
    core = '/Users/albi/.hermes/hermes-agent'
    if core not in sys.path:
        sys.path.append(core)  # Public native schema, read only; never production config.
    try:
        from gateway.hosted_room_peer import GatewayRoomCatalog
        if set(value) != {'scope', 'grant', 'catalog', 'signature'} or value['scope'] != scope:
            raise ValueError()
        encode(value)
        token = value['grant']
        if not isinstance(token, str) or not 0 < len(token) <= 8192:
            raise ValueError()
        payload, signature = token.split('.')
        if len(base64.urlsafe_b64decode(signature + '=' * (-len(signature) % 4))) != 32:
            raise ValueError()
        claims = decode(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)))
        binding = scope['binding']
        expected = {'room_id': scope['room_id'], 'home_install_id': scope['host_id'],
                    'authority_gateway_id': scope['host_id'], 'authority_epoch': 1,
                    'member_id': scope['member_id'], 'target_install_id': binding['target_install_id'],
                    'target_profile': binding['target_profile']}
        if any(claims.get(k) != v for k, v in expected.items()):
            raise ValueError()
        if not claims['issued_at'] < claims['expires_at'] <= claims['status_expires_at'] or not claims['issued_at'] - 30 <= time.time() < claims['expires_at']:
            raise ValueError()
        catalog = GatewayRoomCatalog.from_mapping(value['catalog'])
        if (catalog.installation_id != binding['target_install_id'] or catalog.execution_policy.target_profile != binding['target_profile']
                or catalog.execution_policy.policy_digest != claims['execution_policy_digest']):
            raise ValueError()
    except Exception:
        raise ControlError('material_unavailable') from None


class PrivateRegistry:
    """Home-side private resolver for ONE approved joining-peer scope."""
    def __init__(self, root, scope, adapter_url):
        from room_lobby_service import _validate_adapter
        _validate_adapter(adapter_url)
        self.scope = json.loads(canonical(scope))
        self.adapter_url = adapter_url
        self.storage = MintJournal(root)

    def accept(self, scope, envelope):
        if scope != self.scope:
            raise ControlError('material_unavailable')
        validate_material(scope, envelope)
        # Transactionally insert complete ciphertext; delivery retry is byte-identical.
        encoded = self.storage._scope(scope)
        nonce = os.urandom(12)
        blob = nonce + self.storage._cipher.encrypt(nonce, encode(envelope), encoded.encode())
        with self.storage._db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT state FROM material WHERE scope=?', (encoded,)).fetchone()
            if row:
                if row[0] != 'ready' or self.storage.load(scope) != envelope:
                    raise ControlError('material_conflict')
            else:
                db.execute('INSERT INTO material VALUES(?,?,?,?)', (encoded, 'received', 'ready', blob))
            db.commit()
        return {'status': 'material_ready'}

    async def resolve(self, peer):
        from room_lobby_service import PeerMaterial
        # Require explicit room/lobby/member/native binding, never a global grant lookup.
        expected = dict(self.scope['binding'], lobby_id=self.scope['lobby_id'], member_id=self.scope['member_id'], room_id=self.scope['room_id'])
        if peer != expected:
            raise ControlError('material_unavailable')
        envelope = self.storage.load(self.scope)
        validate_material(self.scope, envelope)
        return PeerMaterial(binding=dict(self.scope['binding']), catalog=envelope['catalog'],
                            grant=envelope['grant'], adapter_url=self.adapter_url)


class TargetMint:
    """B local-owner only; never accepts RPC parameters or URLs from a message."""
    def __init__(self, joining_owner, journal, rpc):
        self.join = joining_owner
        self.journal = journal
        self.rpc = rpc

    def approved_scope(self, payload):
        if (set(payload) != {'lobby_id', 'invite_id'} or payload['lobby_id'] != self.join.lobby_id
                or not isinstance(payload['invite_id'], str)):
            raise ControlError('invalid_owner_scope')
        with self.join._db() as db:
            row = db.execute('SELECT receipt FROM consents WHERE invite_id=?', (payload['invite_id'],)).fetchone()
            confirmed = db.execute('SELECT scope FROM confirmed_scope WHERE singleton=1').fetchone()
        if row is None or confirmed is None:
            raise ControlError('both_consents_required')
        scope = dict(json.loads(confirmed[0]), invite_id=payload['invite_id'])
        if json.loads(row[0]) != dict(scope, purpose='joining-owner-consent-v1'):
            raise ControlError('both_consents_required')
        status = self.join.control.call({'op': 'status', 'invite_id': payload['invite_id']})
        if status != {'status': 'ready', 'scope': scope}:
            raise ControlError('both_consents_required')
        return scope

    async def mint(self, payload):
        import asyncio
        import base64
        scope = await asyncio.to_thread(self.approved_scope, payload)
        from lobby_reconcile import refuse_if_quarantined
        refuse_if_quarantined(self.journal, scope)  # typed block, never replay
        state = self.journal.state(scope)
        if state == 'ready':
            envelope = self.journal.load(scope)
        else:
            grant_id = self.journal.begin(scope)  # Durable exclusive intent BEFORE call.
            try:
                binding = scope['binding']
                result = await self.rpc('groups.peer.invite', {
                    'profile': binding['target_profile'], 'room_id': scope['room_id'],
                    'home_install_id': scope['host_id'], 'authority_gateway_id': scope['host_id'],
                    'authority_epoch': 1, 'member_id': scope['member_id'], 'grant_id': grant_id, 'ttl_seconds': 600})
                if set(result) != {'grant', 'target_profile', 'catalog', 'endpoint'} or result['target_profile'] != binding['target_profile'] or result['endpoint'] != result['catalog'].get('endpoint'):
                    raise ValueError()
                envelope = {'scope': scope, 'grant': result['grant'], 'catalog': result['catalog'], 'signature': ''}
                validate_material(scope, envelope)
                unsigned = {k: v for k, v in envelope.items() if k != 'signature'}
                envelope['signature'] = base64.b64encode(self.join._key.sign(canonical(dict(unsigned, purpose='joining-owner-material-v1')))).decode()
                self.journal.complete(scope, envelope)
            except asyncio.CancelledError:
                raise  # Intent remains blocked, never replay.
            except Exception:
                raise MintAmbiguous() from None
        validate_material(scope, envelope)
        return await asyncio.to_thread(self.join.control.call, {'op': 'material', 'invite_id': scope['invite_id'], 'envelope': envelope})


# Compatibility adapter: durable journal and material validation live in the
# product package. The remaining code in this module is lab-only glue.
from pathlib import Path as _Path
import sys as _sys
_SERVER = _Path(__file__).resolve().parents[2] / 'server'
if str(_SERVER) not in _sys.path:
    _sys.path.insert(0, str(_SERVER))
from invite_rooms.joining_material import (MintJournal as _ProductMintJournal,
                                            MintAmbiguous as _ProductMintAmbiguous,
                                            MaterialError as _ProductMaterialError,
                                            validate_material as _product_validate_material)
from lobby_control import ControlError as _ControlError

class MintAmbiguous(_ControlError):
    def __init__(self):
        super().__init__('mint_ambiguous')

class MintJournal(_ProductMintJournal):
    """Lab exception-shape adapter over the product-owned journal."""
    def load(self, scope):
        try:
            return super().load(scope)
        except _ProductMaterialError:
            raise _ControlError('material_unavailable') from None

    def begin(self, scope):
        try:
            return super().begin(scope)
        except _ProductMintAmbiguous:
            raise MintAmbiguous() from None

    def complete(self, scope, value):
        try:
            return super().complete(scope, value)
        except _ProductMaterialError:
            raise _ControlError('material_unavailable') from None

def validate_material(scope, value):
    try:
        _product_validate_material(scope, value)
    except Exception:
        raise _ControlError('material_unavailable') from None
