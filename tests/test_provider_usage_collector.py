"""Tests for selected-provider CodexBar invocation and normalization."""

from __future__ import annotations

import json
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from provider_usage_collector import collect_codexbar_usage
from provider_usage_contract import normalize_codexbar_entry


class ProviderUsageCollectorTests(unittest.TestCase):
    def test_duplicate_account_payload_is_rejected_without_exposing_identity(self) -> None:
        payload = json.dumps([
            {"provider": "deepseek", "account": "fixture-one@example.invalid", "usage": {"primary": {"usedPercent": 1}}},
            {"provider": "deepseek", "account": "fixture-two@example.invalid", "usage": {"primary": {"usedPercent": 2}}},
            {"provider": "claude", "usage": {"primary": {"usedPercent": 3}}},
        ])
        completed = type("Completed", (), {"returncode": 0, "stdout": payload})()
        catalog = [{"provider": provider, "source": "codexbar"} for provider in ("deepseek", "claude")]
        with patch("provider_usage_collector.shutil.which", return_value="/test/codexbar"), \
             patch("provider_usage_collector.subprocess.run", return_value=completed):
            results = collect_codexbar_usage(("deepseek", "claude"), catalog)
        self.assertFalse(results[0]["available"])
        self.assertEqual(results[0]["dataState"], "error")
        self.assertEqual(results[0]["error"], "Multiple CodexBar accounts returned for provider.")
        self.assertEqual(results[0]["windows"], [])
        self.assertEqual(results[1]["windows"][0]["usedPercent"], 3)
        self.assertNotIn("example.invalid", repr(results))

    def test_serialized_cli_fixture_reaches_dynamic_collector_without_provider_mapping(self) -> None:
        path = Path(__file__).resolve().parent / "fixtures/provider-usage/codexbar-v0.70.0.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload[0]["provider"] = "future-provider"
        completed = type("Completed", (), {"returncode": 0, "stdout": json.dumps(payload)})()
        with patch("provider_usage_collector.shutil.which", return_value="/test/codexbar"), \
             patch("provider_usage_collector.subprocess.run", return_value=completed) as run:
            result = collect_codexbar_usage(("future-provider",), [{"provider": "future-provider", "source": "codexbar"}])[0]
        self.assertEqual(result["windows"][0]["label"], "API key spend cap")
        self.assertEqual(result["balances"][0]["value"], 88)
        self.assertTrue(any(row.get("kind") == "chart" for row in result["metrics"]))
        self.assertEqual(next(row["value"] for row in result["metrics"] if row["label"] == "Account spend"), "$12.00")
        self.assertEqual(run.call_args.kwargs["timeout"], 30)
        self.assertEqual(run.call_args.args[0][3], "future-provider")

    def test_selected_dynamic_ids_are_invoked_individually_and_normalized(self) -> None:
        payload = json.dumps([
            {
                "provider": "deepseek",
                "source": "oauth",
                "usage": {
                    "primary": {"usedPercent": 12, "windowMinutes": 300},
                    "privateField": "must-not-leak",
                },
            },
            {
                "provider": "ollama",
                "source": "web",
                "usage": {"primary": {"usedPercent": 25}},
            },
        ])
        completed = type("Completed", (), {"returncode": 0, "stdout": payload})()
        catalog = [
            {"provider": "deepseek", "source": "codexbar"},
            {"provider": "ollama", "source": "codexbar"},
            {"provider": "nous", "source": "mission-control"},
        ]

        with patch("provider_usage_collector.shutil.which", return_value="/test/codexbar"), \
             patch("provider_usage_collector.subprocess.run", return_value=completed) as run:
            results = collect_codexbar_usage(("deepseek", "ollama"), catalog)

        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["provider"], "deepseek")
        self.assertEqual(results[0]["windows"][0]["usedPercent"], 12)
        self.assertEqual(results[1]["provider"], "ollama")
        self.assertEqual(run.call_count, 2)
        calls = [call.args[0] for call in run.call_args_list]
        self.assertCountEqual(calls, [
            ["/test/codexbar", "usage", "--provider", "deepseek", "--json", "--no-color"],
            ["/test/codexbar", "usage", "--provider", "ollama", "--json", "--no-color"],
        ])
        self.assertNotIn("must-not-leak", repr(results))

    def test_unlisted_and_native_ids_never_reach_codexbar_argv(self) -> None:
        catalog = [{"provider": "deepseek", "source": "codexbar"}, {"provider": "nous", "source": "mission-control"}]
        with patch("provider_usage_collector.shutil.which", return_value="/test/codexbar"), \
             patch("provider_usage_collector.subprocess.run") as run:
            results = collect_codexbar_usage(("not-in-catalog", "nous"), catalog)

        run.assert_not_called()
        self.assertEqual([item["provider"] for item in results], ["not-in-catalog"])
        self.assertFalse(results[0]["available"])

    def test_one_provider_failure_does_not_abort_selected_batch(self) -> None:
        successful = type("Completed", (), {
            "returncode": 0,
            "stdout": json.dumps([{"provider": "deepseek", "usage": {"primary": {"usedPercent": 10}}}]),
        })()
        catalog = [
            {"provider": "deepseek", "source": "codexbar"},
            {"provider": "claude", "source": "codexbar"},
        ]
        def run_for_provider(arguments: list[str], **_kwargs: object) -> object:
            if arguments[3] == "deepseek":
                return successful
            raise OSError("private path")

        with patch("provider_usage_collector.shutil.which", return_value="/test/codexbar"), \
             patch("provider_usage_collector.subprocess.run", side_effect=run_for_provider):
            results = collect_codexbar_usage(("deepseek", "claude"), catalog)

        self.assertTrue(results[0]["available"])
        self.assertFalse(results[1]["available"])
        self.assertNotIn("private path", repr(results))

    def test_unexpected_worker_error_preserves_successful_sibling_result(self) -> None:
        successful = {"provider": "deepseek", "available": True, "windows": [], "balances": [], "metrics": []}
        catalog = [
            {"provider": "deepseek", "source": "codexbar"},
            {"provider": "claude", "source": "codexbar"},
        ]

        def collect(provider: str, _catalog_ids: set[str]) -> dict[str, object]:
            if provider == "deepseek":
                return successful
            raise UnicodeDecodeError("utf-8", b"\\xff", 0, 1, "invalid start byte")

        with patch("provider_usage_collector.collect_codexbar_provider", side_effect=collect):
            results = collect_codexbar_usage(("deepseek", "claude"), catalog)

        self.assertEqual(results[0], successful)
        self.assertFalse(results[1]["available"])
        self.assertNotIn("UnicodeDecodeError", repr(results))

    def test_selected_batch_uses_bounded_parallel_collection_and_preserves_order(self) -> None:
        providers = tuple(f"provider-{index}" for index in range(10))
        catalog = [{"provider": provider, "source": "codexbar"} for provider in providers]
        lock = threading.Lock()
        release = threading.Event()
        active = 0
        peak = 0

        def collect(provider: str, _catalog_ids: set[str]) -> dict[str, object]:
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
                if peak == 5:
                    release.set()
            if not release.wait(timeout=1):
                release.set()
            with lock:
                active -= 1
            return {"provider": provider, "available": False}

        with patch("provider_usage_collector.collect_codexbar_provider", side_effect=collect):
            results = collect_codexbar_usage(providers, catalog)

        self.assertEqual(peak, 5)
        self.assertEqual([item["provider"] for item in results], list(providers))

    def test_raw_error_source_timestamp_unit_and_pace_are_not_echoed(self) -> None:
        payload = [{
            "provider": "deepseek",
            "source": "private-source-marker",
            "error": {"message": "private-error-marker"},
            "usage": {
                "updatedAt": "not-a-timestamp",
                "primary": {"usedPercent": 10, "unit": "private-unit-marker"},
                "pace": {"secret": "private-pace-marker"},
            },
        }]

        result = normalize_codexbar_entry("deepseek", payload)

        self.assertFalse(result["available"])
        self.assertEqual(result["source"], "cli")
        self.assertEqual(result["error"], "CodexBar returned a provider error.")
        self.assertNotIn("private-", repr(result))

    def test_generic_details_map_numeric_rows_without_exposing_identity_or_raw_fields(self) -> None:
        payload = [{
            "provider": "deepseek",
            "source": "openai-web",
            "usage": {
                "primary": {"usedPercent": 41},
                "rateWindowLabels": {"primary": "Five-hour quota"},
                "identity": {"accountEmail": "fixture@example.invalid"},
                "details": [{
                    "title": "Account details",
                    "rows": [
                        {"label": "Fast requests", "value": 12},
                        {"label": "Account email", "value": "fixture@example.invalid"},
                        {"label": "Authorization: Bearer private-token-marker", "value": 2},
                        {"label": "Premium active", "value": True},
                    ],
                    "bars": [{"secret": "private-chart-marker"}],
                }],
            },
        }]

        result = normalize_codexbar_entry("deepseek", payload)

        self.assertEqual(result["source"], "openai-web")
        self.assertEqual(result["windows"][0]["label"], "Five-hour quota")
        self.assertEqual([metric["value"] for metric in result["metrics"]], [12, True])
        self.assertNotIn("fixture@example.invalid", repr(result))
        self.assertNotIn("private-token-marker", repr(result))
        self.assertNotIn("private-chart-marker", repr(result))


if __name__ == "__main__":
    unittest.main()
