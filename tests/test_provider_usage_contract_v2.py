"""Behavior contracts for the common CodexBar CLI and compatible MC cache."""

from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from provider_usage_contract import normalize_cached_entry, normalize_codexbar_entry


class GenericContractTests(unittest.TestCase):
    def payload(self):
        path = ROOT / "tests/fixtures/provider-usage/codexbar-v0.70.0.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def normalize(self, usage, **extra):
        return normalize_codexbar_entry("future-provider", [{
            "provider": "future-provider", "source": "api", "usage": usage, **extra,
        }])

    def test_cached_semantic_roles_are_optional_validated_metadata(self):
        roles = ("quota", "spend_limit", "limit_remaining", "account_balance", "spendable_balance",
                 "balance_component", "credits", "spend_today", "spend_month", "spend",
                 "paid_access", "reset_credits", "diagnostic")
        for group in ("windows", "balances", "metrics"):
            for role in roles:
                with self.subTest(group=group, role=role):
                    entry = {"provider": "future-provider", "available": True,
                             "windows": [], "balances": [], "metrics": []}
                    field = {"id": "custom", "label": "Unchanged", "role": role,
                             **({"usedPercent": 7} if group == "windows" else {"value": 7})}
                    entry[group] = [field]
                    self.assertEqual(normalize_cached_entry(entry)[group], [field])
            for invalid in (None, "unknown", "QUOTA", 7, True, [], {}):
                with self.subTest(group=group, invalid=invalid):
                    field["role"] = invalid
                    normalized = normalize_cached_entry(entry)
                    self.assertEqual(normalized[group], [{key: value for key, value in field.items() if key != "role"}])
                    self.assertTrue(normalized["available"])
                    self.assertEqual(normalized["dataState"], "ready")
                    self.assertIn("invalid_field", normalized.get("warnings", []))
            field.pop("role")
            self.assertEqual(normalize_cached_entry(entry)[group], [field])

    def test_long_source_ids_and_duplicate_ids_survive_cache_roundtrip(self):
        source_id = "x" * 160
        usage = {
            "extraRateWindows": [{"id": source_id, "title": "Long quota", "window": {"usedPercent": 7}}] * 2,
            "details": [{"title": "Counters", "rows": [{"id": source_id, "label": "Long counter", "value": 8}] * 2}],
        }
        normalized = self.normalize(usage)
        cached = normalize_cached_entry(normalized)
        for group in ("windows", "metrics"):
            self.assertEqual(len(cached[group]), 2)
            self.assertEqual(cached[group], normalized[group])
            self.assertEqual(len({field["id"] for field in cached[group]}), 2)
            self.assertTrue(all(len(field["id"]) <= 160 for field in cached[group]))
        self.assertEqual(normalize_cached_entry(cached), cached)
        self.assertEqual(self.normalize(usage), normalized)

    def test_common_generated_fields_have_roles_without_provider_dispatch(self):
        usage = {"primary": {"usedPercent": 7}, "secondary": {"usedPercent": 12},
                 "tertiary": {"usedPercent": 3},
                 "extraRateWindows": [{"id": "daily", "title": "Calls", "window": {"remaining": 2}}],
                 "providerCost": {"used": 2, "personalUsed": 1, "nextRegenAmount": 4,
                                  "limit": 10, "balance": 8, "currencyCode": "USD"},
                 "codexResetCredits": {"availableCount": 0}}
        expected = {"primary": "quota", "secondary": "quota", "tertiary": "quota", "extra:daily": "quota",
                    "cost_budget": "spend_limit", "balance": "account_balance", "credits_remaining": "credits",
                    "cost_used": "spend", "cost_personal_used": "spend", "cost_next_regen": "diagnostic",
                    "reset_credits_available": "reset_credits"}
        for provider in ("openrouter", "future-provider"):
            normalized = normalize_codexbar_entry(provider, [{"provider": provider, "usage": usage,
                                                             "credits": {"remaining": 5}}])
            roles = {field["id"]: field.get("role") for group in ("windows", "balances", "metrics")
                     for field in normalized[group]}
            self.assertEqual(roles, expected)
            self.assertEqual(normalize_cached_entry(normalized), normalized)

    def test_codex_plan_observation_is_normalized_without_partial_data_warning(self):
        usage = {
            "secondary": {"usedPercent": 6, "windowMinutes": 10080},
            "planObservation": {"rawPlan": "prolite", "source": "usage_response", "observedAt": "2026-11-07T09:49:51Z"},
        }
        result = normalize_codexbar_entry("codex", [{"provider": "codex", "source": "oauth", "usage": usage}])
        self.assertEqual(result.get("plan"), "prolite")
        self.assertNotIn("unsupported_extension", result.get("warnings", []))
        self.assertEqual(result["windows"][0]["usedPercent"], 6)

    def test_detail_roles_use_exact_shared_vocabulary_not_formatted_money(self):
        vocabulary = {
            "API key": {"API key limit": "spend_limit", "API key remaining": "limit_remaining",
                        "API key used": "diagnostic", "Today": "spend_today", "This month": "spend_month",
                        "This week": "diagnostic"},
            "Credits": {"Remaining": "account_balance", "Used": "diagnostic", "Total added": "diagnostic"},
        }
        details = [{"title": section, "rows": [{"label": label, "value": "$12.34"} for label in rows]}
                   for section, rows in vocabulary.items()]
        details.extend([
            {"title": "Billing", "rows": [{"label": "Today", "value": "$99.00"}]},
            {"title": "API key", "rows": [{"label": "Today extra", "value": "$99.00"},
                                           {"label": "today", "value": "$99.00"}]},
            {"title": "Credits", "rows": [{"label": "Remaining balance", "value": "$99.00"}],
             "chart": {"kind": "bars", "title": "Remaining", "points": [{"label": "Day", "value": 1}]}},
        ])
        result = self.normalize({"details": details})
        for field in result["metrics"]:
            if field.get("kind") == "chart":
                self.assertEqual(field.get("role"), "diagnostic")
            else:
                expected = vocabulary.get(field.get("sectionLabel"), {}).get(field["label"])
                self.assertEqual(field.get("role"), expected)
                self.assertEqual(field["value"], "$12.34" if expected else "$99.00")
                self.assertNotIn("usageValue", field)
                self.assertNotIn("currency", field)
        self.assertEqual(normalize_cached_entry(result), result)
        old = copy.deepcopy(result)
        for field in old["metrics"]:
            field.pop("role", None)
        cached = normalize_cached_entry(old)
        self.assertEqual([field["id"] for field in cached["metrics"]], [field["id"] for field in result["metrics"]])
        self.assertEqual([field["legacyIds"] for field in cached["metrics"] if "legacyIds" in field],
                         [field["legacyIds"] for field in result["metrics"] if "legacyIds" in field])

    def test_empty_usage_is_no_data(self):
        result = self.normalize({})
        self.assertFalse(result["available"])
        self.assertEqual(result["dataState"], "no_data")
        self.assertIsNone(result["updatedAt"])
        for usage in (None, [], "invalid"):
            with self.subTest(usage=usage):
                invalid = self.normalize(usage)
                self.assertFalse(invalid["available"])
                self.assertEqual(invalid["dataState"], "error")
        ready = self.normalize({"primary": {"usedPercent": 0}})
        self.assertEqual(ready["dataState"], "ready")
        self.assertTrue(ready["available"])

    def test_common_windows_preserve_real_quota_semantics(self):
        result = normalize_codexbar_entry("openrouter", self.payload())
        windows = {item["id"]: item for item in result["windows"]}
        self.assertIn("extra:daily", windows)
        self.assertEqual(windows["extra:daily"]["label"], "Daily requests")
        self.assertEqual(windows["extra:daily"]["usedPercent"], 125)
        unknown = windows["extra:unknown"]
        self.assertFalse(unknown["usageKnown"])
        self.assertNotIn("usedPercent", unknown)
        self.assertEqual(unknown["resetDescription"], "Tomorrow")
        self.assertEqual(unknown["nextRegenPercent"], 10)
        self.assertNotIn("extra:placeholder", windows)
        self.assertIn("synthetic_placeholder", result["warnings"])
        neutral = self.normalize({"primary": {"usedPercent": 0}})
        self.assertEqual(neutral["windows"][0]["label"], "Primary quota")
        for value in (None, True, float("nan"), float("inf"), "12%"):
            with self.subTest(value=value):
                result = self.normalize({"primary": {"usedPercent": value}, "secondary": {"usedPercent": 5}})
                self.assertEqual([item["id"] for item in result["windows"]], ["secondary"])
        result = self.normalize({"extraRateWindows": [
            {"id": "daily", "title": "Daily", "window": {"usedPercent": 0}},
            {"id": "daily", "title": "Daily", "window": {"usedPercent": 1}},
            None,
            {"id": "bad@example.invalid", "title": "Bad", "window": {"usedPercent": 1}},
        ]})
        self.assertEqual([item["id"] for item in result["windows"]], ["extra:daily", "extra:daily:2"])
        self.assertIn("invalid_field", result["warnings"])

    def test_cost_is_structured_separately_from_account_balance(self):
        cost = {"used": 125, "limit": 100, "currencyCode": "EUR", "period": "Monthly",
                "balance": -3, "balanceIsWorkspace": True,
                "balanceUpdatedAt": "2026-10-05T00:00:00Z", "updatedAt": "2026-10-06T00:00:00Z",
                "resetsAt": "2026-11-01T00:00:00Z", "personalUsed": 0, "nextRegenAmount": 5}
        result = self.normalize({"providerCost": cost})
        self.assertEqual(len(result["windows"]), 1)
        budget = result["windows"][0]
        self.assertEqual((budget["id"], budget["usedPercent"], budget["remaining"]), ("cost_budget", 125, -25))
        self.assertEqual(budget["unit"], "EUR")
        self.assertEqual(budget["updatedAt"], cost["updatedAt"])
        balance = result["balances"][0]
        self.assertEqual((balance["id"], balance["value"], balance["currency"]), ("balance", -3, "EUR"))
        self.assertEqual(balance["scope"], "workspace")
        self.assertEqual(balance["updatedAt"], cost["balanceUpdatedAt"])
        metrics = {item["id"]: item for item in result["metrics"]}
        self.assertEqual(metrics["cost_personal_used"]["value"], 0)
        self.assertEqual(metrics["cost_next_regen"]["value"], 5)
        self.assertEqual(metrics["cost_used"]["value"], 125)
        self.assertEqual(metrics["cost_used"]["currency"], "EUR")
        for limit in (None, 0, -1, True, float("nan")):
            with self.subTest(limit=limit):
                cost["limit"] = limit
                uncapped = self.normalize({"providerCost": cost})
                self.assertEqual(uncapped["windows"], [])
                self.assertTrue(uncapped["available"])
        cost.update(currencyCode="unknown", balanceIsUnavailable=True)
        unknown = self.normalize({"providerCost": cost})
        self.assertEqual(unknown["balances"], [])
        self.assertNotIn("currency", unknown["metrics"][0])
        self.assertNotIn("USD", repr(unknown))
        self.assertIn("unknown_currency", unknown["warnings"])
        self.assertIn("balance_unavailable", unknown["warnings"])

    def test_string_details_preserve_structured_metadata_and_stable_identity(self):
        result = normalize_codexbar_entry("openrouter", self.payload())
        rows = {item["label"]: item for item in result["metrics"]}
        self.assertIn("Account spend", rows)
        spend = rows["Account spend"]
        self.assertEqual(spend["value"], "$12.00")
        self.assertEqual(spend["id"], "detail:account-spend")
        self.assertEqual(spend["secondaryValue"], "This month")
        self.assertEqual(spend["usageValue"], 12)
        self.assertEqual(spend["sectionLabel"], "Billing")
        self.assertEqual(spend["legacyIds"], ["detail-0-0"])
        self.assertEqual(rows["Requests"]["progress"], {"used": 12, "total": 100})
        chart = next(item for item in result["metrics"] if item.get("kind") == "chart")
        self.assertEqual(chart["chart"]["points"], [{"label": "Mon", "value": 2}, {"label": "Tue", "value": 10}])
        self.assertEqual(chart["chart"]["kind"], "bars")
        self.assertEqual(chart["chart"]["unit"], "requests")
        details = [{"title": "Billing", "rows": [
            {"label": "Spend", "value": "12,34 €", "progress": {"used": 125, "total": 100}},
            {"label": "Spend", "value": "Different"},
            {"id": "given", "label": "Calls", "value": "7"},
        ]}]
        first = self.normalize({"details": details})["metrics"]
        self.assertEqual(first[0]["value"], "12,34 €")
        self.assertEqual(first[0]["progress"]["used"], 125)
        self.assertEqual(first[1]["id"], first[0]["id"] + ":2")
        details[0]["rows"][0]["value"] = "99,99 €"
        details[0]["rows"] = list(reversed(details[0]["rows"]))
        second = self.normalize({"details": details})["metrics"]
        self.assertEqual({row["id"] for row in first}, {row["id"] for row in second})
        self.assertEqual(next(row for row in second if row["label"] == "Calls")["id"], "detail:given")
        for progress in ({"used": 1, "total": 0}, {"used": True, "total": 1}, 0.5):
            details[0]["rows"] = [{"label": "Good", "value": "Text", "progress": progress}]
            row = self.normalize({"details": details})["metrics"][0]
            self.assertNotIn("progress", row)
            self.assertEqual(row["value"], "Text")

    def test_detail_and_window_bounds_preserve_complete_valid_payloads(self):
        sections = [{"title": f"Section {section}", "rows": [
            {"label": f"Metric {row}", "value": "x" * 120, "secondaryValue": "s" * 120,
             "progress": {"used": row, "total": 24}, "usageValue": row}
            for row in range(24)
        ], "chart": {"kind": "line", "unit": "compute units", "points": [
            {"label": f"Day {point}", "value": point - 60} for point in range(120)
        ]}} for section in range(8)]
        sections[0]["rows"].append({"label": "Too many", "value": "excluded"})
        sections[0]["chart"]["points"].append({"label": "Extra", "value": 1})
        sections.append({"title": "Extra", "rows": [{"label": "Excluded", "value": "Text"}]})
        result = self.normalize({"details": sections, "extraRateWindows": [
            {"id": f"lane-{index}", "title": "Quota", "window": {"usedPercent": index}} for index in range(65)
        ]})
        self.assertEqual(len(result["metrics"]), 8 * 25)
        self.assertEqual(len(result["windows"]), 64)
        self.assertIn("truncated", result["warnings"])
        rows = [item for item in result["metrics"] if item.get("kind") != "chart"]
        self.assertTrue(all(len(row["value"]) == 120 and len(row["secondaryValue"]) == 120 for row in rows))
        self.assertTrue(all("progress" in row and "usageValue" in row for row in rows))
        charts = [item["chart"] for item in result["metrics"] if item.get("kind") == "chart"]
        self.assertTrue(all(len(chart["points"]) == 120 and chart["unit"] == "compute units" for chart in charts))

    def test_common_credits_and_subscription_metadata_do_not_depend_on_provider(self):
        result = self.normalize({
            "subscriptionRenewsAt": "2026-11-01T00:00:00Z",
            "subscriptionExpiresAt": "2026-12-01T00:00:00Z",
            "dataConfidence": "percentOnly", "updatedAt": "2026-10-06T00:00:00Z",
            "codexResetCredits": {"availableCount": 0},
        }, credits={"remaining": 0})
        self.assertEqual(len(result["balances"]), 1)
        self.assertEqual(result["balances"][0]["id"], "credits_remaining")
        self.assertEqual(result["balances"][0]["value"], 0)
        self.assertEqual(result["renewsAt"], "2026-11-01T00:00:00Z")
        self.assertEqual(result["updatedAt"], "2026-10-06T00:00:00Z")
        self.assertEqual(result["dataConfidence"], "percentOnly")
        metrics = {item["id"]: item for item in result["metrics"]}
        self.assertEqual(metrics["reset_credits_available"]["value"], 0)
        self.assertEqual(metrics["subscription_expires"]["kind"], "timestamp")
        self.assertEqual(metrics["subscription_expires"]["value"], "2026-12-01T00:00:00Z")
        self.assertNotIn("freshUntil", result)
        for confidence in ("exact", "estimated", "unknown"):
            self.assertEqual(self.normalize({"dataConfidence": confidence})["dataConfidence"], confidence)
        invalid = self.normalize({"subscriptionRenewsAt": "not a date", "dataConfidence": "private-marker"})
        self.assertNotIn("renewsAt", invalid)
        self.assertNotIn("dataConfidence", invalid)
        original = self.payload()
        expected = normalize_codexbar_entry("openrouter", original)
        expected.pop("provider")
        for provider in ("claude", "gemini", "future-provider"):
            payload = copy.deepcopy(original)
            payload[0]["provider"] = provider
            result = normalize_codexbar_entry(provider, payload)
            result.pop("provider")
            self.assertEqual(result, expected)

    def test_privacy_filters_and_extension_warnings_are_structurally_bounded(self):
        unsafe = ("fixture@example.invalid", "Bearer fixture-private", "api_key=fixture-private",
                  "sk-proj-" + "a" * 32, "ghp_" + "b" * 36)
        for secret in unsafe:
            with self.subTest(secret=secret[:10]):
                result = self.normalize({
                    "primary": {"usedPercent": 0, "resetDescription": secret},
                    "identity": {"accountID": "private-identity-marker"},
                    "details": [{"title": secret, "rows": [
                        {"id": secret, "label": "Safe", "value": "Count", "secondaryValue": secret},
                        {"label": "Unsafe value", "value": secret},
                        {"label": secret, "value": "Count"},
                    ], "chart": {"kind": "bars", "title": secret, "unit": secret, "points": [
                        {"label": secret, "value": 1}, {"label": "Safe", "value": 0},
                    ]}}],
                    "mistralUsage": {"raw": "private-extension-marker"},
                    "privateField": "private-top-marker",
                }, diagnostic="private-diagnostic-marker", account="private-account-marker")
                text = json.dumps(result)
                self.assertNotIn(secret, text)
                self.assertNotIn("private-", text)
                self.assertIn("unsupported_extension", result["warnings"])
                self.assertTrue(result["available"])
        clean = self.normalize({"details": [{"rows": [{"label": "Calls\u0000today", "value": "12\ntotal"}]}]})
        self.assertEqual(clean["metrics"][0]["label"], "Calls today")
        self.assertEqual(clean["metrics"][0]["value"], "12 total")
        private_only = self.normalize({"identity": {"accountEmail": "fixture@example.invalid"}})
        self.assertEqual(private_only["dataState"], "no_data")
        self.assertNotIn("warnings", private_only)

    def test_cached_v2_fields_are_reconstructed_and_invalid_siblings_are_isolated(self):
        cached = normalize_codexbar_entry("openrouter", self.payload())
        cached["windows"].append(None)
        cached["metrics"].append({"id": "bad", "label": "Bad", "value": float("nan")})
        cached["privateField"] = "private-root-marker"
        cached["source"] = "portal-account"
        cached["plan"] = "Pro"
        cached["windows"][0]["featured"] = True
        cached["metrics"][0]["raw"] = "private-metric-marker"
        original = copy.deepcopy(cached)
        result = normalize_cached_entry(cached)
        self.assertEqual(result["source"], "portal-account")
        self.assertNotIn(None, result["windows"])
        self.assertFalse(any(row["id"] == "bad" for row in result["metrics"]))
        self.assertNotIn("private-", json.dumps(result, allow_nan=False))
        self.assertIn("invalid_field", result["warnings"])
        self.assertEqual(result["dataState"], "ready")
        self.assertEqual(result["plan"], "Pro")
        self.assertTrue(result["windows"][0]["featured"])
        chart = next(item for item in result["metrics"] if item.get("kind") == "chart")
        self.assertEqual(chart["chart"]["points"][0]["value"], 2)
        detail = next(item for item in result["metrics"] if item["id"] == "detail:requests")
        self.assertEqual(detail["progress"], {"used": 12, "total": 100})
        self.assertEqual(detail["legacyIds"], ["detail-0-1"])
        self.assertEqual(cached, original)
        invalid = normalize_cached_entry({"provider": "future-provider", "available": True,
            "source": "private-source-marker", "windows": [None], "balances": [], "metrics": [],
            "error": "Bearer private-error-marker", "updatedAt": "not a timestamp"})
        self.assertFalse(invalid["available"])
        self.assertEqual(invalid["dataState"], "error")
        self.assertEqual(invalid["source"], "cli")
        self.assertIsNone(invalid["updatedAt"])
        self.assertNotIn("private-", repr(invalid))

    def test_cached_scheduling_metadata_is_validated_without_becoming_usage_time(self):
        entry = self.normalize({"primary": {"usedPercent": 1}, "dataConfidence": "estimated",
                                "updatedAt": "2026-10-06T00:00:00Z"})
        metadata = {"refreshState": "running", "staleAfterSeconds": 900,
                    "lastAttemptAt": "2026-10-06T00:01:00Z", "freshUntil": "2026-10-06T00:15:00Z",
                    "nextRetryAt": "2026-10-06T00:06:00Z", "refreshStartedAt": "2026-10-06T00:01:00Z",
                    "refreshDeadlineAt": "2026-10-06T00:02:00Z"}
        entry.update(metadata, warnings=["clock_skew", "no_data", "private-warning-marker"])
        result = normalize_cached_entry(entry)
        self.assertIn("refreshState", result)
        for key, value in metadata.items():
            self.assertEqual(result[key], value)
        self.assertEqual(result["dataConfidence"], "estimated")
        self.assertEqual(result["updatedAt"], entry["updatedAt"])
        self.assertEqual(result["warnings"], ["clock_skew", "no_data"])
        for key in ("freshUntil", "nextRetryAt", "refreshStartedAt", "refreshDeadlineAt"):
            entry[key] = None
        nullable = normalize_cached_entry(entry)
        self.assertTrue(all(nullable[key] is None for key in ("freshUntil", "nextRetryAt", "refreshStartedAt", "refreshDeadlineAt")))
        for key in metadata:
            entry[key] = "Bearer private-metadata-marker"
        entry["dataConfidence"] = {"raw": "private-confidence-marker"}
        invalid = normalize_cached_entry(entry)
        self.assertTrue(invalid["available"])
        self.assertIn("invalid_field", invalid["warnings"])
        self.assertNotIn("private-", repr(invalid))
        self.assertNotIn("staleAfterSeconds", invalid)
        self.assertNotIn("refreshState", invalid)
        for invalid_seconds in (True, 0, -1, float("inf")):
            entry["staleAfterSeconds"] = invalid_seconds
            self.assertNotIn("staleAfterSeconds", normalize_cached_entry(entry))

    def test_cache_roundtrip_preserves_currency_units_aliases_and_legacy_data(self):
        fresh = self.normalize({"providerCost": {"used": 0, "limit": 10, "balance": -1, "currencyCode": "EUR"},
                                "details": [{"rows": [{"id": "r" * 120, "label": "Row", "value": "0"}]}]})
        result = normalize_cached_entry(fresh)
        self.assertEqual(result, fresh)
        legacy = {"provider": "openrouter", "available": True, "source": "api",
                  "primary": {"usedPercent": 37}, "openRouter": {"balance": -2},
                  "creditsRemaining": 0, "resetCreditsAvailable": 0,
                  "updatedAt": "2026-10-06T00:00:00Z", "lastAttemptAt": "2026-10-06T00:01:00Z"}
        result = normalize_cached_entry(legacy)
        self.assertEqual(result["windows"][0]["id"], "primary")
        self.assertEqual(result["windows"][0]["label"], "Session")
        self.assertEqual([row["id"] for row in result["balances"]], ["balance", "credits_remaining"])
        self.assertEqual(result["metrics"][0]["id"], "reset_credits_available")
        self.assertEqual(result["lastAttemptAt"], legacy["lastAttemptAt"])
        self.assertEqual(result["updatedAt"], legacy["updatedAt"])
        v1 = {"provider": "claude", "available": True, "source": "oauth", "windows": [], "balances": [],
              "metrics": [{"id": "detail-0-0", "label": "Premium", "value": True}]}
        self.assertEqual(normalize_cached_entry(v1)["metrics"], v1["metrics"])
        capped = normalize_cached_entry({"provider": "claude", "available": True, "windows": [], "balances": [],
            "metrics": [{"id": f"metric-{index}", "label": "Count", "value": 0} for index in range(257)]})
        self.assertEqual(len(capped["metrics"]), 256)
        self.assertIn("truncated", capped["warnings"])
        for invalid in (None, [], {}, {"provider": "invalid/id"}):
            self.assertIsNone(normalize_cached_entry(invalid))

    def test_malformed_optional_fields_are_local_and_never_crash_normalization(self):
        result = self.normalize({"primary": {"usedPercent": 10 ** 400}, "secondary": {"usedPercent": 2},
                                "details": [{"title": "Good", "rows": [{"label": "Count", "value": "2",
                                    "progress": {"used": 1, "total": True}, "usageValue": float("inf")}]}, None]})
        self.assertEqual(result["windows"][0]["id"], "secondary")
        self.assertEqual(result["metrics"][0]["value"], "2")
        self.assertIn("invalid_field", result["warnings"])
        self.assertNotIn("progress", result["metrics"][0])
        json.dumps(result, allow_nan=False)
        for value in (None, [], True, "invalid"):
            cached = self.normalize({"primary": {"usedPercent": 0}})
            cached["metrics"] = [None, {"id": "kept", "label": "Kept", "value": 0,
                                       "currency": value, "kind": value, "legacyIds": [None, "detail-0-0", "secret=private"]}]
            cached["windows"].append({"id": "bad", "label": "Bad", "usedPercent": 10 ** 400})
            normalized = normalize_cached_entry(cached)
            self.assertTrue(normalized["available"])
            self.assertEqual(normalized["metrics"][0]["value"], 0)
            self.assertEqual(normalized["metrics"][0]["legacyIds"], ["detail-0-0"])
            self.assertNotIn("currency", normalized["metrics"][0])
            self.assertNotIn("bad", [item["id"] for item in normalized["windows"]])
            self.assertNotIn("private", repr(normalized))

    def test_generic_credits_do_not_expose_an_unread_placeholder_balance(self):
        credits = {"remaining": 0, "events": [], "updatedAt": "2026-10-06T00:00:00Z",
                   "balanceReadSucceeded": False, "balanceIsWorkspace": True}
        result = self.normalize({}, credits=credits)
        self.assertEqual(result["balances"], [])
        self.assertEqual(result["dataState"], "no_data")
        self.assertIn("balance_unavailable", result["warnings"])
        credits["balanceReadSucceeded"] = True
        confirmed = self.normalize({}, credits=credits)
        self.assertEqual(confirmed["balances"][0]["value"], 0)
        self.assertEqual(confirmed["balances"][0]["scope"], "workspace")
        self.assertEqual(confirmed["balances"][0]["updatedAt"], credits["updatedAt"])
        self.assertEqual(normalize_cached_entry(confirmed), confirmed)

    def test_credits_only_cli_response_still_has_usable_data(self):
        result = self.normalize(None, credits={"remaining": 7})
        self.assertTrue(result["available"])
        self.assertEqual(result["dataState"], "ready")
        self.assertEqual(result["balances"][0]["value"], 7)
        self.assertIsNone(result["updatedAt"])
        self.assertNotIn("error", result)

    def test_invalid_optional_values_are_annotated_without_removing_valid_fields(self):
        result = self.normalize({"primary": {"usedPercent": 2, "resetsAt": "2026-10-06"},
                                "details": [{"rows": [{"label": "Count", "value": "2",
                                    "progress": {"used": 1, "total": 0}, "usageValue": True,
                                    "secondaryValue": "fixture@example.invalid"}]}]})
        self.assertNotIn("resetsAt", result["windows"][0])
        self.assertEqual(result["metrics"][0]["value"], "2")
        self.assertIn("invalid_field", result["warnings"])
        cached = self.normalize({"primary": {"usedPercent": 0}})
        cached["windows"][0].update(updatedAt="invalid", legacyIds=["bad@example.invalid"], scope="private-scope")
        cached["dataState"] = "private-state-marker"
        normalized = normalize_cached_entry(cached)
        self.assertTrue(normalized["available"])
        self.assertIn("invalid_field", normalized["warnings"])
        self.assertNotIn("private-", repr(normalized))
        self.assertNotIn("scope", normalized["windows"][0])
        self.assertNotIn("legacyIds", normalized["windows"][0])

    def test_capped_and_uncapped_serialized_shapes_preserve_billing_semantics(self):
        payload = self.payload()
        capped = copy.deepcopy(payload[1])
        uncapped = copy.deepcopy(payload[2])
        for item in (capped, uncapped):
            item["provider"] = "openrouter"
        cap = normalize_codexbar_entry("openrouter", [capped])
        self.assertEqual(cap["windows"][0]["label"], "API key spend cap")
        self.assertEqual(cap["balances"], [])
        self.assertFalse(any(row["id"] == "cost_used" for row in cap["metrics"]))
        self.assertEqual(next(row["secondaryValue"] for row in cap["metrics"] if row["label"] == "API key limit"), "Spending cap, not balance")
        no_cap = normalize_codexbar_entry("openrouter", [uncapped])
        self.assertEqual(no_cap["windows"], [])
        self.assertEqual(no_cap["balances"][0]["value"], 88)
        self.assertEqual(next(row["value"] for row in no_cap["metrics"] if row["id"] == "cost_used"), 12)
        self.assertEqual(next(row["value"] for row in no_cap["metrics"] if row["label"] == "API key limit"), "No limit configured")
        self.assertTrue(no_cap["available"])

    def test_top_level_labels(self):
        payload = self.payload()
        payload[0]["usage"]["rateWindowLabels"] = {"primary": "Legacy label"}
        result = normalize_codexbar_entry("openrouter", payload)
        self.assertEqual(result["windows"][0]["label"], "API key spend cap")


if __name__ == "__main__":
    unittest.main()
