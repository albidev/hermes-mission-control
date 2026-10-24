"""Thin, owner-only HTTP routes over the already-tested invite-rooms service.

Mounted at ``/api/local/room-invites/*`` (see runtime_config for the feature
gate). Every security decision here mirrors the existing MC sidecar
conventions and the P2 route contract:

  * MC bearer auth is checked first, exactly like every other route in
    ``local_telemetry_server`` -- unauthenticated callers get the same 401
    whether the feature is enabled, disabled, or the lobby exists or not.
  * Once authenticated, a disabled/misconfigured feature or an unmatched path
    returns the identical ``{"error": "not_found"}`` 404 used everywhere else
    in this server for an unknown path -- no existence/config oracle.
  * Origin/CSRF: any request carrying an Origin header requires
    ``MISSION_CONTROL_ALLOWED_ORIGIN`` to be configured and to match exactly.
  * Every response goes through ``handler._json`` (Cache-Control: no-store,
    the server's own CORS policy) -- nothing here ever logs a request/response
    body, and a secret is only ever returned from the issue-invite response.
  * Bodies are read with the same bound as ``Handler._read_json_body`` plus
    duplicate-key and unknown-field rejection; the raw body is never echoed.
  * Path ids are validated against the exact store-generated patterns before
    any lookup.
  * A fixed ServiceError/InviteError/PeerRegistryError -> HTTP status mapping
    makes an IDOR probe (foreign-owner lobby, mismatched invite/lobby) return
    the exact same response as a nonexistent id.
"""
from __future__ import annotations

import asyncio
import contextlib
import json as json_module
import logging
import os
import re
from pathlib import Path
from typing import Optional

from invite_rooms import runtime_config
from invite_rooms import pairing_codec
from invite_rooms.pairing_journal import E_BUSY, JournalError
from invite_rooms.pairing_service import PairingError, PairingService
from invite_rooms.peer_registry import PeerRegistryError
from invite_rooms.service import InviteRoomsService, ServiceError, _valid_expected_binding
from invite_rooms.guest import GuestRoomService, GuestServiceError
from room_invite_store import InviteError

PREFIX = "/api/local/room-invites"

_LOBBY_ID = r"lobby-[0-9a-f]{32}"
_INVITE_ID = r"invite-[0-9a-f]{32}"
_PAIRING_ID = r"pairing-[0-9a-f]{32}"
_PEER_ID = r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}"

_MAX_ROUTE_BODY_BYTES = 16 * 1024
_ACTIVATE_TIMEOUT_SECONDS = 9.0

_LOG = logging.getLogger("invite_rooms.routes")

# Seam for the joining-owner material channel (P2b). This module never
# invents a JSON config format for it and never wires one on its own: as
# long as this stays ``None`` (the real-config default), activation of any
# lobby with a remote roster member fails closed with
# ``material_channel_unavailable`` before any native RPC call. P2b's
# integration point is to replace this attribute with a real resolver
# coroutine at process wiring time -- never a request-scoped value.
_MATERIAL_RESOLVER = None
# P3b.3b.2 installs a per-config pinned-link connector. Absence means
# host_offline for log, not a disabled local guest view.
_HOST_CHANNEL_FACTORY = None


def configure_guest_host_channel(factory=None) -> None:
    global _HOST_CHANNEL_FACTORY
    if factory is not None and not callable(factory):
        raise ValueError("invalid_host_channel_factory")
    _HOST_CHANNEL_FACTORY = factory


def _guest_rpc_factory(config):
    if config.native_rpc is None:
        return None

    async def call(method, params):
        async with _connect_native_rpc(config.native_rpc) as rpc:
            return await rpc(method, params)
    return call


# Seam for the owner's own catalog (``groups.capabilities`` -> ``room_link.catalog``) used by pairing.  ``None`` means
# "ask the configured native RPC"; tests may replace it with a labelled stand-in ``callable(config, ctx) -> dict``.
_PAIRING_CATALOG_SOURCE = None


# --------------------------------------------------------------------------
# Fixed error-code -> HTTP status mapping. Every code a route handler might
# see from the service/store/registry is bucketed here; anything unlisted
# (including a bare Exception) falls back to 500 {"error": "internal"} with
# no traceback text, per the P2 contract's error-mapping requirement.
# --------------------------------------------------------------------------

_NOT_FOUND_CODES = {
    "lobby_unavailable", "invite_unavailable", "owner_profile_mismatch",
    "owner_context_mismatch", "unknown_peer", "peer_not_paired", "peer_binding_mismatch",
}
_BAD_REQUEST_CODES = {
    "invalid_lobby", "invalid_invite", "invalid_decision", "invalid_peer_binding",
    "invalid_peer_target", "invalid_roster", "mvp_roster_limit",
    "distinct_peer_installation_required", "catalog_digest_mismatch", "invalid_binding",
    "invalid_material_scope", "invalid_owner_scope", "invalid_peer_id",
}
_CONFLICT_CODES = {
    "member_already_redeemed", "fingerprint_mismatch", "host_approval_required",
    "decision_final", "consent_required", "native_spec_conflict", "catalog_drift",
    "material_scope_conflict", "material_scope_mismatch", "material_scope_unapproved",
    "activation_not_ready", "native_room_conflict", "native_peer_conflict", "native_rejected",
    "registration_incomplete", "binding_changed",
}
_UNAVAILABLE_CODES = {
    "activation_unavailable", "creation_ambiguous", "registration_ambiguous",
    "native_peer_unconfirmed", "native_contract_unavailable", "activation_failed",
    "material_channel_unavailable", "native_unavailable",
}
_FORBIDDEN_CODES = {"authority_mismatch"}


def _map_error(code: str) -> tuple[int, dict]:
    if code in _NOT_FOUND_CODES:
        return 404, {"error": "not_found"}
    if code in _BAD_REQUEST_CODES:
        return 400, {"error": "invalid_request", "code": code}
    if code in _CONFLICT_CODES:
        return 409, {"error": "conflict", "code": code}
    if code in _UNAVAILABLE_CODES:
        return 503, {"error": "unavailable", "code": code}
    if code in _FORBIDDEN_CODES:
        return 403, {"error": "forbidden"}
    return 500, {"error": "internal"}


_KNOWN_ERRORS = (ServiceError, InviteError, PeerRegistryError)


def _respond_error(handler, exc: Exception) -> None:
    status, payload = _map_error(str(exc))
    handler._json(status, payload)


# --------------------------------------------------------------------------
# Request-level helpers
# --------------------------------------------------------------------------

def _origin_allowed(handler) -> bool:
    origin = (handler.headers.get("Origin") or "").strip()
    if not origin:
        return True  # non-browser / same-origin callers are not subject to this CSRF check
    configured = (os.getenv("MISSION_CONTROL_ALLOWED_ORIGIN") or "").strip()
    return bool(configured) and origin == configured


def _content_type_is_json(handler) -> bool:
    content_type = (handler.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
    return content_type == "application/json"


def _reject_duplicate_keys(pairs):
    seen: dict = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError("duplicate_key")
        seen[key] = value
    return seen


def _read_strict_body(handler, allowed_fields: set[str]) -> Optional[dict]:
    """Bounded, duplicate-key-rejecting JSON body read. Sends 400/413 and
    returns None on any violation; never echoes the raw body back."""
    header = (handler.headers.get("Content-Length") or "").strip()
    try:
        length = int(header) if header else 0
    except ValueError:
        handler._json(400, {"error": "bad_request", "code": "invalid_content_length"})
        return None
    if length < 0:
        handler._json(400, {"error": "bad_request", "code": "invalid_content_length"})
        return None
    if length > _MAX_ROUTE_BODY_BYTES:
        handler._json(413, {"error": "payload_too_large"})
        return None
    raw = handler.rfile.read(length)
    try:
        text = raw.decode("utf-8") if raw else "{}"
    except UnicodeDecodeError:
        handler._json(400, {"error": "bad_request", "code": "invalid_body"})
        return None
    try:
        payload = json_module.loads(text or "{}", object_pairs_hook=_reject_duplicate_keys)
    except ValueError:
        handler._json(400, {"error": "bad_request", "code": "invalid_body"})
        return None
    if not isinstance(payload, dict) or set(payload) - allowed_fields:
        handler._json(400, {"error": "bad_request", "code": "invalid_body"})
        return None
    return payload


class _RouteValidationError(Exception):
    """Internal: body shape is well-formed JSON but fails a route-level rule."""


_MEMBER_ALLOWED = {"member_id", "handle", "display_name", "target"}

# The 5 durable binding fields shown to the owner in the activation view and
# required (unmodified) back on a decision -- validated by the single shared
# rule in invite_rooms.service (see service._REDEEMED_BINDING_KEYS /
# service._valid_expected_binding), deliberately excluding
# endpoint_id/secrets/grants. No duplicated rule here: service.decide_host
# applies the identical check before the comparison, so the HTTP contract
# and the direct-service contract can never drift apart.


def _expand_roster(service: InviteRoomsService, ctx, roster_in) -> list[dict]:
    """Expand a browser-safe roster into the full store/service roster shape.

    The browser may only ever select a pre-paired ``peer_id``; every other
    peer attribute (installation id, profile, capability digest, fingerprint,
    endpoint, grant, adapter URL) is resolved server-side from the owner's own
    peer registry, never trusted from the request body. The local member's
    own profile always comes from the trusted owner context, never the body.
    """
    if not isinstance(roster_in, list):
        raise _RouteValidationError()
    expanded = []
    for member in roster_in:
        if not isinstance(member, dict) or set(member) - _MEMBER_ALLOWED or not {"member_id", "handle"} <= set(member):
            raise _RouteValidationError()
        member_id, handle = member["member_id"], member["handle"]
        if not isinstance(member_id, str) or not isinstance(handle, str):
            raise _RouteValidationError()
        out = {"member_id": member_id, "handle": handle}
        if "display_name" in member:
            if not isinstance(member["display_name"], str):
                raise _RouteValidationError()
            out["display_name"] = member["display_name"]
        if "target" in member:
            target_in = member["target"]
            if not isinstance(target_in, dict) or set(target_in) != {"peer_id"} or not isinstance(target_in["peer_id"], str):
                raise _RouteValidationError()
            try:
                record = service.registry.lookup(target_in["peer_id"])
            except PeerRegistryError:
                raise _RouteValidationError() from None
            out["profile"] = record.profile
            out["target"] = {
                "kind": "peer",
                "peer_id": record.peer_id,
                "installation_id": record.installation_id,
                "profile": record.profile,
                "capability_digest": record.catalog_digest,
            }
        else:
            out["profile"] = ctx.profile
        expanded.append(out)
    return expanded


# --------------------------------------------------------------------------
# Native RPC connection -- the only transport allowed to be replaced by a
# recording stand-in in tests (see test_room_invite_routes.py). Everything
# else (auth, gate, origin, body validation, store, registry) is real.
# --------------------------------------------------------------------------

@contextlib.asynccontextmanager
async def _connect_native_rpc(rpc_config: dict):
    import aiohttp

    from invite_rooms.native_rpc import BoundedNativeRpc

    token = Path(rpc_config["token_path"]).read_text(encoding="utf-8").strip()
    if not token:
        raise ServiceError("activation_unavailable")
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(rpc_config["endpoint"], headers={"Authorization": f"Bearer {token}"}) as ws:
            yield BoundedNativeRpc(ws, require_register_admission=True)


async def _run_activation(service: InviteRoomsService, ctx, lobby_id: str, config) -> dict:
    """Open the native transport only after every transport-free check has
    already decided the lobby must proceed (R1): an already-created+fully-
    registered duplicate or a not-ready/missing-material lobby returns (or
    raises) here with zero connect attempts, before ``_connect_native_rpc``
    is ever called. Expected transport/token failures while ACQUIRING the
    transport (a refused connection, a closed socket, an empty token, ...)
    are mapped to a single stable, sanitized ``native_unavailable`` 503 --
    never left to bubble up as an uncaught 500.

    Once ``scoped.activate`` has actually started, its outcome (a returned
    result, or an already-classified ``ServiceError``/known error) is final:
    a transport-teardown failure afterwards must never replace it (P-1).
    Teardown faults are only logged, by exception class name, never allowed
    to mask a durable outcome that already happened. Control signals
    (``KeyboardInterrupt``/``SystemExit``/``GeneratorExit``/
    ``CancelledError``) and ``TimeoutError``/known errors are re-raised
    exactly as they escaped the body, regardless of what the transport's
    own teardown claims to have suppressed (P-2): ``suppressed`` may only
    ever influence the handling of an ordinary, unrecognized ``Exception``.
    An unclassified exception that still manages to leak out of an
    in-flight activation is not reported as ``native_unavailable`` either
    (that would wrongly imply nothing was attempted); it is classified from
    durable state alone via ``InviteRoomsService.classify_unconfirmed_mutation``
    (P-3), the same derivation the service itself uses for its own
    catch-all, so routes and the service always agree.
    """
    lobby, early = service.preflight_activate(ctx, lobby_id)
    if early is not None:
        return early
    if config.native_rpc is None:
        # No native transport configured at all for this process: no connect
        # is possible, so fail through the service's own stable code.
        return await service.activate(ctx, lobby_id)

    cm = _connect_native_rpc(config.native_rpc)
    try:
        rpc = await cm.__aenter__()
    except (asyncio.TimeoutError, asyncio.CancelledError, *_KNOWN_ERRORS):
        raise
    except Exception:
        # Nothing has been attempted yet: token read, ClientSession, or the
        # ws handshake itself failed before any RPC could be sent.
        raise ServiceError("native_unavailable") from None

    scoped = InviteRoomsService(service.store, service.registry, trusted_owner=ctx, rpc=rpc,
                                 material_resolver=service._material_resolver)
    result: Optional[dict] = None
    outcome_exc: Optional[BaseException] = None
    try:
        result = await scoped.activate(ctx, lobby_id)
    except BaseException as exc:  # noqa: BLE001 - outcome is re-classified below
        outcome_exc = exc

    exc_type = type(outcome_exc) if outcome_exc is not None else None
    exc_tb = outcome_exc.__traceback__ if outcome_exc is not None else None
    try:
        suppressed = await cm.__aexit__(exc_type, outcome_exc, exc_tb)
    except Exception as teardown_exc:
        _LOG.warning(
            "native transport teardown failed after activation (ignored): %s",
            type(teardown_exc).__name__,
        )
        suppressed = False

    if outcome_exc is None:
        assert result is not None
        return result
    if not isinstance(outcome_exc, Exception) or isinstance(outcome_exc, (asyncio.TimeoutError, *_KNOWN_ERRORS)):
        # Control signal or an already-classified/known outcome: final,
        # regardless of ``suppressed`` (P-2). Re-raised exactly as it
        # escaped the body -- never converted, never masked.
        raise outcome_exc
    if suppressed:
        # The transport generator claims to have handled the in-flight
        # exception itself; there is no durable result to return, so this
        # still must not be reported as native_unavailable (point 3).
        _LOG.warning("activation outcome suppressed by teardown (ignored): %s", type(outcome_exc).__name__)
        raise ServiceError(service.classify_unconfirmed_mutation(lobby_id)) from None
    _LOG.warning("unclassified activation failure (ignored): %s", type(outcome_exc).__name__)
    raise ServiceError(service.classify_unconfirmed_mutation(lobby_id)) from None


# --------------------------------------------------------------------------
# Route handlers. Each receives the already-authenticated/authorized request,
# the long-lived service+ctx built from the current config, the config
# itself (native RPC reference only), the regex path groups, and the parsed
# query params (unused by every current route, kept for a uniform signature).
# --------------------------------------------------------------------------

def _h_create_lobby(handler, service, ctx, config, args, params) -> None:
    if not _content_type_is_json(handler):
        handler._json(400, {"error": "bad_request", "code": "invalid_content_type"})
        return
    body = _read_strict_body(handler, {"name", "roster"})
    if body is None:
        return
    name, roster_in = body.get("name"), body.get("roster")
    if not isinstance(name, str) or not isinstance(roster_in, list):
        handler._json(400, {"error": "bad_request", "code": "invalid_body"})
        return
    try:
        roster = _expand_roster(service, ctx, roster_in)
    except _RouteValidationError:
        handler._json(400, {"error": "bad_request", "code": "invalid_roster"})
        return
    try:
        view = service.create_lobby(ctx, name=name, roster=roster)
    except _KNOWN_ERRORS as exc:
        _respond_error(handler, exc)
        return
    handler._json(200, {"lobby": view})


def _h_inspect_lobby(handler, service, ctx, config, args, params) -> None:
    (lobby_id,) = args
    try:
        view = service.inspect_lobby(ctx, lobby_id)
    except _KNOWN_ERRORS as exc:
        _respond_error(handler, exc)
        return
    handler._json(200, {"lobby": view})


def _h_issue_invite(handler, service, ctx, config, args, params) -> None:
    (lobby_id,) = args
    if not _content_type_is_json(handler):
        handler._json(400, {"error": "bad_request", "code": "invalid_content_type"})
        return
    body = _read_strict_body(handler, {"member_id", "invitee_label", "ttl_seconds"})
    if body is None:
        return
    member_id, invitee_label = body.get("member_id"), body.get("invitee_label")
    ttl_seconds = body.get("ttl_seconds", 86400)
    if (not isinstance(member_id, str) or not isinstance(invitee_label, str)
            or type(ttl_seconds) not in (int, float) or isinstance(ttl_seconds, bool)):
        handler._json(400, {"error": "bad_request", "code": "invalid_body"})
        return
    try:
        issued = service.issue_invite(ctx, lobby_id, member_id, invitee_label=invitee_label, ttl_seconds=ttl_seconds)
    except _KNOWN_ERRORS as exc:
        _respond_error(handler, exc)
        return
    # The secret is returned ONLY here; _json already sets Cache-Control: no-store.
    handler._json(200, {"invite_id": issued.invite_id, "secret": issued.secret, "expires_at": issued.expires_at})


def _h_revoke_invite(handler, service, ctx, config, args, params) -> None:
    lobby_id, invite_id = args
    if _read_strict_body(handler, set()) is None:
        # Bodyless POST contract: empty body or exactly {} only. Any other
        # body is rejected before any mutation or lookup (R3).
        return
    try:
        result = service.revoke_invite(ctx, lobby_id, invite_id)
    except _KNOWN_ERRORS as exc:
        _respond_error(handler, exc)
        return
    handler._json(200, {"revoked": bool(result)})


def _h_decide_host(handler, service, ctx, config, args, params) -> None:
    lobby_id, invite_id = args
    if not _content_type_is_json(handler):
        handler._json(400, {"error": "bad_request", "code": "invalid_content_type"})
        return
    body = _read_strict_body(handler, {"peer_id", "approved", "expected_binding"})
    if body is None:
        return
    peer_id, approved, expected_binding = body.get("peer_id"), body.get("approved"), body.get("expected_binding")
    if not isinstance(peer_id, str) or type(approved) is not bool or not _valid_expected_binding(expected_binding):
        handler._json(400, {"error": "bad_request", "code": "invalid_body"})
        return
    try:
        result = service.decide_host(ctx, lobby_id, invite_id, peer_id=peer_id, approved=approved,
                                      expected_binding=expected_binding)
    except _KNOWN_ERRORS as exc:
        _respond_error(handler, exc)
        return
    handler._json(200, {"redemption": result})


def _h_paired_peers(handler, service, ctx, config, args, params) -> None:
    try:
        peers = service.list_paired_peers(ctx)
    except _KNOWN_ERRORS as exc:
        _respond_error(handler, exc)
        return
    # The owner profile is the trusted local lobby member; the browser needs it to
    # label the owner and to pick a remote handle that does not collide with it.
    handler._json(200, {"peers": peers, "owner_profile": ctx.profile})


def _h_activation_status(handler, service, ctx, config, args, params) -> None:
    (lobby_id,) = args
    try:
        status_view = service.activation_status(ctx, lobby_id)
    except _KNOWN_ERRORS as exc:
        _respond_error(handler, exc)
        return
    handler._json(200, status_view)


def _h_activate(handler, service, ctx, config, args, params) -> None:
    (lobby_id,) = args
    if _read_strict_body(handler, set()) is None:
        # Bodyless POST contract: empty body or exactly {} only. Any other
        # body is rejected before any connect or mutation attempt (R3).
        return
    try:
        result = asyncio.run(asyncio.wait_for(_run_activation(service, ctx, lobby_id, config), timeout=_ACTIVATE_TIMEOUT_SECONDS))
    except asyncio.TimeoutError:
        # Never retried here, never mints a new id: the lobby is left exactly
        # as the coordinator's own ambiguity handling leaves it.
        handler._json(503, {"error": "unavailable", "code": "activation_timeout"})
        return
    except _KNOWN_ERRORS as exc:
        _respond_error(handler, exc)
        return
    handler._json(200, result)


def _h_guest_rooms(handler, service, ctx, config, args, params) -> None:
    handler._json(200, {"rooms": service.list_rooms()})


def _query_int(params: dict, key: str, default: int) -> int:
    values = params.get(key, [])
    raw = values[0] if isinstance(values, list) and values else default
    value = int(raw)
    if value < 0:
        raise ValueError()
    return value


def _h_guest_log(handler, service, ctx, config, args, params) -> None:
    try:
        since_seq = _query_int(params, "since_seq", 0)
        limit = _query_int(params, "limit", 200)
        if limit < 1 or limit > 200:
            raise ValueError()
        page = service.log(args[0], since_seq=since_seq, limit=limit)
    except GuestServiceError as exc:
        if str(exc) == "host_offline":
            handler._json(503, {"code": "host_offline", "last_seen_at": None})
        else:
            handler._json(403, {"code": "forbidden"})
        return
    except (TypeError, ValueError):
        handler._json(400, {"code": "invalid_request"})
        return
    handler._json(200, page)


def _h_guest_bot(handler, service, ctx, config, args, params) -> None:
    try:
        result = service.my_bot(args[0])
    except GuestServiceError as exc:
        code = str(exc)
        status = 501 if code == "not_supported" else 503 if code == "host_offline" else 403
        handler._json(status, {"code": code if status != 403 else "forbidden"})
        return
    handler._json(200, result)


def _h_guest_approval(handler, service, ctx, config, args, params) -> None:
    if not _content_type_is_json(handler):
        handler._json(400, {"code": "invalid_request"})
        return
    body = _read_strict_body(handler, {"choice"})
    if body is None or body.get("choice") not in {"approve", "reject"}:
        handler._json(400, {"code": "invalid_request"})
        return
    try:
        service.decide_approval(args[0], args[1], body["choice"])
    except GuestServiceError as exc:
        code = str(exc)
        if code == "invalid_choice":
            handler._json(400, {"code": "invalid_request"})
        elif code == "not_supported":
            handler._json(501, {"code": "not_supported"})
        elif code == "host_offline":
            handler._json(503, {"code": "host_offline"})
        else:
            handler._json(403, {"code": "forbidden"})
        return
    handler._json(200, {})


def _h_guest_leave(handler, service, ctx, config, args, params) -> None:
    if _read_strict_body(handler, set()) is None:
        return
    try:
        service.leave(args[0])
    except GuestServiceError as exc:
        handler._json(503 if str(exc) == "host_offline" else 403,
                      {"code": "host_offline" if str(exc) == "host_offline" else "forbidden"})
        return
    handler._json(200, {})


# --------------------------------------------------------------------------
# Pairing routes (P3b.1a-2).  Same auth/Origin/404/no-store rules; request and response bodies carry pairing codes
# and SAS values and are never logged.
# --------------------------------------------------------------------------

_PAIRING_PATH = re.compile(r"^/(?:pairing(?:/.*)?|peers(?:/.*)?)$")
_PAIRING_ERROR_STATUS = {
    "pairing_code_invalid": (400, "invalid_request"),
    "pairing_sas_invalid": (400, "invalid_request"),
    "pairing_unavailable": (409, "conflict"),
    "pairing_sas_mismatch": (409, "conflict"),
    "pairing_pending_exists": (409, "conflict"),
    "pairing_cooldown": (429, "too_many_attempts"),
    "pairing_native_unavailable": (503, "unavailable"),
    "pairing_identity_unavailable": (503, "unavailable"),
    "pairing_busy": (503, "unavailable"),
}


def _pairing_catalog(config, ctx) -> dict:
    if _PAIRING_CATALOG_SOURCE is not None:
        return _PAIRING_CATALOG_SOURCE(config, ctx)
    if config.native_rpc is None:
        raise ServiceError("native_unavailable")

    async def _fetch():
        async with _connect_native_rpc(config.native_rpc) as rpc:
            result = await rpc("groups.capabilities", {})
        link = result.get("room_link") if isinstance(result, dict) else None
        if not isinstance(link, dict) or link.get("enabled") is not True or not isinstance(link.get("catalog"), dict):
            raise ServiceError("native_contract_unavailable")
        return link["catalog"]

    return asyncio.run(asyncio.wait_for(_fetch(), timeout=_ACTIVATE_TIMEOUT_SECONDS))


def _pairing_service(config, ctx) -> PairingService:
    return PairingService(config.pairing, installation_id=ctx.installation_id, profile=ctx.profile,
                          catalog_source=lambda: _pairing_catalog(config, ctx))


def _pairing_call(handler, ctx, config, fn, *, status=200):
    """Run ``fn(service)``; map every failure to a generic response.  A corrupt journal or a tampered projection
    makes the pairing feature look absent (404 + one sanitized machine-code warning), never partially working."""
    try:
        result = fn(_pairing_service(config, ctx))
    except PairingError as exc:
        code_status = _PAIRING_ERROR_STATUS.get(exc.code)
        if code_status is None:
            handler._json(500, {"error": "internal"})
        else:
            handler._json(code_status[0], {"error": code_status[1], "code": exc.code})
        return
    except JournalError as exc:
        if exc.code == E_BUSY:
            handler._json(503, {"error": "unavailable", "code": "pairing_busy"})
            return
        runtime_config._warn_once("pairing-" + exc.code, "pairing journal unavailable (" + exc.code + ")")
        handler._json(404, {"error": "not_found", "path": handler.path.split("?", 1)[0]})
        return
    handler._json(status, result)


def _body_code(handler) -> Optional[str]:
    if not _content_type_is_json(handler):
        handler._json(400, {"error": "bad_request", "code": "invalid_content_type"})
        return None
    body = _read_strict_body(handler, {"code"})
    if body is None:
        return None
    code = body.get("code")
    if not isinstance(code, str) or not 0 < len(code) <= pairing_codec.MAX_WIRE_CHARS:
        handler._json(400, {"error": "bad_request", "code": "invalid_body"})
        return None
    return code


def _body_sas(handler) -> Optional[str]:
    if not _content_type_is_json(handler):
        handler._json(400, {"error": "bad_request", "code": "invalid_content_type"})
        return None
    body = _read_strict_body(handler, {"sas"})
    if body is None:
        return None
    sas = body.get("sas")
    if not isinstance(sas, str) or len(sas) > 64:
        handler._json(400, {"error": "bad_request", "code": "invalid_body"})
        return None
    return sas


def _h_pairing_offer(handler, service, ctx, config, args, params) -> None:
    if _read_strict_body(handler, set()) is None:
        return
    _pairing_call(handler, ctx, config, lambda s: {"offer": s.start_offer()})


def _h_pairing_accept(handler, service, ctx, config, args, params) -> None:
    code = _body_code(handler)
    if code is not None:
        _pairing_call(handler, ctx, config, lambda s: {"reveal": s.accept_offer(args[0], code)})


def _h_pairing_confirm_offer(handler, service, ctx, config, args, params) -> None:
    sas = _body_sas(handler)
    if sas is not None:
        _pairing_call(handler, ctx, config, lambda s: {"pairing": s.confirm_offer(args[0], sas)})


def _h_pairing_cancel(handler, service, ctx, config, args, params) -> None:
    if _read_strict_body(handler, set()) is None:
        return
    _pairing_call(handler, ctx, config, lambda s: (s.cancel_offer(args[0]), {"cancelled": True})[1])


def _h_pairing_import(handler, service, ctx, config, args, params) -> None:
    code = _body_code(handler)
    if code is not None:
        _pairing_call(handler, ctx, config, lambda s: {"accept": s.import_offer(code)})


def _h_pairing_reveal(handler, service, ctx, config, args, params) -> None:
    code = _body_code(handler)
    if code is not None:
        _pairing_call(handler, ctx, config, lambda s: s.reveal_import(args[0], code))


def _h_pairing_confirm_import(handler, service, ctx, config, args, params) -> None:
    sas = _body_sas(handler)
    if sas is not None:
        _pairing_call(handler, ctx, config, lambda s: {"pairing": s.confirm_import(args[0], sas)})


def _h_pairing_list(handler, service, ctx, config, args, params) -> None:
    _pairing_call(handler, ctx, config, lambda s: {"available": True, "pairings": s.list_pairings()})


def _h_pairing_peers(handler, service, ctx, config, args, params) -> None:
    _pairing_call(handler, ctx, config, lambda s: {"peers": s.list_peers()})


def _h_pairing_unpair(handler, service, ctx, config, args, params) -> None:
    _pairing_call(handler, ctx, config, lambda s: {"unpaired": True, **s.unpair(args[0])})


_ROUTES = (
    ("POST", re.compile(r"^/lobbies$"), _h_create_lobby),
    ("GET", re.compile(r"^/guest-rooms$"), _h_guest_rooms),
    ("GET", re.compile(r"^/guest-rooms/([A-Za-z0-9][A-Za-z0-9._:-]{0,127})/log$"), _h_guest_log),
    ("GET", re.compile(r"^/guest-rooms/([A-Za-z0-9][A-Za-z0-9._:-]{0,127})/my-bot$"), _h_guest_bot),
    ("POST", re.compile(r"^/guest-rooms/([A-Za-z0-9][A-Za-z0-9._:-]{0,127})/approvals/([A-Za-z0-9][A-Za-z0-9._:-]{0,127})$"), _h_guest_approval),
    ("POST", re.compile(r"^/guest-rooms/([A-Za-z0-9][A-Za-z0-9._:-]{0,127})/leave$"), _h_guest_leave),
    ("GET", re.compile(rf"^/lobbies/({_LOBBY_ID})$"), _h_inspect_lobby),
    ("POST", re.compile(rf"^/lobbies/({_LOBBY_ID})/invites$"), _h_issue_invite),
    ("POST", re.compile(rf"^/lobbies/({_LOBBY_ID})/invites/({_INVITE_ID})/revoke$"), _h_revoke_invite),
    ("POST", re.compile(rf"^/lobbies/({_LOBBY_ID})/invites/({_INVITE_ID})/decision$"), _h_decide_host),
    ("GET", re.compile(rf"^/lobbies/({_LOBBY_ID})/activation$"), _h_activation_status),
    ("POST", re.compile(rf"^/lobbies/({_LOBBY_ID})/activate$"), _h_activate),
    ("GET", re.compile(r"^/paired-peers$"), _h_paired_peers),
)

_PAIRING_ROUTES = (
    ("POST", re.compile(r"^/pairing/offers$"), _h_pairing_offer),
    ("POST", re.compile(rf"^/pairing/offers/({_PAIRING_ID})/accept$"), _h_pairing_accept),
    ("POST", re.compile(rf"^/pairing/offers/({_PAIRING_ID})/confirm$"), _h_pairing_confirm_offer),
    ("POST", re.compile(rf"^/pairing/offers/({_PAIRING_ID})/cancel$"), _h_pairing_cancel),
    ("POST", re.compile(r"^/pairing/imports$"), _h_pairing_import),
    ("POST", re.compile(rf"^/pairing/imports/({_PAIRING_ID})/reveal$"), _h_pairing_reveal),
    ("POST", re.compile(rf"^/pairing/imports/({_PAIRING_ID})/confirm$"), _h_pairing_confirm_import),
    ("GET", re.compile(r"^/pairing$"), _h_pairing_list),
    ("GET", re.compile(r"^/peers$"), _h_pairing_peers),
    ("DELETE", re.compile(rf"^/peers/({_PEER_ID})$"), _h_pairing_unpair),
)


def handle_request(handler, method: str, path: str, params: dict, is_authorized) -> bool:
    """Entry point called from local_telemetry_server's thin dispatch.

    Returns False only when ``path`` is not under :data:`PREFIX` at all (the
    caller should keep its own fallback behavior in that case). Every path
    under the prefix is fully owned here and always results in a response.
    """
    if not (path == PREFIX or path.startswith(PREFIX + "/")):
        return False

    # Auth first, exactly like every other route in this server: an invalid
    # or missing bearer token gets the same 401 regardless of whether the
    # feature is enabled or the path below it would otherwise exist.
    if not is_authorized(handler):
        handler._unauthorized()
        return True

    is_guest_path = path[len(PREFIX):].startswith("/guest-rooms")
    if is_guest_path:
        guest = runtime_config.load_guest_service(
            channel_factory=_HOST_CHANNEL_FACTORY, rpc_factory=_guest_rpc_factory)
        loaded = (guest, None, None) if guest is not None else None
    else:
        loaded = runtime_config.load_service(material_resolver=_MATERIAL_RESOLVER)
    if loaded is None:
        # Disabled or misconfigured: identical to an unknown path.
        handler._json(404, {"error": "not_found", "path": path})
        return True
    service, ctx, config = loaded

    suffix = path[len(PREFIX):] or "/"
    if _PAIRING_PATH.match(suffix) and config.pairing is None:
        # Pairing OFF (absent/invalid block): indistinguishable from an unknown path, before the Origin check.
        handler._json(404, {"error": "not_found", "path": path})
        return True

    if not _origin_allowed(handler):
        handler._json(403, {"error": "forbidden_origin"})
        return True

    for allowed_method, pattern, fn in _ROUTES + _PAIRING_ROUTES:
        if allowed_method != method:
            continue
        match = pattern.match(suffix)
        if match:
            try:
                fn(handler, service, ctx, config, match.groups(), params)
            except Exception as exc:
                # Sanitized: exception type name + route pattern only, never a
                # traceback (which can carry local values, secrets or paths).
                _LOG.error("room-invites route handler failed: %s pattern=%s",
                           type(exc).__name__, pattern.pattern)
                handler._json(500, {"error": "internal"})
            return True

    handler._json(404, {"error": "not_found", "path": path})
    return True
