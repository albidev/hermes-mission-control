"""W2d Card2-A2b2b: inert target dispatch backend injection.

Witnesses for the connector seam (``native_backend_factory``), the unique-import
selector and the composite backend. Everything is in-process: no listener is
started (``TargetConnector.start`` is never called), no Core/gateway is
contacted, and the receipt store / material source / HTTP client are scratch
fakes unless a test explicitly drives the real ``TargetHttpClient`` source lane
with the network write patched out.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import socket
import tempfile
import threading
import time
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

from invite_rooms import target_http as target_http_module
from invite_rooms import target_http_io
from invite_rooms.control import connector as connector_module
from invite_rooms.control.connector import LinkBinding, LinkIdentity, TargetConnector
from invite_rooms.control.dispatcher import LinkState
from invite_rooms.control.firewall import ForwardedRequest, Request, UpstreamResponse
from invite_rooms.joining_store import JoiningStore, JoiningStoreError
from invite_rooms.peer_registry import MaterialScope, shutdown_admission_executor
from invite_rooms.runtime_config import TargetHttpConfig
from invite_rooms.target_http import TargetHttpClient, TargetHttpError

try:
    from server.tests import lab_pki
except ModuleNotFoundError:
    import lab_pki

HOME = "install:" + "1" * 32
TARGET = "install:" + "2" * 32
ROOM = "room-1"
MEMBER = "member-1"
BASE_URL = "http://127.0.0.1:19001"
EXPECTED = {"room_id": ROOM, "home_install_id": HOME, "authority_gateway_id": "gw", "authority_epoch": 1,
            "member_id": MEMBER, "target_install_id": TARGET, "target_profile": "default"}
FINGERPRINT = "a" * 64
RUN_ID = "run_" + "a" * 32
DENIED_BODY = b'{"error":"room request denied"}'
HOST_GRANT = "HermesRoom host-forwarded-grant"


def _tdb():
    import invite_rooms.target_dispatch_backend as module
    return module


def _binding():
    return LinkBinding("ep", 1, ("127.0.0.1", 1), None, FINGERPRINT,
                       {"endpoint_id": "ep", "fingerprint": FINGERPRINT}, peer_ca_data="CA")


def seed(store, *, n, host_peer="ep:1", room=ROOM, member=MEMBER, host_id=HOME, target=TARGET,
         profile="default", binding_endpoint="ep", state="consent_given"):
    lobby, invite = f"lobby-{n}", f"invite-{n}"
    import_id = store.create_import(lobby_id=lobby, invite_id=invite, secret="A" * 43, host_peer_id=host_peer)
    if state != "previewed":
        store.mark_redeemed(import_id)
    store.confirm_scope(import_id, {
        "host_id": host_id, "room_id": room, "lobby_id": lobby, "member_id": member,
        "binding": {"endpoint_id": binding_endpoint, "fingerprint": FINGERPRINT, "target_install_id": target,
                    "target_profile": profile, "catalog_digest": "c" * 64, "policy_digest": "d" * 64}})
    if state in ("consent_pending_delivery", "consent_given", "left"):
        store.begin_consent(import_id, receipt={"n": n}, signature="sig")
    if state in ("consent_given", "left"):
        store.mark_consent_delivered(import_id)
    if state == "left":
        store.mark_left(import_id)
    return import_id, lobby, invite


def dispatch_raw(task="task-1", gen=1, scope=None):
    prompt = "caff\u00e8 e\u0301 \U0001f9ea"
    dispatch = {"protocol_version": 2, **(scope or EXPECTED), "task_id": task, "execution_generation": gen,
                "source_event_seq": 1, "cancellation_scope_id": "cancel-" + task, "prompt": prompt,
                "prompt_digest": hashlib.sha256(prompt.encode()).hexdigest(), "capability_digest": "c" * 64,
                "execution_policy_digest": "d" * 64, "trace_id": "trace-" + task}
    # Unusual key order and JSON whitespace; must reach the client byte-for-byte.
    return json.dumps({"hosted_room_dispatch": dispatch, "input": prompt}, ensure_ascii=False,
                      separators=(",\n\t ", " :\t")).encode("utf-8")


def _headers(key=None, auth=HOST_GRANT):
    headers = [("authorization", auth), ("accept", "application/json"), ("content-type", "application/json")]
    if key is not None:
        headers.append(("idempotency-key", key))
    return tuple(headers)


def dispatch_forwarded(task="task-1", gen=1, scope=None):
    return ForwardedRequest("POST", "/v1/runs", _headers(f"room:{task}:{gen}"), dispatch_raw(task, gen, scope))


class _Authority:
    def __init__(self, binding, generation=1):
        self.lock = threading.RLock()
        self.state = LinkState(binding, generation, "acknowledged")
        self.admitted = True
        self.operations = []

    def current(self, endpoint):
        with self.lock:
            return self.state if endpoint == "ep" else None

    def admit(self, endpoint, generation, operation):
        self.operations.append(operation)
        return self.admitted and endpoint == "ep" and self.state.generation == generation


class Harness:
    """Scratch joining store plus recorded fakes patched into the new module."""

    def __init__(self, test, *, real_client=False):
        self.test = test
        tmp = tempfile.TemporaryDirectory(prefix="w2d-a2b2b-")
        test.addCleanup(tmp.cleanup)
        self.store = JoiningStore(Path(tmp.name) / "joining")
        test.addCleanup(self.store.close)
        self.source_ctor, self.source_calls = [], []
        self.client_ctor, self.client_calls = [], []
        self.passthrough_ctor, self.passthrough_calls = [], []
        self.resolve_exc = self.admit_exc = self.admit_effect = None
        self.admit_result = {"run_id": RUN_ID, "status": "started", "replayed": False}
        self.resolved = object()
        self.source_instance = None
        self.load_result = None
        self.deps = (object(), lambda **kw: None, lambda **kw: None, TargetHttpConfig(BASE_URL, "default"), object())
        h = self

        class Source:
            def __init__(self, **kw):
                h.source_ctor.append(kw)
                h.source_instance = self

            def resolve(self, *, lobby_id, invite_id, deadline):
                h.source_calls.append({"lobby_id": lobby_id, "invite_id": invite_id, "deadline": deadline})
                if h.resolve_exc is not None:
                    raise h.resolve_exc
                return h.resolved

            def load(self, scope, *, credential_ref, deadline):
                return dict(h.load_result)

        class Client:
            def __init__(self, config, **kw):
                h.client_ctor.append((config, kw))

            def admit_run(self, **kw):
                h.client_calls.append(kw)
                if h.admit_effect is not None:
                    h.admit_effect()
                if h.admit_exc is not None:
                    raise h.admit_exc
                return dict(h.admit_result)

        def passthrough_factory(base, *, timeout):
            h.passthrough_ctor.append((base, timeout))

            def forward(request, *, deadline=None):
                h.passthrough_calls.append((request, deadline))
                return UpstreamResponse(200, (("content-type", "application/json"),), b'{"ok":true}')
            return forward

        self.Source = Source
        module = _tdb()
        patches = [mock.patch.object(module, "TargetMaterialSource", Source),
                   mock.patch.object(module, "native_http_backend", passthrough_factory)]
        if not real_client:
            patches.append(mock.patch.object(module, "TargetHttpClient", Client))
        for patch in patches:
            patch.start()
            test.addCleanup(patch.stop)

    def make_factory(self):
        return _tdb().make_target_dispatch_backend_factory(self.store, *self.deps)

    def build(self):
        self.factory = self.make_factory()
        self.native = self.factory(_binding(), dict(EXPECTED))
        return self.native

    def assert_denied(self, response):
        t = self.test
        t.assertEqual(response.status, 403)
        t.assertEqual(response.body, DENIED_BODY)
        t.assertEqual(self.source_calls, [])
        t.assertEqual(self.client_calls, [])


class SelectorAndDispatchTests(unittest.TestCase):
    def call(self, h, request=None, deadline=None):
        return h.native(request or dispatch_forwarded(),
                        deadline=time.monotonic() + 5 if deadline is None else deadline)

    def test_exactly_one_current_consented_import_is_selected_and_resolved(self):
        h = Harness(self)
        _, lobby, invite = seed(h.store, n=1)
        for n, kw in enumerate((dict(host_peer="ep:2"), dict(host_peer="other:1"), dict(room="room-x"),
                                dict(member="member-x"), dict(host_id="install:" + "3" * 32),
                                dict(target="install:" + "4" * 32), dict(profile="other"),
                                dict(binding_endpoint="zz"), dict(state="redeemed"),
                                dict(state="consent_pending_delivery"), dict(state="left")), start=2):
            seed(h.store, n=n, **kw)
        h.build()
        deadline = time.monotonic() + 5
        request = dispatch_forwarded()
        with mock.patch.object(h.store, "list_imports", wraps=h.store.list_imports) as listing, \
                mock.patch.object(h.store, "confirmed_scope", wraps=h.store.confirmed_scope) as confirmed:
            response = h.native(request, deadline=deadline)
        self.assertEqual(response.status, 202)
        self.assertEqual(len(h.source_calls), 1)
        call = h.source_calls[0]
        self.assertEqual((call["lobby_id"], call["invite_id"]), (lobby, invite))
        self.assertIs(call["deadline"], deadline)
        listing.assert_called_once()
        self.assertEqual(listing.call_args.args, ())
        self.assertIs(listing.call_args.kwargs["deadline"], deadline)
        for item in confirmed.call_args_list:
            self.assertIs(item.kwargs["deadline"], deadline)
        client = h.client_calls[0]
        self.assertEqual(set(client), {"body", "idempotency_key", "admission_store", "forwarded_body",
                                       "source", "resolved", "deadline"})
        self.assertIs(client["forwarded_body"], request.body)
        self.assertEqual(client["body"], json.loads(request.body))
        self.assertEqual(client["idempotency_key"], "room:task-1:1")
        self.assertIs(client["deadline"], deadline)
        self.assertIs(client["source"], h.source_instance)
        self.assertIs(client["resolved"], h.resolved)
        self.assertIs(client["admission_store"], h.deps[4])

    def test_source_and_client_are_built_from_caller_owned_dependencies_only(self):
        h = Harness(self)
        h.build()
        authority, identity_reader, catalog_reader, config, _store = h.deps
        self.assertEqual(len(h.source_ctor), 1)
        ctor = h.source_ctor[0]
        self.assertEqual(set(ctor), {"joining_store", "authority", "identity_reader", "catalog_reader",
                                     "target_http", "endpoint_id", "generation"})
        self.assertIs(ctor["joining_store"], h.store)
        self.assertIs(ctor["authority"], authority)
        self.assertIs(ctor["identity_reader"], identity_reader)
        self.assertIs(ctor["catalog_reader"], catalog_reader)
        self.assertIs(ctor["target_http"], config)
        self.assertEqual((ctor["endpoint_id"], ctor["generation"]), ("ep", 1))
        self.assertEqual(h.client_ctor, [(config, {})])
        self.assertEqual(h.passthrough_ctor, [(BASE_URL, _binding().timeout)])

    def test_selector_refuses_zero_wrong_binding_and_wrong_state(self):
        cases = {"zero": None, "wrong_host_peer_generation": dict(host_peer="ep:2"),
                 "wrong_endpoint": dict(host_peer="other:1"), "wrong_room": dict(room="room-x"),
                 "wrong_member": dict(member="member-x"), "wrong_host": dict(host_id="install:" + "3" * 32),
                 "wrong_target": dict(target="install:" + "4" * 32), "wrong_profile": dict(profile="other"),
                 "wrong_binding_endpoint": dict(binding_endpoint="zz"),
                 "redeemed": dict(state="redeemed"), "pending": dict(state="consent_pending_delivery"),
                 "left": dict(state="left"), "previewed": dict(state="previewed")}
        for name, kw in cases.items():
            with self.subTest(name):
                h = Harness(self)
                if kw is not None:
                    seed(h.store, n=1, **kw)
                h.build()
                h.assert_denied(self.call(h))

    def test_connector_profile_mismatch_with_target_config_refuses(self):
        h = Harness(self)
        seed(h.store, n=1)
        h.deps = h.deps[:3] + (TargetHttpConfig(BASE_URL, "other"), h.deps[4])
        h.build()
        h.assert_denied(self.call(h))

    def test_multiple_matches_fail_closed_and_never_first_match(self):
        h = Harness(self)
        seed(h.store, n=1)
        seed(h.store, n=2)  # same room/member/host, distinct invite
        h.build()
        h.assert_denied(self.call(h))

    def test_selector_signature_takes_no_request_material(self):
        parameters = tuple(inspect.signature(_tdb().select_target_import).parameters)
        self.assertEqual(parameters, ("joining_store", "endpoint_id", "generation", "expected_scope", "deadline"))

    def test_request_cannot_select_another_import(self):
        h = Harness(self)
        evil = {**EXPECTED, "room_id": "room-evil", "member_id": "member-evil"}
        seed(h.store, n=2, room="room-evil", member="member-evil")
        h.build()
        # Bypass the firewall on purpose: even if the body names another room/member, only the
        # owner-fixed connector scope selects.
        h.assert_denied(self.call(h, dispatch_forwarded(scope=evil)))
        _, lobby, _invite = seed(h.store, n=1)
        self.assertEqual(self.call(h, dispatch_forwarded(scope=evil)).status, 202)
        self.assertEqual([c["lobby_id"] for c in h.source_calls], [lobby])

    def test_no_cache_every_request_reselects_and_fails_closed(self):
        h = Harness(self)
        first, lobby, _ = seed(h.store, n=1)
        h.build()
        with mock.patch.object(h.store, "list_imports", wraps=h.store.list_imports) as listing:
            self.assertEqual(self.call(h).status, 202)
            second, _, _ = seed(h.store, n=2)
            denied = self.call(h)
            self.assertEqual((denied.status, denied.body), (403, DENIED_BODY))
            self.assertEqual(len(h.source_calls), 1)
            h.store.mark_left(second)
            self.assertEqual(self.call(h).status, 202)
            self.assertEqual(len(h.source_calls), 2)
            h.store.mark_left(first)
            denied = self.call(h)
            self.assertEqual((denied.status, denied.body), (403, DENIED_BODY))
            self.assertEqual(len(h.source_calls), 2)
            self.assertEqual(listing.call_count, 4)
        self.assertEqual(len(h.client_calls), 2)

    def test_forwarded_bytes_key_and_deadline_reach_the_client_unchanged(self):
        h = Harness(self)
        seed(h.store, n=1)
        h.build()
        request = dispatch_forwarded("task-\u0041b", 7)
        raw = request.body
        self.assertIn(b"\n\t ", raw)
        self.assertIn(" :\t".encode(), raw)
        self.assertIn("caff\u00e8".encode(), raw)
        deadline = time.monotonic() + 3
        self.assertEqual(h.native(request, deadline=deadline).status, 202)
        client = h.client_calls[0]
        self.assertIs(client["forwarded_body"], raw)
        self.assertEqual(client["idempotency_key"], "room:task-Ab:7")
        self.assertIs(client["deadline"], deadline)

    def test_missing_or_duplicate_idempotency_key_is_a_pre_effect_denial(self):
        h = Harness(self)
        seed(h.store, n=1)
        h.build()
        raw = dispatch_raw()
        for headers in (_headers(None), _headers("room:task-1:1") + (("Idempotency-Key", "x"),)):
            with self.subTest(headers=len(headers)):
                h.assert_denied(self.call(h, ForwardedRequest("POST", "/v1/runs", headers, raw)))

    def test_expired_or_missing_deadline_does_zero_work(self):
        h = Harness(self)
        seed(h.store, n=1)
        h.build()
        for value in (None, time.monotonic() - 1, float("nan"), float("inf"), True, "x", 0):
            with self.subTest(value=value), \
                    mock.patch.object(h.store, "list_imports") as listing, \
                    mock.patch.object(h.store, "confirmed_scope") as confirmed:
                h.assert_denied(h.native(dispatch_forwarded(), deadline=value))
                listing.assert_not_called()
                confirmed.assert_not_called()

    def test_unparseable_body_is_a_pre_effect_denial(self):
        h = Harness(self)
        seed(h.store, n=1)
        h.build()
        for body in (b"\xff", b"{", b'{"a":1,"a":2}', b"[]"):
            with self.subTest(body=body):
                h.assert_denied(self.call(h, ForwardedRequest("POST", "/v1/runs", _headers("room:task-1:1"), body)))

    def test_forwarded_host_grant_is_never_compared_or_forwarded(self):
        h = Harness(self)
        seed(h.store, n=1)
        h.build()
        for auth in ("HermesRoom host-forwarded-grant", "HermesRoom something-else"):
            request = ForwardedRequest("POST", "/v1/runs", _headers("room:task-1:1", auth), dispatch_raw())
            self.assertEqual(self.call(h, request).status, 202)
        for client in h.client_calls:
            self.assertNotIn("host-forwarded-grant", repr(client))
            self.assertNotIn("something-else", repr(client))
        self.assertEqual(len(h.client_calls), 2)

    def test_pre_effect_resolve_refusal_is_generic_403_without_admit(self):
        from invite_rooms.target_material import TargetMaterialSourceError
        for exc in (TargetMaterialSourceError("material_unavailable"), RuntimeError("secret detail")):
            with self.subTest(exc=type(exc).__name__):
                h = Harness(self)
                seed(h.store, n=1)
                h.resolve_exc = exc
                h.build()
                response = self.call(h)
                self.assertEqual((response.status, response.body), (403, DENIED_BODY))
                self.assertEqual(h.client_calls, [])
                self.assertEqual(len(h.source_calls), 1)

    def test_store_failure_during_selection_is_a_generic_403(self):
        h = Harness(self)
        seed(h.store, n=1)
        h.build()
        with mock.patch.object(h.store, "list_imports", side_effect=JoiningStoreError("material_unavailable")):
            h.assert_denied(self.call(h))

    def test_every_exception_after_admit_run_entry_propagates(self):
        errors = [TargetHttpError(code) for code in (
            "target_admission_ambiguous", "target_unavailable", "target_rejected", "invalid_new_admission",
            "invalid_replay", "invalid_request", "invalid_target_response", "target_profile_mismatch")]
        errors += [RuntimeError("x"), TimeoutError()]
        for exc in errors:
            with self.subTest(exc=repr(exc)):
                h = Harness(self)
                seed(h.store, n=1)
                h.admit_exc = exc
                h.build()
                with self.assertRaises(type(exc)):
                    self.call(h)
                self.assertEqual(len(h.client_calls), 1)

    def test_success_is_202_with_exactly_three_keys(self):
        h = Harness(self)
        seed(h.store, n=1)
        h.admit_result = {"run_id": RUN_ID, "status": "started", "replayed": False, "extra": "no"}
        h.build()
        response = self.call(h)
        self.assertEqual(response.status, 202)
        self.assertEqual(dict(response.headers)["content-type"], "application/json")
        self.assertEqual(json.loads(response.body), {"run_id": RUN_ID, "status": "started", "replayed": False})

    def test_all_non_dispatch_operations_use_unchanged_passthrough(self):
        h = Harness(self)
        h.build()
        key = ("authorization", HOST_GRANT)
        run = "run_" + "b" * 32
        requests = [
            ForwardedRequest("GET", "/v1/room-members/capabilities", (key,), b""),
            ForwardedRequest("GET", f"/v1/runs/{run}", (key,), b""),
            ForwardedRequest("POST", f"/v1/runs/{run}/resolve-unknown", (key,), b'{"admission_id":"a"}'),
            ForwardedRequest("POST", f"/v1/runs/{run}/stop", (key,), b"{}"),
            ForwardedRequest("POST", "/v1/room-members/grants/revoke", (key,), b"{}"),
            ForwardedRequest("POST", "/v1/room-members/grants/refresh", (key,), b'{"ttl_seconds":60}'),
            ForwardedRequest("GET", "/v1/runs", (key,), b""),
            ForwardedRequest("POST", "/v1/runs/", (key,), b"{}"),
        ]
        for request in requests:
            with self.subTest(path=request.path):
                deadline = time.monotonic() + 5
                before = len(h.passthrough_calls)
                response = h.native(request, deadline=deadline)
                self.assertEqual(response.status, 200)
                passed, passed_deadline = h.passthrough_calls[before]
                self.assertIs(passed, request)
                self.assertIs(passed_deadline, deadline)
        self.assertEqual(h.client_calls, [])
        self.assertEqual(h.source_calls, [])

    def test_factory_construction_is_pure(self):
        h = Harness(self)
        threads = set(threading.enumerate())
        with mock.patch.object(socket, "socket", side_effect=AssertionError("socket created")):
            factory = h.make_factory()
        self.assertEqual((h.source_ctor, h.client_ctor, h.passthrough_ctor), ([], [], []))
        self.assertEqual(set(threading.enumerate()), threads)
        self.assertTrue(callable(factory))
        self.assertFalse(any(name for name in vars(_tdb()) if name.endswith("_CACHE")))

    def test_list_imports_deadline_is_additive(self):
        h = Harness(self)
        seed(h.store, n=1)
        self.assertEqual(len(h.store.list_imports()), 1)
        self.assertEqual(len(h.store.list_imports(deadline=time.monotonic() + 5)), 1)
        with self.assertRaises(JoiningStoreError):
            h.store.list_imports(deadline=time.monotonic() - 1)
        with self.assertRaises(TypeError):
            h.store.list_imports(time.monotonic() + 5)  # keyword-only


class RealSourceLaneTests(unittest.TestCase):
    """Real TargetHttpClient source lane; only the network write and receipt store are scratch fakes."""

    def test_target_grant_not_forwarded_host_grant_reaches_core(self):
        @dataclass(frozen=True)
        class Resolved:
            scope: MaterialScope
            credential_ref: str

        class Store:
            def __init__(self):
                self.puts = []

            def find(self, **kw):
                return None

            def reserve(self, **kw):
                return None

            def put(self, **kw):
                self.puts.append(kw)

        h = Harness(self, real_client=True)
        _, lobby, invite = seed(h.store, n=1)
        scope = MaterialScope(
            owner_installation_id=HOME, owner_profile="default", lobby_id=lobby, planned_room_id=ROOM,
            member_id=MEMBER, invite_id=invite, peer_id="peer", endpoint_id="ep", fingerprint=FINGERPRINT,
            target_install_id=TARGET, target_profile="default", catalog_digest="c" * 64,
            policy_digest="d" * 64, pairing_generation=1)
        h.resolved = Resolved(scope, "cred-ref")
        h.load_result = {"grant": "TARGET-GRANT", "catalog": {"k": 1}, "binding": scope.binding}
        store = Store()
        h.deps = h.deps[:4] + (store,)
        posted = []

        def post(url, headers, payload, *, deadline=None):
            posted.append((url, dict(headers), payload))
            return (202, json.dumps({"run_id": RUN_ID, "status": "started", "replayed": False}).encode())

        self.addCleanup(shutdown_admission_executor)
        with mock.patch.object(target_http_module, "TargetMaterialSource", h.Source), \
                mock.patch.object(target_http_module, "ResolvedTargetMaterial", Resolved), \
                mock.patch.object(target_http_module, "AdmissionReceiptStore", Store), \
                mock.patch.object(target_http_io, "post_loopback", post):
            h.build()
            request = dispatch_forwarded()
            response = h.native(request, deadline=time.monotonic() + 5)
        self.assertEqual(response.status, 202)
        self.assertEqual(json.loads(response.body), {"run_id": RUN_ID, "status": "started", "replayed": False})
        self.assertEqual(len(posted), 1)
        url, headers, payload = posted[0]
        self.assertEqual(url, BASE_URL + "/v1/runs")
        self.assertEqual(headers["Authorization"], "HermesRoom TARGET-GRANT")
        self.assertNotIn("host-forwarded-grant", repr(headers))
        self.assertEqual(headers["Idempotency-Key"], "room:task-1:1")
        self.assertEqual(payload, request.body)
        self.assertEqual(len(store.puts), 1)

    def test_legacy_three_argument_transport_lane_is_intact(self):
        scope = MaterialScope(
            owner_installation_id=HOME, owner_profile="default", lobby_id="lobby-1", planned_room_id=ROOM,
            member_id=MEMBER, invite_id="invite-1", peer_id="peer", endpoint_id="ep", fingerprint=FINGERPRINT,
            target_install_id=TARGET, target_profile="default", catalog_digest="c" * 64,
            policy_digest="d" * 64, pairing_generation=1)
        sent, puts = [], []

        class Material:
            def load(self, value):
                return {"grant": "legacy-grant"}

        class Receipts:
            def find(self, **kw):
                return None

            def reserve(self, **kw):
                return None

            def put(self, **kw):
                puts.append(kw)

        def transport(url, headers, payload):
            sent.append((url, dict(headers), payload))
            return (202, json.dumps({"run_id": RUN_ID, "status": "started", "replayed": False}).encode())

        result = TargetHttpClient(TargetHttpConfig(BASE_URL, "default"), transport=transport).admit_run(
            body={"input": "x"}, idempotency_key="room:t:1", admission_store=Receipts(),
            scope=scope, material_store=Material())
        self.assertEqual(result, {"run_id": RUN_ID, "status": "started", "replayed": False})
        self.assertEqual(sent[0][0], BASE_URL + "/v1/runs")
        self.assertEqual(sent[0][1]["Authorization"], "HermesRoom legacy-grant")
        self.assertEqual(len(puts), 1)


class ConnectorInjectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="w2d-a2b2b-pki-")
        root = Path(cls._tmp.name)
        cls.peer = lab_pki.init_owner(root / "a", "a")
        cls.own = lab_pki.init_owner(root / "b", "b")

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def binding(self):
        return LinkBinding("ep", 1, ("127.0.0.1", 1), self.peer["ca_file"], self.peer["fingerprint"],
                           {"endpoint_id": "ep", "fingerprint": self.peer["fingerprint"]})

    def connector(self, h, *, authority=None, **extra):
        binding = self.binding()
        authority = authority or _Authority(binding.expected_binding)
        options = dict(binding=binding, identity=LinkIdentity(self.own["cert_file"], self.own["key_file"]),
                       authority=authority, expected_scope=dict(EXPECTED), verify_native=lambda *_: True)
        options.update(extra)
        if "target_base" not in options and "native_backend_factory" not in options:
            options["native_backend_factory"] = h.make_factory()
        connector = TargetConnector(**options)
        self.addCleanup(connector.close)
        return connector, authority

    def request(self, task="task-1", gen=1):
        return Request("POST", "/v1/runs", _headers(f"room:{task}:{gen}"), dispatch_raw(task, gen))

    def test_dispatch_through_firewall_reaches_client_with_connector_deadline(self):
        h = Harness(self)
        seed(h.store, n=1)
        connector, authority = self.connector(h)
        deadline = time.monotonic() + 5
        response = connector.respond(self.request(), deadline=deadline)
        self.assertEqual(response.status, 202)
        self.assertEqual(json.loads(response.body), {"run_id": RUN_ID, "status": "started", "replayed": False})
        self.assertIs(h.client_calls[0]["deadline"], deadline)
        self.assertIs(h.source_calls[0]["deadline"], deadline)
        self.assertEqual(authority.operations, ["data_native"])
        self.assertEqual(h.passthrough_calls, [])

    def test_pre_effect_denial_is_generic_403_with_no_reservation_path(self):
        h = Harness(self)
        connector, _ = self.connector(h)  # no import at all
        response = connector.respond(self.request(), deadline=time.monotonic() + 5)
        self.assertEqual(response.status, 403)
        self.assertEqual(json.loads(response.body), {"error": {
            "message": "native request rejected", "type": "room_error", "code": None}})
        self.assertEqual((h.source_calls, h.client_calls), ([], []))

    def test_exception_after_admit_run_entry_is_ambiguous_502(self):
        for exc in (TargetHttpError("target_rejected"), TargetHttpError("target_unavailable"),
                    RuntimeError("detail")):
            with self.subTest(exc=repr(exc)):
                h = Harness(self)
                seed(h.store, n=1)
                h.admit_exc = exc
                connector, _ = self.connector(h)
                response = connector.respond(self.request(), deadline=time.monotonic() + 5)
                self.assertEqual(response, connector_module._AMBIGUOUS)
                self.assertEqual(response.status, 502)
                self.assertEqual(len(h.client_calls), 1)

    def test_connector_admission_wrapper_is_preserved_around_injected_backend(self):
        h = Harness(self)
        seed(h.store, n=1)
        connector, authority = self.connector(h)
        authority.admitted = False
        response = connector.respond(self.request(), deadline=time.monotonic() + 5)
        self.assertEqual(response, connector_module._AMBIGUOUS)
        self.assertEqual((h.source_calls, h.client_calls), ([], []))
        authority.admitted = True
        h.admit_effect = lambda: setattr(authority, "state", LinkState(
            authority.state.binding, 2, "acknowledged"))
        response = connector.respond(self.request(), deadline=time.monotonic() + 5)
        self.assertEqual(response, connector_module._AMBIGUOUS)  # post-I/O check() still ambiguous
        self.assertEqual(len(h.client_calls), 1)

    def test_expired_connector_deadline_is_ambiguous_before_any_work(self):
        h = Harness(self)
        seed(h.store, n=1)
        connector, _ = self.connector(h)
        response = connector.respond(self.request(), deadline=time.monotonic() - 1)
        self.assertEqual(response, connector_module._AMBIGUOUS)
        self.assertEqual((h.source_calls, h.client_calls), ([], []))

    def test_non_dispatch_and_denied_routes_through_firewall(self):
        h = Harness(self)
        connector, _ = self.connector(h)
        auth = (("authorization", HOST_GRANT),)
        response = connector.respond(Request("GET", "/v1/room-members/capabilities", auth),
                                     deadline=time.monotonic() + 5)
        self.assertEqual(response.status, 200)
        self.assertEqual([r.path for r, _ in h.passthrough_calls], ["/v1/room-members/capabilities"])
        denied = connector.respond(Request("GET", "/v1/unknown", auth), deadline=time.monotonic() + 5)
        self.assertEqual(denied, connector_module._DENIED)
        self.assertEqual(len(h.passthrough_calls), 1)
        self.assertEqual((h.source_calls, h.client_calls), ([], []))

    def test_factory_receives_fixed_binding_and_a_copy_of_expected_scope(self):
        h = Harness(self)
        calls = []
        native_calls = []

        def factory(binding, scope):
            calls.append((binding, scope))

            def native(request, *, deadline):
                native_calls.append((request, deadline))
                return UpstreamResponse(200, (("content-type", "application/json"),), b"{}")
            return native

        binding = self.binding()
        expected = dict(EXPECTED)
        connector, _ = self.connector(h, binding=binding, expected_scope=expected, native_backend_factory=factory)
        self.assertEqual(len(calls), 1)
        self.assertIs(calls[0][0], binding)
        self.assertEqual(calls[0][1], EXPECTED)
        self.assertIsNot(calls[0][1], expected)
        deadline = time.monotonic() + 5
        connector.respond(Request("GET", "/v1/room-members/capabilities", (("authorization", HOST_GRANT),)),
                          deadline=deadline)
        self.assertIs(native_calls[0][1], deadline)

    def test_factory_and_target_base_are_exactly_one(self):
        h = Harness(self)
        called = []
        factory = lambda binding, scope: called.append(1) or (lambda request, *, deadline: None)
        with self.assertRaises(ValueError):
            self.connector(h, target_base=BASE_URL, native_backend_factory=factory)
        self.assertEqual(called, [])
        with self.assertRaises(ValueError):
            self.connector(h, target_base=None, native_backend_factory=None)
        with self.assertRaises(ValueError):
            self.connector(h, native_backend_factory="not-callable")
        with self.assertRaises(ValueError):
            self.connector(h, native_backend_factory=lambda binding, scope: "not-callable")
        with self.assertRaises(ValueError):
            TargetConnector(inner={"peer_ca_file": self.peer["ca_file"], "cert_file": self.own["cert_file"],
                                   "key_file": self.own["key_file"], "peer_fingerprint": self.peer["fingerprint"]},
                            firewall=object(), native_backend_factory=factory)

    def test_default_target_base_path_is_unchanged(self):
        h = Harness(self)
        built = []

        def backend(base, *, timeout):
            built.append((base, timeout))
            return lambda request, *, deadline=None: UpstreamResponse(
                200, (("content-type", "application/json"),), b"{}")

        with mock.patch.object(connector_module, "native_http_backend", backend):
            connector, authority = self.connector(h, target_base=BASE_URL)
            response = connector.respond(Request("GET", "/v1/room-members/capabilities",
                                                 (("authorization", HOST_GRANT),)), deadline=time.monotonic() + 5)
        self.assertEqual(built, [(BASE_URL, 10.0)])
        self.assertEqual(response.status, 200)
        self.assertEqual(authority.operations, ["data_native"])

    def test_no_listener_or_socket_is_created(self):
        h = Harness(self)
        seed(h.store, n=1)
        threads = set(threading.enumerate())
        with mock.patch.object(socket, "socket", side_effect=AssertionError("socket created")):
            connector, _ = self.connector(h)
            self.assertEqual(connector.respond(self.request(), deadline=time.monotonic() + 5).status, 202)
        self.assertIsNone(connector._listener)
        self.assertIsNone(connector._thread)
        self.assertFalse(any(t.name == "room-data-accept" for t in threading.enumerate()))
        self.assertEqual(set(threading.enumerate()), threads)


if __name__ == "__main__":
    unittest.main()
