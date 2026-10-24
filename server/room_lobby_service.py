"""Fixed-roster local-owner coordinator over supported native groups RPC.

Unimported from live MC. The owner-authenticated caller supplies RPC and a PRIVATE
peer-material resolver backed by pinned connector/native routes, never HTTP input.
This module neither mints a grant nor sends a prompt. A Room Card is not an input.
"""
from __future__ import annotations

import contextvars
import copy
from dataclasses import dataclass, field
import re
from urllib.parse import urlsplit

from room_invite_store import InviteError, RoomInviteStore


class NativeRpcRejected(InviteError):
    """An adapter-proven JSON-RPC rejection, NOT a socket/protocol exception.

    Future owner RPC adapters may raise this only after receiving an explicit
    native error response. Unrecognized diagnostics never cross this boundary.
    """
    def __init__(self, code="native_rejected"):
        super().__init__(code if code in {"room_conflict", "invalid_roster", "native_rejected"} else "native_rejected")


class AdmissionDenied(Exception):
    """Raised by a transport when the register admission seam refuses; nothing was sent."""


# R1-05 admission seam. The coordinator sets a synchronous callable here for exactly one
# ``groups.peer.register`` call; a transport that owns the queue/idle-drain waits (BoundedNativeRpc)
# runs it AFTER those waits and immediately before the frame is written, with no await in between.
REGISTER_ADMISSION: contextvars.ContextVar = contextvars.ContextVar("native_register_admission", default=None)


@dataclass(frozen=True)
class PeerMaterial:
    binding: dict
    catalog: dict
    grant: str = field(repr=False)
    adapter_url: str


def _validate_adapter(url):
    if not isinstance(url, str) or len(url) > 256:
        raise InviteError("invalid_adapter")
    try:
        parts = urlsplit(url)
        valid = (parts.scheme == "http" and parts.hostname in {"127.0.0.1", "::1"}
                 and parts.username is None and parts.password is None
                 and parts.port is not None and 0 < parts.port <= 65535
                 and not parts.query and not parts.fragment
                 and re.fullmatch(r"/links/[A-Za-z0-9_-]{1,128}", parts.path))
    except ValueError:
        valid = False
    if not valid:
        raise InviteError("invalid_adapter")


class RoomLobbyService:
    def __init__(self, store: RoomInviteStore, rpc, peer_material, *, expected_authority=None, pre_register_check=None,
                 register_admission=None):
        self.store = store
        self.rpc = rpc
        self.peer_material = peer_material
        self.expected_authority = expected_authority
        self.pre_register_check = pre_register_check
        self.register_admission = register_admission  # (member_id, material) -> sync callable raising on refusal

    async def _plan(self, lobby_id):
        lobby = self.store.seal_lobby(lobby_id)
        caps = await self.rpc("groups.capabilities", {})
        if not isinstance(caps, dict):
            raise InviteError("native_unavailable")
        authority = caps.get("authority_gateway_id")
        if caps.get("driver") is not True or not isinstance(authority, str) or not re.fullmatch(r"install:[0-9a-f]{32}", authority):
            raise InviteError("native_unavailable")
        if self.expected_authority is not None and authority != self.expected_authority:
            raise InviteError("authority_mismatch")
        members = copy.deepcopy(lobby["roster"])
        material_by_member = {}
        for member in members:
            if "target" not in member:
                member["target"] = {"kind": "local", "profile": member["profile"]}
                continue
            peer = next(p for p in lobby["peers"] if p["member_id"] == member["member_id"])
            material = await self.peer_material(copy.deepcopy(peer))
            # Generic coordinator retains the accepted four-field lab contract.
            # NULL means unapproved product policy, never fabricated zero digests.
            # The product wrapper cannot admit this branch (mandatory six pins).
            full_policy = peer.get("catalog_digest") is not None or peer.get("policy_digest") is not None
            keys = ("endpoint_id", "fingerprint", "target_install_id", "target_profile")
            if full_policy:
                keys += ("catalog_digest", "policy_digest")
            expected_binding = {k: peer[k] for k in keys}
            if not isinstance(material, PeerMaterial) or material.binding != expected_binding:
                raise InviteError("fingerprint_mismatch")
            _validate_adapter(material.adapter_url)
            catalog = material.catalog
            if not full_policy:
                digest = catalog.get("catalog_digest") if isinstance(catalog, dict) else None
                if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                    raise InviteError("invalid_catalog")
                if (catalog.get("installation_id") != peer["target_install_id"]
                        or catalog.get("execution_policy", {}).get("target_profile") != peer["target_profile"]):
                    raise InviteError("peer_scope_mismatch")
                if member["target"].get("capability_digest", digest) != digest:
                    raise InviteError("policy_changed")
                member["target"]["capability_digest"] = digest
                material_by_member[member["member_id"]] = material
                continue
            try:
                from gateway.hosted_room_peer import GatewayRoomCatalog
            except ImportError:
                raise InviteError("native_contract_unavailable") from None
            try:
                native_catalog = GatewayRoomCatalog.from_mapping(copy.deepcopy(catalog))
            except Exception:
                raise InviteError("invalid_catalog") from None
            policy = native_catalog.execution_policy
            digest = native_catalog.catalog_digest
            if (native_catalog.installation_id != peer["target_install_id"]
                    or policy.target_profile != peer["target_profile"]
                    or policy.policy_digest != peer["policy_digest"]
                    or not native_catalog.text or native_catalog.attachments
                    or tuple(sorted(policy.enabled_toolsets)) != ("bot_room",)
                    or policy.approval_mode != "manual"):
                raise InviteError("peer_scope_mismatch")
            if digest != peer["catalog_digest"] or member["target"].get("capability_digest", digest) != digest:
                raise InviteError("policy_changed")
            member["target"]["capability_digest"] = digest
            material_by_member[member["member_id"]] = material
        spec = {"room_id": lobby["planned_room_id"], "name": lobby["name"], "members": members}
        self.store.pin_native_spec(lobby_id, spec | {"authority_gateway_id": authority})
        return spec, authority, material_by_member

    @staticmethod
    def _verify_room(room, spec, authority):
        if (not isinstance(room, dict) or any(room.get(k) != v for k, v in spec.items())
                or room.get("authority_gateway_id") != authority or room.get("authority_epoch") != 1
                or room.get("disbanded_at") is not None):
            raise InviteError("native_room_conflict")

    async def create_room(self, lobby_id: str) -> dict:
        spec, authority, _ = await self._plan(lobby_id)
        return await self._create(lobby_id, spec, authority)

    async def _create(self, lobby_id, spec, authority):
        try:
            await self.rpc("groups.create", spec)
        except NativeRpcRejected as exc:
            raise InviteError(str(exc)) from None
        except Exception:
            # Includes reset/disconnect/protocol faults AFTER possible admission.
            raise InviteError("creation_ambiguous") from None
        try:
            result = await self.rpc("groups.state", {"room_id": spec["room_id"]})
        except Exception:
            # A status rejection cannot undo the preceding create admission.
            raise InviteError("creation_ambiguous") from None
        room = result.get("room") if isinstance(result, dict) else None
        required = {"room_id", "name", "members", "authority_gateway_id", "authority_epoch"}
        if not isinstance(room, dict) or not required <= set(room):
            raise InviteError("creation_ambiguous")
        self._verify_room(room, spec, authority)
        self.store.mark_created(lobby_id, room["room_id"])
        return room

    async def activate(self, lobby_id: str) -> dict:
        spec, authority, materials = await self._plan(lobby_id)
        room = await self._create(lobby_id, spec, authority)
        for member_id, material in materials.items():
            try:
                if self.pre_register_check is not None and not self.pre_register_check(member_id, material):
                    raise InviteError("peer_binding_changed")
                admission = self.register_admission(member_id, material) if self.register_admission is not None else None
                token = REGISTER_ADMISSION.set(admission)
                try:
                    registered = await self.rpc("groups.peer.register", {
                        "room_id": room["room_id"], "member_id": member_id,
                        "target_url": material.adapter_url, "target_profile": material.binding["target_profile"],
                        "catalog": material.catalog, "grant": material.grant,
                        "cancellation_scope_id": "cancel-" + room["room_id"],
                        "trace_id": "trace-" + room["room_id"],
                    })
                finally:
                    REGISTER_ADMISSION.reset(token)
                state = await self.rpc("groups.state", {"room_id": room["room_id"]})
            except AdmissionDenied:
                # The transport refused BEFORE writing the frame: the binding changed under the request.
                raise InviteError("peer_binding_changed") from None
            except Exception:
                # A lost register reply is not proof the route was never installed.
                raise InviteError("registration_ambiguous") from None
            if not isinstance(registered, dict) or not isinstance(state, dict) or not isinstance(state.get("room"), dict):
                raise InviteError("registration_ambiguous")
            self._verify_room(state["room"], spec, authority)
            expected = material.binding
            if (registered.get("registered") is not True or registered.get("target_install_id") != expected["target_install_id"]
                    or registered.get("target_profile") != expected["target_profile"]):
                raise InviteError("native_peer_conflict")
            driver = state.get("driver_status", {})
            routes = driver.get("peer_routes", []) if isinstance(driver, dict) else None
            if not isinstance(routes, list) or any(not isinstance(route, dict) for route in routes):
                raise InviteError("registration_ambiguous")
            if not any(r.get("member_id") == member_id and r.get("room_id") == room["room_id"] and r.get("status") == "ready" for r in routes):
                raise InviteError("native_peer_unconfirmed")
            self.store.mark_peer_registered(lobby_id, member_id)
        return room
