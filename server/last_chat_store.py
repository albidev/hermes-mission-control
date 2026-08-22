"""Last-active-chat state, shared across all Mission Control devices.

A tiny JSON-file-backed store so desktop and mobile land on the SAME chat:
whichever device opens/resumes a session last becomes the "current" chat for
every device (Discord-style). Zero core involvement — the ChatDrawer reads and
writes this through the local telemetry server.
"""

from __future__ import annotations

import json
import threading
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


def get_last_chat() -> Optional[Dict[str, Any]]:
    """Return the last active chat snapshot, or None when never set."""
    with _LOCK:
        data = _load()
    if not data.get("sessionId"):
        return None
    return {
        "sessionId": str(data["sessionId"]),
        "sessionKey": data.get("sessionKey"),
        "sessionTitle": data.get("sessionTitle"),
        "modelIdentity": data.get("modelIdentity"),
        "updatedAt": data.get("updatedAt"),
    }


def set_last_chat(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Persist the last active chat. Only known keys are stored."""
    with _LOCK:
        data = _load()
        for key in _ALLOWED_KEYS:
            if key in payload and payload[key] is not None:
                data[key] = payload[key]
        data["updatedAt"] = int(payload.get("updatedAt") or 0)
        _save(data)
    return get_last_chat() or {}


def clear_last_chat() -> None:
    with _LOCK:
        try:
            _STATE_FILE.unlink()
        except FileNotFoundError:
            pass
