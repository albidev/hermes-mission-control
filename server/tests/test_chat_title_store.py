"""Durable Mission Control chat title metadata tests."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import chat_title_store


class ChatTitleStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self._original = chat_title_store._STATE_FILE
        self._tmp = tempfile.TemporaryDirectory()
        chat_title_store._STATE_FILE = Path(self._tmp.name) / "chat_titles.json"

    def tearDown(self) -> None:
        chat_title_store._STATE_FILE = self._original
        self._tmp.cleanup()

    def test_title_is_read_by_runtime_id_and_session_key(self) -> None:
        saved = chat_title_store.set_chat_title("runtime-1", "session-key-1", "Spiegami Inspector")
        self.assertEqual(saved["title"], "Spiegami Inspector")
        self.assertEqual(chat_title_store.get_chat_title("runtime-1"), "Spiegami Inspector")
        self.assertEqual(chat_title_store.get_chat_title("session-key-1"), "Spiegami Inspector")

    def test_title_is_trimmed_and_capped(self) -> None:
        chat_title_store.set_chat_title("runtime-1", "", "  A title  ")
        self.assertEqual(chat_title_store.get_chat_title("runtime-1"), "A title")
        with self.assertRaises(ValueError):
            chat_title_store.set_chat_title("", "", "missing identifiers")


if __name__ == "__main__":
    unittest.main()
