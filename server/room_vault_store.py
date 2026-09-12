"""Durable Mission Control metadata: room → vault routing.

Group Chat rooms live in state.db (core, transient retention); the vault a
room must synthesize into belongs to MC-level curation (per-vault nightly).
Keep the routing map outside core, mirroring chat_title_store.py. The nightly
room_inventory reads it to pick the BDH vault per room.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Dict

SERVER_DIR = Path(__file__).resolve().parent
_STATE_FILE = SERVER_DIR / "room_vaults.json"
_LOCK = threading.RLock()


def _read() -> Dict[str, str]:
    try:
        payload = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
        return {str(key): str(value) for key, value in payload.items() if isinstance(key, str) and isinstance(value, str)} if isinstance(payload, dict) else {}
    except (FileNotFoundError, OSError, ValueError):
        return {}


def _write(payload: Dict[str, str]) -> None:
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = _STATE_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(_STATE_FILE)


def set_room_vault(room_id: str, vault: str) -> Dict[str, Any]:
    room = str(room_id or "").strip()
    target = str(vault or "").strip()[:64]
    if not room or not target:
        raise ValueError("room_id and vault are required")
    with _LOCK:
        payload = _read()
        payload[room] = target
        _write(payload)
    return {"roomId": room, "vault": target}


def get_room_vault(room_id: str) -> str:
    with _LOCK:
        return _read().get(str(room_id or "").strip(), "")


def list_room_vaults() -> Dict[str, str]:
    with _LOCK:
        return dict(_read())


def clear_room_vault(room_id: str) -> bool:
    room = str(room_id or "").strip()
    with _LOCK:
        payload = _read()
        removed = payload.pop(room, None) is not None
        if removed:
            _write(payload)
        return removed
