"""Trusted host-side assembly for the bounded Room history lane.

The control listener supplies the only identity input: an immutable journal-issued
AuthenticatedPeerContext.  Room/member/policy coordinates come from the owner
store and authenticated native state; request fields are selectors only.
"""
from __future__ import annotations

import asyncio
import base64
import json
import time
from typing import Any, Mapping

from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from .consent import ConsentKey, canonical
from .control.dispatcher import AuthenticatedPeerContext, ControlError
from .history_consent import HistoryConsentError, HistoryConsentStore, PURPOSE_GUEST, PURPOSE_OWNER
from .room_log import RoomLogAuthorization, OwnerRoomLogHandler, RoomLogDenied


class _Signer(ConsentKey):
    """ConsentKey facade over the already-loaded paired identity key."""
    def __init__(self, private_key):
        self._key = private_key
        self.public_bytes = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)

    def sign(self, receipt: dict) -> str:
        return base64.b64encode(self._key.sign(canonical(receipt))).decode()


class OwnerHistoryHandlers:
    """Contextual handlers installed only on the real host control listener."""
    def __init__(self, *, store, owner_installation_id: str, owner_key, consent_db, native_rpc,
                 admission_check=None, clock=time.monotonic):
        if not callable(native_rpc) or not isinstance(owner_installation_id, str) or not owner_installation_id:
            raise ValueError("history native attachment required")
        self.store = store
        self.owner_installation_id = owner_installation_id
        self.owner_key = owner_key
        self.consent_db = consent_db
        self.native_rpc = native_rpc
        self.admission_check = admission_check
        self.clock = clock

    @staticmethod
    def _context(context: Any) -> tuple[str, dict]:
        if not isinstance(context, AuthenticatedPeerContext) or context.role != "host" or not context.readmit():
            raise ControlError("forbidden")
        peer_id = context.peer_id
        binding = context.binding_dict()
        if type(peer_id) is not str or not peer_id or not isinstance(binding, dict):
            raise ControlError("forbidden")
        return peer_id, binding

    def _native_room(self, value: Any) -> Any:
        room = value.get("room") if isinstance(value, dict) else None
        if (isinstance(room, dict) and "home_install_id" not in room and "status" not in room
                and room.get("authority_gateway_id") == self.owner_installation_id
                and type(room.get("revision")) is int and room["revision"] >= 1
                and not room.get("disbanded_at")):
            # Core canonical state has an authority and disbanded_at, not the
            # MC attachment envelope. Derive local home only for our authority;
            # never replace members or accept a foreign/retired attachment.
            room = dict(room, home_install_id=self.owner_installation_id, status="live")
        return room

    async def _state(self, context, room_id: str) -> dict:
        value = await self.native_rpc("groups.state", {"room_id": room_id},
                                      effect_admission=context.readmit, deadline=context.deadline)
        if not context.readmit():
            raise ControlError("forbidden")
        room = self._native_room(value)
        if (not isinstance(room, dict) or room.get("room_id") != room_id
                or room.get("home_install_id") != self.owner_installation_id
                or room.get("status") != "live"
                or type(room.get("authority_gateway_id")) is not str
                or not room["authority_gateway_id"]
                or type(room.get("authority_epoch")) is not int or room["authority_epoch"] < 1):
            raise ControlError("forbidden")
        return room

    @staticmethod
    def _member_matches(room, member_id, peer_id, binding):
        members = room.get("members")
        if not isinstance(members, list):
            return False
        found = [m for m in members if isinstance(m, dict) and m.get("member_id") == member_id]
        if len(found) != 1:
            return False
        target = found[0].get("target")
        return (isinstance(target, dict) and target.get("kind") == "peer"
                and target.get("peer_id") == peer_id
                and target.get("installation_id") == binding.get("target_install_id")
                and target.get("profile") == binding.get("target_profile")
                and target.get("capability_digest") == binding.get("catalog_digest"))

    def _scope(self, context, room: dict) -> dict:
        peer_id, binding = self._context(context)
        try:
            base = self.store.history_scope_for_peer(room["room_id"], peer_id=peer_id, binding=binding)
        except Exception:
            raise ControlError("forbidden") from None
        scope = {
            "room_id": room["room_id"], "home_install_id": room["home_install_id"],
            "authority_gateway_id": room["authority_gateway_id"], "authority_epoch": room["authority_epoch"],
            "member_id": base["member_id"], "recipient_id": peer_id,
            "pairing_generation": context.generation,
            "history_policy": base["history_policy"], "from_seq_inclusive": base["from_seq_inclusive"],
        }
        if (not self._member_matches(room, base["member_id"], peer_id, binding)
                or base.get("owner_installation_id", self.owner_installation_id) != self.owner_installation_id):
            raise ControlError("forbidden")
        return scope

    def _ledger(self, guest_key: bytes) -> HistoryConsentStore:
        return HistoryConsentStore(self.consent_db, owner_public_key=self.owner_key.public_bytes,
                                   guest_public_key=guest_key)

    async def owner_approve_for_owner(self, *, room_id: str, recipient_id: str,
                                      guest_public_key: bytes, binding: Mapping[str, Any],
                                      owner_installation_id: str, owner_profile: str,
                                      pairing_generation: int) -> dict:
        """The explicit local-owner approval boundary.

        This method is intentionally separate from the guest control handlers:
        only the already authenticated owner HTTP route may call it.  It derives
        the immutable room/admission scope from the owner store and a fresh
        native state read, then persists the owner signature before returning.
        No guest request, invite decision, or execution consent reaches this
        method.
        """
        if (owner_installation_id != self.owner_installation_id or
                not isinstance(owner_profile, str) or not owner_profile or
                not isinstance(room_id, str) or not room_id or
                not isinstance(recipient_id, str) or not recipient_id or
                not isinstance(guest_public_key, (bytes, bytearray)) or len(guest_public_key) != 32 or
                type(pairing_generation) is not int or pairing_generation < 1 or
                not isinstance(binding, dict) or set(binding) != {
                    "endpoint_id", "fingerprint", "target_install_id", "target_profile",
                    "catalog_digest", "policy_digest"} or
                any(value is not None and not isinstance(value, str) for value in binding.values())):
            raise ControlError("forbidden")
        deadline = self.clock() + 10.0
        try:
            state = await self.native_rpc("groups.state", {"room_id": room_id},
                                         effect_admission=lambda: self.clock() < deadline,
                                         deadline=deadline)
        except Exception:
            raise ControlError("forbidden") from None
        room = self._native_room(state)
        if (not isinstance(room, dict) or room.get("room_id") != room_id
                or room.get("home_install_id") != self.owner_installation_id
                or room.get("status") != "live"
                or not isinstance(room.get("authority_gateway_id"), str)
                or not room["authority_gateway_id"]
                or type(room.get("authority_epoch")) is not int or room["authority_epoch"] < 1):
            raise ControlError("forbidden")
        try:
            base = self.store.history_scope_for_peer(room_id, peer_id=recipient_id, binding=binding)
            member_id = base["member_id"]
            if (not isinstance(member_id, str) or not member_id or
                    not self._member_matches(room, member_id, recipient_id, binding)):
                raise ValueError()
            scope = {
                "room_id": room_id, "home_install_id": self.owner_installation_id,
                "authority_gateway_id": room["authority_gateway_id"],
                "authority_epoch": room["authority_epoch"], "member_id": member_id,
                "recipient_id": recipient_id, "pairing_generation": pairing_generation,
                "history_policy": base["history_policy"],
                "from_seq_inclusive": base["from_seq_inclusive"],
            }
            if self.admission_check is not None and self.admission_check() is not True:
                raise ControlError("forbidden")
            ledger = self._ledger(bytes(guest_public_key))
            receipt = dict(scope, purpose=PURPOSE_OWNER)
            signature = self.owner_key.sign(receipt)
            if (self.clock() >= deadline
                    or (self.admission_check is not None and self.admission_check() is not True)):
                raise ControlError("forbidden")
            ledger.owner_approve(scope, receipt, signature)
            return {"status": "owner_approved", "scope": scope,
                    "owner_receipt": receipt, "owner_signature": signature}
        except (HistoryConsentError, KeyError, TypeError, ValueError):
            raise ControlError("forbidden") from None

    async def history_read_scope(self, context, request: Mapping[str, Any]) -> dict:
        self._context(context)
        if set(request) != {"op", "room_id"} or request.get("op") != "history_read_scope":
            raise ControlError("invalid_request")
        room = await self._state(context, request["room_id"])
        scope = self._scope(context, room)
        try:
            ledger = self._ledger(context.consent_public_key)
            # A guest request is disclosure only.  It MUST NOT mint owner
            # consent; approval is a separate owner action.
            snap = ledger.snapshot(scope)
            receipt = json.loads(snap["owner_receipt_json"])
            signature = snap["owner_signature"]
            if not isinstance(receipt, dict) or not isinstance(signature, str):
                raise HistoryConsentError("missing_owner_approval")
        except Exception:
            raise ControlError("forbidden") from None
        if not context.readmit():
            raise ControlError("forbidden")
        return {"scope": scope, "owner_receipt": receipt, "owner_signature": signature}

    async def history_read_acceptance(self, context, request: Mapping[str, Any]) -> dict:
        self._context(context)
        if set(request) != {"op", "scope", "receipt", "signature"} or request.get("op") != "history_read_acceptance":
            raise ControlError("invalid_request")
        scope = request.get("scope")
        if not isinstance(scope, dict):
            raise ControlError("forbidden")
        room = await self._state(context, scope.get("room_id"))
        expected_scope = self._scope(context, room)
        if scope != expected_scope:
            raise ControlError("forbidden")
        receipt, signature = request.get("receipt"), request.get("signature")
        if not isinstance(receipt, dict) or not isinstance(signature, str):
            raise ControlError("forbidden")
        try:
            ledger = self._ledger(context.consent_public_key)
            snap = ledger.snapshot(scope)
            if not snap.get("owner_receipt_json") or not snap.get("owner_signature"):
                raise HistoryConsentError("missing_owner_approval")
            ledger.guest_accept(scope, receipt, signature)
            if not ledger.authorized(scope) or not context.readmit():
                raise HistoryConsentError("not_ready")
        except Exception:
            raise ControlError("forbidden") from None
        return {"status": "ready"}

    async def room_log(self, context, request: Mapping[str, Any]) -> dict:
        self._context(context)
        if set(request) != {"op", "room_id", "since_seq", "limit"} or request.get("op") != "room_log":
            raise ControlError("invalid_request")
        room = await self._state(context, request["room_id"])
        scope = self._scope(context, room)
        try:
            ledger = self._ledger(context.consent_public_key)
            snap = ledger.snapshot(scope)
            if snap.get("revoked") or not snap.get("owner_receipt_json") or not snap.get("guest_receipt_json"):
                raise HistoryConsentError("not_authorized")
            admission = self.store.history_scope_for_peer(
                scope["room_id"], peer_id=context.peer_id, binding=context.binding_dict())
            auth = RoomLogAuthorization(
                authorization_id=canonical(scope).hex(), generation=context.generation,
                peer_id=context.peer_id, endpoint_id=context.endpoint_id,
                pairing_generation=context.generation, room_id=scope["room_id"],
                home_install_id=scope["home_install_id"], authority_gateway_id=scope["authority_gateway_id"],
                authority_epoch=scope["authority_epoch"], member_id=scope["member_id"],
                admission_generation=self.store.history_admission_generation(admission["lobby_id"], scope["member_id"], admission["invite_id"]), history_policy=scope["history_policy"],
                from_seq_inclusive=scope["from_seq_inclusive"],
                expires_at_monotonic=min(context.deadline, self.clock() + 10.0),
                recipient_id=scope["recipient_id"], invite_id=admission["invite_id"],
                lobby_id=admission["lobby_id"])
            authority = _SingleAuthorization(auth, ledger, self.store)
            def readmit():
                authority.admit(context, scope["room_id"], self.clock())
                return True
            async def bound_native(method, params):
                value = await self.native_rpc(method, params, effect_admission=readmit,
                                              deadline=context.deadline)
                if method == "groups.state":
                    current_room = self._native_room(value)
                    if not isinstance(current_room, dict) or self._scope(context, current_room) != scope:
                        raise ControlError("forbidden")
                    return {"room": current_room}
                return value
            result = await OwnerRoomLogHandler(read_authority=authority, native_rpc=bound_native,
                                               clock=self.clock)(context, request)
            if len(json.dumps(result, separators=(",", ":"), allow_nan=False).encode()) > 16 * 1024:
                raise RoomLogDenied("page_too_large")
            return result
        except (RoomLogDenied, HistoryConsentError, ValueError, TypeError):
            raise ControlError("forbidden") from None


class _SingleAuthorization:
    """Adapter that rechecks the exact durable consent row and context capability."""
    def __init__(self, authorization, ledger, store):
        self.authorization = authorization
        self.ledger = ledger
        self.store = store

    def admit(self, context, room_id, now):
        if not context.readmit() or room_id != self.authorization.room_id or now >= self.authorization.expires_at_monotonic:
            raise RoomLogDenied()
        try:
            # Re-resolve the exact admission and native registration at every
            # effect/release fence.  The cursor is never used as lifecycle
            # identity; the durable generation must remain unchanged.
            current = self.store.history_scope_for_peer(
                self.authorization.room_id, peer_id=context.peer_id,
                binding=context.binding_dict())
            if (current.get("lobby_id") != self.authorization.lobby_id
                    or current.get("invite_id") != self.authorization.invite_id
                    or current.get("member_id") != self.authorization.member_id
                    or current.get("owner_installation_id") != self.authorization.home_install_id
                    or current.get("history_policy") != self.authorization.history_policy
                    or current.get("from_seq_inclusive") != self.authorization.from_seq_inclusive):
                raise RoomLogDenied()
            if self.store.history_admission_generation(
                    current["lobby_id"], current["member_id"], current["invite_id"]) != self.authorization.admission_generation:
                raise RoomLogDenied()
            snap = self.ledger.snapshot({
                "room_id": self.authorization.room_id, "home_install_id": self.authorization.home_install_id,
                "authority_gateway_id": self.authorization.authority_gateway_id, "authority_epoch": self.authorization.authority_epoch,
                "member_id": self.authorization.member_id, "recipient_id": self.authorization.recipient_id,
                "pairing_generation": self.authorization.pairing_generation,
                "history_policy": self.authorization.history_policy, "from_seq_inclusive": self.authorization.from_seq_inclusive,
            })
            if snap.get("revoked") or not snap.get("owner_receipt_json") or not snap.get("guest_receipt_json"):
                raise RoomLogDenied()
        except RoomLogDenied:
            raise
        except Exception:
            raise RoomLogDenied() from None
        return self.authorization

    def current(self):
        return self.authorization
