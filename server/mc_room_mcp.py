#!/usr/bin/env python3
"""Read-only MCP adapter for Mission Control Group Rooms.

This is deliberately a Mission Control-owned edge adapter: it reads the hosted
room store in SQLite without importing Hermes core and never writes room,
Jira, vault, or deployment state.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any

try:
    from mcp.server import MCPServer
except ImportError:  # pragma: no cover - exercised only without MCP installed
    MCPServer = None  # type: ignore[assignment,misc]

LOGGER = logging.getLogger("mission_control.room_mcp")
MAX_LIMIT = 100
MAX_TEXT = 2000


def rooms_db_path() -> Path:
    configured = os.environ.get("MC_GROUP_ROOMS_DB_PATH", "").strip()
    if configured:
        return Path(configured).expanduser()
    # Hosted Group Rooms belong to the default Mission Control gateway. The
    # active Hermes profile is used only for membership authorization below.
    return Path.home() / ".hermes" / "state.db"


def active_profile() -> str:
    explicit = os.environ.get("HERMES_PROFILE", "").strip()
    if explicit:
        return explicit
    home = os.environ.get("HERMES_HOME", "").strip()
    if home:
        path = Path(home).expanduser()
        if path.parent.name == "profiles":
            return path.name
    return "default"


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _members_summary(raw: str) -> list[dict[str, str]]:
    try:
        parsed = json.loads(raw or "[]")
    except (TypeError, ValueError):
        parsed = []
    if not isinstance(parsed, list):
        return []
    result: list[dict[str, str]] = []
    for member in parsed:
        if not isinstance(member, dict):
            continue
        result.append({
            key: str(member[key])
            for key in ("member_id", "id", "profile", "handle", "display_name")
            if member.get(key) not in (None, "")
        })
    return result


def _room_allowed(room_id: str, members: list[dict[str, str]]) -> bool:
    allowlist = {
        item.strip()
        for item in os.environ.get("MC_ROOM_READ_ALLOWLIST", "").split(",")
        if item.strip()
    }
    if allowlist:
        return room_id in allowlist
    profile = active_profile()
    if not profile or profile == "default":
        return False
    return any(
        profile in {member.get("profile", ""), member.get("handle", "")}
        for member in members
    )


def _resolve_room(
    db: sqlite3.Connection,
    room_id: str,
    room_name: str,
    include_disbanded: bool,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    query = (
        "SELECT room_id, name, members_json, authority_gateway_id, authority_epoch, "
        "next_seq, created_at, updated_at, disbanded_at FROM hosted_rooms "
    )
    if room_id:
        rows = db.execute(query + "WHERE room_id = ?", (room_id,)).fetchall()
    elif room_name:
        rows = db.execute(query + "WHERE name = ? ORDER BY updated_at DESC", (room_name,)).fetchall()
    else:
        return None, {"error": "Provide exactly one of 'room_id' or 'room_name'."}
    if not rows:
        return None, {"error": "Room not found."}
    if len(rows) > 1:
        return None, {
            "error": "ambiguous_room_name",
            "rooms": [{"room_id": row[0], "name": row[1]} for row in rows],
        }
    row = rows[0]
    if row[8] is not None and not include_disbanded:
        return None, {"error": "Room is disbanded. Set include_disbanded=true to read its history."}
    members = _members_summary(row[2])
    return {
        "room_id": row[0],
        "name": row[1],
        "members": members,
        "authority_gateway_id": row[3],
        "authority_epoch": row[4],
        "latest_seq": max(0, int(row[5] or 1) - 1),
        "created_at": row[6],
        "updated_at": row[7],
        "disbanded_at": row[8],
    }, None


def read_room(
    *,
    room_id: str = "",
    room_name: str = "",
    since_seq: int = 0,
    limit: int = 50,
    include_disbanded: bool = False,
) -> dict[str, Any]:
    """Read a bounded Mission Control Group Room window without writing."""
    try:
        limit = min(max(int(limit), 1), MAX_LIMIT)
    except (TypeError, ValueError):
        limit = 50
    try:
        since_seq = max(int(since_seq), 0)
    except (TypeError, ValueError):
        since_seq = 0
    db_path = rooms_db_path()
    if not db_path.is_file():
        return {"error": "Mission Control Group Room store is unavailable."}
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1.0) as db:
            room, error = _resolve_room(
                db,
                room_id.strip(),
                room_name.strip(),
                bool(include_disbanded),
            )
            if error:
                return error
            assert room is not None
            if not _room_allowed(room["room_id"], room["members"]):
                return {"error": "Room access denied for the active profile."}
            rows = db.execute(
                "SELECT seq, event_id, kind, actor_json, payload_json, created_at "
                "FROM hosted_room_events WHERE room_id = ? AND seq > ? "
                "ORDER BY seq ASC LIMIT ?",
                (room["room_id"], since_seq, limit + 1),
            ).fetchall()
    except (sqlite3.Error, OSError) as exc:
        LOGGER.warning("Group Room read failed: %s", exc)
        return {"error": "Mission Control Group Room store could not be read."}

    has_more = len(rows) > limit
    events: list[dict[str, Any]] = []
    for seq, event_id, kind, actor_raw, payload_raw, created_at in rows[:limit]:
        actor = _json_object(actor_raw)
        payload = _json_object(payload_raw)
        text = str(payload.get("text") or "").strip()
        events.append({
            "seq": int(seq),
            "event_id": str(event_id),
            "kind": str(kind),
            "actor": {
                key: str(actor[key])
                for key in ("kind", "id", "display_name")
                if actor.get(key) not in (None, "")
            },
            "text": text[:MAX_TEXT],
            "thread_id": str(payload["thread_id"]) if payload.get("thread_id") else None,
            "created_at": created_at,
        })
    return {
        "room": room,
        "events": events,
        "next_cursor": events[-1]["seq"] if events else since_seq,
        "has_more": has_more,
        "source": "mission_control_hosted_room_events",
        "read_only": True,
    }


def room_read(
    room_id: str = "",
    room_name: str = "",
    since_seq: int = 0,
    limit: int = 50,
    include_disbanded: bool = False,
) -> str:
    """Read a bounded, read-only window from an authorized Mission Control Group Room.

    Use the stable room_id when possible. Exact room names are accepted only
    when they resolve to one room; ambiguous names fail closed.
    """
    return json.dumps(read_room(
        room_id=room_id,
        room_name=room_name,
        since_seq=since_seq,
        limit=limit,
        include_disbanded=include_disbanded,
    ), ensure_ascii=False)


def room_context(
    room_id: str = "",
    room_name: str = "",
    since_seq: int = 0,
    limit: int = 50,
    include_disbanded: bool = False,
) -> str:
    """Read recent authorized Room events as a bounded context window for synthesis."""
    result = read_room(
        room_id=room_id,
        room_name=room_name,
        since_seq=since_seq,
        limit=limit,
        include_disbanded=include_disbanded,
    )
    if result.get("error"):
        return json.dumps(result, ensure_ascii=False)
    lines = []
    for event in result["events"]:
        actor = event["actor"].get("display_name") or event["actor"].get("id") or "unknown"
        lines.append(f"[{event['seq']}] {actor}: {event['text'] or '[' + event['kind'] + ']'}")
    result["context_text"] = "\n".join(lines)
    result["instruction"] = "Summarize only from these Room events and cite sequence numbers when useful."
    return json.dumps(result, ensure_ascii=False)


def create_mcp_server() -> Any:
    if MCPServer is None:
        raise RuntimeError("The 'mcp' package is required to run mc-room-tools.")
    server = MCPServer(
        "mc-room-tools",
        instructions="Read-only access to authorized Mission Control Group Rooms. No write operations are exposed.",
    )
    server.tool()(room_read)
    server.tool()(room_context)
    return server


async def _run() -> None:
    server = create_mcp_server()
    await server.run_stdio_async()


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, stream=__import__("sys").stderr)
    asyncio.run(_run())
