"""Tests for the ephemeral Mission Control resume runtime presence."""
from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
presence = importlib.import_module("chat_runtime_presence")
mission_control_agents = importlib.import_module("mission_control_agents")


class RuntimePresenceLeaseTest(unittest.TestCase):
    def setUp(self) -> None:
        presence.clear_runtime_presence()

    def tearDown(self) -> None:
        presence.clear_runtime_presence()

    def test_multiple_devices_keep_the_same_resumed_runtime_live(self) -> None:
        first = {
            "clientId": "desktop",
            "runtimeSessionId": "runtime-1",
            "resumedFrom": "stored-1",
            "sessionKey": "stored-1",
            "phase": "connected",
        }
        second = {**first, "clientId": "iphone"}

        presence.update_runtime_presence(first)
        result = presence.update_runtime_presence(second)

        self.assertEqual(result["activeLeases"], 2)
        self.assertEqual({item["clientId"] for item in presence.active_runtime_presences()}, {"desktop", "iphone"})

        presence.update_runtime_presence({**first, "phase": "closed"})
        self.assertEqual([item["clientId"] for item in presence.active_runtime_presences()], ["iphone"])

    def test_expired_lease_falls_back_to_session_db_state(self) -> None:
        with patch.object(presence.time, "time", return_value=100.0):
            presence.update_runtime_presence({
                "clientId": "desktop",
                "runtimeSessionId": "runtime-1",
                "resumedFrom": "stored-1",
                "phase": "connected",
            })
        with patch.object(presence.time, "time", return_value=100.0 + presence.PRESENCE_TTL_SECONDS + 1):
            self.assertEqual(presence.active_runtime_presences(), [])

    def test_invalid_phase_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            presence.update_runtime_presence({
                "clientId": "desktop",
                "runtimeSessionId": "runtime-1",
                "resumedFrom": "stored-1",
                "phase": "live-forever",
            })

    def test_runtime_lease_merges_into_canonical_session_without_duplicate(self) -> None:
        item = {
            "sessionId": "stored-1",
            "title": "Original conversation",
            "status": "ended",
            "endedAt": 99,
            "lastActiveAt": 99,
            "model": "gpt-5.6-luna-900k",
        }
        lease = {
            "runtimeSessionId": "runtime-1",
            "resumedFrom": "stored-1",
            "sessionKey": "stored-1",
            "source": "mission-control",
            "model": "gpt-5.6-luna-900k",
            "title": "Original conversation",
            "updatedAt": 100,
        }
        with patch.object(mission_control_agents, "active_runtime_presences", return_value=[lease]):
            items = [item]
            mission_control_agents._apply_runtime_presence(items)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["sessionId"], "stored-1")
        self.assertEqual(items[0]["status"], "live")
        self.assertEqual(items[0]["runtimeSessionId"], "runtime-1")


if __name__ == "__main__":
    unittest.main()
