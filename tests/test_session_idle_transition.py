"""Tests for the live -> idle transition detector and gateway forwarder.

The detector is a *separate* observer over session liveness facts. It must
emit exactly one authenticated idle signal per live -> idle transition, never
repeat while idle, re-arm only after the session is observed live again, and
exclude ended sessions. The forwarder must never raise: gateway failures are
returned as ``{ok: False, ...}`` so normal session polling is unaffected.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

import session_idle_transition as sit  # noqa: E402
import mission_control_agents as mca  # noqa: E402


def _facts(session_id, last_active, ended_at=None):
    return {"sessionId": session_id, "lastActiveAt": last_active, "endedAt": ended_at}


class IdleTransitionDetectorTests(unittest.TestCase):
    def test_live_to_idle_emits_single_signal(self):
        detector = sit.IdleTransitionDetector(live_window_seconds=300)
        with mock.patch.object(mca.time, "time", return_value=1000.0):
            detector.observe([_facts("s1", 900.0)], now=1000.0)  # live
        with mock.patch.object(mca.time, "time", return_value=1400.0):
            signals = detector.observe([_facts("s1", 900.0)], now=1400.0)  # idle
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0]["event"], "session_idle")
        self.assertEqual(signals[0]["session_id"], "s1")
        self.assertEqual(signals[0]["transition_key"], "live_to_idle:s1:1")
        self.assertEqual(signals[0]["occurred_at"], 1400.0)

    def test_no_repeat_while_idle(self):
        detector = sit.IdleTransitionDetector()
        with mock.patch.object(mca.time, "time", return_value=1000.0):
            detector.observe([_facts("s1", 900.0)], now=1000.0)
        with mock.patch.object(mca.time, "time", return_value=1400.0):
            self.assertEqual(len(detector.observe([_facts("s1", 900.0)], now=1400.0)), 1)
        with mock.patch.object(mca.time, "time", return_value=2000.0):
            self.assertEqual(detector.observe([_facts("s1", 900.0)], now=2000.0), [])

    def test_rearm_after_live_again(self):
        detector = sit.IdleTransitionDetector()
        with mock.patch.object(mca.time, "time", return_value=1000.0):
            detector.observe([_facts("s1", 900.0)], now=1000.0)
        with mock.patch.object(mca.time, "time", return_value=1400.0):
            first = detector.observe([_facts("s1", 900.0)], now=1400.0)
        with mock.patch.object(mca.time, "time", return_value=1500.0):
            detector.observe([_facts("s1", 1490.0)], now=1500.0)  # live again
        with mock.patch.object(mca.time, "time", return_value=2000.0):
            second = detector.observe([_facts("s1", 1490.0)], now=2000.0)  # idle again
        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 1)
        self.assertNotEqual(first[0]["transition_key"], second[0]["transition_key"])
        self.assertEqual(second[0]["transition_key"], "live_to_idle:s1:2")

    def test_ended_sessions_excluded(self):
        detector = sit.IdleTransitionDetector()
        with mock.patch.object(mca.time, "time", return_value=1000.0):
            detector.observe([_facts("s1", 900.0)], now=1000.0)
        # Session ends before going idle -> no signal.
        with mock.patch.object(mca.time, "time", return_value=1400.0):
            signals = detector.observe([_facts("s1", 900.0, ended_at=1200.0)], now=1400.0)
        self.assertEqual(signals, [])

    def test_ended_while_idle_forgets_state(self):
        detector = sit.IdleTransitionDetector()
        with mock.patch.object(mca.time, "time", return_value=1000.0):
            detector.observe([_facts("s1", 900.0)], now=1000.0)
        with mock.patch.object(mca.time, "time", return_value=1400.0):
            detector.observe([_facts("s1", 900.0)], now=1400.0)  # fires
        # Ended now; a later poll must not fire again.
        with mock.patch.object(mca.time, "time", return_value=2000.0):
            self.assertEqual(detector.observe([_facts("s1", 900.0, ended_at=1500.0)], now=2000.0), [])

    def test_idle_on_first_observation_no_signal(self):
        detector = sit.IdleTransitionDetector()
        with mock.patch.object(mca.time, "time", return_value=1400.0):
            self.assertEqual(detector.observe([_facts("s1", 900.0)], now=1400.0), [])


class IdleSignalForwarderTests(unittest.TestCase):
    def test_forward_success_posts_authenticated_payload(self):
        seen = {}

        class FakeResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b"{}"

        def fake_urlopen(request, timeout):
            seen["url"] = request.full_url
            seen["method"] = request.method
            seen["auth"] = request.get_header("Authorization")
            seen["body"] = json.loads(request.data.decode("utf-8"))
            seen["timeout"] = timeout
            return FakeResponse()

        signal = {"event": "session_idle", "session_id": "s1", "transition_key": "k", "occurred_at": 1.0}
        with mock.patch.object(sit.urllib.request, "urlopen", fake_urlopen):
            result = sit.forward_idle_signal(signal, base_url="http://gw/api/gateway/session-idle", token="tok")

        self.assertTrue(result["ok"])
        self.assertEqual(seen["url"], "http://gw/api/gateway/session-idle")
        self.assertEqual(seen["method"], "POST")
        self.assertEqual(seen["auth"], "Bearer tok")
        self.assertEqual(seen["body"], signal)
        self.assertEqual(seen["timeout"], 5.0)

    def test_default_forward_target_matches_core_idle_endpoint(self):
        seen = {}

        class FakeResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b"{}"

        def fake_urlopen(request, timeout):
            seen["url"] = request.full_url
            return FakeResponse()

        signal = {"event": "session_idle", "session_id": "s1", "transition_key": "k", "occurred_at": 1.0}
        with mock.patch.object(sit.urllib.request, "urlopen", fake_urlopen):
            result = sit.forward_idle_signal(signal, token="tok")

        self.assertTrue(result["ok"])
        self.assertEqual(seen["url"], "http://127.0.0.1:8642/api/sessions/s1/idle")

    def test_idle_signal_token_prefers_dedicated_gateway_secret(self):
        with mock.patch.dict(os.environ, {"MISSION_CONTROL_IDLE_SIGNAL_TOKEN": "gateway", "API_SERVER_KEY": "ui"}):
            self.assertEqual(sit._idle_signal_token(), "gateway")

    def test_forward_http_error_is_handled(self):
        import urllib.error

        def fake_urlopen(request, timeout):
            raise urllib.error.HTTPError(request.full_url, 503, "unavailable", {}, None)

        with mock.patch.object(sit.urllib.request, "urlopen", fake_urlopen):
            result = sit.forward_idle_signal({"session_id": "s1"}, base_url="http://gw", token="t")
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], 503)

    def test_forward_network_error_is_handled(self):
        import urllib.error

        def fake_urlopen(request, timeout):
            raise urllib.error.URLError("connection refused")

        with mock.patch.object(sit.urllib.request, "urlopen", fake_urlopen):
            result = sit.forward_idle_signal({"session_id": "s1"}, base_url="http://gw", token="t")
        self.assertFalse(result["ok"])
        self.assertIn("gateway unavailable", result["error"])

    def test_watcher_tick_handles_gateway_failure(self):
        detector = sit.IdleTransitionDetector()
        with mock.patch.object(sit, "_collect_session_facts", return_value=[_facts("s1", 900.0)]):
            with mock.patch.object(mca.time, "time", return_value=1000.0):
                sit._idle_watcher_tick(detector)  # live -> no signal
            with mock.patch.object(mca.time, "time", return_value=1400.0):
                with mock.patch.object(
                    sit, "forward_idle_signal", return_value={"ok": False, "error": "gateway unavailable"}
                ):
                    results = sit._idle_watcher_tick(detector)
        self.assertEqual(results, [{"ok": False, "error": "gateway unavailable"}])


class IdleSignalContractTests(unittest.TestCase):
    def test_contract_schema_describes_idle_signal(self):
        schema_path = (
            Path(__file__).resolve().parents[1] / "docs" / "contracts" / "mission-control-idle-signal-v1.json"
        )
        schema = json.loads(schema_path.read_text())
        self.assertEqual(schema["title"], "Mission Control Idle Signal v1")
        required = schema["required"]
        self.assertIn("event", required)
        self.assertIn("session_id", required)
        self.assertIn("transition_key", required)
        self.assertIn("occurred_at", required)

    def test_emitted_signal_matches_contract(self):
        detector = sit.IdleTransitionDetector()
        with mock.patch.object(mca.time, "time", return_value=1000.0):
            detector.observe([_facts("s1", 900.0)], now=1000.0)
        with mock.patch.object(mca.time, "time", return_value=1400.0):
            signals = detector.observe([_facts("s1", 900.0)], now=1400.0)
        self.assertEqual(len(signals), 1)
        signal = signals[0]
        self.assertEqual(signal["event"], "session_idle")
        self.assertIsInstance(signal["session_id"], str)
        self.assertTrue(signal["transition_key"].startswith("live_to_idle:s1:"))
        self.assertIsInstance(signal["occurred_at"], float)


if __name__ == "__main__":
    unittest.main()
