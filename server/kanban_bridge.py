"""Kanban bridge — Mission Control sidecar endpoints backed by the Hermes core kanban DB.

Thin delegation layer: every handler opens a connection via ``kanban_db.connect()``
and reuses ``hermes_cli.kanban_db`` mutators so MC cannot drift from CLI/gateway/
dashboard behavior (same principle as the core dashboard plugin, which is itself
a thin wrapper over ``kanban_db``).

The sidecar is a satellite app: this module imports ``kanban_db`` lazily inside
each call so a missing/unimportable core checkout degrades to a clean error
payload instead of crashing the whole telemetry server.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

BOARD_COLUMNS = [
    "triage", "todo", "scheduled", "ready", "running", "blocked", "review", "done",
]

_CARD_SUMMARY_PREVIEW_CHARS = 200

_import_lock = threading.Lock()
_kanban_db = None


def _kb():
    """Lazy-import hermes_cli.kanban_db from the live core checkout."""
    global _kanban_db
    if _kanban_db is None:
        with _import_lock:
            if _kanban_db is None:
                core_root = Path.home() / ".hermes" / "hermes-agent"
                if str(core_root) not in sys.path:
                    sys.path.insert(0, str(core_root))
                try:
                    from hermes_cli import kanban_db as kb  # noqa: WPS433
                except Exception as exc:  # pragma: no cover - environment dependent
                    raise RuntimeError(f"Hermes core kanban_db unavailable: {exc}") from exc
                _kanban_db = kb
    return _kanban_db


class KanbanError(Exception):
    """Raised for user-visible errors; maps to HTTP 4xx."""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------

def _task_dict(task, conn=None) -> Dict[str, Any]:
    kb = _kb()
    d: Dict[str, Any] = {
        "id": task.id,
        "title": task.title,
        "status": task.status,
        "priority": getattr(task, "priority", 0),
        "assignee": getattr(task, "assignee", None),
        "created_at": getattr(task, "created_at", None),
        "started_at": getattr(task, "started_at", None),
        "completed_at": getattr(task, "completed_at", None),
        "tenant": getattr(task, "tenant", None),
        "session_id": getattr(task, "session_id", None),
        "current_run_id": getattr(task, "current_run_id", None),
    }
    if conn is not None:
        # comment count + parent/child counts, cheap aggregates
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM task_comments WHERE task_id = ?", (task.id,)
            ).fetchone()
            d["comment_count"] = int(row["n"]) if row else 0
            parents = [r["parent_id"] for r in conn.execute(
                "SELECT parent_id FROM task_links WHERE child_id = ? ORDER BY parent_id", (task.id,)
            ).fetchall()]
            children = [r["child_id"] for r in conn.execute(
                "SELECT child_id FROM task_links WHERE parent_id = ? ORDER BY child_id", (task.id,)
            ).fetchall()]
            d["parents"] = parents
            d["children"] = children
            prog_total = len(children)
            prog_done = 0
            for cid in children:
                c = conn.execute("SELECT status FROM tasks WHERE id = ?", (cid,)).fetchone()
                if c and c["status"] == "done":
                    prog_done += 1
            d["progress"] = {"done": prog_done, "total": prog_total} if prog_total else None
        except Exception:
            d["comment_count"] = 0
            d["parents"] = []
            d["children"] = []
            d["progress"] = None
    return d


def _resolve_board(board: Optional[str]) -> Optional[str]:
    kb = _kb()
    if board is None or board == "":
        return None
    try:
        normed = kb._normalize_board_slug(board)
    except ValueError as exc:
        raise KanbanError(400, str(exc))
    if normed and normed != kb.DEFAULT_BOARD and not kb.board_exists(normed):
        raise KanbanError(404, f"board {normed!r} does not exist")
    return normed


def _conn(board: Optional[str] = None):
    kb = _kb()
    board = _resolve_board(board)
    return kb.connect(board=board)


# ---------------------------------------------------------------------------
# Board
# ---------------------------------------------------------------------------

def get_board(board: Optional[str] = None) -> Dict[str, Any]:
    kb = _kb()
    conn = _conn(board=board)
    try:
        tasks = kb.list_tasks(conn)
        columns: Dict[str, list] = {c: [] for c in BOARD_COLUMNS}
        columns["archived"] = []
        for t in tasks:
            col = t.status if t.status in columns else "todo"
            columns[col].append(_task_dict(t, conn=conn))
        tenants = [r["tenant"] for r in conn.execute(
            "SELECT DISTINCT tenant FROM tasks WHERE tenant IS NOT NULL ORDER BY tenant"
        ).fetchall()]
        assignees = [r["assignee"] for r in conn.execute(
            "SELECT DISTINCT assignee FROM tasks WHERE assignee IS NOT NULL AND status != 'archived' ORDER BY assignee"
        ).fetchall()]
        latest_event_id = conn.execute(
            "SELECT COALESCE(MAX(id), 0) AS m FROM task_events"
        ).fetchone()["m"]
        return {
            "columns": [{"name": name, "tasks": columns[name]} for name in BOARD_COLUMNS],
            "tenants": tenants,
            "assignees": assignees,
            "latestEventId": int(latest_event_id),
            "now": int(time.time()),
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Task detail / CRUD
# ---------------------------------------------------------------------------

def _latest_summary(conn, task_id: str) -> Optional[str]:
    try:
        row = conn.execute(
            "SELECT summary FROM task_runs WHERE task_id = ? AND summary IS NOT NULL "
            "ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        return row["summary"] if row else None
    except Exception:
        return None


def get_task_detail(task_id: str, board: Optional[str] = None) -> Dict[str, Any]:
    kb = _kb()
    conn = _conn(board=board)
    try:
        task = kb.get_task(conn, task_id)
        if task is None:
            raise KanbanError(404, f"task {task_id} not found")
        d = _task_dict(task, conn=conn)
        body = getattr(task, "body", None)
        d["body"] = body
        d["result"] = getattr(task, "result", None)
        latest = _latest_summary(conn, task_id)
        d["latest_summary"] = (latest or "")[:_CARD_SUMMARY_PREVIEW_CHARS * 2] if latest else None
        comments = [
            {
                "id": c.id,
                "task_id": c.task_id,
                "author": c.author,
                "body": c.body,
                "created_at": c.created_at,
            }
            for c in kb.list_comments(conn, task_id)
        ]
        runs = []
        try:
            for r in conn.execute(
                "SELECT * FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT 20",
                (task_id,),
            ).fetchall():
                runs.append({
                    "id": r["id"],
                    "profile": r["profile"],
                    "status": r["status"],
                    "outcome": r["outcome"],
                    "summary": r["summary"],
                    "error": r["error"],
                    "started_at": r["started_at"],
                    "ended_at": r["ended_at"],
                })
        except Exception:
            pass
        events = []
        try:
            for ev in conn.execute(
                "SELECT id, kind, payload, created_at FROM task_events "
                "WHERE task_id = ? ORDER BY id DESC LIMIT 50",
                (task_id,),
            ).fetchall():
                payload = None
                try:
                    payload = json.loads(ev["payload"]) if ev["payload"] else None
                except Exception:
                    payload = None
                events.append({
                    "id": ev["id"], "kind": ev["kind"], "payload": payload,
                    "created_at": ev["created_at"],
                })
        except Exception:
            pass
        d["comments"] = comments
        d["runs"] = runs
        d["events"] = events
        return d
    finally:
        conn.close()


def create_task(payload: Dict[str, Any], board: Optional[str] = None,
                author: Optional[str] = "mission-control") -> Dict[str, Any]:
    kb = _kb()
    title = (payload.get("title") or "").strip()
    if not title:
        raise KanbanError(400, "title is required")
    conn = _conn(board=board)
    try:
        task_id = kb.create_task(
            conn,
            title=title,
            body=(payload.get("body") or "").strip() or None,
            created_by=author,
            priority=int(payload.get("priority") or 0),
        )
        return {"id": task_id}
    except KanbanError:
        raise
    except Exception as exc:
        raise KanbanError(400, str(exc))
    finally:
        conn.close()


def update_task(task_id: str, payload: Dict[str, Any], board: Optional[str] = None) -> Dict[str, Any]:
    kb = _kb()
    conn = _conn(board=board)
    try:
        task = kb.get_task(conn, task_id)
        if task is None:
            raise KanbanError(404, f"task {task_id} not found")

        new_status = payload.get("status")
        if new_status is not None:
            ok = True
            reason = None
            if new_status == "done":
                ok = kb.complete_task(conn, task_id, result=payload.get("result"),
                                      summary=payload.get("summary"))
            elif new_status == "blocked":
                ok = kb.block_task(conn, task_id, reason=payload.get("block_reason"))
            elif new_status == "scheduled":
                ok = kb.schedule_task(conn, task_id, reason=payload.get("block_reason"))
            elif new_status == "review":
                ok = kb.request_review(conn, task_id, summary=payload.get("summary"))
            elif new_status == "ready":
                current = kb.get_task(conn, task_id)
                if current and current.status in ("blocked", "scheduled"):
                    ok = kb.unblock_task(conn, task_id)
                else:
                    reopened = _reopen_if_review(conn, task_id)
                    ok = reopened if reopened is not None else _set_status_direct(conn, task_id, "ready")
            elif new_status == "archived":
                ok = kb.archive_task(conn, task_id)
            elif new_status == "running":
                raise KanbanError(400, "Cannot set 'running' directly; use the dispatcher/claim path")
            elif new_status in ("todo", "triage", "scheduled"):
                current = kb.get_task(conn, task_id) if new_status == "todo" else None
                reopened = _reopen_if_review(conn, task_id)
                ok = reopened if reopened is not None else _set_status_direct(conn, task_id, new_status)
            else:
                raise KanbanError(400, f"unknown status: {new_status}")
            if not ok:
                raise KanbanError(409, f"status transition to {new_status!r} not valid from current state")

        if payload.get("assignee") is not None:
            try:
                kb.assign_task(conn, task_id, payload.get("assignee") or None)
            except RuntimeError as exc:
                raise KanbanError(409, str(exc))

        title = payload.get("title")
        body = payload.get("body")
        if title is not None or body is not None:
            sets, vals = [], []
            if title is not None:
                if not title.strip():
                    raise KanbanError(400, "title cannot be empty")
                sets.append("title = ?")
                vals.append(title.strip())
            if body is not None:
                sets.append("body = ?")
                vals.append(body)
            vals.append(task_id)
            with kb.write_txn(conn):
                conn.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?", vals)

        updated = kb.get_task(conn, task_id)
        return {"task": _task_dict(updated, conn=conn) if updated else None}
    finally:
        conn.close()


def _reopen_if_review(conn, task_id: str) -> Optional[bool]:
    kb = _kb()
    current = kb.get_task(conn, task_id)
    if current and current.status == "review":
        return kb.reopen_review_task(conn, task_id)
    return None


def _set_status_direct(conn, task_id: str, status: str) -> bool:
    kb = _kb()
    if status not in ("todo", "triage", "ready", "done"):
        return False
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET status = ? WHERE id = ? AND status != 'archived'",
            (status, task_id),
        )
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, 'status_changed', ?, ?)",
            (task_id, json.dumps({"status": status}), int(time.time())),
        )
    return True


def delete_task(task_id: str, board: Optional[str] = None) -> Dict[str, Any]:
    kb = _kb()
    conn = _conn(board=board)
    try:
        task = kb.get_task(conn, task_id)
        if task is None:
            raise KanbanError(404, f"task {task_id} not found")
        ok = kb.archive_task(conn, task_id)
        if not ok:
            raise KanbanError(409, f"cannot archive task {task_id}")
        return {"ok": True}
    finally:
        conn.close()


def add_comment(task_id: str, payload: Dict[str, Any], board: Optional[str] = None,
                author: str = "mission-control") -> Dict[str, Any]:
    kb = _kb()
    body = (payload.get("body") or "").strip()
    if not body:
        raise KanbanError(400, "comment body is required")
    conn = _conn(board=board)
    try:
        task = kb.get_task(conn, task_id)
        if task is None:
            raise KanbanError(404, f"task {task_id} not found")
        kb.add_comment(conn, task_id, author=author, body=body)
        return {"ok": True}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Boards & events
# ---------------------------------------------------------------------------

def list_boards(include_archived: bool = False) -> Dict[str, Any]:
    kb = _kb()
    boards = kb.list_boards(include_archived=include_archived)
    current = kb.get_current_board()
    out = []
    for b in boards:
        b = dict(b)
        slug = b.get("slug")
        b["is_current"] = (slug == current)
        counts: Dict[str, int] = {}
        total = 0
        try:
            path = kb.kanban_db_path(board=slug)
            if path.exists():
                conn = sqlite3.connect(path)
                conn.row_factory = sqlite3.Row
                try:
                    for r in conn.execute(
                        "SELECT status, COUNT(*) AS n FROM tasks GROUP BY status"
                    ).fetchall():
                        counts[r["status"]] = int(r["n"])
                        if r["status"] != "archived":
                            total += int(r["n"])
                finally:
                    conn.close()
        except Exception:
            pass
        b["counts"] = counts
        b["total"] = total
        out.append(b)
    return {"boards": out, "current": current}


def switch_board(slug: str) -> Dict[str, Any]:
    kb = _kb()
    try:
        normed = kb._normalize_board_slug(slug)
    except ValueError as exc:
        raise KanbanError(400, str(exc))
    if not normed or not kb.board_exists(normed):
        raise KanbanError(404, f"board {slug!r} does not exist")
    kb.set_current_board(normed)
    return {"current": normed}


def delete_board(slug: str, hard: bool = False) -> Dict[str, Any]:
    """Archive (default) or permanently delete a board.

    Guards: the ``default`` board cannot be deleted, and the last remaining
    board cannot be removed.
    """
    kb = _kb()
    try:
        normed = kb._normalize_board_slug(slug)
    except ValueError as exc:
        raise KanbanError(400, str(exc))
    if not normed:
        raise KanbanError(400, "invalid board slug")
    if normed == kb.DEFAULT_BOARD:
        raise KanbanError(409, "the default board cannot be deleted")
    if not kb.board_exists(normed):
        raise KanbanError(404, f"board {slug!r} does not exist")
    live_boards = [b["slug"] for b in kb.list_boards(include_archived=False)]
    if len([s for s in live_boards if s != normed]) == 0:
        raise KanbanError(409, "cannot delete the last remaining board")
    # If we are deleting the current board, move the pointer first.
    was_current = kb.get_current_board() == normed
    res = kb.remove_board(normed, archive=not hard)
    if was_current:
        kb.set_current_board(kb.DEFAULT_BOARD)
    return {"result": dict(res), "current": kb.get_current_board()}


def create_board(payload: Dict[str, Any], switch: bool = False) -> Dict[str, Any]:
    kb = _kb()
    slug = (payload.get("slug") or "").strip()
    name = (payload.get("name") or "").strip()
    if not slug and not name:
        raise KanbanError(400, "board slug or name is required")
    # Slug is authoritative; derive from name only when omitted.
    if not slug:
        slug = name.lower().replace(" ", "-")
    try:
        meta = kb.create_board(
            slug,
            name=name or None,
            description=(payload.get("description") or "").strip() or None,
            icon=(payload.get("icon") or "").strip() or None,
            default_workdir=(payload.get("default_workdir") or "").strip() or None,
        )
        if switch:
            kb.set_current_board(meta["slug"])
        return {"board": dict(meta), "current": kb.get_current_board()}
    except ValueError as exc:
        raise KanbanError(400, str(exc))
    except Exception as exc:
        raise KanbanError(500, str(exc)[:240])


def get_events(since: int = 0, limit: int = 200, board: Optional[str] = None) -> Dict[str, Any]:
    """Long-poll-friendly snapshot of new events since ``since``.

    The MC UI polls this endpoint on an interval (no WS needed server-side;
    the sidecar is stdlib-only).
    """
    conn = _conn(board=board)
    try:
        rows = conn.execute(
            "SELECT id, task_id, run_id, kind, payload, created_at FROM task_events "
            "WHERE id > ? ORDER BY id ASC LIMIT ?",
            (int(since), min(int(limit), 500)),
        ).fetchall()
        events = []
        cursor = int(since)
        for r in rows:
            payload = None
            try:
                payload = json.loads(r["payload"]) if r["payload"] else None
            except Exception:
                payload = None
            events.append({
                "id": r["id"], "task_id": r["task_id"], "run_id": r["run_id"],
                "kind": r["kind"], "payload": payload, "created_at": r["created_at"],
            })
            cursor = r["id"]
        return {"events": events, "cursor": cursor}
    finally:
        conn.close()
