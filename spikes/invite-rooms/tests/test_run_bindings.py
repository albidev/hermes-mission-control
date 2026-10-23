"""Owner-fixed SQLite binding tests; no native identity fabrication in acceptance."""
import hashlib
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_room_firewall import SCOPE

class HostBindingTests(unittest.TestCase):
    def test_only_validated_ack_enrolls_persistent_scope(self):
        import json
        from run_bindings import HostRunBindings
        from room_firewall import RoomFirewall, Request, UpstreamResponse, FirewallDenied, UpstreamAmbiguous
        from test_room_firewall import dispatch_body
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'host.db'
            store = HostRunBindings(path)
            calls = []
            reply = [UpstreamResponse(202, body=b'{"run_id":"native-1","status":"started","replayed":false}')]
            def backend(request):
                calls.append(request.path)
                if isinstance(reply[0], Exception): raise reply[0]
                return reply[0]
            firewall = RoomFirewall(expected_scope=SCOPE, backend=backend, verify_native=store,
                                    on_admitted=store.record_ack)
            headers = (('authorization','HermesRoom test'),('content-type','application/json'),('idempotency-key','fixed-key'))
            request = Request('POST','/v1/runs', headers, dispatch_body())
            with self.assertRaises(FirewallDenied):
                firewall.forward(Request('GET','/v1/runs/native-1',headers))
            self.assertEqual(calls, [])
            for body in (b'{}', b'{"run_id":"bad/id","status":"started","replayed":false}', b'{"run_id":"native-1","status":"started","replayed":"yes"}', b'{"run_id":"native-1","status":[],"replayed":false}'):
                reply[0] = UpstreamResponse(202, body=body)
                with self.assertRaises(UpstreamAmbiguous): firewall.forward(request)
                self.assertFalse(store('native-1', SCOPE))
            reply[0] = ConnectionResetError('lost reply')
            with self.assertRaises(UpstreamAmbiguous): firewall.forward(request)
            self.assertFalse(store('native-1', SCOPE))
            reply[0] = UpstreamResponse(202, body=b'{"run_id":"native-1","status":"completed","replayed":true}')
            firewall.forward(request)
            self.assertTrue(HostRunBindings(path)('native-1', SCOPE))
            for field in SCOPE:
                changed = dict(SCOPE)
                changed[field] = SCOPE[field]+1 if field == 'authority_epoch' else SCOPE[field]+'x'
                self.assertFalse(store('native-1', changed))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            firewall.forward(Request('GET','/v1/runs/native-1',headers))
            firewall.forward(Request('POST','/v1/runs/native-1/stop',headers,b'{}'))
            self.assertEqual(calls[-2:], ['/v1/runs/native-1','/v1/runs/native-1/stop'])

class ReceiptTests(unittest.TestCase):
    def test_native_readonly_receipt_scope_and_fail_closed(self):
        from run_bindings import NativeReceiptVerifier
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'native.db'
            verify = NativeReceiptVerifier(path)
            self.assertFalse(verify('run-1', SCOPE))
            self.assertFalse(path.exists())
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE run_idempotency(scope TEXT, run_id TEXT)')
                scope = hashlib.sha256('\0'.join(str(SCOPE[k]) for k in ('room_id','home_install_id','authority_gateway_id','authority_epoch','member_id','target_install_id','target_profile')).encode()).hexdigest()
                db.execute('INSERT INTO run_idempotency VALUES (?,?)', (scope, 'run-1'))
            before = path.read_bytes()
            self.assertTrue(verify('run-1', SCOPE))
            self.assertTrue(NativeReceiptVerifier(path)('run-1', SCOPE))
            self.assertFalse(verify('unknown', SCOPE))
            for field in SCOPE:
                altered = dict(SCOPE)
                altered[field] = SCOPE[field]+1 if field == 'authority_epoch' else SCOPE[field]+'x'
                self.assertFalse(verify('run-1', altered), field)
            self.assertEqual(before, path.read_bytes())
            path.write_bytes(b'not sqlite')
            self.assertFalse(verify('run-1', SCOPE))
            self.assertFalse(NativeReceiptVerifier(Path(directory))('run-1', SCOPE))
