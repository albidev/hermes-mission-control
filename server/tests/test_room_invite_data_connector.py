"""Real, ephemeral loopback data-plane tests. No Hermes gateway or live config."""
from __future__ import annotations

import concurrent.futures
import contextlib
import importlib
import importlib.util
import io
import json
import logging
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from invite_rooms.control.dispatcher import LinkState
from room_lobby_service import _validate_adapter
from invite_rooms.peer_registry import MaterialScope, adapter_id_for

try:  # test-only lab PKI; moved out of the product package on purpose
    from server.tests import lab_pki
except ModuleNotFoundError:
    import lab_pki

SCOPE = {"room_id": "r", "home_install_id": "install:" + "a" * 32, "authority_gateway_id": "g",
         "authority_epoch": 1, "member_id": "m", "target_install_id": "install:" + "b" * 32,
         "target_profile": "default"}
AUTH = "HermesRoom " + "test-only-secret-" * 3
CAPABILITIES = "/v1/room-members/capabilities"


class Authority:
    """Atomic admission fixture, the journal implementation plugs in at this seam."""
    def __init__(self, binding):
        self.lock = threading.RLock()
        self.state = LinkState(binding, 1, "acknowledged")
        self.admissions = []

    def current(self, endpoint):
        with self.lock:
            return self.state if self.state.binding["endpoint_id"] == endpoint else None

    def admit(self, endpoint, generation, operation):
        with self.lock:
            state = self.current(endpoint)
            if state is None or state.generation != generation or state.protocol_state != "acknowledged":
                return False
            self.admissions.append(operation)
            return True

    def unpair(self):
        with self.lock:
            self.state = LinkState(self.state.binding, 2, "unpaired")


class Native:
    def __init__(self):
        self.calls = []
        self.status = 202
        self.body = b'{"status":"ok"}'
        self.redirect = None
        self.before_reply = lambda: None
        native = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.respond()

            def do_POST(self):
                self.respond()

            def respond(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                native.calls.append((self.command, self.path, dict(self.headers), body))
                native.before_reply()
                self.send_response(native.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(native.body)))
                if native.redirect:
                    self.send_header("Location", native.redirect)
                self.end_headers()
                try:
                    self.wfile.write(native.body)
                except OSError:
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:%s" % self.server.server_port

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)


class DataConnectorTests(unittest.TestCase):
    def product(self):
        # Deliberate executable capability assertion on the pre-port baseline,
        # not an import or setUp error. Every behavior invokes the real API below.
        name = "invite_rooms.control.connector"
        self.assertIsNotNone(importlib.util.find_spec(name), "product mTLS data connector is missing")
        return importlib.import_module(name)

    @contextlib.contextmanager
    def link(self, *, max_inflight=4, timeout=2.0):
        c = self.product()
        pki = lab_pki
        adapter_module = importlib.import_module("invite_rooms.control.adapter")
        with tempfile.TemporaryDirectory(prefix="data-connector-") as root:
            a = pki.init_owner(Path(root) / "a", "a")
            b = pki.init_owner(Path(root) / "b", "b")
            a_peer = {"endpoint_id": "ep-x", "fingerprint": b["fingerprint"]}
            b_peer = {"endpoint_id": "ep-x", "fingerprint": a["fingerprint"]}
            host_authority, target_authority = Authority(a_peer), Authority(b_peer)
            native = Native()
            identity_a = c.LinkIdentity(a["cert_file"], a["key_file"])
            identity_b = c.LinkIdentity(b["cert_file"], b["key_file"])
            target_binding = c.LinkBinding("ep-x", 1, ("127.0.0.1", 1), a["ca_file"],
                                           a["fingerprint"], b_peer, timeout, max_inflight)
            target = c.TargetConnector(binding=target_binding, identity=identity_b,
                authority=target_authority, expected_scope=SCOPE, verify_native=lambda *_: True,
                target_base=native.url)
            target.start()
            binding = c.LinkBinding("ep-x", 1, target.address, b["ca_file"],
                                    b["fingerprint"], a_peer, timeout, max_inflight)
            adapter = adapter_module.LoopbackAdapter(authority=host_authority.current)
            adapter.start()
            try:
                url = adapter.register_link("ep-x", 1, expected_scope=SCOPE,
                    verify_native=lambda *_: True, binding=binding, identity=identity_a,
                    authority=host_authority, material_scope=self.material_scope(binding))
                yield c, native, target, adapter, url, binding, identity_a, host_authority, target_authority
            finally:
                adapter.close()
                target.close()
                native.close()

    def call(self, url, path=CAPABILITIES, *, body=None):
        headers = {"Authorization": AUTH, "Content-Type": "application/json"}
        request = urllib.request.Request(url + path, data=body, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as response:
            with response:
                return response.code, dict(response.headers), response.read()

    def material_scope(self, binding, *, lobby_id="l", invite_id="i"):
        return MaterialScope(owner_installation_id=SCOPE["home_install_id"], owner_profile="default",
            lobby_id=lobby_id, planned_room_id=SCOPE["room_id"], member_id=SCOPE["member_id"],
            invite_id=invite_id, peer_id="peer", endpoint_id=binding.endpoint_id,
            fingerprint=binding.peer_fingerprint, target_install_id=SCOPE["target_install_id"],
            target_profile=SCOPE["target_profile"], catalog_digest="c" * 64, policy_digest="d" * 64,
            pairing_generation=binding.generation)

    def test_adapter_mtls_native_preserves_body_headers_status_and_redacts_auth(self):
        output = io.StringIO()
        handler = logging.StreamHandler(output)
        logging.getLogger().addHandler(handler)
        try:
            with contextlib.redirect_stderr(output), self.link() as data:
                _, native, _, _, url, *_ = data
                native.body = b'{"object":"room.grant.revoked","revoked":true}'
                status, headers, body = self.call(url, "/v1/room-members/grants/revoke", body=b"{}")
                self.assertEqual((status, body), (202, native.body))
                self.assertEqual(headers["content-type"], "application/json")
                self.assertEqual(len(native.calls), 1)
                method, path, received, sent = native.calls[0]
                self.assertEqual((method, path, sent), ("POST", "/v1/room-members/grants/revoke", b"{}"))
                self.assertEqual(received["Authorization"], AUTH)
        finally:
            logging.getLogger().removeHandler(handler)
        self.assertNotIn("HermesRoom", output.getvalue())
        self.assertNotIn(AUTH, output.getvalue())

    def test_wrong_b_leaf_refused_without_native_call(self):
        with self.link() as (c, native, _, _, _, binding, identity, authority, _):
            wrong = replace(binding, peer_fingerprint="0" * 64,
                            expected_binding={"endpoint_id": "ep-x", "fingerprint": "0" * 64})
            authority.state = LinkState(wrong.expected_binding, 1, "acknowledged")
            hop = c.make_link_connector(wrong, identity, authority)
            with self.assertRaises(c.UpstreamAmbiguous):
                hop(self.forwarded(c))
            self.assertEqual(native.calls, [])

    def test_wrong_ca_refused_without_native_call(self):
        with self.link() as (c, native, _, _, _, binding, identity, authority, _):
            pki = lab_pki
            with tempfile.TemporaryDirectory() as root:
                other = pki.init_owner(Path(root), "unrelated")
                hop = c.make_link_connector(replace(binding, peer_ca_file=other["ca_file"]), identity, authority)
                with self.assertRaises(c.UpstreamAmbiguous):
                    hop(self.forwarded(c))
            self.assertEqual(native.calls, [])

    def forwarded(self, c, path=CAPABILITIES, body=b""):
        return c.ForwardedRequest("GET", path, (("authorization", AUTH),), body)

    def test_generation_change_after_adapter_admission_before_connect_refused(self):
        with self.link() as (c, native, target, _, url, _, _, authority, _):
            original = c.HostConnector.forward
            def cutover(host, request, admit=None):
                authority.unpair()
                return original(host, request, admit)
            with patch.object(c.HostConnector, "forward", cutover):
                status, _, _ = self.call(url)
            self.assertIn(status, (403, 502))
            self.assertEqual(target.accepted, 0)
            self.assertEqual(native.calls, [])

    def test_generation_change_after_handshake_before_send_refused(self):
        with self.link() as (c, native, _, _, url, _, _, authority, _):
            original = c.wrap_client
            def cutover(*args, **kwargs):
                stream = original(*args, **kwargs)
                authority.unpair()
                return stream
            with patch.object(c, "wrap_client", cutover):
                self.assertIn(self.call(url)[0], (403, 502))
            self.assertEqual(native.calls, [])

    def test_generation_change_during_response_is_generic_and_never_retried(self):
        with self.link() as (_, native, target, _, url, _, _, authority, _):
            native.before_reply = authority.unpair
            status, _, body = self.call(url)
            self.assertEqual(status, 502)
            self.assertIsNone(json.loads(body)["error"]["code"])
            self.assertEqual((len(native.calls), target.accepted), (1, 1))

    def test_target_firewall_denies_path_even_when_host_sends_it(self):
        with self.link() as (c, native, _, _, _, binding, identity, authority, _):
            response = c.make_link_connector(binding, identity, authority)(self.forwarded(c, "/v1/models"))
            self.assertEqual(response.status, 403)
            self.assertEqual(native.calls, [])

    def test_native_redirect_never_follows_credentials(self):
        with self.link() as (_, native, _, _, url, *_):
            destination = Native()
            try:
                native.status = 307
                native.redirect = destination.url + "/stolen"
                self.assertEqual(self.call(url)[0], 502)
                self.assertEqual(len(native.calls), 1)
                self.assertEqual(destination.calls, [])
            finally:
                destination.close()

    def test_native_timeout_is_generic_ambiguous_no_retry(self):
        with self.link(timeout=.15) as (_, native, target, _, url, *_):
            native.before_reply = lambda: time.sleep(.3)
            status, _, body = self.call(url)
            self.assertIn(status, (502, 504))
            self.assertIsNone(json.loads(body)["error"]["code"])
            self.assertEqual((len(native.calls), target.accepted), (1, 1))

    def test_oversize_body_refused_before_target(self):
        with self.link() as (c, native, target, adapter, url, binding, identity, authority, _):
            adapter.max_body_bytes = 8
            self.assertEqual(self.call(url, body=b"x" * 9)[0], 403)
            with self.assertRaises(c.FirewallDenied):
                c.make_link_connector(binding, identity, authority)(self.forwarded(c, body=b"x" * c.MAX_MESSAGE))
            self.assertEqual((native.calls, target.accepted), ([], 0))

    def test_concurrent_same_link_is_bounded_without_deadlock(self):
        with self.link(max_inflight=2) as (c, native, target, _, _, binding, identity, authority, _):
            entered, release = threading.Event(), threading.Event()
            native.before_reply = lambda: (entered.set(), release.wait(2))
            hop = c.make_link_connector(binding, identity, authority)
            def invoke():
                try:
                    return hop(self.forwarded(c)).status
                except c.UpstreamAmbiguous:
                    return 503
            with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
                first = pool.submit(invoke)
                self.assertTrue(entered.wait(1))
                others = [pool.submit(invoke) for _ in range(11)]
                time.sleep(.05)
                release.set()
                statuses = [first.result(4)] + [f.result(4) for f in others]
            self.assertIn(202, statuses)
            self.assertTrue(all(status in (202, 503) for status in statuses))
            self.assertLessEqual(target.peak_active, 2)
            self.assertEqual(hop.active, 0)

    def test_link_x_cannot_reach_link_y_native(self):
        with self.link() as (c, native_x, _, _, _, binding_x, identity_x, authority_x, _):
            with self.link() as (_, native_y, target_y, _, _, _, _, _, _):
                hop = c.make_link_connector(replace(binding_x, address=target_y.address), identity_x, authority_x)
                with self.assertRaises(c.UpstreamAmbiguous):
                    hop(self.forwarded(c))
                self.assertEqual(native_x.calls, [])
                self.assertEqual(native_y.calls, [])

    def test_same_pinned_identities_cannot_cross_endpoint_routes(self):
        with self.link() as (c, native_x, target_x, _, _, binding, identity, authority_x, _):
            native_y = Native()
            peer_y = {"endpoint_id": "ep-y", "fingerprint": target_x.peer_fingerprint}
            authority_y = Authority(peer_y)
            # Same TLS identities/CA: endpoint isolation must be application-bound,
            # not an accidental consequence of different certificate chains.
            # Build a fresh target using exactly the same certificate files.
            target_binding = replace(target_x.admission.binding, endpoint_id="ep-y", expected_binding=peer_y)
            certificate_root = Path(identity.cert_file).parent.parent
            identity_y = c.LinkIdentity(str(certificate_root / "b" / "leaf.pem"),
                                        str(certificate_root / "b" / "leaf.key"))
            target_y = c.TargetConnector(binding=target_binding, identity=identity_y,
                authority=authority_y, expected_scope=SCOPE, verify_native=lambda *_: True,
                target_base=native_y.url)
            target_y.start()
            try:
                hop = c.make_link_connector(replace(binding, address=target_y.address), identity, authority_x)
                with self.assertRaises(c.UpstreamAmbiguous):
                    hop(self.forwarded(c))
                self.assertEqual(native_x.calls, [])
                self.assertEqual(native_y.calls, [])
            finally:
                target_y.close()
                native_y.close()

    def test_scoped_routes_do_not_overwrite_and_cutover_removes_all(self):
        with self.link() as (_, native, _, adapter, first, binding, identity, authority, _):
            scope = self.material_scope(binding, lobby_id="second-lobby", invite_id="second-invite")
            second = adapter.register_link("ep-x", 1, expected_scope=SCOPE,
                verify_native=lambda *_: True, binding=binding, identity=identity,
                authority=authority, material_scope=scope)
            self.assertNotEqual(first, second)
            self.assertTrue(second.endswith("/links/" + adapter_id_for(scope)))
            self.assertEqual(self.call(first)[0], 202)
            self.assertEqual(self.call(second)[0], 202)
            self.assertEqual(len(native.calls), 2)
            with self.assertRaises(ValueError):
                adapter.register_link("ep-x", 1, expected_scope=SCOPE,
                    verify_native=lambda *_: True, binding=binding, identity=identity,
                    authority=authority, material_scope=scope)
            adapter.observe_generation("ep-x", 2)
            self.assertEqual(self.call(first)[0], 404)
            self.assertEqual(self.call(second)[0], 404)
            self.assertEqual(len(native.calls), 2)

    def test_scope_route_id_is_stable_after_adapter_restart(self):
        with self.link() as (_, _, _, adapter, first, binding, identity, authority, _):
            old_id = first.rsplit("/", 1)[-1]
            adapter.close()
            adapter.start()
            restored = adapter.register_link("ep-x", 1, expected_scope=SCOPE,
                verify_native=lambda *_: True, binding=binding, identity=identity,
                authority=authority, material_scope=self.material_scope(binding))
            self.assertEqual(restored.rsplit("/", 1)[-1], old_id)
            self.assertEqual(self.call(restored)[0], 202)

    def test_target_generation_cutover_before_native_effect_refused(self):
        with self.link() as (_, native, _, _, url, _, _, _, target_authority):
            original = target_authority.admit
            def deny(endpoint, generation, operation):
                target_authority.unpair()
                return original(endpoint, generation, operation)
            with patch.object(target_authority, "admit", deny):
                self.assertEqual(self.call(url)[0], 502)
            self.assertEqual(native.calls, [])

    def test_real_run_dispatch_crosses_both_admission_boundaries_once(self):
        with self.link() as (_, native, target, _, url, _, _, host_authority, target_authority):
            native.body = b'{"run_id":"run_one","status":"running","replayed":false}'
            dispatch = dict(SCOPE, protocol_version=1, task_id="task", execution_generation=1,
                source_event_seq=1, cancellation_scope_id="cancel", prompt="hello", prompt_digest="a" * 64,
                capability_digest="b" * 64, execution_policy_digest="c" * 64, trace_id="trace")
            body = json.dumps({"input":"hello", "hosted_room_dispatch":dispatch}).encode()
            request = urllib.request.Request(url + "/v1/runs", data=body,
                headers={"Authorization":AUTH, "Content-Type":"application/json", "Idempotency-Key":"room:task:1"})
            with urllib.request.urlopen(request, timeout=4) as response:
                self.assertEqual(response.status, 202)
                self.assertEqual(response.read(), native.body)
            self.assertEqual(len(native.calls), 1)
            self.assertEqual(native.calls[0][3], body)
            self.assertEqual(native.calls[0][2]["Idempotency-Key"], "room:task:1")
            self.assertEqual(host_authority.admissions, ["data_send"])
            self.assertEqual(target_authority.admissions, ["data_native"])
            self.assertEqual(target.accepted, 1)

    def test_factory_requires_atomic_authority_and_acknowledged_binding(self):
        with self.link() as (c, _, _, _, _, binding, identity, authority, _):
            with self.assertRaises(ValueError):
                c.make_link_connector(binding, identity, object())
            authority.state = LinkState(binding.expected_binding, 1, "confirmed")
            with self.assertRaises(c.FirewallDenied):
                c.make_link_connector(binding, identity, authority)

    def test_adapter_url_passes_native_validator(self):
        with self.link() as (_, _, _, _, url, *_):
            _validate_adapter(url)
            self.assertTrue(url.startswith("http://127.0.0.1:"))

    def test_adapter_and_connector_never_bind_wildcard(self):
        c = self.product()
        adapter_module = importlib.import_module("invite_rooms.control.adapter")
        for address in ("0.0.0.0", "::", "192.0.2.1"):
            with self.subTest(address=address):
                with self.assertRaises(ValueError):
                    adapter_module.LoopbackAdapter().start(host=address)
                with self.link() as (_, _, target, _, _, binding, identity, authority, _):
                    with self.assertRaises(ValueError):
                        target.start(host=address)
                    with self.assertRaises(ValueError):
                        c.make_link_connector(replace(binding, address=(address, 1234)), identity, authority)


if __name__ == "__main__":
    unittest.main()
