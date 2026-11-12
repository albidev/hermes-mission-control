"""Bounded owner-authorized room-log spike for the MC contextual host lane.

This module is intentionally a lab slice, not a product default.  The owner
side owns the authorization record and reads the canonical journal through the
already authenticated local native RPC.  The remote request supplies only a
room selector and cursor; identity, home and epoch are server-side facts.
"""
from __future__ import annotations

import dataclasses
import math
import threading
import uuid
from typing import Any, Mapping

from .control.dispatcher import AuthenticatedPeerContext, ControlError


class RoomLogDenied(ControlError):
    def __init__(self, code: str = "forbidden"):
        super().__init__(code)


@dataclasses.dataclass(frozen=True)
class RoomLogAuthorization:
    """Owner-issued read authority; deliberately independent from execution grants."""
    authorization_id: str
    generation: int
    peer_id: str
    endpoint_id: str
    pairing_generation: int
    room_id: str
    home_install_id: str
    authority_gateway_id: str
    authority_epoch: int
    member_id: str
    admission_generation: int
    history_policy: str = "on"
    projection: str = "room-public-v1"
    from_seq_inclusive: int = 1
    through_seq_inclusive: int | None = None
    expires_at_monotonic: float = math.inf
    active: bool = True
    record_token: str = dataclasses.field(default_factory=lambda: uuid.uuid4().hex, repr=False)
    lifecycle_revision: int = 1
    recipient_id: str = ""
    invite_id: str = ""
    lobby_id: str = ""


def _valid_authorization(a: Any) -> bool:
    if not isinstance(a, RoomLogAuthorization):
        return False
    strings = ("authorization_id", "peer_id", "endpoint_id", "room_id", "home_install_id",
               "authority_gateway_id", "member_id", "projection", "record_token")
    if any(type(getattr(a, key)) is not str or not getattr(a, key) for key in strings):
        return False
    if a.history_policy not in {"on", "off"}:
        return False
    if any(type(getattr(a, key)) is not int or getattr(a, key) < 1
           for key in ("generation", "pairing_generation", "authority_epoch",
                       "admission_generation", "lifecycle_revision", "from_seq_inclusive")):
        return False
    if a.through_seq_inclusive is not None and (
            type(a.through_seq_inclusive) is not int or a.through_seq_inclusive < a.from_seq_inclusive):
        return False
    if type(a.active) is not bool:
        return False
    if type(a.expires_at_monotonic) is not float or (
            not math.isinf(a.expires_at_monotonic) and not math.isfinite(a.expires_at_monotonic)):
        return False
    return True


class InMemoryOwnerReadAuthority:
    """Synthetic owner registry; it is not durable Room policy storage.

    ``history_policy`` is the immutable Room creation decision.  For OFF,
    ``from_seq_inclusive`` belongs to each admission and can change on a fresh
    issue.  Every issue/revoke consumes a monotonic lifecycle revision.
    """
    def __init__(self, authorization: RoomLogAuthorization | None = None,
                 *, room_history_policy: str | None = None, consent_store=None,
                 allow_legacy_without_consent: bool = False):
        self._lock = threading.RLock()
        if authorization is not None and not _valid_authorization(authorization):
            raise ValueError("invalid_authorization")
        policy = (authorization.history_policy if room_history_policy is None and authorization
                  else room_history_policy)
        if policy not in {None, "on", "off"}:
            raise ValueError("invalid_history_policy")
        if authorization is not None and authorization.history_policy != policy:
            raise ValueError("immutable_history_policy")
        if authorization is not None and policy == "on" and authorization.from_seq_inclusive != 1:
            raise ValueError("invalid_history_boundary")
        self._room_history_policy = policy
        self._authorization = authorization
        self._consent_store = consent_store
        # Explicit test/lab escape hatch only. Production contextual assembly
        # never enables this flag; omission therefore remains deny-by-default.
        self._allow_legacy_without_consent = (allow_legacy_without_consent is True)
        self._next_revision = authorization.lifecycle_revision + 1 if authorization else 1
        self._revoked_tokens: set[str] = set()

    def _prepare(self, authorization: RoomLogAuthorization) -> RoomLogAuthorization:
        if not _valid_authorization(authorization):
            raise ValueError("invalid_authorization")
        current = self._authorization
        if authorization.record_token in self._revoked_tokens:
            raise ValueError("revoked_authorization")
        if current is not None and (
                authorization.generation < current.generation
                or authorization.admission_generation < current.admission_generation):
            raise ValueError("authorization_rollback")
        if (current is not None and self._room_history_policy == "off"
                and authorization.admission_generation == current.admission_generation
                and authorization.from_seq_inclusive != current.from_seq_inclusive):
            raise ValueError("invalid_history_boundary")
        if self._room_history_policy is None:
            self._room_history_policy = authorization.history_policy
        if authorization.history_policy != self._room_history_policy:
            raise ValueError("immutable_history_policy")
        if self._room_history_policy == "on" and authorization.from_seq_inclusive != 1:
            raise ValueError("invalid_history_boundary")
        if authorization.record_token in self._revoked_tokens:
            raise ValueError("revoked_authorization")
        revision = self._next_revision
        self._next_revision += 1
        return dataclasses.replace(authorization, lifecycle_revision=revision)

    def issue(self, authorization: RoomLogAuthorization) -> None:
        # Prepare and publish are one lifecycle transition. Never hold this
        # lock while the handler performs native/network I/O.
        with self._lock:
            self._authorization = self._prepare(authorization)

    def revoke(self) -> None:
        with self._lock:
            current = self._authorization
            if current is not None:
                self._revoked_tokens.add(current.record_token)
                self._authorization = dataclasses.replace(current, active=False,
                    generation=current.generation + 1, lifecycle_revision=self._next_revision)
                self._next_revision += 1

    def cutover(self, **changes: Any) -> None:
        with self._lock:
            if self._authorization is None:
                return
            self._authorization = self._prepare(dataclasses.replace(self._authorization, **changes))

    def current(self) -> RoomLogAuthorization | None:
        with self._lock:
            return self._authorization

    def admit(self, ctx: AuthenticatedPeerContext, room_id: str, now: float) -> RoomLogAuthorization:
        with self._lock:
            a = self._authorization
            if (not _valid_authorization(a) or not a.active or not ctx.readmit()
                    or now >= a.expires_at_monotonic or ctx.role != "host"
                    or ctx.peer_id != a.peer_id or ctx.endpoint_id != a.endpoint_id
                    or ctx.generation != a.pairing_generation or room_id != a.room_id):
                raise RoomLogDenied()
            if self._consent_store is None and not self._allow_legacy_without_consent:
                raise RoomLogDenied()
            if self._consent_store is not None:
                scope = {
                    "room_id": a.room_id, "home_install_id": a.home_install_id,
                    "authority_gateway_id": a.authority_gateway_id,
                    "authority_epoch": a.authority_epoch, "member_id": a.member_id,
                    "recipient_id": a.recipient_id or a.peer_id,
                    "history_policy": a.history_policy,
                    "from_seq_inclusive": a.from_seq_inclusive,
                }
                try:
                    if not self._consent_store.authorized(scope):
                        raise RoomLogDenied()
                except RoomLogDenied:
                    raise
                except Exception:
                    raise RoomLogDenied() from None
            return a


def _public_events(events: Any, *, auth: RoomLogAuthorization) -> list[dict]:
    if not isinstance(events, list):
        raise RoomLogDenied("invalid_page")
    result = []
    previous = 0
    event_ids = set()
    for event in events:
        if not isinstance(event, dict) or not {"seq", "event_id", "kind", "actor", "payload", "room_id", "authority_epoch"} <= set(event):
            raise RoomLogDenied("invalid_page")
        seq, event_id = event["seq"], event["event_id"]
        if (type(seq) is not int or seq <= previous or not isinstance(event_id, str)
                or not event_id or event_id in event_ids
                or event["room_id"] != auth.room_id
                or type(event["authority_epoch"]) is not int
                or not 1 <= event["authority_epoch"] <= auth.authority_epoch):
            raise RoomLogDenied("invalid_page")
        previous = seq
        event_ids.add(event_id)
        if seq < auth.from_seq_inclusive:
            continue
        if auth.through_seq_inclusive is not None and seq > auth.through_seq_inclusive:
            continue
        kind = event["kind"]
        if kind not in {"message.user", "message.member"}:
            continue
        actor = event["actor"]
        payload = event["payload"]
        if (not isinstance(actor, dict) or set(actor) != {"kind", "id"}
                or not all(isinstance(actor[k], str) and actor[k] for k in actor)
                or not isinstance(payload, dict) or not {"text"} <= set(payload)
                or not set(payload) <= {"text", "thread_id"}
                or ("thread_id" in payload and not isinstance(payload["thread_id"], str))
                or not isinstance(payload["text"], str)):
            raise RoomLogDenied("invalid_page")
        result.append({"seq": seq, "event_id": event_id, "authority_epoch": event["authority_epoch"],
                       "kind": kind, "actor": {"kind": actor["kind"], "id": actor["id"]},
                       "payload": {"text": payload["text"]}})
    return result


class OwnerRoomLogHandler:
    """Contextual handler implementing state -> log -> state with release fences."""
    def __init__(self, *, read_authority: InMemoryOwnerReadAuthority, native_rpc, clock):
        self.read_authority = read_authority
        self.native_rpc = native_rpc
        self.clock = clock

    async def __call__(self, context: AuthenticatedPeerContext, request: Mapping[str, Any]) -> dict:
        if not isinstance(context, AuthenticatedPeerContext) or not isinstance(request, dict):
            raise RoomLogDenied()
        room_id, since_seq, limit = request.get("room_id"), request.get("since_seq"), request.get("limit")
        if (set(request) != {"op", "room_id", "since_seq", "limit"} or request.get("op") != "room_log"
                or not isinstance(room_id, str) or not room_id or type(since_seq) is not int or since_seq < 0
                or type(limit) is not int or not 1 <= limit <= 200):
            raise RoomLogDenied("invalid_request")
        auth = self.read_authority.admit(context, room_id, self.clock())
        pre = await self.native_rpc("groups.state", {"room_id": room_id})
        _check_state(pre, auth)
        self._recheck(context, room_id, auth)
        # The caller cursor is only a lower bound; OFF adds the immutable
        # per-admission join boundary. ON retains the full journal lineage.
        native_since = since_seq if auth.history_policy == "on" else max(since_seq, auth.from_seq_inclusive - 1)
        page = await self.native_rpc("groups.log", {"room_id": room_id, "since_seq": native_since, "limit": limit})
        _validate_page(page, auth=auth, since_seq=native_since, limit=limit)
        public = _public_events(page["events"], auth=auth)
        post = await self.native_rpc("groups.state", {"room_id": room_id})
        _check_state(post, auth)
        self._recheck(context, room_id, auth)
        return {"room_id": room_id, "events": public, "cursor": page["cursor"],
                "latest_seq": page["latest_seq"], "has_more": page["has_more"],
                "authority": {"gateway_id": auth.authority_gateway_id, "epoch": auth.authority_epoch},
                "projection": auth.projection}

    def _recheck(self, context, room_id, expected):
        current = self.read_authority.admit(context, room_id, self.clock())
        if (current is not expected or current.record_token != expected.record_token
                or current.lifecycle_revision != expected.lifecycle_revision):
            raise RoomLogDenied()


def _validate_page(page: Any, *, auth: RoomLogAuthorization, since_seq: int, limit: int) -> None:
    if not isinstance(page, dict):
        raise RoomLogDenied("invalid_page")
    authority = page.get("authority")
    if (not isinstance(authority, dict) or set(authority) != {"gateway_id", "epoch"}
            or type(authority["gateway_id"]) is not str or not authority["gateway_id"]
            or type(authority["epoch"]) is not int or authority["epoch"] < 1
            or authority != {"gateway_id": auth.authority_gateway_id, "epoch": auth.authority_epoch}):
        raise RoomLogDenied("invalid_page")
    events = page.get("events")
    if not isinstance(events, list) or len(events) > limit:
        raise RoomLogDenied("invalid_page")
    if events and (not isinstance(events[0], dict) or type(events[0].get("seq")) is not int
                   or events[0]["seq"] <= since_seq):
        raise RoomLogDenied("invalid_page")
    latest, cursor, has_more = page.get("latest_seq"), page.get("cursor"), page.get("has_more")
    if (type(latest) is not int or latest < 0 or type(cursor) is not int or cursor < 0
            or type(has_more) is not bool or latest < cursor or latest < since_seq
            or cursor != (events[-1]["seq"] if events else since_seq)
            or has_more != (cursor < latest)):
        raise RoomLogDenied("invalid_page")


def _check_state(value: Any, auth: RoomLogAuthorization) -> None:
    room = value.get("room") if isinstance(value, dict) else None
    if (not isinstance(room, dict)
            or type(room.get("room_id")) is not str or room.get("room_id") != auth.room_id
            or type(room.get("home_install_id")) is not str or room.get("home_install_id") != auth.home_install_id
            or type(room.get("authority_gateway_id")) is not str
            or room.get("authority_gateway_id") != auth.authority_gateway_id
            or type(room.get("authority_epoch")) is not int
            or room.get("authority_epoch") != auth.authority_epoch
            or room.get("status") != "live"):
        raise RoomLogDenied("forbidden")
    members = room.get("members")
    if (not isinstance(members, list)
            or sum(1 for member in members if isinstance(member, dict) and member.get("member_id") == auth.member_id) != 1):
        raise RoomLogDenied("forbidden")
