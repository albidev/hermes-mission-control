"""Aggregate validator negative fixtures; not native evidence."""
import copy
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gate_c_aggregate import validate_cases
from test_s1_relay_native import CASE_CHECKS


class AggregateTests(unittest.TestCase):
    def test_rejects_missing_predicate_source_drift_and_teardown(self):
        source = {'head': 'fixture', 'hashes': {'x': 'a'}}
        reports = []
        for case, names in CASE_CHECKS.items():
            reports.append(dict(c_case=case, source_start=source, source_end=source,
                packet_complete=True, teardown_complete=True, cleanup_failures=[],
                owned_group_survivors=[], ports_free_after={'18790': True, '18791': True},
                lab_model_request_overrides={'chat_template_kwargs': {'enable_thinking': False}},
                lab_model_response_observations=[{'reasoning_chars': 0, 'visible_chars': 1,
                    'reasoning_tokens': None, 'request_extra_body': {'chat_template_kwargs': {'enable_thinking': False}}}],
                checks={name: {'ok': True} for name in names}))
        receipts = [dict(case=c, exit_code=0, wall_seconds=100) for c in CASE_CHECKS]
        self.assertTrue(validate_cases(reports, receipts, source)['packet_complete'])
        for defect in ('missing', 'drift', 'teardown', 'exit', 'ports', 'model', 'order', 'late_drift'):
            rs, es, end = copy.deepcopy((reports, receipts, source))
            if defect == 'missing': rs[0]['checks'].clear()
            if defect == 'drift': rs[0]['source_end'] = {'head': 'other'}
            if defect == 'teardown': rs[0]['teardown_complete'] = False
            if defect == 'exit': es[0]['exit_code'] = 1
            if defect == 'ports': rs[0]['ports_free_after']['18790'] = False
            if defect == 'model': rs[0]['lab_model_response_observations'] = []
            if defect == 'order': rs.reverse()
            if defect == 'late_drift': end = {'head': 'other'}
            self.assertFalse(validate_cases(rs, es, end)['packet_complete'], defect)

class ControllerProcessTests(unittest.TestCase):
    def exercise_controller(self, signum=None):
        import json
        import os
        import signal
        import subprocess
        import tempfile
        import time
        from gate_s1_native import ROOT, processes
        with tempfile.TemporaryDirectory(dir=ROOT, prefix='c-controller-test-') as tmp:
            out = Path(tmp)
            script = '''import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from gate_c_aggregate import controller_session, run_owned_child
out = Path(sys.argv[2])
child = "import signal,time,pathlib; signal.signal(signal.SIGTERM,signal.SIG_IGN); signal.signal(signal.SIGINT,signal.SIG_IGN); pathlib.Path(%r).touch(); time.sleep(60)" % str(out/'child-ready')
def action(meta, stop):
    for case in ('first', 'must-not-launch'):
        run_owned_child([sys.executable,'-B','-c',child], case, out, meta, stop,
                        timeout=float(sys.argv[3]), cleanup_timeout=.2)
    return 0
raise SystemExit(controller_session(out, action))
'''
            proc = subprocess.Popen([sys.executable, '-B', '-c', script,
                str(Path(__file__).resolve().parents[1]), tmp, '10' if signum else '.5'],
                start_new_session=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                deadline = time.monotonic() + 15
                while not (out/'child-ready').exists() and proc.poll() is None and time.monotonic() < deadline:
                    time.sleep(.03)
                self.assertTrue((out/'child-ready').exists())
                startup = json.loads((out/'controller-start.json').read_text())
                self.assertEqual(startup['pid'], proc.pid)
                self.assertEqual(startup['pgid'], proc.pid)
                self.assertEqual(startup['sid'], proc.pid)
                if signum:
                    proc.send_signal(signum)
                stdout, stderr = proc.communicate(timeout=15)
                self.assertNotEqual(proc.returncode, 0, stderr)
                final = json.loads((out/'controller-exit.json').read_text())
                report = json.loads((out/'report.json').read_text())
                receipt = json.loads((out/'first-exit.json').read_text())
                self.assertEqual(final['exit_code'], proc.returncode)
                self.assertFalse(report['packet_complete'])
                self.assertEqual([r['case'] for r in report['cases']], ['first'])
                self.assertEqual(receipt['outcome'], 'killed')
                self.assertEqual(receipt['exit_code'], -signal.SIGKILL)
                self.assertEqual(receipt['owned_group_survivors'], [])
                self.assertEqual(receipt['identity']['pid'], receipt['identity']['pgid'])
                self.assertTrue(receipt['identity']['birth'])
                self.assertFalse(any(r['pgid'] == receipt['identity']['pgid'] for r in processes()))
                self.assertFalse((out/'must-not-launch-exit.json').exists())
                if signum:
                    self.assertEqual(receipt['requested_signal'], signal.Signals(signum).name)
                    self.assertEqual(final['requested_signal'], signal.Signals(signum).name)
                else:
                    self.assertEqual(receipt['requested_signal'], 'SIGTERM')
            finally:
                if proc.poll() is None:
                    proc.send_signal(signal.SIGTERM)
                    proc.wait(timeout=15)
                proc.stdout.close()
                proc.stderr.close()

    def test_stubborn_child_killed_reaped_receipted_no_next_case(self):
        self.exercise_controller()

    def test_controller_sigterm_and_sigint_forward_reap_and_final_receipt(self):
        import signal
        for sig in (signal.SIGTERM, signal.SIGINT):
            with self.subTest(signal=sig):
                self.exercise_controller(sig)


if __name__ == '__main__': unittest.main()
