"""Per-sidecar relay sessions derive outer identities from current journal trust."""
from __future__ import annotations

import asyncio
import contextlib
import json
import ssl
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from invite_rooms.pairing_journal import PeerTrust
from invite_rooms.relay_transport import RelaySessionManager, RelayTransportError, relay_identity_for
from invite_rooms.runtime_config import RelayLinkConfig
from server.tests import lab_pki

ROOT=Path(__file__).resolve().parents[2]
SPIKE=ROOT/"spikes"/"invite-rooms"
sys.path.insert(0,str(SPIKE))
import relay_daemon
from test_room_invite_relay_transport import _EchoServer

INSTALL_A="install:"+"a"*32
INSTALL_B="install:"+"b"*32
ENDPOINT="link-"+"c"*32
PEER_A="peer-"+"a"*32
PEER_B="peer-"+"b"*32
LINK_ID="rly-link-manager-test"
RELAY_ROOM="rly-room-manager-test"
ADAPTER="link-"+"d"*32


def _trust(local_role, peer_id, peer_install, local_leaf, peer_ca, endpoint=ENDPOINT, generation=1, state="acknowledged"):
    binding={"endpoint_id":endpoint,"fingerprint":local_leaf,"target_install_id":peer_install,
             "target_profile":"default","catalog_digest":"c"*64,"policy_digest":"d"*64}
    return PeerTrust(endpoint,generation,local_role,state,peer_id,peer_ca,local_leaf,tuple(sorted(binding.items())))


class _Authority:
    def __init__(self, trust):
        self.trust=trust
    def active_trusts(self):
        return () if self.trust is None else (self.trust,)
    def peer_trust(self, endpoint, generation, *, role, states):
        t=self.trust
        return t if (t is not None and (t.endpoint_id,t.generation,t.role)==(endpoint,generation,role)
                     and t.protocol_state in states) else None


class RelaySessionManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix="relay-manager-");self.root=Path(self.tmp.name)
        owner_a=lab_pki.init_owner(self.root/"a","a")
        owner_b=lab_pki.init_owner(self.root/"b","b")
        relay_ca=lab_pki.init_owner(self.root/"relay-ca","relay")
        relay_cert=lab_pki.issue_server(self.root/"relay-ca",self.root/"relay-tls","IP:127.0.0.1")
        provisioned=relay_daemon.provision(self.root/"routes.json",self.root/"creds",link_id=LINK_ID,
            room_id=RELAY_ROOM,source=relay_identity_for(INSTALL_A,LINK_ID),target=relay_identity_for(INSTALL_B,LINK_ID))
        self.daemon=relay_daemon.RelayDaemon(self.root/"routes.json",host="127.0.0.1",port=0,
            tls=relay_daemon._server_tls(relay_cert["cert_file"],relay_cert["key_file"]))
        self.loop=asyncio.new_event_loop();self.ready=threading.Event()
        def run():
            asyncio.set_event_loop(self.loop);self.loop.run_until_complete(self.daemon.run(self.ready.set,install_signals=False))
        self.daemon_thread=threading.Thread(target=run,daemon=True);self.daemon_thread.start()
        self.assertTrue(self.ready.wait(5))
        self.echo_a=_EchoServer();self.echo_b=_EchoServer();self.echo_data=_EchoServer()
        self.trust_a=_trust("host",PEER_B,INSTALL_B,owner_b["fingerprint"],Path(owner_b["ca_file"]).read_bytes())
        self.trust_b=_trust("joining",PEER_A,INSTALL_A,owner_a["fingerprint"],Path(owner_a["ca_file"]).read_bytes())
        cred_a=Path(provisioned["credential_files"][relay_identity_for(INSTALL_A,LINK_ID)])
        cred_b=Path(provisioned["credential_files"][relay_identity_for(INSTALL_B,LINK_ID)])
        self.link_a=RelayLinkConfig(ENDPOINT,1,PEER_B,LINK_ID,RELAY_ROOM,cred_a)
        self.link_b=RelayLinkConfig(ENDPOINT,1,PEER_A,LINK_ID,RELAY_ROOM,cred_b)
        url=f"wss://127.0.0.1:{self.daemon.bound_port}/data"
        self.auth_a=_Authority(self.trust_a);self.auth_b=_Authority(self.trust_b)
        self.manager_a=RelaySessionManager(relay_url=url,links={ENDPOINT:self.link_a},installation_id=INSTALL_A,
            authority=self.auth_a,control_target=self.echo_a.address,data_targets={},
            relay_ssl_context=ssl.create_default_context(cafile=relay_ca["ca_file"]))
        self.manager_b=RelaySessionManager(relay_url=url,links={ENDPOINT:self.link_b},installation_id=INSTALL_B,
            authority=self.auth_b,control_target=self.echo_b.address,data_targets={ADAPTER:self.echo_data.address},
            relay_ssl_context=ssl.create_default_context(cafile=relay_ca["ca_file"]))

    def tearDown(self):
        for manager in (getattr(self,"manager_a",None),getattr(self,"manager_b",None)):
            if manager is not None:
                with contextlib.suppress(Exception):manager.close()
        for echo in (getattr(self,"echo_a",None),getattr(self,"echo_b",None),getattr(self,"echo_data",None)):
            if echo is not None:echo.close()
        if getattr(self,"daemon",None) is not None and getattr(self.daemon,"stop_event",None) is not None:
            self.loop.call_soon_threadsafe(self.daemon.stop_event.set)
        if getattr(self,"daemon_thread",None) is not None:self.daemon_thread.join(5)
        if getattr(self,"loop",None) is not None and not self.loop.is_closed():self.loop.close()
        self.tmp.cleanup()

    def test_manager_shares_one_peer_session_for_control_and_data_and_refuses_after_unpair(self):
        self.assertEqual(self.manager_a.refresh(),1)
        self.assertEqual(self.manager_b.refresh(),1)
        client_a=self.manager_a.client_for(self.trust_a)
        client_b=self.manager_b.client_for(self.trust_b)
        self.assertEqual((self.manager_a.session_count,self.manager_b.session_count),(1,1))
        for client, channel, echo, payload in (
            (client_a,"control",self.echo_b,b"status-call"),
            (client_b,"control",self.echo_a,b"pair-ack"),
            (client_a,"data:"+ADAPTER,self.echo_data,b"room-data"),
        ):
            with client.open_stream(channel=channel,timeout=5) as stream:
                stream.settimeout(5)
                stream.sendall(len(payload).to_bytes(4,"big")+payload)
                size=int.from_bytes(_EchoServer._read_exact(stream,4),"big")
                self.assertEqual(_EchoServer._read_exact(stream,size),b"echo:"+payload)
            self.assertEqual(echo.calls[-1],payload)
        self.auth_b.trust=None
        with client_a.open_stream(channel="control",timeout=5) as stream:
            stream.settimeout(3)
            stream.sendall(b"should-not-route")
            self.assertEqual(stream.recv(64),b"")
        self.assertEqual(self.echo_b.calls, [b"status-call"])
        self.manager_a.close()
        self.manager_b.close()
        self.assertEqual((self.manager_a.session_count,self.manager_b.session_count),(0,0))
        self.assertFalse([thread.name for thread in threading.enumerate()
                          if thread.name.startswith("relay-") and thread is not threading.current_thread()])
    def test_failed_client_cleanup_retains_session_for_retry(self):
        self.manager_a.refresh()
        session=self.manager_a._sessions[(ENDPOINT,1)]
        original_stop=session.client.stop
        attempts=[]
        def fail_once():
            attempts.append(1)
            if len(attempts)==1:
                raise RelayTransportError("connector_teardown_incomplete")
            original_stop()
        session.client.stop=fail_once
        try:
            with self.assertRaises(RelayTransportError):
                self.manager_a.close()
            self.assertEqual(self.manager_a.session_count,1)
            self.manager_a.close()
            self.assertEqual((self.manager_a.session_count,len(attempts)),(0,2))
        finally:
            if session.client._thread is not None and session.client._thread.is_alive():
                original_stop()


if __name__=="__main__":
    unittest.main()
