"""af5 canonical controls vs MC: unit boundaries, NOT daemon/full HTTP flow.

Reference fields are extracted from frozen gateway/session_group_controls.py,
not the legacy TUI schema or MC's own allowlist. Native signature/catalog code
is real; the RPC peer and owner channel remain explicit test fixtures.
"""
import asyncio
import base64
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import aiohttp
from invite_rooms.consent import canonical
from invite_rooms.joining_material import TargetMaterialMint, MintAmbiguous
from invite_rooms.joining_store import JoiningStore, JoiningStoreError
from invite_rooms.native_rpc import BoundedNativeRpc, NativeRpcDenied
from room_lobby_service import RoomLobbyService, PeerMaterial, REGISTER_ADMISSION, AdmissionDenied
import test_room_invite_joining as joining_fixture
from test_room_invite_joining import CATALOG, SELF_BINDING
from gateway.hosted_room_peer import issue_room_grant, decode_room_grant

REFERENCE = json.loads((Path(__file__).parent/'fixtures/af5c2014_group_controls.json').read_text())

class WireSocket:
    """In-memory protocol peer, not a native handler."""
    def __init__(self): self.frames=[]
    async def send_str(self, raw): self.frames.append(json.loads(raw))
    async def receive(self, timeout=None):
        if timeout is not None: raise TimeoutError()
        return SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=json.dumps(
            {'jsonrpc':'2.0','id':self.frames[-1]['id'],'result':{}}))

class CanonicalFields(unittest.IsolatedAsyncioTestCase):
    async def test_no_local_identifiers_or_arbitrary_fields_reach_wire(self):
        ws=WireSocket(); rpc=BoundedNativeRpc(ws)
        invite=dict(room_id='room',home_install_id='home',authority_gateway_id='home',authority_epoch=1,member_id='peer')
        register=dict(room_id='room',member_id='peer',target_url='http://127.0.0.1:1',target_profile='default',grant='opaque',catalog={})
        for method,params,extras in [('groups.peer.invite',invite,['grant_id','arbitrary']),
                                     ('groups.peer.register',register,['cancellation_scope_id','trace_id','arbitrary'])]:
            for key in extras:
                with self.subTest(method=method,key=key):
                    denied_ws=WireSocket(); denied_rpc=BoundedNativeRpc(denied_ws)
                    with self.assertRaises(NativeRpcDenied):
                        await denied_rpc(method,dict(params,**{key:'local'}))
                    self.assertEqual(denied_ws.frames,[])
        await rpc('groups.peer.invite',dict(invite,status_ttl_seconds=600))
        await rpc('groups.peer.register',register)
        for frame in ws.frames:
            self.assertLessEqual(set(frame['params']),set(REFERENCE['allowed'][frame['method']]))

    async def test_pre_effect_invite_cancel_denies_without_frame(self):
        ws=WireSocket(); rpc=BoundedNativeRpc(ws)
        call=rpc.bind_request(invite_admission=lambda:False,deadline=time.monotonic()+2)
        with self.assertRaises(AdmissionDenied):
            await call('groups.peer.invite',dict(room_id='r',home_install_id='h',authority_gateway_id='h',authority_epoch=1,member_id='m'))
        self.assertEqual(ws.frames,[])

    async def test_register_producer_preserves_local_fence_not_route_identity(self):
        room=dict(room_id='room',name='test',members=[],authority_gateway_id='install:'+'a'*32,authority_epoch=1)
        material=PeerMaterial(dict(SELF_BINDING),CATALOG,'opaque-native','http://127.0.0.1:19001/links/unit')
        store=mock.Mock(); frames=[]; fence=mock.Mock(return_value=None)
        async def rpc(method,params):
            if method=='groups.peer.register':
                frames.append(params)
                self.assertIs(REGISTER_ADMISSION.get(),fence)
                fence()  # same admission capability remains request-local
                self.assertLessEqual(set(params),set(REFERENCE['allowed'][method]))
                return dict(registered=True,target_install_id=SELF_BINDING['target_install_id'],target_profile='default')
            if method=='groups.state':
                return dict(room=room,driver_status={'peer_routes':[dict(room_id='room',member_id='peer',status='ready',trace_id='native-trace',cancellation_scope_id='native-cancel')]})
            raise AssertionError(method)
        svc=RoomLobbyService(store,rpc,None,register_admission=lambda m,v:fence)
        svc._plan=mock.AsyncMock(return_value=(dict(room_id='room',name='test',members=[]),room['authority_gateway_id'],{'peer':material}))
        svc._create=mock.AsyncMock(return_value=room)
        await svc.activate('lobby')
        self.assertEqual(set(frames[0]),{'room_id','member_id','target_url','target_profile','grant','catalog'})
        fence.assert_called_once_with(); store.mark_peer_registered.assert_called_once_with('lobby','peer')
        self.assertIsNone(REGISTER_ADMISSION.get())

class DurableIssuerIDs(unittest.IsolatedAsyncioTestCase):
    def fixture(self, root):
        fx=joining_fixture.TargetMaterialMintTests()._approved_fixture(root)
        self.addCleanup(fx['joining_store'].close)
        return fx

    def mint(self, fx, rpc, store=None):
        return TargetMaterialMint(store or fx['joining_store'],consent_key=fx['consent_key'],channel=fx['channel'],rpc=rpc,host_peer_id='host-a')

    async def test_issuer_id_is_independent_and_ready_replays_exact_encrypted_bytes_after_reopen(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx=self.fixture(Path(tmp)); calls=[]; secret=b'i'*32; native_id='issuer-owned-not-local'
            mint=None
            async def rpc(method,params):
                calls.append(dict(params))
                self.assertLessEqual(set(params),set(REFERENCE['allowed'][method]))
                scope=mint.approved_scope(fx['payload']); key=canonical(scope).decode()
                self.assertEqual(fx['joining_store'].mint_state(key),'intent')
                self.assertNotEqual(fx['joining_store'].mint_grant_id(key),native_id)
                token=issue_room_grant(secret,grant_id=native_id,room_id=params['room_id'],home_install_id=params['home_install_id'],authority_gateway_id=params['authority_gateway_id'],authority_epoch=params['authority_epoch'],member_id=params['member_id'],target_install_id=SELF_BINDING['target_install_id'],target_profile=params['profile'],execution_policy_digest=SELF_BINDING['policy_digest'],issued_at=time.time(),ttl_seconds=params['ttl_seconds'])
                return dict(grant=token,target_profile='default',catalog=CATALOG,endpoint=CATALOG['endpoint'])
            mint=self.mint(fx,rpc)
            self.assertEqual(await mint.mint(fx['payload']),{'status':'material_ready'})
            scope=mint.approved_scope(fx['payload']); key=canonical(scope).decode(); store=fx['joining_store']
            raw=store.mint_load(key); local=store.mint_grant_id(key); envelope=json.loads(raw)
            self.assertEqual(decode_room_grant(secret,envelope['grant'],permission='dispatch')['grant_id'],native_id)
            self.assertNotEqual(local,native_id); self.assertEqual(calls[0]['ttl_seconds'],600)
            self.assertNotIn('status_ttl_seconds',calls[0])  # no horizon broadening
            store.close(); reopened=JoiningStore(store.root); self.addCleanup(reopened.close)
            replay=self.mint(fx,rpc,reopened)
            self.assertEqual(await replay.mint(fx['payload']),{'status':'material_ready'})
            self.assertEqual(reopened.mint_load(key),raw)
            self.assertEqual(canonical(fx['channel'].material),raw)
            self.assertEqual(len(calls),1)
            with reopened._read() as db:
                row=db.execute('SELECT nonce,ciphertext FROM material WHERE scope_key=?',(key,)).fetchone()
            self.assertNotIn(envelope['grant'].encode(),bytes(row['ciphertext']))

    async def test_lost_response_and_cancel_leave_intent_never_reissue_after_reopen(self):
        for cancelled in (False,True):
            with self.subTest(cancelled=cancelled), tempfile.TemporaryDirectory() as tmp:
                fx=self.fixture(Path(tmp)); calls=[]; issued=asyncio.Event()
                async def rpc(method,params):
                    # Simulated peer effect uses real native token issuance,
                    # then loses its response or waits for caller cancellation.
                    token=issue_room_grant(b'l'*32,grant_id='issuer-lost-response',
                        room_id=params['room_id'],home_install_id=params['home_install_id'],
                        authority_gateway_id=params['authority_gateway_id'],authority_epoch=1,
                        member_id=params['member_id'],target_install_id=SELF_BINDING['target_install_id'],
                        target_profile='default',execution_policy_digest=SELF_BINDING['policy_digest'],
                        issued_at=time.time(),ttl_seconds=600)
                    calls.append(token); issued.set()
                    if cancelled: await asyncio.Event().wait()
                    raise TimeoutError('lost response after issuance')
                mint=self.mint(fx,rpc); key=canonical(mint.approved_scope(fx['payload'])).decode()
                task=asyncio.create_task(mint.mint(fx['payload']))
                await asyncio.wait_for(issued.wait(),2)
                if cancelled: task.cancel()
                with self.assertRaises(asyncio.CancelledError if cancelled else MintAmbiguous): await task
                store=fx['joining_store']; local=store.mint_grant_id(key)
                self.assertEqual(store.mint_state(key),'intent'); store.close()
                reopened=JoiningStore(store.root); self.addCleanup(reopened.close)
                self.assertEqual(reopened.mint_grant_id(key),local)
                with self.assertRaises(MintAmbiguous): await self.mint(fx,rpc,reopened).mint(fx['payload'])
                self.assertEqual(len(calls),1)

    async def test_local_pre_intent_admission_cancel_creates_no_intent_or_native_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx=self.fixture(Path(tmp)); rpc=mock.AsyncMock(); mint=self.mint(fx,rpc)
            key=canonical(mint.approved_scope(fx['payload'])).decode()
            with self.assertRaises(JoiningStoreError):
                await mint.mint(fx['payload'],final_admission=lambda:False)
            self.assertIsNone(fx['joining_store'].mint_state(key)); rpc.assert_not_called()

class ActualNativeFieldRejection(unittest.IsolatedAsyncioTestCase):
    """Real frozen dispatch admission; downstream effects are inert fixtures.

    This covers actor/profile/field validation, not native issuance/register
    effects, authenticated transport, catalog probing, or HTTP activation.
    """
    async def test_actual_dispatch_accepts_canonical_fields_before_inert_effect_boundary(self):
        from gateway import session_group_controls as controls
        from hermes_constants import get_hermes_home
        home=Path(get_hermes_home())
        actor=SimpleNamespace(profile_id=str(home),capabilities={'session:operator','session:control'})
        authority=SimpleNamespace(profile_id=str(home),db=SimpleNamespace(db_path=str(home/'state.db')))
        connection=SimpleNamespace(actor=actor,authority=authority,host_scoped=False)
        requests={
            'groups.peer.invite':dict(profile='default',room_id='room',home_install_id='home',authority_gateway_id='home',authority_epoch=1,member_id='peer',ttl_seconds=600),
            'groups.peer.register':dict(room_id='room',member_id='peer',target_url='http://127.0.0.1:19001/links/unit',target_profile='default',grant='opaque-native',catalog=CATALOG),
        }
        # Run real capability/profile/field and owned-home checks, then stop
        # at the terminal invoke boundary BEFORE bootstrapping gateway.run.
        # The sidecar CI intentionally installs only its own dependencies.
        # No native runtime scope, handler effect or transport is claimed.
        original_to_thread = controls.asyncio.to_thread
        for method,params in requests.items():
            terminal=[]
            async def stop_before_runtime(fn,*args,**kwargs):
                if fn.__module__ == controls.__name__ and fn.__name__ == 'invoke':
                    terminal.append(fn)
                    return {'effect_fixture':True}
                return await original_to_thread(fn,*args,**kwargs)
            with mock.patch.object(controls.asyncio,'to_thread',new=stop_before_runtime):
                result=await controls.dispatch_group_control(connection,method,params)
            self.assertEqual(result,{'effect_fixture':True})
            self.assertEqual(len(terminal),1)

    async def test_actual_dispatch_rejects_fields_from_legacy_MC_wire(self):
        from gateway.session_group_controls import dispatch_group_control, _FIELDS
        from hermes_state_runtime import RuntimeStoreError
        for method in REFERENCE['allowed']:
            self.assertEqual(set(REFERENCE['allowed'][method]),_FIELDS[method]|{'profile'})
        actor=SimpleNamespace(profile_id='private',capabilities={'session:operator','session:control'})
        connection=SimpleNamespace(actor=actor,authority=SimpleNamespace(profile_id='private'))
        for method,key in [('groups.peer.invite','grant_id'),('groups.peer.register','trace_id'),('groups.peer.register','cancellation_scope_id')]:
            with self.assertRaises(RuntimeStoreError) as raised:
                await dispatch_group_control(connection,method,{key:'local'})
            self.assertEqual(raised.exception.reason,'invalid_params')
