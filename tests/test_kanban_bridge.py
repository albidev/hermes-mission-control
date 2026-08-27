"""Tests for server/kanban_bridge.py — the Mission Control → core kanban delegation layer.

Uses a temp HERMES_HOME so it never touches the user's real kanban DB.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent.parent / "server"
CORE_ROOT = Path.home() / ".hermes" / "hermes-agent"

# These tests exercise the real core kanban_db — they can only run on a
# machine with the Hermes core checkout (~/.hermes/hermes-agent) AND a
# Python compatible with it (>=3.11). CI and other environments skip them.
_CORE_DB = CORE_ROOT / "hermes_cli" / "kanban_db.py"
_CORE_AVAILABLE = _CORE_DB.exists()
kb = None
kanban_db = None
if _CORE_AVAILABLE:
    try:
        sys.path.insert(0, str(CORE_ROOT))
        sys.path.insert(0, str(SERVER_DIR))
        # Point hermes at an isolated home BEFORE importing the bridge.
        # Kanban workers inherit explicit board pins so their own task tools
        # cannot escape the assigned board. Those pins have higher precedence
        # than HERMES_HOME; if they leak into this test process, the fixtures
        # are written to the real board instead of the temporary test DB.
        # Clear every worker-specific pin before setting the isolated home.
        for _name in (
            "HERMES_KANBAN_DB",
            "HERMES_KANBAN_BOARD",
            "HERMES_KANBAN_WORKSPACES_ROOT",
            "HERMES_KANBAN_HOME",
            "HERMES_KANBAN_TASK",
            "HERMES_KANBAN_RUN_ID",
            "HERMES_KANBAN_CLAIM_LOCK",
            "HERMES_KANBAN_WORKSPACE",
            "HERMES_KANBAN_BRANCH",
        ):
            os.environ.pop(_name, None)
        os.environ["HERMES_HOME"] = tempfile.mkdtemp(prefix="mc-kanban-test-")
        # Probe: hermes_constants uses `str | object` unions and fails with
        # TypeError on Python < 3.10 — sometimes lazily (inside connect()).
        # Import it directly here so the incompatibility is caught up front.
        import hermes_constants  # type: ignore  # noqa: E402, F401
        import kanban_bridge as kb  # noqa: E402
        from hermes_cli import kanban_db  # type: ignore  # noqa: E402
    except TypeError:
        _CORE_AVAILABLE = False

_skip_reason = (
    "kanban bridge tests need the Hermes core checkout and a Python >= 3.10 "
    "compatible with it (use ~/.hermes/hermes-agent/venv)"
)


@unittest.skipUnless(_CORE_AVAILABLE, _skip_reason)
class KanbanBridgeTest(unittest.TestCase):
    def setUp(self):
        # Fresh DB per test: connect auto-inits the schema.
        self.conn = kanban_db.connect()
        try:
            yield_conn = True
        except Exception:
            yield_conn = False
        assert yield_conn

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:
            pass

    def _make_task(self, title="Task", **kwargs):
        return kanban_db.create_task(self.conn, title=title, **kwargs)

    # ------------------------------------------------------------------

    def test_board_groups_tasks_by_column(self):
        tid = self._make_task("Alpha")
        board = kb.get_board()
        cols = {c["name"]: c for c in board["columns"]}
        ready_ids = [t["id"] for t in cols["ready"]["tasks"]]
        todo_ids = [t["id"] for t in cols["todo"]["tasks"]]
        self.assertTrue(tid in ready_ids or tid in todo_ids)
        self.assertEqual(board["latestEventId"], self._max_event_id())

    def _max_event_id(self):
        row = self.conn.execute("SELECT COALESCE(MAX(id), 0) AS m FROM task_events").fetchone()
        return int(row["m"])

    def test_create_task_via_bridge(self):
        out = kb.create_task({"title": "From MC", "body": "hello"}, author="mission-control")
        self.assertTrue(out["id"])
        task = kanban_db.get_task(self.conn, out["id"])
        self.assertIsNotNone(task)
        self.assertEqual(task.title, "From MC")

    def test_create_task_requires_title(self):
        with self.assertRaises(kb.KanbanError) as ctx:
            kb.create_task({"title": "   "})
        self.assertEqual(ctx.exception.status_code, 400)

    def test_update_status_direct_and_archive(self):
        tid = self._make_task("Move me")
        out = kb.update_task(tid, {"status": "todo"})
        self.assertEqual(out["task"]["status"], "todo")
        kb.delete_task(tid)
        task = kanban_db.get_task(self.conn, tid)
        self.assertEqual(task.status, "archived")

    def test_cannot_set_running_directly(self):
        tid = self._make_task("No running")
        with self.assertRaises(kb.KanbanError) as ctx:
            kb.update_task(tid, {"status": "running"})
        self.assertEqual(ctx.exception.status_code, 400)

    def test_comment_flow(self):
        tid = self._make_task("With comment")
        kb.add_comment(tid, {"body": "note from MC"}, author="mission-control")
        detail = kb.get_task_detail(tid)
        bodies = [c["body"] for c in detail["comments"]]
        self.assertIn("note from MC", bodies)

    def test_task_detail_includes_runs_and_events(self):
        tid = self._make_task("Detail")
        detail = kb.get_task_detail(tid)
        self.assertEqual(detail["id"], tid)
        self.assertIsInstance(detail["runs"], list)
        self.assertIsInstance(detail["events"], list)
        self.assertGreaterEqual(len(detail["events"]), 1)

    def test_events_cursor_advances(self):
        before = kb.get_events(since=0)["cursor"]
        self._make_task("Cursor probe")
        after = kb.get_events(since=before)
        kinds = [e["kind"] for e in after["events"]]
        self.assertTrue(any(k in ("created",) for k in kinds), f"unexpected: {kinds}")
        self.assertGreater(after["cursor"], before)

    def test_unknown_task_404(self):
        with self.assertRaises(kb.KanbanError) as ctx:
            kb.get_task_detail("t_doesnotexist")
        self.assertEqual(ctx.exception.status_code, 404)

    def test_boards_listing(self):
        data = kb.list_boards()
        slugs = [b["slug"] for b in data["boards"]]
        self.assertIn("default", slugs)


if __name__ == "__main__":
    unittest.main(verbosity=2)
