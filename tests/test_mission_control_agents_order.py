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


if __name__ == "__main__":
    unittest.main()
