"""Last-active-room pointer, shared across all Mission Control devices.

Mirror of last_chat_store.py: a tiny JSON-file-backed store so desktop and
mobile land on the SAME room. Whichever device selects a room last becomes
the "current" room for every device (Discord-style). Zero core involvement:
the drawer reads/writes this through the local telemetry server, and the
localStorage key is only a synchronous fallback for first paint.

The pointer is a roomId (+ room name snapshot for debuggability). CAS is
revisioned exactly like the chat pointer; a stale client gets 409 and can
adopt the canonical room.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

SERVER_DIR = Path(__file__).resolve().parent
_STATE_FILE = SERVER_DIR / "last_room.json"
_LOCK = threading.Lock()

_ALLOWED_KEYS = {"roomId", "roomName"}


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
    if not data.get("roomId"):
        return 0
    try:
        revision = int(data.get("revision", 1))
    except (TypeError, ValueError):
        revision = 1
    return max(1, revision)


def _canonical_pointer(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not data.get("roomId"):
        return None
    return {
        "roomId": str(data["roomId"]),
        "roomName": data.get("roomName"),
        "revision": _stored_revision(data),
        "updatedAt": data.get("updatedAt"),
    }


def get_last_room() -> Optional[Dict[str, Any]]:
    with _LOCK:
        return _canonical_pointer(_load())


def set_last_room(
    payload: Dict[str, Any],
    expected_revision: Optional[int] = None,
) -> Dict[str, Any]:
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
                "lastRoom": current_pointer,
            }

        for key in _ALLOWED_KEYS:
            if key in payload and payload[key] is not None:
                data[key] = payload[key]
        next_revision = current_revision + 1
        raw_updated_at = data.get("updatedAt")
        if isinstance(raw_updated_at, (int, float)) and raw_updated_at > 0:
            previous_updated_at = int(raw_updated_at)
        else:
            previous_updated_at = 0
        data["revision"] = next_revision
        data["updatedAt"] = max(int(time.time() * 1000), previous_updated_at + 1)
        _save(data)
        pointer = _canonical_pointer(data)
        if pointer is None:  # roomId is validated at the HTTP boundary.
            raise ValueError("Missing roomId")
        return {
            "accepted": True,
            "conflict": False,
            "lastRoom": pointer,
        }


def clear_last_room() -> None:
    with _LOCK:
        try:
            _STATE_FILE.unlink()
        except FileNotFoundError:
            pass
