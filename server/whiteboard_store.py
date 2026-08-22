"""Session-scoped whiteboard snapshots and agent commands."""
from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any

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


def get_whiteboard(session_id: str) -> dict[str, Any]:
    with _LOCK:
        state = _load().get(session_id) or {}
    return {
        "sessionId": session_id,
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


def enqueue_command(session_id: str, command: dict[str, Any]) -> dict[str, Any]:
    command = dict(command)
    if command.get("type") == "create_line" and isinstance(command.get("w"), (int, float)):
        width = float(command["w"])
        if width < 0:
            command["x"] = float(command.get("x") or 180) + width
            command["w"] = abs(width)
    item = {"id": uuid.uuid4().hex, "createdAt": int(time.time() * 1000), **command}
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
