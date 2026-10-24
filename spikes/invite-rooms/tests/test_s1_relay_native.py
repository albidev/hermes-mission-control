"""Opt-in C packet: serial fresh native cases, never aggregate S1 acceptance."""
import asyncio
import json
import os
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gate_s1_native import NativeLab, native_signals

CASE_CHECKS = {
    'canonical': ['canonical_host_send_exact_seq_event_id_after_C'],
    'relay': ['relay_restarted_same_port', 'relay_restart_same_run_same_receipt',
              'relay_restart_no_authority_change', 'relay_offline_no_replay_no_hidden_post',
              'relay_back_online_new_dispatch_succeeds', 'relay_restart_runs_completed'],
    'host': ['host_connector_restarted', 'host_connector_restart_same_run_same_receipt',
             'host_connector_restart_no_authority_change',
             'host_connector_lost_admission_same_coordinates',
             'host_connector_lost_admission_replay_same_run'],
    'target': ['target_connector_restarted', 'target_connector_restart_same_run_same_receipt',
               'target_connector_restart_no_authority_change'],
    'bounds': ['unrelated_room_scope_rejected_before_target',
               'connector_oversize_send_rejected_locally',
               'connector_request_timeout_measured_concurrent_stream'],
}
CASE_METHODS = dict(zip(CASE_CHECKS, ('c_canonical_readback', 'relay_restart_checks',
    'host_connector_restart_checks', 'target_connector_restart_checks', 'unrelated_link_bounds_checks')))


def required_checks(case):
    if case == 'all':
        return [name for names in CASE_CHECKS.values() for name in names]
    if case not in CASE_CHECKS:
        raise ValueError('unknown C case')
    return list(CASE_CHECKS[case])


async def exercise_case(lab, case):
    required_checks(case)  # reject typo before lab or model activity
    await lab.baseline()
    for name in (CASE_METHODS if case == 'all' else [case]):
        await getattr(lab, CASE_METHODS[name])()


def finish_packet_flags(report):
    required = required_checks(report.get('c_case', 'all'))
    checks = report.get('checks', {})
    report['case_complete'] = (
        not report.get('error_type') and bool(report.get('c_exercise_complete'))
        and all(checks.get(name, {}).get('ok') is True for name in required)
        and bool(checks) and all(check.get('ok') is True for check in checks.values())
        and report.get('teardown_complete') is True
        and report.get('source_start') == report.get('source_end'))
    # Supervisor-authorized semantics: packet here is explicitly ONE case;
    # only the separate aggregate validator may claim all18 coverage.
    report['packet_complete'] = report['case_complete']


def run_packet(lab, exercise):
    with native_signals(lab.report):
        try:
            lab.setup()
            asyncio.run(exercise(lab))
        except BaseException as exc:
            lab.report['error_type'] = type(exc).__name__
            raise
        finally:
            lab.close()
            lab.report['packet_scope'] = 'C case ' + lab.report.get('c_case', 'all') + '; D/E incomplete'
            finish_packet_flags(lab.report)
            lab.flush()
            print('C_RELAY_RESTART_REPORT=' + str(lab.out / 'report.json'), flush=True)


@unittest.skipUnless(os.environ.get('S1_NATIVE_C_PACKET') == '1', 'explicit serial C native packet only')
class NativeCRelayRestartTests(unittest.TestCase):
    def test_relay_connector_restart_and_multi_link_bounds(self):
        case = os.environ.get('S1_NATIVE_C_CASE', 'all')
        required_checks(case)
        lab = NativeLab()
        original = lab.out
        lab.out = original.with_name('c-native-' + lab.stamp)
        original.rename(lab.out)
        lab.report['c_case'] = case
        run_packet(lab, self.exercise)
        self.assertTrue(lab.report['packet_complete'])
        self.assertFalse(lab.report['matrix_complete'])

    async def exercise(self, lab):
        case = lab.report['c_case']
        await exercise_case(lab, case)
        required = required_checks(case)
        for name in required:
            self.assertIn(name, lab.report['checks'], f'missing check: {name}')
            self.assertTrue(lab.report['checks'][name]['ok'], f'failed check: {name}')
        model_rows = [json.loads(line) for line in lab.audit.read_text().splitlines()
                      if json.loads(line).get('event') == 'lab_model_response']
        lab.report['lab_model_response_observations'] = model_rows
        lab.check('lab_model_thinking_disabled_observed', bool(model_rows)
                  and all(row['reasoning_chars'] == 0
                          and row['reasoning_tokens'] in (None, 0)
                          and row['visible_chars'] > 0
                          and row['request_extra_body'].get('chat_template_kwargs') == {'enable_thinking': False}
                          for row in model_rows),
                  response_count=len(model_rows),
                  token_count_caveat='None means server omitted usage field; decoded reasoning content measured separately')
        lab.report['c_required_checks'] = required
        lab.report['c_exercise_complete'] = True


if __name__ == '__main__':
    unittest.main()
