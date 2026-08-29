"""Tests for local provider-usage visibility configuration."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from provider_usage_config import visible_usage_providers


class ProviderUsageConfigTests(unittest.TestCase):
    def setUp(self):
        self._backup = os.environ.get("MISSION_CONTROL_USAGE_PROVIDERS")

    def tearDown(self):
        if self._backup is None:
            os.environ.pop("MISSION_CONTROL_USAGE_PROVIDERS", None)
        else:
            os.environ["MISSION_CONTROL_USAGE_PROVIDERS"] = self._backup

    def test_default_keeps_all_builtin_providers_visible(self):
        os.environ.pop("MISSION_CONTROL_USAGE_PROVIDERS", None)
        self.assertEqual(visible_usage_providers(), ("codex", "ollama", "openrouter", "nous"))

    def test_local_allowlist_hides_openrouter_and_deduplicates(self):
        os.environ["MISSION_CONTROL_USAGE_PROVIDERS"] = "codex, ollama, nous, codex"
        self.assertEqual(visible_usage_providers(), ("codex", "ollama", "nous"))

    def test_unknown_provider_ids_are_ignored(self):
        os.environ["MISSION_CONTROL_USAGE_PROVIDERS"] = "codex,not-a-provider,nous"
        self.assertEqual(visible_usage_providers(), ("codex", "nous"))


if __name__ == "__main__":
    unittest.main()
