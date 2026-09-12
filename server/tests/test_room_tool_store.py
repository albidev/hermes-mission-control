from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import room_tool_store  # noqa: E402


def _write_db(path: Path, rooms: list[tuple[str, str, list[dict]]], members_msgs: dict[str, list[tuple[str, str, str, str, float]]]) -> None:
    """Write a minimal hosted-room store.

    rooms: (room_id, name, members-list). members_msgs is unused here —
    profile stores live in their own files (see _write_profile_db).
    """
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE hosted_rooms (
            room_id TEXT PRIMARY KEY, name TEXT NOT NULL, members_json TEXT NOT NULL,
            authority_gateway_id TEXT NOT NULL, authority_epoch INTEGER NOT NULL,
            next_seq INTEGER NOT NULL, event_bytes INTEGER NOT NULL DEFAULT 0,
            revision INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL,
            updated_at REAL NOT NULL, disbanded_at REAL
        );
        """
    )
    for room_id, name, members in rooms:
        db.execute(
            "INSERT INTO hosted_rooms (room_id, name, members_json, authority_gateway_id, "
            "authority_epoch, next_seq, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (room_id, name, json.dumps(members), "install:test", 1, 1, 1.0, 1.0),
        )
    db.commit()
    db.close()


def _write_profile_db(path: Path, session_id: str, session_title: str, rows: list[tuple[str, str, str, str, float]]) -> None:
    """Write a minimal member-profile store with one Group session and messages."""
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY, title TEXT NOT NULL, last_activity_at REAL NOT NULL
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
            role TEXT NOT NULL, tool_name TEXT, content TEXT, tool_calls TEXT,
            timestamp REAL NOT NULL
        );
        """
    )
    db.execute("INSERT INTO sessions (id, title, last_activity_at) VALUES (?,?,?)",
               (session_id, session_title, 1.0))
    for role, tool_name, content, tool_calls, ts in rows:
        db.execute(
            "INSERT INTO messages (session_id, role, tool_name, content, tool_calls, timestamp) VALUES (?,?,?,?,?,?)",
            (session_id, role, tool_name, content, tool_calls, ts),
        )
    db.commit()
    db.close()


class RoomToolStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tempfile.TemporaryDirectory()
        self.state = Path(self.root.name) / "state.db"
        self.profiles = {
            "crossnection": Path(self.root.name) / "profiles" / "crossnection" / "state.db",
            "crossnection-triage": Path(self.root.name) / "profiles" / "crossnection-triage" / "state.db",
        }
        self.profiles["crossnection"].parent.mkdir(parents=True)
        self.profiles["crossnection-triage"].parent.mkdir(parents=True)
        members = [
            {"member_id": "m0", "profile": "crossnection", "handle": "crossnection"},
            {"member_id": "m1", "profile": "crossnection-triage", "handle": "crossnection-triage"},
        ]
        _write_db(
            self.state,
            [("room-1", "Triage IN-202", members), ("room-2", "Empty", [{"member_id": "m2", "profile": "empty", "handle": "empty"}])],
            {},
        )
        _write_profile_db(self.profiles["crossnection"], "sess-crossnection", "Group: room-1", [
            # one call row (assistant) + one tool row
            ("assistant", None, None,
             json.dumps([{"function": {"name": "skill_view", "arguments": "{\"name\":\"jira-crossnection\"}"}}]),
             100.0),
            ("tool", "skill_view", "{\"success\": true, \"name\": \"jira-crossnection\"}", None, 101.0),
            # TUI wrapper 'tool_call' must be translated to the real name
            ("assistant", None, None,
             json.dumps([{"function": {"name": "tool_call", "arguments": "{\"name\":\"mcp__atlassian__getJiraIssue\",\"arguments\":{}}"}}]),
             200.0),
            ("tool", "mcp__atlassian__getJiraIssue", "<untrusted result>", None, 201.0),
        ])
        _write_profile_db(self.profiles["crossnection-triage"], "sess-triage", "Group: room-1", [
            ("tool", "bdh_query", "{\"error\": \"BDH down\"}", None, 150.0),
        ])

    def tearDown(self) -> None:
        self.root.cleanup()

    def _profile_path(self, p: str) -> Path:
        if p in self.profiles:
            return self.profiles[p]
        return Path(self.root.name) / "profiles" / p / "state.db"

    def test_collects_member_tools(self) -> None:
        with patch.object(room_tool_store, "room_store_path", return_value=self.state), \
             patch.object(room_tool_store, "profile_store_path", side_effect=self._profile_path):
            snap = room_tool_store.build_room_tools_snapshot()
        self.assertEqual(snap["room_count"], 2)
        rooms = {r["name"]: r for r in snap["rooms"]}
        triage = rooms["Triage IN-202"]
        self.assertEqual(len(triage["tools"]), 3)
        names = [t["toolName"] for t in triage["tools"]]
        self.assertIn("skill_view", names)
        self.assertIn("mcp__atlassian__getJiraIssue", names)
        self.assertNotIn("tool_call", names)
        self.assertIn("bdh_query", names)
        skill = next(t for t in triage["tools"] if t["toolName"] == "skill_view")
        self.assertEqual(skill["toolInput"], "{\"name\":\"jira-crossnection\"}")
        self.assertIn("success", skill["output"])
        self.assertGreaterEqual(skill.get("durationS", 0), 0.1)
        self.assertEqual(skill["memberHandle"], "crossnection")
        empty = rooms["Empty"]
        self.assertEqual(empty["tools"], [])

    def test_read_room_tools_writes_cache(self) -> None:
        with patch.object(room_tool_store, "room_store_path", return_value=self.state), \
             patch.object(room_tool_store, "profile_store_path", side_effect=self._profile_path), \
             patch.object(room_tool_store, "cache_path", return_value=Path(self.root.name) / "room_tools.json"):
            tools = room_tool_store.read_room_tools("room-1", max_age_seconds=600)
        self.assertEqual(len(tools), 3)
        self.assertTrue((Path(self.root.name) / "room_tools.json").is_file())


if __name__ == "__main__":
    unittest.main()
