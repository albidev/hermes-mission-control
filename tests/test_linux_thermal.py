"""Tests for the Linux thermal telemetry backend (issue #15).

Covers the sysfs (/sys/class/thermal) filesystem-fixture path and the
lm-sensors fallback, plus the structured ``unavailable`` state that must
be returned when the host has no usable thermal sensor.

The macOS powermetrics branch is intentionally left untouched; these tests
exercise only the platform-independent helpers and the Linux branch, which
is selected by ``platform.system() == "Linux"`` at runtime.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

# ---------------------------------------------------------------------------
# Fake psutil so the module imports without a real psutil dependency.
# ---------------------------------------------------------------------------
fake_psutil = types.SimpleNamespace(
    cpu_percent=lambda interval=None: 7.5,
    virtual_memory=lambda: types.SimpleNamespace(percent=42.0, used=8 * 1024**3, available=8 * 1024**3, total=16 * 1024**3),
    disk_usage=lambda path: types.SimpleNamespace(percent=55.0, free=100 * 1024**3, total=200 * 1024**3),
    Process=lambda: types.SimpleNamespace(memory_info=lambda: types.SimpleNamespace(rss=256 * 1024**2)),
)
sys.modules.setdefault("psutil", fake_psutil)

MODULE_PATH = Path(__file__).resolve().parents[1] / "server" / "local_telemetry_server.py"
SPEC = importlib.util.spec_from_file_location("mission_control_thermal_server", MODULE_PATH)
local_telemetry_server = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(local_telemetry_server)


class LinuxThermalSysfsTests(unittest.TestCase):
    """Filesystem fixtures under a fake /sys/class/thermal."""

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="mc-thermal-sysfs-"))
        self._zones = self._tmp / "thermal"
        self._zones.mkdir()

        self._original_dir = local_telemetry_server.Path
        self._original_run = local_telemetry_server.subprocess.run

        # Point Path("/sys/class/thermal") at our fixture tree.
        def _patched_path(*args):
            if args and str(args[0]) == "/sys/class/thermal":
                return Path(self._zones)
            return Path(*args)

        local_telemetry_server.Path = _patched_path
        # No lm-sensors binary in this environment; sysfs must win.
        def _no_sensors(*args, **kwargs):
            raise FileNotFoundError("sensors")

        local_telemetry_server.subprocess.run = _no_sensors

    def tearDown(self):
        local_telemetry_server.Path = self._original_dir
        local_telemetry_server.subprocess.run = self._original_run

    def _write_zone(self, name: str, zone_type: str, temp_milli: int) -> Path:
        zone = self._zones / name
        zone.mkdir()
        (zone / "type").write_text(zone_type, encoding="utf-8")
        (zone / "temp").write_text(str(temp_milli), encoding="utf-8")
        return zone

    def test_reads_sysfs_thermal_zones(self):
        self._write_zone("thermal_zone0", "x86_pkg_temp", 58000)
        self._write_zone("thermal_zone1", "cpu-thermal", 52000)

        result = local_telemetry_server._collect_linux_thermal_snapshot()

        self.assertEqual(result["source"], "sysfs-thermal")
        # Lowest zone temperature is reported (52.0°C), not the average.
        self.assertEqual(result["thermalPressure"], 52.0)
        self.assertIsNone(result["thermalLevel"])
        self.assertIsNone(result["error"])

    def test_skips_malformed_or_zero_zones(self):
        # Malformed temp value (non-numeric).
        bad = self._zones / "thermal_zone0"
        bad.mkdir()
        (bad / "type").write_text("broken", encoding="utf-8")
        (bad / "temp").write_text("not-a-number", encoding="utf-8")
        # Zone with a temp of 0 (some platforms report 0 for inactive zones).
        self._write_zone("thermal_zone1", "acpitz", 0)
        # Valid zone still yields a value.
        self._write_zone("thermal_zone2", "x86_pkg_temp", 61000)

        result = local_telemetry_server._collect_linux_thermal_snapshot()

        self.assertEqual(result["source"], "sysfs-thermal")
        self.assertEqual(result["thermalPressure"], 61.0)
        self.assertIsNone(result["error"])

    def test_missing_temp_file_is_skipped(self):
        zone = self._zones / "thermal_zone0"
        zone.mkdir()
        (zone / "type").write_text("x86_pkg_temp", encoding="utf-8")
        # No 'temp' file at all.
        self._write_zone("thermal_zone1", "x86_pkg_temp", 47000)

        result = local_telemetry_server._collect_linux_thermal_snapshot()

        self.assertEqual(result["source"], "sysfs-thermal")
        self.assertEqual(result["thermalPressure"], 47.0)

    def test_empty_sysfs_falls_back_to_lm_sensors(self):
        # No thermal_zone* entries; sensors binary is available and returns data.
        def _fake_sensors(*args, **kwargs):
            return types.SimpleNamespace(
                returncode=0,
                stdout="coretemp-isa-0000\nAdapter: ISA adapter\n  Package id 0:\n    temp1_input: 54.000\n",
                stderr="",
            )

        local_telemetry_server.subprocess.run = _fake_sensors

        result = local_telemetry_server._collect_linux_thermal_snapshot()

        self.assertEqual(result["source"], "lm-sensors")
        self.assertEqual(result["thermalPressure"], 54.0)
        self.assertIsNone(result["error"])

    def test_no_sensors_returns_unavailable(self):
        # Empty sysfs directory AND no sensors binary → structured unavailable.
        result = local_telemetry_server._collect_linux_thermal_snapshot()

        self.assertEqual(result["source"], "unavailable")
        self.assertIsNone(result["thermalPressure"])
        self.assertIsNone(result["thermalLevel"])
        self.assertIsNotNone(result["error"])

    def test_sensors_failure_returns_unavailable(self):
        def _failing_sensors(*args, **kwargs):
            return types.SimpleNamespace(returncode=1, stdout="", stderr="sensors: no sensors found")

        local_telemetry_server.subprocess.run = _failing_sensors

        result = local_telemetry_server._collect_linux_thermal_snapshot()

        self.assertEqual(result["source"], "unavailable")
        self.assertIn("no sensors found", result["error"])

    def test_sensors_with_no_temp_inputs_returns_unavailable(self):
        def _empty_sensors(*args, **kwargs):
            return types.SimpleNamespace(returncode=0, stdout="acpitz-acpi-0\nAdapter: ACPI interface\n", stderr="")

        local_telemetry_server.subprocess.run = _empty_sensors

        result = local_telemetry_server._collect_linux_thermal_snapshot()

        self.assertEqual(result["source"], "unavailable")
        self.assertIsNotNone(result["error"])


class ThermalUnavailablePayloadTests(unittest.TestCase):
    """The structured unavailable payload shape shared by every backend."""

    def test_unavailable_payload_shape(self):
        payload = local_telemetry_server._thermal_unavailable("boom")

        self.assertEqual(
            set(payload.keys()),
            {"fanRpm", "fanCount", "thermalPressure", "thermalLevel", "levelSource", "source", "error"},
        )
        self.assertEqual(payload["source"], "unavailable")
        self.assertEqual(payload["error"], "boom")
        self.assertIsNone(payload["thermalPressure"])
        self.assertIsNone(payload["thermalLevel"])


if __name__ == "__main__":
    unittest.main()
