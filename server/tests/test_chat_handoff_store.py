"""Durable Mission Control Bot handoff storage tests."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import chat_handoff_store


class ChatHandoffStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self._original = chat_handoff_store._STATE_FILE
        self._tmp = tempfile.TemporaryDirectory()
        chat_handoff_store._STATE_FILE = Path(self._tmp.name) / "chat_handoffs.json"
        chat_handoff_store.clear_handoffs()

    def tearDown(self) -> None:
        chat_handoff_store.clear_handoffs()
        chat_handoff_store._STATE_FILE = self._original
        self._tmp.cleanup()

    def test_upsert_is_durable_and_deduplicated_by_handoff_id(self) -> None:
        queued = {"id": "handoff-1", "handle": "crossnection", "status": "queued", "request": "Pentair?", "updatedAt": 1}
        completed = {**queued, "status": "completed", "reply": "A synthesized answer.", "updatedAt": 2}
        chat_handoff_store.upsert_handoff("origin-session", queued)
        chat_handoff_store.upsert_handoff("origin-session", completed)
        self.assertEqual(chat_handoff_store.list_handoffs("origin-session"), [completed])
        self.assertEqual(chat_handoff_store.list_handoffs("other-session"), [])
        chat_handoff_store.upsert_handoff("origin-session", {**queued, "status": "queued", "updatedAt": 0})
        self.assertEqual(chat_handoff_store.list_handoffs("origin-session"), [completed])
        self.assertIn("origin-session", chat_handoff_store._STATE_FILE.read_text(encoding="utf-8"))

    def test_sessions_are_isolated(self) -> None:
        row = {"id": "handoff-1", "status": "failed"}
        chat_handoff_store.upsert_handoff("s1", row)
        chat_handoff_store.upsert_handoff("s2", row)
        self.assertEqual(len(chat_handoff_store.list_handoffs("s1")), 1)
        self.assertEqual(len(chat_handoff_store.list_handoffs("s2")), 1)


if __name__ == "__main__":
    unittest.main()
