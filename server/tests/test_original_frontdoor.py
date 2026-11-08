"""Original-material frontdoor regressions. Fixture catalogs/backend, NOT native effect acceptance.

Real pairing codec/journal/consent signatures, owned registry, HTTP/firewall and mTLS.
No model or native run submission: the sole effect backend is an explicit no-op fixture.
"""
import asyncio
import copy
import dataclasses
import hashlib
import http.client
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import pairing_support as ps
from gateway.hosted_room_peer import catalog_mapping
import base64
from invite_rooms.consent import canonical
from invite_rooms.identity import load_identity
from invite_rooms.peer_registry import MaterialScope, RegistrySource
from invite_rooms.material_receipts import MaterialReceiptStore
from invite_rooms.runtime_config import build_peer_registry
from invite_rooms.control.adapter import LoopbackAdapter
from invite_rooms.control.connector import LinkBinding, LinkIdentity, TargetConnector
from invite_rooms.control.scoped_registry import ScopedAdapterRegistry
from invite_rooms.original_frontdoor import OriginalFrontdoor, FrontdoorDenied
from test_room_invite_data_connector import Native


class OriginalFrontdoorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='original-frontdoor-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.adapter = LoopbackAdapter()
        self.adapter.start()
        self.addCleanup(self.adapter.close)
        port = self.adapter._server.server_address[1]
        self.url = f'http://127.0.0.1:{port}/frontdoor'
        policy = ps.make_catalog(ps.INSTALL_B)['execution_policy']
        # Explicit unit fixture advertised BEFORE pairing; no production catalog override.
        catalog = catalog_mapping(installation_id=ps.INSTALL_B, protocol_versions=(2,),
            link_modes=('direct',), persistent_process=True, text=True, attachments=False,
            endpoint={'available': True, 'url': self.url, 'transport_security': 'loopback'},
            target_profile='default', execution_policy=policy)
        self.a = ps.Side(self.root, 'a', ps.INSTALL_A)
        self.b = ps.Side(self.root, 'b', ps.INSTALL_B, catalog=catalog)
        self.ap, self.bp = self.a.service(), self.b.service()
        paired = ps.drive_pairing(self.ap, self.bp)
        ack = self.ap.ack_request(paired['a_id'])
        self.ap.complete_ack(paired['a_id'], self.bp.handle_pair_ack(ack.request))
        self.scopes = ScopedAdapterRegistry(self.adapter, self.ap.authority)
        self.addCleanup(self.scopes.close)
        self.allowed = True
        self.front = OriginalFrontdoor(self.adapter, self.scopes, self.url, self.fresh, self.ready)
        self.addCleanup(self.front.close)
        self.receipts = MaterialReceiptStore(self.root / 'receipts')
        self.addCleanup(self.receipts.close)
        self.targets = []
        self.native = Native()
        self.native.status = 200  # Explicit no-op fixture, not native capability acceptance.
        self.addCleanup(self.native.close)

    def fresh(self):
        registry = build_peer_registry(self.a.peers_path)
        registry._source = RegistrySource(self.a.peers_path, build_peer_registry)
        registry._authority = self.ap.authority
        registry._receipt_store = self.receipts
        registry._route_provider = self.scopes.lookup
        registry._original_frontdoor = self.front
        return registry

    def ready(self, scope):
        if not self.allowed:
            raise FrontdoorDenied('scope_unavailable')

    def produce(self, n, signed=True):
        reg = self.fresh()
        peer = reg.list_peers()[0]['peer_id']
        binding = reg.binding_for(peer)
        scope = MaterialScope(owner_installation_id=ps.INSTALL_A, owner_profile='default',
            lobby_id='lobby-'+str(n), planned_room_id='room-'+str(n), member_id='remote',
            invite_id='invite-'+str(n), peer_id=peer, pairing_generation=1, **binding)
        ab = load_identity(self.a.identity_dir, installation_id=ps.INSTALL_A, profile='default')
        bb = load_identity(self.b.identity_dir, installation_id=ps.INSTALL_B, profile='default')
        bt = self.bp.authority.peer_trust(scope.endpoint_id, 1, role='joining')
        at = self.ap.authority.peer_trust(scope.endpoint_id, 1, role='host')
        expected = dict(room_id=scope.planned_room_id, home_install_id=ps.INSTALL_A,
            authority_gateway_id=ps.INSTALL_A, authority_epoch=1, member_id='remote',
            target_install_id=ps.INSTALL_B, target_profile='default')
        target = TargetConnector(binding=LinkBinding(scope.endpoint_id, 1, ('127.0.0.1',1),
            str(ab.ca_file), ab.leaf_fingerprint, bt.binding_dict()),
            identity=LinkIdentity(str(bb.leaf_cert_file),str(bb.leaf_key_file)), authority=self.bp.authority,
            expected_scope=expected, verify_native=lambda *_: True, target_base=self.native.url)
        target.start(); self.targets.append(target); self.addCleanup(target.close)
        route = self.scopes.register(scope=scope,
            binding=LinkBinding(scope.endpoint_id,1,target.address,bb.ca_file,at.fingerprint,at.binding_dict()),
            identity=LinkIdentity(str(ab.leaf_cert_file),str(ab.leaf_key_file)),
            expected_scope=expected, verify_native=lambda *_: True)
        grant = 'fixture-original-opaque-'+str(n)+'-'+'x'*32
        catalog = reg._peers[peer].catalog
        material = dict(binding=binding, catalog=catalog, grant=grant)
        wire_scope = dict(host_id=ps.INSTALL_A,room_id=scope.planned_room_id,lobby_id=scope.lobby_id,
            member_id=scope.member_id,invite_id=scope.invite_id,binding=binding)
        op = dict(endpoint_id=scope.endpoint_id,generation=1,lobby_id=scope.lobby_id,
            invite_id=scope.invite_id,request_id=('%032x'%n),expires_at_utc_ms=int(time.time()*1000)+30000)
        payload = dict(v=2,scope=wire_scope,grant=grant,catalog=catalog,operation=op,
            purpose='joining-owner-material-v2')
        envelope = {k:v for k,v in payload.items() if k!='purpose'}
        envelope['signature'] = base64.b64encode(bb.consent_key().sign(canonical(payload))).decode()
        if signed:
            reg.admit_signed_material(scope,envelope)
            self.front.enroll(reg,scope)
        else:
            self.receipts.admit(scope,material)
        return reg,scope,route,grant,envelope,target

    def request(self, grant, child='/v1/room-members/capabilities', method='GET', body=None, extra=()):
        c = http.client.HTTPConnection('127.0.0.1', self.adapter._server.server_address[1], timeout=5)
        c.putrequest(method,'/frontdoor'+child)
        c.putheader('Authorization','HermesRoom '+grant)
        for k,v in extra:c.putheader(k,v)
        if body is not None:
            c.putheader('Content-Length',str(len(body)));c.putheader('Content-Type','application/json')
        c.endheaders(body)
        r=c.getresponse();data=r.read();status=r.status;c.close()
        return status,data

    def test_two_signed_scopes_same_endpoint_private_routes_distinct(self):
        a,b = self.produce(1),self.produce(2)
        self.assertNotEqual(a[2].url,b[2].url)
        for reg,scope,route,grant,_,target in (a,b):
            self.assertEqual(self.request(grant)[0],200)
            material=asyncio.run(reg.resolve_scoped_material(scope))
            self.assertEqual(material.adapter_url,route.url)
            self.assertEqual(material.native_target.url,self.url)
            self.assertEqual(self.scopes.lookup(scope),route)
        self.assertEqual([x.served for x in self.targets],[1,1])

    def test_manual_receipt_unknown_replacement_duplicate_and_renewal_deny(self):
        a=self.produce(1);reg,scope,route,grant,_,target=a
        manual=self.produce(2,signed=False)
        with self.assertRaises(FrontdoorDenied):self.front.enroll(manual[0],manual[1])
        for token in ('unknown',grant+'replacement',manual[3]):self.assertEqual(self.request(token)[0],403)
        self.assertEqual(self.request(grant,extra=(('Authorization','HermesRoom '+grant),))[0],403)
        self.assertEqual(self.request(grant,'/v1/room-members/grants/refresh','POST',b'{}')[0],501)
        self.assertEqual(target.served,0)
        self.assertEqual(self.request(grant)[0],200)

    def test_retire_preserves_cleanup_reference_and_sibling(self):
        a,b=self.produce(1),self.produce(2)
        self.front.retire(a[1])
        self.assertEqual(self.request(a[3])[0],403)
        self.assertEqual(self.request(b[3])[0],200)
        tombstone=self.front.cleanup_pending(a[1])
        self.assertTrue(tombstone)
        self.assertEqual(tombstone['grant_digest'],hashlib.sha256(a[3].encode()).hexdigest())
        self.assertEqual(self.receipts.load(a[1])['grant'],a[3])
        self.assertEqual(a[5].served,0)

    def test_cancellation_and_route_replacement_after_selection_deny(self):
        a=self.produce(1)
        self.allowed=False
        self.assertEqual(self.request(a[3])[0],403)
        self.assertEqual(a[5].served,0)
        self.allowed=True
        original=self.front.select
        def selected(grant):
            result=original(grant)
            self.scopes.revoke(a[1])
            return result
        with patch.object(self.front,'select',side_effect=selected):
            self.assertEqual(self.request(a[3])[0],403)
        self.assertEqual(a[5].served,0)

    def dispatch_body(self, scope):
        dispatch = dict(room_id=scope.planned_room_id, home_install_id=ps.INSTALL_A,
            authority_gateway_id=ps.INSTALL_A, authority_epoch=1, member_id='remote',
            target_install_id=ps.INSTALL_B, target_profile='default', protocol_version=2,
            task_id='fixture-task', execution_generation=1, source_event_seq=1,
            cancellation_scope_id='fixture-cancel', prompt='fixture no-op',
            prompt_digest='a'*64, capability_digest=scope.catalog_digest,
            execution_policy_digest=scope.policy_digest, trace_id='fixture-trace')
        return dict(input='fixture no-op',hosted_room_dispatch=dispatch)

    def test_fixture_only_effect_forwarding_and_cross_scope_body_grammar(self):
        a,b=self.produce(1),self.produce(2)
        body=self.dispatch_body(a[1])
        key=(('Idempotency-Key','fixture-original-effect'),)
        # This HTTP destination is the explicit Native no-op fixture, never Core /runs.
        self.native.body=b'{"run_id":"fixture-run","status":"completed","replayed":false}'
        self.assertEqual(self.request(a[3],'/v1/runs','POST',json.dumps(body).encode(),key)[0],200)
        self.assertEqual(a[5].served,1)
        self.assertEqual(len(self.native.calls),1)
        attempts=[]
        for field,value in [('room_id',b[1].planned_room_id),('member_id','other'),
                            ('authority_epoch',2),('execution_generation',False),('target_profile','other')]:
            changed=copy.deepcopy(body);changed['hosted_room_dispatch'][field]=value
            attempts.append(json.dumps(changed).encode())
        attempts += [b'{"input":"a","input":"b"}',b'{}',b'[]']
        for bad in attempts:
            with self.subTest(body_shape=bad[:35]):
                self.assertEqual(self.request(a[3],'/v1/runs','POST',bad,key)[0],403)
        self.assertEqual(a[5].served,1)
        self.assertEqual(b[5].served,0)
        self.assertEqual(len(self.native.calls),1)
        # Real receipt verifiers are used in production. This explicit rejecting
        # unit verifier proves lookup does not turn arbitrary run IDs into authority.
        self.scopes.owned_link(a[1]).firewall._verify_native=lambda *_:False
        self.assertEqual(self.request(a[3],'/v1/runs/other-run')[0],403)
        self.assertEqual(self.request(a[3],'/v1/runs/other-run/stop','POST',b'{}')[0],403)
        self.assertEqual(len(self.native.calls),1)

    def test_cancel_after_selection_before_connector_io_has_zero_backend_calls(self):
        a=self.produce(1)
        link=self.scopes.owned_link(a[1]); original=link.connector.forward
        def queued(request,admit=None,**kwargs):
            self.allowed=False
            admit()  # Same per-I/O callback a HostConnector uses after its wait.
            return original(request,admit,**kwargs)
        with patch.object(link.connector,'forward',side_effect=queued):
            self.assertEqual(self.request(a[3])[0],502)  # conservative unknown; no false post-effect 4xx
        self.assertEqual(a[5].served,0)
        self.assertEqual(self.native.calls,[])

    def test_enrollment_cancelled_after_blocking_reads_retains_pending_cleanup(self):
        a=self.produce(1,signed=False)
        a[0].admit_signed_material(a[1],a[4])
        live=[True];ready=self.front._ready
        def cancel(scope):
            ready(scope);live[0]=False
        with patch.object(self.front,'_ready',side_effect=cancel):
            with self.assertRaises(FrontdoorDenied):self.front.enroll(a[0],a[1],admit=lambda:live[0])
        self.assertEqual(self.request(a[3])[0],403)
        self.assertTrue(self.front.cleanup_pending(a[1]))
        self.assertEqual(a[5].served,0)

    def test_current_receipt_unreadable_and_typed_material_substitution_deny(self):
        a,b=self.produce(1),self.produce(2)
        from room_lobby_service import _native_target_url
        material=asyncio.run(a[0].resolve_scoped_material(a[1]))
        other=asyncio.run(b[0].resolve_scoped_material(b[1]))
        with self.assertRaises(FrontdoorDenied):
            _native_target_url(dataclasses.replace(other,native_target=material.native_target))
        fence=material.native_target.registration_admission(material)
        self.front.retire(a[1])
        with self.assertRaises(FrontdoorDenied):fence()
        path=self.receipts.root/self.receipts.receipt_name(b[1])
        path.chmod(0o644)
        self.assertEqual(self.request(b[3])[0],403)
        self.assertEqual(b[5].served,0)
        path.chmod(0o600)
        self.assertEqual(self.request(b[3])[0],200)

    def test_explicit_frontdoor_topology_and_http_framing_deny(self):
        from invite_rooms.original_frontdoor import validate_frontdoor
        for url in ('http://example.invalid:80/frontdoor','http://127.0.0.1:1/v1',
                    'http://localhost:1/frontdoor','http://127.0.0.1:1/frontdoor?x=1'):
            with self.assertRaises(FrontdoorDenied):validate_frontdoor(url)
        a=self.produce(1)
        for child in ('/v1/room-members/capabilities?x=1','/%76%31/runs','//v1/runs'):
            self.assertEqual(self.request(a[3],child)[0],403)
        for extra in ((('Transfer-Encoding','chunked'),),(('Cookie','x=y'),),
                      (('Content-Length','-1'),),(('Content-Length','0'),('Content-Length','0'))):
            self.assertEqual(self.request(a[3],extra=extra)[0],403)
        self.assertEqual(a[5].served,0)

    def test_generation_drift_and_cold_instance_deny(self):
        a=self.produce(1)
        self.ap.unpair(a[1].peer_id)
        self.assertEqual(self.request(a[3])[0],403)
        self.assertEqual(a[5].served,0)
        self.front.close()
        other=OriginalFrontdoor(self.adapter,self.scopes,self.url,self.fresh,self.ready)
        self.addCleanup(other.close)
        with self.assertRaises(FrontdoorDenied):other.select(a[3])
