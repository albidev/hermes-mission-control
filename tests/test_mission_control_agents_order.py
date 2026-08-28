import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).resolve().parents[1] / "server" / "mission_control_agents.py"
SPEC = importlib.util.spec_from_file_location("mission_control_agents_order_test", MODULE_PATH)
assert SPEC and SPEC.loader
mission_control_agents = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = mission_control_agents
SPEC.loader.exec_module(mission_control_agents)


class MissionControlSessionOrderTests(unittest.TestCase):
    def test_index_only_sessions_fill_first_page_before_db_history(self):
        index = {
            "gateway-live": {
                "session_id": "gateway-live",
                "platform": "telegram",
                "updated_at": "2026-08-24T12:00:10+00:00",
            },
            "gateway-recent": {
                "session_id": "gateway-recent",
                "platform": "discord",
                "updated_at": "2026-08-24T12:00:05+00:00",
            },
        }
        with (
            patch.object(mission_control_agents, "_read_gateway_sessions_index", return_value=index),
            patch.object(mission_control_agents, "_iter_db_session_ids", return_value=["db-new", "db-old"]),
            patch.object(mission_control_agents, "_try_get_session_db", return_value=None),
            patch.object(mission_control_agents, "_read_session_jsonl", return_value=[]),
        ):
            items = mission_control_agents._collect_agent_sessions(limit=2)

        self.assertEqual([item["sessionId"] for item in items], ["gateway-live", "gateway-recent"])
    def test_session_item_exposes_canonical_origin_metadata(self):
        with patch.object(mission_control_agents, "_trace_mode_for_artifacts", return_value="native"):
            item = mission_control_agents._build_session_item(
                "cron-1",
                None,
                None,
                {
                    "source": "cron",
                    "model": "deepseek-v4-flash",
                    "title": "Nightly consolidation",
                    "last_active": "2026-08-28T10:00:00+00:00",
                    "ended_at": "2026-08-28T10:01:00+00:00",
                },
                300,
            )

        self.assertEqual(item["category"], "automation")
        self.assertEqual(item["originLabel"], "Cron")
        self.assertFalse(item["isResumable"])
        self.assertEqual(item["status"], "ended")

    def test_session_title_ignores_workspace_path_and_uses_preview(self):
        with patch.object(mission_control_agents, "_trace_mode_for_artifacts", return_value="native"):
            item = mission_control_agents._build_session_item(
                "session-1",
                {"display_name": "~/Projects/hermes-mission-control"},
                None,
                {
                    "source": "mission-control",
                    "model": "gpt-test",
                    "title": "~/Projects/hermes-mission-control",
                    "preview": "Mostrare il titolo della sessione",
                    "last_active": "2026-08-28T10:00:00+00:00",
                },
                300,
            )

        self.assertEqual(item["title"], "Mostrare il titolo della sessione")

    def test_session_title_uses_untitled_when_only_path_fallbacks_exist(self):
        with patch.object(mission_control_agents, "_trace_mode_for_artifacts", return_value="native"):
            item = mission_control_agents._build_session_item(
                "session-2",
                {"display_name": "~/Projects/hermes-mission-control"},
                None,
                {
                    "source": "mission-control",
                    "model": "gpt-test",
                    "title": "/Users/albi/Projects/hermes-mission-control",
                    "preview": "~/Projects/hermes-mission-control",
                    "last_active": "2026-08-28T10:00:00+00:00",
                },
                300,
            )

        self.assertEqual(item["title"], "Untitled session")

    def test_session_facets_group_status_and_category(self):
        items = [
            {"status": "live", "category": "conversation", "source": "tui"},
            {"status": "ended", "category": "automation", "source": "cron"},
            {"status": "idle", "category": "automation", "source": "kanban"},
        ]
        facets = mission_control_agents._build_session_facets(items)
        self.assertEqual(facets["status"], {"live": 1, "idle": 1, "ended": 1})
        self.assertEqual(facets["category"], {"conversation": 1, "automation": 2, "system": 0, "unknown": 0})
        self.assertEqual(facets["origin"], {"tui": 1, "cron": 1, "kanban": 1})
        self.assertEqual(facets["model"], {})

    def test_session_tab_counts_apply_shared_filters_without_tab_leaking(self):
        items = [
            {"status": "live", "category": "conversation", "source": "tui", "model": "model-a", "title": "alpha"},
            {"status": "ended", "category": "conversation", "source": "discord", "model": "model-a", "title": "alpha"},
            {"status": "ended", "category": "automation", "source": "cron", "model": "model-a", "title": "alpha"},
            {"status": "live", "category": "system", "source": "system", "model": "model-b", "title": "beta"},
        ]
        counts = mission_control_agents._build_session_tab_counts(items, {"query": "alpha", "model": "model-a"})
        self.assertEqual(counts, {"all": 3, "live": 1, "conversation": 2, "automation": 1, "system": 0})

    def test_filtered_snapshot_applies_filter_before_offset_and_limit(self):
        index = {
            "conversation-1": {"session_id": "conversation-1", "platform": "tui", "updated_at": "2026-08-20T12:00:00+00:00"},
            "system-1": {"session_id": "system-1", "platform": "system", "updated_at": "2026-08-19T12:00:00+00:00"},
            "system-2": {"session_id": "system-2", "platform": "system", "updated_at": "2026-08-18T12:00:00+00:00"},
        }
        with (
            patch.object(mission_control_agents, "_read_gateway_sessions_index", return_value=index),
            patch.object(mission_control_agents, "_iter_db_session_ids", return_value=[]),
            patch.object(mission_control_agents, "_try_get_session_db", return_value=None),
            patch.object(mission_control_agents, "_read_session_jsonl", return_value=[]),
        ):
            snapshot = mission_control_agents.load_agents_sessions_snapshot(
                limit=1,
                offset=1,
                filters={"category": "system"},
            )

        self.assertEqual([item["sessionId"] for item in snapshot["items"]], ["system-2"])
        self.assertEqual(snapshot["pagination"], {"total": 2, "offset": 1, "limit": 1, "hasMore": False})
        self.assertEqual(snapshot["stats"]["totalSessions"], 3)
        self.assertEqual(snapshot["facets"]["category"]["system"], 2)
        self.assertEqual(snapshot["tabCounts"], {"all": 3, "live": 0, "conversation": 1, "automation": 0, "system": 2})

    def test_identical_snapshot_requests_use_short_cache(self):
        payload = {"success": True, "items": [], "pagination": {"total": 0}}
        with patch.object(mission_control_agents, "_load_agents_sessions_snapshot_uncached", return_value=payload) as uncached:
            first = mission_control_agents.load_agents_sessions_snapshot(limit=7, offset=0, filters={"query": "cache-probe"})
            second = mission_control_agents.load_agents_sessions_snapshot(limit=7, offset=0, filters={"query": "cache-probe"})

        self.assertIs(first, second)
        uncached.assert_called_once()


if __name__ == "__main__":
    unittest.main()
