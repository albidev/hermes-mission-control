"""Tests for local provider-usage visibility configuration."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from provider_usage_config import apply_provider_display_config, visible_usage_providers


class ProviderUsageConfigTests(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="mc-provider-config-"))
        self._hermes_home_backup = os.environ.get("HERMES_HOME")
        self._backup = os.environ.get("MISSION_CONTROL_USAGE_PROVIDERS")
        os.environ["HERMES_HOME"] = str(self._tmp / "hermes")
        (self._tmp / "hermes").mkdir()

    def tearDown(self):
        if self._hermes_home_backup is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = self._hermes_home_backup
        if self._backup is None:
            os.environ.pop("MISSION_CONTROL_USAGE_PROVIDERS", None)
        else:
            os.environ["MISSION_CONTROL_USAGE_PROVIDERS"] = self._backup
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_default_keeps_all_builtin_providers_visible(self):
        os.environ.pop("MISSION_CONTROL_USAGE_PROVIDERS", None)
        self.assertEqual(visible_usage_providers(), ("codex", "ollama", "openrouter", "nous"))

    def test_local_allowlist_hides_openrouter_and_deduplicates(self):
        os.environ["MISSION_CONTROL_USAGE_PROVIDERS"] = "codex, ollama, nous, codex"
        self.assertEqual(visible_usage_providers(), ("codex", "ollama", "nous"))

    def test_display_config_hides_codex_credits_and_features_reset_count(self):
        config_path = self._tmp / "hermes" / "mission-control-usage.json"
        config_path.write_text(
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
        entry = {
            "provider": "codex",
            "available": True,
            "windows": [{"id": "primary"}],
            "balances": [
                {"id": "credits_remaining", "value": 0},
                {"id": "other_balance", "value": 5},
            ],
            "metrics": [
                {"id": "reset_credits_available", "value": 1},
                {"id": "other_metric", "value": 2},
            ],
        }

        result = apply_provider_display_config(entry)

        self.assertEqual([item["id"] for item in result["balances"]], ["other_balance"])
        self.assertTrue(result["metrics"][0]["featured"])
        self.assertEqual(result["metrics"][0]["id"], "reset_credits_available")
        self.assertFalse(result["metrics"][1].get("featured", False))

    def test_unknown_provider_ids_are_ignored(self):
        os.environ["MISSION_CONTROL_USAGE_PROVIDERS"] = "codex,not-a-provider,nous"
        self.assertEqual(visible_usage_providers(), ("codex", "nous"))


if __name__ == "__main__":
    unittest.main()
