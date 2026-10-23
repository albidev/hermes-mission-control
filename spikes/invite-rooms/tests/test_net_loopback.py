"""Loopback E2E of the networked transport with real processes.

relay_daemon (WSS, own TLS) + host connector process + target connector process
+ a stub HTTP target. This proves the transport mechanics (outbound-only
connectors, inner mTLS through the relay, both-side firewalls, relay restart
without replay). It is NOT native interoperability evidence: the native run
through this transport is gate_f1_local.py, and cross-host is F1.
"""
import asyncio
import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import pki
import relay_daemon

PY = sys.executable
SENTINEL = "PLAINTEXT_SENTINEL_7f3c"
SCOPE = {"room_id": "room-net", "home_install_id": "install:host", "authority_gateway_id": "install:host",
         "authority_epoch": 1, "member_id": "peer-b", "target_install_id": "install:target",
         "target_profile": "default"}
GRANT = "HermesRoom grant-for-test"


class StubNative:
    def __init__(self):
        self.seen = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format, *args):
                return

            def _reply(self, payload):
                body = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                stub.seen.append(("GET", self.path, self.headers.get("Authorization")))
                self._reply({"installation_id": "install:target", "protocol_versions": [2], "text": True,
                             "catalog_digest": SENTINEL})

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                stub.seen.append(("POST", self.path, self.headers.get("Authorization"), self.headers.get("Idempotency-Key"), body))
                self._reply({"run_id": "run_stub_1", "status": "queued", "replayed": False, "output": SENTINEL})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _readline_json(proc, event, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if not line:
            if proc.poll() is not None:
                raise RuntimeError(f"process exited early: {proc.stderr.read()[-2000:]}")
            continue
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if value.get("event") == event:
            return value
    raise TimeoutError(event)


def _request(url, method="GET", body=None, headers=None):
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=40) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _listeners(pid):
    out = subprocess.run(["lsof", "-nP", "-a", "-p", str(pid), "-iTCP", "-sTCP:LISTEN"], capture_output=True, text=True).stdout
    return [line for line in out.splitlines()[1:] if line.strip()]


def _dispatch_body():
    dispatch = dict(SCOPE, protocol_version=2, task_id="dtask:net", execution_generation=1, source_event_seq=1,
                    cancellation_scope_id="c1", prompt="hi", prompt_digest="d", capability_digest="c",
                    execution_policy_digest="p", trace_id="t")
    return json.dumps({"input": "hi", "hosted_room_dispatch": dispatch}).encode()


class NetLoopbackE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp(prefix="rooms-net-"))
        cls.procs = []
        a = pki.init_owner(cls.root / "owner-a", "a")
        b = pki.init_owner(cls.root / "owner-b", "b")
        relay_ca = pki.init_owner(cls.root / "relay-ca", "relay")
        server = pki.issue_server(cls.root / "relay-ca", cls.root / "relay-tls", "IP:127.0.0.1")
        prov = relay_daemon.provision(cls.root / "routes.json", cls.root / "creds", link_id="link-net",
                                      room_id="room-net", source="install:host", target="install:target")
        cls.port = _free_port()
        cls.relay_args = [PY, str(HERE / "relay_daemon.py"), "serve", "--routes", str(cls.root / "routes.json"),
                          "--port", str(cls.port), "--tls-cert", server["cert_file"], "--tls-key", server["key_file"]]
        cls.relay = cls._spawn(cls.relay_args)
        _readline_json(cls.relay, "listening")
        cls.stub = StubNative()
        common = {"relay_url": f"wss://127.0.0.1:{cls.port}/data", "relay_ca_file": relay_ca["ca_file"],
                  "link_id": "link-net", "room_id": "room-net", "scope": SCOPE}
        target_cfg = dict(common, role="target", identity="install:target", peer_identity="install:host",
                          native_receipt_db=str(cls.root / 'native-fixture.db'),
                          credential_file=prov["credential_files"]["install:target"], target_base=cls.stub.base,
                          inner_tls={"cert_file": b["cert_file"], "key_file": b["key_file"],
                                     "peer_ca_file": a["ca_file"], "peer_fingerprint": a["fingerprint"]})
        host_cfg = dict(common, role="host", identity="install:host", peer_identity="install:target",
                        host_binding_db=str(cls.root / 'host-bindings.db'),
                        credential_file=prov["credential_files"]["install:host"],
                        inner_tls={"cert_file": a["cert_file"], "key_file": a["key_file"],
                                   "peer_ca_file": b["ca_file"], "peer_fingerprint": b["fingerprint"]})
        (cls.root / "target.json").write_text(json.dumps(target_cfg))
        (cls.root / "host.json").write_text(json.dumps(host_cfg))
        cls.target = cls._spawn([PY, str(HERE / "connector.py"), "target", "--config", str(cls.root / "target.json")])
        _readline_json(cls.target, "target_ready")
        cls.host = cls._spawn([PY, str(HERE / "connector.py"), "host", "--config", str(cls.root / "host.json")])
        cls.adapter_url = _readline_json(cls.host, "host_ready")["adapter_url"]

    @classmethod
    def _spawn(cls, args):
        env = {k: v for k, v in os.environ.items() if not k.lower().endswith("_proxy")}
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        cls.procs.append(proc)
        return proc

    @classmethod
    def tearDownClass(cls):
        for proc in cls.procs:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(10)
            proc.stdout.close()
            proc.stderr.close()
        cls.stub.close()
        shutil.rmtree(cls.root, ignore_errors=True)
        for proc in cls.procs:
            assert proc.poll() is not None, "owned subprocess was not reaped"
            assert proc.stdout.closed and proc.stderr.closed, "owned subprocess pipes were not closed"

    def test_1_capabilities_round_trip_and_grant_forwarded(self):
        status, body = _request(self.adapter_url + "/v1/room-members/capabilities",
                                headers={"Authorization": GRANT, "Accept": "application/json"})
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["installation_id"], "install:target")
        self.assertIn(("GET", "/v1/room-members/capabilities", GRANT), self.stub.seen)

    def test_2_dispatch_round_trip_keeps_idempotency_key(self):
        status, body = _request(self.adapter_url + "/v1/runs", "POST", _dispatch_body(),
                                {"Authorization": GRANT, "Content-Type": "application/json",
                                 "Idempotency-Key": "room:dtask:net:1"})
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["run_id"], "run_stub_1")
        posts = [s for s in self.stub.seen if s[0] == "POST"]
        self.assertEqual(posts[-1][3], "room:dtask:net:1")

    def test_3_unlisted_route_never_leaves_host(self):
        before = len(self.stub.seen)
        status, _ = _request(self.adapter_url + "/v1/sessions", headers={"Authorization": GRANT})
        self.assertEqual(status, 403)
        self.assertEqual(len(self.stub.seen), before)

    def test_4_outbound_only_listener_topology(self):
        self.assertEqual(_listeners(self.target.pid), [], "target connector must not listen at all")
        host_listeners = _listeners(self.host.pid)
        self.assertEqual(len(host_listeners), 1)
        self.assertIn("127.0.0.1:", host_listeners[0])

    def test_5_relay_restart_fails_closed_then_recovers_without_replay(self):
        posts_before = len([s for s in self.stub.seen if s[0] == "POST"])
        self.relay.terminate(); self.relay.wait(10)
        status, _ = _request(self.adapter_url + "/v1/room-members/capabilities", headers={"Authorization": GRANT})
        self.assertGreaterEqual(status, 500)
        relay = self._spawn(self.relay_args)
        _readline_json(relay, "listening")
        type(self).relay = relay
        deadline = time.monotonic() + 60
        status = None
        while time.monotonic() < deadline:
            status, _ = _request(self.adapter_url + "/v1/room-members/capabilities", headers={"Authorization": GRANT})
            if status == 200:
                break
            time.sleep(1)
        self.assertEqual(status, 200)
        self.assertEqual(len([s for s in self.stub.seen if s[0] == "POST"]), posts_before, "no replay after reconnect")


class RelaySeesCiphertextOnly(unittest.TestCase):
    """In-process relay core spy: payload bytes routed never contain plaintext."""

    def test_native_gate_transport_releases_relay_loop(self):
        from gate_f1_local import NetworkTransport
        from room_firewall import RoomFirewall
        transport = NetworkTransport()
        stub = StubNative()
        try:
            transport.start()
            transport.native_receipt_db = transport.root / 'native-fixture.db'
            transport.bind("cleanup", RoomFirewall(expected_scope=SCOPE, backend=lambda _: None,
                                                   verify_native=lambda *_: True),
                           target_url=stub.base, scope=SCOPE)
        finally:
            transport.close()
            stub.close()
        self.assertTrue(transport.loop.is_closed(), "native gate relay loop was not closed")
        self.assertFalse(any(c._thread.is_alive() for c in transport.clients))
        from gate_f1_local import EVIDENCE
        self.assertTrue(EVIDENCE["thread_inventory_complete"])
        self.assertEqual(EVIDENCE["application_threads_alive"], [])

    def test_relay_payloads_do_not_contain_plaintext(self):
        import relay as relay_mod
        import connector

        observed = []
        original = relay_mod.Relay.route

        async def spy(self, connection, frame):
            if frame.get("type") == "DATA":
                observed.append(base64.b64decode(frame["payload"]))
            return await original(self, connection, frame)

        root = Path(tempfile.mkdtemp(prefix="rooms-spy-"))
        stub = StubNative()
        loop = asyncio.new_event_loop()
        try:
            a = pki.init_owner(root / "a", "a")
            b = pki.init_owner(root / "b", "b")
            prov = relay_daemon.provision(root / "routes.json", root / "creds", link_id="l", room_id="room-net",
                                          source="install:host", target="install:target")
            relay_mod.Relay.route = spy
            daemon = relay_daemon.RelayDaemon(root / "routes.json", host="127.0.0.1", port=_free_port(), tls=None)
            ready = threading.Event()

            def run_loop():
                asyncio.set_event_loop(loop)
                loop.run_until_complete(daemon.run(ready.set, install_signals=False))

            relay_thread = threading.Thread(target=run_loop, daemon=True)
            relay_thread.start()
            self.assertTrue(ready.wait(10))
            url = f"ws://127.0.0.1:{daemon.port}/data"
            target_cfg = {"relay_url": url, "identity": "install:target", "peer_identity": "install:host",
                          "native_receipt_db": str(root / 'native-fixture.db'),
                          "link_id": "l", "room_id": "room-net", "scope": SCOPE, "target_base": stub.base,
                          "credential_file": prov["credential_files"]["install:target"],
                          "inner_tls": {"cert_file": b["cert_file"], "key_file": b["key_file"],
                                        "peer_ca_file": a["ca_file"], "peer_fingerprint": a["fingerprint"]}}
            host_cfg = dict(target_cfg, identity="install:host", peer_identity="install:target",
                            host_binding_db=str(root / 'host-bindings.db'),
                            credential_file=prov["credential_files"]["install:host"],
                            inner_tls={"cert_file": a["cert_file"], "key_file": a["key_file"],
                                       "peer_ca_file": b["ca_file"], "peer_fingerprint": b["fingerprint"]})
            target_client, _ = connector.run_target(target_cfg)
            host_client, adapter, adapter_url = connector.run_host(host_cfg)
            try:
                status, body = _request(adapter_url + "/v1/runs", "POST", _dispatch_body(),
                                        {"Authorization": GRANT, "Content-Type": "application/json",
                                         "Idempotency-Key": "room:dtask:net:1"})
                self.assertEqual(status, 200, body)
                self.assertIn(SENTINEL.encode(), body)
                blob = b"".join(observed)
                self.assertGreater(len(observed), 4)
                for marker in (SENTINEL.encode(), b"grant-for-test", b"hosted_room_dispatch", b"run_stub_1"):
                    self.assertNotIn(marker, blob)
            finally:
                host_client.stop(); target_client.stop(); adapter.close()
        finally:
            relay_mod.Relay.route = original
            loop.call_soon_threadsafe(daemon.stop_event.set)
            relay_thread.join(10)
            self.assertFalse(relay_thread.is_alive(), "spy daemon thread did not stop")
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()
            stub.close()
            shutil.rmtree(root, ignore_errors=True)
            self.assertTrue(loop.is_closed(), "spy loop ownership not released")


if __name__ == "__main__":
    unittest.main(verbosity=2)
