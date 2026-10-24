"""Read-only guest Room view over an acknowledged joining control link.

The guest never owns a Room and never calls groups.create.  The channel is the
only remote boundary; native RPC, when supplied, is used only for the guest's
own bot state.  Approval and leave are intentionally unsupported until core
provides a remote-room command.
"""
from __future__ import annotations

import time
from typing import Any

from invite_rooms.joining import JoiningError
from invite_rooms.joining_store import JoiningStore, JoiningStoreError


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

    def list_rooms(self) -> list[dict]:
        result = []
        for record in self.store.list_imports():
            if record["state"] != "consent_given":
                continue
            scope = self.store.confirmed_scope(record["import_id"])
            if not isinstance(scope, dict) or not isinstance(scope.get("room_id"), str):
                continue
            result.append({
                "room_id": scope["room_id"], "name": scope["room_id"], "owner": self.host_label,
                "owner_online": True, "owner_offline_since": None, "members": [], "permissions": [],
            })
        return result

    def _room(self, room_id: str) -> dict:
        for record in self.store.list_imports():
            scope = self.store.confirmed_scope(record["import_id"])
            if record["state"] == "consent_given" and isinstance(scope, dict) and scope.get("room_id") == room_id:
                return record
        raise GuestServiceError("forbidden")

    def log(self, room_id: str, *, since_seq: int, limit: int) -> dict:
        self._room(room_id)
        try:
            page = self.channel.call({"op": "room_log", "room_id": room_id,
                                      "since_seq": since_seq, "limit": limit})
        except Exception as exc:
            raise GuestServiceError("host_offline") from exc
        if not isinstance(page, dict):
            raise GuestServiceError("host_offline")
        return page

    def my_bot(self, room_id: str) -> dict:
        record = self._room(room_id)
        if self.rpc is None:
            return {"status": "offline", "approvals": [], "permissions": []}
        try:
            state = self.rpc("groups.state", {"room_id": room_id})
            if not isinstance(state, dict):
                raise ValueError()
        except Exception:
            return {"status": "offline", "approvals": [], "permissions": []}
        bot = state.get("bot") if isinstance(state.get("bot"), dict) else {}
        approvals = bot.get("approvals", []) if isinstance(bot.get("approvals", []), list) else []
        permissions = bot.get("permissions", []) if isinstance(bot.get("permissions", []), list) else []
        return {"status": bot.get("status", "online"), "approvals": approvals, "permissions": permissions}

    def decide_approval(self, room_id: str, request_id: str, choice: str) -> None:
        self._room(room_id)
        raise GuestServiceError("not_supported")

    def leave(self, room_id: str) -> None:
        self._room(room_id)
        raise GuestServiceError("not_supported")
