"""Private approved peer registry for invite-rooms product operations.

The caller selects pre-paired peer IDs — never URL, fingerprint, catalog or grant.
Exact installation/profile/fingerprint/policy/catalog binding is validated before
any native effect.  Unknown peers or binding changes fail closed.  Defensive
snapshots prevent input/output mutation.  Private native material (adapter URL,
grant, catalog) is resolved only when activation actually needs it, with exact
revalidation.  The registry does NOT infer trust from a loopback URL alone.

A consent_public_key seam is stored for later signed Joining-owner operations,
but it is NOT authenticated: the presence of a public key or an owner flag in a
dict never constitutes consent.
"""
from __future__ import annotations

import copy
import re

from dataclasses import dataclass, field
from typing import Optional

from room_invite_store import InviteError
from room_lobby_service import PeerMaterial, _validate_adapter


def _native_catalog(value: dict):
    """Use the canonical core RoomLink validator; never trust caller digests."""
    try:
        from gateway.hosted_room_peer import GatewayRoomCatalog
    except ImportError:
        raise PeerRegistryError("native_contract_unavailable") from None
    try:
        catalog = GatewayRoomCatalog.from_mapping(copy.deepcopy(value))
    except Exception:
        raise PeerRegistryError("invalid_catalog") from None
    policy = catalog.execution_policy
    if (not catalog.text or catalog.attachments
            or tuple(sorted(policy.enabled_toolsets)) != ("bot_room",)
            or policy.approval_mode != "manual"
            or not 1 <= policy.max_iterations <= 1000):
        raise PeerRegistryError("invalid_catalog")
    return catalog


class PeerRegistryError(ValueError):
    """Machine code only: never interpolates remote values or secrets."""


# Validation patterns (kept consistent with room_invite_store).
_TOKEN = r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}"
_PROFILE = r"[a-z0-9][a-z0-9_-]{0,63}"
_INSTALL = r"install:[0-9a-f]{32}"
_FINGERPRINT = r"[0-9a-f]{64}"
_CATALOG_DIGEST = r"[0-9a-f]{64}"


def _check(value: str, pattern: str, label: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise PeerRegistryError(f"invalid_{label}")


@dataclass(frozen=True)
class MaterialScope:
    """Exact private association; supplied by owner integration, never browser input."""
    owner_installation_id: str
    owner_profile: str
    lobby_id: str
    planned_room_id: str
    member_id: str
    invite_id: str
    peer_id: str
    endpoint_id: str
    fingerprint: str
    target_install_id: str
    target_profile: str
    catalog_digest: str
    policy_digest: str

    def __post_init__(self):
        for name in ('owner_installation_id', 'target_install_id'):
            _check(getattr(self, name), _INSTALL, 'material_scope')
        for name in ('owner_profile', 'target_profile'):
            _check(getattr(self, name), _PROFILE, 'material_scope')
        for name in ('lobby_id', 'planned_room_id', 'member_id', 'invite_id', 'peer_id', 'endpoint_id'):
            _check(getattr(self, name), _TOKEN, 'material_scope')
        for name in ('fingerprint', 'catalog_digest', 'policy_digest'):
            _check(getattr(self, name), _FINGERPRINT, 'material_scope')

    @property
    def binding(self):
        return {k: getattr(self, k) for k in ('endpoint_id', 'fingerprint', 'target_install_id',
                                             'target_profile', 'catalog_digest', 'policy_digest')}


@dataclass(frozen=True)
class ScopedPeerMaterial:
    scope: MaterialScope
    material: PeerMaterial = field(repr=False)


@dataclass(frozen=True)
class PeerRecord:
    """Public, redacted view of a pre-paired peer. No grant, adapter URL, or catalog."""
    peer_id: str
    installation_id: str
    profile: str
    fingerprint: str
    endpoint_id: str
    catalog_digest: str
    consent_public_key: Optional[bytes] = None


@dataclass
class _PeerEntry:
    """Private internal record holding all material including secrets."""
    peer_id: str
    installation_id: str
    profile: str
    fingerprint: str
    endpoint_id: str
    catalog: dict
    catalog_digest: str
    policy_digest: str
    adapter_url: str
    grant: str = field(repr=False)
    consent_public_key: Optional[bytes] = None


class PeerRegistry:
    """Private pre-paired peer store. No global production paths or background work."""

    def __init__(self):
        self._peers: dict[str, _PeerEntry] = {}
        self._scoped_material: dict[MaterialScope, ScopedPeerMaterial] = {}

    def register(self, *, peer_id: str, installation_id: str, profile: str,
                 fingerprint: str, endpoint_id: str, catalog: dict,
                 adapter_url: str, grant: str,
                 consent_public_key: Optional[bytes] = None) -> None:
        _check(peer_id, _TOKEN, "peer_id")
        _check(installation_id, _INSTALL, "installation_id")
        _check(profile, _PROFILE, "profile")
        _check(fingerprint, _FINGERPRINT, "fingerprint")
        _check(endpoint_id, _TOKEN, "endpoint_id")
        if peer_id in self._peers:
            raise PeerRegistryError("peer_already_registered")
        if not isinstance(catalog, dict):
            raise PeerRegistryError("invalid_catalog")
        validated = _native_catalog(catalog)
        catalog_digest = validated.catalog_digest
        policy_digest = validated.execution_policy.policy_digest
        if validated.installation_id != installation_id:
            raise PeerRegistryError("catalog_installation_mismatch")
        if validated.execution_policy.target_profile != profile:
            raise PeerRegistryError("catalog_profile_mismatch")
        _validate_adapter(adapter_url)  # reuses accepted loopback adapter validation
        if not isinstance(grant, str) or not grant:
            raise PeerRegistryError("invalid_grant")
        if consent_public_key is not None:
            if not isinstance(consent_public_key, bytes) or len(consent_public_key) != 32:
                raise PeerRegistryError("invalid_consent_public_key")
        self._peers[peer_id] = _PeerEntry(
            peer_id=peer_id,
            installation_id=installation_id,
            profile=profile,
            fingerprint=fingerprint,
            endpoint_id=endpoint_id,
            catalog=copy.deepcopy(catalog),
            catalog_digest=catalog_digest,
            policy_digest=policy_digest,
            adapter_url=adapter_url,
            grant=grant,
            consent_public_key=consent_public_key,
        )

    def lookup(self, peer_id: str) -> PeerRecord:
        _check(peer_id, _TOKEN, "peer_id")
        entry = self._peers.get(peer_id)
        if entry is None:
            raise PeerRegistryError("unknown_peer")
        return PeerRecord(
            peer_id=entry.peer_id,
            installation_id=entry.installation_id,
            profile=entry.profile,
            fingerprint=entry.fingerprint,
            endpoint_id=entry.endpoint_id,
            catalog_digest=entry.catalog_digest,
            consent_public_key=entry.consent_public_key,
        )

    def binding_for(self, peer_id: str) -> dict:
        """Return the binding dict for use in store.redeem/decide_host. Defensive copy."""
        _check(peer_id, _TOKEN, "peer_id")
        entry = self._peers.get(peer_id)
        if entry is None:
            raise PeerRegistryError("unknown_peer")
        return {
            "endpoint_id": entry.endpoint_id,
            "fingerprint": entry.fingerprint,
            "target_install_id": entry.installation_id,
            "target_profile": entry.profile,
            "catalog_digest": entry.catalog_digest,
            "policy_digest": entry.policy_digest,
        }

    async def resolve_material(self, peer_id: str, *, expected_binding: dict) -> PeerMaterial:
        """Resolve private native material for activation. Validates exact binding.

        Called only when activation actually needs it — never during preview/inspect.
        Revalidates the adapter URL and catalog consistency at resolution time.
        """
        _check(peer_id, _TOKEN, "peer_id")
        entry = self._peers.get(peer_id)
        if entry is None:
            raise PeerRegistryError("unknown_peer")
        # Exact binding revalidation — fail closed on any mismatch.
        if not isinstance(expected_binding, dict):
            raise PeerRegistryError("invalid_binding")
        expected_keys = {"endpoint_id", "fingerprint", "target_install_id", "target_profile", "catalog_digest", "policy_digest"}
        if set(expected_binding) != expected_keys:
            raise PeerRegistryError("invalid_binding")
        if (expected_binding["endpoint_id"] != entry.endpoint_id
                or expected_binding["fingerprint"] != entry.fingerprint
                or expected_binding["target_install_id"] != entry.installation_id
                or expected_binding["target_profile"] != entry.profile
                or expected_binding["catalog_digest"] != entry.catalog_digest
                or expected_binding["policy_digest"] != entry.policy_digest):
            raise PeerRegistryError("binding_mismatch")
        # Revalidate adapter URL at resolution time (fail closed).
        _validate_adapter(entry.adapter_url)
        # Revalidate catalog consistency (detect drift since registration).
        try:
            validated = _native_catalog(entry.catalog)
        except PeerRegistryError:
            raise PeerRegistryError("catalog_drift") from None
        if (validated.catalog_digest != entry.catalog_digest
                or validated.execution_policy.policy_digest != entry.policy_digest
                or validated.installation_id != entry.installation_id
                or validated.execution_policy.target_profile != entry.profile):
            raise PeerRegistryError("catalog_drift")
        binding = self.binding_for(peer_id)  # already a defensive copy
        return PeerMaterial(
            binding=binding,
            catalog=copy.deepcopy(entry.catalog),
            grant=entry.grant,
            adapter_url=entry.adapter_url,
        )

    def _check_scoped_material(self, scoped: ScopedPeerMaterial) -> None:
        if not isinstance(scoped, ScopedPeerMaterial) or not isinstance(scoped.scope, MaterialScope):
            raise PeerRegistryError("invalid_material_scope")
        scope, material = scoped.scope, scoped.material
        binding = self.binding_for(scope.peer_id)
        entry = self._peers[scope.peer_id]
        if (scope.binding != binding or not isinstance(material, PeerMaterial)
                or material.binding != binding or material.adapter_url != entry.adapter_url
                or material.catalog != entry.catalog or material.grant != entry.grant):
            raise PeerRegistryError("material_scope_mismatch")
        validated = _native_catalog(material.catalog)
        if (validated.catalog_digest != scope.catalog_digest
                or validated.execution_policy.policy_digest != scope.policy_digest
                or validated.installation_id != scope.target_install_id
                or validated.execution_policy.target_profile != scope.target_profile):
            raise PeerRegistryError("material_scope_mismatch")
        try:
            _validate_adapter(material.adapter_url)
        except InviteError:
            raise PeerRegistryError("invalid_adapter") from None

    def approve_scoped_material(self, scoped: ScopedPeerMaterial) -> None:
        """Trusted owner provisioning ONLY, not a consent operation or public API.

        Explicitly associate already-provisioned private material with one exact
        owner/lobby/room/member/invite/peer. Global registration alone is NOT this
        approval. The integration must establish provenance before this call;
        this is metadata association, NOT cryptographic native grant verification.
        This method neither mints nor sends, and cannot change immutable consent.
        """
        self._check_scoped_material(scoped)
        old = self._scoped_material.get(scoped.scope)
        if old is not None and old != scoped:
            raise PeerRegistryError("material_scope_conflict")
        self._scoped_material[scoped.scope] = copy.deepcopy(scoped)

    async def resolve_scoped_material(self, scope: MaterialScope, resolver=None) -> PeerMaterial:
        """Product-only lazy resolution: exact approved association, no fallback.

        An optional privileged loader receives the complete immutable scope and
        must return the identical approved scoped material. It cannot select a
        route, a global bearer, or a different room by returning six valid pins.
        """
        if not isinstance(scope, MaterialScope):
            raise PeerRegistryError("invalid_material_scope")
        approved = self._scoped_material.get(scope)
        if approved is None:
            raise PeerRegistryError("material_scope_unapproved")
        approved = copy.deepcopy(approved)
        self._check_scoped_material(approved)
        candidate = await resolver(scope) if resolver is not None else approved
        if not isinstance(candidate, ScopedPeerMaterial) or candidate != approved:
            raise PeerRegistryError("material_scope_mismatch")
        self._check_scoped_material(candidate)
        return copy.deepcopy(candidate.material)