"""Ephemeral runtime presence for Mission Control chat resumes.

This is deliberately separate from the shared last-chat pointer.  The pointer
answers "which chat should a device open?"; this module answers "which resumed
runtime is currently attached to a Mission Control client?".

Presence is process-local and lease-based.  A telemetry restart clears it, and
clients re-publish on their next heartbeat.  Nothing here mutates SessionDB or
Hermes core session identity.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict, List


PRESENCE_TTL_SECONDS = 20.0

_ALLOWED_PHASES = frozenset({"connected", "running", "closed"})

_LOCK = threading.Lock()
_LEASES: Dict[tuple[str, str], Dict[str, Any]] = {}


def _text(value: Any, limit: int = 240) -> str:
    return str(value or "").strip()[:limit]


def _prune(now: float) -> None:
    expired = [key for key, lease in _LEASES.items() if float(lease.get("expiresAt", 0)) <= now]
    for key in expired:
        _LEASES.pop(key, None)


def update_runtime_presence(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Upsert one client lease or close it, returning the active lease count."""
    client_id = _text(payload.get("clientId"), 160)
    runtime_session_id = _text(payload.get("runtimeSessionId"), 160)
    phase = _text(payload.get("phase"), 32).lower()
    if not client_id:
        raise ValueError("clientId is required")
    if not runtime_session_id:
        raise ValueError("runtimeSessionId is required")
    if phase not in _ALLOWED_PHASES:
        raise ValueError("phase must be connected, running, or closed")

    def _valid_profile(value: Any) -> str | None:
        profile = str(value or "").strip()
        return profile or None

    key = (client_id, runtime_session_id)
    now = time.time()
    with _LOCK:
        _prune(now)
        if phase == "closed":
            _LEASES.pop(key, None)
        else:
            model = payload.get("model")
            _LEASES[key] = {
                "clientId": client_id,
                "runtimeSessionId": runtime_session_id,
                "resumedFrom": _text(payload.get("resumedFrom"), 160) or None,
                "sessionKey": _text(payload.get("sessionKey"), 240) or None,
                "profile": _valid_profile(payload.get("profile")),
                "phase": phase,
                "source": _text(payload.get("source"), 80) or "mission-control",
                "title": _text(payload.get("title"), 240) or None,
                "model": _text(model, 240) or "unknown",
                "updatedAt": now,
                "expiresAt": now + PRESENCE_TTL_SECONDS,
            }
        return {
            "success": True,
            "activeLeases": len(_LEASES),
            "runtimeSessionId": runtime_session_id,
            "phase": phase,
        }


def active_runtime_presences() -> List[Dict[str, Any]]:
    """Return a snapshot of non-expired resume runtime leases."""
    now = time.time()
    with _LOCK:
        _prune(now)
        return [dict(lease) for lease in _LEASES.values()]


def clear_runtime_presence() -> None:
    """Test helper; production callers rely on lease expiry instead."""
    with _LOCK:
        _LEASES.clear()
