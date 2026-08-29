"""Regression tests for profile-aware Hermes paths in telemetry."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import candidates as candidates_module  # noqa: E402

fake_psutil = types.SimpleNamespace(
    cpu_percent=lambda interval=None: 7.5,
    virtual_memory=lambda: types.SimpleNamespace(percent=42.0, used=8 * 1024**3, available=8 * 1024**3, total=16 * 1024**3),
    disk_usage=lambda path: types.SimpleNamespace(percent=55.0, free=100 * 1024**3, total=200 * 1024**3),
    Process=lambda: types.SimpleNamespace(memory_info=lambda: types.SimpleNamespace(rss=256 * 1024**2)),
)
sys.modules.setdefault("psutil", fake_psutil)

MODULE_PATH = Path(__file__).resolve().parents[1] / "server" / "local_telemetry_server.py"
SPEC = importlib.util.spec_from_file_location("mission_control_telemetry_paths", MODULE_PATH)
local_telemetry_server = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(local_telemetry_server)


class TelemetryPathResolutionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="mc-telemetry-paths-"))
        self._home_backup = os.environ.get("HOME")
        self._hermes_home_backup = os.environ.get("HERMES_HOME")
        self._vault_backup = os.environ.get("MISSION_CONTROL_VAULT_PATH")
        self._usage_providers_backup = os.environ.get("MISSION_CONTROL_USAGE_PROVIDERS")
        os.environ["HOME"] = str(self._tmp / "home")
        os.environ["HERMES_HOME"] = str(self._tmp / "hermes")
        os.environ.pop("MISSION_CONTROL_VAULT_PATH", None)
        self._hermes_home = Path(os.environ["HERMES_HOME"])

    def tearDown(self):
        if self._home_backup is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self._home_backup
        if self._hermes_home_backup is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = self._hermes_home_backup
        if self._vault_backup is None:
            os.environ.pop("MISSION_CONTROL_VAULT_PATH", None)
        else:
            os.environ["MISSION_CONTROL_VAULT_PATH"] = self._vault_backup
        if self._usage_providers_backup is None:
            os.environ.pop("MISSION_CONTROL_USAGE_PROVIDERS", None)
        else:
            os.environ["MISSION_CONTROL_USAGE_PROVIDERS"] = self._usage_providers_backup
        import shutil

        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_provider_usage_reads_profile_aware_cache(self):
        cache = self._hermes_home / "cache" / "mission-control-provider-usage.json"
        cache.parent.mkdir(parents=True)
        payload = {"success": True, "available": True, "providers": [{"provider": "codex"}]}
        cache.write_text(json.dumps(payload), encoding="utf-8")

        with patch.object(
            local_telemetry_server,
            "collect_nous_portal_usage",
            return_value={
                "provider": "nous",
                "available": False,
                "source": "portal-account",
                "windows": [],
                "balances": [],
                "metrics": [],
            },
        ):
            result = local_telemetry_server.collect_provider_usage()

        self.assertEqual(result["schemaVersion"], 1)
        self.assertTrue(result["available"])
        self.assertEqual(result["providers"][0]["provider"], "codex")
        self.assertEqual(result["providers"][0]["windows"], [])
        self.assertEqual(result["providers"][-1]["provider"], "nous")

    def test_local_allowlist_filters_hidden_provider_from_cache_and_fetches(self):
        cache = self._hermes_home / "cache" / "mission-control-provider-usage.json"
        cache.parent.mkdir(parents=True)
        cache.write_text(
            json.dumps({
                "success": True,
                "available": True,
                "providers": [
                    {"provider": "codex", "available": True, "windows": [], "balances": [], "metrics": []},
                    {"provider": "openrouter", "available": True, "windows": [], "balances": [], "metrics": []},
                ],
            }),
            encoding="utf-8",
        )
        os.environ["MISSION_CONTROL_USAGE_PROVIDERS"] = "codex,nous"
        nous = {"provider": "nous", "available": True, "windows": [], "balances": [], "metrics": []}

        with patch.object(local_telemetry_server, "collect_nous_portal_usage", return_value=nous), \
             patch.object(local_telemetry_server.subprocess, "run") as run:
            result = local_telemetry_server.collect_provider_usage()

        self.assertEqual([item["provider"] for item in result["providers"]], ["codex", "nous"])
        run.assert_not_called()

    def test_local_display_rules_filter_codex_balance_and_feature_reset_metric(self):
        config = self._hermes_home / "mission-control-usage.json"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(
            json.dumps({
                "providers": {
                    "codex": {
                        "hidden": {"balances": ["credits_remaining"]},
                        "featured": {"metrics": ["reset_credits_available"]},
                    }
                }
            }),
            encoding="utf-8",
        )
        cache = self._hermes_home / "cache" / "mission-control-provider-usage.json"
        cache.parent.mkdir(parents=True)
        cache.write_text(
            json.dumps({
                "schemaVersion": 1,
                "available": True,
                "providers": [{
                    "provider": "codex",
                    "available": True,
                    "windows": [],
                    "balances": [{"id": "credits_remaining", "value": 0, "unit": "credits"}],
                    "metrics": [{"id": "reset_credits_available", "value": 1, "unit": "count"}],
                }],
            }),
            encoding="utf-8",
        )
        os.environ["MISSION_CONTROL_USAGE_PROVIDERS"] = "codex"

        result = local_telemetry_server.collect_provider_usage()
        provider = result["providers"][0]

        self.assertEqual(provider["balances"], [])
        self.assertEqual(provider["metrics"][0]["id"], "reset_credits_available")
        self.assertTrue(provider["metrics"][0]["featured"])

    def test_runtime_home_follows_central_resolver(self):
        self.assertEqual(local_telemetry_server._get_hermes_home(), self._hermes_home)
        self.assertEqual(local_telemetry_server._knowledge_core_root(), self._hermes_home.resolve())
        self.assertEqual(
            local_telemetry_server._client_diagnostics_log(),
            self._hermes_home / "logs" / "mission-control-client.log",
        )

    def test_candidate_vault_follows_configured_vault_path(self):
        vault = self._tmp / "vault"
        os.environ["MISSION_CONTROL_VAULT_PATH"] = str(vault)

        self.assertEqual(candidates_module.vault_dir_for("core"), vault.resolve())


if __name__ == "__main__":
    unittest.main()
