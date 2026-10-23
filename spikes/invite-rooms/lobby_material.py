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


class _LabMaterialStore:
    """Storage-shape adapter; policy and mint transitions stay product-owned."""
    def __init__(self, join, journal):
        self.join, self.journal = join, journal

    def find_import(self, lobby_id, invite_id):
        if lobby_id != self.join.lobby_id:
            return None
        return {"import_id": invite_id, "host_peer_id": self.join.host_id}

    def confirmed_scope(self, _import_id):
        with self.join._db() as db:
            row = db.execute('SELECT scope FROM confirmed_scope WHERE singleton=1').fetchone()
        return json.loads(row[0]) if row else None

    def get_consent(self, import_id):
        with self.join._db() as db:
            row = db.execute('SELECT receipt,signature FROM consents WHERE invite_id=?', (import_id,)).fetchone()
        return {"receipt": json.loads(row[0]), "signature": row[1]} if row else None

    def mint_state(self, scope_key):
        return self.journal.state(json.loads(scope_key))

    def mint_begin(self, scope_key):
        try:
            return self.journal.begin(json.loads(scope_key))
        except MintAmbiguous:
            raise

    def mint_complete(self, scope_key, value):
        self.journal.complete(json.loads(scope_key), json.loads(value))

    def mint_load(self, scope_key):
        return canonical(self.journal.load(json.loads(scope_key)))


class _LabConsentKey:
    def __init__(self, join):
        self.join = join

    def sign(self, value):
        import base64
        return base64.b64encode(self.join._key.sign(canonical(value))).decode()


class TargetMint:
    """Lab transport/storage adapter over product TargetMaterialMint."""
    def __init__(self, joining_owner, journal, rpc):
        from invite_rooms.joining_material import TargetMaterialMint
        self.join = joining_owner
        self.journal = journal
        self.rpc = rpc
        self._product = None if joining_owner is None else TargetMaterialMint(
            _LabMaterialStore(joining_owner, journal), consent_key=_LabConsentKey(joining_owner),
            channel=joining_owner.control, rpc=rpc, host_peer_id=joining_owner.host_id,
        )

    async def mint(self, payload):
        import asyncio
        if self._product is None:
            from lobby_reconcile import refuse_if_quarantined
            # The test/lab seam may supply the approved scope directly.  The
            # quarantine guard must precede the native mint path regardless.
            resolver = getattr(self, 'approved_scope', None)
            if resolver is None:
                raise ControlError('material_unavailable')
            scope = await asyncio.to_thread(resolver, payload)
            refuse_if_quarantined(self.journal, scope)
            raise ControlError('material_unavailable')
        from lobby_reconcile import refuse_if_quarantined
        # Resolve the exact durable scope before entering the product mint;
        # quarantined scopes are refused before any native RPC can occur.
        resolver = getattr(self._product, 'approved_scope', None)
        if resolver is not None:
            try:
                scope = await asyncio.to_thread(resolver, payload)
            except _ProductMintAmbiguous:
                raise MintAmbiguous() from None
            except _ProductMaterialError as exc:
                raise ControlError(str(exc)) from None
            refuse_if_quarantined(self.journal, scope)
        try:
            return await self._product.mint(payload)
        except _ProductMintAmbiguous:
            raise MintAmbiguous() from None
        except _ProductMaterialError as exc:
            raise ControlError(str(exc)) from None
        except MintAmbiguous:
            raise
        except ControlError:
            raise
        except Exception:
            raise


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
