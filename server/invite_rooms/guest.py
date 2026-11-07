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
                 rpc=None, approvals=None, peer_check=None, local_installation_id=None,
                 host_label: str = "host", own_bot_id: str | None = None):
        if not isinstance(store, JoiningStore) or (channel is not None and
                not callable(getattr(channel, "call", None))):
            raise GuestServiceError("invalid_guest_service")
        self.store = store
        self.channel = channel
        self.rpc = rpc
        self.approvals = approvals
        self.peer_check = peer_check
        self.local_installation_id = local_installation_id
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
        """Legacy scope-only lookup remains unsupported: never guess an ID.

        Canonical discovery belongs to the dedicated authenticated adapter;
        neither this helper nor local scope keys prove a session identity.
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

    def _approval_context(self, room_id):
        from invite_rooms.peer_binding import validate_peer_binding
        record, scope = self._room_context(room_id)
        matches = [r for r in self.store.list_imports() if r['state'] == 'consent_given'
                   and (self.store.confirmed_scope(r['import_id']) or {}).get('room_id') == room_id]
        if len(matches) != 1:
            raise GuestServiceError('forbidden')
        envelope = self._material(record, scope)
        try:
            binding = validate_peer_binding(scope['binding'])
            if binding['target_install_id'] != self.local_installation_id:
                raise ValueError()
            if not scope.get('member_id') or not scope.get('host_id') or self.peer_check is None:
                raise ValueError()
            peer = self.peer_check(record['host_peer_id'], scope['host_id'])
            consent = self.store.get_consent(record['import_id'])
            material_scope = dict(scope, invite_id=record['invite_id'])
            if not consent or consent['receipt'] != dict(material_scope, purpose='joining-owner-consent-v1'):
                raise ValueError()
            from invite_rooms.joining_material import validate_material
            # Reuse the target's existing hygiene/expiry/pinned-catalog validator;
            # this does not pretend to verify the core's private grant authority.
            validate_material(material_scope, envelope)
        except Exception:
            raise GuestServiceError('forbidden') from None
        return {'store': str(self.store.root), 'record': record, 'scope': scope,
                'material': envelope, 'consent': consent, 'peer': peer,
                'profile': binding['target_profile'], 'room_id': scope['room_id']}

    def _approvals(self, room_id, **decision):
        # Old/direct services without the dedicated adapter remain unsupported;
        # never send session verbs through the groups callable.
        if self.approvals is None:
            self._room_context(room_id)
            raise GuestServiceError('not_supported')
        context = self._approval_context(room_id)
        def validate():
            if self._approval_context(room_id) != context:
                raise GuestServiceError('forbidden')
        result = self._invoke(lambda: self.approvals.run(context, validate, **decision))
        validate()  # Also fence socket teardown awaits before projecting a result.
        return result

    def my_bot(self, room_id: str) -> dict:
        approvals = self._approvals(room_id)
        _, scope = self._room_context(room_id)
        permissions = scope.get('permissions', [])
        return {'status': 'online', 'approvals': approvals,
                'permissions': permissions if isinstance(permissions, list) else []}

    def decide_approval(self, room_id: str, request_id: str, choice: str) -> None:
        if choice not in {'approve', 'reject'}:
            raise GuestServiceError('invalid_choice')
        self._approvals(room_id, request_id=request_id,
                        choice='once' if choice == 'approve' else 'deny')

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
