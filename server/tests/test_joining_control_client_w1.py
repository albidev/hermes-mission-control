"""P3b-W1: joining-side loopback control client, in-memory peer CA, explicit owner endpoint.

Real scratch ``runtime_config.load_service()``, real pairing journal + identity bundles (``pairing_support``)
and a TEST-ONLY real loopback mTLS server.  The production peer listener is W2: nothing here claims a
production end-to-end control exchange.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pairing_support as ps
from invite_rooms import identity as identity_module
from invite_rooms import runtime_config
from invite_rooms import runtime_joining as rj
from invite_rooms.control import connector as connector_module
from invite_rooms.control import tls as tls_module
from invite_rooms.control.connector import decode_request, encode_response, recv_message, send_message
from invite_rooms.control.firewall import UpstreamResponse
from invite_rooms.control.framing import SequencedTLS
from invite_rooms.joining import JoiningError

LOBBY = "lobby-" + "1" * 32
INVITE = "invite-" + "2" * 32
PEER = "peer-" + "a" * 32
STATUS = {"op": "status", "invite_id": INVITE}
REPLY = {"status": "ready", "scope": None}


def setUpModule():
    global TEMPLATES
    TEMPLATES = {i: ps.identity_template(i) for i in (ps.INSTALL_A, ps.INSTALL_B, ps.INSTALL_C)}


def tearDownModule():
    for p in TEMPLATES.values():
        shutil.rmtree(p.parent)


def private_json(path: Path, value) -> None:
    ps.private_dir(path.parent)
    path.write_text(json.dumps(value))
    path.chmod(0o600)


class LoopbackMtlsServer:
    """TEST-ONLY real TLS 1.3 mTLS listener: counts handshakes and application messages."""

    def __init__(self, *, cert_file, key_file, client_ca_file, client_fingerprint, host="127.0.0.1"):
        self.context = tls_module.server_context(ca_file=client_ca_file, cert_file=cert_file, key_file=key_file)
        self.client_fingerprint = client_fingerprint
        self.listener = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET)
        self.listener.bind((host, 0))
        self.listener.listen(8)
        self.listener.settimeout(0.05)
        self.port = self.listener.getsockname()[1]
        self.host = host
        self.accepted = 0
        self.handshakes = 0
        self.messages = []
        self.errors = []
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def _loop(self):
        while not self.stop.is_set():
            try:
                sock, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            self.accepted += 1
            sock.settimeout(2)
            try:
                with tls_module.wrap_server(sock, self.context, expected_fingerprint=self.client_fingerprint) as stream:
                    self.handshakes += 1
                    frames = SequencedTLS(stream, deadline=time.monotonic() + 2)
                    envelope = json.loads(recv_message(frames))
                    request = decode_request(json.dumps(envelope["request"]).encode())
                    self.messages.append((envelope, request))
                    send_message(frames, encode_response(UpstreamResponse(
                        200, (("content-type", "application/json"),), json.dumps(REPLY).encode())))
            except Exception as exc:  # handshake refusal / client abort: recorded, never raised
                self.errors.append(type(exc).__name__)
                try:
                    sock.close()
                except OSError:
                    pass

    def close(self):
        self.stop.set()
        self.listener.close()
        self.thread.join(2)


class W1Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.tmp.chmod(0o700)
        self.addCleanup(shutil.rmtree, self.tmp)
        # B = joining side (under test), A = host; the journal on B holds A's CA PEM + leaf pin.
        self.A = ps.Side(self.tmp, "a", ps.INSTALL_A, template=TEMPLATES[ps.INSTALL_A])
        self.B = ps.Side(self.tmp, "b", ps.INSTALL_B, template=TEMPLATES[ps.INSTALL_B])
        self.a, self.b = self.A.service(), self.B.service()
        self.paired = ps.drive_pairing(self.a, self.b)
        d = ps.dispatcher_for(self.b)
        try:
            self.a.send_pair_ack(self.paired["a_id"], ps.link_sender(d))
        finally:
            asyncio.run(d.close())
        self.bundle_a = identity_module.load_identity(self.A.identity_dir, installation_id=ps.INSTALL_A, profile="default")
        self.bundle_b = identity_module.load_identity(self.B.identity_dir, installation_id=ps.INSTALL_B, profile="default")
        self.cfg_path = self.B.root / "cfg" / "runtime.json"
        self.cfg = {"store_path": str(self.B.root / "invites" / "host.sqlite"), "installation_id": ps.INSTALL_B,
                    "profile": "default", "peer_registry_path": str(self.B.peers_path),
                    "pairing": {"identity_dir": str(self.B.identity_dir), "journal_dir": str(self.B.journal_dir),
                                "peers_path": str(self.B.peers_path)},
                    "joining": {"store_dir": str(self.B.root / "joining"), "identity_dir": str(self.B.identity_dir),
                                "hosts_path": str(self.B.root / "cfg" / "hosts.json"),
                                "origins": {PEER: "https://host.example"}, "host_label": "host", "own_bot_id": "self"}}
        env = patch.dict(os.environ, {"MISSION_CONTROL_ROOM_INVITES_CONFIG": str(self.cfg_path)})
        env.start()
        self.addCleanup(env.stop)
        self.servers = []
        rj.configure_joining_control(rj.default_joining_control)
        self.addCleanup(rj.configure_joining_control)
        self.addCleanup(rj.configure_joining_transport)

    def server(self, **kw):
        args = dict(cert_file=str(self.bundle_a.leaf_cert_file), key_file=str(self.bundle_a.leaf_key_file),
                    client_ca_file=str(self.bundle_b.ca_file), client_fingerprint=self.bundle_b.leaf_fingerprint)
        args.update(kw)
        srv = LoopbackMtlsServer(**args)
        self.servers.append(srv)
        self.addCleanup(srv.close)
        return srv

    def endpoints(self, host="127.0.0.1", port=1):
        self.cfg["joining"]["control_endpoints"] = {PEER: {"host": host, "port": port}}

    def owner(self):
        private_json(self.cfg_path, self.cfg)
        loaded = runtime_config.load_service()
        self.assertIsNotNone(loaded, "owner config must load")
        owner = rj.load_owner(loaded[2])
        self.assertIsNotNone(owner, "joining owner must build")
        self.addCleanup(owner.close)
        return owner

    def control(self, owner):
        return owner.control(dict(STATUS), lobby_id=LOBBY)


class OwnerEndpointClientTest(W1Base):
    def test_accepted_peer_uses_explicit_endpoint_and_pinned_mtls(self):
        srv = self.server()
        self.endpoints(port=srv.port)
        owner = self.owner()
        self.assertEqual(self.control(owner), REPLY)
        self.assertEqual((srv.accepted, srv.handshakes, len(srv.messages)), (1, 1, 1))
        envelope, request = srv.messages[0]
        self.assertEqual(envelope["endpoint_id"], owner.endpoint_id)
        self.assertEqual(envelope["generation"], owner.generation)
        self.assertEqual((request.method, request.target), ("POST", rj.CONTROL_PATH))
        self.assertEqual(owner.origin, "https://host.example")   # joining.origins semantics unchanged

    def test_ipv6_loopback_literal_is_accepted(self):
        try:
            srv = self.server(host="::1")
        except OSError:
            self.skipTest("no IPv6 loopback")
        self.endpoints(host="::1", port=srv.port)
        self.assertEqual(self.control(self.owner()), REPLY)

    def test_wrong_ca_is_refused_before_any_application_frame(self):
        # Server presents a certificate from a DIFFERENT authority than the CA in the journal.
        other = ps.Side(self.tmp, "c", ps.INSTALL_C, template=TEMPLATES[ps.INSTALL_C])
        bundle_c = identity_module.load_identity(other.identity_dir, installation_id=ps.INSTALL_C, profile="default")
        srv = self.server(cert_file=str(bundle_c.leaf_cert_file), key_file=str(bundle_c.leaf_key_file))
        self.endpoints(port=srv.port)
        with self.assertRaises(JoiningError):
            self.control(self.owner())
        self.assertEqual(srv.messages, [])

    def test_exact_leaf_pin_is_required(self):
        # Fingerprint pin enforced at the product TLS layer even when the chain verifies.
        srv = self.server()
        context = tls_module.client_context(ca_data=self.bundle_a.ca_pem, cert_file=str(self.bundle_b.leaf_cert_file),
                                            key_file=str(self.bundle_b.leaf_key_file))
        sock = socket.create_connection(("127.0.0.1", srv.port), timeout=2)
        with self.assertRaises(Exception):
            tls_module.wrap_client(sock, context, expected_fingerprint="0" * 64)
        sock.close()
        sock = socket.create_connection(("127.0.0.1", srv.port), timeout=2)
        stream = tls_module.wrap_client(sock, context, expected_fingerprint=self.bundle_a.leaf_fingerprint)
        stream.close()

    def test_revoked_link_is_refused_even_with_stale_owner_and_projection(self):
        srv = self.server()
        self.endpoints(port=srv.port)
        owner = self.owner()                       # projection (hosts.json) + link snapshot taken while active
        self.assertEqual(self.control(owner), REPLY)
        self.b.unpair(PEER)                        # durable journal revocation; projection files stay stale
        before = (srv.accepted, len(srv.messages))
        with self.assertRaises(JoiningError):
            self.control(owner)
        self.assertEqual((srv.accepted, len(srv.messages)), before)

    def test_new_generation_does_not_reuse_old_owner_link(self):
        srv = self.server()
        self.endpoints(port=srv.port)
        owner = self.owner()
        self.b.unpair(PEER)
        link = dict(owner.link)
        link["generation"] = owner.generation + 1       # a forged/stale-generation caller view
        channel = rj.default_joining_control(owner.config, link)
        with self.assertRaises(Exception):
            asyncio.run(channel.call(dict(STATUS, lobby_id=LOBBY), deadline=time.monotonic() + 3, admit=owner.admit))
        self.assertEqual(srv.messages, [])

    def test_final_journal_admission_after_connect_before_first_frame(self):
        srv = self.server()
        self.endpoints(port=srv.port)
        owner = self.owner()
        real_wrap = connector_module.wrap_client

        def wrap_then_revoke(*args, **kw):
            stream = real_wrap(*args, **kw)
            self.b.unpair(PEER)            # revoked after the pinned handshake, before the first application frame
            return stream

        with patch.object(connector_module, "wrap_client", wrap_then_revoke):
            with self.assertRaises(JoiningError):
                self.control(owner)
        self.assertEqual(srv.handshakes, 1)
        self.assertEqual(srv.messages, [])


class EndpointConfigTest(W1Base):
    def load(self, endpoints):
        self.cfg["joining"]["control_endpoints"] = endpoints
        private_json(self.cfg_path, self.cfg)
        return runtime_config.load_service()

    def test_absent_endpoint_fails_closed_without_network(self):
        srv = self.server()
        owner = self.owner()                        # no control_endpoints at all
        with self.assertRaises(JoiningError):
            self.control(owner)
        self.assertEqual(srv.accepted, 0)

    def test_endpoint_for_another_peer_is_not_used(self):
        srv = self.server()
        self.cfg["joining"]["control_endpoints"] = {"peer-" + "9" * 32: {"host": "127.0.0.1", "port": srv.port}}
        owner = self.owner()
        with self.assertRaises(JoiningError):
            self.control(owner)
        self.assertEqual(srv.accepted, 0)

    def test_invalid_endpoint_shapes_disable_the_feature(self):
        bad = [
            {PEER: {"host": "localhost", "port": 9}},
            {PEER: {"host": "10.0.0.5", "port": 9}},
            {PEER: {"host": "0.0.0.0", "port": 9}},
            {PEER: {"host": "::ffff:127.0.0.1", "port": 9}},
            {PEER: {"host": "127.0.0.1", "port": True}},
            {PEER: {"host": "127.0.0.1", "port": "9"}},
            {PEER: {"host": "127.0.0.1", "port": 0}},
            {PEER: {"host": "127.0.0.1", "port": 65536}},
            {PEER: {"host": "127.0.0.1", "port": 9.0}},
            {PEER: {"host": "127.0.0.1"}},
            {PEER: {"port": 9}},
            {PEER: {"host": "127.0.0.1", "port": 9, "path": "/x"}},
            {PEER: "127.0.0.1:9"},
            {PEER: ["127.0.0.1", 9]},
            {1: {"host": "127.0.0.1", "port": 9}},
            [],
            "x",
            {},
        ]
        for value in bad:
            with self.subTest(value=value):
                if isinstance(value, dict) and 1 in value:
                    continue   # JSON object keys are always strings
                self.assertIsNone(self.load(value))

    def test_duplicate_peer_entry_in_file_is_refused(self):
        self.endpoints(port=9)
        private_json(self.cfg_path, self.cfg)
        text = self.cfg_path.read_text().replace(
            '"control_endpoints": {"%s": {"host": "127.0.0.1", "port": 9}}' % PEER,
            '"control_endpoints": {"%s": {"host": "127.0.0.1", "port": 9}, "%s": {"host": "127.0.0.1", "port": 10}}' % (PEER, PEER))
        self.assertIn('"port": 10', text)
        self.cfg_path.write_text(text)
        self.assertIsNone(runtime_config.load_service())

    def test_valid_shape_loads_and_origins_are_untouched(self):
        loaded = self.load({PEER: {"host": "127.0.0.1", "port": 4321}})
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded[2].joining_owner["origins"], {PEER: "https://host.example"})

    def test_feature_off_has_no_control_and_no_effect(self):
        srv = self.server()
        self.endpoints(port=srv.port)
        private_json(self.cfg_path, self.cfg)
        with patch.dict(os.environ):
            os.environ.pop("MISSION_CONTROL_ROOM_INVITES_CONFIG")
            self.assertIsNone(runtime_config.load_service())
        self.assertEqual(srv.accepted, 0)


class NoTrustMaterializationTest(W1Base):
    def test_ca_pem_is_used_in_memory_only(self):
        srv = self.server()
        self.endpoints(port=srv.port)
        owner = self.owner()
        tempdir = Path(tempfile.gettempdir())

        def listing():
            out = {}
            for base, _dirs, files in os.walk(self.tmp):       # every owner-private tree: byte-for-byte
                for f in files:
                    p = Path(base) / f
                    out[str(p)] = (p.stat().st_mtime_ns, p.stat().st_size)
            try:                                               # shared temp dir: only trust-anchor-looking entries
                for p in tempdir.iterdir():
                    if p.suffix.lower() in (".pem", ".crt", ".cer", ".key") or "ca" in p.name.lower().split("."):
                        out[str(p)] = None
            except OSError:
                pass
            return out

        before = listing()
        calls = []
        real = ssl.create_default_context

        def spy(purpose=ssl.Purpose.SERVER_AUTH, *, cafile=None, capath=None, cadata=None):
            calls.append((purpose, cafile, cadata))
            return real(purpose, cafile=cafile, capath=capath, cadata=cadata)

        with patch.object(ssl, "create_default_context", spy):
            self.assertEqual(self.control(owner), REPLY)
        client = [c for c in calls if c[0] == ssl.Purpose.SERVER_AUTH]
        self.assertEqual(len(client), 1)
        self.assertIsNone(client[0][1])                                         # no cafile
        self.assertEqual(client[0][2], self.bundle_a.ca_pem)                    # journal CA, as memory data
        self.assertEqual(listing(), before)                                     # no file created / touched
    def test_ca_file_and_ca_data_are_mutually_exclusive(self):
        kw = dict(cert_file=str(self.bundle_b.leaf_cert_file), key_file=str(self.bundle_b.leaf_key_file))
        with self.assertRaises(ValueError):
            tls_module.client_context(**kw)
        with self.assertRaises(ValueError):
            tls_module.client_context(ca_file=str(self.bundle_a.ca_file), ca_data=self.bundle_a.ca_pem, **kw)
        tls_module.client_context(ca_file=str(self.bundle_a.ca_file), **kw)
        tls_module.client_context(ca_data=self.bundle_a.ca_pem, **kw)


class WiringLifecycleTest(W1Base):
    def test_two_configs_resolve_two_endpoints_without_leaking(self):
        one, two = self.server(), self.server()
        self.endpoints(port=one.port)
        owner_one = self.owner()
        self.endpoints(port=two.port)
        owner_two = self.owner()
        self.assertEqual(self.control(owner_one), REPLY)
        self.assertEqual(self.control(owner_two), REPLY)
        self.assertEqual((len(one.messages), len(two.messages)), (1, 1))

    def test_install_and_clear_are_process_scoped_and_idempotent(self):
        rj.clear_joining_wiring()
        self.assertIsNone(rj._CONTROL_FACTORY)
        rj.install_joining_wiring()
        rj.install_joining_wiring()
        self.assertIs(rj._CONTROL_FACTORY, rj.default_joining_control)
        rj.clear_joining_wiring()
        self.assertIsNone(rj._CONTROL_FACTORY)
        self.assertIsNone(rj._TRANSPORT_FACTORY)

    def test_sidecar_main_installs_during_serve_and_clears_after(self):
        import local_telemetry_server as lts
        seen = {}

        class FakeServer:
            def __init__(self, *a, **k):
                pass

            def serve_forever(self):
                seen["during"] = rj._CONTROL_FACTORY

            def shutdown(self):
                pass

        terminal = type("T", (), {"stop": lambda s: None, "join": lambda s, timeout=None: None})()
        rj.clear_joining_wiring()
        with patch.object(lts, "TelemetryHTTPServer", FakeServer), patch.object(lts, "_cpu_sampler", lambda: None), \
                patch.object(lts, "start_gateway_watcher", lambda: None), \
                patch.object(lts, "start_terminal_server", lambda: terminal), \
                patch.object(lts, "shutdown_terminal_sessions", lambda: None), \
                patch.object(lts.signal, "signal", lambda *a, **k: None), \
                patch.object(lts, "_resolve_telemetry_bind", lambda: ("127.0.0.1", 0)):
            lts.main()
        self.assertIs(seen["during"], rj.default_joining_control)
        self.assertIsNone(rj._CONTROL_FACTORY)


HOLDER = (
    "import fcntl, os, sys\n"
    "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)\n"
    "fcntl.flock(fd, fcntl.LOCK_EX)\n"
    "sys.stdout.write('ready\\n'); sys.stdout.flush()\n"
    "sys.stdin.readline()\n"
)


class NonblockingFactoryReadTest(W1Base):
    """R1: the connector factory runs on the event-loop thread; it must never wait for the journal lock."""

    def hold_lock(self):
        proc = subprocess.Popen([sys.executable, "-B", "-c", HOLDER, str(self.B.journal_dir / ".pairing.lock")],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.release_done = False

        def release():
            if not self.release_done:
                self.release_done = True
                try:
                    proc.stdin.close()
                except OSError:
                    pass
                proc.wait(10)
                proc.stdout.close()

        self.addCleanup(release)
        self.assertEqual(proc.stdout.readline().strip(), "ready")      # lock held BEFORE the call starts
        return release

    def run_call(self, owner, *, budget=0.25, neutral_admit=False):
        sockets, admits, ticks = [], [], []
        real_connect = socket.create_connection

        def spy_connect(*a, **k):
            sockets.append(a)
            return real_connect(*a, **k)

        def admit(*a, **k):
            admits.append(a)
            return True if neutral_admit else owner.admit(*a, **k)

        async def main():
            loop = asyncio.get_running_loop()
            start = time.monotonic()
            loop.call_later(0.01, lambda: ticks.append(time.monotonic() - start))
            channel = rj.default_joining_control(owner.config, dict(owner.link))
            outcome = "ok"
            try:
                await channel.call(dict(STATUS, lobby_id=LOBBY), deadline=start + budget, admit=admit)
            except Exception as exc:
                outcome = type(exc).__name__ + ":" + str(exc)
            elapsed = time.monotonic() - start
            if not ticks:
                await asyncio.sleep(0.02)
            return outcome, elapsed

        with patch.object(socket, "create_connection", spy_connect):
            outcome, elapsed = asyncio.run(main())
        return outcome, elapsed, ticks[0], sockets, admits

    def test_busy_journal_refuses_promptly_without_blocking_the_event_loop(self):
        srv = self.server()
        self.endpoints(port=srv.port)
        owner = self.owner()
        release = self.hold_lock()
        outcome, elapsed, tick, sockets, admits = self.run_call(owner)
        release()
        print("R1-TIMING outcome=%s elapsed_ms=%.3f timer_ms=%.3f" % (outcome, elapsed * 1000, tick * 1000),
              file=sys.stderr)
        self.assertTrue(outcome.startswith("ControlError"), outcome)
        self.assertLess(elapsed, 0.05)
        self.assertLess(tick, 0.05)
        self.assertEqual((sockets, admits, srv.accepted, srv.messages), ([], [], 0, []))
        # After release the very same owner reaches the pinned in-memory TLS path and the admitted effect.
        outcome, _elapsed, _tick, sockets, admits = self.run_call(owner, budget=3)
        self.assertEqual(outcome, "ok")
        self.assertEqual(len(sockets), 1)
        self.assertTrue(admits)
        self.assertEqual((srv.handshakes, len(srv.messages)), (1, 1))

    def test_construction_is_exactly_one_nonblocking_read_and_never_snapshot(self):
        srv = self.server()
        self.endpoints(port=srv.port)
        owner = self.owner()
        reads, snapshots = [], []
        real_locked, real_snapshot = rj.Journal._locked, rj.Journal.snapshot

        def locked(journal, *a, **k):
            reads.append(dict(k))
            return real_locked(journal, *a, **k)

        def snapshot(journal):
            snapshots.append(1)
            return real_snapshot(journal)

        factory = rj._config_connector_factory(owner.config)
        trust_calls = []
        real_trust = rj.JournalLinkAuthority.peer_trust

        def trust(authority, *a, **k):
            trust_calls.append(a)
            return real_trust(authority, *a, **k)

        with patch.object(rj.Journal, "_locked", locked), patch.object(rj.Journal, "snapshot", snapshot), \
                patch.object(rj.JournalLinkAuthority, "peer_trust", trust):
            factory(dict(owner.link), 1.0)
        self.assertEqual(snapshots, [])
        self.assertEqual(len(trust_calls), 1)       # ONE authoritative trust read, no second independent one
        # Total journal reads: that one + the accepted, equally non-waiting ``make_link_connector`` current() preflight.
        self.assertEqual(len(reads), 2)
        self.assertTrue(all((r.get("exclusive"), r.get("nonblocking")) == (False, True) for r in reads))
        reads.clear()
        with patch.object(rj.Journal, "_locked", locked), patch.object(rj.Journal, "snapshot", snapshot):
            outcome = self.run_call(owner, budget=3, neutral_admit=True)[0]     # real default_joining_control -> call
        self.assertEqual(outcome, "ok")
        self.assertEqual(snapshots, [])
        self.assertTrue(reads and all(r.get("nonblocking") is True and r.get("exclusive") is False for r in reads))

    def test_journal_lock_is_released_before_identity_and_network_io(self):
        import fcntl
        srv = self.server()
        self.endpoints(port=srv.port)
        owner = self.owner()
        seen = []
        real_load = rj.identity_module.load_identity

        def load(*a, **k):
            probe = os.open(self.B.journal_dir / ".pairing.lock", os.O_RDWR)
            try:
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)      # would fail while the read lock is held
                seen.append("free")
            except OSError:
                seen.append("held")
            finally:
                os.close(probe)
            return real_load(*a, **k)

        with patch.object(rj.identity_module, "load_identity", load):
            self.assertEqual(self.control(owner), REPLY)
        self.assertEqual(seen, ["free"])

    def test_peer_trust_helper_refuses_every_non_matching_link(self):
        self.endpoints(port=1)
        owner = self.owner()
        authority = rj.JournalLinkAuthority(rj.Journal(owner.config.pairing.journal_dir,
                                                       anchor=owner.config.pairing.journal_anchor))
        good = authority.peer_trust(owner.endpoint_id, owner.generation, role="joining")
        self.assertIsNotNone(good)
        self.assertEqual(good.ca_pem, self.bundle_a.ca_pem)
        self.assertEqual(good.peer_id, PEER)
        for args in ((owner.endpoint_id, owner.generation + 1), ("endpoint-unknown", owner.generation), (1, 1),
                     (owner.endpoint_id, True)):
            self.assertIsNone(authority.peer_trust(*args, role="joining"))
        self.assertIsNone(authority.peer_trust(owner.endpoint_id, owner.generation, role="host"))
        self.b.unpair(PEER)
        self.assertIsNone(authority.peer_trust(owner.endpoint_id, owner.generation, role="joining"))


class ExactLoopbackLiteralTest(W1Base):
    """R2: only the literals 127.0.0.1 and ::1 are accepted."""
    REJECTED = ("127.0.0.2", "127.1.2.3", "127.255.255.254", "::1%lo0", "::1%1", "localhost", "::ffff:127.0.0.1",
                "::ffff:7f00:1", "0:0:0:0:0:0:0:1", "0000:0000:0000:0000:0000:0000:0000:0001", "[::1]", "127.0.0.1 ",
                " 127.0.0.1", "127.000.000.001", "127.1", "2130706433", "10.0.0.5", "0.0.0.0", "::", "8.8.8.8", "")

    def test_parser_accepts_exactly_the_two_literals(self):
        for host in ("127.0.0.1", "::1"):
            self.assertEqual(runtime_config.parse_control_endpoints({PEER: {"host": host, "port": 9}}),
                             {PEER: (host, 9)})
        for host in self.REJECTED:
            with self.subTest(host=host), self.assertRaises(runtime_config.RuntimeConfigError):
                runtime_config.parse_control_endpoints({PEER: {"host": host, "port": 9}})

    def test_real_load_service_follows_accepted_and_feature_off_behaviour(self):
        for host in ("127.0.0.1", "::1"):
            self.endpoints(host=host, port=4321)
            private_json(self.cfg_path, self.cfg)
            self.assertIsNotNone(runtime_config.load_service(), host)
        for host in self.REJECTED:
            with self.subTest(host=host):
                self.endpoints(host=host, port=4321)
                private_json(self.cfg_path, self.cfg)
                self.assertIsNone(runtime_config.load_service())

    def test_invalid_endpoint_disables_feature_but_sidecar_main_keeps_serving(self):
        import local_telemetry_server as lts
        self.endpoints(host="127.0.0.2", port=4321)
        private_json(self.cfg_path, self.cfg)
        seen = {}

        class FakeServer:
            def __init__(self, *a, **k):
                pass

            def serve_forever(self):
                seen["served"] = True

            def shutdown(self):
                pass

        terminal = type("T", (), {"stop": lambda s: None, "join": lambda s, timeout=None: None})()
        with patch.object(lts, "TelemetryHTTPServer", FakeServer), patch.object(lts, "_cpu_sampler", lambda: None), \
                patch.object(lts, "start_gateway_watcher", lambda: None), \
                patch.object(lts, "start_terminal_server", lambda: terminal), \
                patch.object(lts, "shutdown_terminal_sessions", lambda: None), \
                patch.object(lts.signal, "signal", lambda *a, **k: None), \
                patch.object(lts, "_resolve_telemetry_bind", lambda: ("127.0.0.1", 0)):
            self.assertIsNone(runtime_config.load_service())
            lts.main()
        self.assertTrue(seen.get("served"))


if __name__ == "__main__":
    unittest.main()
