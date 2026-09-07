"""Last-active-chat state, shared across all Mission Control devices.

A tiny JSON-file-backed store so desktop and mobile land on the SAME chat:
whichever device opens/resumes a session last becomes the "current" chat for
every device (Discord-style). Zero core involvement — the ChatDrawer reads and
writes this through the local telemetry server.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

SERVER_DIR = Path(__file__).resolve().parent
_STATE_FILE = SERVER_DIR / "last_chat.json"
_LOCK = threading.Lock()

_ALLOWED_KEYS = {"sessionId", "sessionKey", "sessionTitle", "modelIdentity"}


def _load() -> Dict[str, Any]:
    try:
        if _STATE_FILE.exists():
            data = json.loads(_STATE_FILE.read_text())
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def _save(data: Dict[str, Any]) -> None:
    tmp = _STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(_STATE_FILE)


def _stored_revision(data: Dict[str, Any]) -> int:
    """Return the current server revision, including for legacy state."""
    if not data.get("sessionId"):
        return 0
    try:
        revision = int(data.get("revision", 1))
    except (TypeError, ValueError):
        revision = 1
    return max(1, revision)


def _canonical_pointer(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not data.get("sessionId"):
        return None
    pointer = {
        "sessionId": str(data["sessionId"]),
        "sessionKey": data.get("sessionKey"),
        "sessionTitle": data.get("sessionTitle"),
        "modelIdentity": data.get("modelIdentity"),
        "revision": _stored_revision(data),
        "updatedAt": data.get("updatedAt"),
    }
    return pointer


def get_last_chat() -> Optional[Dict[str, Any]]:
    """Return the last active chat snapshot, or None when never set."""
    with _LOCK:
        return _canonical_pointer(_load())


def set_last_chat(
    payload: Dict[str, Any],
    expected_revision: Optional[int] = None,
) -> Dict[str, Any]:
    """CAS-update the last active chat using a server-owned revision.

    The first claim may omit ``expected_revision``. Once a pointer exists, a
    matching revision is required; stale or missing revisions return the
    canonical pointer without changing it. ``updatedAt`` is always generated
    by the server and is never taken from the client payload.
    """
    if expected_revision is None and "expectedRevision" in payload:
        expected_revision = payload.get("expectedRevision")

    with _LOCK:
        data = _load()
        current_revision = _stored_revision(data)
        current_pointer = _canonical_pointer(data)
        if current_pointer is not None and expected_revision != current_revision:
            return {
                "accepted": False,
                "conflict": True,
                "lastChat": current_pointer,
            }

        for key in _ALLOWED_KEYS:
            if key in payload and payload[key] is not None:
                data[key] = payload[key]
        next_revision = current_revision + 1
        previous_updated_at = data.get("updatedAt")
        try:
            previous_updated_at = int(previous_updated_at)
        except (TypeError, ValueError):
            previous_updated_at = 0
        data["revision"] = next_revision
        data["updatedAt"] = max(int(time.time() * 1000), previous_updated_at + 1)
        _save(data)
        pointer = _canonical_pointer(data)
        if pointer is None:  # sessionId is validated at the HTTP boundary.
            raise ValueError("Missing sessionId")
        return {
            "accepted": True,
            "conflict": False,
            "lastChat": pointer,
        }


def clear_last_chat() -> None:
    with _LOCK:
        try:
            _STATE_FILE.unlink()
        except FileNotFoundError:
            pass
