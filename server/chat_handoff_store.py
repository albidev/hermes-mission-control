"""Durable Mission Control-only storage for attributed Bot handoffs."""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Dict, List

SERVER_DIR = Path(__file__).resolve().parent
_STATE_FILE = SERVER_DIR / "chat_handoffs.json"
_LOCK = threading.RLock()


def _read() -> Dict[str, List[Dict[str, Any]]]:
    try:
        payload = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (FileNotFoundError, OSError, ValueError):
        return {}


def _write(payload: Dict[str, List[Dict[str, Any]]]) -> None:
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = _STATE_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(_STATE_FILE)


def list_handoffs(session_id: str) -> List[Dict[str, Any]]:
    sid = str(session_id or "").strip()
    if not sid:
        return []
    with _LOCK:
        return [dict(item) for item in _read().get(sid, []) if isinstance(item, dict)]


def upsert_handoff(session_id: str, handoff: Dict[str, Any]) -> Dict[str, Any]:
    sid = str(session_id or "").strip()
    handoff_id = str(handoff.get("id") or "").strip()
    if not sid or not handoff_id:
        raise ValueError("session_id and handoff id are required")
    with _LOCK:
        payload = _read()
        rows = [dict(item) for item in payload.get(sid, []) if isinstance(item, dict)]
        for index, current in enumerate(rows):
            if str(current.get("id") or "").strip() == handoff_id:
                current_updated = current.get("updatedAt")
                incoming_updated = handoff.get("updatedAt")
                if isinstance(current_updated, (int, float)) and isinstance(incoming_updated, (int, float)) and incoming_updated < current_updated:
                    return dict(current)
                rows[index] = {**current, **handoff}
                payload[sid] = rows
                _write(payload)
                return dict(rows[index])
        rows.append(dict(handoff))
        payload[sid] = rows
        _write(payload)
        return dict(handoff)


def clear_handoffs() -> None:
    with _LOCK:
        try:
            _STATE_FILE.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            _write({})
