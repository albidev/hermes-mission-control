"""Product HostConnector/TargetConnector round-trip through an opaque WSS relay."""
from __future__ import annotations

import asyncio
import contextlib
import http.server
import json
import socket
import ssl
import sys
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

from invite_rooms.control.dispatcher import LinkState
from invite_rooms.control.connector import LinkBinding, LinkIdentity, TargetConnector, make_link_connector
from invite_rooms.control.adapter import LoopbackAdapter
from invite_rooms.pairing_journal import PeerTrust
from invite_rooms.peer_registry import MaterialScope, adapter_id_for
from invite_rooms.relay_transport import RelaySessionManager, relay_identity_for
from invite_rooms.runtime_config import RelayLinkConfig
from server.tests import lab_pki

ROOT = Path(__file__).resolve().parents[2]
SPIKE = ROOT / "spikes" / "invite-rooms"
sys.path.insert(0, str(SPIKE))
import relay as relay_server
import relay_daemon

SENTINEL = b"HERMESROOM_PRIVATE_PROMPT_53c2"
AUTH = "HermesRoom relay-test-only-secret"
SCOPE = {"room_id": "room-prod-relay", "home_install_id": "install:" + "a" * 32,
         "authority_gateway_id": "gw:" + "a" * 32, "authority_epoch": 1,
         "member_id": "peer-b", "target_install_id": "install:" + "b" * 32,
         "target_profile": "default"}


class _Authority:
    def __init__(self, binding):
        self.state = LinkState(binding, 1, "acknowledged")
        self.calls = []
        self.lock = threading.RLock()

    def current(self, endpoint):
        with self.lock:
            return self.state if self.state.binding.get("endpoint_id") == endpoint else None

    def admit(self, endpoint, generation, operation):
        with self.lock:
            state = self.current(endpoint)
            if state is None or state.generation != generation or state.protocol_state != "acknowledged":
                return False
            self.calls.append(operation)
            return True


class _ManagerAuthority:
    def __init__(self, trust):
        self.trust = trust
    def active_trusts(self):
        return (self.trust,)
    def peer_trust(self, endpoint_id, generation, *, role, states):
        trust = self.trust
        if ((trust.endpoint_id, trust.generation, trust.role) != (endpoint_id, generation, role)
                or trust.protocol_state not in states):
            return None
        return trust


def _peer_trust(role, peer_id, peer_installation_id, ca_pem, fingerprint):
    binding = {"endpoint_id": "endpoint-1", "fingerprint": fingerprint,
        "target_install_id": peer_installation_id, "target_profile": "default",
        "catalog_digest": "c" * 64, "policy_digest": "d" * 64}
    return PeerTrust("endpoint-1", 1, role, "acknowledged", peer_id, ca_pem, fingerprint,
                     tuple(sorted(binding.items())))


class _Native:
    def __init__(self):
        self.seen = []
        native = self
        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            def log_message(self, *args): pass
            def do_GET(self):
                body = json.dumps({"installation_id": SCOPE["target_install_id"], "protocol_versions": [2],
                                   "text": True, "catalog_digest": "a" * 64}).encode()
                native.seen.append((self.command, self.path, self.headers.get("Authorization"), body))
                self.send_response(200); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
            def do_POST(self):
                n=int(self.headers.get("Content-Length", "0")); body=self.rfile.read(n)
                native.seen.append((self.command, self.path, self.headers.get("Authorization"), body))
                response=json.dumps({"run_id":"run_"+"1"*32,"status":"started","replayed":False,"output":SENTINEL.decode()}).encode()
                self.send_response(202); self.send_header("Content-Type","application/json")
                self.send_header("Content-Length",str(len(response))); self.end_headers(); self.wfile.write(response)
        self.server=http.server.ThreadingHTTPServer(("127.0.0.1",0),Handler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True); self.thread.start()
        self.url=f"http://127.0.0.1:{self.server.server_port}"
    def close(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(2)


class ProductRelayDataPathTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix="product-relay-data-")
        self.root=Path(self.tmp.name)
        self.a=lab_pki.init_owner(self.root/"owner-a","a")
        self.b=lab_pki.init_owner(self.root/"owner-b","b")
        relay_ca=lab_pki.init_owner(self.root/"relay-ca","relay")
        relay_cert=lab_pki.issue_server(self.root/"relay-ca",self.root/"relay-tls","IP:127.0.0.1")
        self.provisioned=relay_daemon.provision(self.root/"routes.json",self.root/"creds",link_id="product-data-link",
            room_id="relay-pair",source=relay_identity_for(SCOPE["home_install_id"],"product-data-link"),
            target=relay_identity_for(SCOPE["target_install_id"],"product-data-link"))
        self.daemon=relay_daemon.RelayDaemon(self.root/"routes.json",host="127.0.0.1",port=0,
            tls=relay_daemon._server_tls(relay_cert["cert_file"],relay_cert["key_file"]))
        self.loop=asyncio.new_event_loop(); self.ready=threading.Event()
        def run():
            asyncio.set_event_loop(self.loop); self.loop.run_until_complete(self.daemon.run(self.ready.set,install_signals=False))
        self.daemon_thread=threading.Thread(target=run,daemon=True); self.daemon_thread.start()
        self.assertTrue(self.ready.wait(5),"relay daemon failed to bind")
        self.native=_Native()
        self.target_authority=_Authority({"endpoint_id":"endpoint-1","fingerprint":self.a["fingerprint"]})
        self.host_authority=_Authority({"endpoint_id":"endpoint-1","fingerprint":self.b["fingerprint"]})
        self.target_binding=LinkBinding("endpoint-1",1,("127.0.0.1",1),self.a["ca_file"],
            self.a["fingerprint"],{"endpoint_id":"endpoint-1","fingerprint":self.a["fingerprint"]},timeout=5)
        self.target=TargetConnector(binding=self.target_binding,identity=LinkIdentity(self.b["cert_file"],self.b["key_file"]),
            authority=self.target_authority,expected_scope=SCOPE,verify_native=lambda *_:True,target_base=self.native.url)
        self.target.start()
        self.adapter=LoopbackAdapter(authority=self.host_authority.current); self.adapter.start()
        self.relay_url=f"wss://127.0.0.1:{self.daemon.bound_port}/data"
        self.relay_ssl=ssl.create_default_context(cafile=relay_ca["ca_file"])
        self.trust_host=_peer_trust("host","peer-b",SCOPE["target_install_id"],
            Path(self.b["ca_file"]).read_bytes(),self.b["fingerprint"])
        self.trust_target=_peer_trust("joining","peer-a",SCOPE["home_install_id"],
            Path(self.a["ca_file"]).read_bytes(),self.a["fingerprint"])
        aid=adapter_id_for(self._material_scope())
        relay_host_id=relay_identity_for(SCOPE["home_install_id"],"product-data-link")
        relay_target_id=relay_identity_for(SCOPE["target_install_id"],"product-data-link")
        host_link=RelayLinkConfig("endpoint-1",1,self.trust_host.peer_id,"product-data-link","relay-pair",
            Path(self.provisioned["credential_files"][relay_host_id]))
        target_link=RelayLinkConfig("endpoint-1",1,self.trust_target.peer_id,"product-data-link","relay-pair",
            Path(self.provisioned["credential_files"][relay_target_id]))
        self.manager_host=RelaySessionManager(relay_url=self.relay_url,links={"endpoint-1":host_link},
            installation_id=SCOPE["home_install_id"],authority=_ManagerAuthority(self.trust_host),
            control_target=("127.0.0.1",1),data_targets={},relay_ssl_context=self.relay_ssl)
        self.manager_target=RelaySessionManager(relay_url=self.relay_url,links={"endpoint-1":target_link},
            installation_id=SCOPE["target_install_id"],authority=_ManagerAuthority(self.trust_target),
            control_target=("127.0.0.1",1),data_targets={aid:self.target.address},relay_ssl_context=self.relay_ssl)
        self.assertEqual((self.manager_host.refresh(),self.manager_target.refresh()),(1,1))
        self.host_relay=self.manager_host.client_for(self.trust_host)
        self.target_relay=self.manager_target.client_for(self.trust_target)
        self.bridge=self.manager_target._sessions[("endpoint-1",1)].bridge

    def _material_scope(self):
        return MaterialScope(owner_installation_id=SCOPE["home_install_id"],owner_profile="default",lobby_id="lobby-1",
            planned_room_id=SCOPE["room_id"],member_id=SCOPE["member_id"],invite_id="invite-1",peer_id="peer-b",
            endpoint_id="endpoint-1",fingerprint=self.b["fingerprint"],target_install_id=SCOPE["target_install_id"],
            target_profile="default",catalog_digest="c"*64,policy_digest="d"*64,pairing_generation=1)

    def tearDown(self):
        for manager in (getattr(self,"manager_host",None),getattr(self,"manager_target",None)):
            if manager is not None:
                with contextlib.suppress(Exception):manager.close()
        if getattr(self,"adapter",None) is not None:
            with contextlib.suppress(Exception):self.adapter.close()
        if getattr(self,"target",None) is not None:
            with contextlib.suppress(Exception): self.target.close()
        if getattr(self,"native",None) is not None: self.native.close()
        if getattr(self,"daemon",None) is not None and getattr(self.daemon,"stop_event",None) is not None:
            self.loop.call_soon_threadsafe(self.daemon.stop_event.set)
        if getattr(self,"daemon_thread",None) is not None: self.daemon_thread.join(5)
        if getattr(self,"loop",None) is not None and not self.loop.is_closed(): self.loop.close()
        self.tmp.cleanup()

    def test_product_adapter_forwards_only_over_relay_inner_mtls(self):
        scope=self._material_scope(); aid=adapter_id_for(scope)
        binding=LinkBinding("endpoint-1",1,None,self.b["ca_file"],self.b["fingerprint"],
            {"endpoint_id":"endpoint-1","fingerprint":self.b["fingerprint"]},timeout=5,transport="relay")
        url=self.adapter.register_link("endpoint-1",1,expected_scope=SCOPE,verify_native=lambda *_:True,
            binding=binding,identity=LinkIdentity(self.a["cert_file"],self.a["key_file"]),
            authority=self.host_authority,material_scope=scope,stream_client=self.host_relay)
        seen=[]; frame_meta=[]; original=relay_server.Relay.route
        async def spy(server, connection, frame):
            if frame.get("type")=="DATA":
                raw=__import__("base64").b64decode(frame["payload"])
                seen.append(raw); frame_meta.append((frame.get("source"),len(raw),raw[:5].hex()))
            return await original(server,connection,frame)
        with patch.object(relay_server.Relay,"route",spy):
            request=urllib.request.Request(url+"/v1/room-members/capabilities",headers={"Authorization":AUTH})
            try:
                with urllib.request.urlopen(request,timeout=10) as response:
                    status=response.status; body=response.read()
            except Exception as exc:
                raise AssertionError({"error":type(exc).__name__,"host_connected":self.host_relay.connected,
                    "target_connected":self.target_relay.connected,"host_sessions":self.host_relay.sessions,
                    "target_sessions":self.target_relay.sessions,"bridge_accepted":self.bridge.accepted,
                    "bridge_refused":self.bridge.refused,"target_accepted":self.target.accepted,
                    "target_served":self.target.served,"native_calls":len(self.native.seen),
                    "relay_frames":frame_meta,"target_authority_admissions":self.target_authority.calls,
                    "host_authority_admissions":self.host_authority.calls}) from exc
        self.assertEqual(status,200)
        self.assertEqual(len(self.native.seen),1)
        self.assertEqual(self.native.seen[0][2],AUTH)
        joined=b"".join(seen)
        for marker in (AUTH.encode(),SENTINEL,b"room-members/capabilities"):
            self.assertNotIn(marker,joined)
        self.assertTrue(self.host_authority.calls)


if __name__ == "__main__":
    unittest.main()
