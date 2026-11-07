"""MVP.2b-fix4 regression tests for R11 (Scenario.__exit__ teardown
aggregation in spikes/invite-rooms/gate_s2e_native.py).

Behaviour-only: fully synthetic Scenario stand-ins (no sockets/subprocess/
native). Ports fix4's external review probe
(evidence/mvp2b-rereview2/review_checks.py R11_multiple_close_failures /
R11_pre_cleanup_read_failure) into the in-repo suite.

R11 (pre-fix4): the pre-teardown secret inventory scan ran OUTSIDE the
teardown `finally`, so an OSError while walking a root (e.g. unreadable
file) aborted the whole __exit__ before host/target/adapter/transport/
control_network were ever closed, and a failed transport close was
unconditionally removed from `lab.transports`/ownership regardless of
whether the close actually succeeded. fix4 moves the inventory scan into a
`try` whose `finally` always attempts every resource close (aggregating
failures instead of aborting), and only drops `transport` from top-level
ownership when its close actually succeeded.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

import gate_s2e_native as ge


def _outcome(fn):
    try:
        return {"accepted": True, "value": fn()}
    except Exception as exc:
        return {"accepted": False, "error": type(exc).__name__ + ":" + str(exc)}


def _fake_scenario(root, fail_transport=False, fail_scan=False):
    visits = []

    class Resource:
        def __init__(self, name, fail=False):
            self.name, self.fail = name, fail
            self.base = "http://127.0.0.1:1"

        def __exit__(self, *args):
            self.close()

        def close(self):
            visits.append(self.name)
            if self.fail:
                raise RuntimeError("synthetic_close_failure")

    network = SimpleNamespace(
        daemon=None, close=Resource("network").close,
        loop=SimpleNamespace(is_closed=lambda: True),
        thread=SimpleNamespace(is_alive=lambda: False), clients=[],
    )
    transport = Resource("transport", fail_transport)
    transport.root = root
    transport.daemon = None
    s = ge.Scenario.__new__(ge.Scenario)
    s.root = root
    s.name = "delta"
    s.network = network
    s.transport = transport
    s.values = []
    s.host = Resource("host", True)
    s.target = Resource("target", True)
    s.adapter = Resource("adapter")
    s.lab = SimpleNamespace(
        secret_bytes=[], networks=[network], transports=[transport], check=lambda *a, **k: None
    )
    return s, visits, transport


class TeardownAggregationTests(unittest.TestCase):
    def test_pre_scan_oserror_still_attempts_every_resource_close(self):
        """A pre-teardown secret-inventory OSError must not short-circuit
        teardown: host/target/adapter/transport/control_network must all
        still be visited, and the failures must be aggregated into one
        RuntimeError rather than propagating the raw OSError."""
        with tempfile.TemporaryDirectory() as td:
            s, visits, transport = _fake_scenario(Path(td), fail_transport=True)
            key = Path(td) / "synthetic.key"
            key.write_bytes(b"not-a-real-key")
            with patch.object(Path, "read_bytes", side_effect=OSError("synthetic_read_failure")):
                result = _outcome(lambda: s.__exit__(None, None, None))

            self.assertFalse(result["accepted"])
            self.assertIn("RuntimeError", result["error"])
            for name in ("host", "target", "adapter", "network"):
                self.assertIn(name, visits)

    def test_multiple_close_failures_aggregated_and_transport_retained(self):
        """Host/target close failures + a failing transport close must all
        be attempted and aggregated; a FAILED transport close must leave the
        transport retained in top-level ownership (never silently dropped)."""
        with tempfile.TemporaryDirectory() as td:
            s, visits, transport = _fake_scenario(Path(td), fail_transport=True)
            result = _outcome(lambda: s.__exit__(None, None, None))

            self.assertFalse(result["accepted"])
            for name in ("host", "target", "adapter", "transport", "network"):
                self.assertIn(name, visits)
            self.assertIn(transport, s.lab.transports, "failed transport close must stay owned")
            self.assertIn(network_sentinel := s.network, s.lab.networks)

    def test_successful_transport_close_is_removed_from_ownership(self):
        """Mirror case: when transport close SUCCEEDS, it must be dropped
        from top-level ownership (no double-close / dangling reference)."""
        with tempfile.TemporaryDirectory() as td:
            s, visits, transport = _fake_scenario(Path(td), fail_transport=False)
            # host/target still fail to force an aggregated RuntimeError, but
            # transport itself must close cleanly.
            _outcome(lambda: s.__exit__(None, None, None))
            self.assertIn("transport", visits)
            self.assertNotIn(transport, s.lab.transports, "succeeded transport close must be dropped")


if __name__ == "__main__":
    unittest.main()
