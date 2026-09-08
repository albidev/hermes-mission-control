"""Canonical SessionDB timestamp bridge tests."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from hermes_state import SessionDB
    import mission_control_agents
except ModuleNotFoundError as exc:
    # The public Mission Control repository does not contain the private/core
    # Hermes checkout. Keep unittest discovery green and let pytest report a
    # proper skip when the optional core dependency is unavailable.
    import unittest

    _missing_module = exc.name or "Hermes core"

    @unittest.skip(f"requires Hermes core module: {_missing_module}")
    class HermesCoreTimestampTests(unittest.TestCase):
        def test_hermes_core_dependency_is_available(self):
            pass
else:
    def test_load_chat_message_timestamps_reads_resolved_sessiondb_rows(tmp_path, monkeypatch):
        db_path = tmp_path / "state.db"
        writable = SessionDB(db_path=db_path)
        writable.create_session("session-parent", "tui", session_key="chat-key")
        writable.append_message("session-parent", role="user", content="repeat", timestamp=100.0)
        writable.append_message("session-parent", role="assistant", content="same answer", timestamp=101.0)
        writable.append_message("session-parent", role="user", content="repeat", timestamp=200.0)
        writable.close()

        def open_fixture_db():
            return SessionDB(db_path=db_path, read_only=True)

        monkeypatch.setattr(mission_control_agents, "_try_get_session_db", open_fixture_db)
        payload = mission_control_agents.load_chat_message_timestamps(session_key="chat-key")

        assert payload["sessionId"] == "session-parent"
        assert payload["sessionKey"] == "chat-key"
        assert [row["timestamp"] for row in payload["messages"]] == [100.0, 101.0, 200.0]
        assert [row["content"] for row in payload["messages"]] == ["repeat", "same answer", "repeat"]


    def test_load_chat_transcript_returns_complete_stable_rows_for_large_repeated_history(tmp_path, monkeypatch):
        db_path = tmp_path / "state.db"
        writable = SessionDB(db_path=db_path)
        writable.create_session("session-large", "tui", session_key="large-key")
        for index in range(240):
            writable.append_message(
                "session-large",
                role="user" if index % 2 == 0 else "assistant",
                content="repeat" if index % 3 == 0 else "same answer",
                timestamp=100.0 + index,
            )
        writable.close()

        def open_fixture_db():
            return SessionDB(db_path=db_path, read_only=True)

        monkeypatch.setattr(mission_control_agents, "_try_get_session_db", open_fixture_db)
        payload = mission_control_agents.load_chat_transcript(session_key="large-key")

        assert payload["complete"] is True
        assert payload["count"] == 240
        assert len(payload["messages"]) == 240
        assert len({row["id"] for row in payload["messages"]}) == 240
        assert [row["id"] for row in payload["messages"]] == [
            row["id"] for row in sorted(
                payload["messages"], key=lambda row: int(row["id"].split(":", 1)[1])
            )
        ]
        assert [row["content"] for row in payload["messages"]].count("repeat") > 1
