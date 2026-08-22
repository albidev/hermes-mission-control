"""Tests for the whiteboard snapshot/command store protocol."""
from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
whiteboard_store = importlib.import_module("whiteboard_store")


class WhiteboardStoreProtocolTest(unittest.TestCase):
    def setUp(self) -> None:
        self._original_file = whiteboard_store._STATE_FILE
        whiteboard_store._STATE_FILE = Path(self._original_file.parent / "whiteboard_state.test.json")
        if whiteboard_store._STATE_FILE.exists():
            whiteboard_store._STATE_FILE.unlink()

    def tearDown(self) -> None:
        if whiteboard_store._STATE_FILE.exists():
            whiteboard_store._STATE_FILE.unlink()
        whiteboard_store._STATE_FILE = self._original_file

    def test_get_reports_protocol_version_and_features(self) -> None:
        state = whiteboard_store.get_whiteboard("s1")
        self.assertEqual(state["protocolVersion"], whiteboard_store.WHITEBOARD_PROTOCOL_VERSION)
        self.assertIn("create_binding", state["features"])
        self.assertIn("move_shape", state["features"])
        self.assertEqual(state["commands"], [])

    def test_enqueue_stamps_protocol_version_and_feature(self) -> None:
        item = whiteboard_store.enqueue_command("s1", {"type": "create_box", "x": 10, "y": 10})
        self.assertEqual(item["protocolVersion"], 2)
        self.assertEqual(item["feature"], "1")
        pending = whiteboard_store.get_whiteboard("s1")["commands"]
        self.assertEqual([c["id"] for c in pending], [item["id"]])

    def test_v2_feature_is_tagged(self) -> None:
        item = whiteboard_store.enqueue_command("s1", {"type": "create_binding", "fromId": "a", "toId": "b"})
        self.assertEqual(item["feature"], "2")

    def test_negative_line_width_normalized(self) -> None:
        item = whiteboard_store.enqueue_command("s1", {"type": "create_line", "x": 200, "y": 5, "w": -180, "h": 0})
        self.assertEqual(item["w"], 180)
        self.assertEqual(item["x"], 20)

    def test_acknowledge_removes_only_applied_commands(self) -> None:
        first = whiteboard_store.enqueue_command("s1", {"type": "clear"})
        second = whiteboard_store.enqueue_command("s1", {"type": "create_frame"})
        whiteboard_store.acknowledge_commands("s1", [first["id"]])
        remaining = whiteboard_store.get_whiteboard("s1")["commands"]
        self.assertEqual([c["id"] for c in remaining], [second["id"]])

    def test_fallback_session_used_when_primary_missing(self) -> None:
        whiteboard_store.save_snapshot("stable-key", {"document": {"store": {}}})
        state = whiteboard_store.get_whiteboard("runtime-id", fallback_session_id="stable-key")
        self.assertIsNotNone(state["snapshot"])

    def test_unknown_feature_is_none_not_error(self) -> None:
        item = whiteboard_store.enqueue_command("s1", {"type": "teleport_board_to_mars"})
        self.assertIsNone(item["feature"])
        # Unknown command stays queued; client decides whether it can apply it.
        pending = whiteboard_store.get_whiteboard("s1")["commands"]
        self.assertEqual(len(pending), 1)


if __name__ == "__main__":
    unittest.main()
