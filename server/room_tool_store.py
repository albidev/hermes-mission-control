"""Mission Control Group Room tool-trace store.

Mission Control-owned edge collector: it is the *only* component that knows
how to turn member activity into tool traces for the Rooms UI. It reads two
kinds of SQLite files **read-only** and never writes to Hermes core:

1. ``~/.hermes/state.db`` — the hosted room registry (rooms + members).
2. ``~/.hermes/profiles/<member-profile>/state.db`` — each member profile's
   sessions table. Room member turns live under ``Group: <room_id>`` sessions
   and the canonical stream already persists the tool_calls / tool rows with
   full payloads (this is where the normal chat UI reads tool traces).

The produced snapshot is written to a Mission Control-owned JSON cache file
(``server/room_tools.json``, gitignored) so the dashboard-api endpoint can
serve it without touching Hermes core.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger("mission_control.room_tools")

MAX_TOOL_TEXT = 6000
DEFAULT_CACHE_TTL_SECONDS = int(os.environ.get("MC_ROOM_TOOLS_TTL_SECONDS", "5"))


class RoomToolsError(RuntimeError):
    """Raised when the room tool store cannot be read safely."""


def _configured_db_path(env_key: str, fallback: Path) -> Path:
    configured = os.environ.get(env_key, "").strip()
    return Path(configured).expanduser() if configured else fallback


def room_store_path() -> Path:
    return _configured_db_path("MC_GROUP_ROOMS_DB_PATH", Path.home() / ".hermes" / "state.db")


def profile_store_path(profile: str) -> Path:
    if not profile or profile == "default":
        return Path.home() / ".hermes" / "state.db"
    return Path.home() / ".hermes" / "profiles" / profile / "state.db"


def cache_path() -> Path:
    return Path(__file__).resolve().parent / "room_tools.json"


def _connect_readonly(db_path: Path) -> sqlite3.Connection | None:
    """Open a SQLite file read-only, tolerating gateway WAL locks gracefully."""
    if not db_path.is_file():
        return None
    try:
        return sqlite3.connect(
            f"file:{db_path}?mode=ro", uri=True, timeout=0.5
        )
    except (sqlite3.Error, OSError):
        return None


def _json_object(value: Any) -> Any:
    """Parse JSON text into objects; returns {} on failure."""
    if isinstance(value, (dict, list)):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, (dict, list)) else {}
    except (TypeError, ValueError):
        return {}


def _reasoning_text(*values: Any) -> str:
    """Extract the member's chain-of-thought from the reasoning columns.

    Columns may hold plain text, a JSON string of items (``codex_reasoning_items``),
    or a JSON blob with a ``reasoning``/``summary``-style key. Returns the trimmed
    text or empty string.
    """
    for value in values:
        if value is None or value == "":
            continue
        if isinstance(value, str):
            text = value.strip()
            if text.startswith("["):
                try:
                    parsed = json.loads(text)
                    if isinstance(parsed, list):
                        parts = [
                            str(item.get("summary") or item.get("content") or item.get("text") or "").strip()
                            for item in parsed
                            if isinstance(item, dict)
                        ]
                        text = "\n".join(part for part in parts if part)
                        if text:
                            return text
                except (TypeError, ValueError):
                    pass
            return text[: MAX_TOOL_TEXT] if text else ""
        if isinstance(value, (dict, list)):
            if isinstance(value, dict):
                for key in ("summary", "content", "text", "reasoning"):
                    if isinstance(value.get(key), str) and value[key].strip():
                        return value[key].strip()[: MAX_TOOL_TEXT]
            elif isinstance(value, list):
                parts = [
                    str(item.get("summary") or item.get("content") or item.get("text") or "").strip()
                    for item in value
                    if isinstance(item, dict)
                ]
                joined = "\n".join(part for part in parts if part)
                if joined:
                    return joined[: MAX_TOOL_TEXT]
    return ""


def _str(value: Any, max_len: int = MAX_TOOL_TEXT) -> str:
    text = str(value or "").strip()
    return text[:max_len]


def _room_members(room_row: Any) -> list[dict[str, str]]:
    """Parse the members_json column into [(profile, handle, display_name)]."""
    members: list[dict[str, str]] = []
    raw = room_row[2] if len(room_row) > 2 else None
    for item in _json_object(raw):
        if not isinstance(item, dict):
            continue
        profile = str(item.get("profile") or "").strip()
        if not profile:
            continue
        members.append(
            {
                "profile": profile,
                "member_id": str(item.get("member_id") or ""),
                "handle": str(item.get("handle") or profile),
                "display_name": str(item.get("display_name") or item.get("handle") or profile),
            }
        )
    return members


def _member_tool_rows(member: dict[str, str], room_id: str) -> list[dict[str, Any]]:
    """Read tool activity from one member profile's Group session.

    The persisted stream keeps the canonical tool shape: an ``assistant`` row
    carrying ``tool_calls`` (name + typed arguments) followed by one ``tool``
    row per executed call (``tool_name`` + result text). The TUI gateway's
    internal ``tool_call`` wrapper name is translated to the real tool name
    from the call payload so the UI shows the actual tool, not the relay.
    """
    profile = member["profile"]
    db = _connect_readonly(profile_store_path(profile))
    if db is None:
        return []
    try:
        try:
            session = db.execute(
                "SELECT id FROM sessions WHERE title = ? ORDER BY last_activity_at DESC LIMIT 1",
                (f"Group: {room_id}",),
            ).fetchone()
        except sqlite3.OperationalError:
            return []
        if not session:
            return []
        session_id = session[0]
        # Some member profiles may still have an old schema without the
        # reasoning columns — detect what exists and only query what's there.
        has_reasoning_columns = any(
            row[1] in ("reasoning", "reasoning_content", "reasoning_details", "codex_reasoning_items")
            for row in db.execute("PRAGMA table_info(messages)").fetchall()
        )
        reasoning_select = ", reasoning, reasoning_content, reasoning_details, codex_reasoning_items" if has_reasoning_columns else ""
        reason_filter = (
            " OR reasoning IS NOT NULL OR reasoning_content IS NOT NULL "
            "OR reasoning_details IS NOT NULL OR codex_reasoning_items IS NOT NULL"
        ) if has_reasoning_columns else ""
        rows = db.execute(
            f"SELECT role, tool_name, content, tool_calls, timestamp{reasoning_select} "
            "FROM messages WHERE session_id = ? "
            "AND (tool_calls IS NOT NULL OR tool_name IS NOT NULL"
            f"{reason_filter}) "
            "ORDER BY timestamp, rowid",
            (session_id,),
        ).fetchall()
        entries: list[dict[str, Any]] = []
        pending: list[dict[str, Any]] = []
        for row in rows:
            role_s = _str(row[0])
            tool_name, content, tool_calls = row[1], row[2], row[3]
            ts = float(row[4] or 0.0)
            reasoning = row[5] if len(row) > 5 else None
            reasoning_content = row[6] if len(row) > 6 else None
            reasoning_details = row[7] if len(row) > 7 else None
            codex_reasoning_items = row[8] if len(row) > 8 else None
            # Reasoning blocks (the member's hidden chain-of-thought) belong
            # in the per-turn strip too — they are part of the same run as
            # the tool calls. Stored as plain text or JSON items.
            reason_text = _reasoning_text(reasoning, reasoning_content, reasoning_details, codex_reasoning_items)
            if reason_text:
                entries.append({
                    "kind": "reasoning",
                    "output": reason_text,
                    "timestamp": ts,
                    "memberHandle": member.get("handle", profile),
                    "memberProfile": profile,
                    "memberDisplayName": member.get("display_name", profile),
                })
            if tool_calls:
                calls_value = _json_object(tool_calls)
                for call in calls_value:
                    if not isinstance(call, dict):
                        continue
                    fn = call.get("function") if isinstance(call.get("function"), dict) else {}
                    name = str(fn.get("name") or call.get("name") or "").strip()
                    args = str(fn.get("arguments") or call.get("arguments") or "")
                    if not name or name == "tool_call":
                        continue
                    entry: dict[str, Any] = {
                        "toolName": name,
                        "toolInput": _str(args),
                        "output": "",
                        "status": "complete",
                        "timestamp": ts,
                        "memberHandle": member.get("handle", profile),
                        "memberProfile": profile,
                        "memberDisplayName": member.get("display_name", profile),
                    }
                    entries.append(entry)
                    pending.append(entry)
                continue
            name = _str(tool_name)
            if not name or name == "tool_call":
                continue
            # Attach this result to the first still-open call with the same
            # tool name (bounded by the batch: calls open until another
            # assistant call row arrives that resets the pending window).
            attached = False
            for index, candidate in enumerate(pending):
                if candidate["toolName"] == name:
                    candidate["output"] = _str(content)
                    candidate["durationS"] = max(round(ts - candidate.get("timestamp", ts), 2), 0.1)
                    pending.pop(index)
                    attached = True
                    break
            if not attached:
                entries.append({
                    "toolName": name,
                    "toolInput": "",
                    "output": _str(content),
                    "status": "complete",
                    "timestamp": ts,
                    "memberHandle": member.get("handle", profile),
                    "memberProfile": profile,
                    "memberDisplayName": member.get("display_name", profile),
                })
        return [entry for entry in entries if entry.get("toolName") or entry.get("kind") == "reasoning"]
    except sqlite3.Error as exc:
        LOGGER.warning("member tool read failed for %s: %s", profile, exc)
        return []
    finally:
        db.close()


def build_room_tools_snapshot() -> dict[str, Any]:
    """Collect tool traces for every active hosted room. Never writes core."""
    db = _connect_readonly(room_store_path())
    if db is None:
        raise RoomToolsError("hosted room store unavailable")
    try:
        rooms = db.execute(
            "SELECT room_id, name, members_json FROM hosted_rooms WHERE disbanded_at IS NULL"
        ).fetchall()
    except sqlite3.Error as exc:
        db.close()
        raise RoomToolsError(f"hosted room store unreadable: {exc}") from exc
    db.close()

    rooms_out: list[dict[str, Any]] = []
    total_tools = 0
    for room in rooms:
        room_id = _str(room[0])
        members = _room_members(room)
        tools: list[dict[str, Any]] = []
        for member in members:
            tools.extend(_member_tool_rows(member, room_id))
        tools.sort(key=lambda item: float(item.get("timestamp") or 0.0))
        total_tools += len(tools)
        rooms_out.append(
            {
                "room_id": room_id,
                "name": _str(room[1]),
                "members": [
                    {"profile": m["profile"], "handle": m["handle"], "display_name": m["display_name"]}
                    for m in members
                ],
                "tools": tools,
            }
        )

    return {
        "generated_at": time.time(),
        "room_count": len(rooms_out),
        "tool_count": total_tools,
        "rooms": rooms_out,
    }


def write_cache_snapshot(snapshot: dict[str, Any]) -> Path:
    target = cache_path()
    target.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def read_room_tools(room_id: str, *, max_age_seconds: int = DEFAULT_CACHE_TTL_SECONDS) -> list[dict[str, Any]]:
    """Return the tool-traces for one room, refreshing the cache if stale.

    The cache is Mission Control-owned (gitignored). The caller (dashboard-api)
    never touches Hermes core — this function opens the SQLite files read-only.
    """
    target = cache_path()
    try:
        if target.is_file() and (time.time() - target.stat().st_mtime) < max_age_seconds:
            payload = json.loads(target.read_text(encoding="utf-8"))
            for room in payload.get("rooms", []):
                if room.get("room_id") == room_id:
                    return room.get("tools", [])
    except (OSError, ValueError):
        pass
    snapshot = build_room_tools_snapshot()
    write_cache_snapshot(snapshot)
    for room in snapshot.get("rooms", []):
        if room.get("room_id") == room_id:
            return room.get("tools", [])
    return []
