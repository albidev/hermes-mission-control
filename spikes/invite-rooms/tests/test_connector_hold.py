"""Behavioral loopback transport fixtures; no native identities/admissions.

Both real firewalls, WSS relay and pinned mTLS connectors remain in the path.
"""
import asyncio
import json
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import connector
from gate_f1_local import NetworkTransport
from room_firewall import RoomFirewall, Request, UpstreamResponse
from test_net_loopback import SCOPE, GRANT, _dispatch_body


class ConnectorHoldTests(unittest.TestCase):
    def setUp(self):
        self.transport = t = NetworkTransport()
        self.seen = []
        self.admissions = {}
        self.entered = threading.Event()
        self.release = threading.Event()
        self.block_first = False
        self.errors = []
        self.workers = []
        t.start()
        t.native_receipt_db = t.root / 'unused-fixture.db'
        # Fixture-only run ownership, not a native admission or allowlist change.
        fw = RoomFirewall(expected_scope=SCOPE, backend=lambda _: None,
                          verify_native=lambda run, scope: run == 'run_fixture')
        t.bind('hold-fixture', fw, target_url='http://127.0.0.1:1', scope=SCOPE)
        t.target.firewall = RoomFirewall(expected_scope=SCOPE, backend=self.backend,
                                        verify_native=lambda run, scope: run == 'run_fixture')
        self.fw = fw
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.release.set()
        for thread in self.workers:
            thread.join(15)
            self.assertFalse(thread.is_alive())
        self.transport.close()
        self.assertEqual(self.errors, [])

    def backend(self, request):
        self.seen.append(request)
        if request.method == 'POST':
            key = dict(request.headers)['idempotency-key']
            self.admissions.setdefault(key, 'run_fixture')
        if self.block_first and len(self.seen) == 1:
            self.entered.set()
            if not self.release.wait(12):
                raise TimeoutError('fixture_release_missing')
        body = json.dumps({'run_id': 'run_fixture', 'status': 'running',
                           'replayed': len(self.seen) > 1}).encode()
        return UpstreamResponse(200, (('content-type', 'application/json'),), body)

    def thread(self, fn):
        def run():
            try:
                fn()
            except BaseException as exc:
                self.errors.append(type(exc).__name__)
            finally:
                self.release.set()
        worker = threading.Thread(target=run)
        self.workers.append(worker)
        worker.start()

    def call(self, post=False):
        headers = (('Authorization', GRANT), ('Content-Type', 'application/json'))
        if post:
            headers += (('Idempotency-Key', 'room:dtask:net:1'),)
        return self.fw.forward(Request('POST' if post else 'GET',
            '/v1/runs' if post else '/v1/runs/run_fixture', headers,
            _dispatch_body() if post else b''))

    def fault(self, target=False):
        self.assertTrue(self.entered.wait(5))
        t = self.transport
        if target:
            old_sessions = t.target_client.sessions
            asyncio.run_coroutine_threadsafe(t.target_client._ws.close(),
                                              t.target_client._loop).result(3)
            deadline = time.monotonic() + 7
            while t.target_client.sessions == old_sessions and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertGreater(t.target_client.sessions, old_sessions)
        else:
            port = t.daemon.bound_port
            t.stop_relay()
            time.sleep(.25)
            t.start_relay(port)

    def test_relay_down_at_open_then_returns_inside_hold(self):
        t = self.transport
        port = t.daemon.bound_port
        t.stop_relay()
        self.assertFalse(t.host_client.connected)
        self.thread(lambda: (time.sleep(.3), t.start_relay(port)))
        started = time.monotonic()
        self.assertEqual(self.call().status, 200)
        self.assertLess(time.monotonic() - started, 11)

    def test_relay_restart_during_inflight_status(self):
        self.block_first = True
        self.thread(self.fault)
        self.assertEqual(self.call().status, 200)
        self.assertGreaterEqual(len(self.seen), 2)

    def test_target_connector_reconnect_during_status(self):
        self.block_first = True
        self.thread(lambda: self.fault(target=True))
        self.assertEqual(self.call().status, 200)
        self.assertGreaterEqual(len(self.seen), 2)

    def test_dispatch_resend_identical_key_body_one_admission(self):
        self.block_first = True
        self.thread(self.fault)
        self.assertEqual(self.call(post=True).status, 200)
        self.assertGreaterEqual(len(self.seen), 2)
        self.assertTrue(all(r == self.seen[0] for r in self.seen))
        self.assertEqual(self.admissions, {'room:dtask:net:1': 'run_fixture'})

    def test_outage_exceeds_hold_fails_closed_in_ten_seconds(self):
        self.transport.stop_relay()
        started = time.monotonic()
        self.assertEqual(self.call().status, 503)
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 9.8)
        self.assertLess(elapsed, 11)


if __name__ == '__main__':
    unittest.main()
