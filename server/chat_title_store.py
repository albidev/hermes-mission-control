"""Durable Mission Control metadata for chat titles.

Hermes owns SessionDB; MC-only sessions can exist before Hermes has a durable
message row. Keep this override outside core and let readers merge it by both
runtime id and persistent session key.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Dict

SERVER_DIR = Path(__file__).resolve().parent
_STATE_FILE = SERVER_DIR / "chat_titles.json"
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


def set_chat_title(session_id: str, session_key: str, title: str) -> Dict[str, Any]:
    sid = str(session_id or "").strip()
    skey = str(session_key or "").strip()
    value = str(title or "").strip()[:120]
    keys = [key for key in (sid, skey) if key]
    if not keys or not value:
        raise ValueError("session_id, session_key and title are required")
    with _LOCK:
        payload = _read()
        for key in keys:
            payload[key] = value
        _write(payload)
    return {"sessionId": sid, "sessionKey": skey, "title": value}


def get_chat_title(*identifiers: Any) -> str:
    with _LOCK:
        payload = _read()
    for identifier in identifiers:
        key = str(identifier or "").strip()
        if key and payload.get(key):
            return payload[key]
    return ""
