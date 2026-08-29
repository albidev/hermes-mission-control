"""Tests for the Mission Control Nous Portal telemetry adapter."""

from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

nous_portal_usage = importlib.import_module("nous_portal_usage")
from provider_usage_contract import normalize_codexbar_entry


class _Response:
    def __init__(self, payload: dict):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps(self._payload).encode("utf-8")


class NousPortalUsageTests(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="mc-nous-usage-"))
        self._home_backup = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = str(self._tmp / "hermes")
        (self._tmp / "hermes").mkdir()
        nous_portal_usage.reset_nous_portal_usage_cache()

    def tearDown(self):
        if self._home_backup is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = self._home_backup
        import shutil

        shutil.rmtree(self._tmp, ignore_errors=True)
        nous_portal_usage.reset_nous_portal_usage_cache()

    def test_normalizes_portal_account_into_billing_shape(self):
        payload = {
            "subscription": {
                "plan": "Plus",
                "monthly_credits": 20,
                "current_period_end": "2026-09-15T00:00:00Z",
            },
            "paid_service_access": {
                "subscription_credits_remaining": 12.5,
                "purchased_credits_remaining": 4.25,
                "total_usable_credits": 16.75,
            },
            "user": {"email": "must-not-leak@example.test"},
        }

        result = nous_portal_usage.normalize_account_payload(payload)

        self.assertEqual(result["provider"], "nous")
        self.assertEqual(result["source"], "portal-account")
        self.assertEqual(result["plan"], "Plus")
        self.assertEqual(result["windows"][0]["id"], "subscription")
        self.assertEqual(result["windows"][0]["usedPercent"], 38)
        self.assertEqual(result["windows"][0]["remaining"], 12.5)
        self.assertEqual(result["windows"][0]["total"], 20.0)
        balances = {item["id"]: item for item in result["balances"]}
        self.assertEqual(balances["subscription_remaining"]["value"], 12.5)
        self.assertEqual(balances["topup_remaining"]["value"], 4.25)
        self.assertEqual(balances["total_spendable"]["value"], 16.75)
        self.assertNotIn("email", result)
        self.assertNotIn("rawAccount", result)

    def test_normalizes_codexbar_windows_and_provider_metrics(self):
        result = normalize_codexbar_entry(
            "codex",
            [{
                "provider": "codex",
                "source": "oauth",
                "usage": {
                    "primary": {"usedPercent": 12, "resetsAt": "2026-09-01T00:00:00Z", "windowMinutes": 300},
                    "secondary": {"usedPercent": 20, "windowMinutes": 10080},
                    "codexResetCredits": {"availableCount": 2},
                },
                "credits": {"remaining": 4},
            }],
        )

        self.assertEqual([window["id"] for window in result["windows"]], ["primary", "secondary"])
        self.assertEqual(result["windows"][0]["usedPercent"], 12)
        self.assertEqual(result["balances"][0]["value"], 4)
        self.assertEqual(result["metrics"][0]["value"], 2)

    def test_fetch_uses_current_access_token_without_refreshing(self):
        auth_path = self._tmp / "hermes" / "auth.json"
        auth_path.write_text(
            json.dumps(
                {
                    "providers": {
                        "nous": {
                            "access_token": "access-only",
                            "refresh_token": "refresh-must-not-be-used",
                            "portal_base_url": "https://portal.nousresearch.com",
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        payload = {"subscription": {}, "paid_service_access": {"total_usable_credits": 3}}

        with patch.object(nous_portal_usage.urllib.request, "urlopen", return_value=_Response(payload)) as urlopen:
            result = nous_portal_usage.fetch_nous_portal_usage()

        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://portal.nousresearch.com/api/oauth/account")
        self.assertEqual(request.get_header("Authorization"), "Bearer access-only")
        balances = {item["id"]: item for item in result["balances"]}
        self.assertEqual(balances["total_spendable"]["value"], 3.0)
        self.assertNotIn("refresh", repr(request.headers).lower())

    def test_expired_token_delegates_refresh_to_hermes_cli(self):
        auth_path = self._tmp / "hermes" / "auth.json"
        auth_path.write_text(
            json.dumps({
                "providers": {
                    "nous": {
                        "access_token": "expired-access",
                        "refresh_token": "refresh-must-stay-private",
                        "expires_at": "2000-01-01T00:00:00Z",
                        "portal_base_url": "https://portal.nousresearch.com",
                    }
                }
            }),
            encoding="utf-8",
        )
        payload = {"subscription": {}, "paid_service_access": {"total_usable_credits": 5}}

        def refresh_via_cli(command, **kwargs):
            self.assertEqual(command[1:], ["portal", "info"])
            auth_path.write_text(
                json.dumps({
                    "providers": {
                        "nous": {
                            "access_token": "fresh-access",
                            "expires_at": "2099-01-01T00:00:00Z",
                            "portal_base_url": "https://portal.nousresearch.com",
                        }
                    }
                }),
                encoding="utf-8",
            )
            return type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        with patch.object(nous_portal_usage.subprocess, "run", side_effect=refresh_via_cli) as run, \
             patch.object(nous_portal_usage.urllib.request, "urlopen", return_value=_Response(payload)) as urlopen:
            result = nous_portal_usage.fetch_nous_portal_usage()

        run.assert_called_once()
        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer fresh-access")
        self.assertEqual(result["balances"][0]["value"], 5.0)

    def test_missing_access_token_fails_open_without_network_call(self):
        auth_path = self._tmp / "hermes" / "auth.json"
        auth_path.write_text(json.dumps({"providers": {"nous": {}}}), encoding="utf-8")

        with patch.object(nous_portal_usage.urllib.request, "urlopen") as urlopen:
            result = nous_portal_usage.fetch_nous_portal_usage()

        urlopen.assert_not_called()
        self.assertFalse(result["available"])
        self.assertEqual(result["error"], "Nous Portal is not authenticated.")


if __name__ == "__main__":
    unittest.main()
