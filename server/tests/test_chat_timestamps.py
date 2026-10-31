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
    try:
        import pytest
    except ModuleNotFoundError as exc:
        # unittest discovery imports this pytest-only module when Hermes core
        # is present, even though pytest is not a Mission Control dependency.
        import unittest

        @unittest.skip(f"requires pytest: {exc.name or 'pytest'}")
        class PytestTimestampTests(unittest.TestCase):
            def test_pytest_dependency_is_available(self):
                pass
    else:
        import local_telemetry_server

        @pytest.mark.parametrize("profile_kwargs", [{}, {"profile": "fixture-bot"}], ids=["default", "explicit-profile"])
        def test_load_chat_message_timestamps_reads_resolved_sessiondb_rows(tmp_path, monkeypatch, profile_kwargs):
            db_path = tmp_path / "state.db"
            writable = SessionDB(db_path=db_path)
            writable.create_session("session-parent", "tui", session_key="chat-key")
            writable.append_message("session-parent", role="user", content="repeat", timestamp=100.0)
            writable.append_message("session-parent", role="assistant", content="same answer", timestamp=101.0)
            writable.append_message("session-parent", role="user", content="repeat", timestamp=200.0)
            writable.close()

            requested_profiles = []

            def open_fixture_db(profile: str | None = None):
                requested_profiles.append(profile)
                return SessionDB(db_path=db_path, read_only=True)

            monkeypatch.setattr(mission_control_agents, "_try_get_session_db", open_fixture_db)
            payload = mission_control_agents.load_chat_message_timestamps(session_key="chat-key", **profile_kwargs)

            assert requested_profiles == [profile_kwargs.get("profile")]
            assert payload["sessionId"] == "session-parent"
            assert payload["sessionKey"] == "chat-key"
            assert [row["timestamp"] for row in payload["messages"]] == [100.0, 101.0, 200.0]
            assert [row["content"] for row in payload["messages"]] == ["repeat", "same answer", "repeat"]


        @pytest.mark.parametrize("profile_kwargs", [{}, {"profile": "fixture-bot"}], ids=["default", "explicit-profile"])
        def test_load_chat_transcript_returns_complete_stable_rows_for_large_repeated_history(tmp_path, monkeypatch, profile_kwargs):
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

            requested_profiles = []

            def open_fixture_db(profile: str | None = None):
                requested_profiles.append(profile)
                return SessionDB(db_path=db_path, read_only=True)

            monkeypatch.setattr(mission_control_agents, "_try_get_session_db", open_fixture_db)
            payload = mission_control_agents.load_chat_transcript(session_key="large-key", **profile_kwargs)

            assert requested_profiles == [profile_kwargs.get("profile")]
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


        @pytest.mark.parametrize("profile_kwargs", [{}, {"profile": "fixture-bot"}], ids=["default", "explicit-profile"])
        def test_load_chat_transcript_pages_preserve_stable_identity_and_order(tmp_path, monkeypatch, profile_kwargs):
            db_path = tmp_path / "state.db"
            writable = SessionDB(db_path=db_path)
            writable.create_session("session-paged", "tui", session_key="paged-key")
            for index in range(11):
                writable.append_message(
                    "session-paged",
                    role="user" if index % 2 == 0 else "assistant",
                    content=f"message-{index}",
                    timestamp=100.0 + index,
                )
            writable.close()

            requested_profiles = []

            def open_fixture_db(profile: str | None = None):
                requested_profiles.append(profile)
                return SessionDB(db_path=db_path, read_only=True)

            monkeypatch.setattr(mission_control_agents, "_try_get_session_db", open_fixture_db)
            kwargs = {**profile_kwargs, "session_key": "paged-key"}
            full = mission_control_agents.load_chat_transcript(**kwargs)
            all_ids = [row["id"] for row in full["messages"]]
            latest = mission_control_agents.load_chat_transcript(**kwargs, limit=4)
            latest_ids = [row["id"] for row in latest["messages"]]

            assert latest["complete"] is True
            assert latest["count"] == 4
            assert latest["totalCount"] == len(all_ids) == 11
            assert latest["hasMore"] is True
            assert latest_ids == all_ids[-4:]
            assert latest["nextCursor"] == latest_ids[0]
            assert [row["canonical_order"] for row in latest["messages"]] == list(range(7, 11))

            older = mission_control_agents.load_chat_transcript(
                **kwargs, limit=4, before_id=latest["nextCursor"]
            )
            assert [row["id"] for row in older["messages"]] == all_ids[-8:-4]
            assert [row["canonical_order"] for row in older["messages"]] == list(range(3, 7))
            assert older["hasMore"] is True
            assert older["nextCursor"] == all_ids[-8]
            assert set(latest_ids).isdisjoint(row["id"] for row in older["messages"])
            assert requested_profiles == [profile_kwargs.get("profile")] * 3


        def test_local_chat_transcript_route_passes_pagination_arguments(monkeypatch):
            calls = []
            responses = []
            monkeypatch.setattr(local_telemetry_server, "_is_authorized", lambda _handler: True)
            monkeypatch.setattr(
                local_telemetry_server,
                "load_chat_transcript",
                lambda *args, **kwargs: calls.append((args, kwargs)) or {"complete": True, "messages": []},
            )
            monkeypatch.setattr(
                local_telemetry_server.Handler,
                "_json",
                lambda _self, status, payload: responses.append((status, payload)),
            )
            handler = object.__new__(local_telemetry_server.Handler)
            handler.path = "/api/local/chat/transcript?session_id=session-1&profile=fixture-bot&limit=120&before_id=db%3A8"

            local_telemetry_server.Handler.do_GET(handler)

            assert calls == [(("session-1", None, "fixture-bot"), {"limit": 120, "before_id": "db:8"})]
            assert responses == [(200, {"complete": True, "messages": []})]
