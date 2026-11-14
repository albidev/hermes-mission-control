"""Real peer client through the room firewall; transport fixture, not native evidence."""
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, '/Users/albi/.hermes/hermes-agent')
from room_firewall import RoomFirewall, Request, UpstreamResponse, FirewallDenied, UpstreamAmbiguous
from test_room_firewall import SCOPE, dispatch_body
from tui_gateway.hosted_room_peer_http import PeerRunsHTTPClient

HEADERS = (('authorization', 'HermesRoom unit-only'), ('content-type', 'application/json'))

class UnknownRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.native = dict(run_id='unit-run', status='unknown', admission_id='unit-admission', execution_generation=3)
        self.bound = True
        def backend(req):
            self.calls.append(req)
            if req.method == 'GET':
                return UpstreamResponse(200, body=json.dumps(self.native).encode())
            return UpstreamResponse(200, body=b'{"run_id":"unit-run","status":"interrupted"}')
        self.firewall = RoomFirewall(expected_scope=SCOPE, backend=backend,
            verify_native=lambda run, scope: self.bound and run == 'unit-run' and scope == SCOPE)

    def test_real_client_resolves_exact_runtime_admission_through_firewall(self):
        client = PeerRunsHTTPClient(base_url='http://127.0.0.1:18791', api_key='')
        record = dict(run_id='unit-run', task_id='task', execution_generation=1,
            room_id=SCOPE['room_id'], target_profile=SCOPE['target_profile'], session_id='session')
        client._runs[('task', 1)] = record
        client.bind_observation(task_id='task', execution_generation=1)
        def transport(req, **kw):
            response = self.firewall.forward(Request(req.get_method(), req.selector,
                tuple((k.lower(),v) for k,v in req.header_items() if k.lower() != 'user-agent'), req.data or b''))
            return io.BytesIO(response.body)
        with patch('hermes_cli.urllib_security.open_credentialed_url', side_effect=transport):
            history = client.history(room_id=SCOPE['room_id'], profile=SCOPE['target_profile'],
                session_id='session', grant='unit-only')
        self.assertEqual(history[0]['status'], 'failed')
        self.assertEqual(history[0]['content'], 'member gateway restarted')
        posts = [r for r in self.calls if r.method == 'POST']
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0].path, '/v1/runs/unit-run/resolve-unknown')
        self.assertEqual(json.loads(posts[0].body), dict(admission_id='unit-admission', execution_generation=3))
        self.assertEqual(record['execution_generation'], 1, 'dispatch domain is NOT runtime domain')

    def test_resolve_denies_wrong_binding_schema_or_native_coordinate(self):
        good = dict(admission_id='unit-admission', execution_generation=3)
        for bad in ({}, dict(good, extra=True), dict(good, admission_id='other'),
                    dict(good, execution_generation=1), dict(good, execution_generation=True),
                    dict(good, admission_id=None)):
            with self.subTest(bad=bad):
                self.calls.clear()
                with self.assertRaises(FirewallDenied):
                    self.firewall.forward(Request('POST', '/v1/runs/unit-run/resolve-unknown', HEADERS, json.dumps(bad).encode()))
                self.assertFalse(any(r.method == 'POST' for r in self.calls))
        self.calls.clear()
        with self.assertRaises(FirewallDenied):
            self.firewall.forward(Request('POST', '/v1/runs/other/resolve-unknown', HEADERS, json.dumps(good).encode()))
        self.assertEqual(self.calls, [])
        self.bound = False
        self.calls.clear()
        with self.assertRaises(FirewallDenied):
            self.firewall.forward(Request('POST', '/v1/runs/unit-run/resolve-unknown', HEADERS, json.dumps(good).encode()))
        self.assertEqual(self.calls, [])

    def test_unknown_ack_requires_true_replay_of_already_bound_run(self):
        acks = []
        reply = dict(run_id='unit-run', status='unknown', replayed=True)
        firewall = RoomFirewall(expected_scope=SCOPE,
            backend=lambda r: UpstreamResponse(202, body=json.dumps(reply).encode()),
            verify_native=lambda run, scope: self.bound and run == 'unit-run' and scope == SCOPE,
            on_admitted=lambda *a: acks.append(a))
        request = Request('POST', '/v1/runs', HEADERS+(('idempotency-key','room:task-1:1'),), dispatch_body())
        # Scope-only callbacks cannot prove an immutable dispatch. Fail closed.
        with self.assertRaises(UpstreamAmbiguous): firewall.forward(request)
        for defect in ('unbound', 'new', 'wrong_run'):
            self.bound = defect != 'unbound'
            reply.update(replayed=defect != 'new', run_id='other' if defect == 'wrong_run' else 'unit-run')
            with self.assertRaises(UpstreamAmbiguous): firewall.forward(request)
        self.assertEqual(len(acks), 0, 'unknown replay must never enroll a new binding')

    def test_unknown_ack_cannot_substitute_another_valid_run_in_same_scope(self):
        import tempfile
        from run_bindings import HostRunBindings
        with tempfile.TemporaryDirectory() as directory:
            store = HostRunBindings(Path(directory) / 'host.db')
            reply = dict(run_id='task-1-run', status='started', replayed=False)
            firewall = RoomFirewall(expected_scope=SCOPE,
                backend=lambda r: UpstreamResponse(202, body=json.dumps(reply).encode()),
                verify_native=store, on_admitted=store.record_ack)
            def request(task, generation=1, key=None):
                return Request('POST', '/v1/runs', HEADERS +
                    (('idempotency-key', key or f'room:{task}:{generation}'),),
                    dispatch_body(task_id=task, execution_generation=generation))
            firewall.forward(request('task-1'))
            reply['run_id'] = 'task-2-run'
            firewall.forward(request('task-2', 99))
            self.assertTrue(store('task-1-run', SCOPE))
            self.assertTrue(store('task-2-run', SCOPE))
            before = store.path.read_bytes()
            reply.update(status='unknown', replayed=True)
            with self.assertRaises(UpstreamAmbiguous):
                firewall.forward(request('task-1'))
            reply['run_id'] = 'task-1-run'
            self.assertEqual(firewall.forward(request('task-1')).status, 202)
            for req in (request('task-1', 2), request('task-2'),
                        request('task-1', key='another-key')):
                with self.assertRaises(UpstreamAmbiguous): firewall.forward(req)
            self.assertEqual(before, store.path.read_bytes(), 'unknown never enrolls')

    def test_admission_only_exposed_for_unknown_run_status(self):
        for status in ('unknown', 'running', 'queued', 'completed', 'cancelled'):
            self.native['status'] = status
            result = json.loads(self.firewall.forward(Request('GET', '/v1/runs/unit-run', HEADERS)).body)
            self.assertEqual('admission_id' in result, status == 'unknown')
        wrong_scope = dict(SCOPE, room_id='other')
        firewall = RoomFirewall(expected_scope=wrong_scope, backend=self.firewall._backend,
            verify_native=lambda run, scope: run == 'unit-run' and scope == SCOPE)
        with self.assertRaises(FirewallDenied):
            firewall.forward(Request('POST', '/v1/runs/unit-run/resolve-unknown', HEADERS,
                b'{"admission_id":"unit-admission","execution_generation":3}'))
