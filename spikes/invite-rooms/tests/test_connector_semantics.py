"""Target connector maps firewall outcomes to statuses the native host client reads correctly."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from connector import TargetConnector  # noqa: E402
from room_firewall import FirewallDenied, Request, UpstreamAmbiguous, UpstreamResponse  # noqa: E402


class _Firewall:
    def __init__(self, outcome):
        self.outcome = outcome

    def forward(self, request):
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def _target(outcome) -> TargetConnector:
    target = TargetConnector.__new__(TargetConnector)  # no TLS context needed for mapping
    target.firewall = _Firewall(outcome)
    return target


class TargetConnectorSemanticsTests(unittest.TestCase):
    request = Request("GET", "/v1/runs/run_abc")

    def test_withheld_native_response_is_ambiguous_5xx_not_refusal(self):
        response = _target(UpstreamAmbiguous("native response shape rejected")).respond(self.request)
        self.assertEqual(response.status, 502)
        self.assertIsNone(json.loads(response.body)["error"]["code"])

    def test_pre_forward_denial_stays_403(self):
        self.assertEqual(_target(FirewallDenied("route not allowed")).respond(self.request).status, 403)

    def test_native_response_passes_through(self):
        native = UpstreamResponse(200, (), b"{}")
        self.assertIs(_target(native).respond(self.request), native)


if __name__ == "__main__":
    unittest.main()
