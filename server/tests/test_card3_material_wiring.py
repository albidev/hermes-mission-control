"""Card3-B1 real local production seams, no native/model/shared service."""
from __future__ import annotations
import asyncio
import contextlib
import dataclasses
import http.client
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import threading
import time
import unittest
from unittest import mock
import test_sidecar_data_plane_assembly_w2d as assembly
from invite_rooms import runtime_config, routes
from invite_rooms.control.connector import LinkBinding
from invite_rooms.control.firewall import ForwardedRequest
from invite_rooms.peer_registry import MaterialScope, adapter_id_for
from invite_rooms.target_dispatch_backend import make_target_dispatch_backend_factory
from test_target_dispatch_backend_w2d import EXPECTED, _binding

def setUpModule(): assembly.setUpModule()
def tearDownModule(): assembly.tearDownModule()

class CatalogAdapterTests(assembly.Base):
    """Both production readers through real BoundedNativeRpc and recording loopback WS."""
    def catalog_case(self, profile, *, pairing):
        import inspect
        from test_room_invite_native_rpc import recording_catalog_rpc
        data = self.config_dict(self.A, self.peer_b)
        data['profile'] = profile
        data['target_http']['profile'] = profile
        path = self.A.root/'cfg'/'catalog-runtime.json'
        assembly.private_json(path, data)
        # Use a real owner-bound loader/store, not a default-profile store with
        # its context relabeled. No identity/target/grant is used by these reads.
        loaded = runtime_config.load_service({assembly.CONFIG_ENV: str(path)})
        self.assertIsNotNone(loaded)
        service, owner, config = loaded
        plane = assembly.sdp().build_sidecar_data_plane((service, owner, config))
        self.assertIsNotNone(plane)
        self.planes.append(plane)
        catalog = {'recording_only': True, 'requested_profile': profile}
        records = []
        @contextlib.asynccontextmanager
        async def connect(rpc_config):
            self.assertEqual(rpc_config, config.native_rpc)
            async with recording_catalog_rpc(records, catalog) as rpc:
                yield rpc
        with mock.patch.object(routes, '_connect_native_rpc', connect), \
                mock.patch.object(routes, '_PAIRING_CATALOG_SOURCE', None):
            if pairing:
                result = routes._pairing_catalog(config, owner)
            else:
                reader = inspect.getclosurevars(plane.target_dispatch_backend_factory).nonlocals['catalog_reader']
                result = reader(deadline=time.monotonic()+3)
        self.assertEqual(result, catalog)
        self.assertEqual(records, [('groups.capabilities', {'profile': profile})])

    def test_pairing_catalog_default_through_bounded_rpc_recording_ws(self):
        self.catalog_case('default', pairing=True)

    def test_pairing_catalog_nondefault_through_bounded_rpc_recording_ws(self):
        self.catalog_case('research', pairing=True)

    def test_target_catalog_default_through_bounded_rpc_recording_ws(self):
        self.catalog_case('default', pairing=False)

    def test_target_catalog_nondefault_through_bounded_rpc_recording_ws(self):
        self.catalog_case('research', pairing=False)

class ProfilePassthroughTests(assembly.Base):
    def test_default_and_nondefault_all_canonical_operations_real_http(self):
        records=[]
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self): self.respond()
            def do_POST(self): self.respond()
            def respond(self):
                body=self.rfile.read(int(self.headers.get('content-length',0)))
                records.append((self.command,self.path,self.headers.get('Authorization'),body))
                self.send_response(404 if '/p/unserved/' in self.path else 200)
                self.send_header('Content-Type','application/json'); self.end_headers(); self.wfile.write(b'{}')
            def log_message(self,format,*args): pass
        server=HTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever); thread.start()
        from invite_rooms.joining_store import JoiningStore
        from invite_rooms.pairing_journal import JournalLinkAuthority
        store=JoiningStore(self.tmp/'profile-joining')
        self.addCleanup(store.close)
        authority=JournalLinkAuthority(self.a.journal)
        def factory(config):
            return make_target_dispatch_backend_factory(store,authority,lambda **kw: None,lambda **kw: None,config,None)
        try:
            for profile in ('default','research','unserved'):
                config=runtime_config.TargetHttpConfig('http://127.0.0.1:'+str(server.server_port),profile)
                backend=factory(config)(
                    _binding(),dict(EXPECTED,target_profile=profile))
                for method,path in [('GET','/v1/room-members/capabilities'),('GET','/v1/runs/run_'+'a'*32),
                    ('POST','/v1/runs/run_'+'a'*32+'/stop'),('POST','/v1/runs/run_'+'a'*32+'/resolve-unknown'),
                    ('POST','/v1/room-members/grants/refresh'),('POST','/v1/room-members/grants/revoke')]:
                    with self.subTest(profile=profile,path=path):
                        request=ForwardedRequest(method,path,(('authorization','HermesRoom original'),('content-type','application/json')),b'{}' if method=='POST' else b'')
                        result=backend(request,deadline=time.monotonic()+3)
                        prefix='' if profile=='default' else '/p/'+profile
                        self.assertEqual(records[-1],(method,prefix+path,'HermesRoom original',request.body))
                        self.assertEqual(result.status,404 if profile=='unserved' else 200)
            self.assertEqual(len(records),18)
            config=runtime_config.TargetHttpConfig('http://127.0.0.1:'+str(server.server_port),'research')
            backend=factory(config)(_binding(),EXPECTED)
            self.assertEqual(backend(ForwardedRequest('GET','/v1/room-members/capabilities',(),b''),deadline=time.monotonic()+3).status,403)
            self.assertEqual(len(records),18)
        finally:
            server.shutdown(); thread.join(3); server.server_close()

class MaterialWiringTests(assembly.Base):
    def setup_flow(self):
        # Establish fixed topology only after generating the original local scope.
        loaded=self.load(self.A,self.peer_b)
        lobby,issued=self.seed_ready_scope(loaded[0].store)
        scope=MaterialScope(owner_installation_id=self.A.installation_id,owner_profile='default',
            lobby_id=lobby['lobby_id'],planned_room_id=lobby['planned_room_id'],member_id='peer-b',
            invite_id=issued.invite_id,peer_id=self.peer_b,pairing_generation=self.generation,**self.binding)
        aid=adapter_id_for(scope)
        ca=self.config_dict(self.A,self.peer_b,adapter_id=aid)
        cb=self.config_dict(self.B,self.peer_a,adapter_id=aid)
        ca['pairing']['control_endpoints']={self.peer_b:cb['pairing']['control_listen']}
        ca['pairing']['data_endpoints']={self.peer_b:{aid:cb['pairing']['target_listeners'][aid]}}
        cb['joining']['control_endpoints']={self.peer_a:ca['pairing']['control_listen']}
        self.envs=[]; planes=[]
        for side,cfg in ((self.A,ca),(self.B,cb)):
            path=side.root/'cfg'/'runtime.json'; assembly.private_json(path,cfg)
            env={assembly.CONFIG_ENV:str(path)}; self.envs.append(env)
            plane=assembly.sdp().build_sidecar_data_plane(runtime_config.load_service(env))
            self.assertIsNotNone(plane); self.planes.append(plane); planes.append(plane)
        a,b=planes
        imported=b.joining_store.create_import(lobby_id=scope.lobby_id,invite_id=scope.invite_id,
            secret=issued.secret,host_peer_id=f'{self.endpoint}:{self.generation}')
        b.joining_store.mark_redeemed(imported)
        confirmed=dict(host_id=scope.owner_installation_id,room_id=scope.planned_room_id,
            lobby_id=scope.lobby_id,member_id=scope.member_id,binding=scope.binding)
        b.joining_store.confirm_scope(imported,confirmed)
        from invite_rooms.identity import load_identity
        from invite_rooms.runtime_joining import _IdentityConsentKey
        signer=_IdentityConsentKey(load_identity(self.B.identity_dir,installation_id=self.B.installation_id,profile='default'))
        receipt=dict(confirmed,invite_id=scope.invite_id,purpose='joining-owner-consent-v1')
        b.joining_store.begin_consent(imported,receipt=receipt,signature=signer.sign(receipt))
        b.joining_store.mark_consent_delivered(imported)
        return a,b,scope

    def native_boundary(self,b,events):
        from gateway.hosted_room_peer import GatewayRoomCatalog, issue_room_grant
        catalog=GatewayRoomCatalog.from_mapping(self.B.catalog)
        @contextlib.asynccontextmanager
        async def connect(config):
            async def rpc(method,params,**kwargs):
                events.append((method,dict(params)))
                if method=='groups.capabilities':
                    return {'driver':True,'authority_gateway_id':self.A.installation_id,
                        'room_link':{'enabled':True,'catalog':self.B.catalog}}
                if method=='groups.peer.invite':
                    token=issue_room_grant(b'k'*32,grant_id=params['grant_id'],room_id=params['room_id'],
                        home_install_id=params['home_install_id'],authority_gateway_id=params['authority_gateway_id'],
                        authority_epoch=params['authority_epoch'],member_id=params['member_id'],
                        target_install_id=catalog.installation_id,target_profile=params['profile'],
                        execution_policy_digest=catalog.execution_policy.policy_digest,issued_at=time.time(),ttl_seconds=params['ttl_seconds'])
                    return dict(grant=token,target_profile=params['profile'],catalog=self.B.catalog,endpoint=self.B.catalog['endpoint'])
                raise AssertionError('unexpected native mutation '+method)
            yield rpc
        return connect

    def test_b1_real_request_receipt_route_target_and_retry_original_operation(self):
        a,b,scope=self.setup_flow(); events=[]
        with mock.patch.object(routes,'_connect_native_rpc',self.native_boundary(b,events)):
            a.start(); b.start()
            # Request-local loader and production service, not plane.registry snapshot.
            kwargs=dict(scoped_adapter_registry=a.scoped_registry)
            if hasattr(a,'material_coordinator'): kwargs['material_coordinator']=a.material_coordinator
            service,ctx,_=runtime_config.load_service(self.envs[0],**kwargs)
            async def caps_rpc(method,params):
                self.assertEqual(method,'groups.capabilities')
                return {'driver':True,'authority_gateway_id':ctx.installation_id}
            service._rpc=caps_rpc
            # Stop precisely at the native create boundary, after exercising all B1 seams.
            with self.assertRaises(Exception): asyncio.run(service.activate(ctx,scope.lobby_id))
            receipt=service.registry._receipt_store.load(scope,deadline=time.monotonic()+2)
            self.assertIsNotNone(a.scoped_registry.lookup(scope))
            self.assertEqual(b.scope_lifecycle.snapshot()[0][1],'running')
            origin=a.journal.material_origin(scope.endpoint_id,scope.lobby_id,scope.invite_id)
            self.assertEqual(origin['status'],'consumed')
            self.assertEqual([m for m,_ in events].count('groups.peer.invite'),1)
            original=json.dumps(receipt,sort_keys=True)
            with self.assertRaises(Exception): asyncio.run(service.activate(ctx,scope.lobby_id))
            self.assertEqual(json.dumps(service.registry._receipt_store.load(scope),sort_keys=True),original)
            self.assertEqual(a.journal.material_origin(scope.endpoint_id,scope.lobby_id,scope.invite_id),origin)
            self.assertEqual([m for m,_ in events].count('groups.peer.invite'),1)

    def test_t6_first_single_attempt_production_material_roundtrip(self):
        import os
        from pathlib import Path
        from invite_rooms.control.connector import HostConnector
        a,b,scope=self.setup_flow(); events=[]; samples=[]
        original=getattr(HostConnector,'forward_material_probe',None)
        def sampled(connector,**kwargs):
            started=time.monotonic()
            result=original(connector,**kwargs)
            samples.append(dict(t1_utc_ms=result.sent_utc_ms,t4_utc_ms=result.received_utc_ms,
                full_sampled_rtt_ms=result.received_utc_ms-result.sent_utc_ms,
                local_call_seconds=time.monotonic()-started))
            return result
        with mock.patch.object(routes,'_connect_native_rpc',self.native_boundary(b,events)), \
                (mock.patch.object(HostConnector,'forward_material_probe',sampled)
                 if original is not None else contextlib.nullcontext()):
            a.start(); b.start()
            service,ctx,_=runtime_config.load_service(self.envs[0],
                material_coordinator=a.material_coordinator, scoped_adapter_registry=a.scoped_registry)
            try:
                asyncio.run(a.material_coordinator.ensure_material(scope,deadline=time.monotonic()+8,service=service))
            finally:
                out=Path(os.environ['TMPDIR'])/'probe-records'; out.mkdir(exist_ok=True)
                (out/'t6.json').write_text(json.dumps(dict(samples=samples,
                    native_methods=[method for method,_ in events]),indent=2)+'\n')
            self.assertIsNotNone(a.scoped_registry.lookup(scope))
            self.assertEqual(b.scope_lifecycle.snapshot()[0][1],'running')
            self.assertEqual(a.journal.material_origin(scope.endpoint_id,scope.lobby_id,scope.invite_id)['status'],'consumed')
            self.assertEqual([m for m,_ in events].count('groups.peer.invite'),1)

    def cancellation_case(self, *, close=False, second=False):
        # Real originating coordinator ingress, not a stand-in cancelled Future.
        import invite_rooms.control.connector as c
        a,b,scope=self.setup_flow(); events=[]; entered=threading.Event(); release=threading.Event()
        original=c.wrap_client; wraps=[]; sends=[]; streams=[]; original_send=c.SequencedTLS.send
        def barrier(*args,**kwargs):
            stream=original(*args,**kwargs); wraps.append(1); streams.append(stream)
            if len(wraps)==(2 if second else 1):
                entered.set(); release.wait(3)
            return stream
        def record_send(frames,payload):
            if frames.stream in streams and frames._next_send==0: sends.append(1)
            return original_send(frames,payload)
        with mock.patch.object(routes,'_connect_native_rpc',self.native_boundary(b,events)), \
                mock.patch.object(c,'wrap_client',barrier), mock.patch.object(c.SequencedTLS,'send',record_send):
            a.start(); b.start()
            service,_,_=runtime_config.load_service(self.envs[0],material_coordinator=a.material_coordinator,
                scoped_adapter_registry=a.scoped_registry)
            async def run():
                task=asyncio.create_task(a.material_coordinator.ensure_material(scope,
                    deadline=time.monotonic()+7,service=service))
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait,2))
                    if close:
                        with self.assertRaises(Exception): a.material_coordinator.close(timeout=.01)
                    else:
                        task.cancel()
                        with self.assertRaises(asyncio.CancelledError): await task
                    self.assertTrue(a.material_coordinator.busy())
                finally: release.set()
                if close:
                    with self.assertRaises(Exception): await task
                await asyncio.to_thread(a.material_coordinator.close,timeout=4)
                self.assertFalse(a.material_coordinator.busy())
            asyncio.run(run())
        self.assertEqual(len(sends),1 if second else 0)
        self.assertEqual(events,[])
        self.assertIsNone(a.scoped_registry.lookup(scope))
        self.assertFalse(b.scope_lifecycle.snapshot())

    def test_t5_cancel_origin_during_handshake_no_frames(self):
        self.cancellation_case()

    def test_t3_in_window_production_delay_denies_before_native_receipt_route_target(self):
        from invite_rooms.control.connector import HostConnector
        a,b,scope=self.setup_flow(); events=[]; probes=[]
        original=b.control_listener._material_probe
        forward=HostConnector.forward
        def delayed(*args):
            time.sleep(.08); return original(*args)
        def observed(connector,request,*args,**kwargs):
            probe=json.loads(request.body).get('probe')
            if probe is not None: probes.append(probe['t4_utc_ms']-probe['t1_utc_ms'])
            return forward(connector,request,*args,**kwargs)
        with mock.patch.object(routes,'_connect_native_rpc',self.native_boundary(b,events)), \
                mock.patch.object(b.control_listener,'_material_probe',delayed), \
                mock.patch.object(HostConnector,'forward',observed):
            a.start(); b.start()
            service,_,_=runtime_config.load_service(self.envs[0],material_coordinator=a.material_coordinator,
                scoped_adapter_registry=a.scoped_registry)
            with self.assertRaises(Exception):
                asyncio.run(a.material_coordinator.ensure_material(scope,deadline=time.monotonic()+5,service=service))
        self.assertTrue(probes)
        self.assertTrue(all(rtt>50 for rtt in probes))
        self.assertEqual(events,[])
        self.assertIsNone(a.scoped_registry.lookup(scope))
        self.assertFalse(b.scope_lifecycle.snapshot())
        self.assertEqual(a.journal.material_origin(scope.endpoint_id,scope.lobby_id,scope.invite_id)['status'],'pending')

    def test_t5_close_origin_during_handshake_retains_then_drains(self):
        self.cancellation_case(close=True)

    def test_t5_cancel_origin_between_exchanges_no_second_send(self):
        self.cancellation_case(second=True)

    def test_b1_owner_config_registry_injection_is_exact(self):
        a,b,scope=self.setup_flow()
        self.assertTrue(hasattr(a,'material_coordinator'),'B1 coordinator must be bundle owned')
        coordinator=a.material_coordinator
        with mock.patch.object(coordinator,'ensure_material',side_effect=AssertionError('must not run')):
            self.assertIsNone(runtime_config.load_service(self.envs[1],material_coordinator=coordinator,scoped_adapter_registry=a.scoped_registry))
            self.assertIsNone(runtime_config.load_service(self.envs[0],material_coordinator=coordinator,scoped_adapter_registry=b.scoped_registry))
            self.assertIsNone(runtime_config.load_service(self.envs[0],material_coordinator=object(),scoped_adapter_registry=a.scoped_registry))
            path=self.A.root/'cfg'/'runtime.json'; data=json.loads(path.read_text()); data['target_http']['profile']='other'; assembly.private_json(path,data)
            self.assertIsNone(runtime_config.load_service(self.envs[0],material_coordinator=coordinator,scoped_adapter_registry=a.scoped_registry))
