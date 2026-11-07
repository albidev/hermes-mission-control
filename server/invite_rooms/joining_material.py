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
import inspect
from contextlib import contextmanager
from pathlib import Path
from invite_rooms.anchored_sqlite import AnchoredSQLite
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
    def __init__(self, root, *, anchor=None):
        self.root = Path(root)
        self.path = self.root / "material.sqlite"
        self._storage = AnchoredSQLite(root, "material.sqlite", "material.key", MaterialError, anchor=anchor)
        try:
            with self._storage.initialize(self._check_schema) as (fresh, cipher):
                self._cipher = cipher
                with self._storage.connection(timeout=5) as db:
                    if fresh:
                        db.execute("CREATE TABLE material(scope TEXT PRIMARY KEY, grant_id TEXT NOT NULL, state TEXT NOT NULL, ciphertext BLOB)")
                    self._check_schema(db)
        except BaseException:
            self.close()
            raise

    def close(self):
        if getattr(self, '_storage', None) is not None:
            self._storage.close()

    @staticmethod
    def _check_schema(db):
        try:
            objects = {(r[0], r[1]) for r in db.execute(
                "SELECT type,name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")}
            layout = tuple((r[1], r[2], r[3], r[5]) for r in db.execute('PRAGMA table_info(material)'))
            indexes = list(db.execute('PRAGMA index_list(material)'))
            # Material's exact layout is the storage contract. Existing callers
            # also keep their operation/reconciliation tables in this database;
            # those do not make an otherwise valid material journal foreign.
            if (('table', 'material') not in objects
                    or layout != (('scope', 'TEXT', 0, 1), ('grant_id', 'TEXT', 1, 0),
                                  ('state', 'TEXT', 1, 0), ('ciphertext', 'BLOB', 0, 0))
                    or len(indexes) != 1 or indexes[0][2] != 1
                    or tuple(r[2] for r in db.execute(f'PRAGMA index_info({indexes[0][1]!r})')) != ('scope',)
                    or list(db.execute('PRAGMA foreign_key_list(material)'))):
                raise MaterialError('material_unavailable')
        except sqlite3.DatabaseError:
            raise MaterialError('material_unavailable') from None

    @contextmanager
    def _db(self):
        with self._storage.connection(timeout=5) as db:
            self._check_schema(db)
            db.execute("PRAGMA synchronous=FULL")
            db.execute("PRAGMA fullfsync=1")
            yield db

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
        if not isinstance(envelope, dict):
            raise ValueError()
        version = envelope.get("v", 1)
        if version == 1:
            if set(envelope) != {"scope", "grant", "catalog", "signature"}:
                raise ValueError()
        elif version == 2:
            if (set(envelope) != {"v", "scope", "grant", "catalog", "operation", "signature"}
                    or envelope["v"] != 2 or not isinstance(envelope.get("operation"), dict)
                    or set(envelope["operation"]) != {"endpoint_id", "generation", "lobby_id", "invite_id",
                                                       "request_id", "expires_at_utc_ms"}):
                raise ValueError()
            if (envelope["operation"]["lobby_id"] != scope["lobby_id"]
                    or envelope["operation"]["invite_id"] != scope["invite_id"]
                    or type(envelope["operation"]["generation"]) is not int
                    or type(envelope["operation"]["expires_at_utc_ms"]) is not int
                    or not isinstance(envelope["operation"]["request_id"], str)):
                raise ValueError()
        else:
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


def _store_call(method, *args, deadline=None, **kwargs):
    """Call old lab stores and deadline-aware product stores alike."""
    try:
        parameters = inspect.signature(method).parameters
    except (TypeError, ValueError):
        parameters = {}
    if "deadline" in parameters:
        kwargs["deadline"] = deadline
    if "final_admission" not in parameters:
        kwargs.pop("final_admission", None)
    return method(*args, **kwargs)


class TargetMaterialMint:
    """B-local-owner only: never accepts RPC parameters or URLs from a message."""

    def __init__(self, store, *, consent_key, channel, rpc, host_peer_id):
        self._store = store
        self._consent_key = consent_key
        self._channel = channel
        self._rpc = rpc
        self._host_peer_id = host_peer_id

    def _resolve_import(self, payload: dict, *, peer_context=None, deadline=None) -> str:
        if (set(payload) != {"lobby_id", "invite_id"} or not isinstance(payload["lobby_id"], str)
                or not isinstance(payload["invite_id"], str)):
            raise MaterialError("invalid_owner_scope")
        record = _store_call(self._store.find_import, payload["lobby_id"], payload["invite_id"],
                             deadline=deadline)
        expected_peer = self._host_peer_id
        if peer_context is not None:
            endpoint_id = getattr(peer_context, "endpoint_id", None)
            generation = getattr(peer_context, "generation", None)
            if not isinstance(endpoint_id, str) or type(generation) is not int:
                raise MaterialError("invalid_owner_scope")
            expected_peer = f"{endpoint_id}:{generation}"
        if record is None:
            raise MaterialError("invalid_owner_scope")
        if record["host_peer_id"] != expected_peer:
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

    async def approved_scope_async(self, payload: dict, *, deadline: float | None = None, peer_context=None) -> dict:
        """Async counterpart used by the real pinned control channel.

        The old synchronous method is retained for the lab adapter; production
        material minting must not manufacture a coroutine in ``to_thread``.
        """
        import asyncio
        import time as _time
        import math as _math
        if deadline is not None and (type(deadline) not in (int, float) or not _math.isfinite(deadline)
                                     or _time.monotonic() >= deadline):
            raise MaterialError("control_unavailable")
        import_id = await asyncio.to_thread(self._resolve_import, payload, peer_context=peer_context,
                                            deadline=deadline)
        if deadline is not None and _time.monotonic() >= deadline:
            raise MaterialError("control_unavailable")
        confirmed = await asyncio.to_thread(_store_call, self._store.confirmed_scope, import_id,
                                            deadline=deadline)
        if deadline is not None and _time.monotonic() >= deadline:
            raise MaterialError("control_unavailable")
        consent = await asyncio.to_thread(_store_call, self._store.get_consent, import_id,
                                          deadline=deadline)
        if deadline is not None and _time.monotonic() >= deadline:
            raise MaterialError("control_unavailable")
        if confirmed is None or consent is None:
            raise MaterialError("both_consents_required")
        scope = dict(confirmed, invite_id=payload["invite_id"])
        if consent["receipt"] != dict(scope, purpose="joining-owner-consent-v1"):
            raise MaterialError("both_consents_required")
        call = getattr(self._channel, "call_async", None)
        # Authenticated request-bound status must stay on the audited channel;
        # falling back to sync ``call`` would drop the inherited authority.
        from invite_rooms.material_control import MaterialControlChannel
        request_bound = peer_context is not None or deadline is not None
        if request_bound:
            if not isinstance(self._channel, MaterialControlChannel) or not callable(call):
                raise MaterialError("control_unavailable")
        if request_bound and callable(call):
            status = await call({"op": "status", "invite_id": payload["invite_id"]},
                                deadline=deadline,
                                admit=peer_context.readmit if peer_context is not None else None)
        else:
            status = await asyncio.to_thread(self._channel.call, {"op": "status", "invite_id": payload["invite_id"]})
        if deadline is not None and _time.monotonic() >= deadline:
            raise MaterialError("control_unavailable")
        if status != {"status": "ready", "scope": scope}:
            raise MaterialError("both_consents_required")
        return scope

    async def mint(self, payload: dict, *, deadline: float | None = None, final_admission=None,
                   peer_context=None, request_meta=None) -> dict:
        # Authentication is carried by the peer context, not by optional
        # request metadata.  A dispatcher/receipt path that has a peer context
        # is therefore request-bound even when a caller drops request_meta;
        # refuse before any journal/store mutation or connector invocation.
        if peer_context is not None and not isinstance(request_meta, dict):
            raise MaterialError("control_unavailable")
        if request_meta is not None and peer_context is None:
            raise MaterialError("control_unavailable")
        if peer_context is not None or request_meta is not None:
            from invite_rooms.material_control import MaterialControlChannel
            if not isinstance(self._channel, MaterialControlChannel):
                raise MaterialError("control_unavailable")
        if deadline is not None and (type(deadline) not in (int, float) or time.monotonic() >= deadline):
            raise MaterialError("control_unavailable")
        scope = await self.approved_scope_async(payload, deadline=deadline, peer_context=peer_context)
        if deadline is not None and time.monotonic() >= deadline:
            raise MaterialError("control_unavailable")
        scope_key = canonical(scope).decode()
        state = await asyncio.to_thread(_store_call, self._store.mint_state, scope_key, deadline=deadline)
        if deadline is not None and time.monotonic() >= deadline:
            raise MaterialError("control_unavailable")
        if state == "ready":
            envelope = await asyncio.to_thread(self._load_envelope, scope_key, scope, deadline=deadline)
        else:
            if state is not None:
                # 'intent' with no completion left over from a prior crash:
                # the mint stays blocked, it is never silently retried.
                raise MintAmbiguous()
            grant_id = await asyncio.to_thread(_store_call, self._store.mint_begin, scope_key,
                                               final_admission=final_admission,
                                               deadline=deadline)  # durable exclusive intent BEFORE the call
            try:
                binding = scope["binding"]
                if deadline is not None and time.monotonic() >= deadline:
                    raise MaterialError("control_unavailable")
                if final_admission is None and deadline is None:
                    # The synchronous/lab adapter has an intentionally narrow
                    # two-argument callback contract.  The real request path
                    # always supplies admission and deadline capabilities below.
                    native_rpc = self._rpc
                else:
                    binder = getattr(self._rpc, "bind_request", None)
                    if not callable(binder):
                        raise MaterialError("control_unavailable")
                    native_rpc = binder(invite_admission=final_admission, deadline=deadline)
                    if not callable(native_rpc) or not getattr(native_rpc, "_request_bound_native_rpc", False):
                        raise MaterialError("control_unavailable")
                result = await native_rpc("groups.peer.invite", {
                        "profile": binding["target_profile"], "room_id": scope["room_id"],
                        "home_install_id": scope["host_id"], "authority_gateway_id": scope["host_id"],
                        "authority_epoch": 1, "member_id": scope["member_id"],
                        "grant_id": grant_id, "ttl_seconds": 600,
                    })
                if deadline is not None and time.monotonic() >= deadline:
                    raise MaterialError("control_unavailable")
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
                if request_meta is not None:
                    operation = {"endpoint_id": peer_context.endpoint_id, "generation": peer_context.generation,
                                 "lobby_id": scope["lobby_id"], "invite_id": scope["invite_id"],
                                 "request_id": request_meta["request_id"],
                                 "expires_at_utc_ms": request_meta["expires_at_utc_ms"]}
                    envelope = {"v": 2, "scope": scope, "grant": result["grant"],
                                "catalog": result["catalog"], "operation": operation, "signature": ""}
                    validate_material(scope, envelope)
                    signed = dict(envelope, purpose="joining-owner-material-v2")
                    signed.pop("signature")
                    envelope["signature"] = self._consent_key.sign(signed)
                else:
                    envelope = {"scope": scope, "grant": result["grant"], "catalog": result["catalog"], "signature": ""}
                    validate_material(scope, envelope)
                    unsigned = {k: v for k, v in envelope.items() if k != "signature"}
                    envelope["signature"] = self._consent_key.sign(dict(unsigned, purpose="joining-owner-material-v1"))
                await asyncio.to_thread(_store_call, self._store.mint_complete, scope_key,
                                        canonical(envelope), deadline=deadline)
            except asyncio.CancelledError:
                raise  # Intent remains durably blocked, never replayed.
            except Exception:
                raise MintAmbiguous() from None
        validate_material(scope, envelope)
        try:
            if request_meta is None:
                command = {"op": "material", "invite_id": scope["invite_id"], "envelope": envelope}
                result = await asyncio.to_thread(self._channel.call, command)
            else:
                if peer_context is None:
                    raise MaterialError("control_unavailable")
                operation = {"endpoint_id": peer_context.endpoint_id, "generation": peer_context.generation,
                             "lobby_id": scope["lobby_id"], "invite_id": scope["invite_id"],
                             "request_id": request_meta["request_id"],
                             "expires_at_utc_ms": request_meta["expires_at_utc_ms"]}
                if envelope.get("v") != 2 or envelope.get("operation") != operation:
                    raise MaterialError("control_unavailable")
                command = {"op": "material", "invite_id": scope["invite_id"], "envelope": envelope,
                           "request_id": request_meta["request_id"], "expires_at_utc_ms": request_meta["expires_at_utc_ms"],
                           "probe": request_meta["probe"], "operation": operation,
                           "operation_signature": self._consent_key.sign({"purpose": "joining-owner-material-v2",
                                                                           "operation": operation})}
                call_async = getattr(self._channel, "call_async", None)
                from invite_rooms.material_control import MaterialControlChannel
                if (not isinstance(self._channel, MaterialControlChannel) or not callable(call_async)):
                    raise MaterialError("control_unavailable")
                result = await call_async(command, deadline=deadline, admit=final_admission)
            if deadline is not None and time.monotonic() >= deadline:
                raise MaterialError("control_unavailable")
            if final_admission is not None and final_admission() is not True:
                raise MaterialError("control_unavailable")
            return result
        except Exception:
            raise MaterialError("control_unavailable") from None

    def _load_envelope(self, scope_key: str, scope: dict, *, deadline=None) -> dict:
        import json as _json
        raw = _store_call(self._store.mint_load, scope_key, deadline=deadline)
        envelope = _json.loads(raw)
        if envelope.get("scope") != scope:
            raise MaterialError("material_unavailable")
        return envelope
