"""Target-side (joining-owner) handlers exercised during host activation: a
consent-status fetch and a scoped material mint, matching the accepted TargetMint/MintJournal contract.

Pure handler functions with exact scope checks against durable local state.
The only writable input from an inbound message is
`payload={'lobby_id','invite_id'}`; every other value used to mint or sign
material comes from already-verified local state (the confirmed scope and
consent receipt produced by invite_rooms.joining.JoiningOwnerService). The
mint journal is durable and encrypted: ambiguity stays visible and is never
silently retried with a new grant_id.
"""
from __future__ import annotations

import asyncio
import base64
import json
import time
import os
import sqlite3
import stat
from contextlib import contextmanager
from pathlib import Path
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from invite_rooms.consent import canonical

# Six-field self-binding only: the product service never accepts the lab's
# legacy four-field binding (that adapter stays confined to the real-lab
# parity test fixture). catalog_digest/policy_digest are the digests the
# owner pinned when this installation's self_binding was configured; they
# are compared against the LIVE catalog returned by the mint, independent
# of (and in addition to) the grant's own embedded policy digest (R2).
_SIX_FIELD = {"endpoint_id", "fingerprint", "target_install_id", "target_profile",
              "catalog_digest", "policy_digest"}


class MintAmbiguous(ValueError):
    def __init__(self):
        super().__init__("mint_ambiguous")


class MaterialError(ValueError):
    """Machine code only: never interpolates remote values or secrets."""


class MintJournal:
    """Product-owned durable one-shot mint journal, also used by the lab adapter."""
    def __init__(self, root):
        import fcntl
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if stat.S_IMODE(os.stat(self.root).st_mode) != 0o700:
            raise MaterialError("private_storage_required")
        self.path = self.root / "material.sqlite"
        lockfd = os.open(self.root / "initialize.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(lockfd, "rb") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            keyfile = self.root / "material.key"
            try:
                fd = os.open(keyfile, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            except FileExistsError:
                pass
            else:
                with os.fdopen(fd, "wb") as out:
                    out.write(AESGCM.generate_key(bit_length=256)); out.flush(); os.fsync(out.fileno())
            fd = os.open(keyfile, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as inp:
                if stat.S_IMODE(os.fstat(inp.fileno()).st_mode) != 0o600:
                    raise MaterialError("private_storage_required")
                key = inp.read(33)
            if len(key) != 32:
                raise MaterialError("private_storage_required")
            self._cipher = AESGCM(key)
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            if stat.S_IMODE(os.fstat(fd).st_mode) != 0o600:
                os.close(fd); raise MaterialError("private_storage_required")
            os.close(fd)
            with self._db() as db:
                db.execute("CREATE TABLE IF NOT EXISTS material(scope TEXT PRIMARY KEY, grant_id TEXT NOT NULL, state TEXT NOT NULL, ciphertext BLOB)")

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, isolation_level=None, timeout=5)
        try:
            db.execute("PRAGMA synchronous=FULL"); db.execute("PRAGMA fullfsync=1")
            yield db
        finally:
            db.close()

    @staticmethod
    def _scope(scope):
        return canonical(scope).decode()

    def begin(self, scope):
        import secrets
        encoded = self._scope(scope)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM material WHERE scope=?", (encoded,)).fetchone():
                raise MintAmbiguous()
            grant_id = "grant-" + secrets.token_hex(16)
            db.execute("INSERT INTO material VALUES(?,?,?,NULL)", (encoded, grant_id, "intent")); db.commit()
        return grant_id

    def state(self, scope):
        with self._db() as db:
            row = db.execute("SELECT state FROM material WHERE scope=?", (self._scope(scope),)).fetchone()
        return row[0] if row else None

    def complete(self, scope, value):
        encoded = self._scope(scope); nonce = os.urandom(12)
        ciphertext = nonce + self._cipher.encrypt(nonce, canonical(value), encoded.encode())
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("UPDATE material SET state='ready',ciphertext=? WHERE scope=? AND state='intent'", (ciphertext, encoded)).rowcount != 1:
                raise MaterialError("material_unavailable")
            db.commit()

    def load(self, scope):
        encoded = self._scope(scope)
        with self._db() as db:
            row = db.execute("SELECT ciphertext FROM material WHERE scope=? AND state='ready'", (encoded,)).fetchone()
        if row is None:
            raise MaterialError("material_unavailable")
        try:
            blob = row[0]
            return json.loads(self._cipher.decrypt(blob[:12], blob[12:], encoded.encode()))
        except Exception:
            raise MaterialError("material_unavailable") from None


def _native_catalog(value):
    """Use the canonical core RoomLink validator; never trust caller digests."""
    try:
        from gateway.hosted_room_peer import GatewayRoomCatalog
    except ImportError:
        raise MaterialError("native_contract_unavailable") from None
    try:
        return GatewayRoomCatalog.from_mapping(value)
    except Exception:
        raise MaterialError("invalid_catalog") from None


def validate_material(scope: dict, envelope: dict) -> None:
    """Scope hygiene only; native target alone can verify the grant's own
    signature authority. Rejects anything that does not describe exactly
    this scope's room/member/installation/profile/policy."""
    try:
        if not isinstance(envelope, dict) or set(envelope) != {"scope", "grant", "catalog", "signature"}:
            raise ValueError()
        if envelope["scope"] != scope:
            raise ValueError()
        token = envelope["grant"]
        if not isinstance(token, str) or not 0 < len(token) <= 8192 or token.count(".") != 1:
            raise ValueError()
        payload_b64, signature_b64 = token.split(".")
        if len(base64.urlsafe_b64decode(signature_b64 + "=" * (-len(signature_b64) % 4))) != 32:
            raise ValueError()
        claims = json.loads(base64.urlsafe_b64decode(payload_b64 + "=" * (-len(payload_b64) % 4)))
        binding = scope["binding"]
        if set(binding) != _SIX_FIELD:
            raise ValueError()
        expected = {
            "room_id": scope["room_id"], "home_install_id": scope["host_id"],
            "authority_gateway_id": scope["host_id"], "authority_epoch": 1,
            "member_id": scope["member_id"], "target_install_id": binding["target_install_id"],
            "target_profile": binding["target_profile"],
        }
        if any(claims.get(k) != v for k, v in expected.items()):
            raise ValueError()
        if not claims["issued_at"] < claims["expires_at"] <= claims["status_expires_at"]:
            raise ValueError()
        if not claims["issued_at"] - 30 <= time.time() < claims["expires_at"]:
            raise ValueError()
        catalog = _native_catalog(envelope["catalog"])
        if (catalog.installation_id != binding["target_install_id"]
                or catalog.execution_policy.target_profile != binding["target_profile"]
                or catalog.execution_policy.policy_digest != claims["execution_policy_digest"]
                # Pin against the OWNER-CONFIRMED digests, not just the
                # grant's own self-reported policy digest: a catalog+policy
                # that changed together, internally consistently, must still
                # be rejected before signing/delivering (R2).
                or catalog.catalog_digest != binding["catalog_digest"]
                or catalog.execution_policy.policy_digest != binding["policy_digest"]
                # MVP scope is fixed text-only/manual: never sign material
                # describing a wider capability than this.
                or catalog.execution_policy.approval_mode != "manual"
                or not catalog.text or catalog.attachments):
            raise ValueError()
    except MaterialError:
        raise
    except Exception:
        raise MaterialError("material_unavailable") from None


class TargetMaterialMint:
    """B-local-owner only: never accepts RPC parameters or URLs from a message."""

    def __init__(self, store, *, consent_key, channel, rpc, host_peer_id):
        self._store = store
        self._consent_key = consent_key
        self._channel = channel
        self._rpc = rpc
        self._host_peer_id = host_peer_id

    def _resolve_import(self, payload: dict) -> str:
        if (set(payload) != {"lobby_id", "invite_id"} or not isinstance(payload["lobby_id"], str)
                or not isinstance(payload["invite_id"], str)):
            raise MaterialError("invalid_owner_scope")
        record = self._store.find_import(payload["lobby_id"], payload["invite_id"])
        if record is None or record["host_peer_id"] != self._host_peer_id:
            raise MaterialError("invalid_owner_scope")
        return record["import_id"]

    def approved_scope(self, payload: dict) -> dict:
        import_id = self._resolve_import(payload)
        confirmed = self._store.confirmed_scope(import_id)
        consent = self._store.get_consent(import_id)
        if confirmed is None or consent is None:
            raise MaterialError("both_consents_required")
        scope = dict(confirmed, invite_id=payload["invite_id"])
        if consent["receipt"] != dict(scope, purpose="joining-owner-consent-v1"):
            raise MaterialError("both_consents_required")
        try:
            status = self._channel.call({"op": "status", "invite_id": payload["invite_id"]})
        except Exception:
            raise MaterialError("control_unavailable") from None
        if status != {"status": "ready", "scope": scope}:
            raise MaterialError("both_consents_required")
        return scope

    async def mint(self, payload: dict) -> dict:
        scope = await asyncio.to_thread(self.approved_scope, payload)
        scope_key = canonical(scope).decode()
        state = self._store.mint_state(scope_key)
        if state == "ready":
            envelope = self._load_envelope(scope_key, scope)
        else:
            if state is not None:
                # 'intent' with no completion left over from a prior crash:
                # the mint stays blocked, it is never silently retried.
                raise MintAmbiguous()
            grant_id = self._store.mint_begin(scope_key)  # durable exclusive intent BEFORE the call
            try:
                binding = scope["binding"]
                result = await self._rpc("groups.peer.invite", {
                    "profile": binding["target_profile"], "room_id": scope["room_id"],
                    "home_install_id": scope["host_id"], "authority_gateway_id": scope["host_id"],
                    "authority_epoch": 1, "member_id": scope["member_id"],
                    "grant_id": grant_id, "ttl_seconds": 600,
                })
                # The native groups.peer.invite contract returns 'endpoint'
                # as the SAME mapping that appears at catalog['endpoint']
                # (tui_gateway/methods_groups.py: `"endpoint": catalog["endpoint"]`),
                # never a bare URL string. Comparing against a URL made a
                # valid real mint permanently un-completable (R1).
                result_catalog = result.get("catalog")
                if (set(result) != {"grant", "target_profile", "catalog", "endpoint"}
                        or result["target_profile"] != binding["target_profile"]
                        or not isinstance(result_catalog, dict)
                        or "endpoint" not in result_catalog
                        or not isinstance(result["endpoint"], dict)
                        or result["endpoint"] != result_catalog["endpoint"]):
                    raise ValueError()
                envelope = {"scope": scope, "grant": result["grant"], "catalog": result["catalog"], "signature": ""}
                validate_material(scope, envelope)
                unsigned = {k: v for k, v in envelope.items() if k != "signature"}
                signed = dict(unsigned, purpose="joining-owner-material-v1")
                envelope["signature"] = self._consent_key.sign(signed)
                self._store.mint_complete(scope_key, canonical(envelope))
            except asyncio.CancelledError:
                raise  # Intent remains durably blocked, never replayed.
            except Exception:
                raise MintAmbiguous() from None
        validate_material(scope, envelope)
        try:
            return await asyncio.to_thread(
                self._channel.call, {"op": "material", "invite_id": scope["invite_id"], "envelope": envelope}
            )
        except Exception:
            raise MaterialError("control_unavailable") from None

    def _load_envelope(self, scope_key: str, scope: dict) -> dict:
        import json as _json
        raw = self._store.mint_load(scope_key)
        envelope = _json.loads(raw)
        if envelope.get("scope") != scope:
            raise MaterialError("material_unavailable")
        return envelope
