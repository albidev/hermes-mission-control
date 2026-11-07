"""C evidence-validator and signal regression tests; not native run evidence."""
import asyncio
import copy
import os
from pathlib import Path
import signal
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gate_s1_native as native


class CReworkTests(unittest.TestCase):
    def test_bounded_case_selection_preserves_full_predicates(self):
        import test_s1_relay_native as packet
        full = packet.required_checks('all')
        cases = ('canonical', 'relay', 'host', 'target', 'bounds')
        self.assertEqual(set(full), set().union(*(set(packet.required_checks(c)) for c in cases)))
        self.assertEqual(len(full), 18)
        with self.assertRaises(ValueError):
            packet.required_checks('typo')
        class FakeLab:
            def __init__(self): self.calls = []
            async def baseline(self): self.calls.append('baseline')
            async def target_connector_restart_checks(self): self.calls.append('target')
        lab = FakeLab()
        asyncio.run(packet.exercise_case(lab, 'target'))
        self.assertEqual(lab.calls, ['baseline', 'target'])
        checks = {name: {'ok': True} for name in packet.required_checks('target')}
        report = dict(checks=checks, teardown_complete=True, source_start={'x': 1},
                      source_end={'x': 1}, c_exercise_complete=True, c_case='target')
        packet.finish_packet_flags(report)
        self.assertTrue(report['case_complete'])
        self.assertTrue(report['packet_complete'])  # supervisor: explicitly scoped case packet
        checks.pop(next(iter(checks)))
        packet.finish_packet_flags(report)
        self.assertFalse(report['case_complete'])

    def test_lab_config_disables_omlx_thinking_via_custom_provider(self):
        import tempfile
        import yaml
        with tempfile.TemporaryDirectory(dir=native.ROOT, prefix='c-config-test-') as tmp:
            home = Path(tmp)
            (home / 'config.yaml').write_text('# isolated config fixture\n')
            native.gc.config(home, 18791, 'unit-only-placeholder', link=True)
            cfg = yaml.safe_load((home / 'config.yaml').read_text())
            provider = next((p for p in cfg.get('custom_providers', [])
                             if p.get('name') == 's1-lab-omlx'), None)
            self.assertIsNotNone(provider, 'missing lab-only oMLX request override')
            self.assertEqual(provider['base_url'], cfg['model']['base_url'])
            self.assertEqual(provider['extra_body'],
                             {'chat_template_kwargs': {'enable_thinking': False}})
            lab = native.NativeLab()
            self.assertEqual(lab.report['lab_model_request_overrides'], provider['extra_body'])

    def test_canonical_validator_rejects_baseline_only_and_missing_fault(self):
        evidence = dict(user_event_id='user-new', user_seq=3, run_id='run-new',
                        task_id='task-new', final='completed',
                        running_before_fault=[{'http': 200, 'run_id': 'run-new', 'status': 'running'}],
                        fault={'old_port': 7, 'new_port': 7}, receipts=[('run-new',)],
                        before=[{'seq': 1, 'event_id': 'baseline'}],
                        canonical=[{'seq': 1, 'event_id': 'baseline'},
                                   {'seq': 3, 'event_id': 'user-new'},
                                   {'seq': 4, 'event_id': 'reply-new'}],
                        db=[{'seq': 1, 'event_id': 'baseline'},
                            {'seq': 3, 'event_id': 'user-new'},
                            {'seq': 4, 'event_id': 'reply-new'}],
                        reply={'seq': 4, 'event_id': 'reply-new', 'kind': 'message.member',
                               'payload': {'task_id': 'task-new', 'member_id': 'peer-b'}})
        evidence['task_replies'] = [evidence['reply']]
        evidence['driver_continuity'] = dict(observer_baseline=1, window_start=1, window_end=2,
            events=[], lease_before=[dict(lease_generation=1)],
            lease_after=[dict(lease_generation=1)], task=[dict(indeterminate_at=None)])
        self.assertTrue(native.valid_c_canonical(evidence))
        for mutate in (
            lambda e: e.pop('driver_continuity'),
            lambda e: e.update(task_replies=[e['reply'], e['reply']]),
            lambda e: e.update(canonical=e['before'], db=e['before']),
            lambda e: e.update(running_before_fault=[]),
            lambda e: e.update(fault={}),
            lambda e: e.update(final='running'),
            lambda e: e['db'].reverse(),
            lambda e: e['reply']['payload'].update(task_id='other-task'),
        ):
            bad = copy.deepcopy(evidence)
            mutate(bad)
            self.assertFalse(native.valid_c_canonical(bad))

    def test_signals_unwind_through_finally_and_restore_handlers(self):
        for sig in (signal.SIGTERM, signal.SIGINT):
            old = signal.getsignal(sig)
            report = {}
            cleaned = []
            with self.assertRaises(native.NativeInterrupted):
                try:
                    with native.native_signals(report):
                        os.kill(os.getpid(), sig)
                finally:
                    cleaned.append(True)
            self.assertEqual(cleaned, [True])
            self.assertEqual(report['requested_signal'], signal.Signals(sig).name)
            self.assertEqual(signal.getsignal(sig), old)

    def test_packet_signal_cleanup_reaps_exact_owned_process_group(self):
        import json
        import select
        import subprocess
        script = '''import asyncio, os, subprocess, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
sys.path.insert(0, str(Path(sys.argv[1]) / 'tests'))
from gate_s1_native import NativeLab, processes
from test_s1_relay_native import run_packet
lab = NativeLab()
old = lab.out
lab.out = old.with_name('c-signal-' + lab.stamp)
old.rename(lab.out)
def setup():
    child = subprocess.Popen([sys.executable, '-B', '-c', 'import time; time.sleep(60)'], start_new_session=True)
    row = next(r for r in processes() if r['pid'] == child.pid)
    record = dict(row, role='cleanup-only-sleeper-not-native', signals=[])
    lab.owned.append((child, record))
    lab.report['processes'].append(record)
    print('READY=' + str(child.pid), flush=True)
lab.setup = setup
async def exercise(lab):
    await asyncio.sleep(60)
run_packet(lab, exercise)
'''
        for sig in (signal.SIGTERM, signal.SIGINT):
            proc = subprocess.Popen([sys.executable, '-B', '-c', script,
                                     str(Path(__file__).resolve().parents[1])],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                self.assertTrue(select.select([proc.stdout], [], [], 15)[0])
                ready = proc.stdout.readline().strip()
                self.assertTrue(ready.startswith('READY='), ready)
                child = int(ready.split('=')[1])
                proc.send_signal(sig)
                out, err = proc.communicate(timeout=40)
                self.assertNotEqual(proc.returncode, 0)
                path = next(line.split('=', 1)[1] for line in out.splitlines()
                            if line.startswith('C_RELAY_RESTART_REPORT='))
                report = json.loads(Path(path).read_text())
                self.assertEqual(report['requested_signal'], signal.Signals(sig).name)
                self.assertTrue(report['teardown_complete'])
                self.assertFalse(report['packet_complete'])
                self.assertEqual(report['owned_group_survivors'], [])
                self.assertEqual(report['processes'][0]['pid'], child)
                self.assertTrue(report['processes'][0]['terminated_on_request'])
                self.assertFalse(any(r['pid'] == child for r in native.processes()))
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
                proc.stdout.close()
                proc.stderr.close()

    def test_behavioral_slow_stream_reports_measured_timeout_and_concurrency(self):
        # Separate real WSS + pinned mTLS transport probe, no native identities/grants.
        result = native.behavioral_slow_stream_probe()
        self.assertTrue(result['timed_out'])
        self.assertTrue(result['concurrent_completed'])
        self.assertLessEqual(result['elapsed'], result['request_timeout'] + 0.75)
        self.assertGreaterEqual(result['elapsed'], result['request_timeout'] * 0.8)
        self.assertEqual(result['test_type'], 'behavioral_transport_not_native')
        self.assertTrue(result['cleanup_complete'])


if __name__ == '__main__':
    unittest.main()
