"""Guest Room read model and the two target-local guest mutations."""
from __future__ import annotations

import asyncio
import inspect
import json
from typing import Any

from invite_rooms.joining import JoiningError
from invite_rooms.joining_store import JoiningStore, JoiningStoreError
from invite_rooms.consent import canonical


class GuestServiceError(ValueError):
    pass


class GuestRoomService:
    def __init__(self, store: JoiningStore, *, channel, rpc=None, host_label: str = "host",
                 own_bot_id: str | None = None):
        if not isinstance(store, JoiningStore) or channel is None or not callable(getattr(channel, "call", None)):
            raise GuestServiceError("invalid_guest_service")
        self.store = store
        self.channel = channel
        self.rpc = rpc
        self.host_label = host_label if isinstance(host_label, str) else "host"
        self.own_bot_id = own_bot_id
        self._left_rooms: set[str] = set()

    @staticmethod
    def _invoke(fn, *args):
        result = fn(*args)
        if inspect.isawaitable(result):
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                return asyncio.run(result)
            raise GuestServiceError("control_unavailable")
        return result

    def _room_context(self, room_id: str) -> tuple[dict, dict]:
        for record in self.store.list_imports():
            scope = self.store.confirmed_scope(record["import_id"])
            if (record["state"] == "consent_given" and isinstance(scope, dict)
                    and scope.get("room_id") == room_id and room_id not in self._left_rooms):
                return record, scope
        raise GuestServiceError("forbidden")

    def _material(self, record: dict, scope: dict) -> dict:
        """Read the exact locally minted envelope; never accept grant/session from HTTP."""
        try:
            raw = self.store.mint_load(canonical(scope).decode())
            envelope = json.loads(raw)
            if not isinstance(envelope, dict) or not isinstance(envelope.get("grant"), str):
                raise ValueError()
            return envelope
        except Exception:
            # Some P3b stores expose the material directly on the confirmed
            # receipt. It is still local state, never request data.
            material = scope.get("material")
            if isinstance(material, dict) and isinstance(material.get("grant"), str):
                return material
            raise GuestServiceError("forbidden") from None

    @staticmethod
    def _session_id(scope: dict, envelope: dict | None = None) -> str:
        for source in (scope, envelope or {}):
            for key in ("session_id", "target_session_id", "session"):
                value = source.get(key)
                if isinstance(value, str) and value:
                    return value
        raise GuestServiceError("forbidden")

    def list_rooms(self) -> list[dict]:
        result = []
        for record in self.store.list_imports():
            if record["state"] != "consent_given":
                continue
            scope = self.store.confirmed_scope(record["import_id"])
            if not isinstance(scope, dict) or not isinstance(scope.get("room_id"), str):
                continue
            if scope["room_id"] in self._left_rooms:
                continue
            result.append({
                "room_id": scope["room_id"], "name": scope["room_id"], "owner": self.host_label,
                "owner_online": True, "owner_offline_since": None, "members": [], "permissions": [],
            })
        return result

    def log(self, room_id: str, *, since_seq: int, limit: int) -> dict:
        if type(since_seq) is not int or since_seq < 0 or type(limit) is not int or not 1 <= limit <= 200:
            raise GuestServiceError("invalid_request")
        self._room_context(room_id)
        try:
            page = self._invoke(self.channel.call, {"op": "room_log", "room_id": room_id,
                                      "since_seq": since_seq, "limit": limit})
        except GuestServiceError:
            raise
        except Exception as exc:
            raise GuestServiceError("host_offline") from exc
        if not isinstance(page, dict):
            raise GuestServiceError("host_offline")
        return page

    def _pending(self, room_id: str) -> tuple[dict, dict, list]:
        record, scope = self._room_context(room_id)
        try:
            envelope = self._material(record, scope)
        except GuestServiceError:
            # The session identity may be persisted in the joining receipt;
            # approvals do not need the grant envelope.
            envelope = {}
        session_id = self._session_id(scope, envelope)
        if self.rpc is None:
            return scope, envelope, []
        try:
            response = self._invoke(self.rpc, "approval.pending", {"session_id": session_id})
        except Exception:
            return scope, envelope, []
        if isinstance(response, dict):
            pending = response.get("approvals", response.get("pending", []))
        else:
            pending = response
        return scope, envelope, pending if isinstance(pending, list) else []

    def my_bot(self, room_id: str) -> dict:
        scope, envelope, approvals = self._pending(room_id)
        raw_permissions = scope.get("permissions", [])
        permissions = raw_permissions if isinstance(raw_permissions, list) else []
        return {"status": "online" if self.rpc is not None else "offline",
                "approvals": approvals, "permissions": permissions}

    def decide_approval(self, room_id: str, request_id: str, choice: str) -> None:
        if choice not in {"approve", "reject"}:
            raise GuestServiceError("invalid_choice")
        scope, envelope, pending = self._pending(room_id)
        found = []
        for item in pending:
            if isinstance(item, dict) and item.get("request_id") == request_id:
                found.append(item)
        if not found:
            raise GuestServiceError("forbidden")
        session_id = self._session_id(scope, envelope)
        if self.rpc is None:
            raise GuestServiceError("host_offline")
        # Core's accepted choices are once/session/always/deny. Guest UI only
        # exposes approve/reject, so approve is the least-broad once choice.
        native_choice = "once" if choice == "approve" else "deny"
        try:
            result = self._invoke(self.rpc, "approval.respond", {
                "session_id": session_id, "request_id": request_id,
                "choice": native_choice, "all": False})
        except Exception as exc:
            raise GuestServiceError("host_offline") from exc
        if not isinstance(result, dict):
            raise GuestServiceError("host_offline")

    def leave(self, room_id: str) -> None:
        if room_id in self._left_rooms:
            return
        record, scope = self._room_context(room_id)
        envelope = self._material(record, scope)
        grant = envelope.get("grant")
        binding = scope.get("binding")
        if not isinstance(binding, dict):
            binding = {}
        profile = binding.get("target_profile")
        if not isinstance(grant, str) or not isinstance(profile, str):
            raise GuestServiceError("forbidden")
        if self.rpc is None:
            raise GuestServiceError("host_offline")
        try:
            self._invoke(self.rpc, "groups.peer.revoke", {"profile": profile, "grant": grant})
        except Exception as exc:
            # The local revoke is the authoritative leave. A missing native
            # reply is not permission to retry with a different grant.
            raise GuestServiceError("host_offline") from exc
        self._left_rooms.add(room_id)
        try:
            self._invoke(self.channel.call, {"op": "member_left", "room_id": room_id})
        except Exception:
            pass
