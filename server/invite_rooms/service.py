"""Owner-authenticated invite-room product boundary."""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass

from room_invite_store import InviteError, RoomInviteStore, IssuedInvite, _validate_roster
from invite_rooms.peer_registry import PeerRegistry, PeerRegistryError, MaterialScope


class ServiceError(ValueError):
    """Stable machine code; never includes remote values or secrets."""


_INSTALL_PATTERN = re.compile(r"install:[0-9a-f]{32}")
_PROFILE_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_BINDING_KEYS = {"endpoint_id", "fingerprint", "target_install_id", "target_profile", "catalog_digest", "policy_digest"}


@dataclass(frozen=True)
class OwnerContext:
    installation_id: str
    profile: str

    def __post_init__(self):
        if not isinstance(self.installation_id, str) or not _INSTALL_PATTERN.fullmatch(self.installation_id):
            raise ServiceError("invalid_installation_id")
        if not isinstance(self.profile, str) or not _PROFILE_PATTERN.fullmatch(self.profile):
            raise ServiceError("invalid_profile")


_LOBBY_VIEW_KEYS = frozenset({"lobby_id", "name", "state", "planned_room_id", "created_at", "updated_at", "roster"})
_REDEMPTION_VIEW_KEYS = frozenset({"invite_id", "member_id", "status", "host_decision", "joining_consent"})
# Durable per-peer binding keys shown to the owner for the exact decision
# contract ("decision bound to the exact peer/profile/scope and policy
# digest shown"). Deliberately excludes endpoint_id (and everything else
# private -- secrets, grants, signatures) from both the activation view and
# the mandatory decision route expected_binding input (R3-1: every caller,
# HTTP or direct, must supply it -- there is no unguarded internal path).
_REDEEMED_BINDING_KEYS = frozenset({"fingerprint", "target_install_id", "target_profile", "catalog_digest", "policy_digest"})


def _valid_expected_binding(value) -> bool:
    """Single source of truth for the 5-key durable binding contract.

    Exact 5 keys, each value str or None -- the same rule the HTTP route
    enforces before ``decide_host`` is ever reached (see routes.py's
    ``_valid_expected_binding``, which now simply re-exports this).
    """
    return (isinstance(value, dict) and set(value) == _REDEEMED_BINDING_KEYS
            and all(v is None or isinstance(v, str) for v in value.values()))


class InviteRoomsService:
    """The only product API. Trusted owner identity is immutable construction config."""

    def __init__(self, store: RoomInviteStore, registry: PeerRegistry, *, trusted_owner: OwnerContext | None = None,
                 rpc=None, material_resolver=None):
        if not isinstance(store, RoomInviteStore) or not isinstance(registry, PeerRegistry):
            raise ServiceError("invalid_service_dependencies")
        if trusted_owner is None or not isinstance(trusted_owner, OwnerContext):
            raise ServiceError("trusted_owner_required")
        self.store = store
        self.registry = registry
        try:
            self.store.bind_owner_scope(trusted_owner.installation_id, trusted_owner.profile)
        except Exception:
            raise ServiceError("owner_scope_mismatch") from None
        self._trusted_owner = trusted_owner
        self._rpc = rpc
        self._material_resolver = material_resolver

    def _validate_ctx(self, ctx: OwnerContext) -> None:
        if not isinstance(ctx, OwnerContext) or ctx != self._trusted_owner:
            raise ServiceError("owner_context_mismatch")

    def _owned_lobby(self, ctx: OwnerContext, lobby_id: str) -> dict:
        self._validate_ctx(ctx)
        lobby = self.store.get_lobby(lobby_id)
        local = [m for m in lobby.get("roster", []) if isinstance(m, dict) and "target" not in m]
        self._validate_mvp_roster(lobby["name"], lobby["roster"])
        if not any(m.get("profile") == self._trusted_owner.profile for m in local):
            raise ServiceError("owner_profile_mismatch")
        return lobby

    @staticmethod
    def _redact_lobby(lobby: dict) -> dict:
        return {k: copy.deepcopy(lobby[k]) for k in _LOBBY_VIEW_KEYS if k in lobby}

    @staticmethod
    def _redact_redemption(redemption: dict) -> dict:
        return {k: copy.deepcopy(redemption[k]) for k in _REDEMPTION_VIEW_KEYS if k in redemption}

    def _approved_target(self, target: dict) -> dict:
        if not isinstance(target, dict) or set(target) - {"kind", "peer_id", "installation_id", "profile", "capability_digest"}:
            raise ServiceError("invalid_peer_target")
        try:
            peer_id = target["peer_id"]
            binding = self.registry.binding_for(peer_id)
        except (KeyError, TypeError, PeerRegistryError):
            raise ServiceError("peer_not_paired") from None
        if target.get("kind") != "peer" or target.get("installation_id") != binding["target_install_id"] or target.get("profile") != binding["target_profile"]:
            raise ServiceError("peer_binding_mismatch")
        if target.get("capability_digest", binding["catalog_digest"]) != binding["catalog_digest"]:
            raise ServiceError("catalog_digest_mismatch")
        return binding

    def _validate_roster_bindings(self, roster) -> None:
        if not isinstance(roster, list):
            raise ServiceError("invalid_roster")
        try:
            for member in roster:
                if not isinstance(member, dict):
                    raise ServiceError("invalid_roster")
                if "target" in member:
                    self._approved_target(member["target"])
        except (KeyError, TypeError):
            raise ServiceError("invalid_roster") from None

    def _validate_mvp_roster(self, name, roster) -> None:
        # Structural validation precedes all indexing and every persistence write.
        _validate_roster(name, roster)
        local = [m for m in roster if "target" not in m]
        remote = [m for m in roster if "target" in m]
        if len(local) != 1 or len(remote) != 1:
            raise ServiceError("mvp_roster_limit")
        if local[0]["profile"] != self._trusted_owner.profile:
            raise ServiceError("owner_profile_mismatch")
        if remote[0]["target"]["installation_id"] == self._trusted_owner.installation_id:
            raise ServiceError("distinct_peer_installation_required")

    def create_lobby(self, ctx: OwnerContext, *, name: str, roster: list[dict]) -> dict:
        self._validate_ctx(ctx)
        self._validate_mvp_roster(name, roster)
        self._validate_roster_bindings(roster)
        if not any(isinstance(m, dict) and "target" not in m and m.get("profile") == self._trusted_owner.profile for m in roster):
            raise ServiceError("owner_profile_mismatch")
        return self._redact_lobby(self.store.create_lobby(name=name, roster=copy.deepcopy(roster)))

    def inspect_lobby(self, ctx: OwnerContext, lobby_id: str) -> dict:
        return self._redact_lobby(self._owned_lobby(ctx, lobby_id))

    def issue_invite(self, ctx: OwnerContext, lobby_id: str, member_id: str, *, invitee_label: str,
                     ttl_seconds: int | float = 86400) -> IssuedInvite:
        self._owned_lobby(ctx, lobby_id)
        return self.store.issue_invite(lobby_id, member_id, invitee_label=invitee_label, ttl_seconds=ttl_seconds)

    def revoke_invite(self, ctx: OwnerContext, lobby_id: str, invite_id: str) -> bool:
        self._owned_lobby(ctx, lobby_id)
        return self.store.revoke_invite(lobby_id, invite_id)

    def list_paired_peers(self, ctx: OwnerContext) -> list:
        """Owner-facing, redacted listing of every pre-paired peer. Pure read:
        never mutates the registry or the store."""
        self._validate_ctx(ctx)
        return self.registry.list_peers()

    def decide_host(self, ctx: OwnerContext, lobby_id: str, invite_id: str, *, peer_id: str, approved: bool,
                     expected_binding: dict | None = None) -> dict:
        lobby = self._owned_lobby(ctx, lobby_id)
        binding = self.registry.binding_for(peer_id)
        redemption = self.store.get_redemption(lobby_id, invite_id)
        # Mandatory owner-UI-facing contract (R3-1): every caller -- HTTP or
        # direct/headless -- must supply the exact durable binding it was
        # shown, and it must still match before any state changes. There is
        # no productive headless caller that may omit this; a missing or
        # malformed value is rejected identically to a stale one, with zero
        # store mutation either way.
        if not _valid_expected_binding(expected_binding):
            raise ServiceError("invalid_peer_binding")
        if any(expected_binding.get(k) != redemption.get(k) for k in _REDEEMED_BINDING_KEYS):
            raise ServiceError("binding_changed")
        target = next((m.get("target") for m in lobby["roster"]
                       if m.get("member_id") == redemption["member_id"]), None)
        if target is None or target.get("peer_id") != peer_id:
            raise ServiceError("peer_binding_mismatch")
        self._approved_target(target)
        return self._redact_redemption(self.store.decide_host(lobby_id, invite_id, binding=copy.deepcopy(binding), approved=approved))

    def _readiness(self, ctx: OwnerContext, lobby_id: str):
        lobby = self._owned_lobby(ctx, lobby_id)
        peers, all_ready = [], True
        latest_invites = self.store.latest_invites(lobby_id)
        for member in lobby["roster"]:
            if "target" not in member:
                continue
            member_id, target = member.get("member_id"), member.get("target")
            try:
                binding = self._approved_target(target)
            except ServiceError:
                binding = None
            # R3-2: the activation view and readiness must pair the member's
            # LATEST (non-revoked) invite with the redemption of that SAME
            # invite_id -- never an older invite's redemption left behind by
            # a prior deny-then-reissue cycle. A latest invite with no
            # redemption of its own is reported as null/pending, not paired
            # with a stale redemption from an earlier invite of the member.
            invite_id = latest_invites.get(member_id)
            redemption = None
            if invite_id is not None:
                try:
                    redemption = self.store.get_redemption(lobby_id, invite_id)
                except InviteError:
                    redemption = None
            pinned = redemption is not None and binding is not None and all(redemption.get(k) == binding[k] for k in _BINDING_KEYS)
            ready = bool(pinned and redemption["host_decision"] == "approved" and redemption["joining_consent"] == "given")
            all_ready &= ready
            peers.append({"member_id": member_id, "peer_id": target.get("peer_id") if isinstance(target, dict) else None,
                          "invite_id": invite_id,
                          "redeemed_binding": {k: redemption[k] for k in _REDEEMED_BINDING_KEYS} if redemption else None,
                          "host_decision": redemption["host_decision"] if redemption else "pending",
                          "joining_consent": redemption["joining_consent"] if redemption else "pending",
                          "material_available": bool(binding), "ready": ready})
        return lobby, all_ready, peers

    def activation_status(self, ctx: OwnerContext, lobby_id: str) -> dict:
        lobby, ready, peers = self._readiness(ctx, lobby_id)
        if lobby["state"] == "created":
            registered = self.store.peer_registration_status(lobby_id)
            for peer in peers:
                peer["registered"] = bool(registered.get(peer["member_id"], False))
        return {"lobby_id": lobby_id, "lobby_state": lobby["state"], "ready": ready, "peers": peers}

    def _activation_result_if_already_created(self, lobby: dict) -> dict:
        """Zero-native-call idempotency check for a lobby already past ``created``.

        The store sets ``state == "created"`` BEFORE every remote roster
        member's native peer registration is confirmed (see
        ``RoomLobbyService.activate``: ``mark_created`` happens first, then
        each ``groups.peer.register`` call, then ``mark_peer_registered``).
        A bare "created" state is therefore ambiguous on its own and must
        never be reported as a successful activation -- only the durable
        per-peer registration projection decides that, and it is read here
        with zero native RPC calls.
        """
        remote_ids = {member["member_id"] for member in lobby["roster"] if "target" in member}
        registered = self.store.peer_registration_status(lobby["lobby_id"])
        if not remote_ids <= {member_id for member_id, ok in registered.items() if ok}:
            raise ServiceError("registration_incomplete")
        return {"lobby_id": lobby["lobby_id"], "status": "activated", "room_id": lobby["planned_room_id"]}

    def preflight_activate(self, ctx: OwnerContext, lobby_id: str):
        """Every check that can be answered with no native transport at all.

        Runs BEFORE any caller is allowed to open a native RPC connection
        (R1): an already-created+fully-registered lobby must return its
        durable idempotent result with zero connect attempts, a not-ready
        lobby must fail closed with zero connect attempts, and a missing
        material resolver must fail closed with zero connect attempts.
        Returns ``(lobby, None)`` when the caller must proceed to open the
        native transport, or ``(None, result)`` with the final response
        already decided and no transport ever required.
        """
        lobby, ready, _ = self._readiness(ctx, lobby_id)
        if lobby["state"] == "created":
            return None, self._activation_result_if_already_created(lobby)
        if not ready:
            raise ServiceError("activation_not_ready")
        if self._material_resolver is None:
            raise ServiceError("material_channel_unavailable")
        return lobby, None

    async def activate(self, ctx: OwnerContext, lobby_id: str) -> dict:
        """Explicit side-effect boundary; no native call occurs before durable approvals are pinned."""
        lobby, early = self.preflight_activate(ctx, lobby_id)
        if early is not None:
            return early
        if self._rpc is None:
            raise ServiceError("activation_unavailable")
        from room_lobby_service import RoomLobbyService

        async def product_material(peer):
            # Re-read the approved target; no browser-selected peer/material or
            # generic legacy resolver is ever passed through to the coordinator.
            member = next((m for m in lobby["roster"] if m["member_id"] == peer["member_id"]), None)
            if member is None or "target" not in member:
                raise ServiceError("peer_binding_mismatch")
            binding = self._approved_target(member["target"])
            if any(peer.get(k) != binding[k] for k in _BINDING_KEYS):
                raise ServiceError("peer_binding_mismatch")
            scope = MaterialScope(owner_installation_id=self._trusted_owner.installation_id,
                                  owner_profile=self._trusted_owner.profile,
                                  lobby_id=lobby_id, planned_room_id=lobby["planned_room_id"],
                                  member_id=peer["member_id"], invite_id=peer["invite_id"],
                                  peer_id=member["target"]["peer_id"], **binding)
            return await self.registry.resolve_scoped_material(scope, self._material_resolver)

        coordinator = RoomLobbyService(self.store, self._rpc, product_material,
                                       expected_authority=self._trusted_owner.installation_id)
        try:
            room = await coordinator.activate(lobby_id)
        except Exception as exc:
            code = str(exc)
            if code not in {"native_room_conflict", "creation_ambiguous", "registration_ambiguous", "native_peer_conflict", "native_peer_unconfirmed", "native_rejected", "authority_mismatch", "native_contract_unavailable"}:
                code = self.classify_unconfirmed_mutation(lobby_id)
            raise ServiceError(code) from None
        if not isinstance(room, dict) or room.get("room_id") != lobby["planned_room_id"]:
            raise ServiceError("native_room_conflict")
        return {"lobby_id": lobby_id, "status": "activated", "room_id": room["room_id"]}

    def classify_unconfirmed_mutation(self, lobby_id: str) -> str:
        """Derive a stable code for an unrecognized failure near a native
        mutation, from durable state ALONE -- never from exception text
        (P-3). Shared by this method's own caller and by the routes'
        native-transport fallback (``_run_activation``) so both agree:

          * durable lobby already ``created`` with an incomplete remote
            peer registration -> ``registration_ambiguous`` (the native
            create is confirmed; some peer registration may or may not
            have landed);
          * a native creation spec was pinned (``pin_native_spec``, which
            only happens right before ``groups.create`` is attempted) but
            the lobby never reached ``created`` -> ``creation_ambiguous``
            (create may have been admitted remotely without a durable
            confirmation);
          * otherwise nothing was durably admitted -> ``activation_failed``.
        """
        try:
            lobby = self.store.get_lobby(lobby_id)
        except InviteError:
            return "activation_failed"
        if lobby["state"] == "created":
            remote_ids = {member["member_id"] for member in lobby["roster"] if "target" in member}
            registered = self.store.peer_registration_status(lobby_id)
            if not remote_ids <= {member_id for member_id, ok in registered.items() if ok}:
                return "registration_ambiguous"
            return "activation_failed"
        if self.store.has_pinned_spec(lobby_id):
            return "creation_ambiguous"
        return "activation_failed"
