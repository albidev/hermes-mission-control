"""Session-scoped whiteboard snapshots and agent commands.

Protocol contract (v2):
- Every GET response carries `protocolVersion` and `features`.
- Every queued command carries `protocolVersion` and `feature` when known.
- A client must only acknowledge commands it actually applied.
- A client that does not support a command's feature must leave it queued.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any

WHITEBOARD_PROTOCOL_VERSION = 2

WHITEBOARD_FEATURES: dict[str, int] = {
    "clear": 1,
    "create_text": 1,
    "create_line": 1,
    "create_box": 1,
    "create_frame": 1,
    "create_arrow": 1,
    "create_shape": 1,
    "move_shape": 2,
    "update_shape": 1,
    "delete_shapes": 1,
    "delete_bindings": 2,
    "duplicate": 1,
    "group": 1,
    "ungroup": 1,
    "bring_to_front": 1,
    "send_to_back": 1,
    "zoom_to_fit": 1,
    "create_binding": 2,
    "create_page": 2,
    "set_current_page": 2,
    "rename_page": 2,
    "delete_page": 2,
    "move_shapes_to_page": 2,
    "align_shapes": 2,
    "distribute_shapes": 2,
    "pack_shapes": 2,
    "flip_shapes": 2,
    "rotate_shapes": 2,
    "resize_shape": 2,
    "toggle_lock": 2,
    "set_style": 2,
    "set_opacity": 2,
    "export_json": 2,
    "export_svg": 2,
    "export_png": 2,
}

_STATE_FILE = Path(__file__).resolve().parent / "whiteboard_state.json"
_LOCK = threading.Lock()


def _load() -> dict[str, Any]:
    try:
        data = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save(data: dict[str, Any]) -> None:
    tmp = _STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(_STATE_FILE)


def get_whiteboard(session_id: str, fallback_session_id: str | None = None) -> dict[str, Any]:
    with _LOCK:
        data = _load()
        state = data.get(session_id) or {}
        if not state and fallback_session_id and fallback_session_id != session_id:
            state = data.get(fallback_session_id) or {}
    return {
        "sessionId": session_id,
        "protocolVersion": WHITEBOARD_PROTOCOL_VERSION,
        "features": sorted(WHITEBOARD_FEATURES),
        "snapshot": state.get("snapshot"),
        "updatedAt": state.get("updatedAt", 0),
        "commands": state.get("commands", []),
    }


def save_snapshot(session_id: str, snapshot: Any) -> dict[str, Any]:
    with _LOCK:
        data = _load()
        state = data.setdefault(session_id, {})
        state["snapshot"] = snapshot
        state["updatedAt"] = int(time.time() * 1000)
        _save(data)
    return get_whiteboard(session_id)


def feature_for_command(command_type: str) -> str | None:
    feature = WHITEBOARD_FEATURES.get(command_type)
    return str(feature) if feature is not None else None


def enqueue_command(session_id: str, command: dict[str, Any]) -> dict[str, Any]:
    command = dict(command)
    if command.get("type") == "create_line" and isinstance(command.get("w"), (int, float)):
        width = float(command["w"])
        if width < 0:
            command["x"] = float(command.get("x") or 180) + width
            command["w"] = abs(width)
    command_type = str(command.get("type") or "")
    item = {
        "id": uuid.uuid4().hex,
        "createdAt": int(time.time() * 1000),
        "protocolVersion": WHITEBOARD_PROTOCOL_VERSION,
        "feature": feature_for_command(command_type),
        **command,
    }
    with _LOCK:
        data = _load()
        state = data.setdefault(session_id, {})
        state.setdefault("commands", []).append(item)
        state["commands"] = state["commands"][-100:]
        _save(data)
    return item


def acknowledge_commands(session_id: str, command_ids: list[str]) -> None:
    with _LOCK:
        data = _load()
        state = data.get(session_id)
        if not state:
            return
        state["commands"] = [item for item in state.get("commands", []) if item.get("id") not in command_ids]
        _save(data)
