from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mc_room_mcp  # noqa: E402


class MissionControlRoomMcpTests(unittest.TestCase):
    def make_db(self, root: Path, *, duplicate_name: bool = False) -> Path:
        path = root / "state.db"
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
            CREATE TABLE hosted_room_events (
                room_id TEXT NOT NULL, seq INTEGER NOT NULL, event_id TEXT NOT NULL,
                kind TEXT NOT NULL, actor_json TEXT NOT NULL, authority_epoch INTEGER,
                payload_json TEXT NOT NULL, created_at REAL NOT NULL,
                PRIMARY KEY (room_id, seq)
            );
            """
        )
        members = json.dumps([
            {"member_id": "ops", "profile": "example-bot", "handle": "example-bot"},
            {"member_id": "triage", "profile": "example-bot-ops", "handle": "example-bot-ops"},
        ])
        rooms = [("room-1", "Triage Board"), ("room-2", "Triage Board")] if duplicate_name else [("room-1", "Triage Board")]
        for room_id, name in rooms:
            db.execute(
                "INSERT INTO hosted_rooms VALUES (?, ?, ?, ?, ?, ?, 0, 1, 1, 2, NULL)",
                (room_id, name, members, "gw", 1, 3),
            )
        db.execute(
            "INSERT INTO hosted_room_events VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("room-1", 1, "evt-1", "message.user", json.dumps({"kind": "user", "id": "operator"}), 1, json.dumps({"text": "Analizza il board"}), 1),
        )
        db.execute(
            "INSERT INTO hosted_room_events VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("room-1", 2, "evt-2", "message.member", json.dumps({"kind": "member", "id": "ops", "display_name": "Example Bot"}), 1, json.dumps({"text": "Coinvolgo il triage."}), 2),
        )
        db.commit()
        db.close()
        return path

    def test_read_and_context_are_bounded_and_authorized(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = self.make_db(root)
            profile_home = root / "profiles" / "example-bot"
            with patch.dict(
                "os.environ",
                {"MC_GROUP_ROOMS_DB_PATH": str(db_path), "HERMES_HOME": str(profile_home)},
                clear=False,
            ):
                result = json.loads(mc_room_mcp.room_read(room_name="Triage Board", limit=1))
                self.assertEqual(result["room"]["room_id"], "room-1")
                self.assertEqual(len(result["events"]), 1)
                self.assertTrue(result["has_more"])
                context = json.loads(mc_room_mcp.room_context(room_id="room-1", since_seq=1))
                self.assertIn("Example Bot", context["context_text"])
                self.assertTrue(context["read_only"])

    def test_ambiguous_name_and_non_member_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = self.make_db(root, duplicate_name=True)
            with patch.dict(
                "os.environ",
                {"MC_GROUP_ROOMS_DB_PATH": str(db_path), "HERMES_HOME": str(root / "profiles" / "reviewer")},
                clear=False,
            ):
                ambiguous = json.loads(mc_room_mcp.room_read(room_name="Triage Board"))
                self.assertEqual(ambiguous["error"], "ambiguous_room_name")
                denied = json.loads(mc_room_mcp.room_read(room_id="room-1"))
                self.assertEqual(denied["error"], "Room access denied for the active profile.")


if __name__ == "__main__":
    unittest.main()
