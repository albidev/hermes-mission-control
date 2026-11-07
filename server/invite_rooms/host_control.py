"""Contextual W2b host-side invitation control handlers.

This module is deliberately a thin wire adapter: journal-authenticated identity and
binding come only from ``AuthenticatedPeerContext``; all persistence and admission
logic remains in ``RoomInviteStore`` H2 helpers.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from . import consent
from .control.dispatcher import ControlError
from .service import OwnerContext


_PURPOSE = "joining-owner-consent-v1"
_STATUSES = frozenset({"awaiting_host_approval", "consent_required", "ready", "denied", "refused"})
_BINDING_KEYS = frozenset({"endpoint_id", "fingerprint", "target_install_id", "target_profile",
                           "catalog_digest", "policy_digest"})
_SCOPE_KEYS = frozenset({"host_id", "room_id", "member_id", "lobby_id", "invite_id", "binding"})


def _unavailable() -> ControlError:
    return ControlError("control_unavailable")


def _request(request: Any, expected: set[str]) -> dict:
    if not isinstance(request, dict) or set(request) != expected:
        raise _unavailable()
    return request


def _ids(request: dict) -> None:
    if (type(request.get("lobby_id")) is not str or not request["lobby_id"]
            or type(request.get("invite_id")) is not str or not request["invite_id"]):
        raise _unavailable()


def _context_args(context: Any) -> tuple[str, dict, Any]:
    try:
        peer_id = context.peer_id
        binding = context.binding_dict()
        readmit = context.readmit
    except Exception:
        raise _unavailable() from None
    if type(peer_id) is not str or not peer_id or not isinstance(binding, dict) or set(binding) != _BINDING_KEYS:
        raise _unavailable()
    if not callable(readmit):
        raise _unavailable()
    return peer_id, binding, readmit


def _preview(store: Any, context: Any, request: Any) -> dict:
    request = _request(request, {"op", "lobby_id", "invite_id"})
    if request["op"] != "preview":
        raise _unavailable()
    _ids(request)
    peer_id, binding, readmit = _context_args(context)
    result = store.peer_preview(request["lobby_id"], request["invite_id"], peer_id=peer_id,
                                binding=binding, before_return=readmit)
    if (not isinstance(result, dict) or set(result) != {"name", "expires_at"}
            or type(result["name"]) is not str or type(result["expires_at"]) not in (int, float)):
        raise _unavailable()
    return {"name": result["name"], "expires_at": result["expires_at"]}


def _redeem(store: Any, context: Any, request: Any) -> dict:
    request = _request(request, {"op", "lobby_id", "invite_id", "secret"})
    if request["op"] != "redeem":
        raise _unavailable()
    _ids(request)
    if type(request["secret"]) is not str or not request["secret"]:
        raise _unavailable()
    peer_id, binding, readmit = _context_args(context)
    store.redeem(request["lobby_id"], request["invite_id"], request["secret"], peer_id=peer_id,
                 binding=binding, before_effect=readmit)
    return {"status": "awaiting_host_approval"}


def _scope(result: Any) -> tuple[str, dict]:
    if not isinstance(result, dict) or set(result) != {"status", "scope"}:
        raise _unavailable()
    status, scope = result["status"], result["scope"]
    if status not in _STATUSES or not isinstance(scope, dict) or set(scope) != _SCOPE_KEYS:
        raise _unavailable()
    if any(type(scope[key]) is not str or not scope[key] for key in _SCOPE_KEYS - {"binding"}):
        raise _unavailable()
    binding = scope["binding"]
    if not isinstance(binding, dict) or set(binding) != _BINDING_KEYS:
        raise _unavailable()
    if any(type(value) is not str or not value for value in binding.values()):
        raise _unavailable()
    return status, scope


def _status(store: Any, context: Any, request: Any) -> dict:
    request = _request(request, {"op", "lobby_id", "invite_id"})
    if request["op"] != "status":
        raise _unavailable()
    _ids(request)
    peer_id, binding, readmit = _context_args(context)
    result = store.peer_scope_status(request["lobby_id"], request["invite_id"], peer_id=peer_id,
                                     binding=binding, before_return=readmit)
    status, scope = _scope(result)
    return {"status": status, "scope": scope}


def _consent(store: Any, context: Any, request: Any) -> dict:
    request = _request(request, {"op", "lobby_id", "invite_id", "receipt", "signature"})
    if request["op"] not in {"consent", "joining_consent"}:
        raise _unavailable()
    _ids(request)
    if not isinstance(request["receipt"], dict) or type(request["signature"]) is not str or not request["signature"]:
        raise _unavailable()
    peer_id, binding, readmit = _context_args(context)
    current = store.peer_scope_status(request["lobby_id"], request["invite_id"], peer_id=peer_id,
                                      binding=binding, before_return=readmit)
    status, scope = _scope(current)
    if status not in {"consent_required", "ready"}:
        raise _unavailable()
    expected = dict(scope, purpose=_PURPOSE)
    try:
        consent.verify(context.consent_public_key, request["receipt"], request["signature"], expected)
    except Exception:
        raise _unavailable() from None
    store.consent_joining(request["lobby_id"], request["invite_id"], binding=binding, consented=True,
                          peer_id=peer_id, before_effect=readmit)
    try:
        admitted = readmit()
    except Exception:
        admitted = False
    if admitted is not True:
        raise _unavailable()
    return {"status": "ready"}


def build_host_handlers(store: Any, owner_context: Any = None) -> dict[str, Any]:
    """Build the injectable H3 contextual handler map; production listener defaults remain empty."""
    if not isinstance(owner_context, OwnerContext):
        raise ValueError("owner context required")
    if store is None or not callable(getattr(store, "is_owner_scope_bound", None)):
        raise ValueError("owner-bound store required")
    if not store.is_owner_scope_bound(owner_context.installation_id, owner_context.profile):
        raise ValueError("owner/store mismatch")
    # The dispatcher supplies the immutable, journal-issued remote peer context
    # per call; this local context only validates the store dependency at build.
    return {
        "preview": lambda context, request: _safe(_preview, store, context, request),
        "redeem": lambda context, request: _safe(_redeem, store, context, request),
        "status": lambda context, request: _safe(_status, store, context, request),
        "joining_consent": lambda context, request: _safe(_consent, store, context, request),
    }


def _safe(function, store, context, request):
    try:
        return function(store, context, request)
    except ControlError:
        raise
    except Exception:
        raise _unavailable() from None
