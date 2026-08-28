"""Tests for telemetry bind address/port resolution.

Covers the canonical MISSION_CONTROL_LOCAL_TELEMETRY_* names, the legacy
TELEMETRY_BIND_* aliases, precedence between the two, and invalid-port
startup errors (see issue #11).
"""

import os
import sys
import types
import unittest
import importlib.util
from pathlib import Path

fake_psutil = types.SimpleNamespace(
    cpu_percent=lambda interval=None: 7.5,
    virtual_memory=lambda: types.SimpleNamespace(percent=42.0, used=8 * 1024**3, available=8 * 1024**3, total=16 * 1024**3),
    disk_usage=lambda path: types.SimpleNamespace(percent=55.0, free=100 * 1024**3, total=200 * 1024**3),
    Process=lambda: types.SimpleNamespace(memory_info=lambda: types.SimpleNamespace(rss=256 * 1024**2)),
)
sys.modules.setdefault("psutil", fake_psutil)

MODULE_PATH = Path(__file__).resolve().parents[1] / "server" / "local_telemetry_server.py"
SPEC = importlib.util.spec_from_file_location("mission_control_local_telemetry_bind", MODULE_PATH)
local_telemetry_server = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(local_telemetry_server)

_BIND_VARS = (
    "MISSION_CONTROL_LOCAL_TELEMETRY_HOST",
    "MISSION_CONTROL_LOCAL_TELEMETRY_PORT",
    "TELEMETRY_BIND_HOST",
    "TELEMETRY_BIND_PORT",
)


class TelemetryBindResolutionTests(unittest.TestCase):
    def setUp(self):
        self._env_backup = {key: os.environ.get(key) for key in _BIND_VARS}
        for key in _BIND_VARS:
            os.environ.pop(key, None)

    def tearDown(self):
        for key, value in self._env_backup.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_defaults_when_unset(self):
        self.assertEqual(
            local_telemetry_server._resolve_telemetry_bind(), ("127.0.0.1", 8765)
        )

    def test_canonical_names_are_applied(self):
        os.environ["MISSION_CONTROL_LOCAL_TELEMETRY_HOST"] = "127.0.0.9"
        os.environ["MISSION_CONTROL_LOCAL_TELEMETRY_PORT"] = "9999"
        self.assertEqual(
            local_telemetry_server._resolve_telemetry_bind(), ("127.0.0.9", 9999)
        )

    def test_legacy_aliases_are_applied(self):
        os.environ["TELEMETRY_BIND_HOST"] = "10.0.0.5"
        os.environ["TELEMETRY_BIND_PORT"] = "7777"
        self.assertEqual(
            local_telemetry_server._resolve_telemetry_bind(), ("10.0.0.5", 7777)
        )

    def test_canonical_wins_over_legacy_alias(self):
        os.environ["MISSION_CONTROL_LOCAL_TELEMETRY_HOST"] = "127.0.0.1"
        os.environ["MISSION_CONTROL_LOCAL_TELEMETRY_PORT"] = "8765"
        os.environ["TELEMETRY_BIND_HOST"] = "0.0.0.0"
        os.environ["TELEMETRY_BIND_PORT"] = "9999"
        self.assertEqual(
            local_telemetry_server._resolve_telemetry_bind(), ("127.0.0.1", 8765)
        )

    def test_non_integer_port_aborts_startup(self):
        os.environ["MISSION_CONTROL_LOCAL_TELEMETRY_PORT"] = "not-a-port"
        with self.assertRaises(SystemExit) as exc:
            local_telemetry_server._resolve_telemetry_bind()
        self.assertIn("invalid telemetry port", str(exc.exception))

    def test_out_of_range_port_aborts_startup(self):
        os.environ["TELEMETRY_BIND_PORT"] = "70000"
        with self.assertRaises(SystemExit) as exc:
            local_telemetry_server._resolve_telemetry_bind()
        self.assertIn("invalid telemetry port", str(exc.exception))

    def test_zero_port_aborts_startup(self):
        os.environ["MISSION_CONTROL_LOCAL_TELEMETRY_PORT"] = "0"
        with self.assertRaises(SystemExit) as exc:
            local_telemetry_server._resolve_telemetry_bind()
        self.assertIn("invalid telemetry port", str(exc.exception))


if __name__ == "__main__":
    unittest.main()
