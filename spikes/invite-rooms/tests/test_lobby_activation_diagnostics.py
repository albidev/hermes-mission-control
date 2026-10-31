"""MVP.2b-fix4 regression tests for R10 (OperationJournal.diagnostic exact-type
dispatch in spikes/invite-rooms/lobby_activation.py).

Behaviour-only: disposable journal dirs under tempfile.TemporaryDirectory(),
no sockets/native. Ports fix4's external review probe
(evidence/mvp2b-rereview2/review_checks.py R10_unknown_subclass /
R10_operation_validation / R10_str_hook_called) into the in-repo suite.

R10 (pre-fix4): `_diagnostic_error` used `isinstance(exc, (ControlError,
OperationAmbiguous))` and `str(exc)` to classify + extract the code. A
subclass with a hostile name/__str__/__repr__ would (a) be accepted by the
isinstance check and (b) have its stringification hook invoked — letting an
attacker-controlled subclass leak into durable diagnostics. fix4 switches to
`type(exc) is ...` exact-type dispatch and reads `exc.args[0]` directly
(never str()/repr()), so any unknown subclass — however it is named or
however it stringifies — is always classified 'unexpected' with code=None
and its class name is never persisted, and its __str__/__repr__ are never
invoked.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

from lobby_control import ControlError
from lobby_activation import OperationJournal, OperationAmbiguous, _diagnostic_operation


class UnknownSubclassDiagnosticTests(unittest.TestCase):
    def test_unknown_control_error_subclass_marker_not_persisted(self):
        """An unknown ControlError subclass, even carrying an allowlisted code
        string and a marker class name, must be classified 'unexpected' with
        code=None, and the marker class name must never appear in the
        durable diagnostics row (exact-type dispatch, not isinstance)."""
        with tempfile.TemporaryDirectory() as tmp:
            journal = OperationJournal(Path(tmp) / "activation")
            marker = "SYNTHETIC_CLASS_TOKEN_" + "Q" * 40
            UnknownControlError = type(marker, (ControlError,), {})
            journal.diagnostic("plan", UnknownControlError("control_unavailable"))
            row = json.loads((journal.path.parent / "diagnostics.jsonl").read_text())
            self.assertEqual(row["exception_class"], "unexpected")
            self.assertIsNone(row["control_error_code"])
            self.assertNotIn(marker, json.dumps(row))

    def test_unknown_operation_ambiguous_subclass_marker_not_persisted(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = OperationJournal(Path(tmp) / "activation")
            marker = "SYNTHETIC_AMBIGUOUS_TOKEN_" + "Z" * 40
            UnknownAmbiguous = type(marker, (OperationAmbiguous,), {})
            # OperationAmbiguous.__init__ derives its own code from `operation`;
            # construct via ControlError.__init__ directly to control args[0].
            exc = UnknownAmbiguous.__new__(UnknownAmbiguous)
            ControlError.__init__(exc, "send_ambiguous")
            journal.diagnostic("send", exc)
            row = json.loads((journal.path.parent / "diagnostics.jsonl").read_text())
            self.assertEqual(row["exception_class"], "unexpected")
            self.assertIsNone(row["control_error_code"])
            self.assertNotIn(marker, json.dumps(row))

    def test_hostile_str_and_repr_never_invoked(self):
        """A subclass whose __str__/__repr__ have side effects (e.g. would
        normally leak a secret into logs) must never have those hooks called
        by the diagnostic path, exact-type dispatch or not."""
        with tempfile.TemporaryDirectory() as tmp:
            journal = OperationJournal(Path(tmp) / "activation")
            calls = []

            class SideEffectString(ControlError):
                def __str__(self):
                    calls.append("str")
                    return "control_unavailable"

                def __repr__(self):
                    calls.append("repr")
                    return "control_unavailable"

            journal.diagnostic("plan", SideEffectString())
            self.assertEqual(calls, [])
            row = json.loads((journal.path.parent / "diagnostics.jsonl").read_text())
            self.assertEqual(row["exception_class"], "unexpected")

    def test_exact_control_error_keeps_its_mapping(self):
        """Exact ControlError (not a subclass) with an allowlisted code is
        still classified 'control_error' with that code — the exact-type
        dispatch must not over-reject legitimate instances."""
        with tempfile.TemporaryDirectory() as tmp:
            journal = OperationJournal(Path(tmp) / "activation")
            journal.diagnostic("plan", ControlError("control_unavailable"))
            row = json.loads((journal.path.parent / "diagnostics.jsonl").read_text())
            self.assertEqual(row["exception_class"], "control_error")
            self.assertEqual(row["control_error_code"], "control_unavailable")

    def test_exact_operation_ambiguous_keeps_its_mapping(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = OperationJournal(Path(tmp) / "activation")
            journal.diagnostic("create", OperationAmbiguous("create"))
            row = json.loads((journal.path.parent / "diagnostics.jsonl").read_text())
            self.assertEqual(row["exception_class"], "operation_ambiguous")
            self.assertEqual(row["control_error_code"], "creation_ambiguous")

    def test_control_error_with_non_allowlisted_code_has_code_none(self):
        """An exact ControlError whose single arg is NOT in the allowlist
        must still be classified 'control_error' but with code=None — the
        arbitrary string itself is never persisted."""
        with tempfile.TemporaryDirectory() as tmp:
            journal = OperationJournal(Path(tmp) / "activation")
            marker = "SYNTHETIC_PRIVATE_TOKEN_" + "Q" * 40
            journal.diagnostic("plan", ControlError(marker))
            row = json.loads((journal.path.parent / "diagnostics.jsonl").read_text())
            self.assertEqual(row["exception_class"], "control_error")
            self.assertIsNone(row["control_error_code"])
            self.assertNotIn(marker, json.dumps(row))

    def test_operation_validation_unknown_vs_known(self):
        pairs = [
            ("plan", "plan"), ("register:peer-b", "register:peer-b"),
            ("bad op", "unknown"), ("register:" + "Q" * 200, "unknown"),
        ]
        for operation, expected in pairs:
            with self.subTest(operation=operation):
                self.assertEqual(_diagnostic_operation(operation), expected)


if __name__ == "__main__":
    unittest.main()
