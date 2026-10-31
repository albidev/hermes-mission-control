"""P3b-W2b0: the REAL host-A ``pair_ack`` sender (actual confirm route -> ``PairingService.send_pair_ack`` ->
pinned ``PairAckConnector`` -> B's real W2a listener over loopback mTLS 1.3).

Real scratch pairings A(host)/B(joining), real identity bundles, real journals.  Nothing here starts a listener from
the sidecar ``main()`` (W2d); the B listener is instantiated directly.
"""
from __future__ import annotations

import datetime as dt
import json
import shutil
import socket
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pairing_support as ps
import test_control_listener_w2a as w2a
import test_room_invite_pairing_routes as base
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID
from invite_rooms import identity as identity_module
from invite_rooms import runtime_config
from invite_rooms.control import connector as connector_module
from invite_rooms.control import framing as framing_module
from invite_rooms.control.connector import (LinkBinding, LinkIdentity, PairAckConnector, make_link_connector,
                                            make_pair_ack_connector)
from invite_rooms.control.firewall import FirewallDenied, ForwardedRequest
from invite_rooms.control.listener import ControlListener
from invite_rooms.pair_ack_sender import PairAckUnavailable, pair_ack_sender
from invite_rooms.pairing_journal import Journal, JournalError, JournalLinkAuthority

PEER_B = "peer-" + "b" * 32
PEER_C = "peer-" + "c" * 32
LOBBY = "lobby-" + "1" * 32
INVITE = "invite-" + "2" * 32
PENDING = {"ack": "pending", "code": "pairing_ack_pending"}
C_TEMPLATE = None


def setUpModule():
    global C_TEMPLATE
    base.setUpModule()
    C_TEMPLATE = ps.identity_template(ps.INSTALL_C)


def tearDownModule():
    base.tearDownModule()
    shutil.rmtree(C_TEMPLATE.parent, ignore_errors=True)


def endpoint(port, host="127.0.0.1"):
    return {"host": host, "port": port}


class W2b0Base(base.PairingRoutesBase):
    """A (host) drives the REAL route; B (joining) is served by a real W2a ``ControlListener``."""
    ENDPOINTS = "default"

    def setUp(self):
        self.b_port = w2a.free_port()
        self.threads_before = set(threading.enumerate())
        super().setUp()
        self.bb = identity_module.load_identity(self.B.identity_dir, installation_id=ps.INSTALL_B, profile="default")
        self.listeners = []
        self.addCleanup(self._close_listeners)
        self.b_calls = []

    def write_config(self, pairing="default"):
        if isinstance(pairing, str) and pairing == "default":
            pairing = {"identity_dir": str(self.A.identity_dir), "journal_dir": str(self.A.journal_dir),
                       "peers_path": str(self.A.peers_path)}
            endpoints = ({PEER_B: endpoint(self.b_port)} if self.ENDPOINTS == "default" else self.ENDPOINTS)
            if endpoints is not None:
                pairing["control_endpoints"] = endpoints
        super().write_config(pairing)

    def _close_listeners(self):
        for item in self.listeners:
            try:
                item.close()
            except Exception:
                pass

    # -- B side ------------------------------------------------------------------------------------------------
    def start_b(self, handler=None, *, port=None, service=None, bundle=None):
        service = service or self.b
        bundle = bundle or self.bb

        def default(request):
            self.b_calls.append(request)
            return service.handle_pair_ack(request)
        item = ControlListener(host="127.0.0.1", port=port or self.b_port,
                               authority=JournalLinkAuthority(service.journal),
                               identity=lambda: (str(bundle.leaf_cert_file), str(bundle.leaf_key_file)),
                               pair_ack_handler=handler or default, timeout=3.0)
        self.listeners.append(item)
        item.start()
        return item

    def ready(self):
        """Pairing driven to: A revealed (route), B CONFIRMED.  A's confirm route is the next (and only) step."""
        offer = self.req("POST", "/pairing/offers", {})[1]["offer"]
        accepted = self.b.import_offer(offer["code"])
        reveal = self.req("POST", f"/pairing/offers/{offer['id']}/accept", {"code": accepted["code"]})[1]["reveal"]
        b_sas = self.b.reveal_import(accepted["id"], reveal["code"])
        self.b.confirm_import(accepted["id"], reveal["sas"])
        return SimpleNamespace(a_id=offer["id"], b_id=accepted["id"], sas=b_sas["sas"])

    def confirm(self, p, sas=None):
        return self.req("POST", f"/pairing/offers/{p.a_id}/confirm", {"sas": sas or p.sas})

    def a_session(self, p):
        return self.A.service().journal.snapshot().sessions[p.a_id]

    def b_session(self, p):
        return self.b.journal.snapshot().sessions[p.b_id]

    def loaded_config(self):
        config = runtime_config._load_config_file(str(self.config_path))
        self.assertIsNotNone(config)
        return config

    def a_trust(self, p):
        link = self.A.service().journal.snapshot().links
        (endpoint_id, record), = link.items()
        snap = self.A.service().journal.snapshot()
        return endpoint_id, snap.link_generation(record)

    def connects(self):
        """Spy on the product's outbound dials (the test's own HTTP client to the sidecar is not one of them)."""
        http_port = self.httpd.server_address[1]
        real = socket.create_connection
        spy = SimpleNamespace(call_count=0, addresses=[])

        def connect(address, *a, **k):
            if address[1] != http_port:
                spy.call_count += 1
                spy.addresses.append(address)
            return real(address, *a, **k)
        patcher = patch.object(socket, "create_connection", connect)

        class Ctx:
            def __enter__(inner):
                patcher.start()
                return spy

            def __exit__(inner, *exc):
                patcher.stop()
        return Ctx()

    def lock_free(self):
        try:
            with Journal(self.A.journal_dir, anchor=self.A.journal_dir.parent)._locked(exclusive=True, nonblocking=True):
                return True
        except JournalError:
            return False


# ======================================================================================================== config
class ControlEndpointsConfigTest(W2b0Base):
    def load(self, endpoints, *, listen=None, raw=None):
        pairing = {"identity_dir": str(self.A.identity_dir), "journal_dir": str(self.A.journal_dir),
                   "peers_path": str(self.A.peers_path), "control_endpoints": endpoints}
        if listen is not None:
            pairing["control_listen"] = listen
        self.write_config(pairing)
        if raw is not None:
            self.config_path.write_text(raw)
        runtime_config._warned_once.clear()
        return runtime_config._load_config_file(str(self.config_path))

    def test_valid_map_is_parsed_read_only_and_distinct_from_control_listen(self):
        cfg = self.load({PEER_B: endpoint(40001), PEER_C: endpoint(40002, "::1")}, listen=endpoint(40003))
        self.assertEqual(dict(cfg.pairing.control_endpoints), {PEER_B: ("127.0.0.1", 40001), PEER_C: ("::1", 40002)})
        self.assertEqual(cfg.pairing.control_listen, ("127.0.0.1", 40003))
        with self.assertRaises(TypeError):
            cfg.pairing.control_endpoints[PEER_B] = ("127.0.0.1", 1)

    def test_control_endpoints_alone_loads_with_absent_control_listen(self):
        cfg = self.load({PEER_B: endpoint(40001)})
        self.assertIsNotNone(cfg)
        self.assertIsNone(cfg.pairing.control_listen)
        self.assertEqual(dict(cfg.pairing.control_endpoints), {PEER_B: ("127.0.0.1", 40001)})
        with self.assertRaises(TypeError):
            cfg.pairing.control_endpoints[PEER_C] = ("127.0.0.1", 1)

    def test_explicit_control_listen_is_validated_as_a_tuple(self):
        cfg = self.load({PEER_B: endpoint(40001)}, listen=endpoint(40003))
        self.assertEqual(cfg.pairing.control_listen, ("127.0.0.1", 40003))
        self.assertIsNone(self.load({PEER_B: endpoint(40001)}, listen={"host": "0.0.0.0", "port": 40003}))

    def test_absent_means_no_sender(self):
        self.ENDPOINTS = None
        self.write_config()
        cfg = runtime_config._load_config_file(str(self.config_path))
        self.assertIsNone(cfg.pairing.control_endpoints)
        self.assertIsNone(pair_ack_sender(cfg))

    def test_invalid_shapes_reject_the_whole_config(self):
        bad = {
            "empty": {}, "not_object": [endpoint(1)], "string": "x",
            "wildcard": {PEER_B: endpoint(40001, "0.0.0.0")}, "dns": {PEER_B: endpoint(40001, "localhost")},
            "other_loopback": {PEER_B: endpoint(40001, "127.0.0.2")}, "scoped": {PEER_B: endpoint(40001, "::1%lo0")},
            "mapped": {PEER_B: endpoint(40001, "::ffff:127.0.0.1")}, "empty_host": {PEER_B: endpoint(40001, "")},
            "port_zero": {PEER_B: endpoint(0)}, "port_big": {PEER_B: endpoint(65536)},
            "port_bool": {PEER_B: {"host": "127.0.0.1", "port": True}}, "port_str": {PEER_B: {"host": "127.0.0.1", "port": "1"}},
            "port_float": {PEER_B: {"host": "127.0.0.1", "port": 4000.0}}, "no_port": {PEER_B: {"host": "127.0.0.1"}},
            "extra_fingerprint": {PEER_B: {"host": "127.0.0.1", "port": 40001, "fingerprint": "ab" * 32}},
            "extra_ca": {PEER_B: {"host": "127.0.0.1", "port": 40001, "ca_pem": "x"}},
            "extra_generation": {PEER_B: {"host": "127.0.0.1", "port": 40001, "generation": 1}},
            "entry_not_object": {PEER_B: "127.0.0.1:40001"},
            "bad_peer_key": {"not-a-peer": endpoint(40001)}, "peer_key_upper": {"peer-" + "B" * 32: endpoint(40001)},
            "peer_key_short": {"peer-abc": endpoint(40001)}, "peer_key_default": {"default": endpoint(40001)},
        }
        for name, endpoints in bad.items():
            self.assertIsNone(self.load(endpoints), name)

    def test_duplicate_json_keys_are_rejected(self):
        a = self.A
        good = json.dumps({"store_path": str(self.tmp / "a" / "store" / "invites.db"), "installation_id": ps.INSTALL_A,
                           "profile": ps.PROFILE, "peer_registry_path": str(a.peers_path),
                           "pairing": {"identity_dir": str(a.identity_dir), "journal_dir": str(a.journal_dir),
                                       "peers_path": str(a.peers_path), "control_endpoints": {PEER_B: endpoint(40001)}}})
        self.assertIsNotNone(self.load({PEER_B: endpoint(40001)}, raw=good))
        dup_peer = good.replace(json.dumps({PEER_B: endpoint(40001)}),
                                '{"%s": {"host": "127.0.0.1", "port": 40001}, "%s": {"host": "127.0.0.1", "port": 40002}}' % (PEER_B, PEER_B))
        self.assertNotEqual(dup_peer, good)
        self.assertIsNone(self.load({PEER_B: endpoint(40001)}, raw=dup_peer))
        dup_field = good.replace('"port": 40001}', '"port": 40001, "port": 40002}')
        self.assertNotEqual(dup_field, good)
        self.assertIsNone(self.load({PEER_B: endpoint(40001)}, raw=dup_field))
        dup_block = good.replace('"control_endpoints"', '"control_endpoints": {}, "control_endpoints"')
        self.assertIsNone(self.load({PEER_B: endpoint(40001)}, raw=dup_block))


# ====================================================================================================== route GREEN
class ConfirmRouteSendsPairAckTest(W2b0Base):
    def test_actual_confirm_route_reaches_b_listener_and_both_journals_acknowledge(self):
        p = self.ready()
        item = self.start_b()
        before = {f for f in self.tmp.rglob("*") if f.is_file()}
        with patch.object(connector_module, "client_context", wraps=connector_module.client_context) as spy:
            status, data, _ = self.confirm(p)
        self.assertEqual((status, data["pairing"]["state"]), (200, "acknowledged"), data)
        self.assertEqual(set(data), {"pairing"})
        # real client TLS context: in-memory CA only
        self.assertIsNone(spy.call_args.kwargs["ca_file"])
        self.assertIn("BEGIN CERTIFICATE", spy.call_args.kwargs["ca_data"])
        self.assertEqual({f for f in self.tmp.rglob("*") if f.is_file() and f.suffix in (".pem", ".crt", ".key")},
                         {f for f in before if f.suffix in (".pem", ".crt", ".key")})
        # B's listener really dispatched exactly the journal-stored signed ack
        a, b = self.a_session(p), self.b_session(p)
        self.assertEqual((a["state"], b["state"]), ("acknowledged", "acknowledged"))
        self.assertEqual(self.b_calls, [{"op": "pair_ack", "ack": a["ack_sent"]}])
        self.assertEqual((item.dispatched, item.handled, item.refused), (1, 1, 0))
        # the exact signed response B minted is what A durably recorded (and B recorded A's ack)
        self.assertEqual(a["ack_recv"], b["ack_sent"])
        self.assertEqual(b["ack_recv"], a["ack_sent"])
        link = next(iter(self.A.service().journal.snapshot().links.values()))
        self.assertEqual(link["protocol_state"], "acknowledged")
        self.assertEqual(next(iter(self.b.journal.snapshot().links.values()))["protocol_state"], "acknowledged")

    def test_exact_retry_after_lost_reply_is_idempotent(self):
        p = self.ready()
        lost = []

        def handle(request):
            self.b_calls.append(request)
            reply = self.b.handle_pair_ack(request)
            if not lost:
                lost.append(reply)
                raise RuntimeError("reply lost")           # B effect is durable, A never learns of it
            return reply
        self.start_b(handle)
        status, data, _ = self.confirm(p)
        self.assertEqual((status, data), (202, {"pairing": data["pairing"], **PENDING}))
        self.assertEqual(data["pairing"]["state"], "confirmed")
        ack_first = json.dumps(self.a_session(p)["ack_sent"], sort_keys=True)
        self.assertEqual((self.a_session(p)["state"], self.b_session(p)["state"]), ("confirmed", "acknowledged"))
        status, data, _ = self.confirm(p)                  # exact same SAS
        self.assertEqual((status, data["pairing"]["state"]), (200, "acknowledged"), data)
        self.assertEqual(json.dumps(self.a_session(p)["ack_sent"], sort_keys=True), ack_first)
        self.assertEqual(len(self.b_calls), 2)
        self.assertEqual(self.b_calls[0], self.b_calls[1])  # identical ack bytes, never re-minted
        self.assertEqual(self.a_session(p)["ack_recv"], lost[0])
        # a third identical confirm is a pure durable no-op: no further send
        status, data, _ = self.confirm(p)
        self.assertEqual((status, data["pairing"]["state"], len(self.b_calls)), (200, "acknowledged", 2))

    def test_unavailable_b_keeps_a_durably_confirmed_then_retry_after_b_returns(self):
        p = self.ready()
        with self.connects() as spy:
            status, data, _ = self.confirm(p)             # nothing listens on B's configured port
        self.assertEqual(spy.call_count, 1)
        self.assertEqual((status, set(data), data["ack"], data["code"]), (202, {"pairing", "ack", "code"},
                                                                          "pending", "pairing_ack_pending"))
        self.assertEqual(data["pairing"]["state"], "confirmed")
        self.assertNotIn("acknowledged", json.dumps(data))
        text = json.dumps(data)
        for leak in (str(self.tmp), "127.0.0.1", str(self.b_port), "Traceback", "refused", "errno"):
            self.assertNotIn(leak, text)
        a = self.a_session(p)
        self.assertEqual((a["state"], self.b_session(p)["state"]), ("confirmed", "confirmed"))
        self.assertIsNone(a["ack_recv"])
        stored = json.dumps(a["ack_sent"], sort_keys=True)
        self.assertEqual(next(iter(self.A.service().journal.snapshot().links.values()))["protocol_state"], "confirmed")
        # a different SAS never resends and never changes anything
        wrong = "000000" if p.sas != "000000" else "000001"
        with self.connects() as spy:
            status, data, _ = self.confirm(p, wrong)
        self.assertEqual((status, data["code"], spy.call_count), (409, "pairing_unavailable", 0))
        self.assertEqual(json.dumps(self.a_session(p)["ack_sent"], sort_keys=True), stored)
        # B returns: exact same-SAS retry resends the IDENTICAL stored ack
        self.start_b()
        status, data, _ = self.confirm(p)
        self.assertEqual((status, data["pairing"]["state"]), (200, "acknowledged"), data)
        self.assertEqual(self.b_calls, [{"op": "pair_ack", "ack": json.loads(stored)}])
        self.assertEqual(json.dumps(self.a_session(p)["ack_sent"], sort_keys=True), stored)
        self.assertEqual(self.b_session(p)["state"], "acknowledged")

    def test_sas_mismatch_never_reaches_the_sender(self):
        p = self.ready()
        self.start_b()
        wrong = "000000" if p.sas != "000000" else "000001"
        with self.connects() as spy:
            status, data, _ = self.confirm(p, wrong)
        self.assertEqual((status, data), (409, {"error": "conflict", "code": "pairing_sas_mismatch"}))
        self.assertEqual((spy.call_count, self.b_calls, self.b_session(p)["state"]), (0, [], "confirmed"))

    def test_without_control_endpoints_confirm_is_pending_with_journaled_ack_then_retry_acknowledges(self):
        self.ENDPOINTS = None
        self.write_config()
        p = self.ready()
        self.start_b()
        with self.connects() as spy:
            status, data, _ = self.confirm(p)
        self.assertEqual((status, set(data), data["ack"], data["code"]),
                         (202, {"pairing", "ack", "code"}, "pending", "pairing_ack_pending"))
        self.assertEqual((data["pairing"]["state"], spy.call_count, self.b_calls), ("confirmed", 0, []))
        self.assertNotIn("acknowledged", json.dumps(data))
        a = self.a_session(p)
        self.assertEqual(a["state"], "confirmed")
        self.assertIsNone(a["ack_recv"])
        self.assertIsNotNone(a["ack_sent"])                  # exact ack journaled once, never lost
        stored = json.dumps(a["ack_sent"], sort_keys=True)
        with self.connects() as spy:                         # repeat without a sender: still pending, same bytes
            status, data, _ = self.confirm(p)
        self.assertEqual((status, data["code"], spy.call_count), (202, "pairing_ack_pending", 0))
        self.assertEqual(json.dumps(self.a_session(p)["ack_sent"], sort_keys=True), stored)
        wrong = "000000" if p.sas != "000000" else "000001"  # wrong SAS refuses before any send
        with self.connects() as spy:
            status, data, _ = self.confirm(p, wrong)
        self.assertEqual((status, data["code"], spy.call_count), (409, "pairing_unavailable", 0))
        self.ENDPOINTS = {PEER_B: endpoint(self.b_port)}     # owner adds the endpoint later
        self.write_config()
        status, data, _ = self.confirm(p)
        self.assertEqual((status, data["pairing"]["state"]), (200, "acknowledged"), data)
        self.assertEqual(set(data), {"pairing"})
        self.assertEqual(self.b_calls, [{"op": "pair_ack", "ack": json.loads(stored)}])
        self.assertEqual(json.dumps(self.a_session(p)["ack_sent"], sort_keys=True), stored)
        self.assertEqual((self.a_session(p)["state"], self.b_session(p)["state"]), ("acknowledged", "acknowledged"))
        status, data, _ = self.confirm(p)                    # idempotent: no further send
        self.assertEqual((status, data["pairing"]["state"], len(self.b_calls)), (200, "acknowledged", 1))

    def test_wrong_sas_without_sender_creates_no_ack(self):
        self.ENDPOINTS = None
        self.write_config()
        p = self.ready()
        wrong = "000000" if p.sas != "000000" else "000001"
        status, data, _ = self.confirm(p, wrong)
        self.assertEqual((status, data["code"]), (409, "pairing_sas_mismatch"))
        self.assertIsNone(self.a_session(p)["ack_sent"])

    def _failing(self, method, exc):
        from invite_rooms.pairing_service import PairingService
        return patch.object(PairingService, method, side_effect=exc)

    def test_local_ack_request_failures_keep_established_mapping_never_pending(self):
        from invite_rooms.pairing_journal import E_BUSY, E_CORRUPT
        from invite_rooms.pairing_service import PairingError
        for exc, expect in ((JournalError(E_CORRUPT), 404), (JournalError(E_BUSY), 503),
                            (PairingError("pairing_busy"), 503), (PairingError("pairing_unavailable"), 409)):
            p = self.ready()
            item = self.start_b()
            with self._failing("ack_request", exc), self.connects() as spy:
                status, data, _ = self.confirm(p)
            self.assertEqual((status, spy.call_count), (expect, 0), (exc, data))
            self.assertNotIn("pairing_ack_pending", json.dumps(data))
            self.assertNotIn("ack", data)
            self.assertEqual(self.b_calls, [])
            self.assertNotEqual(self.a_session(p)["state"], "acknowledged")
            item.close()
            self.listeners.remove(item)

    def test_local_complete_ack_failures_keep_established_mapping_never_pending(self):
        from invite_rooms.pairing_journal import E_BUSY, E_CORRUPT
        from invite_rooms.pairing_service import PairingError
        for exc, expect in ((JournalError(E_CORRUPT), 404), (JournalError(E_BUSY), 503),
                            (PairingError("pairing_unavailable"), 409)):
            p = self.ready()
            self.b_port = w2a.free_port()        # a fresh port per case (closed listeners leave the old one bound)
            self.write_config()
            item = self.start_b()
            with self._failing("complete_ack", exc):
                status, data, _ = self.confirm(p)
            self.assertEqual(status, expect, (exc, data))
            self.assertNotIn("pairing_ack_pending", json.dumps(data))
            self.assertNotIn("ack", data)
            self.assertEqual(self.a_session(p)["state"], "confirmed")     # never claims acknowledged
            self.assertIsNone(self.a_session(p)["ack_recv"])
            item.close()
            self.listeners.remove(item)

    def test_http_response_is_sanitized_for_every_pending_cause(self):
        p = self.ready()
        causes = []
        with patch("invite_rooms.routes.pair_ack_sender", return_value=lambda *a: (_ for _ in ()).throw(
                PairAckUnavailable())):
            status, data, _ = self.confirm(p)
        causes.append((status, data))
        for status, data in causes:
            self.assertEqual((status, set(data), data["code"]), (202, {"pairing", "ack", "code"}, "pairing_ack_pending"))
            self.assertNotIn("secret", json.dumps(data))
        self.assertEqual(self.a_session(p)["state"], "confirmed")

    def test_locking_and_admission_order_on_the_real_wire(self):
        p = self.ready()
        self.start_b()
        events, owner = [], []
        real_connect, real_admit, real_send = socket.create_connection, JournalLinkAuthority.admit, framing_module.SequencedTLS.send
        a_dir = self.A.journal_dir

        http_port = self.httpd.server_address[1]

        def connect(address, *a, **k):
            if address[1] == http_port:
                return real_connect(address, *a, **k)
            owner.append(threading.get_ident())
            events.append(("connect", self.lock_free()))
            return real_connect(address, *a, **k)

        def admit(auth, endpoint_id, generation, operation):
            if Path(auth.journal.directory) == a_dir:
                events.append(("admit", operation, self.lock_free()))
            return real_admit(auth, endpoint_id, generation, operation)

        def send(frames, *a, **k):
            if threading.get_ident() in owner:
                events.append(("frame", self.lock_free()))
            return real_send(frames, *a, **k)
        with patch.object(socket, "create_connection", connect), \
                patch.object(JournalLinkAuthority, "admit", admit), \
                patch.object(framing_module.SequencedTLS, "send", send):
            status, data, _ = self.confirm(p)
        self.assertEqual(status, 200, data)
        self.assertEqual(events[:3], [("connect", True), ("admit", "pair_ack", True), ("frame", True)], events)
        self.assertEqual(sum(1 for e in events if e[0] == "admit"), 1)
        self.assertTrue(all(e[-1] for e in events), events)           # zero journal locks held over socket/handler


# ===================================================================================== sender-level fail-closed
class SenderFailClosedTest(W2b0Base):
    def setUp(self):
        super().setUp()
        self.p = self.ready()
        self.item = self.start_b()
        self.service = self.A.service()
        self.service.confirm_offer(self.p.a_id, self.p.sas)          # durable A confirm WITHOUT the route/sender
        self.req_ = self.service.ack_request(self.p.a_id)            # A is host + confirmed: ack journaled once
        self.config = self.loaded_config()
        self.send = pair_ack_sender(self.config)

    def attempt(self, send=None, request="default", endpoint_id=None, generation=None):
        with self.connects() as spy:
            try:
                out = (send or self.send)(endpoint_id or self.req_.endpoint_id,
                                          self.req_.generation if generation is None else generation,
                                          self.req_.request if request == "default" else request)
            except (PairAckUnavailable, JournalError) as exc:
                out = exc
        return out, spy.call_count

    def untouched(self):
        self.assertEqual((self.b_calls, self.item.dispatched, self.b_session(self.p)["state"]), ([], 0, "confirmed"))
        self.assertEqual(self.a_session(self.p)["state"], "confirmed")

    def test_baseline_send_succeeds_through_service_contract(self):
        row = self.service.send_pair_ack(self.p.a_id, self.send)
        self.assertEqual(row["state"], "acknowledged")
        self.assertEqual(self.b_session(self.p)["state"], "acknowledged")

    def test_wrong_peer_id_in_owner_map_never_dials(self):
        self.ENDPOINTS = {PEER_C: endpoint(self.b_port)}
        self.write_config()
        out, calls = self.attempt(send=pair_ack_sender(self.loaded_config()))
        self.assertIsInstance(out, PairAckUnavailable)
        self.assertEqual(calls, 0)
        self.untouched()

    def test_wrong_endpoint_other_peers_listener_fails_tls_with_zero_effect(self):
        c = ps.Side(self.tmp, "c", ps.INSTALL_C, template=C_TEMPLATE)
        bundle_c = identity_module.load_identity(c.identity_dir, installation_id=ps.INSTALL_C, profile="default")
        c_calls = []
        c_port = w2a.free_port()
        c_listener = ControlListener(host="127.0.0.1", port=c_port, authority=JournalLinkAuthority(self.b.journal),
                                     identity=lambda: (str(bundle_c.leaf_cert_file), str(bundle_c.leaf_key_file)),
                                     pair_ack_handler=lambda r: c_calls.append(r), timeout=3.0)
        self.listeners.append(c_listener)
        c_listener.start()
        self.ENDPOINTS = {PEER_B: endpoint(c_port)}
        self.write_config()
        out, calls = self.attempt(send=pair_ack_sender(self.loaded_config()))
        self.assertIsInstance(out, PairAckUnavailable)
        self.assertEqual((calls, c_calls, c_listener.dispatched), (1, [], 0))
        self.untouched()

    def test_wrong_pin_with_correct_ca_is_refused_by_the_client(self):
        """B's listener presents a leaf signed by B's REAL CA but not the journal-pinned leaf."""
        cur = self.B.identity_dir / "current" / "tls"
        ca_key = serialization.load_pem_private_key((cur / "ca.key").read_bytes(), password=None)
        ca = x509.load_pem_x509_certificate((cur / "ca.pem").read_bytes())
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        leaf = identity_module._build(subject="mc-rooms-peer", public_key=key.public_key(), issuer=ca.subject,
                                      issuer_key=ca_key,
                                      issuer_ski=x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()),
                                      days=10, ca=False, now=dt.datetime.now(dt.timezone.utc),
                                      eku=[ExtendedKeyUsageOID.SERVER_AUTH, ExtendedKeyUsageOID.CLIENT_AUTH])
        rogue = ps.private_dir(self.tmp / "rogue")
        (rogue / "leaf.pem").write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
        (rogue / "leaf.key").write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                           serialization.NoEncryption()))
        self.item.close()                                 # free B's port for the rogue-leaf listener
        self.listeners.append(rogue_listener := ControlListener(
            host="127.0.0.1", port=self.b_port, authority=JournalLinkAuthority(self.b.journal),
            identity=lambda: (str(rogue / "leaf.pem"), str(rogue / "leaf.key")),
            pair_ack_handler=self.b.handle_pair_ack, timeout=3.0))
        rogue_listener.start()
        out, calls = self.attempt()
        self.assertIsInstance(out, PairAckUnavailable)
        self.assertEqual(calls, 1)
        self.assertEqual(rogue_listener.dispatched, 0)
        self.assertEqual(self.b_session(self.p)["state"], "confirmed")

    def test_busy_journal_refuses_before_any_socket(self):
        holder = Journal(self.A.journal_dir, anchor=self.A.journal_dir.parent)
        with holder._locked(exclusive=True):
            out, calls = self.attempt()
        self.assertIsInstance(out, JournalError)         # R02: typed local busy, not a peer outage
        self.assertEqual(out.code, "journal_busy")
        self.assertEqual(calls, 0)
        self.untouched()
        out, calls = self.attempt()                      # lock released: the very same call now works
        self.assertEqual((type(out), calls), (dict, 1))

    def test_unpaired_stale_and_wrong_generation_refuse_before_any_socket(self):
        for generation in (self.req_.generation + 1, self.req_.generation - 1, 0, "1", True):
            out, calls = self.attempt(generation=generation)
            self.assertIsInstance(out, PairAckUnavailable, generation)
            self.assertEqual(calls, 0)
        out, calls = self.attempt(endpoint_id="link-" + "0" * 32)
        self.assertEqual((type(out), calls), (PairAckUnavailable, 0))
        self.service.unpair(PEER_B)
        out, calls = self.attempt()
        self.assertEqual((type(out), calls), (PairAckUnavailable, 0))
        self.assertEqual((self.b_calls, self.item.dispatched, self.b_session(self.p)["state"]), ([], 0, "confirmed"))

    def test_repaired_link_refuses_the_old_generation_and_old_endpoint(self):
        clock = ps.Clock(time.time())
        a, b = self.A.service(clock), self.b.__class__ and self.B.service(clock)
        a.unpair(PEER_B)
        b.unpair("peer-" + "a" * 32)
        clock.advance(7 * 24 * 3600)
        p2 = ps.drive_pairing(a, b)
        out, calls = self.attempt()                       # request minted for the OLD link/generation
        self.assertEqual((type(out), calls), (PairAckUnavailable, 0))
        self.assertEqual(b.journal.snapshot().sessions[p2["b_id"]]["state"], "confirmed")

    def test_request_is_bound_to_the_journal_link_and_pair_ack_shape(self):
        for request in (dict(self.req_.request, op="status"), {"op": "pair_ack"}, {"op": "pair_ack", "ack": 1},
                        {"op": "pair_ack", "ack": dict(self.req_.request["ack"], endpoint_id="link-" + "9" * 32)},
                        {"op": "pair_ack", "ack": self.req_.request["ack"], "x": 1}, None, "x"):
            out, calls = self.attempt(request=request)
            self.assertIsInstance(out, PairAckUnavailable, request)
            self.assertEqual(calls, 0, request)
        self.untouched()


# ==================================================================================== connector operation matrix
class OperationMatrixTest(W2b0Base):
    def setUp(self):
        super().setUp()
        self.p = self.ready()
        self.item = self.start_b()
        self.A.service().confirm_offer(self.p.a_id, self.p.sas)
        self.req_ = self.A.service().ack_request(self.p.a_id)
        self.authority = JournalLinkAuthority(self.A.service().journal)
        trust = self.authority.peer_trust(self.req_.endpoint_id, self.req_.generation, role="host")
        self.binding = LinkBinding(endpoint_id=self.req_.endpoint_id, generation=self.req_.generation,
                                   address=("127.0.0.1", self.b_port), peer_ca_file=None, peer_ca_data=trust.ca_pem,
                                   peer_fingerprint=trust.fingerprint, expected_binding=trust.binding_dict(), timeout=3.0)
        ba = identity_module.load_identity(self.A.identity_dir, installation_id=ps.INSTALL_A, profile="default")
        self.identity = LinkIdentity(cert_file=str(ba.leaf_cert_file), key_file=str(ba.leaf_key_file))

    def connector(self):
        return make_pair_ack_connector(self.binding, self.identity, self.authority)

    OTHER_OPS = ({"op": "status", "lobby_id": LOBBY, "invite_id": INVITE},
                 {"op": "preview", "lobby_id": LOBBY, "invite_id": INVITE},
                 {"op": "redeem", "lobby_id": LOBBY, "invite_id": INVITE, "secret": "x"},
                 {"op": "consent", "lobby_id": LOBBY, "invite_id": INVITE, "receipt": "r", "signature": "s"},
                 {"op": "material_request", "lobby_id": LOBBY, "invite_id": INVITE},
                 {"op": "material_receipt", "lobby_id": LOBBY, "invite_id": INVITE, "receipt": "r"},
                 {"op": "room_log", "room_id": "r", "since_seq": 0, "limit": 1},
                 {"op": "member_left", "room_id": "r"}, {"op": "bogus"}, {})

    def test_confirmed_pair_ack_succeeds_and_returns_the_signed_reply(self):
        connector = self.connector()
        self.assertIsInstance(connector, PairAckConnector)
        response = connector.forward_pair_ack(self.req_.request)
        self.assertEqual(response.status, 200)
        reply = json.loads(response.body)
        self.assertEqual(reply, self.b_session(self.p)["ack_sent"])
        self.assertEqual(self.A.service().complete_ack(self.p.a_id, reply)["state"], "acknowledged")

    def test_confirmed_link_refuses_every_other_operation_before_any_socket(self):
        connector = self.connector()
        with self.connects() as spy:
            for body in self.OTHER_OPS:
                with self.assertRaises(FirewallDenied, msg=body):
                    connector.forward_pair_ack(body)
            for body in self.OTHER_OPS:               # the generic data entry point is closed on this connector
                with self.assertRaises(FirewallDenied, msg=body):
                    connector.forward(ForwardedRequest("POST", "/control/joining", (), json.dumps(body).encode()))
            with self.assertRaises(FirewallDenied):
                connector.forward(ForwardedRequest("POST", "/anything", (), b"{}"))
        self.assertEqual((spy.call_count, self.b_calls, self.item.dispatched), (0, [], 0))

    def test_plain_data_connector_still_refuses_a_confirmed_link(self):
        with self.connects() as spy:
            with self.assertRaises(FirewallDenied):
                make_link_connector(self.binding, self.identity, self.authority)
        self.assertEqual(spy.call_count, 0)

    def test_admit_label_is_fixed_to_pair_ack(self):
        connector = self.connector()
        for label in ("data_send", "status", "preview", "redeem", "material_request", "joining_consent", ""):
            with self.assertRaises(FirewallDenied, msg=label):
                connector.admission.admit(label)
        connector.admission.admit("pair_ack")             # confirmed + host + exact generation: admitted
        # the journal itself still refuses a data label on a confirmed link (not weakened)
        self.assertFalse(self.authority.admit(self.req_.endpoint_id, self.req_.generation, "data_send"))
        self.assertTrue(self.authority.admit(self.req_.endpoint_id, self.req_.generation, "pair_ack"))

    def test_joining_role_cannot_use_the_host_sender(self):
        b_authority = JournalLinkAuthority(self.b.journal)
        with self.assertRaises(FirewallDenied):
            make_pair_ack_connector(self.binding, self.identity, b_authority)
        self.assertIsNone(b_authority.peer_trust(self.req_.endpoint_id, self.req_.generation, role="host"))

    def test_wrong_pinned_material_is_refused_at_construction_or_first_check(self):
        other = ps.Side(self.tmp, "c", ps.INSTALL_C, template=C_TEMPLATE)
        bundle_c = identity_module.load_identity(other.identity_dir, installation_id=ps.INSTALL_C, profile="default")
        for field, value in (("peer_fingerprint", bundle_c.leaf_fingerprint), ("peer_ca_data", bundle_c.ca_pem)):
            kwargs = {**self.binding.__dict__, field: value}
            kwargs["expected_binding"] = dict(self.binding.expected_binding)
            if field == "peer_fingerprint":
                kwargs["expected_binding"]["fingerprint"] = value
            with self.assertRaises(FirewallDenied, msg=field):
                make_pair_ack_connector(LinkBinding(**kwargs), self.identity, self.authority)

    def test_every_admission_check_pins_role_state_generation_ca_leaf_and_binding(self):
        import dataclasses
        connector = self.connector()
        real = self.authority.peer_trust(self.req_.endpoint_id, self.req_.generation, role="host")
        other = ps.Side(self.tmp, "c", ps.INSTALL_C, template=C_TEMPLATE)
        bundle_c = identity_module.load_identity(other.identity_dir, installation_id=ps.INSTALL_C, profile="default")
        mutations = {"fingerprint": dict(fingerprint=bundle_c.leaf_fingerprint), "ca": dict(ca_pem=bundle_c.ca_pem),
                     "generation": dict(generation=real.generation + 1), "role": dict(role="joining"),
                     "state": dict(protocol_state="revoked"),
                     "binding": dict(binding=tuple(sorted({**real.binding_dict(), "catalog_digest": "0" * 64}.items())))}
        connector.admission.check()                                   # the real record passes
        for name, change in mutations.items():
            with patch.object(self.authority, "peer_trust", return_value=dataclasses.replace(real, **change)):
                with self.assertRaises(FirewallDenied, msg=name):
                    connector.admission.check()
                with self.assertRaises(FirewallDenied, msg=name):
                    connector.admission.admit("pair_ack")
        with patch.object(self.authority, "peer_trust", return_value=None):
            with self.assertRaises(FirewallDenied):
                connector.admission.check()

    def test_acknowledged_retransmission_is_exact_and_other_operations_keep_existing_rules(self):
        first = self.connector().forward_pair_ack(self.req_.request)
        self.A.service().complete_ack(self.p.a_id, json.loads(first.body))
        for label in ("data_send", "status", "preview", "joining_consent"):
            with self.assertRaises(FirewallDenied, msg=label):       # label stays fixed even when acknowledged
                self.connector().admission.admit(label)
        # acknowledged: pair_ack retransmission is answered by B's exact stored reply; a variant is refused by B
        again = self.connector().forward_pair_ack(self.A.service().ack_request(self.p.a_id).request)
        self.assertEqual((again.status, again.body), (200, first.body))
        tampered = json.loads(json.dumps(self.req_.request))
        tampered["ack"]["signature"] = tampered["ack"]["signature"][:-2] + ("AA" if tampered["ack"]["signature"][-2:] != "AA" else "BB")
        denied = self.connector().forward_pair_ack(tampered)
        self.assertNotEqual(denied.status, 200)
        # on an ACKNOWLEDGED link the existing data connector admission is unchanged (builds; admits data labels)
        make_link_connector(self.binding, self.identity, self.authority)
        self.assertTrue(self.authority.admit(self.req_.endpoint_id, self.req_.generation, "data_send"))
        for body in self.OTHER_OPS[:-2]:
            with self.assertRaises(FirewallDenied, msg=body):   # still never carried by the pair_ack connector
                self.connector().forward_pair_ack(body)


class RemoteReplyAndTrustClassificationTest(W2b0Base):
    """R02: unverifiable peer replies are pending (202); real local journal busy/corrupt at SENDER trust time keep
    the established 503/404 mappings with zero dials."""

    def _invalid_reply(self, kind):
        p = self.ready()

        def remote(request):
            self.b_calls.append(request)
            if kind == "shape":
                return {"not": "an ack"}
            reply = self.b.handle_pair_ack(request)          # B durably acknowledges...
            return {**reply, "signature": "AA"}              # ...but the reply is altered on the way back
        self.start_b(remote)
        status, data, _ = self.confirm(p)
        self.assertEqual(status, 202, data)
        self.assertEqual(set(data), {"pairing", "ack", "code"})
        self.assertEqual({k: data[k] for k in PENDING}, PENDING)
        self.assertEqual(data["pairing"]["state"], "confirmed")
        self.assertNotIn("AA", json.dumps(data))
        a = self.a_session(p)
        self.assertEqual(a["state"], "confirmed")
        self.assertIsNotNone(a["ack_sent"])
        self.assertIsNone(a["ack_recv"])
        self.assertEqual(self.b_calls, [{"op": "pair_ack", "ack": a["ack_sent"]}])
        return p, a

    def test_decoded_wrong_shape_remote_reply_is_pending_not_409(self):
        self._invalid_reply("shape")

    def test_altered_signature_remote_reply_is_pending_and_exact_retry_converges(self):
        p, a = self._invalid_reply("signature")
        self.assertEqual(self.b_session(p)["state"], "acknowledged")
        self.listeners[-1].close()
        self.listeners.pop()
        self.b_port = w2a.free_port()                        # fresh port (closed listeners leave the old one bound)
        self.write_config()
        self.start_b()                                       # valid replies again
        status, data, _ = self.confirm(p)
        self.assertEqual((status, data["pairing"]["state"]), (200, "acknowledged"), data)
        self.assertEqual(self.a_session(p)["ack_sent"], a["ack_sent"])
        self.assertEqual(self.a_session(p)["ack_recv"], self.b_session(p)["ack_sent"])
        self.assertEqual(self.b_calls[-1], {"op": "pair_ack", "ack": a["ack_sent"]})

    def _after_real_ack_request(self, fault):
        from invite_rooms.pairing_service import PairingService
        original = PairingService.ack_request
        a_dir = Path(self.A.journal_dir)
        saved = {}

        def prepare(service, session_id):
            req = original(service, session_id)
            if Path(service.journal.directory) == a_dir and "done" not in saved:
                saved["done"] = True
                saved["ack"] = req.request["ack"]
                fault(a_dir, saved)
            return req
        return patch.object(PairingService, "ack_request", prepare), saved

    def test_real_journal_corruption_at_sender_trust_is_404_with_no_dial(self):
        from invite_rooms import pairing_journal as pj
        p = self.ready()
        self.start_b()

        def corrupt(a_dir, saved):
            file = a_dir / pj.JOURNAL_FILE
            saved["before"] = file.read_bytes()
            file.write_bytes(saved["before"] + b"INVALID_JOURNAL_TRAILER")
        patcher, saved = self._after_real_ack_request(corrupt)
        with patcher, self.connects() as spy:
            status, data, _ = self.confirm(p)
        self.assertEqual(spy.call_count, 0)
        self.assertEqual(self.b_calls, [])
        self.assertEqual((status, data), (404, {"error": "not_found",
                                                "path": "/api/local/room-invites/pairing/offers/" + p.a_id + "/confirm"}))
        self.assertNotIn("INVALID_JOURNAL_TRAILER", json.dumps(data))
        # the ack was durably prepared before the corruption; the file is deliberately NOT repaired
        raw = (Path(self.A.journal_dir) / pj.JOURNAL_FILE).read_bytes()
        self.assertEqual(raw, saved["before"] + b"INVALID_JOURNAL_TRAILER")
        self.assertIn(saved["ack"]["signature"].encode(), raw)

    def test_real_journal_lock_held_by_another_process_at_sender_trust_is_503_with_no_dial(self):
        import subprocess
        from invite_rooms import pairing_journal as pj
        p = self.ready()
        self.start_b()
        children = []
        code = ("import fcntl,os,sys\n"
                "fd=os.open(sys.argv[1],os.O_RDWR)\n"
                "fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)\n"
                "sys.stdout.write('locked\\n');sys.stdout.flush()\n"
                "sys.stdin.readline()\n")

        def hold(a_dir, saved):
            child = subprocess.Popen([sys.executable, "-B", "-c", code, str(a_dir / pj.LOCK_FILE)],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
            children.append(child)
            self.assertEqual(child.stdout.readline().strip(), "locked")
        patcher, saved = self._after_real_ack_request(hold)
        try:
            with patcher, self.connects() as spy:
                status, data, _ = self.confirm(p)
            self.assertEqual(spy.call_count, 0)
            self.assertEqual(self.b_calls, [])
            self.assertEqual((status, data), (503, {"error": "unavailable", "code": "pairing_busy"}))
        finally:
            for child in children:
                try:
                    child.stdin.write("\n")
                    child.stdin.flush()
                except Exception:
                    child.kill()
                child.wait(timeout=10)
                child.stdin.close()
                child.stdout.close()
        self.assertTrue(children)
        self.assertTrue(all(c.poll() is not None for c in children))
        self.assertEqual(self.a_session(p)["state"], "confirmed")
        self.assertEqual(self.a_session(p)["ack_sent"], saved["ack"])
        self.assertIsNone(self.a_session(p)["ack_recv"])


class NoResidualThreadsTest(W2b0Base):
    def test_no_threads_left_after_route_send(self):
        p = self.ready()
        item = self.start_b()
        self.assertEqual(self.confirm(p)[0], 200)
        item.close()
        item.join(5)
        time.sleep(.1)
        leftovers = [t.name for t in threading.enumerate() if t not in self.threads_before
                     and (t.name.startswith("room-control") or t.name.startswith("invite-control"))]
        self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
