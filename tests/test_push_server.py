"""Tests for Web Push optional-dependency handling in push_server.

Covers:
- push_status() reasons for disabled/enabled configurations.
- VAPID contact requirement (no hardcoded platform-specific fallback).
- send_push() disabled paths (missing config, missing dependency).
- Gateway watcher resilience when optional deps are absent (telemetry boot
  must not fail on Linux without pywebpush/websockets).
"""
from __future__ import annotations

import importlib
import importlib.util
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

import push_server  # noqa: E402

_VAPID_PRIVATE = "test-private-key"  # opaque: push_status() only checks presence
_VAPID_PUBLIC = "test-public-key"
_VAPID_CONTACT = "mailto:ops@example.com"

_REQUIRED_ENV = (
    "MISSION_CONTROL_VAPID_PRIVATE_KEY",
    "MISSION_CONTROL_VAPID_PUBLIC_KEY",
    "MISSION_CONTROL_VAPID_CONTACT",
)


class PushStatusTest(unittest.TestCase):
    def setUp(self) -> None:
        self._env_backup = {key: __import__("os").environ.get(key) for key in _REQUIRED_ENV}
        for key in _REQUIRED_ENV:
            __import__("os").environ.pop(key, None)

    def tearDown(self) -> None:
        import os

        for key, value in self._env_backup.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_unset_vapid_reports_not_configured(self) -> None:
        status = push_server.push_status()
        self.assertFalse(status["enabled"])
        self.assertEqual(status["reason"], "vapid_not_configured")
        self.assertIn("MISSION_CONTROL_VAPID_PRIVATE_KEY", status["missingConfig"])
        self.assertIn("MISSION_CONTROL_VAPID_PUBLIC_KEY", status["missingConfig"])

    def test_missing_contact_reports_not_configured(self) -> None:
        import os

        os.environ["MISSION_CONTROL_VAPID_PRIVATE_KEY"] = _VAPID_PRIVATE
        os.environ["MISSION_CONTROL_VAPID_PUBLIC_KEY"] = _VAPID_PUBLIC
        status = push_server.push_status()
        self.assertFalse(status["enabled"])
        self.assertEqual(status["reason"], "vapid_not_configured")
        self.assertIn("MISSION_CONTROL_VAPID_CONTACT", status["missingConfig"])

    def test_full_config_reports_ok(self) -> None:
        import os
        from unittest import mock

        os.environ["MISSION_CONTROL_VAPID_PRIVATE_KEY"] = _VAPID_PRIVATE
        os.environ["MISSION_CONTROL_VAPID_PUBLIC_KEY"] = _VAPID_PUBLIC
        os.environ["MISSION_CONTROL_VAPID_CONTACT"] = _VAPID_CONTACT
        # Simulate the optional packages being installed: this test asserts the
        # config path only. Without the mock, a bare install (no pywebpush /
        # websockets) correctly reports missing_dependency instead.
        with mock.patch.object(push_server, "_missing_push_dependencies", return_value=[]):
            status = push_server.push_status()
        self.assertTrue(status["enabled"])
        self.assertEqual(status["reason"], "ok")

    def test_send_push_disabled_without_contact(self) -> None:
        import os

        os.environ["MISSION_CONTROL_VAPID_PRIVATE_KEY"] = _VAPID_PRIVATE
        os.environ["MISSION_CONTROL_VAPID_PUBLIC_KEY"] = _VAPID_PUBLIC
        result = push_server.send_push("t", "b")
        self.assertTrue(result["disabled"])
        self.assertEqual(result["reason"], "vapid_not_configured")


class PushMissingDependencyTest(unittest.TestCase):
    """Simulate pywebpush/websockets being absent.

    We cannot uninstall real packages here, so we shadow the module finder
    and then restore it. This mirrors a bare Linux install where only the
    base requirements.txt was installed.
    """

    def setUp(self) -> None:
        import os

        self._env_backup = {key: os.environ.get(key) for key in _REQUIRED_ENV}
        os.environ["MISSION_CONTROL_VAPID_PRIVATE_KEY"] = _VAPID_PRIVATE
        os.environ["MISSION_CONTROL_VAPID_PUBLIC_KEY"] = _VAPID_PUBLIC
        os.environ["MISSION_CONTROL_VAPID_CONTACT"] = _VAPID_CONTACT

        self._original_find_spec = importlib.util.find_spec

        def find_spec_shadow(name, *args, **kwargs):
            if name in ("pywebpush", "websockets"):
                return None
            return self._original_find_spec(name, *args, **kwargs)

        importlib.util.find_spec = find_spec_shadow

    def tearDown(self) -> None:
        import os

        importlib.util.find_spec = self._original_find_spec
        for key, value in self._env_backup.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_status_reports_missing_dependency(self) -> None:
        status = push_server.push_status()
        self.assertFalse(status["enabled"])
        self.assertEqual(status["reason"], "missing_dependency")
        self.assertIn("pywebpush", status["missingDependencies"])
        self.assertIn("websockets", status["missingDependencies"])

    def test_send_push_disabled_with_reason(self) -> None:
        result = push_server.send_push("t", "b")
        self.assertTrue(result["disabled"])
        self.assertEqual(result["reason"], "missing_dependency")


class PushWatcherResilienceTest(unittest.TestCase):
    """A bare Linux install (no optional deps) must not crash on startup."""

    def test_start_gateway_watcher_tolerates_missing_deps(self) -> None:
        import os

        self._env_backup = {key: os.environ.get(key) for key in _REQUIRED_ENV}
        for key in _REQUIRED_ENV:
            os.environ.pop(key, None)
        try:
            # Should spawn threads and return without raising, even though
            # pywebpush/websockets may not be importable in this interpreter.
            push_server.start_gateway_watcher(interval=0.01)
        finally:
            for key, value in self._env_backup.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        # The watcher loop exits immediately when state.db is unavailable, so
        # no assertion on delivery is possible here; the point is no exception.
        self.assertTrue(True)


if __name__ == "__main__":
    unittest.main()
