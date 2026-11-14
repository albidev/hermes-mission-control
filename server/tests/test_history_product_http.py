"""Maintained HTTP/default-factory/mTLS history integration.

Native state/log is an explicit recording fixture; peer identities, journals,
SQLite stores, production HTTP routes and the pinned mTLS transport are real.
"""
import asyncio,base64,copy,json,os,sqlite3,threading,time,unittest
from pathlib import Path
from contextlib import asynccontextmanager
from http.client import HTTPConnection
from unittest.mock import patch
import test_control_peer_context_w2b as w2b
import pairing_support as ps
from invite_rooms import runtime_config,routes
from invite_rooms.history_consent import HistoryConsentStore,PURPOSE_OWNER,PURPOSE_GUEST,HistoryConsentError
from invite_rooms.history_host import OwnerHistoryHandlers,_Signer
from invite_rooms.joining_store import JoiningStore
from invite_rooms.guest import GuestRoomService,GuestServiceError
from invite_rooms.consent import verify
from invite_rooms.pairing_journal import binding_for
from room_invite_store import RoomInviteStore
_previous_pairing_templates = None

def setUpModule():
    global _previous_pairing_templates
    _previous_pairing_templates = (getattr(w2b, 'TEMPLATES', None), getattr(w2b, 'FOREIGN', None))
    w2b.setUpModule()

def tearDownModule():
    w2b.tearDownModule()
    previous, foreign = _previous_pairing_templates
    if previous is not None:
        w2b.TEMPLATES = previous
    if foreign is not None:
        w2b.FOREIGN = foreign
def private_json(path,value):
 path.parent.mkdir(mode=0o700,parents=True,exist_ok=True);path.write_text(json.dumps(value));path.chmod(0o600)
class ProductOwnerFixture(w2b.H1Base):
 def setUp(self):
  super().setUp(); self.endpoint,self.generation=self.ack(self.a,self.b,self.paired_b)
  self.peer=self.link(self.endpoint)['peer']['peer_id'];self.binding=binding_for(self.link(self.endpoint))
  self.owner_signer=_Signer(self.ba.consent_key());self.guest_signer=_Signer(self.bb.consent_key())
  self.cfg=self.tmp/'owner-cfg';self.cfg.mkdir(mode=0o700)
  self.store=RoomInviteStore(self.cfg/'invites.sqlite');self.store.bind_owner_scope(ps.INSTALL_A,'default')
  self.roster=[{'member_id':'local-a','profile':'default','handle':'owner'},
   {'member_id':'member-b','profile':'default','handle':'guest','target':{'kind':'peer','peer_id':self.peer,'installation_id':ps.INSTALL_B,'profile':'default','capability_digest':self.binding['catalog_digest']}}]
  lobby=self.store.create_lobby(name='Private review',roster=self.roster,history_policy='full');self.lobby=lobby['lobby_id'];self.room=lobby['planned_room_id']
  self.invite=self.ready_invite();self.store.seal_lobby(self.lobby)
  self.store.pin_native_spec(self.lobby,{'room_id':self.room,'name':'Private review','members':self.roster})
  self.store.mark_created(self.lobby,self.room);self.store.mark_peer_registered(self.lobby,'member-b')
  self.store.record_history_admission(self.lobby,'member-b',self.invite.invite_id,1)
  self.epoch=7;self.authority=ps.INSTALL_A;self.members=copy.deepcopy(self.roster);self.calls=[];self.hook=None;self.big=False
  self.history=OwnerHistoryHandlers(store=self.store,owner_installation_id=ps.INSTALL_A,owner_key=self.owner_signer,consent_db=self.cfg/'history-consent.sqlite',native_rpc=self.native)
  self.item=self.listen_host({'history_read_scope':self.history.history_read_scope,'history_read_acceptance':self.history.history_read_acceptance,'room_log':self.history.room_log})
  self.ledger=lambda:HistoryConsentStore(self.cfg/'history-consent.sqlite',owner_public_key=self.ba.consent_public_key,guest_public_key=self.bb.consent_public_key)
  token=self.cfg/'native-token';token.write_text('private-synthetic-native-token');token.chmod(0o600)
  self.config=self.cfg/'config.json'
  private_json(self.config,{'store_path':str(self.store.path),'installation_id':ps.INSTALL_A,'profile':'default','peer_registry_path':str(self.A.peers_path),
   'pairing':{'identity_dir':str(self.A.identity_dir),'journal_dir':str(self.A.journal_dir),'peers_path':str(self.A.peers_path)},
   'native_rpc':{'endpoint':'ws://127.0.0.1:19873','token_path':str(token)}})
  self.env=patch.dict(os.environ,{'MISSION_CONTROL_ROOM_INVITES_CONFIG':str(self.config),'MISSION_CONTROL_TOKEN':'private-review-owner-token','HERMES_HOME':str(self.tmp/'hermes')})
  self.env.start();self.addCleanup(self.env.stop)
  import local_telemetry_server as telemetry
  self.httpd=telemetry.ThreadingHTTPServer(('127.0.0.1',0),telemetry.Handler)
  self.thread=threading.Thread(target=self.httpd.serve_forever,daemon=True);self.thread.start();self.addCleanup(self.close_http)
  @asynccontextmanager
  async def connect_native(config): yield self.native
  self.patch_rpc=patch.object(routes,'_connect_native_rpc',connect_native);self.patch_rpc.start();self.addCleanup(self.patch_rpc.stop)
 def close_http(self): self.httpd.shutdown();self.httpd.server_close();self.thread.join(3)
 def ready_invite(self):
  inv=self.store.issue_invite(self.lobby,'member-b',invitee_label='guest')
  self.store.redeem(self.lobby,inv.invite_id,inv.secret,binding=self.binding)
  self.store.decide_host(self.lobby,inv.invite_id,binding=self.binding,approved=True)
  self.store.consent_joining(self.lobby,inv.invite_id,binding=self.binding,consented=True);return inv
 async def native(self,method,params,*,effect_admission=None,deadline=None):
  if effect_admission is not None and effect_admission() is not True: raise ValueError('admission')
  self.calls.append(method)
  if method=='groups.state': result={'room':{'room_id':self.room,'home_install_id':ps.INSTALL_A,'authority_gateway_id':self.authority,'authority_epoch':self.epoch,'status':'live','members':copy.deepcopy(self.members)},'driver_status':{'peer_routes':[{'member_id':'member-b','status':'ready'}]}}
  elif method=='groups.log':
   events=[{'seq':i,'event_id':'canonical-'+str(i),'kind':'message.user' if i!=2 else 'control.private','room_id':self.room,'authority_epoch':7,'actor':{'kind':'member','id':'member-b'},'payload':{'text':('x'*17000 if self.big else 'hello')} ,'secret':'NONPUBLIC-CANARY'} for i in (1,2,3)]
   events=[e for e in events if e['seq']>params['since_seq']][:params['limit']]
   cursor=events[-1]['seq'] if events else params['since_seq'];result={'events':events,'cursor':cursor,'latest_seq':3,'has_more':cursor<3,'authority':{'gateway_id':self.authority,'epoch':self.epoch}}
  else: raise AssertionError(method)
  if self.hook: self.hook(method)
  return result
 def http(self,payload=None,*,token='private-review-owner-token',origin=None,raw=None):
  c=HTTPConnection(*self.httpd.server_address,timeout=5)
  headers={'Content-Type':'application/json'}
  if token is not None: headers['Authorization']='Bearer '+token
  if origin is not None: headers['Origin']=origin
  c.request('POST',routes.PREFIX+'/rooms/'+self.room+'/history-approval',body=raw if raw is not None else json.dumps(payload or {'recipient_id':self.peer}),headers=headers)
  r=c.getresponse();data=r.read();status=r.status;c.close();return status,json.loads(data)
 def wire(self,req,*,client=None,endpoint=None,generation=None):
  result=self.send(self.item,client=client or self.bb,server=self.ba,request=req,endpoint_id=endpoint or self.endpoint,generation=generation or self.generation)
  return result[1],json.loads(result[2]) if result[0]=='ok' else result
 def scope(self): return self.wire({'op':'history_read_scope','room_id':self.room})
 def log(self): return self.wire({'op':'room_log','room_id':self.room,'since_seq':0,'limit':10})
 def approve(self):
  status,response=self.http();self.assertEqual(status,200,response);return response['scope']
 def accept(self,scope):
  receipt=dict(scope,purpose=PURPOSE_GUEST)
  return self.wire({'op':'history_read_acceptance','scope':scope,'receipt':receipt,'signature':self.guest_signer.sign(receipt)})

from invite_rooms.consent import canonical

class DefaultGuestFactoryHTTP(ProductOwnerFixture):
    def setUp(self):
        super().setUp()
        import local_telemetry_server as telemetry
        self.telemetry = telemetry
        # Build a real guest-side durable import and confirmed scope from B's paired journal.
        self.guest_store = JoiningStore(self.tmp / "guest-joining")
        self.b_link = next(link for link in self.b.journal.snapshot().active_links() if link.get("role") == "joining")
        b_link = self.b_link
        host_peer_id = b_link["peer"]["peer_id"]
        self.host_peer_id = host_peer_id
        iid = self.guest_store.create_import(
            lobby_id=self.lobby, invite_id=self.invite.invite_id, secret="x" * 43,
            host_peer_id=host_peer_id)
        confirmed = {
            "room_id": self.room, "member_id": "member-b", "host_id": ps.INSTALL_A,
            "binding": self.binding,
        }
        self.guest_store.confirm_scope(iid, confirmed)
        self.guest_store.mark_redeemed(iid)
        self.guest_store.begin_consent(iid, receipt={"scope": confirmed}, signature="fixture-execution-receipt")
        self.guest_store.mark_consent_delivered(iid)
        mint_key = canonical(dict(confirmed, invite_id=self.invite.invite_id)).decode()
        self.guest_store.mint_begin(mint_key)
        self.guest_store.mint_complete(mint_key, canonical({"grant": "guest-grant"}))

        from room_invite_store import RoomInviteStore
        guest_room_store = RoomInviteStore(self.tmp / "guest-room.sqlite")
        guest_room_store.bind_owner_scope(ps.INSTALL_B, "default")

        owner_cfg = json.loads(self.config.read_text())
        owner_cfg["history_enabled"] = True
        self.config.write_text(json.dumps(owner_cfg))
        self.config.chmod(0o600)

        guest_cfg = self.tmp / "guest-config.json"
        guest_cfg.parent.mkdir(mode=0o700, exist_ok=True)
        guest_cfg.write_text(json.dumps({
            "store_path": str(guest_room_store.path),
            "installation_id": ps.INSTALL_B,
            "profile": "default",
            "peer_registry_path": str(self.B.peers_path),
            "pairing": {
                "identity_dir": str(self.B.identity_dir),
                "journal_dir": str(self.B.journal_dir),
                "peers_path": str(self.B.peers_path),
            },
            "joining": {
                "store_dir": str(self.guest_store.root),
                "identity_dir": str(self.B.identity_dir),
                "hosts_path": str(self.tmp / "guest-hosts.json"),
                "origins": {"owner": "http://127.0.0.1"},
                "control_endpoints": {
                    host_peer_id: {"host": "127.0.0.1", "port": self.item.address[1]},
                },
            },
            "history_enabled": True,
            "native_rpc": owner_cfg["native_rpc"],
        }))
        guest_cfg.chmod(0o600)
        self.guest_config = guest_cfg
        self.guest_httpd = telemetry.ThreadingHTTPServer(("127.0.0.1", 0), telemetry.Handler)
        self.guest_thread = threading.Thread(target=self.guest_httpd.serve_forever, daemon=True)
        self.guest_thread.start()
        self.addCleanup(self._close_guest_http)
        # Ensure the route passes None into runtime_config, exercising its production default.
        self.factory_patch = patch.object(routes, "_HOST_CHANNEL_FACTORY", None, create=True)
        self.factory_patch.start()
        self.addCleanup(self.factory_patch.stop)
    def _close_guest_http(self):
        self.guest_httpd.shutdown(); self.guest_httpd.server_close(); self.guest_thread.join(3)

    def guest_request(self, method, path, body=None):
        os.environ["MISSION_CONTROL_ROOM_INVITES_CONFIG"] = str(self.guest_config)
        conn = HTTPConnection(*self.guest_httpd.server_address, timeout=8)
        headers = {"Authorization": "Bearer private-review-owner-token", "Content-Type": "application/json"}
        conn.request(method, path, body=json.dumps(body or {}), headers=headers)
        response = conn.getresponse(); raw = response.read(); status = response.status; conn.close()
        return status, json.loads(raw)

    def test_default_factory_real_http_mtls_restart_pagination_and_revocation(self):
        # Owner approval is explicit and goes through the production HTTP route first.
        os.environ["MISSION_CONTROL_ROOM_INVITES_CONFIG"] = str(self.config)
        status, owner = self.http()
        self.assertEqual(status, 200, owner)
        self.assertEqual(owner["status"], "owner_approved")

        consent_path = f"/api/local/room-invites/guest-rooms/{self.room}/history-consent"
        os.environ["MISSION_CONTROL_ROOM_INVITES_CONFIG"] = str(self.guest_config)
        from invite_rooms import runtime_config
        self.assertIsNotNone(runtime_config._load_config_file(str(self.guest_config)), "guest config parse failed")
        self.assertTrue(runtime_config._load_config_file(str(self.guest_config)).history_enabled)
        cfg_obj = runtime_config._load_config_file(str(self.guest_config))
        from invite_rooms.runtime_joining import _config_connector_factory
        connector = _config_connector_factory(cfg_obj)(self.b_link, 5.0)
        self.assertTrue(callable(connector.forward))
        status, accepted = self.guest_request("POST", consent_path, {})
        self.assertEqual(status, 200, accepted)
        self.assertEqual(accepted["status"], "ready")

        log_path = f"/api/local/room-invites/guest-rooms/{self.room}/log?since_seq=0&limit=2"
        status, page = self.guest_request("GET", log_path)
        self.assertEqual(status, 200, page)
        self.assertEqual([e["event_id"] for e in page["events"]], ["canonical-1"])
        self.assertTrue(page["has_more"])
        status, page2 = self.guest_request("GET", f"/api/local/room-invites/guest-rooms/{self.room}/log?since_seq=1&limit=2")
        self.assertEqual(status, 200, page2)
        self.assertEqual([e["event_id"] for e in page2["events"]], ["canonical-3"])
        self.assertFalse(page2["has_more"])

        from invite_rooms import runtime_config
        loaded_cfg = runtime_config._load_config_file(str(self.guest_config))
        self.assertIsNotNone(loaded_cfg, "guest config parse failed")
        self.assertIsNotNone(runtime_config.load_service(), "guest owner service load failed")

        # Restart the guest-side loader/store and prove durable owner+guest consent is reused.
        self.guest_store = JoiningStore(self.guest_store.root)
        status, page = self.guest_request("GET", f"/api/local/room-invites/guest-rooms/{self.room}/log?since_seq=0&limit=2")
        self.assertEqual(status, 200, page)
        self.assertEqual([e["event_id"] for e in page["events"]], ["canonical-1"])

        # Journal revocation is revalidated by the default channel before any send.
        self.b.unpair(self.host_peer_id)
        status, denied = self.guest_request("GET", f"/api/local/room-invites/guest-rooms/{self.room}/log")
        self.assertNotEqual(status, 200)
        self.assertEqual(status, 404, denied)
        self.assertEqual(self.calls.count("groups.log"), 3)



    def enroll(self):
        os.environ['MISSION_CONTROL_ROOM_INVITES_CONFIG'] = str(self.config)
        status, approved = self.http()
        self.assertEqual(status, 200, approved)
        status, accepted = self.guest_request('POST', f'/api/local/room-invites/guest-rooms/{self.room}/history-consent', {})
        self.assertEqual(status, 200, accepted)
        return f'/api/local/room-invites/guest-rooms/{self.room}/log?since_seq=0&limit=2'

    def test_canonical_target_substitution_denies_before_log(self):
        path = self.enroll()
        self.members[1]['target']['installation_id'] = 'install:' + 'c' * 32
        before = self.calls.count('groups.log')
        status, denied = self.guest_request('GET', path)
        self.assertNotEqual(status, 200, denied)
        self.assertEqual(self.calls.count('groups.log'), before)

    def test_admission_removed_during_io_does_not_release_buffered_page(self):
        path = self.enroll()
        def cutover(method):
            if method == 'groups.log':
                with self.store._write() as conn:
                    conn.execute('DELETE FROM history_admissions WHERE lobby_id=?', (self.lobby,))
        self.hook = cutover
        status, denied = self.guest_request('GET', path)
        self.assertNotEqual(status, 200, denied)

    def test_missing_owner_approval_cannot_read_or_accept(self):
        status, denied = self.guest_request('POST', f'/api/local/room-invites/guest-rooms/{self.room}/history-consent', {})
        self.assertNotEqual(status, 200, denied)
        self.assertEqual(self.calls.count('groups.log'), 0)
