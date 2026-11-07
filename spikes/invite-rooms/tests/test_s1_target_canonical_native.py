"""Opt-in E-2 only; current-source native target-connector canonical evidence."""
import asyncio
import json
import os
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gate_s1_native import NativeLab, native_signals


@unittest.skipUnless(os.environ.get('S1_NATIVE_E2_PACKET') == '1', 'explicit serial E-2 native packet only')
class NativeE2Tests(unittest.TestCase):
    def test_host_send_target_connector_canonical(self):
        lab = NativeLab()
        with native_signals(lab.report):
            try:
                lab.setup()
                asyncio.run(self.exercise(lab))
            except BaseException as exc:
                lab.report['error_type'] = type(exc).__name__
                raise
            finally:
                lab.close()
                lab.report['packet_scope'] = 'E-2 target-connector host-send canonical only'
                lab.report['packet_complete'] = bool(
                    lab.report.get('e2_exercise_complete') and not lab.report.get('error_type')
                    and all(c['ok'] for c in lab.report['checks'].values())
                    and lab.report.get('teardown_complete') is True
                    and lab.report.get('source_start') == lab.report.get('source_end'))
                lab.flush()
                print('E2_CANONICAL_REPORT=' + str(lab.out / 'report.json'), flush=True)
        self.assertTrue(lab.report['packet_complete'])

    async def exercise(self, lab):
        await lab.baseline()
        await lab.e_target_canonical_readback()
        rows = [json.loads(line) for line in lab.audit.read_text().splitlines()
                if json.loads(line).get('event') == 'lab_model_response']
        lab.report['lab_model_response_observations'] = rows
        lab.check('lab_model_thinking_disabled_observed', bool(rows) and all(
            r['reasoning_chars'] == 0 and r['reasoning_tokens'] in (None, 0)
            and r['visible_chars'] > 0 and r['request_extra_body'] ==
            {'chat_template_kwargs': {'enable_thinking': False}} for r in rows))
        lab.report['e2_exercise_complete'] = True


if __name__ == '__main__': unittest.main()
