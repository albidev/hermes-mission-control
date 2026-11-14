"""Card3-A receipt-backed target verification and immutable validated host ack bindings."""
import dataclasses
import importlib.util
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from invite_rooms.admission_receipts import AdmissionReceiptStore
from invite_rooms.peer_registry import MaterialScope

RUN = 'run_' + '1'*32
KEY = 'room:task:3'


def scope(**kw):
    args = dict(owner_installation_id='install:'+'a'*32, owner_profile='default', lobby_id='l', planned_room_id='r', member_id='m', invite_id='i', peer_id='peer', endpoint_id='ep', fingerprint='f'*64, target_install_id='install:'+'b'*32, target_profile='default', catalog_digest='c'*64, policy_digest='d'*64, pairing_generation=1)
    args.update(kw)
    return MaterialScope(**args)


def expected(s):
    return dict(room_id=s.planned_room_id, home_install_id=s.owner_installation_id, authority_gateway_id=s.owner_installation_id, authority_epoch=1, member_id=s.member_id, target_install_id=s.target_install_id, target_profile=s.target_profile)


class HostBindingsTests(unittest.TestCase):
    def test_firewall_enrolls_validated_dispatch_ack_but_not_unknown(self):
        import json
        from invite_rooms.control.firewall import RoomFirewall, Request, UpstreamResponse, UpstreamAmbiguous
        from test_target_dispatch_backend_w2d import dispatch_raw
        api = self.api()
        s, e = scope(), expected(scope())
        raw = dispatch_raw(task='task', gen=3, scope=e)
        request = Request('POST', '/v1/runs', (('authorization', 'HermesRoom fixture'),
            ('content-type', 'application/json'), ('idempotency-key', KEY)), raw)
        seen = []
        with tempfile.TemporaryDirectory() as root:
            store = api.HostAckBindingStore(Path(root)/'bindings', anchor=Path(root))
            self.addCleanup(store.close)
            verifier = store.verifier(scope=s, expected_scope=e)
            reply = dict(run_id=RUN, status='unknown', replayed=True)

            def backend(forwarded):
                self.assertEqual(forwarded.body, raw)
                seen.append(forwarded.path)
                return UpstreamResponse(202, (('content-type', 'application/json'),), json.dumps(reply).encode())

            firewall = RoomFirewall(expected_scope=e, backend=backend, verify_native=verifier,
                                    on_admitted=verifier.record_ack)
            with self.assertRaises(UpstreamAmbiguous):
                firewall.forward(request)
            self.assertFalse(verifier(RUN,e))
            reply.update(status='started', replayed=False)
            self.assertEqual(firewall.forward(request).status, 202)
            self.assertTrue(verifier.exact_replay(RUN,e,'task',3,KEY))
            reply.update(status='unknown', replayed=True)
            self.assertEqual(firewall.forward(request).status, 202)
            self.assertEqual(seen, ['/v1/runs']*3)

    def api(self):
        self.assertIsNotNone(importlib.util.find_spec('invite_rooms.run_bindings'), 'product run verifier missing')
        from invite_rooms import run_bindings
        return run_bindings

    def test_validated_ack_hook_is_only_enrollment_and_restart_is_exact(self):
        api = self.api()
        s = scope()
        e = expected(s)
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)/'bindings'
            store = api.HostAckBindingStore(path, anchor=Path(root))
            self.addCleanup(store.close)
            verifier = store.verifier(scope=s, expected_scope=e)
            self.assertFalse(verifier(RUN, e))
            with self.assertRaises(api.RunBindingError):
                verifier.record_ack(RUN, e)  # A generic/unknown acknowledgement cannot create a row.
            self.assertFalse(verifier(RUN, e))
            verifier.record_dispatch_ack(RUN, e, 'task', 3, KEY)
            verifier.record_ack(RUN, e)
            self.assertTrue(verifier(RUN, e))
            self.assertTrue(verifier.exact_replay(RUN, e, 'task', 3, KEY))
            for bad in (dict(e, authority_epoch=2), dict(e, authority_gateway_id='changed'), dict(e, target_profile='other')):
                self.assertFalse(verifier(RUN, bad))
                with self.assertRaises(api.RunBindingError):
                    verifier.record_dispatch_ack(RUN, bad, 'task', 3, KEY)
            for run, task, gen, key in ((RUN, 'other',3,KEY), (RUN,'task',4,KEY), (RUN,'task',3,'room:other:3'), ('run_'+'2'*32,'task',3,KEY)):
                self.assertFalse(verifier.exact_replay(run,e,task,gen,key))
            with self.assertRaises(api.RunBindingError):
                verifier.record_dispatch_ack('run_'+'2'*32, e, 'task', 3, KEY)
            sibling = store.verifier(scope=scope(lobby_id='other'), expected_scope=e)
            self.assertFalse(sibling(RUN,e))
            with self.assertRaises(api.RunBindingError):
                sibling.record_dispatch_ack(RUN,e,'task',3,KEY)
            changed = store.verifier(scope=s, expected_scope=dict(e, authority_epoch=2))
            self.assertFalse(changed(RUN,dict(e, authority_epoch=2)))
            store.close()
            store = api.HostAckBindingStore(path, anchor=Path(root))
            self.addCleanup(store.close)
            verifier = store.verifier(scope=s, expected_scope=e)
            self.assertTrue(verifier.exact_replay(RUN,e,'task',3,KEY))
            self.assertFalse(verifier.exact_replay(RUN,e,'task',True,KEY))

    def test_private_storage_symlinks_modes_and_corrupt_restart_refused(self):
        api = self.api()
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            real = root/'real'
            real.mkdir(mode=0o700)
            alias = root/'alias'
            alias.symlink_to(real, target_is_directory=True)
            with self.assertRaises(Exception):
                api.HostAckBindingStore(alias, anchor=root)
            store = api.HostAckBindingStore(real, anchor=root)
            verifier = store.verifier(scope=scope(), expected_scope=expected(scope()))
            verifier.record_dispatch_ack(RUN,expected(scope()),'task',3,KEY)
            store.close()
            # Corrupt bytes may not be interpreted as an empty store or repaired.
            store.path.write_bytes(b'corrupt')
            with self.assertRaises(Exception):
                api.HostAckBindingStore(real, anchor=root)
            self.assertEqual(store.path.read_bytes(), b'corrupt')
            bad = root/'bad'
            bad.mkdir(mode=0o755)
            with self.assertRaises(Exception):
                api.HostAckBindingStore(bad, anchor=root)


class TargetVerifierTests(unittest.TestCase):
    def test_confirmed_receipts_only_full_scope_and_original_authority(self):
        self.assertIsNotNone(importlib.util.find_spec('invite_rooms.run_bindings'), 'product run verifier missing')
        from invite_rooms.run_bindings import TargetReceiptVerifier
        from test_target_material_source_w2d import genuine_source_fixture
        with genuine_source_fixture() as fx, tempfile.TemporaryDirectory() as root:
            source, s = fx['source'], fx['scope']
            resolved = source.resolve(lobby_id=s.lobby_id, invite_id=s.invite_id, deadline=time.monotonic()+5)
            e = expected(s)
            store = AdmissionReceiptStore(Path(root)/'receipts')
            self.addCleanup(store.close)
            def make(**kw):
                return TargetReceiptVerifier(store=store, source=source, resolved=resolved, expected_scope=e, **kw)
            verifier = make()
            row = dict(scope=s,generation=s.pairing_generation,profile=s.target_profile,base_url='http://127.0.0.1:19001',idempotency_key=KEY,request_fingerprint='e'*64)
            store.reserve(**row)
            self.assertFalse(verifier(RUN,e), 'pending is not admission')
            store.put(**row,run_id=RUN,status='started',replayed=False,verifier_version='mc-target-http-verifier-v1')
            self.assertTrue(verifier(RUN,e))
            self.assertTrue(verifier.exact_replay(RUN,e,'task',3,KEY))
            for field, value in (('authority_epoch',2),('authority_gateway_id','other'),('target_profile','other'),('room_id','other')):
                self.assertFalse(verifier(RUN,dict(e,**{field:value})))
            self.assertFalse(verifier('run_'+'2'*32,e))
            self.assertFalse(verifier.exact_replay(RUN,e,'task',4,KEY))
            self.assertFalse(verifier.exact_replay(RUN,e,'task',3,'room:other:3'))
            # Reader isolation is bounded and public; wrong base/profile/scope has no match.
            for change in (dict(base_url='http://127.0.0.1:19002'),dict(scope=dataclasses.replace(s,lobby_id='other')),dict(profile='other')):
                args = dict(scope=s,generation=s.pairing_generation,profile=s.target_profile,base_url=row['base_url'],run_id=RUN)
                args.update(change)
                try:
                    found = store.find_confirmed_run(**args)
                except ValueError:
                    found = None
                self.assertIsNone(found)
            self.assertIsNone(store.find_confirmed_run(scope=s,generation=s.pairing_generation,profile=s.target_profile,base_url=row['base_url'],run_id=RUN,idempotency_key='room:other:3'))
            with mock.patch.object(source,'load',side_effect=ValueError('material unavailable')):
                self.assertFalse(verifier(RUN,e))
            store.close()
            store = AdmissionReceiptStore(Path(root)/'receipts')
            self.addCleanup(store.close)
            verifier = make()
            self.assertTrue(verifier(RUN,e))
            store.path.write_bytes(b'corrupt')
            self.assertFalse(verifier(RUN,e))


if __name__ == '__main__':
    unittest.main()
