"""P3b-W2a: loopback control listener substrate (journal-driven TLS identity multiplex) and real ``pair_ack``.

Real scratch pairing journals + identity bundles (``pairing_support``) and real loopback TLS 1.3 sockets.  This is a
TRANSPORT PRIMITIVE test: nothing here (or in product code) starts the listener from the sidecar ``main()``.
"""
from __future__ import annotations

import asyncio
import base64
import datetime as dt
import gc
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
import warnings
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pairing_support as ps
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID
from invite_rooms import identity as identity_module
from invite_rooms import runtime_config
from invite_rooms import runtime_joining as rj
from invite_rooms.control import listener as listener_module
from invite_rooms.control import tls as tls_module
from invite_rooms.control.connector import decode_response, recv_message, send_message
from invite_rooms.control.framing import SequencedTLS
from invite_rooms.control.listener import ControlListener, ListenerError, build_pair_ack_listener
from invite_rooms.joining import JoiningError
from invite_rooms.pairing_journal import JournalLinkAuthority, PeerTrust

SERVER_DIR = Path(__file__).resolve().parents[1]
PEER_A = "peer-" + "a" * 32
PEER_B = "peer-" + "b" * 32
LOBBY = "lobby-" + "1" * 32
INVITE = "invite-" + "2" * 32


def setUpModule():
    global TEMPLATES
    TEMPLATES = {i: ps.identity_template(i) for i in (ps.INSTALL_A, ps.INSTALL_B, ps.INSTALL_C)}


def tearDownModule():
    for p in TEMPLATES.values():
        shutil.rmtree(p.parent)


def free_port(host="127.0.0.1") -> int:
    s = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET)
    s.bind((host, 0))
    port = s.getsockname()[1]
    s.close()
    return port


def listener_threads():
    return [t.name for t in threading.enumerate() if t.name.startswith("room-control") or t.name.startswith("invite-control")]


def private_json(path: Path, value) -> None:
    ps.private_dir(path.parent)
    path.write_text(json.dumps(value))
    path.chmod(0o600)


def wire(port, *, client_bundle=None, cert_file=None, key_file=None, server_bundle, request, endpoint_id, generation,
         host="127.0.0.1", envelope=None):
    """One request over a REAL pinned mTLS connection.  Returns ``("ok", status, body)`` or ``("err", ExcName)``."""
    cert_file = cert_file or str(client_bundle.leaf_cert_file)
    key_file = key_file or str(client_bundle.leaf_key_file)
    ctx = tls_module.client_context(ca_data=server_bundle.ca_pem, cert_file=cert_file, key_file=key_file)
    payload = envelope if envelope is not None else {
        "endpoint_id": endpoint_id, "generation": generation,
        "request": {"method": "POST", "path": "/control/joining", "headers": [],
                    "body": base64.b64encode(json.dumps(request, sort_keys=True, separators=(",", ":")).encode()).decode()}}
    try:
        sock = socket.create_connection((host, port), timeout=3)
        tls = tls_module.wrap_client(sock, ctx, expected_fingerprint=server_bundle.leaf_fingerprint)
        try:
            frames = SequencedTLS(tls, deadline=time.monotonic() + 4)
            send_message(frames, json.dumps(payload, separators=(",", ":")).encode())
            reply = decode_response(recv_message(frames))
            return ("ok", reply.status, reply.body)
        finally:
            tls.close()
    except Exception as exc:
        return ("err", type(exc).__name__)


class W2aBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.tmp.chmod(0o700)
        self.addCleanup(shutil.rmtree, self.tmp)
        self.clock = ps.Clock(time.time())
        self.A = ps.Side(self.tmp, "a", ps.INSTALL_A, template=TEMPLATES[ps.INSTALL_A])
        self.B = ps.Side(self.tmp, "b", ps.INSTALL_B, template=TEMPLATES[ps.INSTALL_B])
        self.a, self.b = self.A.service(self.clock), self.B.service(self.clock)
        self.paired = ps.drive_pairing(self.a, self.b)     # both sides ``confirmed``
        self.ba = identity_module.load_identity(self.A.identity_dir, installation_id=ps.INSTALL_A, profile="default")
        self.bb = identity_module.load_identity(self.B.identity_dir, installation_id=ps.INSTALL_B, profile="default")
        self.listeners = []
        self.addCleanup(self._close_all)

    def _close_all(self):
        for item in self.listeners:
            try:
                item.close()
            except Exception:
                pass

    def handler_spy(self, service):
        calls = []

        def handle(request):
            calls.append(request)
            return service.handle_pair_ack(request)
        return calls, handle

    def listen(self, side, bundle, handler, port=None, authority=None, cls=ControlListener, **kw):
        port = port or free_port()
        item = cls(host="127.0.0.1", port=port,
                   authority=authority or JournalLinkAuthority(side.service(self.clock).journal),
                   identity=lambda: (str(bundle.leaf_cert_file), str(bundle.leaf_key_file)),
                   pair_ack_handler=handler, timeout=kw.pop("timeout", 3.0), **kw)
        self.listeners.append(item)
        item.start()
        return item

    def link_b(self):
        return next(iter(self.b.journal.snapshot().links.values()))

    def b_state(self):
        return self.b.journal.snapshot().sessions[self.paired["b_id"]]["state"]

    def ack_on_b(self, item, req=None, **kw):
        req = req or self.a.ack_request(self.paired["a_id"])
        return req, wire(item.address[1], client_bundle=self.ba, server_bundle=self.bb, request=req.request,
                         endpoint_id=req.endpoint_id, generation=req.generation, **kw)


class PairAckOverRealTlsTest(W2aBase):
    def test_round_trip_acknowledges_both_sides_with_exact_protocol_reply(self):
        calls, handle = self.handler_spy(self.b)
        before = sorted(p.name for p in self.tmp.rglob("*") if p.is_file())
        with patch.object(listener_module, "server_context", wraps=tls_module.server_context) as spy:
            item = self.listen(self.B, self.bb, handle)
            req, result = self.ack_on_b(item)
            self.assertEqual(result[0:2], ("ok", 200), result)
            reply = json.loads(result[2])
            self.assertEqual(spy.call_args.kwargs["ca_file"] if "ca_file" in spy.call_args.kwargs else None, None)
            self.assertIn("BEGIN CERTIFICATE", spy.call_args.kwargs["ca_data"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.b_state(), "acknowledged")
        row = self.a.complete_ack(self.paired["a_id"], reply)      # exact reply expected by send_pair_ack
        self.assertEqual(row["state"], "acknowledged")
        # A's link and B's link both durable-acknowledged, same generation.
        self.assertEqual(self.link_b()["protocol_state"], "acknowledged")
        # idempotent retry returns the byte-identical reply; a variant is refused.
        _req, again = self.ack_on_b(item, req)
        self.assertEqual(json.loads(again[2]), reply)
        self.assertEqual(len(calls), 2)
        after = sorted(p.name for p in self.tmp.rglob("*") if p.is_file())
        self.assertEqual([n for n in after if n not in before and n.endswith((".pem", ".crt", ".key"))], [])

    def test_send_pair_ack_over_real_wire_matches_service_contract(self):
        calls, handle = self.handler_spy(self.b)
        item = self.listen(self.B, self.bb, handle)

        def send(endpoint_id, generation, request):
            kind, *rest = wire(item.address[1], client_bundle=self.ba, server_bundle=self.bb, request=request,
                               endpoint_id=endpoint_id, generation=generation)
            self.assertEqual((kind, rest[0]), ("ok", 200))
            return json.loads(rest[1])
        row = self.a.send_pair_ack(self.paired["a_id"], send)
        self.assertEqual(row["state"], "acknowledged")
        self.assertEqual(self.b_state(), "acknowledged")

    def test_confirmed_link_allows_only_pair_ack(self):
        calls, handle = self.handler_spy(self.b)
        item = self.listen(self.B, self.bb, handle)
        link = self.link_b()
        for op in ({"op": "status", "lobby_id": LOBBY, "invite_id": INVITE},
                   {"op": "preview", "lobby_id": LOBBY, "invite_id": INVITE},
                   {"op": "redeem", "lobby_id": LOBBY, "invite_id": INVITE, "secret": "x"},
                   {"op": "material_request", "lobby_id": LOBBY, "invite_id": INVITE},
                   {"op": "room_log", "room_id": "r", "since_seq": 0, "limit": 1},
                   {"op": "bogus"}):
            res = wire(item.address[1], client_bundle=self.ba, server_bundle=self.bb, request=op,
                       endpoint_id=link["endpoint_id"], generation=self.b.journal.snapshot().link_generation(link))
            self.assertEqual(res[0:2], ("ok", 403), (op, res))
            self.assertEqual(res[2], b'{"error":"control denied"}')
        self.assertEqual((calls, self.b_state()), ([], "confirmed"))

    def test_acknowledged_link_still_has_no_w2a_handler_for_other_ops(self):
        calls, handle = self.handler_spy(self.b)
        item = self.listen(self.B, self.bb, handle)
        req, result = self.ack_on_b(item)
        self.assertEqual(result[1], 200)
        link = self.link_b()
        res = wire(item.address[1], client_bundle=self.ba, server_bundle=self.bb,
                   request={"op": "status", "lobby_id": LOBBY, "invite_id": INVITE},
                   endpoint_id=link["endpoint_id"], generation=req.generation)
        self.assertEqual(res[0:2], ("ok", 403))
        self.assertEqual(len(calls), 1)                         # no fake preview/status handler exists


class IdentityMultiplexTest(W2aBase):
    def test_unrelated_journal_ca_is_refused_before_any_frame(self):
        calls, handle = self.handler_spy(self.b)
        item = self.listen(self.B, self.bb, handle)
        other = ps.Side(self.tmp, "c", ps.INSTALL_C, template=TEMPLATES[ps.INSTALL_C])
        bundle_c = identity_module.load_identity(other.identity_dir, installation_id=ps.INSTALL_C, profile="default")
        req = self.a.ack_request(self.paired["a_id"])
        res = wire(item.address[1], client_bundle=bundle_c, server_bundle=self.bb, request=req.request,
                   endpoint_id=req.endpoint_id, generation=req.generation)
        self.assertEqual(res[0], "err")
        time.sleep(.1)
        self.assertEqual((calls, item.dispatched, self.b_state()), ([], 0, "confirmed"))

    def test_correct_ca_but_wrong_leaf_is_refused(self):
        """A leaf signed by the peer's REAL CA but not the journal-pinned leaf: CA alone is not trust."""
        calls, handle = self.handler_spy(self.b)
        item = self.listen(self.B, self.bb, handle)
        cur = self.A.identity_dir / "current" / "tls"
        ca_key = serialization.load_pem_private_key((cur / "ca.key").read_bytes(), password=None)
        ca = x509.load_pem_x509_certificate((cur / "ca.pem").read_bytes())
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        now = dt.datetime.now(dt.timezone.utc)
        leaf = identity_module._build(subject="mc-rooms-peer", public_key=key.public_key(), issuer=ca.subject,
                                      issuer_key=ca_key, issuer_ski=x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()),
                                      days=10, ca=False, now=now,
                                      eku=[ExtendedKeyUsageOID.SERVER_AUTH, ExtendedKeyUsageOID.CLIENT_AUTH])
        rogue = ps.private_dir(self.tmp / "rogue")
        (rogue / "leaf.pem").write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
        (rogue / "leaf.key").write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                           serialization.NoEncryption()))
        req = self.a.ack_request(self.paired["a_id"])
        res = wire(item.address[1], cert_file=str(rogue / "leaf.pem"), key_file=str(rogue / "leaf.key"),
                   server_bundle=self.bb, request=req.request, endpoint_id=req.endpoint_id, generation=req.generation)
        self.assertEqual(res[0], "err")
        time.sleep(.1)
        self.assertEqual((calls, item.dispatched, self.b_state(), item.refused), ([], 0, "confirmed", 1))

    def test_envelope_must_match_resolved_link(self):
        calls, handle = self.handler_spy(self.b)
        item = self.listen(self.B, self.bb, handle)
        req = self.a.ack_request(self.paired["a_id"])
        for endpoint_id, generation in (("link-" + "0" * 32, req.generation), (req.endpoint_id, req.generation + 1)):
            res = wire(item.address[1], client_bundle=self.ba, server_bundle=self.bb, request=req.request,
                       endpoint_id=endpoint_id, generation=generation)
            self.assertEqual(res[0], "err", res)
        self.assertEqual((calls, item.dispatched, self.b_state()), ([], 0, "confirmed"))

    def test_ambiguous_fingerprint_is_refused(self):
        calls, handle = self.handler_spy(self.b)

        class Doubled(JournalLinkAuthority):
            def active_trusts(inner):
                one = super().active_trusts()
                self.assertEqual(len(one), 1)
                t = one[0]
                twin = PeerTrust(endpoint_id="link-" + "9" * 32, generation=t.generation, role=t.role,
                                 protocol_state=t.protocol_state, peer_id="peer-" + "9" * 32, ca_pem=t.ca_pem,
                                 fingerprint=t.fingerprint, binding=t.binding)
                return (t, twin)
        item = self.listen(self.B, self.bb, handle, authority=Doubled(self.b.journal))
        req, res = self.ack_on_b(item)
        self.assertEqual(res[0], "err")
        self.assertEqual((calls, item.dispatched, self.b_state()), ([], 0, "confirmed"))

    def test_unpaired_peer_has_empty_trust_and_is_refused(self):
        calls, handle = self.handler_spy(self.b)
        item = self.listen(self.B, self.bb, handle)
        req = self.a.ack_request(self.paired["a_id"])
        self.b.unpair(PEER_A)
        res = wire(item.address[1], client_bundle=self.ba, server_bundle=self.bb, request=req.request,
                   endpoint_id=req.endpoint_id, generation=req.generation)
        self.assertEqual(res[0], "err")
        self.assertEqual((calls, item.dispatched, self.b.journal.snapshot().active_links()), ([], 0, []))

    def test_repaired_generation_rejects_old_ack_and_accepts_new(self):
        calls, handle = self.handler_spy(self.b)
        item = self.listen(self.B, self.bb, handle)
        old = self.a.ack_request(self.paired["a_id"])
        self.b.unpair(PEER_A)
        self.a.unpair(PEER_B)
        self.clock.advance(7 * 24 * 3600)
        paired2 = ps.drive_pairing(self.a, self.b)
        self.paired = paired2
        res = wire(item.address[1], client_bundle=self.ba, server_bundle=self.bb, request=old.request,
                   endpoint_id=old.endpoint_id, generation=old.generation)
        self.assertEqual(res[0], "err", res)
        self.assertEqual((calls, item.dispatched), ([], 0))
        new_req, ok = self.ack_on_b(item)
        self.assertNotEqual(new_req.endpoint_id, old.endpoint_id)
        self.assertEqual(ok[0:2], ("ok", 200), ok)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.b_state(), "acknowledged")

    def test_unpair_during_handshake_yields_zero_handler_effect(self):
        calls, handle = self.handler_spy(self.b)

        class UnpairAfterResolve(ControlListener):
            def _resolve(inner, fingerprint, leaf_der):
                found = super()._resolve(fingerprint, leaf_der)
                self.assertIsNotNone(found)
                self.b.unpair(PEER_A)               # durable unpair lands between handshake and first frame
                return found
        item = self.listen(self.B, self.bb, handle, cls=UnpairAfterResolve)
        req, res = self.ack_on_b(item)
        self.assertEqual(res[0], "err")
        self.assertEqual((calls, item.dispatched), ([], 0))

    def test_unpair_after_first_frame_but_before_dispatch_yields_zero_handler_effect(self):
        calls, handle = self.handler_spy(self.b)

        class UnpairOnSecondCheck(ControlListener):
            n = 0

            def _still_current(inner, trust):
                inner.n += 1
                if inner.n == 2:                    # the pre-dispatch re-read, after the frame was parsed
                    self.b.unpair(PEER_A)
                return super()._still_current(trust)
        item = self.listen(self.B, self.bb, handle, cls=UnpairOnSecondCheck)
        req, res = self.ack_on_b(item)
        self.assertEqual(res[0], "err")
        self.assertEqual((calls, item.dispatched), ([], 0))

    def test_host_role_link_has_no_handler_for_pair_ack(self):
        """Direction is derived from the journal role: on host A an incoming pair_ack from B has no handler."""
        calls, handle = self.handler_spy(self.a)
        item = self.listen(self.A, self.ba, handle)
        req = self.a.ack_request(self.paired["a_id"])      # any well-formed pair_ack
        link = next(iter(self.a.journal.snapshot().links.values()))
        res = wire(item.address[1], client_bundle=self.bb, server_bundle=self.ba, request=req.request,
                   endpoint_id=link["endpoint_id"], generation=req.generation)
        self.assertEqual(res[0:2], ("ok", 403), res)
        self.assertEqual(calls, [])
        self.assertEqual(item.dispatched, 1)               # reached the dispatcher, which has no B_to_A handlers


class LifecycleTest(W2aBase):
    def test_bind_failure_leaves_no_threads_or_sockets(self):
        busy = socket.socket()
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        self.addCleanup(busy.close)
        before = listener_threads()
        with self.assertRaises(OSError):
            self.listen(self.B, self.bb, lambda r: {}, port=busy.getsockname()[1])
        self.assertEqual(listener_threads(), before)

    def test_normal_shutdown_joins_everything(self):
        item = self.listen(self.B, self.bb, lambda r: {})
        self.assertTrue(listener_threads())
        item.close()
        item.join(3)
        self.assertEqual(listener_threads(), [])
        with self.assertRaises(OSError):
            socket.create_connection(item.address, timeout=1)
        with self.assertRaises(ListenerError):
            item.start()

    def test_shutdown_with_stalled_connection_is_bounded(self):
        item = self.listen(self.B, self.bb, lambda r: {}, timeout=5.0)
        stalled = socket.create_connection(item.address, timeout=2)   # never speaks TLS
        self.addCleanup(stalled.close)
        time.sleep(.2)
        started = time.monotonic()
        item.close()
        self.assertLess(time.monotonic() - started, 4)
        self.assertEqual(listener_threads(), [])

    def test_shutdown_after_handshake_before_frame_is_bounded(self):
        item = self.listen(self.B, self.bb, lambda r: {}, timeout=5.0)
        ctx = tls_module.client_context(ca_data=self.bb.ca_pem, cert_file=str(self.ba.leaf_cert_file),
                                        key_file=str(self.ba.leaf_key_file))
        sock = socket.create_connection(item.address, timeout=2)
        tls = tls_module.wrap_client(sock, ctx, expected_fingerprint=self.bb.leaf_fingerprint)
        self.addCleanup(tls.close)
        time.sleep(.3)
        started = time.monotonic()
        item.close()
        self.assertLess(time.monotonic() - started, 4)
        self.assertEqual(listener_threads(), [])

    def test_bind_must_be_explicit_literal_loopback(self):
        auth = JournalLinkAuthority(self.b.journal)
        for host, port in (("0.0.0.0", 9), ("localhost", 9), ("127.0.0.2", 9), ("::", 9), ("127.0.0.1", 0),
                           ("127.0.0.1", 65536), ("127.0.0.1", True), ("127.0.0.1", "9"), ("127.0.0.1", 1.0)):
            with self.assertRaises(ListenerError, msg=(host, port)):
                ControlListener(host=host, port=port, authority=auth, identity=lambda: ("", ""),
                                pair_ack_handler=lambda r: {})


class ConfigTest(W2aBase):
    def cfg(self, side, listen=None, endpoints=None, raw_listen=None):
        pairing = {"identity_dir": str(side.identity_dir), "journal_dir": str(side.journal_dir),
                   "peers_path": str(side.peers_path)}
        if listen is not None:
            pairing["control_listen"] = {"host": listen[0], "port": listen[1]}
        if raw_listen is not None:
            pairing["control_listen"] = raw_listen
        cfg = {"store_path": str(side.root / "invites" / "host.sqlite"), "installation_id": side.installation_id,
               "profile": "default", "peer_registry_path": str(side.peers_path), "pairing": pairing}
        if endpoints is not None:
            cfg["joining"] = {"store_dir": str(side.root / "joining"), "identity_dir": str(side.identity_dir),
                              "hosts_path": str(side.root / "cfg" / "hosts.json"),
                              "origins": {PEER_A: "https://host.example"}, "host_label": "host", "own_bot_id": "self",
                              "control_endpoints": {PEER_A: {"host": endpoints[0], "port": endpoints[1]}}}
        return cfg

    def load(self, side, cfg, mode=0o600):
        path = side.root / "cfg" / "runtime.json"
        private_json(path, cfg)
        path.chmod(mode)
        with patch.dict(os.environ, {"MISSION_CONTROL_ROOM_INVITES_CONFIG": str(path)}):
            return runtime_config.load_service()

    def test_listen_field_validation(self):
        good = self.load(self.B, self.cfg(self.B, listen=("127.0.0.1", 51001)))
        self.assertEqual(good[2].pairing.control_listen, ("127.0.0.1", 51001))
        self.assertEqual(self.load(self.B, self.cfg(self.B, listen=("::1", 51001)))[2].pairing.control_listen,
                         ("::1", 51001))
        # absent => disabled (None), existing configs keep loading
        self.assertIsNone(self.load(self.B, self.cfg(self.B))[2].pairing.control_listen)
        bad = [{"host": "127.0.0.1"}, {"port": 5}, {"host": "127.0.0.1", "port": 5, "extra": 1},
               {"host": "localhost", "port": 5}, {"host": "0.0.0.0", "port": 5}, {"host": "::", "port": 5},
               {"host": "127.0.0.2", "port": 5}, {"host": "[::1]", "port": 5}, {"host": "::ffff:127.0.0.1", "port": 5},
               {"host": "127.0.0.1", "port": True}, {"host": "127.0.0.1", "port": "5"},
               {"host": "127.0.0.1", "port": 5.0}, {"host": "127.0.0.1", "port": 0},
               {"host": "127.0.0.1", "port": 65536}, {"host": "127.0.0.1", "port": -1}, "127.0.0.1:5", [], {}]
        for raw in bad:
            self.assertIsNone(self.load(self.B, self.cfg(self.B, raw_listen=raw)), raw)
        explicit_null = self.cfg(self.B)
        explicit_null["pairing"]["control_listen"] = None
        self.assertIsNone(self.load(self.B, explicit_null))

    def test_duplicate_keys_and_non_private_config_are_refused(self):
        path = self.B.root / "cfg" / "runtime.json"
        good = self.cfg(self.B, listen=("127.0.0.1", 51001))
        private_json(path, good)
        text = path.read_text().replace('"port": 51001', '"port": 51001, "port": 51002')
        self.assertIn("51002", text)
        path.write_text(text)
        with patch.dict(os.environ, {"MISSION_CONTROL_ROOM_INVITES_CONFIG": str(path)}):
            self.assertIsNone(runtime_config.load_service())
        self.assertIsNone(self.load(self.B, good, mode=0o644))

    def test_listener_bind_and_peer_client_endpoint_must_match(self):
        """Paired scratch configs: B's client tuple == A's pairing.control_listen, else nothing reaches the listener."""
        calls, handle = self.handler_spy(self.a)
        port = free_port()
        a_cfg = self.load(self.A, self.cfg(self.A, listen=("127.0.0.1", port)))[2]
        item = build_pair_ack_listener(a_cfg, installation_id=ps.INSTALL_A, profile="default", timeout=3.0)
        self.listeners.append(item)
        self.assertEqual(item.address, ("127.0.0.1", port))
        item.start()
        # Finish activation on both sides so B's W1 client is allowed to speak (no listener is needed on B).
        d = ps.dispatcher_for(self.b)
        try:
            self.a.send_pair_ack(self.paired["a_id"], ps.link_sender(d))
        finally:
            asyncio.run(d.close())

        def owner(endpoint_port):
            loaded = self.load(self.B, self.cfg(self.B, endpoints=("127.0.0.1", endpoint_port)))
            self.assertIsNotNone(loaded)
            with patch.dict(os.environ, {"MISSION_CONTROL_ROOM_INVITES_CONFIG": str(self.B.root / "cfg" / "runtime.json")}):
                rj.configure_joining_control(rj.default_joining_control)
                self.addCleanup(rj.configure_joining_control)
                self.addCleanup(rj.configure_joining_transport)
                o = rj.load_owner(loaded[2])
            self.assertIsNotNone(o)
            self.addCleanup(o.close)
            return o
        mismatched = owner(free_port())
        with self.assertRaises(JoiningError):
            mismatched.control({"op": "status", "invite_id": INVITE}, lobby_id=LOBBY)
        self.assertEqual((item.dispatched, item.refused), (0, 0))
        matching = owner(port)
        with self.assertRaises(JoiningError):                      # reaches A's dispatcher; W2a has no handler
            matching.control({"op": "status", "invite_id": INVITE}, lobby_id=LOBBY)
        self.assertEqual(item.dispatched, 1)

    def test_listener_requires_configured_bind(self):
        cfg = self.load(self.B, self.cfg(self.B))[2]
        with self.assertRaises(ListenerError):
            build_pair_ack_listener(cfg, installation_id=ps.INSTALL_B, profile="default")

    def test_config_built_listener_serves_real_pair_ack(self):
        port = free_port()
        cfg = self.load(self.B, self.cfg(self.B, listen=("127.0.0.1", port)))[2]
        item = build_pair_ack_listener(cfg, installation_id=ps.INSTALL_B, profile="default", timeout=3.0)
        self.listeners.append(item)
        item.start()
        req, res = self.ack_on_b(item)
        self.assertEqual(res[0:2], ("ok", 200), res)
        self.assertEqual(self.a.complete_ack(self.paired["a_id"], json.loads(res[2]))["state"], "acknowledged")
        self.assertEqual(self.b_state(), "acknowledged")


class EndpointBindingTest(W2aBase):
    """R1: the nested signed pair_ack must name the TLS-resolved link; a valid signature is not authorization."""

    def setUp(self):
        super().setUp()
        self.C = ps.Side(self.tmp, "c", ps.INSTALL_C, template=TEMPLATES[ps.INSTALL_C])
        self.c = self.C.service(self.clock)
        self.paired_c = ps.drive_pairing(self.c, self.b)       # B now holds two confirmed links: A and C
        self.bc = identity_module.load_identity(self.C.identity_dir, installation_id=ps.INSTALL_C, profile="default")
        self.assertNotEqual(self.ba.ca_pem, self.bc.ca_pem)     # genuinely distinct CA keys

    def state(self, session_id):
        return self.b.journal.snapshot().sessions[session_id]["state"]

    def test_foreign_signed_ack_over_a_connection_is_refused_before_handler(self):
        calls, handle = self.handler_spy(self.b)
        item = self.listen(self.B, self.bb, handle)
        a_req = self.a.ack_request(self.paired["a_id"])
        c_req = self.c.ack_request(self.paired_c["a_id"])
        self.assertNotEqual(a_req.endpoint_id, c_req.endpoint_id)
        # A's real leaf + A's envelope endpoint/generation, but C's genuine signed acknowledgement.
        res = wire(item.address[1], client_bundle=self.ba, server_bundle=self.bb, request=c_req.request,
                   endpoint_id=a_req.endpoint_id, generation=a_req.generation)
        self.assertEqual(res[0], "err", res)
        time.sleep(.1)
        self.assertEqual((calls, item.dispatched), ([], 0))
        self.assertEqual((self.state(self.paired["b_id"]), self.state(self.paired_c["b_id"])),
                         ("confirmed", "confirmed"))

    def test_malformed_pair_ack_never_reaches_handler(self):
        calls, handle = self.handler_spy(self.b)
        item = self.listen(self.B, self.bb, handle)
        a_req = self.a.ack_request(self.paired["a_id"])
        for bad in ({"op": "pair_ack", "ack": "x"}, {"op": "pair_ack", "ack": {"v": 1}}):
            res = wire(item.address[1], client_bundle=self.ba, server_bundle=self.bb, request=bad,
                       endpoint_id=a_req.endpoint_id, generation=a_req.generation)
            self.assertEqual(res[0], "err", res)
        self.assertEqual((calls, item.dispatched), ([], 0))

    def test_ordinary_a_and_c_acks_and_exact_retry_still_succeed(self):
        calls, handle = self.handler_spy(self.b)
        item = self.listen(self.B, self.bb, handle)
        a_req = self.a.ack_request(self.paired["a_id"])
        c_req = self.c.ack_request(self.paired_c["a_id"])
        ra = wire(item.address[1], client_bundle=self.ba, server_bundle=self.bb, request=a_req.request,
                  endpoint_id=a_req.endpoint_id, generation=a_req.generation)
        rc = wire(item.address[1], client_bundle=self.bc, server_bundle=self.bb, request=c_req.request,
                  endpoint_id=c_req.endpoint_id, generation=c_req.generation)
        self.assertEqual((ra[0:2], rc[0:2]), (("ok", 200), ("ok", 200)))
        self.assertEqual(self.a.complete_ack(self.paired["a_id"], json.loads(ra[2]))["state"], "acknowledged")
        self.assertEqual(self.c.complete_ack(self.paired_c["a_id"], json.loads(rc[2]))["state"], "acknowledged")
        again = wire(item.address[1], client_bundle=self.ba, server_bundle=self.bb, request=a_req.request,
                     endpoint_id=a_req.endpoint_id, generation=a_req.generation)
        self.assertEqual(again, ra)
        self.assertEqual(len(calls), 3)
        self.assertEqual((self.state(self.paired["b_id"]), self.state(self.paired_c["b_id"])),
                         ("acknowledged", "acknowledged"))


def drain_threads():
    return [t for t in threading.enumerate() if t.name.startswith("invite-control") and t.is_alive()]


class ExecutorDrainTest(W2aBase):
    """R2: a successful close/join means no listener-owned handler thread remains."""

    def hold(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def handle(request):
            entered.set()
            release.wait(30)                     # a noncooperative blocking handler
            return self.b.handle_pair_ack(request)
        return entered, release, handle

    def admitted_request(self, item, entered):
        req = self.a.ack_request(self.paired["a_id"])
        out = []
        client = threading.Thread(target=lambda: out.append(wire(
            item.address[1], client_bundle=self.ba, server_bundle=self.bb, request=req.request,
            endpoint_id=req.endpoint_id, generation=req.generation)))
        client.start()
        self.addCleanup(client.join, 10)
        self.assertTrue(entered.wait(5))
        return out

    def test_normal_close_waits_for_the_owned_executor_thread(self):
        entered, release, handle = self.hold()
        item = self.listen(self.B, self.bb, handle, timeout=1.0)
        self.admitted_request(item, entered)
        self.assertTrue(drain_threads())
        timer = threading.Timer(1.6, release.set)      # lands AFTER the connection worker timed out (~1s)
        timer.start()
        self.addCleanup(timer.cancel)
        started = time.monotonic()
        item.close()
        elapsed = time.monotonic() - started
        self.assertEqual(drain_threads(), [], "close() returned while a handler thread was alive")
        self.assertGreaterEqual(elapsed, 1.4)
        item.join(2)
        self.assertEqual(listener_threads(), [])

    def test_overrun_reports_incomplete_retains_ownership_and_retry_completes(self):
        entered, release, handle = self.hold()
        with patch.object(listener_module, "DRAIN_TIMEOUT", 0.3, create=True):
            item = self.listen(self.B, self.bb, handle, timeout=1.0)
        self.admitted_request(item, entered)
        with self.assertRaises(ListenerError) as cm:
            item.close()
        self.assertEqual(str(cm.exception), "listener teardown incomplete")
        self.assertTrue(drain_threads())                     # truthfully still there ...
        self.assertIsNotNone(item._loop)                     # ... and still owned: loop/dispatchers retained
        self.assertFalse(item._loop.is_closed())
        self.assertTrue(item._dispatchers)
        release.set()
        item.close()                                         # retry completes
        item.join(2)
        self.assertEqual(drain_threads(), [])
        self.assertEqual(listener_threads(), [])
        self.assertIsNone(item._loop)


class StartupRollbackTest(W2aBase):
    """R3: a failed thread start rolls back exactly what was created and surfaces the ORIGINAL error."""

    def check(self, fail_name):
        real = threading.Thread
        boom = OSError("injected-start-failure")

        class Failing(real):
            def start(inner):
                if inner.name == fail_name:
                    raise boom
                return real.start(inner)
        loops = []
        real_new = asyncio.new_event_loop

        def record():
            loop = real_new()
            loops.append(loop)
            return loop
        port = free_port()
        item = ControlListener(host="127.0.0.1", port=port, authority=JournalLinkAuthority(self.b.journal),
                               identity=lambda: (str(self.bb.leaf_cert_file), str(self.bb.leaf_key_file)),
                               pair_ack_handler=lambda r: {}, timeout=3.0)
        self.listeners.append(item)
        before = listener_threads()
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            with patch.object(listener_module.threading, "Thread", Failing), \
                    patch.object(listener_module.asyncio, "new_event_loop", record):
                with self.assertRaises(BaseException) as cm:
                    item.start()
            gc.collect()
        self.assertIs(cm.exception, boom)
        self.assertEqual(len(loops), 1)
        self.assertTrue(loops[0].is_closed())
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=1)
        self.assertEqual(listener_threads(), before)
        self.assertEqual([str(w.message) for w in caught if issubclass(w.category, ResourceWarning)], [])

    def test_loop_thread_start_failure_preserves_original_error_and_closes_loop(self):
        self.check("room-control-loop")

    def test_accept_thread_start_failure_stops_the_started_loop_thread(self):
        self.check("room-control-accept")


class NoSidecarWiringTest(unittest.TestCase):
    def test_telemetry_main_does_not_install_the_listener(self):
        source = (SERVER_DIR / "local_telemetry_server.py").read_text()
        for needle in ("control.listener", "ControlListener", "build_pair_ack_listener", "control_listen"):
            self.assertNotIn(needle, source)
        code = ("import sys; sys.path.insert(0, %r); import local_telemetry_server; "
                "print('invite_rooms.control.listener' in sys.modules)" % str(SERVER_DIR))
        out = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True, text=True, timeout=60,
                             env={"PATH": "/usr/bin:/bin", "HOME": tempfile.gettempdir()})
        self.assertEqual(out.stdout.strip().splitlines()[-1:], ["False"], out.stderr[-500:])


if __name__ == "__main__":
    unittest.main()
