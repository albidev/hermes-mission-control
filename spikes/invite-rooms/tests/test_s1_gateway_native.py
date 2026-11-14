"""Opt-in real D process restarts; serial cases under aggregate ownership."""
import asyncio
import json
import os
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gate_d_runtime import RestartLab, CASE_CHECKS
from gate_s1_native import native_signals


@unittest.skipUnless(os.environ.get('S1_NATIVE_D_PACKET') == '1', 'explicit serial D native packet only')
class NativeGatewayRestartTests(unittest.TestCase):
    def test_gateway_restart(self):
        case = os.environ['S1_NATIVE_D_CASE']
        self.assertIn(case, CASE_CHECKS)
        lab = RestartLab()
        original = lab.out
        lab.out = original.with_name('d-native-' + lab.stamp)
        original.rename(lab.out)
        lab.report.update(d_case=case, packet_complete=False, matrix_complete=False)
        with native_signals(lab.report):
            try:
                lab.setup()
                asyncio.run(self.exercise(lab, case))
                lab.report['d_exercise_complete'] = True
            except BaseException as exc:
                lab.report['error_type'] = type(exc).__name__
                raise
            finally:
                lab.close()
                lab.report['packet_scope'] = 'D gateway process restart ' + case + '; not whole S1'
                # NativeLab's historical scope strings are not acceptance criteria.
                lab.report['scope_not_yet_implemented'] = ['S1 aggregate E and cross-host F1 remain supervisor-owned']
                lab.report['packet_complete'] = bool(lab.report.get('d_exercise_complete')
                    and not lab.report.get('error_type') and lab.report['teardown_complete']
                    and lab.report['source_start'] == lab.report['source_end']
                    and all(v['ok'] is True for v in lab.report['checks'].values())
                    and all(lab.report['checks'].get(k, {}).get('ok') is True for k in CASE_CHECKS[case]))
                lab.flush()
                print('D_GATEWAY_REPORT=' + str(lab.out/'report.json'), flush=True)
        self.assertTrue(lab.report['packet_complete'])

    async def exercise(self, lab, case):
        await lab.baseline()
        await lab.restart_case(case)
        rows = [json.loads(line) for line in lab.audit.read_text().splitlines()
                if json.loads(line).get('event') == 'lab_model_response']
        lab.report['lab_model_response_observations'] = rows
        lab.check('lab_model_thinking_disabled_observed', bool(rows) and all(
            r['reasoning_chars'] == 0 and r['reasoning_tokens'] in (None, 0)
            and r['visible_chars'] > 0 and r['request_extra_body'] == lab.report['lab_model_request_overrides']
            for r in rows), response_count=len(rows))


if __name__ == '__main__': unittest.main()
