from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

_TRACE_MODE_NATIVE = "native"
_TRACE_MODE_TRANSCRIPT = "transcript"
_TRACE_MODE_UNAVAILABLE = "unavailable"
_SKILL_TOOL_NAMES = {"skill_view", "skills_list", "skill_manage"}
_SCHEMA_VERSION = "1"


def _sessions_dir() -> Path:
    from hermes_paths import hermes_sessions_dir

    return hermes_sessions_dir().resolve()


def _safe_read_json(path: Path) -> dict[str, Any] | None:
    try:
        if not path.exists() or not path.is_file():
            return None
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else None
    except Exception:
        _log.debug("Failed to read JSON file %s", path, exc_info=True)
        return None


def _safe_read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        if not path.exists() or not path.is_file():
            return rows
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except Exception:
                continue
            if isinstance(item, dict):
                rows.append(item)
    except Exception:
        _log.debug("Failed to read JSONL file %s", path, exc_info=True)
    return rows


def _parse_timestamp(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        parsed = float(value)
        return parsed if parsed > 1e12 else parsed
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            numeric = float(stripped)
            return numeric / 1000.0 if numeric > 1e12 else numeric
        except Exception:
            pass
        try:
            return datetime.fromisoformat(stripped.replace("Z", "+00:00")).timestamp()
        except Exception:
            return None
    return None


def _redact_home_path(value: str | None) -> str:
    if not value:
        return ""
    from hermes_paths import display_home_path

    return display_home_path(value)


def _normalize_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return _redact_home_path(value)
    try:
        return _redact_home_path(json.dumps(value, ensure_ascii=False, sort_keys=True))
    except Exception:
        return _redact_home_path(str(value))


def _compact_text(value: str, limit: int = 800) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + "\n\n… [compact trace clipped]"


def _single_line_preview(value: str, limit: int = 180) -> str:
    text = " ".join(value.split()).strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def _build_agent_key(source: str, model: str) -> str:
    return f"{source or 'unknown'}::{model or 'unknown'}"


def _is_live(last_active_ts: float | None, ended_at: float | None, live_window_seconds: int) -> bool:
    if ended_at is not None or last_active_ts is None:
        return False
    return (time.time() - last_active_ts) < max(30, live_window_seconds)


def _read_gateway_sessions_index() -> dict[str, dict[str, Any]]:
    path = _sessions_dir() / "sessions.json"
    raw = _safe_read_json(path) or {}
    result: dict[str, dict[str, Any]] = {}
    for entry in raw.values():
        if not isinstance(entry, dict):
            continue
        session_id = str(entry.get("session_id") or "").strip()
        if not session_id:
            continue
        current = result.get(session_id)
        if current is None:
            result[session_id] = dict(entry)
            continue
        current_updated = _parse_timestamp(current.get("updated_at")) or 0
        next_updated = _parse_timestamp(entry.get("updated_at")) or 0
        if next_updated >= current_updated:
            result[session_id] = dict(entry)
    return result


def _read_session_jsonl(session_id: str) -> list[dict[str, Any]]:
    return _safe_read_jsonl(_sessions_dir() / f"{session_id}.jsonl")


def _iter_db_session_ids() -> list[str]:
    """Discover session IDs from SessionDB (replaces sidecar-based discovery)."""
    db = _try_get_session_db()
    try:
        if db is None:
            return []
        rows = db.list_sessions_rich(limit=2000, order_by_last_active=True)
        return [str(row.get("id", "")) for row in rows if row.get("id")]
    except Exception:
        return []
    finally:
        _close_session_db(db)


def _try_get_session_db():
    """Open the Hermes session store strictly read-only for telemetry.

    Mission Control only observes sessions.  A writable ``SessionDB()`` runs
    schema/FTS initialization on every request and competes with the gateway's
    writer, which is precisely the wrong thing for a polling sidecar to do.
    """
    try:
        from hermes_state import SessionDB

        return SessionDB(read_only=True)
    except Exception:
        return None


def _close_session_db(db: Any) -> None:
    try:
        if db is not None:
            db.close()
    except Exception:
        pass


def _get_db_rich_row(db: Any, session_id: str) -> dict[str, Any] | None:
    if db is None:
        return None
    try:
        row = db._get_session_rich_row(session_id)
        return row if isinstance(row, dict) else None
    except Exception:
        return None


def _get_db_messages(db: Any, session_id: str) -> list[dict[str, Any]] | None:
    if db is None:
        return None
    try:
        rows = db.get_messages(session_id)
        return rows if isinstance(rows, list) and rows else None
    except Exception:
        return None


def _first_user_content(messages: list[dict[str, Any]] | None) -> str:
    if not messages:
        return ""
    for message in messages:
        if message.get("role") != "user":
            continue
        content = _normalize_text(message.get("content"))
        if content.strip():
            return content
    return ""


def _derive_title(messages: list[dict[str, Any]] | None, fallback: str) -> str:
    preview = _first_user_content(messages)
    if preview:
        return _single_line_preview(preview, limit=72)
    return fallback or "Untitled session"


def _derive_preview(messages: list[dict[str, Any]] | None, fallback: str = "") -> str:
    preview = _first_user_content(messages)
    if preview:
        return _single_line_preview(preview, limit=180)
    return _single_line_preview(fallback, limit=180)


def _recent_chat_messages(
    db: Any,
    session_id: str,
    limit: int = 4,
) -> list[dict[str, Any]]:
    """Return a small, UI-safe conversation preview for the Sessions list.

    Tool/system rows are intentionally excluded: the row preview is a chat
    affordance, not a second trace viewer. Keep the payload bounded because
    this runs once per visible session row.
    """
    if limit <= 0:
        return []
    messages = _get_db_messages(db, session_id) or _read_session_jsonl(session_id)
    if not messages:
        return []

    recent: list[dict[str, Any]] = []
    for message in messages:
        role = str(message.get("role") or "").strip().lower()
        if role not in {"user", "assistant"}:
            continue
        text = _normalize_text(message.get("content"))
        if not text.strip():
            continue
        recent.append(
            {
                "role": role,
                "text": _single_line_preview(text, limit=240),
                "timestamp": _parse_timestamp(message.get("timestamp")),
            }
        )
    return recent[-limit:]


def _trace_mode_for_artifacts(session_id: str, db_row: dict[str, Any] | None) -> str:
    if db_row:
        return _TRACE_MODE_NATIVE
    if _read_session_jsonl(session_id):
        return _TRACE_MODE_TRANSCRIPT
    return _TRACE_MODE_UNAVAILABLE


def _coerce_int(value: Any, fallback: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return fallback


def _coerce_token_int(value: Any) -> int:
    """Normalize token counters; corrupted/invalid counters cannot be negative."""
    return max(0, _coerce_int(value, 0))


def _session_display_name(index_entry: dict[str, Any] | None, platform: str, title: str) -> str:
    if index_entry and isinstance(index_entry.get("display_name"), str) and index_entry.get("display_name"):
        return _redact_home_path(str(index_entry["display_name"]))
    if platform == "tui":
        return "TUI local session"
    if platform == "cli":
        return "CLI local session"
    return title or f"{platform} session"


def _session_chat_type(index_entry: dict[str, Any] | None, platform: str) -> str:
    if index_entry and isinstance(index_entry.get("chat_type"), str) and index_entry.get("chat_type"):
        return str(index_entry["chat_type"])
    if platform in {"tui", "cli"}:
        return "local"
    return "unknown"


def _build_session_item(
    session_id: str,
    index_entry: dict[str, Any] | None,
    sidecar: dict[str, Any] | None,
    db_row: dict[str, Any] | None,
    live_window_seconds: int,
    recent_messages: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    sidecar_messages = sidecar.get("messages") if isinstance(sidecar, dict) and isinstance(sidecar.get("messages"), list) else None
    source = str(
        (sidecar or {}).get("platform")
        or (db_row or {}).get("source")
        or (index_entry or {}).get("platform")
        or "unknown"
    )
    platform = str((sidecar or {}).get("platform") or (index_entry or {}).get("platform") or source or "unknown")
    model = str((sidecar or {}).get("model") or (db_row or {}).get("model") or "unknown")
    started_at = (
        _parse_timestamp((sidecar or {}).get("session_start"))
        or _parse_timestamp((db_row or {}).get("started_at"))
        or _parse_timestamp((index_entry or {}).get("created_at"))
    )
    last_active_at = (
        _parse_timestamp((sidecar or {}).get("last_updated"))
        or _parse_timestamp((db_row or {}).get("last_active"))
        or _parse_timestamp((index_entry or {}).get("updated_at"))
        or started_at
    )
    ended_at = _parse_timestamp((db_row or {}).get("ended_at"))
    title = _derive_title(
        sidecar_messages,
        _normalize_text((db_row or {}).get("title")) or _normalize_text((index_entry or {}).get("display_name")) or session_id,
    )
    preview = _derive_preview(sidecar_messages, _normalize_text((db_row or {}).get("preview")))
    message_count = max(
        _coerce_int((sidecar or {}).get("message_count"), 0),
        _coerce_int((db_row or {}).get("message_count"), 0),
        len(sidecar_messages or []),
    )
    status = "live" if _is_live(last_active_at, ended_at, live_window_seconds) else ("ended" if ended_at is not None else "idle")
    agent_id = _build_agent_key(source, model)
    trace_mode = _trace_mode_for_artifacts(session_id, db_row)
    return {
        "sessionId": session_id,
        "agentId": agent_id,
        "title": title,
        "source": source,
        "platform": platform,
        "chatType": _session_chat_type(index_entry, platform),
        "displayName": _session_display_name(index_entry, platform, title),
        "model": model,
        "startedAt": started_at,
        "lastActiveAt": last_active_at,
        "endedAt": ended_at,
        "status": status,
        "messageCount": message_count,
        "traceMode": trace_mode,
        "preview": preview,
        "recentMessages": recent_messages or [],
        # Token usage (from SessionDB, fallback to gateway index)
        "inputTokens": _coerce_token_int((db_row or index_entry or {}).get("input_tokens")),
        "outputTokens": _coerce_token_int((db_row or index_entry or {}).get("output_tokens")),
        "cacheReadTokens": _coerce_token_int((db_row or index_entry or {}).get("cache_read_tokens")),
        "cacheWriteTokens": _coerce_token_int((db_row or index_entry or {}).get("cache_write_tokens")),
        "reasoningTokens": _coerce_token_int((db_row or index_entry or {}).get("reasoning_tokens")),
        "estimatedCostUsd": float((db_row or index_entry or {}).get("estimated_cost_usd") or 0.0),
        "costStatus": _normalize_text((db_row or index_entry or {}).get("cost_status")),
    }


def _collect_agent_sessions(live_window_seconds: int = 300, limit: int | None = None, offset: int = 0, session_id: str | None = None) -> list[dict[str, Any]]:
    index_map = _read_gateway_sessions_index()
    # _iter_db_session_ids already returns ids ordered by last_active (recency).
    # Preserve that order, but put index-only sessions first. Those entries are
    # normally live gateway sessions that have not reached SessionDB yet; if we
    # append them after a large DB history, a small first page hides the active
    # session entirely.
    db_ids = _iter_db_session_ids()
    db_id_set = set(db_ids)
    index_only_ids = [
        sid for sid in index_map.keys()
        if sid not in db_id_set
    ]
    index_only_ids.sort(
        key=lambda sid: (_parse_timestamp((index_map.get(sid) or {}).get("updated_at")) or 0),
        reverse=True,
    )
    ordered_ids = index_only_ids + db_ids
    if session_id:
        # Narrow to a single session for the chat drawer preview. Keep the
        # recency ordering contract; the id either exists or yields nothing.
        ordered_ids = [sid for sid in ordered_ids if sid == session_id]
    if offset:
        ordered_ids = ordered_ids[offset:]
    if limit is not None:
        ordered_ids = ordered_ids[:limit]
    db = _try_get_session_db()
    try:
        items: list[dict[str, Any]] = []
        for session_id in ordered_ids:
            db_row = _get_db_rich_row(db, session_id)
            recent_messages = _recent_chat_messages(db, session_id)
            item = _build_session_item(
                session_id,
                index_map.get(session_id),
                None,
                db_row,
                live_window_seconds,
                recent_messages,
            )
            items.append(item)
        # Items already follow the ordered_ids sequence (recency first). Do NOT
        # re-sort here — the client may also re-sort, but the page boundaries
        # must stay contiguous, which requires a stable single ordering.
        return items
    finally:
        _close_session_db(db)


def load_agents_sessions_snapshot(limit: int = 100, live_window_seconds: int = 300, offset: int = 0, session_id: str | None = None) -> dict[str, Any]:
    clamped_limit = max(1, min(limit, 500))
    # Stats come from lightweight counts (single DB query), NOT from materialising
    # every session item — otherwise a limit=50 request still pays the full N+1.
    visible_items = _collect_agent_sessions(live_window_seconds=live_window_seconds, limit=clamped_limit, offset=offset, session_id=session_id)
    live_sessions = [item for item in visible_items if item.get("status") == "live"]
    db = _try_get_session_db()
    total_sessions = 0
    try:
        if db is not None:
            total_sessions = len(db.list_sessions_rich(limit=2000, compact_rows=True))
    except Exception:
        total_sessions = 0
    finally:
        _close_session_db(db)
    return {
        "success": True,
        "schemaVersion": _SCHEMA_VERSION,
        "available": True,
        "items": visible_items,
        "offset": offset,
        "stats": {
            "totalSessions": total_sessions,
            "liveSessions": len(live_sessions),
            "activeAgents": len({item["agentId"] for item in live_sessions}),
        },
    }


def _query_db_token_aggregates() -> dict[str, Any] | None:
    """Query cumulative token totals and per-model breakdown directly from state.db.

    Returns None if the DB is unavailable.  Single SQL query — no N+1.
    """
    db = _try_get_session_db()
    if db is None:
        return None
    try:
        conn = getattr(db, "_conn", None)
        if conn is None:
            return None
        # Newer Hermes versions keep per-call attribution in
        # ``session_model_usage``. Older databases only have the aggregate
        # ``sessions`` row, so retain a schema-compatible fallback while the
        # live gateway performs its migration.
        has_model_usage = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='session_model_usage'"
        ).fetchone()
        if has_model_usage:
            rows = conn.execute("""
                SELECT
                    COALESCE(NULLIF(TRIM(model), ''), 'unknown') AS model,
                    COUNT(DISTINCT session_id)                    AS session_count,
                    COALESCE(SUM(input_tokens), 0)                AS input_tokens,
                    COALESCE(SUM(output_tokens), 0)               AS output_tokens,
                    COALESCE(SUM(cache_read_tokens), 0)           AS cache_read_tokens,
                    COALESCE(SUM(cache_write_tokens), 0)          AS cache_write_tokens,
                    COALESCE(SUM(reasoning_tokens), 0)            AS reasoning_tokens,
                    COALESCE(SUM(estimated_cost_usd), 0.0)        AS estimated_cost_usd,
                    COUNT(DISTINCT CASE
                        WHEN cost_status IN ('actual', 'estimated', 'included')
                             OR estimated_cost_usd > 0
                             OR actual_cost_usd > 0
                        THEN session_id END)                      AS priced_session_count
                FROM session_model_usage
                GROUP BY COALESCE(NULLIF(TRIM(model), ''), 'unknown')
                ORDER BY estimated_cost_usd DESC
            """).fetchall()
        else:
            rows = conn.execute("""
                SELECT
                    COALESCE(NULLIF(TRIM(model), ''), 'unknown') AS model,
                    COUNT(*)                                      AS session_count,
                    COALESCE(SUM(input_tokens), 0)                AS input_tokens,
                    COALESCE(SUM(output_tokens), 0)               AS output_tokens,
                    COALESCE(SUM(cache_read_tokens), 0)            AS cache_read_tokens,
                    COALESCE(SUM(cache_write_tokens), 0)           AS cache_write_tokens,
                    COALESCE(SUM(reasoning_tokens), 0)             AS reasoning_tokens,
                    COALESCE(SUM(estimated_cost_usd), 0.0)         AS estimated_cost_usd,
                    SUM(CASE
                        WHEN cost_status IN ('actual', 'estimated', 'included')
                             OR actual_cost_usd > 0
                        THEN 1 ELSE 0 END)                         AS priced_session_count
                FROM sessions
                GROUP BY COALESCE(NULLIF(TRIM(model), ''), 'unknown')
                ORDER BY estimated_cost_usd DESC
            """).fetchall()

        by_model: list[dict[str, Any]] = []
        totals = {
            "inputTokens": 0, "outputTokens": 0,
            "cacheReadTokens": 0, "cacheWriteTokens": 0,
            "reasoningTokens": 0, "totalTokens": 0,
            "estimatedCostUsd": 0.0, "sessionCount": 0,
            "pricedSessionCount": 0,
        }
        for r in rows:
            d = dict(r)
            model = d["model"]
            inp = max(0, int(d["input_tokens"] or 0))
            out = max(0, int(d["output_tokens"] or 0))
            cr  = max(0, int(d["cache_read_tokens"] or 0))
            cw  = max(0, int(d["cache_write_tokens"] or 0))
            rea = max(0, int(d["reasoning_tokens"] or 0))
            cost = max(0.0, float(d["estimated_cost_usd"] or 0.0))
            # Reasoning is a detail of output, not an additional token bucket.
            total = inp + out + cr + cw
            priced = int(d["priced_session_count"] or 0)
            by_model.append({
                "model": model,
                "inputTokens": inp, "outputTokens": out,
                "cacheReadTokens": cr, "cacheWriteTokens": cw,
                "reasoningTokens": rea, "totalTokens": total,
                "estimatedCostUsd": round(cost, 6),
                "sessionCount": d["session_count"],
                "pricedSessionCount": priced,
            })
            totals["inputTokens"] += inp
            totals["outputTokens"] += out
            totals["cacheReadTokens"] += cr
            totals["cacheWriteTokens"] += cw
            totals["reasoningTokens"] += rea
            totals["totalTokens"] += total
            totals["estimatedCostUsd"] += cost
            totals["sessionCount"] += d["session_count"]
            totals["pricedSessionCount"] += priced

        totals["estimatedCostUsd"] = round(totals["estimatedCostUsd"], 6)
        return {"totals": totals, "byModel": by_model}
    except Exception:
        _log.debug("DB aggregate query failed", exc_info=True)
        return None
    finally:
        _close_session_db(db)


def load_sessions_usage(live_window_seconds: int = 300) -> dict[str, Any]:
    """Aggregate token usage and cost across all sessions, with per-model breakdown.

    Prefers a single aggregate SQL query on state.db (fast, covers ALL 1100+
    sessions).  Falls back to the slower per-session Python loop only when the
    DB is unavailable.
    """
    db_result = _query_db_token_aggregates()
    if db_result is not None:
        return {
            "success": True,
            "available": True,
            "totals": db_result["totals"],
            "byModel": db_result["byModel"],
        }

    # Fallback: iterate sessions in Python (slower, limited by _collect_agent_sessions)
    all_items = _collect_agent_sessions(live_window_seconds=live_window_seconds)
    totals = {
        "inputTokens": 0,
        "outputTokens": 0,
        "cacheReadTokens": 0,
        "cacheWriteTokens": 0,
        "reasoningTokens": 0,
        "totalTokens": 0,
        "estimatedCostUsd": 0.0,
        "sessionCount": len(all_items),
        "pricedSessionCount": 0,
    }
    by_model: dict[str, dict[str, Any]] = {}
    for item in all_items:
        inp = max(0, int(item.get("inputTokens", 0) or 0))
        out = max(0, int(item.get("outputTokens", 0) or 0))
        cache_r = max(0, int(item.get("cacheReadTokens", 0) or 0))
        cache_w = max(0, int(item.get("cacheWriteTokens", 0) or 0))
        reasoning = max(0, int(item.get("reasoningTokens", 0) or 0))
        cost = max(0.0, float(item.get("estimatedCostUsd", 0.0) or 0.0))
        total = inp + out + cache_r + cache_w
        totals["inputTokens"] += inp
        totals["outputTokens"] += out
        totals["cacheReadTokens"] += cache_r
        totals["cacheWriteTokens"] += cache_w
        totals["reasoningTokens"] += reasoning
        totals["totalTokens"] += total
        totals["estimatedCostUsd"] += cost
        if item.get("costStatus") in {"actual", "estimated", "included"}:
            totals["pricedSessionCount"] += 1
        model = item.get("model", "unknown")
        bucket = by_model.get(model)
        if bucket is None:
            bucket = {
                "model": model,
                "inputTokens": 0,
                "outputTokens": 0,
                "cacheReadTokens": 0,
                "cacheWriteTokens": 0,
                "reasoningTokens": 0,
                "totalTokens": 0,
                "estimatedCostUsd": 0.0,
                "sessionCount": 0,
                "pricedSessionCount": 0,
            }
            by_model[model] = bucket
        bucket["inputTokens"] += inp
        bucket["outputTokens"] += out
        bucket["cacheReadTokens"] += cache_r
        bucket["cacheWriteTokens"] += cache_w
        bucket["reasoningTokens"] += reasoning
        bucket["totalTokens"] += total
        bucket["estimatedCostUsd"] += cost
        bucket["sessionCount"] += 1
        if item.get("costStatus") in {"actual", "estimated", "included"}:
            bucket["pricedSessionCount"] += 1
    # Sort by cost descending
    breakdown = sorted(by_model.values(), key=lambda b: b["estimatedCostUsd"], reverse=True)
    # Round cost to 6 decimal places
    totals["estimatedCostUsd"] = round(totals["estimatedCostUsd"], 6)
    for b in breakdown:
        b["estimatedCostUsd"] = round(b["estimatedCostUsd"], 6)
    return {
        "success": True,
        "available": True,
        "totals": totals,
        "byModel": breakdown,
    }


def load_agents_snapshot(live_window_seconds: int = 300) -> dict[str, Any]:
    sessions_snapshot = load_agents_sessions_snapshot(limit=10000, live_window_seconds=live_window_seconds)
    groups: dict[str, dict[str, Any]] = {}
    for item in sessions_snapshot["items"]:
        agent_id = item["agentId"]
        group = groups.get(agent_id)
        if group is None:
            groups[agent_id] = {
                "agentId": agent_id,
                "source": item["source"],
                "model": item["model"],
                "label": f"{item['source']} / {item['model']}",
                "totalSessions": 1,
                "liveSessions": 1 if item["status"] == "live" else 0,
                "lastActiveAt": item.get("lastActiveAt"),
                "traceMode": item["traceMode"],
            }
            continue
        group["totalSessions"] += 1
        if item["status"] == "live":
            group["liveSessions"] += 1
        group["lastActiveAt"] = max(group.get("lastActiveAt") or 0, item.get("lastActiveAt") or 0) or None
        trace_modes = {group["traceMode"], item["traceMode"]}
        if _TRACE_MODE_NATIVE in trace_modes:
            group["traceMode"] = _TRACE_MODE_NATIVE
        elif _TRACE_MODE_TRANSCRIPT in trace_modes:
            group["traceMode"] = _TRACE_MODE_TRANSCRIPT
        else:
            group["traceMode"] = _TRACE_MODE_UNAVAILABLE
    items = sorted(groups.values(), key=lambda item: ((item.get("liveSessions") or 0), (item.get("lastActiveAt") or 0)), reverse=True)
    return {
        "success": True,
        "schemaVersion": _SCHEMA_VERSION,
        "available": True,
        "capabilities": {
            "trace": {
                "stream": True,
                "compact": True,
                "namedSseTraceEvent": True,
            },
            "traceModes": [_TRACE_MODE_NATIVE, _TRACE_MODE_TRANSCRIPT, _TRACE_MODE_UNAVAILABLE],
        },
        "items": items,
    }


def _normalize_trace_session_ref(item: dict[str, Any] | None) -> dict[str, Any] | None:
    if not item:
        return None
    return {
        "sessionId": item.get("sessionId"),
        "agentId": item.get("agentId"),
        "title": item.get("title"),
        "model": item.get("model"),
        "source": item.get("source"),
        "startedAt": item.get("startedAt"),
        "lastActiveAt": item.get("lastActiveAt"),
    }


def _has_error_value(value: Any) -> bool:
    return value not in (None, "", False)


def _tool_result_status(raw_content: str) -> tuple[str, str]:
    text = raw_content.strip()
    if not text:
        return "good", "completed"
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            status = parsed.get("status")
            status_text = status.lower() if isinstance(status, str) else ""
            if parsed.get("success") is False or parsed.get("ok") is False:
                return "bad", "failed"
            if isinstance(parsed.get("exit_code"), int) and parsed.get("exit_code") != 0:
                return "bad", "failed"
            if status_text in {"error", "failed"}:
                return "bad", "failed"
            if _has_error_value(parsed.get("error")):
                return "bad", "failed"
            return "good", "completed"
    except Exception:
        pass
    lower = text.lower()
    if "traceback" in lower or "error:" in lower:
        return "bad", "failed"
    return "good", "completed"


def _build_sequence_edges(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    edges: list[dict[str, Any]] = []
    for index in range(1, len(events)):
        edges.append({"from": events[index - 1]["id"], "to": events[index]["id"], "kind": "sequence"})
    event_by_id = {event["id"]: event for event in events}
    for event in events:
        parent = event.get("parentEventId")
        if parent and parent in event_by_id:
            edges.append({"from": parent, "to": event["id"], "kind": "parent"})
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for edge in edges:
        key = (edge["from"], edge["to"], edge["kind"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(edge)
    return deduped


def _slice_trace_payload(payload: dict[str, Any], limit: int) -> dict[str, Any]:
    if limit <= 0:
        return payload
    events = payload["events"]
    if len(events) <= limit:
        return payload
    visible_events = events[-limit:]
    visible_ids = {event["id"] for event in visible_events}
    payload["events"] = visible_events
    payload["nodes"] = [node for node in payload["nodes"] if node["id"] in visible_ids]
    payload["edges"] = [edge for edge in payload["edges"] if edge["from"] in visible_ids and edge["to"] in visible_ids]
    payload["stats"] = _trace_stats(payload["events"], payload["session"])
    return payload


def _trace_stats(events: list[dict[str, Any]], session_ref: dict[str, Any] | None) -> dict[str, Any]:
    turns = {event.get("turnId", 0) for event in events if event.get("turnId")}
    timestamps = [event.get("timestamp") for event in events if isinstance(event.get("timestamp"), (int, float))]
    duration_seconds = 0
    if timestamps:
        duration_seconds = max(0, int(max(timestamps) - min(timestamps)))
    elif session_ref:
        start = session_ref.get("startedAt")
        end = session_ref.get("lastActiveAt")
        if isinstance(start, (int, float)) and isinstance(end, (int, float)):
            duration_seconds = max(0, int(end - start))
    return {
        "turns": len(turns),
        "toolCalls": len([event for event in events if event.get("type") == "tool_call_started"]),
        "skills": len([event for event in events if event.get("type") == "skill_used"]),
        "thoughts": len([event for event in events if event.get("type") == "thought"]),
        "errors": len([event for event in events if event.get("tone") == "bad"]),
        "durationSeconds": duration_seconds,
    }


def _build_trace_from_messages(
    session_item: dict[str, Any] | None,
    messages: list[dict[str, Any]],
    trace_mode: str,
    limit: int,
    compact: bool,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    session_ref = _normalize_trace_session_ref(session_item)
    base_ts = None
    if session_ref:
        base_ts = session_ref.get("startedAt") or session_ref.get("lastActiveAt")
    if not isinstance(base_ts, (int, float)):
        base_ts = time.time()

    events: list[dict[str, Any]] = []
    pending_calls: dict[str, dict[str, Any]] = {}
    turn_id = 0
    event_index = 0

    def next_id(prefix: str) -> str:
        nonlocal event_index
        event_index += 1
        return f"{prefix}_{event_index}"

    def append_event(event: dict[str, Any]) -> None:
        if compact:
            for field in ("detail", "request", "response"):
                if isinstance(event.get(field), str):
                    event[field] = _compact_text(event[field])
        events.append(event)

    for message_index, raw_message in enumerate(messages):
        message = raw_message if isinstance(raw_message, dict) else {}
        role = str(message.get("role") or "")
        starts_new_turn = role == "user"
        if starts_new_turn:
            turn_id += 1
        elif turn_id == 0:
            turn_id = 1

        timestamp = _parse_timestamp(message.get("timestamp"))
        if timestamp is None:
            timestamp = base_ts + message_index

        content = _normalize_text(message.get("content"))
        reasoning = _normalize_text(message.get("reasoning") or message.get("reasoning_content"))

        if starts_new_turn:
            append_event({
                "id": next_id("evt"),
                "type": "turn_started",
                "label": f"Turn {turn_id} started",
                "detail": content or f"Turn {turn_id}",
                "tone": "good",
                "status": "completed",
                "timestamp": timestamp,
                "sessionId": session_ref.get("sessionId") if session_ref else "",
                "turnId": turn_id,
            })
            append_event({
                "id": next_id("evt"),
                "type": "user_message",
                "label": "User message",
                "detail": content,
                "tone": "good",
                "status": "completed",
                "timestamp": timestamp,
                "sessionId": session_ref.get("sessionId") if session_ref else "",
                "turnId": turn_id,
            })
            continue

        if role == "assistant" and reasoning.strip():
            append_event({
                "id": next_id("evt"),
                "type": "thought",
                "label": "Model reasoning",
                "detail": reasoning,
                "tone": "good",
                "status": "completed",
                "timestamp": timestamp,
                "sessionId": session_ref.get("sessionId") if session_ref else "",
                "turnId": turn_id,
            })

        tool_calls = message.get("tool_calls") if isinstance(message.get("tool_calls"), list) else []
        if role == "assistant" and tool_calls:
            for call in tool_calls:
                if not isinstance(call, dict):
                    continue
                function = call.get("function") if isinstance(call.get("function"), dict) else {}
                tool_name = str(function.get("name") or message.get("tool_name") or "tool")
                call_id = str(call.get("call_id") or call.get("id") or message.get("tool_call_id") or next_id("call"))
                request = _normalize_text(function.get("arguments"))
                start_event = {
                    "id": next_id("evt"),
                    "type": "tool_call_started",
                    "label": f"Tool: {tool_name}",
                    "detail": request or f"Calling {tool_name}",
                    "tone": "good",
                    "status": "running",
                    "timestamp": timestamp,
                    "sessionId": session_ref.get("sessionId") if session_ref else "",
                    "turnId": turn_id,
                    "toolName": tool_name,
                    "callId": call_id,
                    "request": request or None,
                }
                pending_calls[call_id] = start_event
                append_event(start_event)
                if tool_name in _SKILL_TOOL_NAMES:
                    append_event({
                        "id": next_id("evt"),
                        "type": "skill_used",
                        "label": f"Skill tool: {tool_name}",
                        "detail": request or tool_name,
                        "tone": "good",
                        "status": "completed",
                        "timestamp": timestamp,
                        "sessionId": session_ref.get("sessionId") if session_ref else "",
                        "turnId": turn_id,
                        "toolName": tool_name,
                        "callId": call_id,
                        "skillName": tool_name,
                        "parentEventId": start_event["id"],
                        "request": request or None,
                    })

        if role == "tool":
            call_id = str(message.get("tool_call_id") or "")
            pending = pending_calls.get(call_id)
            tool_name = str(message.get("tool_name") or (pending or {}).get("toolName") or "tool")
            tone, status = _tool_result_status(content)
            append_event({
                "id": next_id("evt"),
                "type": "tool_call_completed",
                "label": f"Tool result: {tool_name}",
                "detail": content,
                "tone": tone,
                "status": status,
                "timestamp": timestamp,
                "sessionId": session_ref.get("sessionId") if session_ref else "",
                "turnId": turn_id,
                "toolName": tool_name,
                "callId": call_id or None,
                "parentEventId": (pending or {}).get("id"),
                "response": content or None,
                "request": (pending or {}).get("request"),
            })
            continue

        if role == "assistant" and content.strip():
            append_event({
                "id": next_id("evt"),
                "type": "assistant_response",
                "label": "Assistant response",
                "detail": content,
                "tone": "good",
                "status": "completed",
                "timestamp": timestamp,
                "sessionId": session_ref.get("sessionId") if session_ref else "",
                "turnId": turn_id,
            })

    nodes = [
        {
            "id": event["id"],
            "kind": event["type"],
            "label": event["label"],
            "status": event.get("status") or ("failed" if event.get("tone") == "bad" else "completed"),
            "turnId": event["turnId"],
            "timestamp": event["timestamp"],
        }
        for event in events
    ]
    payload = {
        "success": True,
        "schemaVersion": _SCHEMA_VERSION,
        "available": bool(session_ref),
        "mode": "live" if session_item and session_item.get("status") == "live" else "post",
        "traceMode": trace_mode,
        "session": session_ref,
        "events": events,
        "nodes": nodes,
        "edges": _build_sequence_edges(events),
        "stats": _trace_stats(events, session_ref),
        "warnings": warnings or [],
    }
    return _slice_trace_payload(payload, limit)


def _build_trace_from_transcript(session_id: str, limit: int = 300, compact: bool = False) -> dict[str, Any] | None:
    session_item = next((item for item in _collect_agent_sessions() if item["sessionId"] == session_id), None)
    jsonl_rows = _read_session_jsonl(session_id)
    if jsonl_rows:
        messages = [row for row in jsonl_rows if row.get("role") != "session_meta"]
        if messages:
            return _build_trace_from_messages(
                session_item,
                messages,
                trace_mode=_TRACE_MODE_TRANSCRIPT,
                limit=limit,
                compact=compact,
                warnings=["Native tool-call trace unavailable; built from transcript artifacts."],
            )
    return None


def _build_trace_native(session_id: str, limit: int = 300, compact: bool = False) -> dict[str, Any] | None:
    db = _try_get_session_db()
    try:
        if db is None:
            return None
        messages = _get_db_messages(db, session_id)
        if not messages:
            return None
        row = _get_db_rich_row(db, session_id)
        session_item = _build_session_item(session_id, _read_gateway_sessions_index().get(session_id), None, row, 300)
        return _build_trace_from_messages(session_item, messages, trace_mode=_TRACE_MODE_NATIVE, limit=limit, compact=compact)
    finally:
        _close_session_db(db)


def _fallback_unavailable_trace(session_id: str | None, reason: str) -> dict[str, Any]:
    session_item = None
    if session_id:
        session_item = next((item for item in _collect_agent_sessions() if item["sessionId"] == session_id), None)
    return {
        "success": True,
        "schemaVersion": _SCHEMA_VERSION,
        "available": False,
        "mode": "post",
        "traceMode": _TRACE_MODE_UNAVAILABLE,
        "session": _normalize_trace_session_ref(session_item),
        "events": [],
        "nodes": [],
        "edges": [],
        "stats": {
            "turns": 0,
            "toolCalls": 0,
            "skills": 0,
            "thoughts": 0,
            "errors": 0,
            "durationSeconds": 0,
        },
        "warnings": [reason],
    }


def _pick_default_session_id() -> str | None:
    snapshot = load_agents_sessions_snapshot(limit=10000)
    items = snapshot["items"]
    if not items:
        return None
    live_items = [item for item in items if item.get("status") == "live"]
    candidate_pool = live_items or items
    rich = next((item for item in candidate_pool if item.get("messageCount", 0) >= 4), None)
    chosen = rich or candidate_pool[0]
    return chosen.get("sessionId")


def load_agent_trace_snapshot(session_id: str | None = None, limit: int = 300, compact: bool = False) -> dict[str, Any]:
    resolved_session_id = session_id or _pick_default_session_id()
    if not resolved_session_id:
        return _fallback_unavailable_trace(None, "No session artifacts were found for Mission Control trace.")

    native = _build_trace_native(resolved_session_id, limit=limit, compact=compact)
    if native is not None:
        return native

    transcript = _build_trace_from_transcript(resolved_session_id, limit=limit, compact=compact)
    if transcript is not None:
        return transcript

    return _fallback_unavailable_trace(resolved_session_id, "No transcript or native trace data available for this session.")
