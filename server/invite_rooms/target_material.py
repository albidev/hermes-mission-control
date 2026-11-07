"""Read-only trusted target material resolution for one acknowledged joining link.

This module is deliberately a source, not a producer: it never mints, repairs,
registers, contacts Core, or follows a URL.  It joins independently read local
provenance with one existing encrypted READY material row.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import math
import time
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from invite_rooms.consent import canonical, verify as verify_consent
from invite_rooms.joining_material import MaterialError, validate_material
from invite_rooms.joining_store import JoiningStore
from invite_rooms.material_validators import validate_scope_attributes, validate_scope_dict
from invite_rooms.pairing_journal import JournalLinkAuthority
from invite_rooms.pairing_service import _peer_id
from invite_rooms.peer_registry import MaterialScope, _native_catalog


class TargetMaterialSourceError(ValueError):
    """Sanitized fail-closed source error; never includes material or tokens."""


@dataclass(frozen=True)
class ResolvedTargetMaterial:
    scope: MaterialScope
    credential_ref: str

    def __repr__(self) -> str:
        return f"ResolvedTargetMaterial(scope={self.scope!r}, credential_ref=<opaque>)"


_FIELDS = frozenset({
    "owner_installation_id", "owner_profile", "lobby_id", "planned_room_id",
    "member_id", "invite_id", "peer_id", "endpoint_id", "fingerprint",
    "target_install_id", "target_profile", "catalog_digest", "policy_digest",
    "pairing_generation",
})
_OPERATION_FIELDS = frozenset({
    "endpoint_id", "generation", "lobby_id", "invite_id", "request_id", "expires_at_utc_ms",
})


def _fail() -> None:
    raise TargetMaterialSourceError("material_unavailable")


def _deadline(deadline: float) -> None:
    if type(deadline) not in (int, float) or isinstance(deadline, bool) or not math.isfinite(deadline):
        raise TargetMaterialSourceError("invalid_deadline")
    if time.monotonic() >= deadline:
        raise TargetMaterialSourceError("material_unavailable")


def _dup(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError
        value[key] = item
    return value


def _json_object(value: bytes):
    return json.loads(value.decode("utf-8"), object_pairs_hook=_dup)


def _verify_signature(public: bytes, value: dict, signature: str, purpose: str) -> None:
    try:
        if type(signature) is not str or len(signature) != 88:
            raise ValueError
        raw = base64.b64decode(signature, validate=True)
        if len(raw) != 64 or type(public) is not bytes or len(public) != 32:
            raise ValueError
        Ed25519PublicKey.from_public_bytes(public).verify(raw, canonical(dict(value, purpose=purpose)))
    except (ValueError, TypeError, InvalidSignature):
        _fail()


def _claim_payload(token: str) -> dict:
    try:
        if type(token) is not str or not 0 < len(token) <= 8192 or token.count(".") != 1:
            raise ValueError
        payload, sig = token.split(".")
        if len(base64.urlsafe_b64decode(sig + "=" * (-len(sig) % 4))) != 32:
            raise ValueError
        claims = _json_object(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        if type(claims) is not dict:
            raise ValueError
        return claims
    except Exception:
        _fail()


def _same_binding(a: dict, b: dict) -> bool:
    return type(a) is dict and type(b) is dict and set(a) == set(b) and all(
        type(a[k]) is type(b[k]) and a[k] == b[k] for k in a
    )


def _detached(value):
    return copy.deepcopy(value)


class TargetMaterialSource:
    """Resolve one exact, already-produced target material row."""

    def __init__(self, *, joining_store, authority, identity_reader, catalog_reader,
                 target_http, endpoint_id, generation):
        from invite_rooms.runtime_config import TargetHttpConfig
        if not isinstance(target_http, TargetHttpConfig):
            raise TargetMaterialSourceError("invalid_target_config")
        if type(joining_store) is not JoiningStore or type(authority) is not JournalLinkAuthority:
            raise TargetMaterialSourceError("invalid_source")
        if type(endpoint_id) is not str or type(generation) is not int or generation < 1:
            raise TargetMaterialSourceError("invalid_link")
        if not callable(identity_reader) or not callable(catalog_reader):
            raise TargetMaterialSourceError("invalid_source")
        self._store = joining_store
        self._authority = authority
        self._identity_reader = identity_reader
        self._catalog_reader = catalog_reader
        self._target_http = TargetHttpConfig(target_http.base_url, target_http.profile)
        self._endpoint_id = endpoint_id
        self._generation = generation

    def _trust(self):
        try:
            trust = self._authority.peer_trust_strict(
                self._endpoint_id, self._generation, role="joining", states=("acknowledged",)
            )
        except Exception:
            _fail()
        if trust is None:
            _fail()
        binding = trust.binding_dict()
        if type(binding) is not dict or set(binding) != {
            "endpoint_id", "fingerprint", "target_install_id", "target_profile",
            "catalog_digest", "policy_digest",
        } or binding.get("endpoint_id") != self._endpoint_id:
            _fail()
        return trust, binding

    def _locals(self, lobby_id: str, invite_id: str, deadline: float):
        _deadline(deadline)
        try:
            record = self._store.find_import(lobby_id, invite_id, deadline=deadline)
            _deadline(deadline)
            if record is None or type(record) is not dict:
                _fail()
            expected_peer = f"{self._endpoint_id}:{self._generation}"
            if record.get("host_peer_id") != expected_peer or record.get("state") != "consent_given":
                _fail()
            import_id = record.get("import_id")
            if type(import_id) is not str:
                _fail()
            confirmed = self._store.confirmed_scope(import_id, deadline=deadline)
            _deadline(deadline)
            consent = self._store.get_consent(import_id, deadline=deadline)
            _deadline(deadline)
        except TargetMaterialSourceError:
            raise
        except Exception:
            _fail()
        if type(confirmed) is not dict or type(consent) is not dict:
            _fail()
        return record, confirmed, consent

    def _derive(self, lobby_id: str, invite_id: str, deadline: float):
        trust, host_binding = self._trust()
        record, confirmed, consent = self._locals(lobby_id, invite_id, deadline)
        _deadline(deadline)
        try:
            identity = self._identity_reader(deadline=deadline)
            _deadline(deadline)
            catalog_raw = self._catalog_reader(deadline=deadline)
            _deadline(deadline)
            catalog = _native_catalog(copy.deepcopy(catalog_raw))
            if (identity.installation_id != catalog.installation_id
                    or identity.profile != catalog.execution_policy.target_profile):
                _fail()
            target_binding = {
                "endpoint_id": self._endpoint_id,
                "fingerprint": identity.leaf_fingerprint,
                "target_install_id": identity.installation_id,
                "target_profile": identity.profile,
                "catalog_digest": catalog.catalog_digest,
                "policy_digest": catalog.execution_policy.policy_digest,
            }
            if confirmed.get("host_id") != host_binding["target_install_id"]:
                _fail()
            wire_scope = {
                "host_id": host_binding["target_install_id"],
                "room_id": confirmed.get("room_id"),
                "lobby_id": confirmed.get("lobby_id"),
                "member_id": confirmed.get("member_id"),
                "invite_id": invite_id,
                "binding": target_binding,
            }
            if (set(confirmed) != {"host_id", "room_id", "lobby_id", "member_id", "binding"}
                    or confirmed.get("lobby_id") != lobby_id or not _same_binding(confirmed.get("binding"), target_binding)):
                _fail()
            expected_receipt = dict(wire_scope, purpose="joining-owner-consent-v1")
            if consent.get("receipt") != expected_receipt:
                _fail()
            verify_consent(identity.consent_public_key, expected_receipt, consent.get("signature"), expected_receipt)
            scope = MaterialScope(
                owner_installation_id=host_binding["target_install_id"],
                owner_profile=host_binding["target_profile"],
                lobby_id=lobby_id, planned_room_id=confirmed["room_id"],
                member_id=confirmed["member_id"], invite_id=invite_id,
                peer_id=_peer_id(identity.installation_id), pairing_generation=self._generation,
                **target_binding,
            )
            validate_scope_attributes(scope, generation=self._generation, profile=self._target_http.profile)
        except TargetMaterialSourceError:
            raise
        except Exception:
            _fail()
        return scope, wire_scope, identity, catalog_raw, catalog, trust, record

    def _read_material(self, scope, wire_scope, identity, catalog_raw, catalog, trust, record, deadline):
        _deadline(deadline)
        try:
            scope_key = canonical(wire_scope).decode()
            state = self._store.mint_state(scope_key, deadline=deadline)
            _deadline(deadline)
            if state != "ready":
                _fail()
            credential_ref = self._store.mint_grant_id(scope_key, deadline=deadline)
            _deadline(deadline)
            if type(credential_ref) is not str:
                _fail()
            raw = self._store.mint_load(scope_key, deadline=deadline)
            _deadline(deadline)
            if type(raw) is not bytes:
                _fail()
            envelope = _json_object(raw)
            if (type(envelope) is not dict or set(envelope) != {"v", "scope", "grant", "catalog", "operation", "signature"}
                    or type(envelope.get("v")) is not int or envelope["v"] != 2 or envelope["scope"] != wire_scope):
                _fail()
            operation = envelope["operation"]
            if (type(operation) is not dict or set(operation) != _OPERATION_FIELDS
                    or operation["endpoint_id"] != self._endpoint_id
                    or type(operation["generation"]) is not int or operation["generation"] != self._generation
                    or operation["lobby_id"] != scope.lobby_id or operation["invite_id"] != scope.invite_id
                    or type(operation["request_id"]) is not str or type(operation["expires_at_utc_ms"]) is not int):
                _fail()
            validate_material(wire_scope, envelope)
            claims = _claim_payload(envelope["grant"])
            if claims.get("grant_id") != credential_ref:
                _fail()
            unsigned = dict(envelope)
            signature = unsigned.pop("signature")
            _verify_signature(identity.consent_public_key, unsigned, signature, "joining-owner-material-v2")
            if _native_catalog(copy.deepcopy(envelope["catalog"])).catalog_digest != catalog.catalog_digest:
                _fail()
            if envelope["catalog"] != catalog_raw:
                _fail()
        except TargetMaterialSourceError:
            raise
        except (MaterialError, Exception):
            _fail()
        _deadline(deadline)
        return credential_ref, envelope

    def _final_admission(self, scope, trust, deadline):
        """Re-admit the result only after all producer/read work has finished."""
        _record, confirmed, consent = self._locals(scope.lobby_id, scope.invite_id, deadline)
        expected_wire = {
            "host_id": scope.owner_installation_id,
            "room_id": scope.planned_room_id,
            "lobby_id": scope.lobby_id,
            "member_id": scope.member_id,
            "invite_id": scope.invite_id,
            "binding": scope.binding,
        }
        expected_receipt = dict(expected_wire, purpose="joining-owner-consent-v1")
        if (type(consent) is not dict
                or confirmed != {"host_id": expected_wire["host_id"],
                          "room_id": expected_wire["room_id"],
                          "lobby_id": expected_wire["lobby_id"],
                          "member_id": expected_wire["member_id"],
                          "binding": expected_wire["binding"]}
                or consent.get("receipt") != expected_receipt):
            _fail()
        _deadline(deadline)
        fresh, fresh_binding = self._trust()
        if fresh != trust or fresh_binding != trust.binding_dict():
            _fail()
        _deadline(deadline)

    def _resolve_checked(self, lobby_id, invite_id, deadline):
        _deadline(deadline)
        scope, wire, identity, catalog_raw, catalog, trust, record = self._derive(lobby_id, invite_id, deadline)
        credential_ref, envelope = self._read_material(scope, wire, identity, catalog_raw, catalog, trust, record, deadline)
        # The journal trust is the remote HOST binding.  scope.binding is the
        # independently derived TARGET binding and is expected to differ.
        self._final_admission(scope, trust, deadline)
        return ResolvedTargetMaterial(scope, credential_ref), envelope

    def resolve(self, *, lobby_id: str, invite_id: str, deadline: float) -> ResolvedTargetMaterial:
        result, _ = self._resolve_checked(lobby_id, invite_id, deadline)
        return result

    def load(self, scope: MaterialScope, *, credential_ref: str, deadline: float) -> dict:
        _deadline(deadline)
        if type(scope) is not MaterialScope or type(credential_ref) is not str:
            _fail()
        try:
            validate_scope_attributes(scope, generation=self._generation, profile=self._target_http.profile)
        except Exception:
            _fail()
        resolved, envelope = self._resolve_checked(scope.lobby_id, scope.invite_id, deadline)
        if resolved.scope != scope or resolved.credential_ref != credential_ref:
            _fail()
        admission_trust, _binding = self._trust()
        # Detach first, then perform the final local-owner/HOST admission and
        # deadline check. Nothing blocking follows this guard.
        catalog = _detached(envelope["catalog"])
        binding = _detached(scope.binding)
        self._final_admission(scope, admission_trust, deadline)
        return {"grant": envelope["grant"], "catalog": catalog, "binding": binding}
