"""Preflight native probes are hermetic and never leak a process tree.

Regression for the two S2 suite timeouts (test_preflight CLI 60s, test_s2d_control
policy probe 120s): a probe in a fresh scratch HERMES_HOME made core provision a
~140 MB isolated runtime over the network, and on timeout the probe's children
survived as orphans still downloading into a (deleted) scratch home.
"""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

import preflight  # noqa: E402


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class HermeticProbeTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix='probe-hermetic-', dir=os.environ.get('TMPDIR')))

    def test_probe_child_never_lazily_provisions_a_runtime(self):
        payload = preflight._native_probe(self.home, (
            'import json, os\n'
            'print(json.dumps({"lazy": os.environ.get("HERMES_DISABLE_LAZY_INSTALLS")}))'))
        self.assertEqual(payload, {'lazy': '1'})
        self.assertFalse((self.home / 'installs').exists())

    def test_every_lab_child_env_disables_lazy_runtime_provisioning(self):
        # Native gateway/TUI children use the same env builder as the probes.
        self.assertEqual(preflight.isolated_child_env(self.home)['HERMES_DISABLE_LAZY_INSTALLS'], '1')

    def test_real_policy_probe_is_fast_in_a_cold_home(self):
        start = time.monotonic()
        policy = preflight._native_probe(self.home, preflight._POLICY_PROBE)
        self.assertIsInstance(policy, dict)
        self.assertLess(time.monotonic() - start, 30)
        self.assertFalse((self.home / 'installs').exists())

    def test_timeout_kills_the_whole_probe_tree(self):
        pidfile = self.home / 'grandchild.pid'
        script = (
            'import subprocess, sys, time\n'
            'p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])\n'
            f'open({str(pidfile)!r}, "w").write(str(p.pid))\n'
            'time.sleep(60)\n')
        with patch.object(preflight, '_PROBE_TIMEOUT', 2):
            with self.assertRaises(preflight.PreflightBlocked):
                preflight._native_probe(self.home, script)
        grandchild = int(pidfile.read_text())
        deadline = time.monotonic() + 3
        while _alive(grandchild) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(_alive(grandchild), 'probe grandchild survived the timeout')


if __name__ == '__main__':
    unittest.main(verbosity=2)
