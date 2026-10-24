"""Guest read model. Target-local mutations never accept native scope from HTTP."""
from __future__ import annotations

import asyncio
import inspect
import json
from typing import Protocol

from invite_rooms.joining_store import JoiningStore, JoiningStoreError
from invite_rooms.consent import canonical
from invite_rooms.control.dispatcher import ControlError


class HostChannel(Protocol):
    """One authenticated, owner-pinned host link.

    The sidecar factory selects the configured link, never request input. Its
    implementation must enforce acknowledged endpoint/generation admission and
    recheck it after awaits (P3b.3a); ControlError('forbidden') is not offline.
    """
    def call(self, request: dict): ...


class GuestServiceError(ValueError):
    pass


class GuestRoomService:
    def __init__(self, store: JoiningStore, *, channel: HostChannel | None = None,
                 rpc=None, host_label: str = "host", own_bot_id: str | None = None):
        if not isinstance(store, JoiningStore) or (channel is not None and
                not callable(getattr(channel, "call", None))):
            raise GuestServiceError("invalid_guest_service")
        self.store = store
        self.channel = channel
        self.rpc = rpc
        self.host_label = host_label if isinstance(host_label, str) else "host"
        self.own_bot_id = own_bot_id

    @staticmethod
    def _invoke(fn, *args):
        result = fn(*args)
        if inspect.isawaitable(result):
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                return asyncio.run(result)
            if inspect.iscoroutine(result):
                result.close()
            raise GuestServiceError("control_unavailable")
        return result

    def _room_context(self, room_id: str, *, allow_left: bool = False) -> tuple[dict, dict]:
        for record in self.store.list_imports():
            if record["state"] not in (("consent_given", "left") if allow_left else ("consent_given",)):
                continue
            scope = self.store.confirmed_scope(record["import_id"])
            if isinstance(scope, dict) and scope.get("room_id") == room_id:
                return record, scope
        raise GuestServiceError("forbidden")

    def _material(self, record: dict, scope: dict) -> dict:
        """Single source: the encrypted target mint journal, not a scope fallback."""
        try:
            # TargetMaterialMint.approved_scope adds the import's invite_id to
            # the confirmed scope before using it as its sole journal key.
            mint_scope = dict(scope, invite_id=record["invite_id"])
            envelope = json.loads(self.store.mint_load(canonical(mint_scope).decode()))
            if not isinstance(envelope, dict) or not isinstance(envelope.get("grant"), str) or not envelope["grant"]:
                raise ValueError()
            return envelope
        except Exception:
            raise GuestServiceError("forbidden") from None

    @staticmethod
    def _session_id(scope: dict, envelope: dict | None = None) -> str:
        """No native grant→target-session lookup exists in the current core.

        groups.peer.invite does not return a session. The hidden target session
        is bound only during run dispatch, not minting; groups.replica_state
        does not expose it either. Never derive its hash or trust scope keys.
        See the P6-B.3 report for exact core locations. A future implementation
        must consume a verified native binding before enabling approvals.
        """
        raise GuestServiceError("not_supported")

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
                "owner_online": self.channel is not None, "owner_offline_since": None,
                "members": [], "permissions": [],
            })
        return result

    def log(self, room_id: str, *, since_seq: int, limit: int) -> dict:
        if type(since_seq) is not int or since_seq < 0 or type(limit) is not int or not 1 <= limit <= 200:
            raise GuestServiceError("invalid_request")
        self._room_context(room_id)
        if self.channel is None:
            raise GuestServiceError("host_offline")
        try:
            page = self._invoke(self.channel.call, {"op": "room_log", "room_id": room_id,
                                      "since_seq": since_seq, "limit": limit})
        except ControlError as exc:
            code = "forbidden" if exc.code == "forbidden" else "host_offline"
            raise GuestServiceError(code) from None
        except GuestServiceError:
            raise
        except Exception:
            raise GuestServiceError("host_offline") from None
        self._room_context(room_id)
        if not isinstance(page, dict):
            raise GuestServiceError("host_offline")
        return page

    def _pending(self, room_id: str) -> tuple[dict, dict, list]:
        record, scope = self._room_context(room_id)
        # Resolve before opening any native connection: unsupported is not an
        # empty approvals list, and arbitrary session-like keys grant nothing.
        session_id = self._session_id(scope)
        envelope = self._material(record, scope)
        if self.rpc is None:
            raise GuestServiceError("host_offline")
        try:
            response = self._invoke(self.rpc, "approval.pending", {"session_id": session_id})
        except Exception:
            raise GuestServiceError("host_offline") from None
        self._room_context(room_id)
        if not isinstance(response, dict) or not isinstance(response.get("approvals"), list):
            raise GuestServiceError("host_offline")
        return scope, envelope, response["approvals"]

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
        found = [item for item in pending if isinstance(item, dict) and item.get("request_id") == request_id]
        if not found:
            raise GuestServiceError("forbidden")
        session_id = self._session_id(scope, envelope)
        if any(item.get("session_id", session_id) != session_id for item in found):
            raise GuestServiceError("forbidden")
        if self.rpc is None:
            raise GuestServiceError("host_offline")
        self._room_context(room_id)
        native_choice = "once" if choice == "approve" else "deny"
        try:
            result = self._invoke(self.rpc, "approval.respond", {
                "session_id": session_id, "request_id": request_id,
                "choice": native_choice, "all": False})
        except Exception:
            raise GuestServiceError("host_offline") from None
        if not isinstance(result, dict):
            raise GuestServiceError("host_offline")

    def leave(self, room_id: str) -> None:
        record, scope = self._room_context(room_id, allow_left=True)
        if record['state'] == 'left':
            return
        envelope = self._material(record, scope)
        grant = envelope["grant"]
        binding = scope.get("binding")
        profile = binding.get("target_profile") if isinstance(binding, dict) else None
        if not isinstance(profile, str) or not profile:
            raise GuestServiceError("forbidden")
        if self.rpc is None:
            raise GuestServiceError("host_offline")
        try:
            result = self._invoke(self.rpc, "groups.peer.revoke", {"profile": profile, "grant": grant})
        except Exception:
            raise GuestServiceError("host_offline") from None
        if not isinstance(result, dict) or result.get("revoked") is not True:
            raise GuestServiceError("host_offline")
        try:
            self.store.mark_left(record["import_id"])
        except JoiningStoreError:
            raise GuestServiceError("forbidden") from None
        # A notification failure never rolls back an acknowledged local revoke.
        if self.channel is not None:
            try:
                self._invoke(self.channel.call, {"op": "member_left", "room_id": room_id})
            except Exception:
                pass
