"""D evidence predicates: structural fixtures are NOT native evidence."""
import copy
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class RestartPredicateTests(unittest.TestCase):
    def test_rejects_unproven_restart_receipt_identity_and_canonical_changes(self):
        from gate_d_runtime import valid_restart
        pairs = [{'seq': 1, 'event_id': 'original'}, {'seq': 2, 'event_id': 'user'}]
        e = dict(role='host', case='host', running=[dict(http=200, status='running', run_id='run', execution_generation=3)],
                 run_id='run', task_id='task', dispatch_generation=1,
                 old_process=dict(pid=10, pgid=10, birth='old', returncode=1, terminated_on_request=True),
                 new_process=dict(pid=11, pgid=11, birth='new', alive=True),
                 stable_before={'install': 'a', 'route': 'b'}, stable_after={'install': 'a', 'route': 'b'},
                 authority_before={'mint_count': 2}, authority_after={'mint_count': 2},
                 receipts_before=[['run']], receipts_after=[['run']], replay_http=200,
                 replay={'run_id': 'run', 'replayed': True}, before=copy.deepcopy(pairs), canonical=copy.deepcopy(pairs), db=copy.deepcopy(pairs),
                 user={'seq': 2, 'event_id': 'user'}, observed_outcome={'http': 200, 'status': 'completed', 'run_id': 'run'},
                 driver_tasks=[{'task_id': 'task', 'execution_generation': 1}])
        e['old_process'].update(fault_case='host', fault_identity_verified=dict(pid=10, pgid=10, birth='old'),
                                signals=[dict(signal='SIGTERM')])
        self.assertTrue(valid_restart(e))
        for defect in ('wrong_case', 'missing_identity', 'kill_in_graceful', 'extra_signal'):
            bad = copy.deepcopy(e)
            if defect == 'wrong_case': bad['case'] = 'target_crash_restart'
            if defect == 'missing_identity': bad['old_process'].pop('fault_identity_verified')
            if defect == 'kill_in_graceful': bad['old_process']['signals'] = [dict(signal='SIGKILL')]
            if defect == 'extra_signal': bad['old_process']['signals'].append(dict(signal='SIGKILL'))
            self.assertFalse(valid_restart(bad), defect)
        for defect in ('not_running','same_pid','not_stopped','identity','authority','extra_receipt','new_run','ids','db','user','new_task','missing_outcome'):
            row = copy.deepcopy(e)
            if defect == 'not_running': row['running'][-1]['status'] = 'queued'
            if defect == 'same_pid': row['new_process']['pid'] = 10
            if defect == 'not_stopped': row['old_process']['returncode'] = None
            if defect == 'identity': row['stable_after']['route'] = 'changed'
            if defect == 'authority': row['authority_after']['mint_count'] = 3
            if defect == 'extra_receipt': row['receipts_after'].append(['other'])
            if defect == 'new_run': row['replay']['run_id'] = 'other'
            if defect == 'ids': row['canonical'][0]['event_id'] = 'new'
            if defect == 'db': row['db'].reverse()
            if defect == 'user': row['user']['event_id'] = 'missing'
            if defect == 'new_task': row['driver_tasks'].append({'task_id': 'other', 'execution_generation': 1})
            if defect == 'missing_outcome': row['observed_outcome'] = {}
            self.assertFalse(valid_restart(row), defect)
        row = copy.deepcopy(e)
        row['role'] = 'target'
        row['observed_outcome'] = {'http': 404, 'code': 'run_not_found', 'classification': 'lost'}
        self.assertFalse(valid_restart(row), 'sanitizer-only evidence cannot prove target outcome')

    def test_target_truthful_outcome_requires_raw_native_and_fresh_control(self):
        from gate_d_runtime import target_process_restart_truthful_outcome
        pairs = [{'seq': 1, 'event_id': 'original'}, {'seq': 2, 'event_id': 'user'},
                 {'seq': 3, 'event_id': 'fresh-user'}, {'seq': 4, 'event_id': 'fresh-reply'}]
        receipt = [{'run_id': 'run', 'idempotency_key': 'key', 'task_id': 'task', 'execution_generation': 1}]
        e = dict(run_id='run', task_id='task', dispatch_generation=1, outcome_class='indeterminate',
                 raw_native=[dict(provenance='native_backend_before_sanitizer', method='GET', path='/v1/runs/run',
                                  http=200, run_id='run', status='unknown'),
                             dict(provenance='native_backend_before_sanitizer', method='POST', path='/v1/runs',
                                  http=503, error_type='server_error', error_code='run_execution_unknown', request_run_id='run')],
                 native_before_replay=receipt, native_after_replay=copy.deepcopy(receipt),
                 canonical_reply_count=0, canonical_events=[], canonical=copy.deepcopy(pairs), db=copy.deepcopy(pairs),
                 replay={'error': 'sanitized'}, replay_http=502,
                 fresh=dict(run_id='fresh-run', task_id='fresh-task', status='completed', receipts=[['fresh-run']],
                            user=pairs[2], reply=dict(pairs[3], kind='message.member',
                                  payload=dict(task_id='fresh-task', member_id='peer-b'))))
        self.assertTrue(target_process_restart_truthful_outcome(e))
        completed = copy.deepcopy(e)
        completed['outcome_class'] = 'completed'
        completed['raw_native'][0]['status'] = 'completed'
        completed['raw_native'][1].update(http=202, run_id='run', replayed=True)
        completed['canonical_reply_count'] = 1
        completed['canonical_events'] = [dict(seq=2.5, event_id='original-reply', kind='message.member',
            payload=dict(task_id='task', member_id='peer-b'))]
        completed['canonical'].insert(2, dict(seq=2.5, event_id='original-reply'))
        completed['db'] = copy.deepcopy(completed['canonical'])
        self.assertTrue(target_process_restart_truthful_outcome(completed), 'completed drain is a truthful outcome')
        for defect in ('second_receipt', 'new_replay_id', 'missing_reply', 'duplicate_reply', 'wrong_member', 'sanitizer_only'):
            bad = copy.deepcopy(completed)
            if defect == 'second_receipt': bad['native_after_replay'].append(dict(run_id='other'))
            if defect == 'new_replay_id': bad['raw_native'][1]['run_id'] = 'other'
            if defect == 'missing_reply': bad['canonical_events'] = []
            if defect == 'duplicate_reply': bad['canonical_events'] *= 2
            if defect == 'wrong_member': bad['canonical_events'][0]['payload']['member_id'] = 'other'
            if defect == 'sanitizer_only': bad['raw_native'][0]['provenance'] = 'adapter'
            self.assertFalse(target_process_restart_truthful_outcome(bad), defect)
        for defect in ('second_receipt','new_run','new_generation','forged_reply','sanitizer_only',
                       'wrong_status_run','raw_replay_other_run','raw_replay_missing','fresh_failed',
                       'fresh_old_key','fresh_reply_missing','fresh_wrong_task','fresh_db_order'):
            bad = copy.deepcopy(e)
            if defect == 'second_receipt': bad['native_after_replay'].append(dict(receipt[0], run_id='new'))
            if defect == 'new_run': bad['native_after_replay'][0]['run_id'] = 'new'
            if defect == 'new_generation': bad['native_after_replay'][0]['execution_generation'] = 2
            if defect == 'forged_reply': bad['canonical_events'].append(dict(kind='message.member', payload=dict(task_id='task')))
            if defect == 'sanitizer_only': bad['raw_native'][0]['provenance'] = 'adapter'
            if defect == 'wrong_status_run': bad['raw_native'][0]['run_id'] = 'other'
            if defect == 'raw_replay_other_run': bad['raw_native'][1]['run_id'] = 'other'
            if defect == 'raw_replay_missing': bad['raw_native'].pop()
            if defect == 'fresh_failed': bad['fresh']['status'] = 'failed'
            if defect == 'fresh_old_key': bad['fresh']['run_id'] = 'run'
            if defect == 'fresh_reply_missing': bad['canonical'].pop()
            if defect == 'fresh_wrong_task': bad['fresh']['reply']['payload']['task_id'] = 'other'
            if defect == 'fresh_db_order': bad['db'].reverse()
            self.assertFalse(target_process_restart_truthful_outcome(bad), defect)


class KnownCoreLimitationTests(unittest.TestCase):
    def test_known_limitation_real_peer_cancelled_history_omits_terminal(self):
        """Known limitation, NOT skipped and NOT a native gate receipt."""
        from unittest.mock import patch
        import io
        import json
        core = '/Users/albi/.hermes/hermes-agent'
        sys.path.insert(0, core)
        from tui_gateway.hosted_room_peer_http import PeerRunsHTTPClient
        client = PeerRunsHTTPClient(base_url='http://127.0.0.1:18791', api_key='')
        client._runs[('unit-task', 1)] = dict(run_id='unit-run', task_id='unit-task', execution_generation=1,
            room_id='unit-room', target_profile='unit-profile', session_id='unit-session')
        client.bind_observation(task_id='unit-task', execution_generation=1)
        kw = dict(room_id='unit-room', profile='unit-profile', session_id='unit-session', grant='unit-only-grant')
        with patch('hermes_cli.urllib_security.open_credentialed_url',
                   side_effect=lambda *a, **k: io.BytesIO(json.dumps(dict(run_id='unit-run', status='cancelled')).encode())) as transport:
            self.assertEqual(client.history(**kw), [])
            status = client.status(**kw)
            self.assertEqual(status['status'], 'cancelled')
            self.assertFalse(status['active'])
            self.assertEqual(status['task_id'], 'unit-task')
            self.assertEqual(transport.call_count, 1, 'cancelled is terminal-cached but omitted from history')

    def test_exact_cancelled_wedge_classification_not_generic_failure(self):
        from gate_d_runtime import target_process_restart_truthful_outcome
        pairs = [dict(seq=1, event_id='old'), dict(seq=2, event_id='fresh')]
        receipt = [dict(run_id='run')]
        e = dict(case='target_graceful_restart', outcome_class='blocked_core_cancelled_history', run_id='run', task_id='task',
            raw_native=[dict(provenance='native_backend_before_sanitizer', method='GET', path='/v1/runs/run',
                http=200, run_id='run', status='cancelled'), dict(provenance='native_backend_before_sanitizer',
                method='POST', path='/v1/runs', request_run_id='run', http=202, run_id='run', replayed=True)],
            native_before_replay=receipt, native_after_replay=copy.deepcopy(receipt), canonical_reply_count=0,
            canonical_events=[], canonical=copy.deepcopy(pairs), db=copy.deepcopy(pairs),
            fresh=dict(status='blocked_core_cancelled_history', user=pairs[1], observed_seconds=55,
                driver_tasks=[], original_driver=[dict(task_id='task', status='running', execution_generation=1)]), dispatch_generation=1)
        self.assertTrue(target_process_restart_truthful_outcome(e))
        for defect in ('raw', 'new_receipt', 'forged_reply', 'fresh_task', 'host_not_running', 'canonical', 'bound', 'wrong_case'):
            bad = copy.deepcopy(e)
            if defect == 'raw': bad['raw_native'][0]['provenance'] = 'adapter'
            if defect == 'new_receipt': bad['native_after_replay'].append(dict(run_id='other'))
            if defect == 'forged_reply': bad['canonical_events'] = [dict(kind='message.member', payload=dict(task_id='task'))]
            if defect == 'fresh_task': bad['fresh']['driver_tasks'] = [dict(task_id='new')]
            if defect == 'host_not_running': bad['fresh']['original_driver'][0]['status'] = 'failed'
            if defect == 'canonical': bad['db'] = []
            if defect == 'bound': bad['fresh']['observed_seconds'] = 0
            if defect == 'wrong_case': bad['case'] = 'target_crash_restart'
            self.assertFalse(target_process_restart_truthful_outcome(bad), defect)


class RestartHarnessTests(unittest.TestCase):
    def test_separate_graceful_and_crash_cases_required(self):
        from gate_d_runtime import CASE_CHECKS
        self.assertEqual(list(CASE_CHECKS), ['host', 'target_graceful_restart', 'target_crash_restart'])
        self.assertEqual(len({n for names in CASE_CHECKS.values() for n in names}), 3)

    def test_exact_owned_crash_is_immediate_kill_and_first_receipt_survives(self):
        from gate_d_runtime import RestartLab, processes
        import subprocess
        lab = object.__new__(RestartLab)
        proc = subprocess.Popen([sys.executable, '-B', '-c', 'import time; time.sleep(60)'], start_new_session=True)
        try:
            identity = next(r for r in processes() if r['pid'] == proc.pid)
            record = dict(identity, signals=[])
            lab.owned = [(proc, record)]
            lab.flush = lambda: None
            lab.fault_process(proc, 'target_crash_restart')
            self.assertEqual(proc.returncode, -9)
            self.assertEqual(record['returncode'], -9)
            self.assertTrue(record['terminated_on_request'])
            self.assertEqual([s['signal'] for s in record['signals']], ['SIGKILL'])
            self.assertEqual(record['fault_identity_verified'], identity)
            original = copy.deepcopy(record)
            lab.terminate(proc)
            self.assertEqual(record, original)
        finally:
            if proc.poll() is None: proc.kill()
            proc.wait()

    def test_fault_refuses_wrong_birth_or_pgid_before_signal(self):
        from gate_d_runtime import RestartLab
        from unittest.mock import Mock, patch
        for field in ('birth', 'pgid'):
            lab = object.__new__(RestartLab)
            proc = Mock(pid=10)
            proc.poll.return_value = None
            original = dict(pid=10, pgid=10, birth='old')
            lab.owned = [(proc, dict(original, signals=[]))]
            current = dict(original, **{field: 'changed'})
            with patch('gate_d_runtime.processes', return_value=[current]):
                with self.assertRaises(RuntimeError): lab.fault_process(proc, 'target_crash_restart')
            proc.kill.assert_not_called()
            proc.terminate.assert_not_called()

    def test_raw_projection_excludes_messages_output_and_credentials(self):
        from gate_d_runtime import raw_native_projection
        from room_firewall import UpstreamResponse, ForwardedRequest
        import json
        request = ForwardedRequest('GET', '/v1/runs/run', (), b'')
        response = UpstreamResponse(200, (), json.dumps(dict(run_id='run', status='unknown',
            grant='SECRET', output='SECRET', error=dict(type='server_error', code='run_execution_unknown', message='SECRET'))).encode())
        row = raw_native_projection(request, response)
        self.assertEqual(row['provenance'], 'native_backend_before_sanitizer')
        self.assertEqual(row['status'], 'unknown')
        self.assertNotIn('SECRET', json.dumps(row))
        self.assertEqual(row['error_code'], 'run_execution_unknown')

    def test_host_single_use_token_is_rearmed_same_value_private(self):
        from gate_d_runtime import RestartLab, NativeLab, gc
        from unittest.mock import patch
        import tempfile
        import stat
        with tempfile.TemporaryDirectory(dir=gc.ROOT, prefix='d-token-unit-') as tmp:
            lab = object.__new__(RestartLab)
            lab.home = Path(tmp)
            token = lab.home/'handoff'/'session.token'
            lab.args = {'host': ['--ssh-session-token-file', str(token)]}
            lab.keys = ['unused', 'unused', 'UNIT-SECRET-NOT-NATIVE']
            with patch.object(NativeLab, 'spawn', return_value='unit-no-process'):
                for _ in range(2):
                    self.assertEqual(lab.spawn('host'), 'unit-no-process')
                    self.assertEqual(token.read_text(), lab.keys[2])
                    self.assertEqual(stat.S_IMODE(token.stat().st_mode), 0o600)
                    token.unlink()  # supported native serve consumes the handoff

    def test_first_termination_receipt_survives_close_revisit(self):
        from gate_d_runtime import RestartLab, NativeLab
        from unittest.mock import Mock, patch
        lab = object.__new__(RestartLab)
        proc = Mock()
        proc.poll.return_value = -9
        record = dict(returncode=-9, returncode_before_requested_termination=None,
                      terminated_on_request=True, signals=[{'signal': 'SIGTERM'}, {'signal': 'SIGKILL'}])
        initial = copy.deepcopy(record)
        lab.owned = [(proc, record)]
        with patch.object(NativeLab, 'terminate') as parent:
            lab.terminate(proc)
            lab.terminate(proc)
            parent.assert_not_called()
        self.assertEqual(record, initial)


class FreshTimeoutLabelTests(unittest.IsolatedAsyncioTestCase):
    async def test_generic_fresh_timeout_is_not_a_core_limitation(self):
        from gate_d_runtime import RestartLab
        from unittest.mock import AsyncMock, patch
        lab = object.__new__(RestartLab)
        lab.scope = {'room_id': 'unit-room'}
        lab.stamp = 'unit'
        lab.home = Path('/unused-unit-home')
        lab.report = {}
        lab.flush = lambda: None
        user = dict(seq=7, event_id='unit-fresh')
        with patch('gate_d_runtime.gc.rpc', new=AsyncMock(return_value={'event': user})), \
             patch('gate_d_runtime.gc.read_db', return_value=[]), \
             patch('gate_d_runtime.time') as clock:
            clock.monotonic.side_effect = [0, 56, 56]
            fresh = await lab.fresh_control(None, 'unit-task', 1)
        self.assertEqual(fresh['status'], 'not_completed')
        self.assertEqual(fresh['user'], user)


class AggregateRejectTests(unittest.TestCase):
    def test_aggregate_rejects_bad_case_receipts_sources_cleanup_and_coverage(self):
        from gate_d_aggregate import validate_cases, CASE_CHECKS
        from unittest.mock import patch
        override = {'chat_template_kwargs': {'enable_thinking': False}}
        reports = [dict(d_case=case, packet_complete=True, teardown_complete=True,
            source_start={'frozen': 'sha'}, source_end={'frozen': 'sha'}, cleanup_failures=[], owned_group_survivors=[],
            ports_free_after={'18790': True, '18791': True},
            checks={n: {'ok': True} for n in CASE_CHECKS[case]}, d_evidence={'unit': True, 'case': case},
            lab_model_request_overrides=override, lab_model_response_observations=[dict(reasoning_chars=0,
                reasoning_tokens=0, visible_chars=10, request_extra_body=override)]) for case in CASE_CHECKS]
        receipts = [dict(case=c, exit_code=0, wall_seconds=200) for c in CASE_CHECKS]
        # Only the structural envelope is under test here. Raw predicates have
        # independent negative tests above; these are never native evidence.
        with patch('gate_d_aggregate.valid_restart', return_value=True):
            base = validate_cases(reports, receipts, {'frozen':'sha'}, {'frozen':'sha'})
            self.assertTrue(base['packet_complete'])
            self.assertEqual(base['known_core_limitations'], [])
            limited = copy.deepcopy(reports)
            limited[1]['d_evidence']['outcome_class'] = 'blocked_core_cancelled_history'
            classified = validate_cases(limited, receipts, {'frozen':'sha'}, {'frozen':'sha'})
            self.assertTrue(classified['packet_complete'])
            self.assertEqual(classified['known_core_limitations'][0]['case'], 'target_graceful_restart')
            self.assertFalse(classified['known_core_limitations'][0]['fresh_send_completed'])
            for defect in ('missing_case','missing_receipt','source','cleanup','busy_port','exit','timeout','coverage','model'):
                rr, rc = copy.deepcopy(reports), copy.deepcopy(receipts)
                if defect == 'missing_case': rr.pop()
                if defect == 'missing_receipt': rc.pop()
                if defect == 'source': rr[1]['source_end'] = {'frozen':'other'}
                if defect == 'cleanup': rr[1]['owned_group_survivors'] = [{'pid':1}]
                if defect == 'busy_port': rr[1]['ports_free_after']['18791'] = False
                if defect == 'exit': rc[1]['exit_code'] = 1
                if defect == 'timeout': rc[1]['wall_seconds'] = 361
                if defect == 'coverage': rr[1]['checks'] = {}
                if defect == 'model': rr[1]['lab_model_response_observations'] = []
                self.assertFalse(validate_cases(rr, rc, {'frozen':'sha'}, {'frozen':'sha'})['packet_complete'], defect)

    def test_missing_raw_evidence_is_rejected_not_promoted_from_packet_flag(self):
        from gate_d_aggregate import validate_cases
        report = dict(d_case='host', packet_complete=True)
        result = validate_cases([report], [dict(case='host', exit_code=0, wall_seconds=10)], {}, {})
        self.assertFalse(result['packet_complete'])
        self.assertIn('host:invalid_raw_restart_evidence', result['errors'])


if __name__ == '__main__': unittest.main()
