"""MVP.2b-fix4 regression test for R11 (Popen-registered-before-processes()
ordering in the 3 lab-child-spawn call sites).

fix4 touches THREE byte-identical inline patterns (verified by diff against
the pre-fix4 snapshot): gate_s1_native.py NativeLab.spawn, and the native
fault-injection helpers in s2c_mint_faults.py / s2e_activation_faults.py.
Pre-fix4, each site called `processes()` (which shells out to `ps` and can
raise, e.g. StopIteration if the child already exited, or a subprocess
failure) BEFORE appending `(proc, record)` to `lab.owned` / `self.owned` — so
a raise there would leak an already-spawned, already-owned child with no
registration anywhere to reap it. fix4 reorders this to register FIRST,
then call `processes()`.

This test drives the real, directly-callable site (gate_s1_native.py
NativeLab.spawn) with a harmless local subprocess (no network, no ports
18790/18791, no actual native gateway/TUI process — `PY`/`self.entry` are
monkeypatched to a trivial `sys.exit(0)` script) and asserts the ordering
behaviourally: when `processes()` is made to raise, the child is still
found in `self.owned` afterward (and is reaped/waited by this test). This
is a dynamic behavioural check, not a source read: the two other call
sites in s2c_mint_faults.py / s2e_activation_faults.py share byte-identical
code (diffed and confirmed in review evidence) but are only reachable from
inside native fault-injection gates that require a live ControlLab/network,
which is out of scope here (FORBIDDEN: native gates, ports 18790/18791).
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

import gate_s1_native as g


class _MinimalLab:
    """Exercises ONLY NativeLab.spawn's body: no setup()/native entrypoints."""

    def __init__(self, out, entry, home):
        self.out = out
        self.owned = []
        self.logs = []
        self.report = {"processes": [], "commands": []}
        self.entry = entry
        self.home = home
        self.target = home
        self.audit = out / "audit.jsonl"
        self.args = {"host": [], "target": []}

    def flush(self):
        pass

    spawn = g.NativeLab.spawn


class PopenRegisteredBeforeProcessesTests(unittest.TestCase):
    def test_child_registered_in_owned_even_when_processes_raises(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            (home / "runtime").mkdir(parents=True)
            (home / "tmp").mkdir(parents=True)
            entry = root / "entry.py"
            entry.write_text("import time\ntime.sleep(5)\n")

            lab = _MinimalLab(root, entry, home)

            with patch.object(g, "PY", Path(sys.executable)), \
                 patch.object(g, "processes", side_effect=RuntimeError("synthetic_ps_failure")):
                with self.assertRaises(RuntimeError):
                    lab.spawn("host")

            self.assertEqual(len(lab.owned), 1, "the child must be registered even though processes() raised")
            proc, record = lab.owned[0]
            self.assertEqual(record["role"], "host")
            try:
                self.assertIsNone(proc.poll(), "child must still be alive/owned, not silently lost")
            finally:
                proc.kill()
                proc.wait(timeout=5)
                for log in lab.logs:
                    log.close()

    def test_child_registered_before_processes_is_even_called(self):
        """Stronger ordering check: processes() must observe the append has
        already happened (call-order, not just post-hoc survival)."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            (home / "runtime").mkdir(parents=True)
            (home / "tmp").mkdir(parents=True)
            entry = root / "entry.py"
            entry.write_text("import time\ntime.sleep(5)\n")

            lab = _MinimalLab(root, entry, home)
            seen_owned_len_at_call = []
            real_processes = g.processes

            def spying_processes():
                seen_owned_len_at_call.append(len(lab.owned))
                return real_processes()

            with patch.object(g, "PY", Path(sys.executable)), \
                 patch.object(g, "processes", side_effect=spying_processes):
                proc = lab.spawn("host")

            try:
                self.assertEqual(seen_owned_len_at_call, [1], "owned must already contain the child when processes() runs")
            finally:
                proc.kill()
                proc.wait(timeout=5)
                for log in lab.logs:
                    log.close()


if __name__ == "__main__":
    unittest.main()
