"""Durable witnesses for the source admission boundary.

B1/B2/B3/B4 are accepted as scoped gates. The retained method bodies are being
completed in serial evidence-bound slices; placeholders and compatibility
assertions do not count as coverage. Tests use a genuine source fixture and
scratch loopback only (no live Core/agent execution). Owned comparator faults
are wiring witnesses, not valid READY replacement exploits; narrow local-prior
controls do not prove restart/lost-reply recovery.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import socket
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from contextlib import closing, ExitStack
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from invite_rooms.admission_receipts import AdmissionReceiptStore
from invite_rooms.peer_registry import ADMISSION_DEADLINE
from invite_rooms.runtime_config import TargetHttpConfig
from invite_rooms.target_http import TargetHttpClient, TargetHttpError, _parse_body, _validate_source_shape, request_fingerprint

from test_target_material_source_w2d import genuine_source_fixture

_ACTIVE_LOOPBACK = None


class _Loopback:
    def __init__(self, *, response=None):
        self.posts: list[tuple[bytes, dict[str, str]]] = []
        self.paths: list[str] = []
        self.raw_ingress = 0
        self.verified_native_effect = 0
        self.verify_native = None
        self.native_capture = None
        self.closed = False
        self.response = response or {"run_id": "run_" + "a" * 32, "status": "started", "replayed": False}
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers["Content-Length"])
                raw = self.rfile.read(length)
                owner.posts.append((raw, {k: v for k, v in self.headers.items()}))
                owner.paths.append(self.path)
                owner.raw_ingress += 1
                verified = owner.verify_native is not None and owner.verify_native(raw, self.headers)
                if verified:
                    owner.verified_native_effect += 1
                payload = (json.dumps(owner.response, separators=(",", ":")).encode()
                           if verified else b'{"error":"forbidden"}')
                self.send_response(202 if verified else 403)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(payload)
                self.wfile.flush()
                self.close_connection = True

            def log_message(self, *_args):
                return

        class OwnedServer(ThreadingHTTPServer):
            daemon_threads = False
            block_on_close = True

        self.server = OwnedServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.config = TargetHttpConfig(f"http://127.0.0.1:{self.server.server_port}", "default")

    def close(self):
        if self.closed:
            return
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.closed = True


def _source_for(fx, config):
    from invite_rooms.target_material import TargetMaterialSource
    original = fx["source"]
    return TargetMaterialSource(
        joining_store=fx["fx"]["joining_store"], authority=original._authority,
        identity_reader=original._identity_reader, catalog_reader=original._catalog_reader,
        target_http=config, endpoint_id=fx["scope"].endpoint_id,
        generation=fx["scope"].pairing_generation,
    )


def _native_request(fx, task_id: str, config):
    """Actual Core formatting/network boundary capture, FORMATTING ONLY.

    prepare/session/response hooks are scratch formatting scaffolding, not live
    Core or agent admission. Original bytes, Authorization and key stay in memory.
    """
    from gateway.hosted_room_peer import HostedMemberDispatch, decode_room_grant
    from tui_gateway.hosted_room_peer_http import PeerRunsHTTPClient

    scope = fx["scope"]
    claims = decode_room_grant(fx["native_secret"], fx["grant"], permission="dispatch")
    prompt = "R02 café e\u0301 🧪"
    dispatch = {
        "protocol_version": 2, "room_id": scope.planned_room_id,
        "home_install_id": scope.owner_installation_id,
        "authority_gateway_id": claims["authority_gateway_id"],
        "authority_epoch": claims["authority_epoch"], "member_id": scope.member_id,
        "target_install_id": scope.target_install_id, "target_profile": scope.target_profile,
        "task_id": task_id, "execution_generation": 1, "source_event_seq": 1,
        "cancellation_scope_id": "cancel-" + task_id, "prompt": prompt,
        "prompt_digest": hashlib.sha256(prompt.encode()).hexdigest(),
        "capability_digest": scope.catalog_digest, "execution_policy_digest": scope.policy_digest,
        "trace_id": "trace-" + task_id,
    }
    checked = HostedMemberDispatch.from_mapping(dispatch)
    peer = PeerRunsHTTPClient(base_url=config.base_url, api_key="")
    peer.prepare = lambda **_kwargs: {"session_id": "roomlink_capture"}
    captured = {}
    class Response:
        headers = {"Content-Length": "68"}
        def __init__(self):
            self.payload = b'{"run_id":"run_' + b"a" * 32 + b'","status":"started","replayed":false}'
        def read1(self, _size):
            payload, self.payload = self.payload, b""
            return payload
        read = read1
    class Opener:
        def __call__(self, request, **_kwargs):
            captured["request"] = request
            return self
        def __enter__(self):
            return Response()
        def __exit__(self, *_args):
            return False
    with mock.patch("hermes_cli.urllib_security.open_credentialed_url", Opener()):
        peer._admit_dispatch(checked, grant=fx["grant"])
    request = captured["request"]
    raw = request.data
    headers = dict(request.header_items())
    headers["Idempotency-Key"] = request.get_header("Idempotency-key")
    body = json.loads(raw.decode("utf-8"))
    if _ACTIVE_LOOPBACK is not None:
        _ACTIVE_LOOPBACK.native_capture = {
            "raw": raw, "authorization": request.get_header("Authorization"),
            "key": headers["Idempotency-Key"], "route": request.selector,
            "profile": checked.target_profile,
        }
    return body, raw, headers["Idempotency-Key"], checked


def _args(fx, source, store, task_id, config, budget=5, *, clear_capture=True):
    body, raw, key, checked = _native_request(fx, task_id, config)
    # Shared scratch cryptographic receive verification, NOT a native run effect.
    # Parse RECEIVED bytes and verify RECEIVED Authorization, not fixture values.
    from gateway.hosted_room_peer import HostedMemberDispatch, verify_room_grant

    def verify_received(received_raw, headers):
        try:
            received = json.loads(received_raw.decode("utf-8"))
            dispatch = HostedMemberDispatch.from_mapping(received["hosted_room_dispatch"])
            scheme, grant = headers["Authorization"].split(" ", 1)
            if scheme != "HermesRoom":
                return False
            verify_room_grant(fx["native_secret"], grant, dispatch, permission="dispatch")
            return dispatch == checked and received["input"] == checked.prompt
        except (ValueError, TypeError, KeyError, IndexError, AttributeError):
            # Expected malformed input/native verification failures: no raw error,
            # header, material or traceback is exposed in the scratch response.
            return False

    _ACTIVE_LOOPBACK.verify_native = verify_received
    if clear_capture:
        _ACTIVE_LOOPBACK.posts.clear()
        _ACTIVE_LOOPBACK.paths.clear()
        _ACTIVE_LOOPBACK.raw_ingress = 0
        _ACTIVE_LOOPBACK.verified_native_effect = 0
    deadline = time.monotonic() + budget
    resolved = source.resolve(lobby_id=fx["scope"].lobby_id, invite_id=fx["scope"].invite_id, deadline=deadline)
    return dict(body=body, forwarded_body=raw, idempotency_key=key, source=source,
                resolved=resolved, admission_store=store, deadline=deadline), checked


def _producer_snapshot(fx):
    # Secret-bearing values stay in memory; callers compare only booleans.
    with fx["fx"]["joining_store"]._read() as connection:
        rows = tuple(tuple(row) for row in connection.execute(
            "SELECT * FROM material ORDER BY scope_key"))
    return rows, len(fx["producer"]["native_calls"])


def _lookup(fx, http, args):
    return dict(scope=fx["scope"], generation=fx["scope"].pairing_generation,
                profile=http.config.profile, base_url=http.config.base_url,
                idempotency_key=args["idempotency_key"],
                request_fingerprint=request_fingerprint(args["body"]))


def _pending(store):
    # The actual owned-store lock/recovery protocol, not committed-count inference.
    with store._transaction(deadline=time.monotonic() + 5):
        return store._read_pending()


def _receipt_witness(test, fx, http, args, store, path, *, expected=None):
    lookup = _lookup(fx, http, args)
    found = store.find(**lookup)
    test.assertIsNotNone(found, "matching durable receipt absent")
    test.assertTrue(found["scope"] == fx["scope"].as_dict())
    test.assertTrue(all(found[k] == v for k, v in lookup.items() if k != "scope"))
    test.assertEqual(found["run_id"], http.response["run_id"])
    test.assertEqual(found["status"], "started")
    test.assertIs(found["replayed"], False)
    if expected is not None:
        test.assertTrue(found == expected, "original receipt changed")
    with closing(AdmissionReceiptStore(path)) as reopened:
        test.assertEqual(reopened.count(), 1)
        test.assertTrue(reopened.find(**lookup) == found, "fresh reopen binding changed")
    return found


def _foreign_ref_witness(test):
    # Standalone so the archived original client can exercise ordering without
    # importing the later identity helpers. Both real producers remain alive.
    with genuine_source_fixture() as a, genuine_source_fixture() as b, _managed_loopback() as http:
        sa, sb = _source_for(a, http.config), _source_for(b, http.config)
        ra = sa.resolve(lobby_id=a["scope"].lobby_id, invite_id=a["scope"].invite_id,
                        deadline=time.monotonic() + 5)
        rb = sb.resolve(lobby_id=b["scope"].lobby_id, invite_id=b["scope"].invite_id,
                        deadline=time.monotonic() + 5)
        test.assertTrue(ra.credential_ref != rb.credential_ref, "genuine refs must differ")
        for fx, source, resolved in ((a, sa, ra), (b, sb, rb)):
            loaded = source.load(resolved.scope, credential_ref=resolved.credential_ref,
                                 deadline=time.monotonic() + 5)
            test.assertTrue(loaded["grant"] == fx["grant"], "own real ref must load")
        before = (_producer_snapshot(a), _producer_snapshot(b))
        with tempfile.TemporaryDirectory() as directory, ExitStack() as resources:
            path = Path(directory) / "foreign"
            store = resources.enter_context(closing(AdmissionReceiptStore(path)))
            control = resources.enter_context(closing(AdmissionReceiptStore(Path(directory) / "control")))
            args, _ = _args(a, sa, store, "exact", http.config)
            original = http.native_capture
            lookup = _lookup(a, http, args)
            # Transparent OWN recorders proven outside SUT, then reset. The
            # reserve control uses its own store, never the negative baseline.
            with mock.patch.object(sa, "load", wraps=sa.load) as load, \
                    mock.patch.object(AdmissionReceiptStore, "find", autospec=True,
                                      side_effect=AdmissionReceiptStore.find) as find, \
                    mock.patch.object(AdmissionReceiptStore, "reserve", autospec=True,
                                      side_effect=AdmissionReceiptStore.reserve) as reserve:
                load(ra.scope, credential_ref=ra.credential_ref, deadline=time.monotonic() + 5)
                control.find(**lookup)
                control.reserve(**lookup)
                test.assertEqual((load.call_count, find.call_count, reserve.call_count), (1, 1, 1))
                test.assertEqual(len(_pending(control)), 1)
                load.reset_mock(); find.reset_mock(); reserve.reset_mock()
                test.assertEqual(len(_pending(store)), 0)
                wrong = dict(args, resolved=replace(args["resolved"], credential_ref=rb.credential_ref))
                test.assertTrue(wrong["body"] is args["body"])
                test.assertTrue(wrong["forwarded_body"] == original["raw"])
                test.assertEqual(wrong["idempotency_key"], "room:exact:1")
                with test.assertRaisesRegex(TargetHttpError, "^target_admission_ambiguous$"):
                    TargetHttpClient(http.config).admit_run(**wrong)
                test.assertEqual(load.call_count, 1, "foreign-ref initial genuine load not reached")
                test.assertEqual(find.call_count, 0, "foreign-ref reached find before authorization")
                test.assertEqual(reserve.call_count, 0, "foreign-ref reached reserve before authorization")
            test.assertEqual((http.raw_ingress, http.verified_native_effect), (0, 0))
            test.assertEqual(store.count(), 0)
            test.assertIsNone(store.find(**lookup))
            test.assertEqual(len(_pending(store)), 0, "foreign-ref published unauthorized pending")
            with closing(AdmissionReceiptStore(path)) as reopened:
                test.assertEqual(reopened.count(), 0)
                test.assertIsNone(reopened.find(**lookup))
                test.assertEqual(len(_pending(reopened)), 0)
            test.assertTrue((_producer_snapshot(a), _producer_snapshot(b)) == before,
                            "foreign-ref consumer changed a producer")


def _identity_witness(test, source, args):
    from invite_rooms.target_http import _material_identity, _require_same_material
    genuine = source.load(args["resolved"].scope, credential_ref=args["resolved"].credential_ref,
                          deadline=time.monotonic() + 5)
    identity = _material_identity(genuine)
    again = source.load(args["resolved"].scope, credential_ref=args["resolved"].credential_ref,
                        deadline=time.monotonic() + 5)
    _require_same_material(identity, again)
    _require_same_material(identity, copy.deepcopy(genuine))
    # UNIT REPRESENTATIONS ONLY: none is ever returned from Source.load.
    for field in ("grant", "catalog", "binding"):
        with test.subTest(detached_identity=field):
            changed = copy.deepcopy(genuine)
            if field == "grant":
                changed[field] += "copied-unit-only"
            elif field == "catalog":
                changed[field]["execution_policy"]["target_profile"] = "copied-unit-only"
            else:
                changed[field]["target_profile"] = "copied-unit-only"
            with test.assertRaisesRegex(TargetHttpError, "^target_admission_ambiguous$"):
                _require_same_material(identity, changed)
    for field in ("catalog", "binding"):
        with test.subTest(detached_after_capture=field):
            changed = copy.deepcopy(genuine)
            captured = _material_identity(changed)
            if field == "catalog":
                changed[field]["execution_policy"]["target_profile"] = "after-capture"
            else:
                changed[field]["target_profile"] = "after-capture"
            test.assertTrue(captured == identity, "identity retained a mutable alias")
            _require_same_material(captured, genuine)
            with test.assertRaisesRegex(TargetHttpError, "^target_admission_ambiguous$"):
                _require_same_material(captured, changed)


def _comparison_witness(test, branch, fault=None, inherited_earlier=True):
    """OWNED GUARD FAULT, not a reachable READY-material replacement exploit."""
    from invite_rooms import target_http as module
    with genuine_source_fixture() as fx, _managed_loopback() as http, \
            tempfile.TemporaryDirectory() as directory, ExitStack() as resources:
        source = _source_for(fx, http.config)
        path = Path(directory) / "admissions"
        store = resources.enter_context(closing(AdmissionReceiptStore(path)))
        args, _ = _args(fx, source, store, "exact", http.config, budget=15)
        prior = None
        if branch == "prior":
            # Actual earlier LOCAL matching receipt, produced by PUBLIC admit_run.
            result = TargetHttpClient(http.config).admit_run(**args)
            test.assertIs(result["replayed"], False)
            prior = _receipt_witness(test, fx, http, args, store, path)
            test.assertEqual((http.raw_ingress, http.verified_native_effect), (1, 1))
            http.posts.clear(); http.paths.clear()
            http.raw_ingress = http.verified_native_effect = 0
            http.response = dict(run_id=result["run_id"], status="running", replayed=True)
        before = _producer_snapshot(fx)
        explicit = args["deadline"]
        inherited = explicit - 2 if inherited_earlier else explicit + 2
        effective = min(explicit, inherited)
        previous_context = ADMISSION_DEADLINE.get()
        token = ADMISSION_DEADLINE.set(inherited)
        identities, comparisons, loads, boundaries, put_entries = [], [], [], [], []
        original_identity, original_compare = module._material_identity, module._require_same_material
        original_load = source.load

        def identity(loaded):
            value = original_identity(loaded)
            identities.append(value)
            return value

        def load(*a, **kw):
            boundaries.append(("source.load", kw["deadline"]))
            value = original_load(*a, **kw)
            loads.append(value)
            return value

        def compare(initial, loaded):
            original_compare(initial, loaded)
            comparisons.append(initial)
            test.assertTrue(initial is identities[0], "gate did not retain ORIGINAL immutable identity")
            test.assertTrue(loaded is loads[-1], "comparison did not receive genuine fresh load")
            if fault == len(comparisons):
                raise TargetHttpError("target_admission_ambiguous")

        def record(label, original):
            def wrapped(*a, **kw):
                boundaries.append((label, kw["deadline"]))
                if label == "receipt.put":
                    put_entries.append(True)
                return original(*a, **kw)
            return wrapped

        try:
            with ExitStack() as patches:
                patches.enter_context(mock.patch.object(module, "_material_identity", side_effect=identity))
                patches.enter_context(mock.patch.object(module, "_require_same_material", side_effect=compare))
                patches.enter_context(mock.patch.object(source, "load", side_effect=load))
                targets = [(store, n, "receipt." + n) for n in ("find", "reserve", "put", "_transaction")]
                targets += [(fx["fx"]["joining_store"], n, "source-store." + n) for n in
                            ("find_import", "confirmed_scope", "get_consent", "mint_state", "mint_grant_id", "mint_load")]
                targets += [(source, n, n) for n in ("_identity_reader", "_catalog_reader")]
                targets += [(module.target_http_io, "post_loopback", "network")]
                for owner, name, label in targets:
                    patches.enter_context(mock.patch.object(owner, name, side_effect=record(label, getattr(owner, name))))
                if fault is None:
                    result = TargetHttpClient(http.config).admit_run(**args)
                    test.assertIs(result["replayed"], branch == "prior")
                    test.assertEqual(result["status"], "running" if branch == "prior" else "started")
                    test.assertEqual(result["run_id"], http.response["run_id"])
                else:
                    with test.assertRaisesRegex(TargetHttpError, "^target_admission_ambiguous$"):
                        TargetHttpClient(http.config).admit_run(**args)
        finally:
            ADMISSION_DEADLINE.reset(token)
        test.assertTrue(ADMISSION_DEADLINE.get() == previous_context, "ContextVar was not restored")
        expected = 3 if fault is None else fault
        test.assertEqual(len(comparisons), expected, "comparison gate count/reached fault differs")
        test.assertEqual(len(loads), expected + 1, "genuine fresh-load count differs")
        test.assertTrue(all(value is identities[0] for value in comparisons))
        test.assertTrue(boundaries and all(end == effective for _, end in boundaries),
                        "original effective absolute deadline changed at a real boundary")
        labels = {label for label, _ in boundaries}
        test.assertTrue({"source.load", "receipt.find", "receipt._transaction", "source-store.mint_load",
                         "_identity_reader", "_catalog_reader"} <= labels)
        emitted = fault != 1
        test.assertEqual((http.raw_ingress, http.verified_native_effect), (int(emitted), int(emitted)))
        puts = int(branch == "new" and (fault is None or fault == 3))
        test.assertEqual(len(put_entries), puts, "effect crossed the selected comparison gate")
        if puts or branch == "prior":
            _receipt_witness(test, fx, http, args, store, path, expected=prior)
        else:
            test.assertEqual(store.count(), 0)
            test.assertIsNone(store.find(**_lookup(fx, http, args)))
            pending = _pending(store)
            test.assertEqual(len(pending), 1)
            test.assertTrue(pending[0] == dict(_lookup(fx, http, args), scope=fx["scope"].as_dict()))
            with closing(AdmissionReceiptStore(path)) as reopened:
                test.assertEqual(reopened.count(), 0)
                test.assertTrue(_pending(reopened) == pending)
        test.assertTrue(_producer_snapshot(fx) == before, "comparison witness changed producer")


def _cutover_witness(test, when):
    from invite_rooms import target_http as module
    from invite_rooms.target_material import TargetMaterialSourceError
    with genuine_source_fixture() as fx, _managed_loopback() as http, \
            tempfile.TemporaryDirectory() as directory, ExitStack() as resources:
        source = _source_for(fx, http.config)
        path = Path(directory) / when
        store = resources.enter_context(closing(AdmissionReceiptStore(path)))
        args, _ = _args(fx, source, store, "cutover-" + when, http.config)
        before = _producer_snapshot(fx)
        original_load, original_put = source.load, store.put
        original_post = module.target_http_io.post_loopback
        markers, puts = [], []

        def leave():
            row = fx["fx"]["joining_store"].find_import(fx["scope"].lobby_id, fx["scope"].invite_id)
            fx["fx"]["joining_store"].mark_left(row["import_id"])
            markers.append(True)

        def post(*a, **kw):
            value = original_post(*a, **kw)
            if when == "before":
                leave()
            return value

        def put(**kw):
            puts.append(True)
            value = original_put(**kw)
            if when == "after":
                leave()
            return value

        with mock.patch.object(module.target_http_io, "post_loopback", side_effect=post), \
                mock.patch.object(store, "put", side_effect=put):
            with test.assertRaisesRegex(TargetHttpError, "^target_admission_ambiguous$"):
                TargetHttpClient(http.config).admit_run(**args)
        test.assertEqual(len(markers), 1, "real mark_left not reached")
        test.assertEqual((http.raw_ingress, http.verified_native_effect), (1, 1))
        # Independently exercise ORIGINAL Source after the client has finished.
        with test.assertRaisesRegex(TargetMaterialSourceError, "^material_unavailable$"):
            original_load(fx["scope"], credential_ref=args["resolved"].credential_ref,
                          deadline=time.monotonic() + 5)
        test.assertEqual(len(puts), int(when == "after"), "cutover allowed a late put")
        if when == "before":
            test.assertEqual(store.count(), 0)
            test.assertIsNone(store.find(**_lookup(fx, http, args)))
            pending = _pending(store)
            test.assertEqual(len(pending), 1)
            test.assertTrue(pending[0] == dict(_lookup(fx, http, args), scope=fx["scope"].as_dict()))
            with closing(AdmissionReceiptStore(path)) as reopened:
                test.assertEqual(reopened.count(), 0)
                test.assertTrue(_pending(reopened) == pending)
        else:
            _receipt_witness(test, fx, http, args, store, path)
        test.assertTrue(_producer_snapshot(fx) == before, "leave changed producer material/native calls")
        http.close()
        test.assertFalse(http.thread.is_alive())
        test.assertEqual(http.server.fileno(), -1)


class SourceDeadlinePrimitiveTests(unittest.TestCase):
    def test_accepted_source_and_legacy_real_receipt_preservation_control(self):
        with genuine_source_fixture() as fx, _managed_loopback() as http:
            source = _source_for(fx, http.config)
            with tempfile.TemporaryDirectory() as directory, closing(
                    AdmissionReceiptStore(Path(directory) / "admissions")) as store, closing(http):
                args, checked = _args(fx, source, store, "control", http.config)
                # Owned callback store closes before the surrounding scratch root.
                def producer_snapshot():
                    with fx["fx"]["joining_store"]._read() as connection:
                        rows = tuple(tuple(row) for row in connection.execute(
                            "SELECT * FROM material ORDER BY scope_key"))
                    return rows, len(fx["producer"]["native_calls"])
                before = producer_snapshot()
                original = http.native_capture
                lookup = dict(scope=fx["scope"], generation=fx["scope"].pairing_generation,
                              profile=http.config.profile, base_url=http.config.base_url,
                              idempotency_key=args["idempotency_key"],
                              request_fingerprint=request_fingerprint(args["body"]))
                expected_run = http.response["run_id"]
                result = TargetHttpClient(http.config).admit_run(**args)
                self.assertIs(result["replayed"], False)
                self.assertEqual(result["run_id"], expected_run)
                self.assertEqual(result["status"], "started")
                self.assertEqual(len(http.posts), 1)
                self.assertEqual((http.raw_ingress, http.verified_native_effect), (1, 1))
                self.assertTrue(http.posts[0][0] == original["raw"])
                self.assertTrue(http.posts[0][1]["Authorization"] == original["authorization"])
                self.assertTrue(http.posts[0][1]["Idempotency-Key"] == original["key"])
                self.assertEqual(http.paths, [original["route"]])
                self.assertEqual(original["profile"], "default")
                self.assertEqual(store.count(), 1)
                found = store.find(**lookup)
                self.assertIsNotNone(found)
                self.assertTrue(found["scope"] == fx["scope"].as_dict())
                self.assertTrue(all(found[field] == value for field, value in lookup.items()
                                    if field != "scope"))
                self.assertEqual(found["run_id"], result["run_id"])
                self.assertEqual(found["status"], "started")
                self.assertIs(found["replayed"], False)
                with closing(AdmissionReceiptStore(Path(directory) / "admissions")) as reopened:
                    self.assertEqual(reopened.count(), 1)
                    self.assertTrue(reopened.find(**lookup) == found)
                self.assertTrue(producer_snapshot() == before, "source consumer changed producer")
                # The legacy three-argument callback remains untouched.
                legacy_calls = []
                from test_target_http_w2d_card1 import Scope as LegacyScope, Material as LegacyMaterial
                legacy = TargetHttpClient(http.config, transport=lambda url, headers, payload: (
                    legacy_calls.append((url, headers, payload)),
                    type("R", (), {"status": 202, "body": json.dumps({
                        "run_id": "run_" + "b" * 32, "status": "started", "replayed": False}).encode()})())[1])
                with closing(AdmissionReceiptStore(Path(directory) / "legacy")) as legacy_store:
                    legacy_scope = LegacyScope()
                    legacy_result = legacy.admit_run(body={"input": "legacy"}, idempotency_key="legacy",
                        scope=legacy_scope, material_store=LegacyMaterial(), admission_store=legacy_store)
                    self.assertFalse(legacy_result["replayed"])
                    self.assertEqual(legacy_result["run_id"], "run_" + "b" * 32)
                    self.assertEqual(legacy_result["status"], "started")
                    self.assertEqual(len(legacy_calls), 1)
                    self.assertEqual(len(legacy_calls[0]), 3)
                    self.assertEqual(legacy_store.count(), 1)
                    legacy_lookup = dict(scope=legacy_scope, generation=legacy_scope.pairing_generation,
                                         profile=http.config.profile, base_url=http.config.base_url,
                                         idempotency_key="legacy",
                                         request_fingerprint=request_fingerprint({"input": "legacy"}))
                    legacy_found = legacy_store.find(**legacy_lookup)
                    self.assertIsNotNone(legacy_found)
                    self.assertTrue(legacy_found["scope"] == legacy_scope.as_dict())
                    self.assertTrue(all(legacy_found[field] == value for field, value in legacy_lookup.items()
                                        if field != "scope"))
                    self.assertEqual(legacy_found["run_id"], legacy_result["run_id"])
                    self.assertEqual(legacy_found["status"], "started")
                    self.assertIs(legacy_found["replayed"], False)
                    with closing(AdmissionReceiptStore(Path(directory) / "legacy")) as reopened:
                        self.assertEqual(reopened.count(), 1)
                        self.assertTrue(reopened.find(**legacy_lookup) == legacy_found)
                    self.assertEqual((http.raw_ingress, http.verified_native_effect), (1, 1))
                    self.assertTrue(producer_snapshot() == before, "legacy control changed producer")
                http.close()
                self.assertFalse(http.thread.is_alive())
                self.assertEqual(http.server.fileno(), -1)

    def test_admitted_visibility_finishes_late_without_success_and_preserves_recovery(self):
        import errno
        from invite_rooms import admission_receipts as receipts_module
        from invite_rooms.admission_receipts import AdmissionReceiptError

        # Already-admitted final visibility: the real os.replace(commit, final) is
        # ADMITTED while the original deadline is live, then held on a test event
        # until that same deadline expires.  Pre-visibility expiry is covered by
        # the accepted five-phase method; here visibility is never refused.
        final_name = "admission_receipts.json.enc"
        lock_name = "admission_receipts.lock"
        pending_name = "admission_pending.json.enc"
        real_replace = receipts_module.os.replace
        real_rename = receipts_module.os.rename
        real_unlink = receipts_module.os.unlink
        real_open = receipts_module.os.open
        pending_keys = {"scope", "generation", "profile", "base_url",
                        "idempotency_key", "request_fingerprint"}
        receipt_keys = pending_keys | {"version", "run_id", "status", "replayed"}

        def sha(data):
            return hashlib.sha256(data).hexdigest()

        def disk(root):
            # Lock-free raw view; secrets are only ever compared as digests.
            raw = (root / lock_name).read_bytes().rstrip(b" ")
            phase = None if len(raw) <= 1 else AdmissionReceiptStore._parse_journal(raw)["phase"]
            names = sorted(item.name for item in root.iterdir()
                           if item.name.startswith(".") and
                           item.name.endswith((".tmp", ".commit", ".backup", ".guard")))
            return {"phase": phase, "names": names,
                    "kinds": sorted(name.rsplit(".", 1)[1] for name in names),
                    "final": sha((root / final_name).read_bytes()),
                    "pending_file": (root / pending_name).exists()}

        def scenario(label, *, hold, fault, budget):
            with genuine_source_fixture() as fx, _managed_loopback() as http, \
                    tempfile.TemporaryDirectory() as directory, ExitStack() as resources:
                source = _source_for(fx, http.config)
                root = Path(directory) / "admissions"
                store = resources.enter_context(closing(AdmissionReceiptStore(root)))
                # Prior acknowledged receipt via the PUBLIC path: later publication
                # of the receipt file is then a real replace (not create-only).
                prior_args, _ = _args(fx, source, store, "prior", http.config, budget=20)
                self.assertIs(TargetHttpClient(http.config).admit_run(**prior_args)["replayed"], False)
                prior_lookup = _lookup(fx, http, prior_args)
                prior_found = _receipt_witness(self, fx, http, prior_args, store, root)
                old_sha = sha((root / final_name).read_bytes())
                self.assertEqual(store.count(), 1)
                self.assertFalse((root / pending_name).exists())
                self.assertEqual(disk(root)["phase"], None)

                args, _ = _args(fx, source, store, "late", http.config, budget=budget)
                lookup = _lookup(fx, http, args)
                candidate = dict(lookup, scope=fx["scope"].as_dict())
                deadline = args["deadline"]
                self.assertNotEqual(lookup["idempotency_key"], prior_lookup["idempotency_key"])

                state = {"final_calls": [], "barrier": None, "fault_hits": [],
                         "guard_unlinks": [], "renames": [], "inspect_after": [],
                         "flip": [], "puts": [], "put_outcome": [], "threads": []}
                reached, release, put_done = threading.Event(), threading.Event(), threading.Event()
                worker_done = threading.Event()
                real_put, real_inspect = store.put, store._inspect
                original_worker = TargetHttpClient._admit_source_worker

                def observed_worker(client, **kwargs):
                    # Transparent call-through: settlement is signalled only after
                    # the real worker method has actually returned (or raised).
                    try:
                        return original_worker(client, **kwargs)
                    finally:
                        worker_done.set()

                def put(**kwargs):
                    state["puts"].append(True)
                    state["threads"].append(threading.current_thread())
                    try:
                        value = real_put(**kwargs)
                        state["put_outcome"].append(("returned", None))
                        return value
                    except BaseException as error:
                        state["put_outcome"].append((type(error).__name__, str(error)))
                        raise
                    finally:
                        put_done.set()

                def replaced(source_name, target, *a, **kw):
                    if str(source_name).endswith(".commit") and str(target) == final_name:
                        state["barrier"] = {
                            "live": time.monotonic() < deadline,
                            "disk": disk(root),
                            # Decode the raw bytes: while final shares an inode with the
                            # guard the store's own regular-file read refuses it.
                            "rows": len(store._decode_raw((root / final_name).read_bytes(), final_name, receipt_keys)),
                            "pending": store._decode_raw((root / pending_name).read_bytes(), pending_name, pending_keys),
                        }
                        reached.set()
                        if hold and state["barrier"]["live"]:
                            if not release.wait(30):
                                raise RuntimeError("D01 hold was not released")
                        state["final_calls"].append(True)
                        value = real_replace(source_name, target, *a, **kw)
                        if fault == "verify":
                            fd = real_open(str(root / final_name), receipts_module.os.O_RDWR)
                            try:
                                state["flip"].append(sha((root / final_name).read_bytes()))
                                byte = receipts_module.os.pread(fd, 1, 5)
                                receipts_module.os.pwrite(fd, bytes([byte[0] ^ 1]), 5)
                            finally:
                                receipts_module.os.close(fd)
                        return value
                    return real_replace(source_name, target, *a, **kw)

                def renamed(source_name, target, *a, **kw):
                    state["renames"].append((str(source_name).endswith(".backup"), str(target) == final_name))
                    return real_rename(source_name, target, *a, **kw)

                def unlinked(path, *a, **kw):
                    if str(path).endswith(".guard"):
                        state["guard_unlinks"].append(True)
                        if fault == "guard" and not state["fault_hits"]:
                            state["fault_hits"].append(True)
                            raise OSError(errno.EIO, "injected one-shot guard cleanup failure")
                    return real_unlink(path, *a, **kw)

                def inspected(name, **kw):
                    result = real_inspect(name, **kw)
                    if state["flip"] and name == final_name and result is not None:
                        state["inspect_after"].append(result[0][4])
                    return result

                def receipts_state(handle, *, late, pending_retired):
                    self.assertEqual(handle.count(), 2 if late else 1)
                    self.assertTrue(handle.find(**prior_lookup) == prior_found,
                                    "prior acknowledged receipt changed")
                    found = handle.find(**lookup)
                    if late:
                        self.assertIsNotNone(found, "late durable receipt absent")
                        self.assertTrue(found["scope"] == fx["scope"].as_dict())
                        self.assertTrue(all(found[k] == v for k, v in lookup.items() if k != "scope"))
                        self.assertEqual(found["run_id"], http.response["run_id"])
                        self.assertEqual(found["status"], "started")
                        self.assertIs(found["replayed"], False)
                        self.assertEqual(_pending(handle), [] if pending_retired else [candidate])
                    else:
                        self.assertIsNone(found)
                        self.assertTrue(_pending(handle) == [candidate])

                started = time.monotonic()
                returned = None
                held = None
                try:
                    with mock.patch.object(TargetHttpClient, "_admit_source_worker", new=observed_worker), \
                            mock.patch.object(store, "put", side_effect=put), \
                            mock.patch.object(store, "_inspect", side_effect=inspected), \
                            mock.patch.object(receipts_module.os, "replace", side_effect=replaced), \
                            mock.patch.object(receipts_module.os, "rename", side_effect=renamed), \
                            mock.patch.object(receipts_module.os, "unlink", side_effect=unlinked):
                        try:
                            if hold:
                                with self.assertRaisesRegex(TargetHttpError, "^target_admission_ambiguous$"):
                                    TargetHttpClient(http.config).admit_run(**args)
                                returned = time.monotonic()
                                # The physical worker is STILL held: the real final
                                # call-through has not happened and the caller expired.
                                held = {"disk": disk(root), "calls": len(state["final_calls"]),
                                        "put_done": put_done.is_set(), "reached": reached.is_set(),
                                        "worker_done": worker_done.is_set()}
                            else:
                                result = TargetHttpClient(http.config).admit_run(**args)
                                returned = time.monotonic()
                                self.assertIs(result["replayed"], False)
                                self.assertEqual(result["status"], "started")
                                self.assertEqual(result["run_id"], http.response["run_id"])
                        finally:
                            # Recorder stays installed until the real worker method has completed.
                            release.set()
                            self.assertTrue(put_done.wait(30), "late admission worker did not settle")
                            self.assertTrue(worker_done.wait(30), "admission worker method did not return")
                    # Harness integrity first: a missing boundary is never source evidence.
                    self.assertIsNotNone(state["barrier"], "real final visibility boundary not reached")
                    self.assertTrue(state["barrier"]["live"], "boundary reached after the original deadline (harness timing)")
                    barrier = state["barrier"]
                    self.assertEqual(barrier["disk"]["phase"], "commit")
                    self.assertEqual(barrier["disk"]["kinds"], ["backup", "commit", "guard", "tmp"])
                    self.assertTrue(barrier["disk"]["final"] == old_sha, "final visible before admitted call")
                    self.assertEqual(barrier["rows"], 1)
                    self.assertTrue(barrier["pending"] == [candidate])
                    self.assertTrue(barrier["disk"]["pending_file"])
                    self.assertEqual(state["puts"], [True])
                    self.assertEqual(state["final_calls"], [True], "final call-through not exactly once")
                    self.assertEqual((http.raw_ingress, http.verified_native_effect), (1, 1))
                    if hold:
                        self.assertGreaterEqual(returned, deadline - 0.05, "caller returned before its deadline")
                        self.assertLess(returned - deadline, 3.0, "caller was not bounded")
                        self.assertLess(returned - started, budget + 3.0)
                        self.assertEqual(held["calls"], 0, "worker was not held at final visibility")
                        self.assertFalse(held["put_done"])
                        self.assertFalse(held["worker_done"], "worker settled while the barrier was held")
                        self.assertTrue(held["reached"])
                        self.assertEqual(held["disk"]["phase"], "commit")
                        self.assertTrue(held["disk"]["final"] == old_sha)
                        self.assertEqual(held["disk"]["kinds"], ["backup", "commit", "guard", "tmp"])
                        self.assertTrue(held["disk"]["pending_file"])
                    outcome = state["put_outcome"]
                    self.assertEqual(len(outcome), 1)
                    after = disk(root)
                    if fault is None:
                        self.assertEqual(state["fault_hits"], [])
                        # Valid late durable receipt: complete closure, no rollback demanded.
                        # A late store write never acknowledges success: the original
                        # deadline is re-checked at transaction exit after full closure.
                        self.assertEqual(outcome[0], ("returned", None) if not hold else
                                         ("AdmissionReceiptError", "admission_deadline_exceeded"))
                        self.assertEqual(after["phase"], None)
                        self.assertEqual(after["kinds"], ["backup"])
                        self.assertTrue(sha((root / after["names"][0]).read_bytes()) == old_sha,
                                        "retained prior generation changed")
                        self.assertTrue(after["final"] != old_sha)
                        self.assertEqual(after["pending_file"], hold)
                        self.assertEqual(state["guard_unlinks"], [True])
                        receipts_state(store, late=True, pending_retired=not hold)
                        store.close()
                        with closing(AdmissionReceiptStore(root)) as reopened:
                            receipts_state(reopened, late=True, pending_retired=not hold)
                        self.assertTrue(disk(root)["final"] == after["final"])
                    elif fault == "verify":
                        self.assertEqual(state["flip"].__len__(), 1)
                        self.assertGreaterEqual(len(state["inspect_after"]), 1, "verification seam not reached")
                        self.assertTrue(state["inspect_after"][0] != state["flip"][0],
                                        "verification did not observe the injected fault")
                        self.assertEqual(state["renames"], [(True, True)])
                        self.assertEqual(outcome[0], ("AdmissionReceiptError", "admission_storage_unavailable"))
                        # Real rollback: acknowledged bytes/rows and pending reservation preserved.
                        self.assertEqual(after["phase"], None)
                        self.assertEqual(after["kinds"], [])
                        self.assertTrue(after["final"] == old_sha, "rollback did not restore prior bytes")
                        self.assertTrue(after["pending_file"])
                        receipts_state(store, late=False, pending_retired=False)
                        store.close()
                        with closing(AdmissionReceiptStore(root)) as reopened:
                            receipts_state(reopened, late=False, pending_retired=False)
                        self.assertTrue(disk(root)["final"] == old_sha)
                    else:
                        self.assertEqual(state["fault_hits"], [True])
                        self.assertEqual(state["guard_unlinks"], [True])
                        self.assertEqual(outcome[0], ("AdmissionReceiptError", "admission_deadline_exceeded"))
                        # Committed receipt stays; cleaning journal and guard await recovery.
                        self.assertEqual(after["phase"], "cleaning")
                        self.assertEqual(after["kinds"], ["backup", "guard"])
                        self.assertTrue(after["final"] != old_sha)
                        self.assertTrue(after["pending_file"], "pending retirement started after expiry")
                        store.close()
                        with closing(AdmissionReceiptStore(root)) as reopened:
                            recovered = disk(root)
                            self.assertEqual(recovered["phase"], None)
                            self.assertEqual(recovered["kinds"], [])
                            self.assertTrue(recovered["final"] == after["final"])
                            receipts_state(reopened, late=True, pending_retired=False)
                    self.assertEqual(state["renames"], [(True, True)] if fault == "verify" else [])
                finally:
                    release.set()

        with self.subTest(d01="positive_control_live_deadline_replace_recorded_once"):
            scenario("control", hold=False, fault=None, budget=20)
        with self.subTest(d01="late_visibility_completes_without_caller_success"):
            scenario("late", hold=True, fault=None, budget=6)
        with self.subTest(d01="late_visibility_verification_failure_rolls_back"):
            scenario("verify", hold=True, fault="verify", budget=6)
        with self.subTest(d01="late_visibility_deferred_guard_cleanup_recoverable"):
            scenario("guard", hold=True, fault="guard", budget=6)

    def test_binding_conflict_before_post_but_new_dispatch_key_is_valid(self):
        with genuine_source_fixture() as fx, _managed_loopback() as http, \
                tempfile.TemporaryDirectory() as directory:
            source = _source_for(fx, http.config)
            before = _producer_snapshot(fx)
            client = TargetHttpClient(http.config)

            for index, coordinate in enumerate(("scope", "generation", "profile", "base_url", "fingerprint")):
                path = Path(directory) / str(index)
                with self.subTest(binding_conflict=coordinate), closing(AdmissionReceiptStore(path)) as store:
                    args, _ = _args(fx, source, store, "exact", http.config, budget=30)
                    scope = fx["scope"]
                    generation = int(scope.pairing_generation)
                    profile = http.config.profile
                    base_url = http.config.base_url
                    key = args["idempotency_key"]
                    fingerprint = request_fingerprint(args["body"])
                    conflict_scope = scope
                    conflict_generation = generation
                    conflict_profile = profile
                    conflict_base_url = base_url
                    conflict_fingerprint = fingerprint
                    if coordinate == "scope":
                        conflict_scope = replace(scope, planned_room_id=scope.planned_room_id + "-other")
                    elif coordinate == "generation":
                        conflict_generation = generation + 1
                        conflict_scope = replace(scope, pairing_generation=conflict_generation)
                    elif coordinate == "profile":
                        conflict_profile = "alternate"
                        conflict_scope = replace(scope, target_profile=conflict_profile)
                    elif coordinate == "base_url":
                        conflict_base_url = "http://127.0.0.1:1"
                    else:
                        other_body = copy.deepcopy(args["body"])
                        other_body["hosted_room_dispatch"]["trace_id"] = "trace-prior"
                        conflict_fingerprint = request_fingerprint(other_body)
                    self.assertEqual(key, "room:exact:1")
                    self.assertIsNone(store.reserve(
                        scope=conflict_scope, generation=conflict_generation, profile=conflict_profile,
                        base_url=conflict_base_url, idempotency_key=key,
                        request_fingerprint=conflict_fingerprint, deadline=time.monotonic() + 5))
                    pending_before = _pending(store)
                    self.assertEqual(len(pending_before), 1)

                    with closing(AdmissionReceiptStore(Path(directory) / (str(index) + "-control"))) as control, \
                            mock.patch.object(source, "load", wraps=source.load) as load, \
                            mock.patch.object(AdmissionReceiptStore, "find", autospec=True,
                                              side_effect=AdmissionReceiptStore.find) as find, \
                            mock.patch.object(AdmissionReceiptStore, "reserve", autospec=True,
                                              side_effect=AdmissionReceiptStore.reserve) as reserve:
                        loaded = source.load(scope, credential_ref=args["resolved"].credential_ref,
                                             deadline=time.monotonic() + 30)
                        self.assertTrue(loaded["grant"] == fx["grant"], "M1 own Source.load control")
                        control_key = "room:recorder-control:1"
                        self.assertIsNone(control.find(scope=scope, generation=generation, profile=profile,
                            base_url=base_url, idempotency_key=control_key, request_fingerprint=fingerprint,
                            deadline=time.monotonic() + 30))
                        self.assertIsNone(control.reserve(scope=scope, generation=generation, profile=profile,
                            base_url=base_url, idempotency_key=control_key, request_fingerprint=fingerprint,
                            deadline=time.monotonic() + 30))
                        self.assertEqual((load.call_count, find.call_count, reserve.call_count), (1, 1, 1),
                                         "M1 recorders must work outside public admit_run")
                        control_pending = _pending(control)
                        self.assertEqual(len(control_pending), 1)
                        load.reset_mock(); find.reset_mock(); reserve.reset_mock()
                        self.assertEqual((load.call_count, find.call_count, reserve.call_count), (0, 0, 0))
                        self.assertTrue(_pending(store) == pending_before,
                                        "outside-SUT recorder controls touched the negative store")
                        with self.assertRaises(TargetHttpError) as refusal:
                            client.admit_run(**args)
                        self.assertIn(str(refusal.exception),
                                      ("target_admission_ambiguous", "idempotency_binding_mismatch"))
                        self.assertEqual(load.call_count, 1, "conflict must follow initial genuine authorization")
                        self.assertEqual(find.call_count, 1, "same-key conflict must be inspected")
                        self.assertEqual(reserve.call_count, 1, "same-key conflict must be rejected at reserve")
                        self.assertTrue(_pending(control) == control_pending,
                                        "public conflict changed separate recorder-control store")

                    self.assertEqual((http.raw_ingress, http.verified_native_effect), (0, 0))
                    self.assertEqual(http.posts, [])
                    self.assertEqual(store.count(), 0)
                    self.assertIsNone(store.find(scope=scope, generation=generation, profile=profile,
                                                 base_url=base_url, idempotency_key=key,
                                                 request_fingerprint=fingerprint))
                    self.assertTrue(_pending(store) == pending_before, "conflicting reservation changed")
                    with closing(AdmissionReceiptStore(path)) as reopened:
                        self.assertEqual(reopened.count(), 0)
                        self.assertTrue(_pending(reopened) == pending_before)

                    if coordinate == "fingerprint":
                        # An unrelated pending key must not poison a fresh native dispatch.
                        new_args, _ = _args(fx, source, store, "new-key", http.config, budget=30)
                        self.assertEqual(new_args["idempotency_key"], "room:new-key:1")
                        result = client.admit_run(**new_args)
                        self.assertEqual((result["status"], result["replayed"]), ("started", False))
                        self.assertEqual(result["run_id"], http.response["run_id"])
                        self.assertEqual((http.raw_ingress, http.verified_native_effect), (1, 1))
                        capture = http.native_capture
                        self.assertIsNotNone(capture)
                        assert capture is not None
                        self.assertEqual(http.posts[0][0], capture["raw"])
                        self.assertTrue(http.posts[0][1]["Authorization"] == capture["authorization"])
                        self.assertEqual(http.posts[0][1]["Idempotency-Key"], "room:new-key:1")
                        _receipt_witness(self, fx, http, new_args, store, path)
                        self.assertTrue(_pending(store) == pending_before,
                                        "new dispatch should retire only its own reservation")
                    self.assertTrue(_producer_snapshot(fx) == before, "binding checks changed producer")

            http.close()
            self.assertFalse(http.thread.is_alive())
            self.assertEqual(http.server.fileno(), -1)

    def test_cutover_after_http_and_after_put_never_succeeds(self):
        for when in ("before", "after"):
            with self.subTest(real_cutover=when):
                _cutover_witness(self, when)

    def test_durable_lost_reply_restart_exact_replay(self):
        with genuine_source_fixture() as fx, _managed_loopback() as http, \
                tempfile.TemporaryDirectory() as directory:
            source = _source_for(fx, http.config)
            path = Path(directory) / "admissions"
            first_store = AdmissionReceiptStore(path)
            client = TargetHttpClient(http.config)
            before = _producer_snapshot(fx)
            first_args, _ = _args(fx, source, first_store, "restart", http.config, budget=30)
            scope = fx["scope"]
            generation = int(scope.pairing_generation)
            profile = http.config.profile
            base_url = http.config.base_url
            key = first_args["idempotency_key"]
            fingerprint = request_fingerprint(first_args["body"])
            lost_ack = []

            class CallerReplyLost(Exception):
                pass

            original_admit = client.admit_run

            def drop_caller_reply(**kwargs):
                result = original_admit(**kwargs)
                self.assertEqual((result["status"], result["replayed"]), ("started", False))
                self.assertEqual((http.raw_ingress, http.verified_native_effect), (1, 1))
                committed = first_store.find(scope=scope, generation=generation, profile=profile,
                    base_url=base_url, idempotency_key=key, request_fingerprint=fingerprint)
                self.assertIsNotNone(committed, "public success must have committed before the caller reply is dropped")
                assert committed is not None
                self.assertTrue(committed["run_id"] == result["run_id"], "lost reply receipt did not match the public run")
                self.assertEqual(first_store.count(), 1)
                self.assertEqual(_pending(first_store), [])
                lost_ack.append(True)
                raise CallerReplyLost("test-only caller acknowledgement loss after durable commit")

            with closing(first_store):
                with mock.patch.object(first_store, "put", wraps=first_store.put) as first_put:
                    with mock.patch.object(client, "admit_run", side_effect=drop_caller_reply):
                        with self.assertRaises(CallerReplyLost):
                            client.admit_run(**first_args)
                    self.assertEqual(lost_ack, [True])
                    self.assertEqual(first_put.call_count, 1, "the first public attempt must durably publish once")
                first_receipt = first_store.find(scope=scope, generation=generation, profile=profile,
                    base_url=base_url, idempotency_key=key, request_fingerprint=fingerprint)
                self.assertIsNotNone(first_receipt)
                assert first_receipt is not None
                self.assertEqual(first_store.count(), 1)
                self.assertEqual(_pending(first_store), [])
                first_capture = http.native_capture
                self.assertIsNotNone(first_capture)
                assert first_capture is not None
                self.assertEqual(len(http.posts), 1)
                self.assertTrue(http.posts[0][0] == first_capture["raw"],
                                "initial public POST changed native captured bytes")
                self.assertTrue(http.posts[0][1]["Authorization"] == first_capture["authorization"],
                                "initial public POST changed native Authorization")
                self.assertTrue(http.posts[0][1]["Idempotency-Key"] == first_capture["key"],
                                "initial public POST changed native idempotency key")
                self.assertTrue(http.paths == [first_capture["route"]],
                                "initial public POST changed native route")

            # Fresh client and freshly opened store model restart after the durable
            # receipt exists; the caller's first result was deliberately discarded.
            reopened = AdmissionReceiptStore(path)
            replay_client = TargetHttpClient(http.config)
            second_args, _ = _args(fx, source, reopened, "restart", http.config, budget=30)
            self.assertEqual(second_args["idempotency_key"], key)
            second_capture = http.native_capture
            self.assertIsNotNone(second_capture)
            assert second_capture is not None
            self.assertTrue(second_args["forwarded_body"] == first_capture["raw"],
                            "restart retry must reuse exact native bytes")
            self.assertTrue(second_args["forwarded_body"] == second_capture["raw"])
            http.response = {"run_id": first_receipt["run_id"], "status": "running", "replayed": True}

            original_find, original_reserve = reopened.find, reopened.reserve
            calls = []
            find_results = []
            reserve_results = []

            def observed_find(**kwargs):
                value = original_find(**kwargs)
                calls.append("find")
                find_results.append(value)
                return value

            def observed_reserve(**kwargs):
                value = original_reserve(**kwargs)
                calls.append("reserve")
                reserve_results.append(value)
                return value

            with closing(reopened):
                with mock.patch.object(reopened, "find", side_effect=observed_find) as find_spy, \
                        mock.patch.object(reopened, "reserve", side_effect=observed_reserve) as reserve_spy:
                    # Positive controls prove both real store recorders work before reset.
                    self.assertTrue(reopened.find(scope=scope, generation=generation, profile=profile,
                        base_url=base_url, idempotency_key=key, request_fingerprint=fingerprint,
                        deadline=time.monotonic() + 5) == first_receipt,
                        "fresh-store find recorder must observe the durable prior")
                    self.assertTrue(reopened.reserve(scope=scope, generation=generation, profile=profile,
                        base_url=base_url, idempotency_key=key, request_fingerprint=fingerprint,
                        deadline=time.monotonic() + 5) == first_receipt,
                        "fresh-store reserve recorder must observe the same committed prior")
                    self.assertEqual(calls, ["find", "reserve"])
                    self.assertEqual((find_spy.call_count, reserve_spy.call_count), (1, 1))
                    self.assertEqual(len(find_results), 1)
                    self.assertTrue(find_results[0] == first_receipt)
                    self.assertEqual(len(reserve_results), 1)
                    self.assertTrue(reserve_results[0] == first_receipt)
                    self.assertEqual(_pending(reopened), [])
                    calls.clear(); find_results.clear(); reserve_results.clear()
                    find_spy.reset_mock(); reserve_spy.reset_mock()

                    with mock.patch.object(reopened, "put", wraps=reopened.put) as replay_put:
                        replay = replay_client.admit_run(**second_args)
                        self.assertEqual(replay_put.call_count, 0, "durable replay must not write a second receipt")
                    self.assertEqual(calls, ["find"], "reopened durable prior must take the exact find-hit replay path")
                    self.assertEqual((find_spy.call_count, reserve_spy.call_count), (1, 0))
                    self.assertEqual(len(find_results), 1)
                    self.assertTrue(find_results[0] == first_receipt)
                    self.assertEqual(reserve_results, [])
                    self.assertEqual((http.raw_ingress, http.verified_native_effect), (1, 1))
                    self.assertEqual(len(http.posts), 1)
                    self.assertTrue(http.posts[0][0] == first_capture["raw"])
                    self.assertTrue(http.posts[0][1]["Authorization"] == first_capture["authorization"])
                    self.assertEqual(http.posts[0][1]["Idempotency-Key"], first_capture["key"])
                    self.assertEqual(http.paths, [first_capture["route"]])
                    self.assertEqual(reopened.count(), 1)
                    self.assertTrue(original_find(scope=scope, generation=generation, profile=profile,
                        base_url=base_url, idempotency_key=key,
                        request_fingerprint=fingerprint) == first_receipt)
                    self.assertEqual(_pending(reopened), [])

            self.assertEqual((replay["status"], replay["replayed"]), ("running", True))
            self.assertTrue(replay["run_id"] == first_receipt["run_id"], "restart replay changed run_id")
            with closing(AdmissionReceiptStore(path)) as final_reopen:
                self.assertEqual(final_reopen.count(), 1)
                self.assertTrue(final_reopen.find(scope=scope, generation=generation, profile=profile,
                    base_url=base_url, idempotency_key=key,
                    request_fingerprint=fingerprint) == first_receipt,
                    "fresh post-replay reopen changed the original durable receipt")
                self.assertEqual(_pending(final_reopen), [])
            self.assertTrue(_producer_snapshot(fx) == before, "restart/replay changed source producer or mint state")
            http.close()
            self.assertFalse(http.thread.is_alive())
            self.assertEqual(http.server.fileno(), -1)

    def test_earlier_context_deadline_reaches_effecting_worker(self):
        with genuine_source_fixture() as fx, _managed_loopback() as http, tempfile.TemporaryDirectory() as directory, ExitStack() as resources:
            source = _source_for(fx, http.config)
            path = Path(directory) / "admissions"
            store = resources.enter_context(closing(AdmissionReceiptStore(path)))
            args, _ = _args(fx, source, store, "earlier-context", http.config, budget=5)
            explicit = args["deadline"]
            inherited = explicit - 1.0
            before = _producer_snapshot(fx)
            executor = __import__("invite_rooms.peer_registry", fromlist=["admission_executor"]).admission_executor()
            reservation_reached = threading.Event()
            release_reservation = threading.Event()
            worker_done = threading.Event()
            executor_boundaries = []
            worker_boundaries = []
            load_boundaries = []
            reserve_boundaries = []
            original_run = executor.run
            original_worker = TargetHttpClient._admit_source_worker
            original_load = source.load
            original_reserve = store.reserve

            async def observed_run(fn, *positional, **kwargs):
                executor_boundaries.append({
                    "worker_deadline": positional[0] if positional else None,
                    "end_deadline": kwargs.get("end_deadline"),
                })
                return await original_run(fn, *positional, **kwargs)

            def observed_worker(client, **kwargs):
                worker_boundaries.append({
                    "argument": kwargs["deadline"],
                    "context": ADMISSION_DEADLINE.get(),
                })
                try:
                    return original_worker(client, **kwargs)
                finally:
                    worker_done.set()

            def observed_load(*load_args, **kwargs):
                load_boundaries.append({
                    "deadline": kwargs.get("deadline"),
                    "context": ADMISSION_DEADLINE.get(),
                })
                return original_load(*load_args, **kwargs)

            def observed_reserve(**kwargs):
                value = original_reserve(**kwargs)
                reserve_boundaries.append({
                    "deadline": kwargs.get("deadline"),
                    "context": ADMISSION_DEADLINE.get(),
                })
                reservation_reached.set()
                if not release_reservation.wait(10):
                    raise AssertionError("test reservation barrier was not released")
                return value

            previous_context = ADMISSION_DEADLINE.get()
            token = ADMISSION_DEADLINE.set(inherited)
            try:
                try:
                    with mock.patch.object(executor, "run", side_effect=observed_run), mock.patch.object(TargetHttpClient, "_admit_source_worker", new=observed_worker), mock.patch.object(source, "load", side_effect=observed_load), mock.patch.object(store, "reserve", side_effect=observed_reserve):
                        with self.assertRaisesRegex(TargetHttpError, "^target_admission_ambiguous$"):
                            TargetHttpClient(http.config).admit_run(**args)
                finally:
                    ADMISSION_DEADLINE.reset(token)
                self.assertIs(ADMISSION_DEADLINE.get(), previous_context)
                self.assertTrue(reservation_reached.wait(2), "real reservation boundary was not reached")
                self.assertFalse(worker_done.is_set(), "physical worker was not retained at caller timeout")
            finally:
                release_reservation.set()
                self.assertTrue(worker_done.wait(10), "physical worker did not quiesce")

            self.assertIs(ADMISSION_DEADLINE.get(), previous_context)
            self.assertTrue(executor_boundaries, "executor boundary was not observed")
            self.assertTrue(worker_boundaries, "physical worker boundary was not observed")
            self.assertTrue(load_boundaries, "genuine Source.load boundary was not observed")
            self.assertTrue(reserve_boundaries, "real reserve boundary was not observed")
            self.assertEqual(executor_boundaries[0]["end_deadline"], inherited)
            self.assertEqual(executor_boundaries[0]["worker_deadline"], inherited)
            self.assertEqual(worker_boundaries[0]["argument"], inherited)
            self.assertEqual(worker_boundaries[0]["context"], inherited)
            self.assertEqual(load_boundaries[0]["deadline"], inherited)
            self.assertEqual(load_boundaries[0]["context"], inherited)
            self.assertEqual(reserve_boundaries[0]["deadline"], inherited)
            self.assertEqual(reserve_boundaries[0]["context"], inherited)
            self.assertGreater(explicit, inherited)
            self.assertEqual((http.raw_ingress, http.verified_native_effect, len(http.posts)), (0, 0, 0))
            self.assertEqual(store.count(), 0)
            self.assertEqual(len(_pending(store)), 1, "real reserve pending row was not retained")
            self.assertEqual(_producer_snapshot(fx), before)
            self.assertFalse(worker_done.is_set() is False, "owned worker remained before teardown")
            self.assertEqual((http.raw_ingress, http.verified_native_effect, len(http.posts)), (0, 0, 0))

    def test_executor_retains_capacity_until_worker_closure(self):
        import asyncio
        from invite_rooms.peer_registry import AdmissionExecutor, LANE_CALLBACK, PeerRegistryError

        async def wait_for_event(event, label):
            reached = await asyncio.to_thread(event.wait, 10)
            self.assertTrue(reached, label)

        async def witness_capacity():
            executor = AdmissionExecutor(workers=2, queue_cap=0, reserved=0)
            started = (threading.Event(), threading.Event())
            release = (threading.Event(), threading.Event())
            completed = (threading.Event(), threading.Event())
            probe_ran = threading.Event()

            def worker(index):
                started[index].set()
                try:
                    self.assertTrue(release[index].wait(10), "worker release barrier was not reached")
                    return index
                finally:
                    completed[index].set()

            def probe():
                probe_ran.set()

            first = second = None
            try:
                first = asyncio.create_task(executor.run(worker, 0))
                second = asyncio.create_task(executor.run(worker, 1))
                await asyncio.gather(*(wait_for_event(event, "material worker did not start") for event in started))

                first.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await first
                self.assertFalse(completed[0].is_set() or completed[1].is_set(),
                                 "caller cancellation released physical worker capacity")

                with self.assertRaisesRegex(PeerRegistryError, "^admission_overloaded$"):
                    await executor.run(probe, end_deadline=time.monotonic() + 1)
                self.assertFalse(probe_ran.is_set(),
                                 "over-capacity probe ran after caller cancellation")
            finally:
                release[0].set()
                release[1].set()
                await asyncio.gather(*(asyncio.shield(task) for task in (first, second)
                                       if task is not None and not task.done()),
                                     return_exceptions=True)
                await asyncio.gather(*(wait_for_event(event, "physical worker did not complete")
                                       for event in completed))
                executor.shutdown(wait=True)

        asyncio.run(witness_capacity())

        async def witness_nested_refusal():
            executor = AdmissionExecutor(workers=1, queue_cap=0, reserved=0)
            callback_effect = threading.Event()
            worker_done = threading.Event()

            def callback():
                callback_effect.set()

            def nested_worker():
                try:
                    for deadline, expected in (
                            (time.monotonic() - 1, "admission_deadline_exceeded"),
                            (time.monotonic() + 5, "admission_overloaded")):
                        with self.assertRaisesRegex(PeerRegistryError, f"^{expected}$"):
                            asyncio.run(executor.run(callback, lane=LANE_CALLBACK, end_deadline=deadline))
                        self.assertFalse(callback_effect.is_set(),
                                         "refused nested callback caused a side effect")
                finally:
                    worker_done.set()

            try:
                await executor.run(nested_worker)
                self.assertTrue(worker_done.is_set(), "nested refusal worker did not settle")
                self.assertFalse(callback_effect.is_set(), "nested callback side effect escaped refusal")
            finally:
                executor.shutdown(wait=True)

        asyncio.run(witness_nested_refusal())

    def test_expiry_and_cutover_after_reserve_before_send(self):
        from invite_rooms import target_http_io
        from invite_rooms.target_material import TargetMaterialSourceError

        with self.subTest(boundary="expiry-after-reserve"):
            with genuine_source_fixture() as fx, _managed_loopback() as http, \
                    tempfile.TemporaryDirectory() as directory, ExitStack() as resources:
                source = _source_for(fx, http.config)
                path = Path(directory) / "expiry"
                store = resources.enter_context(closing(AdmissionReceiptStore(path)))
                args, _ = _args(fx, source, store, "expiry-after-reserve", http.config, budget=3)
                before = _producer_snapshot(fx)
                deadline = args["deadline"]
                reserve_reached = threading.Event()
                post_reserve_loaded = threading.Event()
                release_worker = threading.Event()
                worker_done = threading.Event()
                original_reserve = store.reserve
                original_load = source.load
                original_worker = TargetHttpClient._admit_source_worker
                original_post = target_http_io.post_loopback
                load_calls = [0]
                engine_entries = []

                def observed_post(url, headers, payload, *, deadline=None):
                    engine_entries.append({
                        "expired_at_entry": deadline is not None and time.monotonic() >= deadline,
                    })
                    return original_post(url, headers, payload, deadline=deadline)

                recorder_patch = mock.patch.object(target_http_io, "post_loopback", side_effect=observed_post)
                recorder_patch.start()
                with self.assertRaisesRegex(ValueError, "^invalid target$"):
                    target_http_io.post_loopback(
                        "http://example.invalid/v1/runs", {}, b"{}",
                        deadline=time.monotonic() + 5)
                self.assertEqual(len(engine_entries), 1, "engine recorder did not observe control")
                self.assertFalse(engine_entries[0]["expired_at_entry"],
                                 "engine control entered with an expired deadline")
                engine_entries.clear()

                def observed_reserve(**kwargs):
                    value = original_reserve(**kwargs)
                    self.assertLess(time.monotonic(), deadline)
                    reserve_reached.set()
                    return value

                def observed_load(*load_args, **kwargs):
                    load_calls[0] += 1
                    value = original_load(*load_args, **kwargs)
                    if load_calls[0] == 2:
                        self.assertLess(time.monotonic(), deadline)
                        post_reserve_loaded.set()
                        self.assertTrue(release_worker.wait(10), "post-reserve release barrier was not reached")
                    return value

                def observed_worker(client, **kwargs):
                    try:
                        return original_worker(client, **kwargs)
                    finally:
                        worker_done.set()

                try:
                    with mock.patch.object(store, "reserve", side_effect=observed_reserve), \
                            mock.patch.object(source, "load", side_effect=observed_load), \
                            mock.patch.object(TargetHttpClient, "_admit_source_worker", new=observed_worker):
                        with self.assertRaisesRegex(TargetHttpError, "^target_admission_ambiguous$"):
                            TargetHttpClient(http.config).admit_run(**args)
                    self.assertTrue(reserve_reached.is_set(), "real reserve boundary was not reached")
                    self.assertTrue(post_reserve_loaded.is_set(), "fresh post-reserve Source.load was not reached")
                    self.assertGreaterEqual(time.monotonic(), deadline)
                    self.assertFalse(worker_done.is_set(), "worker closed before caller budget expired")
                    self.assertEqual((http.raw_ingress, http.verified_native_effect, len(http.posts)), (0, 0, 0))
                    self.assertEqual(len(engine_entries), 0, "expired worker entered the POST engine before release")
                    self.assertEqual(store.count(), 0)
                    self.assertEqual(len(_pending(store)), 1)
                    self.assertEqual(_producer_snapshot(fx), before)
                finally:
                    release_worker.set()
                    self.assertTrue(worker_done.wait(10), "physical worker did not quiesce")
                self.assertEqual((http.raw_ingress, http.verified_native_effect, len(http.posts)), (0, 0, 0))
                self.assertEqual(len(engine_entries), 0, "expired worker entered the POST engine after release")
                self.assertEqual(store.count(), 0)
                self.assertEqual(len(_pending(store)), 1)
                with closing(AdmissionReceiptStore(path)) as reopened:
                    self.assertEqual(reopened.count(), 0)
                    self.assertEqual(len(_pending(reopened)), 1)
                self.assertEqual(_producer_snapshot(fx), before)
                recorder_patch.stop()

        with self.subTest(boundary="cutover-after-reserve"):
            with genuine_source_fixture() as fx, _managed_loopback() as http, \
                    tempfile.TemporaryDirectory() as directory, ExitStack() as resources:
                source = _source_for(fx, http.config)
                path = Path(directory) / "cutover"
                store = resources.enter_context(closing(AdmissionReceiptStore(path)))
                control = source.load(fx["scope"], credential_ref=fx["credential_ref"],
                                     deadline=time.monotonic() + 5)
                self.assertTrue(control["grant"] == fx["grant"])
                args, _ = _args(fx, source, store, "cutover-after-reserve", http.config, budget=5)
                before = _producer_snapshot(fx)
                import_row = fx["fx"]["joining_store"].find_import(
                    fx["scope"].lobby_id, fx["scope"].invite_id)
                import_id = import_row["import_id"]
                reserve_reached = threading.Event()
                reload_attempted = threading.Event()
                reload_refused = threading.Event()
                worker_done = threading.Event()
                original_reserve = store.reserve
                original_load = source.load
                original_worker = TargetHttpClient._admit_source_worker
                load_calls = [0]

                def observed_reserve(**kwargs):
                    value = original_reserve(**kwargs)
                    fx["fx"]["joining_store"].mark_left(import_id)
                    reserve_reached.set()
                    return value

                def observed_load(*load_args, **kwargs):
                    load_calls[0] += 1
                    if load_calls[0] == 2:
                        reload_attempted.set()
                    try:
                        return original_load(*load_args, **kwargs)
                    except TargetMaterialSourceError:
                        if load_calls[0] == 2:
                            reload_refused.set()
                        raise

                def observed_worker(client, **kwargs):
                    try:
                        return original_worker(client, **kwargs)
                    finally:
                        worker_done.set()

                try:
                    with mock.patch.object(store, "reserve", side_effect=observed_reserve), \
                            mock.patch.object(source, "load", side_effect=observed_load), \
                            mock.patch.object(TargetHttpClient, "_admit_source_worker", new=observed_worker):
                        with self.assertRaisesRegex(TargetHttpError, "^target_admission_ambiguous$"):
                            TargetHttpClient(http.config).admit_run(**args)
                    self.assertTrue(reserve_reached.is_set(), "real reserve boundary was not reached")
                    self.assertTrue(reload_attempted.is_set(), "fresh post-reserve Source.load was not attempted")
                    self.assertTrue(reload_refused.is_set(), "real post-reserve Source.load was not refused")
                    self.assertTrue(worker_done.wait(10), "physical worker did not quiesce")
                    self.assertEqual((http.raw_ingress, http.verified_native_effect, len(http.posts)), (0, 0, 0))
                    self.assertEqual(store.count(), 0)
                    self.assertEqual(len(_pending(store)), 1)
                    self.assertEqual(_producer_snapshot(fx), before)
                finally:
                    self.assertTrue(worker_done.wait(10), "physical worker did not quiesce")
                with closing(AdmissionReceiptStore(path)) as reopened:
                    self.assertEqual(reopened.count(), 0)
                    self.assertEqual(len(_pending(reopened)), 1)
                self.assertEqual(_producer_snapshot(fx), before)

    def test_expiry_before_stage_backup_guard_commit_and_visibility_is_recoverable(self):
        from invite_rooms import admission_receipts as receipts_module
        from invite_rooms.admission_receipts import AdmissionReceiptError
        from test_admission_publication_recovery_w2d import receipt
        from test_target_http_w2d_card1 import Scope

        phases = ("before_stage_open", "after_stage_witness", "after_backup_copy",
                  "after_guard_link", "after_commit_link")
        real_open = receipts_module.os.open
        real_fsync = receipts_module.os.fsync
        real_replace = receipts_module.os.replace

        for phase in phases:
            with self.subTest(publication_phase=phase), tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / "admissions"
                store = AdmissionReceiptStore(root)
                old = receipt(Scope(), 1)
                new = receipt(Scope(), 2)
                old_lookup = {key: old[key] for key in
                              ("scope", "generation", "profile", "base_url",
                               "idempotency_key", "request_fingerprint")}
                new_lookup = {key: new[key] for key in
                              ("scope", "generation", "profile", "base_url",
                               "idempotency_key", "request_fingerprint")}
                worker = None
                reached = threading.Event()
                release = threading.Event()
                worker_done = threading.Event()
                outcome = []
                barriers = []
                stage_fd = [None]
                stage_witnesses = []
                final_visibility = []
                deadline = time.monotonic() + 1.5

                def state_snapshot():
                    journal = b""
                    if store._journal_fd is not None:
                        journal = receipts_module.os.pread(store._journal_fd, 4097, 0).rstrip(b" ")
                    record = (receipts_module.AdmissionReceiptStore._parse_journal(journal)
                              if journal and journal != receipts_module._MARKER else None)
                    artifacts = sorted(item.name for item in root.iterdir()
                                       if item.name.startswith(".") and
                                       item.name.endswith((".tmp", ".commit", ".backup", ".guard")))
                    return {"phase": None if record is None else record["phase"],
                            "artifacts": artifacts,
                            "stage_exists": any(name.endswith(".tmp") for name in artifacts),
                            "backup_absent": not any(name.endswith(".backup") for name in artifacts)}

                def hold(label):
                    self.assertLess(time.monotonic(), deadline,
                                    "publication barrier was reached after deadline")
                    barriers.append(label)
                    reached.set()
                    if not release.wait(10):
                        raise AssertionError("publication barrier was not released")

                real_drop_retained = store._drop_retained

                def dropped(name):
                    value = real_drop_retained(name)
                    if phase == "before_stage_open":
                        snapshot = state_snapshot()
                        self.assertIsNone(snapshot["phase"])
                        self.assertEqual(snapshot["artifacts"], [])
                        self.assertFalse(snapshot["stage_exists"])
                        self.assertTrue(snapshot["backup_absent"])
                        hold("before_stage_open")
                    return value

                def opened(path, flags, *args, **kwargs):
                    name = str(path)
                    fd = real_open(path, flags, *args, **kwargs)
                    if name.endswith(".tmp"):
                        stage_fd[0] = fd
                    return fd

                def synced(fd):
                    result = real_fsync(fd)
                    if fd == stage_fd[0] and phase == "after_stage_witness" and not reached.is_set():
                        snapshot = state_snapshot()
                        stage_witnesses.append(snapshot)
                        self.assertEqual(snapshot["phase"], "stage")
                        self.assertTrue(snapshot["stage_exists"])
                        self.assertTrue(snapshot["backup_absent"])
                        hold("after_stage_witness")
                    return result

                def copy(source, target, expected):
                    value = real_copy(source, target, expected)
                    if phase == "after_backup_copy":
                        snapshot = state_snapshot()
                        self.assertEqual(snapshot["phase"], "stage")
                        self.assertTrue(snapshot["stage_exists"])
                        self.assertFalse(snapshot["backup_absent"])
                        hold("after_backup_copy")
                    return value

                def linked(source, target, *args, **kwargs):
                    value = real_link(source, target, *args, **kwargs)
                    name = str(target)
                    if phase == "after_guard_link" and name.endswith(".guard"):
                        snapshot = state_snapshot()
                        self.assertEqual(snapshot["phase"], "backup")
                        self.assertTrue(any(name.endswith(".guard") for name in snapshot["artifacts"]))
                        hold("after_guard_link")
                    elif phase == "after_commit_link" and name.endswith(".commit"):
                        snapshot = state_snapshot()
                        self.assertEqual(snapshot["phase"], "guard")
                        self.assertTrue(any(name.endswith(".commit") for name in snapshot["artifacts"]))
                        hold("after_commit_link")
                    return value

                def replaced(source, target, *args, **kwargs):
                    name = str(source)
                    if name.endswith(".commit") and str(target) == "admission_receipts.json.enc":
                        final_visibility.append((name, str(target)))
                    return real_replace(source, target, *args, **kwargs)

                def publish():
                    try:
                        store.put(**new, deadline=deadline)
                    except BaseException as error:
                        outcome.append(error)
                    finally:
                        worker_done.set()

                real_copy = store._copy_snapshot
                real_link = receipts_module.os.link
                store.put(**old, deadline=time.monotonic() + 10)
                old_bytes = store.path.read_bytes()
                self.assertEqual(store.count(), 1)
                deadline = time.monotonic() + 1.5
                try:
                    with mock.patch.object(receipts_module.os, "open", side_effect=opened), \
                            mock.patch.object(receipts_module.os, "fsync", side_effect=synced), \
                            mock.patch.object(store, "_drop_retained", side_effect=dropped), \
                            mock.patch.object(store, "_copy_snapshot", side_effect=copy), \
                            mock.patch.object(receipts_module.os, "link", side_effect=linked), \
                            mock.patch.object(receipts_module.os, "replace", side_effect=replaced):
                        worker = threading.Thread(target=publish)
                        worker.start()
                        self.assertTrue(reached.wait(5),
                                        "named publication barrier was not reached: " +
                                        (str(outcome[0]) if outcome else "worker still running"))
                        while time.monotonic() < deadline:
                            time.sleep(0.01)
                        release.set()
                        self.assertTrue(worker_done.wait(10), "publication worker did not settle")
                        worker.join(10)
                        self.assertFalse(worker.is_alive(), "publication worker remained alive")
                finally:
                    release.set()
                    if worker is not None:
                        worker.join(10)
                        self.assertFalse(worker.is_alive(), "publication worker remained alive in cleanup")
                    store.close()

                self.assertEqual(barriers, [phase])
                self.assertEqual(len(outcome), 1)
                self.assertIs(type(outcome[0]), AdmissionReceiptError)
                self.assertEqual(str(outcome[0]), "admission_deadline_exceeded")
                self.assertEqual(final_visibility, [])
                self.assertTrue(store.path.read_bytes() == old_bytes)
                with closing(AdmissionReceiptStore(root)) as reopened:
                    self.assertEqual(reopened.count(), 1)
                    self.assertIsNotNone(reopened.find(**old_lookup))
                    self.assertIsNone(reopened.find(**new_lookup))
                    self.assertTrue(reopened.path.read_bytes() == old_bytes)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "positive"
            store = AdmissionReceiptStore(root)
            old = receipt(Scope(), 1)
            new = receipt(Scope(), 2)
            final_visibility = []
            real_replace = receipts_module.os.replace

            def replaced(source, target, *args, **kwargs):
                if str(source).endswith(".commit") and str(target) == "admission_receipts.json.enc":
                    final_visibility.append((str(source), str(target)))
                return real_replace(source, target, *args, **kwargs)

            try:
                with mock.patch.object(receipts_module.os, "replace", side_effect=replaced):
                    store.put(**old, deadline=time.monotonic() + 10)
                    store.put(**new, deadline=time.monotonic() + 10)
                self.assertEqual(len(final_visibility), 1)
                self.assertEqual(store.count(), 2)
            finally:
                store.close()
            with closing(AdmissionReceiptStore(root)) as reopened:
                self.assertEqual(reopened.count(), 2)

    def test_inherited_budget_mutex_and_process_flock(self):
        self.assertTrue(hasattr(AdmissionReceiptStore, "_acquire_flock"))

    def test_mc_and_core_fingerprint_domains_preserved_for_unicode(self):
        import asyncio
        import sys
        from gateway import hosted_room_peer, hosted_rooms
        from gateway.hosted_room_peer import decode_room_grant
        from gateway.platforms import api_server, api_server_room_grants, api_server_runs
        from types import SimpleNamespace

        with genuine_source_fixture() as fx, tempfile.TemporaryDirectory() as directory:
            scope = fx["scope"]
            config = TargetHttpConfig("http://127.0.0.1:19001", "default")
            capture = SimpleNamespace(native_capture=None)
            with mock.patch.object(sys.modules[__name__], "_ACTIVE_LOOPBACK", capture):
                body, raw, key, checked = _native_request(fx, "fingerprint-domain", config)
            native_capture = capture.native_capture
            self.assertIsNotNone(native_capture)
            assert native_capture is not None
            self.assertTrue(body["input"] == "R02 café e\u0301 🧪", "native fixture lost Unicode")
            self.assertTrue(raw == native_capture["raw"] and key == native_capture["key"],
                            "native formatter capture changed")

            # The real Core grant validator reads a scratch reservation, never production state.
            core_db = Path(directory) / "core-room-state.db"
            claims = decode_room_grant(fx["native_secret"], fx["grant"], permission="dispatch")
            hosted_rooms.reserve_peer_room(core_db, claims=claims, expires_at=float(claims["expires_at"]))
            self.assertTrue(hosted_rooms.peer_room_grant_is_current(core_db, claims=claims),
                            "real scratch Core reservation control failed")
            catalog_map = copy.deepcopy(fx["flow"].b_side.catalog)
            policy_map = catalog_map["execution_policy"]

            class CoreRequest(dict):
                path = "/v1/runs"
                method = "POST"

                @property
                def headers(self):
                    return self["headers"]

            request = CoreRequest(headers={
                "Authorization": native_capture["authorization"], "Idempotency-Key": key})
            adapter = api_server.APIServerAdapter.__new__(api_server.APIServerAdapter)

            async def prepared_session(dispatch):
                self.assertTrue(dispatch == checked, "Core normalizer changed the native dispatch")
                return "roomlink_capture"  # same deterministic prepare hook used by _native_request

            profile_token = api_server._api_request_profile.set(scope.target_profile)
            try:
                with mock.patch.object(hosted_rooms, "default_db_path", return_value=core_db), \
                        mock.patch.object(hosted_rooms, "local_authority_gateway_id",
                                          return_value=scope.target_install_id), \
                        mock.patch.object(api_server_room_grants, "_local_room_catalog",
                                          return_value=(policy_map, catalog_map)), \
                        mock.patch.object(hosted_room_peer, "gateway_room_grant_secret",
                                          return_value=fx["native_secret"]), \
                        mock.patch.object(adapter, "_ensure_hosted_member_session", new=prepared_session):
                    normalized, error = asyncio.run(adapter._normalize_room_dispatch(request, body))
                    self.assertIsNone(error, "actual Core normalizer rejected genuine scratch authority")
                    self.assertTrue(normalized["input"] == body["input"])
                    self.assertTrue(normalized["hosted_room_dispatch"] == body["hosted_room_dispatch"])
                    self.assertTrue(normalized["session_id"] == "roomlink_capture")
                    self.assertTrue("_room_execution_policy" in normalized)
                    core_scope, core_normalized = api_server_runs._run_idempotency_identity(
                        adapter, request, normalized, "", key)
                    core_raw_scope, core_raw = api_server_runs._run_idempotency_identity(
                        adapter, request, body, "", key)
            finally:
                api_server._api_request_profile.reset(profile_token)

            mc_source = request_fingerprint(body, gateway_session_key="")
            mc_normalized = request_fingerprint(normalized, gateway_session_key="")
            self.assertTrue(type(core_scope) is str and core_scope == core_raw_scope and bool(core_scope),
                            "actual Core request identity scope was not stable")
            self.assertTrue(len(core_normalized) == len(core_raw) == 64,
                            "actual Core identity helper did not return fingerprints")
            self.assertTrue(mc_source != mc_normalized,
                            "MC Unicode serializer did not distinguish source and normalized bodies")
            self.assertTrue(core_raw != core_normalized,
                            "Core Unicode serializer did not distinguish source and normalized bodies")
            self.assertTrue(mc_source != core_raw,
                            "MC and Core Unicode serializer domains unexpectedly collapsed")
            self.assertTrue(mc_normalized != core_normalized,
                            "MC and Core Unicode fingerprint domains collapsed on identical normalized body")


    def test_no_receipt_remote_replay_remains_ambiguous(self):
        self.assertTrue(hasattr(TargetHttpClient, "_admit_source_worker"))

    def test_pending_retirement_expiry_skips_new_effect_but_keeps_integrity_checks(self):
        import sys
        from invite_rooms import admission_receipts as receipts_module

        # D02: visibility is ADMITTED while the original deadline is live (real
        # os.replace(commit, final) held on a test event until the deadline
        # expires).  After expiry the closure must skip NEW pending unlink/rewrite
        # but still run the receipt-integrity check; a really damaged final
        # receipt must still roll back receipts AND pending bytes.
        final_name = "admission_receipts.json.enc"
        lock_name = "admission_receipts.lock"
        pending_name = "admission_pending.json.enc"
        real_replace = receipts_module.os.replace
        real_unlink = receipts_module.os.unlink
        real_open = receipts_module.os.open
        pending_keys = {"scope", "generation", "profile", "base_url",
                        "idempotency_key", "request_fingerprint"}
        receipt_keys = pending_keys | {"version", "run_id", "status", "replayed"}

        def sha(data):
            return hashlib.sha256(data).hexdigest()

        def disk(root):
            raw = (root / lock_name).read_bytes().rstrip(b" ")
            phase = None if len(raw) <= 1 else AdmissionReceiptStore._parse_journal(raw)["phase"]
            names = sorted(item.name for item in root.iterdir()
                           if item.name.startswith(".") and
                           item.name.endswith((".tmp", ".commit", ".backup", ".guard")))
            pending_path = root / pending_name
            return {"phase": phase, "kinds": sorted(name.rsplit(".", 1)[1] for name in names),
                    "final": sha((root / final_name).read_bytes()),
                    "pending_file": pending_path.exists(),
                    "pending_sha": sha(pending_path.read_bytes()) if pending_path.exists() else None}

        def scenario(label, *, hold, fault, budget, retain_unrelated=False):
            with genuine_source_fixture() as fx, _managed_loopback() as http, \
                    tempfile.TemporaryDirectory() as directory, ExitStack() as resources:
                source = _source_for(fx, http.config)
                root = Path(directory) / "admissions"
                store = resources.enter_context(closing(AdmissionReceiptStore(root)))
                prior_args, _ = _args(fx, source, store, "prior", http.config, budget=20)
                self.assertIs(TargetHttpClient(http.config).admit_run(**prior_args)["replayed"], False)
                prior_lookup = _lookup(fx, http, prior_args)
                prior_found = _receipt_witness(self, fx, http, prior_args, store, root)
                old_sha = sha((root / final_name).read_bytes())
                self.assertEqual(store.count(), 1)
                self.assertFalse((root / pending_name).exists())

                other_candidate = None
                if retain_unrelated:
                    other_args, _ = _args(fx, source, store, label + "-retained", http.config,
                                          budget=20, clear_capture=False)
                    other_lookup = _lookup(fx, http, other_args)
                    other_candidate = dict(other_lookup, scope=fx["scope"].as_dict())
                    other_generation = fx["scope"].pairing_generation
                    self.assertIs(type(other_generation), int)
                    self.assertIsNone(store.reserve(
                        scope=fx["scope"], generation=other_generation,
                        profile=http.config.profile, base_url=http.config.base_url,
                        idempotency_key=other_args["idempotency_key"],
                        request_fingerprint=request_fingerprint(other_args["body"]),
                        deadline=time.monotonic() + 20))
                    self.assertEqual(_pending(store), [other_candidate])

                args, _ = _args(fx, source, store, "late", http.config, budget=budget)
                lookup = _lookup(fx, http, args)
                candidate = dict(lookup, scope=fx["scope"].as_dict())
                deadline = args["deadline"]
                self.assertNotEqual(lookup["idempotency_key"], prior_lookup["idempotency_key"])

                state = {"barrier": None, "final_calls": [], "flip": [], "puts": [],
                         "put_outcome": [], "pending_effects": [], "inspect_after": [],
                         "restore_calls": [], "restoring_pending": False,
                         "write_blob_calls": []}
                reached, release, put_done, worker_done = (
                    threading.Event(), threading.Event(), threading.Event(), threading.Event())
                real_put, real_inspect = store.put, store._inspect
                real_write_blob, real_publish = store._write_blob, store._publish
                real_restore = store._restore_pending
                original_worker = TargetHttpClient._admit_source_worker

                def observed_worker(client, **kwargs):
                    try:
                        return original_worker(client, **kwargs)
                    finally:
                        worker_done.set()

                def put(**kwargs):
                    state["puts"].append(True)
                    try:
                        value = real_put(**kwargs)
                        state["put_outcome"].append(("returned", None))
                        return value
                    except BaseException as error:
                        state["put_outcome"].append((type(error).__name__, str(error)))
                        raise
                    finally:
                        put_done.set()

                def effect(kind):
                    state["pending_effects"].append(
                        (kind, time.monotonic() < deadline, state["restoring_pending"]))

                def replaced(source_name, target, *a, **kw):
                    if str(source_name).endswith(".commit") and str(target) == final_name:
                        state["barrier"] = {
                            "live": time.monotonic() < deadline, "disk": disk(root),
                            "rows": len(store._decode_raw((root / final_name).read_bytes(), final_name, receipt_keys)),
                            "pending": store._decode_raw((root / pending_name).read_bytes(), pending_name, pending_keys),
                        }
                        reached.set()
                        if hold and state["barrier"]["live"]:
                            if not release.wait(30):
                                raise RuntimeError("D02 hold was not released")
                        state["final_calls"].append(True)
                        return real_replace(source_name, target, *a, **kw)
                    if str(target) == pending_name and state["final_calls"]:
                        effect("rollback-replace-pending" if state["restoring_pending"] else "replace-pending")
                    return real_replace(source_name, target, *a, **kw)

                def unlinked(path, *a, **kw):
                    if str(path) == pending_name and state["final_calls"]:
                        effect("rollback-unlink-pending" if state["restoring_pending"] else "unlink-pending")
                    return real_unlink(path, *a, **kw)

                def write_blob(name, *a, **kw):
                    if name == pending_name and state["final_calls"]:
                        state["write_blob_calls"].append(
                            (time.monotonic() < deadline, state["restoring_pending"]))
                        effect("rollback-rewrite-pending" if state["restoring_pending"] else "rewrite-pending")
                    return real_write_blob(name, *a, **kw)

                def publish(name, *a, **kw):
                    if name == pending_name and state["final_calls"]:
                        effect("rollback-publish-pending" if state["restoring_pending"] else "publish-pending")
                    return real_publish(name, *a, **kw)

                def restore_pending(raw):
                    state["restore_calls"].append((sha(raw), time.monotonic() < deadline))
                    state["restoring_pending"] = True
                    try:
                        return real_restore(raw)
                    finally:
                        state["restoring_pending"] = False

                def inspected(name, **kw):
                    if (fault == "verify" and name == final_name and not state["flip"]
                            and state["final_calls"] and any(
                                sys._getframe(depth).f_code.co_name == "_retire_pending" for depth in range(1, 6))):
                        # One-shot REAL damage of the visible final receipt exactly at the
                        # closure's integrity check (after _publish's own verification).
                        fd = real_open(str(root / final_name), receipts_module.os.O_RDWR)
                        try:
                            state["flip"].append(sha((root / final_name).read_bytes()))
                            byte = receipts_module.os.pread(fd, 1, 5)
                            receipts_module.os.pwrite(fd, bytes([byte[0] ^ 1]), 5)
                        finally:
                            receipts_module.os.close(fd)
                    result = real_inspect(name, **kw)
                    if state["final_calls"] and name == final_name and result is not None:
                        state["inspect_after"].append(result[0][4])
                    return result

                def receipts_state(handle, *, late, pending_retired, expected_pending=None):
                    self.assertEqual(handle.count(), 2 if late else 1)
                    self.assertTrue(handle.find(**prior_lookup) == prior_found,
                                    "prior acknowledged receipt changed")
                    found = handle.find(**lookup)
                    if late:
                        self.assertIsNotNone(found, "late durable receipt absent")
                        self.assertTrue(found["scope"] == fx["scope"].as_dict())
                        self.assertTrue(all(found[k] == v for k, v in lookup.items() if k != "scope"))
                        self.assertEqual(found["run_id"], http.response["run_id"])
                    else:
                        self.assertIsNone(found)
                    expected = [] if pending_retired else [candidate]
                    if expected_pending is not None:
                        expected = expected_pending
                    self.assertEqual(_pending(handle), expected)

                returned = None
                try:
                    with mock.patch.object(TargetHttpClient, "_admit_source_worker", new=observed_worker), \
                            mock.patch.object(store, "put", side_effect=put), \
                            mock.patch.object(store, "_inspect", side_effect=inspected), \
                            mock.patch.object(store, "_write_blob", side_effect=write_blob), \
                            mock.patch.object(store, "_publish", side_effect=publish), \
                            mock.patch.object(store, "_restore_pending", side_effect=restore_pending), \
                            mock.patch.object(receipts_module.os, "replace", side_effect=replaced), \
                            mock.patch.object(receipts_module.os, "unlink", side_effect=unlinked):
                        try:
                            if hold:
                                with self.assertRaisesRegex(TargetHttpError, "^target_admission_ambiguous$"):
                                    TargetHttpClient(http.config).admit_run(**args)
                                returned = time.monotonic()
                                held_pending = disk(root)["pending_sha"]
                                held_calls = len(state["final_calls"])
                                held_effects = list(state["pending_effects"])
                            else:
                                result = TargetHttpClient(http.config).admit_run(**args)
                                returned = time.monotonic()
                                self.assertIs(result["replayed"], False)
                                self.assertEqual(result["run_id"], http.response["run_id"])
                        finally:
                            release.set()
                            self.assertTrue(put_done.wait(30), "late admission worker did not settle")
                            self.assertTrue(worker_done.wait(30), "admission worker method did not return")
                    # Harness integrity first: an unreached boundary is never source evidence.
                    barrier = state["barrier"]
                    self.assertIsNotNone(barrier, "real final visibility boundary not reached")
                    self.assertTrue(barrier["live"], "boundary reached after the original deadline (harness timing)")
                    self.assertEqual(barrier["disk"]["phase"], "commit")
                    kinds = barrier["disk"]["kinds"]
                    self.assertEqual(set(kinds), {"backup", "commit", "guard", "tmp"})
                    self.assertGreaterEqual(kinds.count("backup"), 1)
                    self.assertEqual(kinds.count("commit"), 1)
                    self.assertEqual(kinds.count("guard"), 1)
                    self.assertEqual(kinds.count("tmp"), 1)
                    self.assertTrue(barrier["disk"]["final"] == old_sha, "final visible before admitted call")
                    self.assertEqual(barrier["rows"], 1)
                    expected_at_barrier = ([candidate] if other_candidate is None else
                                            [other_candidate, candidate])
                    self.assertEqual(barrier["pending"], expected_at_barrier)
                    self.assertTrue(barrier["disk"]["pending_file"])
                    original_pending_sha = barrier["disk"]["pending_sha"]
                    self.assertEqual(state["puts"], [True])
                    self.assertEqual(state["final_calls"], [True], "final call-through not exactly once")
                    self.assertEqual((http.raw_ingress, http.verified_native_effect), (1, 1))
                    if hold:
                        self.assertGreaterEqual(returned, deadline - 0.05, "caller returned before its deadline")
                        self.assertLess(returned - deadline, 3.0, "caller was not bounded")
                        self.assertEqual(held_calls, 0, "worker was not held at final visibility")
                        self.assertTrue(held_pending == original_pending_sha)
                        self.assertEqual(held_effects, [])
                    self.assertGreaterEqual(len(state["inspect_after"]), 1,
                                            "receipt-integrity inspection seam not reached after visibility")
                    outcome = state["put_outcome"]
                    self.assertEqual(len(outcome), 1)
                    after = disk(root)
                    effects = state["pending_effects"]
                    if fault is None and not hold:
                        # Live positive control: pending retirement completes while live.
                        self.assertEqual(outcome[0], ("returned", None))
                        self.assertEqual(after["phase"], None)
                        self.assertTrue(after["final"] != old_sha)
                        self.assertEqual(state["restore_calls"], [])
                        if other_candidate is None:
                            self.assertEqual(effects, [("unlink-pending", True, False)])
                            self.assertFalse(after["pending_file"])
                            self.assertEqual(state["write_blob_calls"], [])
                            receipts_state(store, late=True, pending_retired=True)
                        else:
                            # This control proves the nonempty-retained rewrite seam is live.
                            self.assertTrue(after["pending_file"])
                            self.assertNotEqual(after["pending_sha"], original_pending_sha)
                            self.assertEqual(state["write_blob_calls"], [(True, False)])
                            receipts_state(store, late=True, pending_retired=False,
                                           expected_pending=[other_candidate])
                        store.close()
                        with closing(AdmissionReceiptStore(root)) as reopened:
                            if other_candidate is None:
                                receipts_state(reopened, late=True, pending_retired=True)
                            else:
                                receipts_state(reopened, late=True, pending_retired=False,
                                               expected_pending=[other_candidate])
                    elif fault is None:
                        self.assertEqual(outcome[0], ("AdmissionReceiptError", "admission_deadline_exceeded"))
                        self.assertTrue(after["final"] != old_sha)
                        self.assertEqual(after["phase"], None)
                        self.assertEqual(state["restore_calls"], [])
                        # Expiry skips NEW pending effects: no unlink/rewrite/publish of pending.
                        self.assertEqual(effects, [], "pending mutated after the original deadline expired")
                        self.assertEqual(state["write_blob_calls"], [],
                                         "pending rewrite phase started after the original deadline")
                        self.assertTrue(after["pending_file"], "covered pending row retired after expiry")
                        self.assertTrue(after["pending_sha"] == original_pending_sha)
                        # The late receipt is durable and intact (integrity check ran and passed).
                        expected_pending = ([candidate] if other_candidate is None else
                                            [other_candidate, candidate])
                        receipts_state(store, late=True, pending_retired=False,
                                       expected_pending=expected_pending)
                        store.close()
                        with closing(AdmissionReceiptStore(root)) as reopened:
                            receipts_state(reopened, late=True, pending_retired=False,
                                           expected_pending=expected_pending)
                        self.assertTrue(disk(root)["pending_sha"] == original_pending_sha)
                    else:
                        self.assertEqual(len(state["flip"]), 1)
                        self.assertTrue(state["inspect_after"][-1] != state["flip"][0],
                                        "verification did not observe the injected fault")
                        self.assertEqual(outcome[0], ("AdmissionReceiptError", "admission_storage_unavailable"))
                        # Mandatory rollback survives expiry: old receipt bytes/rows and original pending bytes.
                        self.assertEqual(after["phase"], None)
                        self.assertEqual(after["kinds"], [])
                        self.assertTrue(after["final"] == old_sha, "rollback did not restore prior bytes")
                        self.assertTrue(after["pending_file"])
                        self.assertTrue(after["pending_sha"] == original_pending_sha,
                                        "pending bytes not restored to the original")
                        receipts_state(store, late=False, pending_retired=False)
                        store.close()
                        with closing(AdmissionReceiptStore(root)) as reopened:
                            receipts_state(reopened, late=False, pending_retired=False)
                        self.assertTrue(disk(root)["final"] == old_sha)
                        self.assertTrue(disk(root)["pending_sha"] == original_pending_sha)
                        # Expiry forbids NEW retirement effects; restoration from _restore_pending
                        # remains mandatory and is tracked separately from retirement.
                        retirement_effects = [effect for effect in effects if not effect[2]]
                        self.assertEqual(retirement_effects, [],
                                         "pending retirement ran after the original deadline")
                        self.assertEqual(state["restore_calls"], [(original_pending_sha, False)],
                                         "integrity failure skipped mandatory pending restoration")
                finally:
                    release.set()

        with self.subTest(d02="positive_control_live_deadline_retires_pending"):
            scenario("control", hold=False, fault=None, budget=20)
        with self.subTest(d02="live_nonempty_retained_pending_rewrite_positive_control"):
            scenario("rewrite-control", hold=False, fault=None, budget=20,
                     retain_unrelated=True)
        with self.subTest(d02="expired_admitted_visibility_skips_pending_retirement_keeps_integrity"):
            scenario("late", hold=True, fault=None, budget=8)
        with self.subTest(d02="expired_nonempty_retained_pending_rewrite_is_not_started"):
            scenario("rewrite-late", hold=True, fault=None, budget=8,
                     retain_unrelated=True)
        with self.subTest(d02="expired_integrity_fault_restores_receipt_and_pending"):
            scenario("verify", hold=True, fault="verify", budget=8)

    def test_redirect_non202_invalid_schema_and_wrong_replay_are_not_success(self):
        c = TargetHttpClient(TargetHttpConfig("http://127.0.0.1:1", "default"))
        with self.assertRaises(TargetHttpError):
            c._urlopen("http://127.0.0.1:1/v1/runs", {}, b"{}", deadline=time.monotonic()-1)

    def test_reserve_race_promotes_real_receipt(self):
        with genuine_source_fixture() as fx, _managed_loopback() as http, \
                tempfile.TemporaryDirectory() as directory, ExitStack() as resources:
            source = _source_for(fx, http.config)
            path = Path(directory) / "admissions"
            store = resources.enter_context(closing(AdmissionReceiptStore(path)))
            client = TargetHttpClient(http.config)
            before = _producer_snapshot(fx)
            first_args, _ = _args(fx, source, store, "race", http.config, budget=30)
            first = client.admit_run(**first_args)
            self.assertEqual((first["status"], first["replayed"]), ("started", False))
            self.assertEqual((http.raw_ingress, http.verified_native_effect), (1, 1))
            self.assertEqual(len(http.posts), 1)
            first_capture = http.native_capture
            self.assertIsNotNone(first_capture)
            assert first_capture is not None
            lookup_scope = fx["scope"]
            lookup_generation = int(lookup_scope.pairing_generation)
            lookup_profile = http.config.profile
            lookup_base_url = http.config.base_url
            lookup_key = first_args["idempotency_key"]
            lookup_fingerprint = request_fingerprint(first_args["body"])
            prior = store.find(scope=lookup_scope, generation=lookup_generation, profile=lookup_profile,
                base_url=lookup_base_url, idempotency_key=lookup_key,
                request_fingerprint=lookup_fingerprint)
            self.assertIsNotNone(prior)
            assert prior is not None
            self.assertEqual(prior["run_id"], first["run_id"])
            self.assertEqual(store.count(), 1)
            self.assertEqual(_pending(store), [])

            second_args, _ = _args(fx, source, store, "race", http.config, budget=30)
            self.assertEqual(second_args["idempotency_key"], first_args["idempotency_key"])
            self.assertTrue(second_args["forwarded_body"] == first_capture["raw"])
            self.assertTrue(second_args["forwarded_body"] == http.native_capture["raw"])
            http.response = {"run_id": first["run_id"], "status": "running", "replayed": True}

            original_find, original_reserve = store.find, store.reserve
            calls = []
            reserve_results = []
            stale_snapshots = []
            force_stale_miss = [False]

            def observed_find(**kwargs):
                value = original_find(**kwargs)
                calls.append("find")
                if force_stale_miss[0]:
                    force_stale_miss[0] = False
                    self.assertTrue(value == prior, "race setup must begin with the durable receipt")
                    stale_snapshots.append(value)
                    return None  # deterministic stale-find TOCTOU schedule; durable prior stays present
                return value

            def observed_reserve(**kwargs):
                value = original_reserve(**kwargs)
                calls.append("reserve")
                reserve_results.append(value)
                return value

            # Positive recorder controls are outside admit_run. They exercise the
            # real methods and see the actual durable prior before their counters reset.
            with mock.patch.object(store, "find", side_effect=observed_find) as find_spy, \
                    mock.patch.object(store, "reserve", side_effect=observed_reserve) as reserve_spy, \
                    mock.patch.object(store, "put", wraps=store.put) as put_spy:
                self.assertTrue(store.find(scope=lookup_scope, generation=lookup_generation, profile=lookup_profile,
                    base_url=lookup_base_url, idempotency_key=lookup_key,
                    request_fingerprint=lookup_fingerprint) == prior, "reserve-race find recorder control")
                self.assertTrue(store.reserve(scope=lookup_scope, generation=lookup_generation, profile=lookup_profile,
                    base_url=lookup_base_url, idempotency_key=lookup_key,
                    request_fingerprint=lookup_fingerprint, deadline=time.monotonic() + 5) == prior,
                    "reserve-race reserve recorder control")
                self.assertEqual(calls, ["find", "reserve"])
                self.assertEqual((find_spy.call_count, reserve_spy.call_count), (1, 1))
                self.assertEqual(len(reserve_results), 1)
                self.assertTrue(reserve_results[0] == prior)
                self.assertEqual(_pending(store), [])
                calls.clear(); reserve_results.clear(); stale_snapshots.clear()
                find_spy.reset_mock(); reserve_spy.reset_mock(); put_spy.reset_mock()
                force_stale_miss[0] = True

                try:
                    replay = client.admit_run(**second_args)
                    replay_error = None
                except TargetHttpError as exc:
                    replay = None
                    replay_error = str(exc)

                self.assertEqual(calls, ["find", "reserve"], "the second request must take the reserve race")
                self.assertEqual((find_spy.call_count, reserve_spy.call_count), (1, 1))
                self.assertEqual(len(stale_snapshots), 1)
                self.assertTrue(stale_snapshots[0] == prior, "stale find snapshot was not the real receipt")
                self.assertEqual(len(reserve_results), 1)
                self.assertTrue(reserve_results[0] == prior, "reserve must promote the real durable receipt")
                self.assertEqual(put_spy.call_count, 0, "a raced prior receipt must not be rewritten")

            self.assertEqual((http.raw_ingress, http.verified_native_effect), (1, 1),
                             "the raced prior still performs exactly one verified HTTP replay")
            self.assertEqual(len(http.posts), 1)
            self.assertIsNone(replay_error, "reserve return was not promoted to the prior receipt")
            assert replay is not None
            self.assertEqual((replay["status"], replay["replayed"]), ("running", True))
            self.assertEqual(replay["run_id"], first["run_id"])
            self.assertEqual((http.raw_ingress, http.verified_native_effect), (1, 1))
            self.assertEqual(len(http.posts), 1)
            self.assertTrue(http.posts[0][0] == first_capture["raw"])
            self.assertTrue(http.posts[0][1]["Authorization"] == first_capture["authorization"])
            self.assertEqual(http.posts[0][1]["Idempotency-Key"], first_capture["key"])
            self.assertEqual(http.paths, [first_capture["route"]])
            self.assertEqual(store.count(), 1)
            self.assertTrue(store.find(scope=lookup_scope, generation=lookup_generation, profile=lookup_profile,
                base_url=lookup_base_url, idempotency_key=lookup_key,
                request_fingerprint=lookup_fingerprint) == prior, "raced receipt was replaced")
            self.assertEqual(_pending(store), [])
            with closing(AdmissionReceiptStore(path)) as reopened:
                self.assertEqual(reopened.count(), 1)
                self.assertTrue(reopened.find(scope=lookup_scope, generation=lookup_generation, profile=lookup_profile,
                    base_url=lookup_base_url, idempotency_key=lookup_key,
                    request_fingerprint=lookup_fingerprint) == prior, "fresh reopen changed raced receipt")
                self.assertEqual(_pending(reopened), [])
            self.assertTrue(_producer_snapshot(fx) == before, "reserve race changed producer")
            http.close()
            self.assertFalse(http.thread.is_alive())
            self.assertEqual(http.server.fileno(), -1)

    def test_source_lane_positive_exact_native_bytes_key_and_durable_receipt(self):
        with genuine_source_fixture() as fx, _managed_loopback() as http:
            source = _source_for(fx, http.config)
            with tempfile.TemporaryDirectory() as directory, closing(
                    AdmissionReceiptStore(Path(directory) / "admissions")) as store, closing(http):
                args, checked = _args(fx, source, store, "exact", http.config)
                def producer_snapshot():
                    with fx["fx"]["joining_store"]._read() as connection:
                        rows = tuple(tuple(row) for row in connection.execute(
                            "SELECT * FROM material ORDER BY scope_key"))
                    return rows, len(fx["producer"]["native_calls"])
                before = producer_snapshot()
                original = http.native_capture
                self.assertEqual(checked.prompt, "R02 café e\u0301 🧪")
                self.assertEqual(args["body"]["input"], checked.prompt)
                self.assertEqual(original["key"], "room:exact:1")
                self.assertEqual(original["route"], "/v1/runs")
                self.assertEqual(original["profile"], "default")
                self.assertEqual(http.config.profile, original["profile"])
                with self.subTest(wire_invariant="captured_bytes"):
                    self.assertTrue(args["forwarded_body"] == original["raw"], "captured native bytes changed")
                self.assertTrue(args["idempotency_key"] == original["key"])
                for escaped in (b'\\u00e9', b'\\u0301', b'\\ud83e\\uddea'):
                    with self.subTest(wire_invariant=escaped.decode("ascii")):
                        self.assertTrue(escaped in args["forwarded_body"], "native escaped wire bytes missing")
                lookup = dict(scope=fx["scope"], generation=fx["scope"].pairing_generation,
                              profile=http.config.profile, base_url=http.config.base_url,
                              idempotency_key=original["key"],
                              request_fingerprint=request_fingerprint(args["body"]))
                expected_run = http.response["run_id"]
                result = TargetHttpClient(http.config).admit_run(**args)
                self.assertEqual(result["status"], "started")
                self.assertEqual(result["run_id"], expected_run)
                self.assertIs(result["replayed"], False)
                self.assertEqual(len(http.posts), 1)
                raw, headers = http.posts[0]
                self.assertTrue(raw == original["raw"], "forwarded bytes differ from native capture")
                self.assertTrue(headers["Idempotency-Key"] == original["key"])
                self.assertTrue(headers["Authorization"] == original["authorization"],
                                "forwarded Authorization differs from native capture")
                self.assertEqual(http.paths, ["/v1/runs"])
                self.assertEqual(http.raw_ingress, 1)
                self.assertEqual(http.verified_native_effect, 1)
                self.assertEqual(store.count(), 1)
                found = store.find(**lookup)
                self.assertIsNotNone(found)
                self.assertTrue(found["scope"] == fx["scope"].as_dict())
                self.assertTrue(all(found[field] == value for field, value in lookup.items()
                                    if field != "scope"))
                self.assertEqual(found["run_id"], result["run_id"])
                self.assertEqual(found["status"], "started")
                self.assertIs(found["replayed"], False)
                with closing(AdmissionReceiptStore(Path(directory) / "admissions")) as reopened:
                    self.assertEqual(reopened.count(), 1)
                    self.assertTrue(reopened.find(**lookup) == found, "reopened receipt binding differs")
                self.assertTrue(producer_snapshot() == before, "positive consumer changed producer")

                # RECEIVER-ONLY negatives: same actual capture, owned direct HTTP.
                # No Source replacement and no product valid-grant mutation claim.
                for corruption in ("body", "grant"):
                    with self.subTest(receiver_corruption=corruption), closing(
                            AdmissionReceiptStore(Path(directory) / ("receiver-" + corruption))) as negative_store:
                        baseline = (http.raw_ingress, http.verified_native_effect)
                        self.assertEqual(negative_store.count(), 0)
                        corrupt_raw = original["raw"]
                        authorization = original["authorization"]
                        if corruption == "body":
                            corrupt_body = json.loads(original["raw"].decode("utf-8"))
                            corrupt_body["hosted_room_dispatch"]["prompt"] += " changed"
                            self.assertTrue(corrupt_body["hosted_room_dispatch"]["prompt_digest"] ==
                                            args["body"]["hosted_room_dispatch"]["prompt_digest"])
                            corrupt_raw = json.dumps(corrupt_body, separators=(",", ":")).encode()
                            self.assertTrue(corrupt_raw != original["raw"])
                        else:
                            prefix, signature = authorization.rsplit(".", 1)
                            flipped = ("A" if signature[0] != "A" else "B") + signature[1:]
                            authorization = prefix + "." + flipped
                            self.assertTrue(authorization != original["authorization"])
                            self.assertTrue(corrupt_raw == original["raw"])
                        with closing(HTTPConnection(
                                "127.0.0.1", http.server.server_port, timeout=3)) as connection:
                            connection.request("POST", original["route"], corrupt_raw, {
                                "Authorization": authorization, "Idempotency-Key": original["key"],
                                "Content-Type": "application/json"})
                            with connection.getresponse() as response:
                                status = response.status
                                response_headers = str(response.getheaders()).encode()
                                payload = response.read()
                        self.assertEqual(http.raw_ingress - baseline[0], 1)
                        self.assertEqual(http.verified_native_effect - baseline[1], 0,
                                         "corrupt input counted verified")
                        self.assertEqual(status, 403)
                        self.assertEqual(payload, b'{"error":"forbidden"}')
                        self.assertTrue(all(value.encode() not in payload + response_headers
                                            for value in (authorization, original["authorization"], fx["grant"])))
                        self.assertEqual(negative_store.count(), 0)
                        self.assertIsNone(negative_store.find(**lookup))
                        self.assertEqual(store.count(), 1)
                        self.assertTrue(store.find(**lookup) == found)
                        self.assertTrue(producer_snapshot() == before, "receiver control changed producer")
                _identity_witness(self, source, args)
                self.assertTrue(producer_snapshot() == before, "unit identity checks changed producer")
                http.close()
                self.assertFalse(http.thread.is_alive())
                self.assertEqual(http.server.fileno(), -1)
        with self.subTest(foreign_existing_ref=True):
            _foreign_ref_witness(self)
        for branch in ("new", "prior"):
            for earlier in (True, False):
                with self.subTest(public_comparison_control=branch, inherited_earlier=earlier):
                    _comparison_witness(self, branch, inherited_earlier=earlier)
            for fault in (1, 2, 3):
                with self.subTest(owned_guard_fault=branch, comparison=fault):
                    _comparison_witness(self, branch, fault)

    def test_source_lane_rejects_mixed_partial_duck_and_injected_transport_before_access(self):
        # B3 is entry-only. All authority and successful wire data come from the
        # unchanged real producer; negative shapes delegate only for recorder
        # controls, never as replacement valid authority.
        import asyncio
        from invite_rooms.target_material import TargetMaterialSource, ResolvedTargetMaterial
        from invite_rooms.peer_registry import MaterialScope

        with genuine_source_fixture() as fx, _managed_loopback() as http, \
                tempfile.TemporaryDirectory() as directory, ExitStack() as resources:
            source = _source_for(fx, http.config)
            root = Path(directory)
            control = resources.enter_context(closing(AdmissionReceiptStore(root / "control")))
            before = _producer_snapshot(fx)
            ordinal = 0

            def fresh(label):
                nonlocal ordinal
                ordinal += 1
                path = root / str(ordinal)
                store = resources.enter_context(closing(AdmissionReceiptStore(path)))
                args, _ = _args(fx, source, store, label, http.config, budget=30)
                return args, store, path

            def deny(label, change, *, client=None, events=None, inherited=None, running=False,
                     supplied_store=None):
                args, store, path = fresh(label)
                bad = change(dict(args))
                lookup = _lookup(fx, http, args)
                baseline = _pending(store)
                self.assertEqual(baseline, [])
                supplied_pending = _pending(supplied_store) if supplied_store is not None else None
                if supplied_store is not None:
                    self.assertEqual(supplied_pending, [])
                # Constructor and recorder controls are OUTSIDE the public
                # method-entry window. reserve mutates ONLY the control store.
                with mock.patch.object(source, "load", wraps=source.load) as load, \
                        mock.patch.object(AdmissionReceiptStore, "find", autospec=True,
                                          side_effect=AdmissionReceiptStore.find) as find, \
                        mock.patch.object(AdmissionReceiptStore, "reserve", autospec=True,
                                          side_effect=AdmissionReceiptStore.reserve) as reserve:
                    load(args["resolved"].scope, credential_ref=args["resolved"].credential_ref,
                         deadline=args["deadline"])
                    control.find(**lookup)
                    control.reserve(**lookup)
                    self.assertEqual((load.call_count, find.call_count, reserve.call_count), (1, 1, 1),
                                     "B3 outside method-entry recorder control")
                    load.reset_mock(); find.reset_mock(); reserve.reset_mock()
                    if events is not None:
                        events.clear()
                    previous = ADMISSION_DEADLINE.get()
                    token = ADMISSION_DEADLINE.set(inherited)
                    try:
                        def invoke():
                            # Observe effects even if a bypass changes the error
                            # category or returns success. This is the RED oracle.
                            error = None
                            try:
                                (client or TargetHttpClient(http.config)).admit_run(**bad)
                            except TargetHttpError as exc:
                                error = str(exc)
                            finally:
                                if events is not None:
                                    self.assertEqual(events, [], "B3 supplied capability accessed: " + label)
                                self.assertEqual(load.call_count, 0, "B3 Source.load entry: " + label)
                                self.assertEqual(find.call_count, 0, "B3 find METHODENTRY: " + label)
                                self.assertEqual(reserve.call_count, 0, "B3 reserve METHODENTRY: " + label)
                                self.assertEqual((http.raw_ingress, http.verified_native_effect), (0, 0),
                                                 "B3 network effect: " + label)
                            self.assertEqual(error, "invalid_request", "B3 entry refusal: " + label)
                        if running:
                            async def inside():
                                self.assertTrue(asyncio.get_running_loop().is_running())
                                invoke()
                            asyncio.run(inside())
                        else:
                            invoke()
                    finally:
                        ADMISSION_DEADLINE.reset(token)
                    self.assertTrue(ADMISSION_DEADLINE.get() == previous)
                self.assertEqual(store.count(), 0)
                self.assertIsNone(store.find(**lookup))
                self.assertTrue(_pending(store) == baseline, "B3 actual pending delta: " + label)
                with closing(AdmissionReceiptStore(path)) as reopened:
                    self.assertEqual(reopened.count(), 0)
                    self.assertIsNone(reopened.find(**lookup))
                    self.assertTrue(_pending(reopened) == baseline, "B3 reopened pending delta: " + label)
                if supplied_store is not None:
                    self.assertEqual(supplied_store.count(), 0)
                    self.assertTrue(_pending(supplied_store) == supplied_pending)
                    with closing(AdmissionReceiptStore(supplied_store.root)) as reopened:
                        self.assertEqual(reopened.count(), 0)
                        self.assertTrue(_pending(reopened) == supplied_pending)
                self.assertTrue(_producer_snapshot(fx) == before, "B3 changed genuine producer")

            # Each structural row has a separate recorder. Subclasses inherit
            # the actual relevant method/attribute; only access is instrumented.
            seed, _, _ = fresh("structural-seed")
            for field, base, original, observed in (
                    ("source", TargetMaterialSource, source, "load"),
                    ("resolved", ResolvedTargetMaterial, seed["resolved"], "scope"),
                    ("scope", MaterialScope, seed["resolved"].scope, "target_profile"),
                    ("admission_store", AdmissionReceiptStore, control, "find")):
                for kind in ("subclass", "duck", "class-spoof"):
                    label = field + "-" + kind
                    with self.subTest(capability=field, shape=kind):
                        events = []
                        if field == "admission_store":
                            original = resources.enter_context(closing(AdmissionReceiptStore(root / (label + "-delegate"))))
                        if kind == "subclass":
                            def access(obj, name):
                                events.append(name)
                                return object.__getattribute__(obj, name)
                            shape_type = type("InheritedB3", (base,), {"__getattribute__": access})
                            if field == "source":
                                shape = shape_type(joining_store=source._store, authority=source._authority,
                                    identity_reader=source._identity_reader, catalog_reader=source._catalog_reader,
                                    target_http=http.config, endpoint_id=source._endpoint_id,
                                    generation=source._generation)
                            elif field == "resolved":
                                shape = shape_type(original.scope, original.credential_ref)
                            elif field == "scope":
                                shape = shape_type(**original.as_dict())
                            else:
                                shape = shape_type(root / label)
                                resources.callback(AdmissionReceiptStore.close, shape)
                            self.assertNotIn(observed, shape_type.__dict__)
                        else:
                            # __class__ spoof is deliberately not a subclass.
                            def access(obj, name):
                                events.append(name)
                                if name == "__class__" and kind == "class-spoof":
                                    return base
                                return getattr(original, name)
                            shape = type("DelegatingB3", (), {"__getattribute__": access})()
                        events.clear()  # constructor accesses precede controls
                        if kind == "class-spoof":
                            self.assertIs(shape.__class__, base)
                            self.assertIsNot(type(shape), base)
                            self.assertIsInstance(shape, base)
                            self.assertIn("__class__", events, "B3 OWN spoof control: " + label)
                        if field == "source":
                            loaded = shape.load(seed["resolved"].scope,
                                credential_ref=seed["resolved"].credential_ref, deadline=time.monotonic() + 30)
                            self.assertTrue(loaded["grant"] == fx["grant"])
                        elif field == "admission_store":
                            self.assertIsNone(shape.find(**_lookup(fx, http, seed)))
                        else:
                            getattr(shape, observed)
                        self.assertIn(observed, events, "B3 OWN recorder positive: " + label)
                        events.clear()
                        def change(args):
                            if field == "scope":
                                args["resolved"] = replace(args["resolved"], scope=shape)
                            else:
                                args[field] = shape
                            return args
                        deny(label, change, events=events,
                             supplied_store=(shape if kind == "subclass" else original)
                             if field == "admission_store" else None)

            for field in ("scope", "material_store"):
                with self.subTest(mixed=field):
                    deny("mixed-" + field, lambda a: dict(a, **{field: fx["scope"] if field == "scope" else source}))
            for field in ("forwarded_body", "source", "resolved", "deadline"):
                with self.subTest(missing=field):
                    def missing(args):
                        del args[field]
                        return args
                    deny("missing-" + field, missing)
            for field in ("forwarded_body", "source", "resolved", "deadline", "admission_store"):
                with self.subTest(explicit_none=field):
                    deny("none-" + field, lambda a: dict(a, **{field: None}))

            for falsey in (False, True):
                with self.subTest(supplied_transport_falsey=falsey):
                    calls = []
                    class Transport:
                        def __bool__(self):
                            return not falsey
                        def __call__(self, url, headers, payload):
                            calls.append("transport-entry")
                    transport = Transport()
                    transport(None, None, None)
                    self.assertEqual(calls, ["transport-entry"])
                    client = TargetHttpClient(http.config, transport=transport)
                    calls.clear()  # bool/constructor control outside entry
                    deny("transport-" + str(falsey), lambda a: a, client=client, events=calls)

            with self.subTest(entry_shape="running-loop"):
                deny("running-loop", lambda a: a, running=True)
            invalid = (("bool", True), ("none", None), ("nan", float("nan")),
                       ("positive-infinity", float("inf")), ("negative-infinity", -float("inf")),
                       ("expired", time.monotonic() - 1), ("string", "deadline"))
            for origin in ("explicit", "inherited"):
                for name, value in invalid:
                    if origin == "inherited" and value is None:
                        continue  # None means absent, exercised by success below
                    for inherited_valid in ((False, True) if origin == "explicit" else (True,)):
                        label = origin + "-" + name + "-" + str(inherited_valid)
                        with self.subTest(entry_deadline=label):
                            deny(label, (lambda a: dict(a, deadline=value)) if origin == "explicit" else (lambda a: a),
                                 inherited=(time.monotonic() + 30 if inherited_valid else None)
                                 if origin == "explicit" else value)

            class Sentinel(Exception):
                pass
            worker_calls, urlopen_calls = [], []
            class WorkerOverride(TargetHttpClient):
                def _admit_source_worker(self, **kwargs):
                    worker_calls.append("worker-only-sentinel")
                    raise Sentinel("B3-worker-only")
            class UrlopenOverride(TargetHttpClient):
                def _urlopen(self, *args, **kwargs):
                    urlopen_calls.append("urlopen-only-sentinel")
                    raise Sentinel("B3-urlopen-only")
            for label, cls, method, calls, marker, message in (
                    ("worker-override", WorkerOverride, "_admit_source_worker", worker_calls,
                     "worker-only-sentinel", "B3-worker-only"),
                    ("urlopen-override", UrlopenOverride, "_urlopen", urlopen_calls,
                     "urlopen-only-sentinel", "B3-urlopen-only")):
                with self.subTest(independent_override=label):
                    client = cls(http.config)
                    with self.assertRaisesRegex(Sentinel, "^" + message + "$"):
                        getattr(client, method)()
                    self.assertEqual(calls, [marker], "B3 outside sentinel control")
                    calls.clear()
                    args, store, path = fresh(label)
                    previous = ADMISSION_DEADLINE.get()
                    token = ADMISSION_DEADLINE.set(None)  # explicit valid, inherited ABSENT
                    try:
                        try:
                            result = client.admit_run(**args)
                        finally:
                            self.assertEqual(calls, [], "B3 public override invoked: " + label)
                    finally:
                        ADMISSION_DEADLINE.reset(token)
                    self.assertTrue(ADMISSION_DEADLINE.get() == previous)
                    self.assertEqual(result["status"], "started")
                    self.assertEqual(result["run_id"], http.response["run_id"])
                    self.assertIs(result["replayed"], False)
                    self.assertEqual((http.raw_ingress, http.verified_native_effect), (1, 1))
                    self.assertTrue(http.posts[0][0] == http.native_capture["raw"])
                    self.assertTrue(http.posts[0][1]["Authorization"] == http.native_capture["authorization"])
                    self.assertEqual(http.posts[0][1]["Idempotency-Key"], http.native_capture["key"])
                    self.assertEqual(http.paths, [http.native_capture["route"]])
                    self.assertEqual(store.count(), 1)
                    _receipt_witness(self, fx, http, args, store, path)
                    self.assertEqual(_pending(store), [])
                    self.assertTrue(_producer_snapshot(fx) == before)
            http.close()
            self.assertFalse(http.thread.is_alive())
            self.assertEqual(http.server.fileno(), -1)

    def test_strict_raw_json_types_finiteness_dispatch_and_key(self):
        from gateway.hosted_room_peer import HostedMemberDispatch, verify_room_grant
        from tui_gateway.hosted_room_peer_http import PeerRunsHTTPClient
        from invite_rooms.target_http import MAX_REQUEST_BYTES

        # Native domain map: strings (identifiers/digests/input/prompt), and ONLY
        # exact positive ints in these four slots. No free float/container slot.
        positive_ints = ("protocol_version", "authority_epoch", "execution_generation", "source_event_seq")
        identifiers = ("room_id", "home_install_id", "authority_gateway_id", "member_id",
                       "target_install_id", "target_profile", "task_id", "cancellation_scope_id", "trace_id")
        digests = ("capability_digest", "execution_policy_digest", "prompt_digest")
        # Parent ruling: native-valid signed-zero parity is
        # NOT_APPLICABLE_UNREPRESENTABLE; the named public refusals below are
        # native-domain-invalid, NOT independent parity proof or skipped tests.
        with genuine_source_fixture() as fx, _managed_loopback() as http, \
                tempfile.TemporaryDirectory() as directory:
            source = _source_for(fx, http.config)
            before = _producer_snapshot(fx)
            ordinal = 0

            def encode(body):
                return json.dumps(body, separators=(",", ":")).encode("utf-8")

            def native_variant(args, checked, changes):
                # Original native producer, FORMATTING ONLY. Reuse the genuine
                # dispatch/authority, changing only requested unbound coordinates.
                mapping = checked.as_mapping()
                mapping.update(changes)
                checked = HostedMemberDispatch.from_mapping(mapping)
                peer = PeerRunsHTTPClient(base_url=http.config.base_url, api_key="")
                peer.prepare = lambda **_kwargs: {"session_id": "roomlink_capture"}
                captured = {}
                class Response:
                    headers = {"Content-Length": "68"}
                    def __init__(self):
                        self.payload = b'{"run_id":"run_' + b"a" * 32 + b'","status":"started","replayed":false}'
                    def read1(self, _size):
                        payload, self.payload = self.payload, b""
                        return payload
                    read = read1
                class Opener:
                    def __call__(self, request, **_kwargs):
                        captured["request"] = request
                        return self
                    def __enter__(self):
                        return Response()
                    def __exit__(self, *_args):
                        return False
                with mock.patch("hermes_cli.urllib_security.open_credentialed_url", Opener()):
                    peer._admit_dispatch(checked, grant=fx["grant"])
                request = captured["request"]
                args.update(body=json.loads(request.data.decode("utf-8")), forwarded_body=request.data,
                            idempotency_key=request.get_header("Idempotency-key"))
                http.native_capture = dict(raw=request.data, authorization=request.get_header("Authorization"),
                    key=args["idempotency_key"], route=request.selector, profile=checked.target_profile)
                def verify_received(raw, headers):
                    try:
                        body = json.loads(raw.decode("utf-8"))
                        dispatch = HostedMemberDispatch.from_mapping(body["hosted_room_dispatch"])
                        scheme, grant = headers["Authorization"].split(" ", 1)
                        if scheme != "HermesRoom":
                            return False
                        verify_room_grant(fx["native_secret"], grant, dispatch, permission="dispatch")
                        return dispatch == checked and body["input"] == checked.prompt
                    except (ValueError, TypeError, KeyError, IndexError, AttributeError):
                        return False
                http.verify_native = verify_received

            def fresh(store, changes=None):
                args, checked = _args(fx, source, store, "exact", http.config, budget=30)
                if changes:
                    native_variant(args, checked, changes)
                self.assertTrue(args["forwarded_body"] == http.native_capture["raw"], "B4 native capture basis")
                self.assertTrue(args["idempotency_key"] == http.native_capture["key"])
                return args

            def positive(label, *, changes=None, padded=False):
                nonlocal ordinal
                ordinal += 1
                path = Path(directory) / str(ordinal)
                with self.subTest(b4=label), closing(AdmissionReceiptStore(path)) as store:
                    args = fresh(store, changes)
                    original = http.native_capture
                    if padded:
                        self.assertLess(len(original["raw"]), MAX_REQUEST_BYTES)
                        args["forwarded_body"] = original["raw"] + b" " * (MAX_REQUEST_BYTES - len(original["raw"]))
                        self.assertEqual(len(args["forwarded_body"]), MAX_REQUEST_BYTES)
                        self.assertTrue(json.loads(args["forwarded_body"]) == args["body"])
                    result = TargetHttpClient(http.config).admit_run(**args)
                    self.assertEqual((result["status"], result["replayed"]), ("started", False))
                    self.assertEqual(result["run_id"], http.response["run_id"])
                    self.assertEqual((http.raw_ingress, http.verified_native_effect), (1, 1))
                    self.assertTrue(http.posts[0][0] == args["forwarded_body"])
                    self.assertTrue(http.posts[0][1]["Authorization"] == original["authorization"])
                    self.assertTrue(http.posts[0][1]["Idempotency-Key"] == original["key"])
                    self.assertEqual(http.paths, [original["route"]])
                    self.assertEqual(store.count(), 1)
                    _receipt_witness(self, fx, http, args, store, path)
                    self.assertEqual(_pending(store), [])
                    self.assertTrue(_producer_snapshot(fx) == before, "B4 positive producer unchanged")

            def deny(label, change, *, changes=None):
                nonlocal ordinal
                ordinal += 1
                path = Path(directory) / str(ordinal)
                with self.subTest(b4=label), closing(AdmissionReceiptStore(path)) as store, \
                        closing(AdmissionReceiptStore(Path(directory) / (str(ordinal) + "-control"))) as control:
                    args = fresh(store, changes)
                    lookup = _lookup(fx, http, args)
                    bad = change(copy.deepcopy(args["body"]), dict(args))
                    baseline = _pending(store)
                    self.assertEqual(baseline, [])
                    with mock.patch.object(source, "load", wraps=source.load) as load, \
                            mock.patch.object(AdmissionReceiptStore, "find", autospec=True,
                                              side_effect=AdmissionReceiptStore.find) as find, \
                            mock.patch.object(AdmissionReceiptStore, "reserve", autospec=True,
                                              side_effect=AdmissionReceiptStore.reserve) as reserve:
                        load(args["resolved"].scope, credential_ref=args["resolved"].credential_ref,
                             deadline=time.monotonic() + 30)
                        control.find(**lookup)
                        control.reserve(**lookup)
                        self.assertEqual((load.call_count, find.call_count, reserve.call_count), (1, 1, 1),
                                         "B4 outside method-entry recorder controls")
                        self.assertEqual(len(_pending(control)), 1)
                        load.reset_mock(); find.reset_mock(); reserve.reset_mock()
                        bad["deadline"] = time.monotonic() + 30  # independent valid budget per public call
                        error = None
                        try:
                            TargetHttpClient(http.config).admit_run(**bad)
                        except TargetHttpError as exc:
                            error = str(exc)
                        finally:
                            # An observation, NOT a normative no-source-access assertion.
                            with self.subTest(b4_observation=label, source_load=load.call_count,
                                              find_entries=find.call_count, reserve_entries=reserve.call_count,
                                              raw=http.raw_ingress, verified=http.verified_native_effect):
                                self.assertGreaterEqual(load.call_count, 0)
                            self.assertEqual(find.call_count, 0, "B4 find METHODENTRY: " + label)
                            self.assertEqual(reserve.call_count, 0, "B4 reserve METHODENTRY: " + label)
                            self.assertEqual((http.raw_ingress, http.verified_native_effect), (0, 0),
                                             "B4 zero network effect: " + label)
                        self.assertEqual(error, "invalid_request", "B4 exact public refusal: " + label)
                    self.assertEqual(store.count(), 0)
                    self.assertIsNone(store.find(**lookup))
                    self.assertTrue(_pending(store) == baseline, "B4 actual pending delta: " + label)
                    with closing(AdmissionReceiptStore(path)) as reopened:
                        self.assertEqual(reopened.count(), 0)
                        self.assertIsNone(reopened.find(**lookup))
                        self.assertTrue(_pending(reopened) == baseline, "B4 reopened pending delta: " + label)
                    self.assertTrue(_producer_snapshot(fx) == before, "B4 negative producer unchanged")

            def dispatch_change(field, value, *, supplied_only=False):
                def change(body, args):
                    body["hosted_room_dispatch"][field] = value
                    args["body"] = body
                    if not supplied_only:
                        args["forwarded_body"] = encode(body)
                    return args
                return change

            # Must precede ALL malformed rows: original captured bytes, exact key,
            # real native verifier, bound receipt and fresh reopen.
            positive("positive-native-capture-room:exact:1")
            positive("positive-MAX_REQUEST_BYTES-whitespace-wire-control", padded=True)
            positive("positive-trace-256", changes={"trace_id": "a" * 256})
            positive("positive-trace-string-1.0", changes={"trace_id": "1.0"})
            task248, task249 = "a" * 248, "a" * 249
            self.assertEqual((len(task248), len(task249)), (248, 249))
            self.assertEqual((len("room:" + task248 + ":1"), len("room:" + task249 + ":1")), (255, 256))
            positive("positive-derived-key-255", changes={"task_id": task248})

            def raw_change(kind):
                def change(body, args):
                    raw = args["forwarded_body"]
                    if kind in ("duplicate-top", "duplicate-nested"):
                        if kind == "duplicate-top":
                            duplicate = b'"input":' + json.dumps(body["input"]).encode() + b","
                            modified = b"{" + duplicate + raw[1:]
                            name = "input"
                        else:
                            token = b'"hosted_room_dispatch":'
                            self.assertEqual(raw.count(token), 1)
                            start = raw.index(b"{", raw.index(token) + len(token))
                            duplicate = b'"protocol_version":' + str(body["hosted_room_dispatch"]["protocol_version"]).encode() + b","
                            modified = raw[:start + 1] + duplicate + raw[start + 1:]
                            name = "protocol_version"
                        self.assertTrue(modified != raw, "B4 duplicate raw really changed")
                        pairs = []
                        json.loads(modified, object_pairs_hook=lambda items: (pairs.append(items), dict(items))[1])
                        self.assertTrue(any(sum(key == name for key, _ in items) == 2 for items in pairs),
                                        "B4 intended duplicate introduced")
                        args["forwarded_body"] = modified
                    elif kind in ("NaN", "Infinity", "-Infinity", "1e999"):
                        token = b'"source_event_seq":'
                        start = raw.index(token) + len(token)
                        end = start
                        while raw[end:end + 1] in (b" ", b"\t"):
                            end += 1
                        start = end
                        while raw[end:end + 1].isdigit():
                            end += 1
                        self.assertGreater(end, start)
                        args["forwarded_body"] = raw[:start] + kind.encode() + raw[end:]
                        body["hosted_room_dispatch"]["source_event_seq"] = float(kind)
                        args["body"] = body
                    elif kind == "invalid-utf8":
                        args["forwarded_body"] = raw.replace(b"R02", b"R\xff2", 1)
                        self.assertTrue(args["forwarded_body"] != raw)
                    elif kind == "top-list":
                        args.update(body=[body], forwarded_body=b"[" + raw + b"]")
                    elif kind == "top-scalar":
                        args.update(body=1, forwarded_body=b"1")
                    elif kind == "bytearray":
                        args["forwarded_body"] = bytearray(raw)
                        self.assertTrue(bytes(args["forwarded_body"]) == raw)
                    else:
                        args["forwarded_body"] = raw + b" " * (MAX_REQUEST_BYTES + 1 - len(raw))
                        self.assertEqual(len(args["forwarded_body"]), MAX_REQUEST_BYTES + 1)
                    return args
                return change
            for kind in ("duplicate-top", "duplicate-nested", "NaN", "Infinity", "-Infinity", "1e999",
                         "invalid-utf8", "top-list", "top-scalar", "bytearray", "MAX_REQUEST_BYTES+1"):
                deny("raw-" + kind, raw_change(kind))

            deny("parity-supplied-True-wire-1", dispatch_change("source_event_seq", True, supplied_only=True))
            deny("parity-supplied-1.0-wire-1", dispatch_change("source_event_seq", 1.0, supplied_only=True))
            deny("parity-supplied-float-1.0-wire-string-1.0",
                 dispatch_change("trace_id", 1.0, supplied_only=True), changes={"trace_id": "1.0"})
            deny("parity-recursive-value", dispatch_change("trace_id", "different", supplied_only=True))
            deny("parity-recursive-tuple", dispatch_change("trace_id", ("trace-exact",), supplied_only=True))
            class CustomValue:
                pass
            deny("parity-recursive-custom", dispatch_change("trace_id", CustomValue(), supplied_only=True))
            deny("parity-unequal-payload", lambda body, args: dict(args, body=dict(body, input=body["input"] + "changed")))
            for supplied, wire, label in ((-0.0, 0.0, "minus-vs-plus"), (0.0, -0.0, "plus-vs-minus")):
                def signed_zero(body, args):
                    body["hosted_room_dispatch"]["source_event_seq"] = wire
                    args["forwarded_body"] = encode(body)
                    body["hosted_room_dispatch"]["source_event_seq"] = supplied
                    args["body"] = body
                    return args
                deny("native-domain-invalid-signed-zero-" + label, signed_zero)

            for level in ("top", "dispatch"):
                # Every required field, not just a representative missing member.
                fields = ("input", "hosted_room_dispatch") if level == "top" else (
                    *positive_ints, *identifiers, *digests, "prompt")
                for field in fields:
                    def missing(body, args):
                        del (body if level == "top" else body["hosted_room_dispatch"])[field]
                        return dict(args, body=body, forwarded_body=encode(body))
                    deny("native-missing-" + level + "-" + field, missing)
                def extra(body, args):
                    (body if level == "top" else body["hosted_room_dispatch"])["unexpected"] = "extra"
                    return dict(args, body=body, forwarded_body=encode(body))
                deny("native-extra-" + level, extra)
            for field in positive_ints:
                for kind, value in (("bool", True), ("zero", 0), ("negative", -1), ("float", 1.0), ("string", "1")):
                    deny("native-int-" + field + "-" + kind, dispatch_change(field, value))
            for field in identifiers:
                for kind, value in (("wrongtype", 1), ("empty", ""), ("invalidchar", "bad space"), ("257", "a" * 257)):
                    deny("native-identifier-" + field + "-" + kind, dispatch_change(field, value))
            for field in digests:
                for kind, value in (("wrongtype", 1), ("63", "a" * 63), ("65", "a" * 65),
                                    ("uppercase", "A" * 64), ("nonhex", "g" * 64)):
                    deny("native-digest-" + field + "-" + kind, dispatch_change(field, value))
            deny("native-input-prompt-disagreement", lambda body, args: dict(
                args, body=dict(body, input="different"), forwarded_body=encode(dict(body, input="different"))))
            def prompt_change(prompt):
                def change(body, args):
                    body["input"] = body["hosted_room_dispatch"]["prompt"] = prompt
                    body["hosted_room_dispatch"]["prompt_digest"] = hashlib.sha256(prompt.encode()).hexdigest()
                    raw = encode(body)
                    if len(prompt.encode()) > MAX_REQUEST_BYTES:
                        self.assertGreater(len(raw), MAX_REQUEST_BYTES)  # wire bound dominates
                    return dict(args, body=body, forwarded_body=raw)
                return change
            deny("native-blank-prompt", prompt_change("   "))
            deny("native-prompt-wire-overflow-dominated", prompt_change("a" * (MAX_REQUEST_BYTES + 1)))
            deny("native-wrong-prompt-digest", dispatch_change("prompt_digest", "0" * 64))
            for field in ("room_id", "home_install_id", "member_id", "target_install_id", "target_profile",
                          "capability_digest", "execution_policy_digest"):
                def scope_change(body, args):
                    old = body["hosted_room_dispatch"][field]
                    value = ("f" * 64 if old != "f" * 64 else "e" * 64) if field in digests else old + "x"
                    self.assertTrue(value != old)
                    return dispatch_change(field, value)(body, args)
                deny("scope-bound-" + field, scope_change)
            for label, key in (("empty", ""), ("visible-wrong", "room:other:1"), ("space", "room:bad key:1"),
                               ("control", "room:bad\nkey:1"), ("nonASCII", "room:café:1"), ("256", "a" * 256)):
                deny("key-" + label, lambda body, args: dict(args, idempotency_key=key))
            def key256(body, args):
                body["hosted_room_dispatch"]["task_id"] = task249
                key = "room:" + task249 + ":1"
                self.assertEqual(len(key), 256)
                return dict(args, body=body, forwarded_body=encode(body), idempotency_key=key)
            deny("derived-key-256-identifier-valid", key256)
            http.close()
            self.assertFalse(http.thread.is_alive())
            self.assertEqual(http.server.fileno(), -1)

    def test_whole_budget_connect_write_status_and_trickled_headers_body(self):
        """N01: fragmented loopback responses under ONE absolute deadline.

        Real numeric-loopback TCP server, genuine source fixture, scratch receipt
        store and the public TargetHttpClient.admit_run source lane.  Nothing in
        the socket/selector/HTTPResponse path is faked: the only recorders are
        transparent call-throughs.
        """
        import io
        import selectors as real_selectors
        import types
        from http.client import parse_headers
        from invite_rooms import target_http_io
        from invite_rooms.target_http_io import _SocketFacade

        self.assertTrue(hasattr(TargetHttpClient, "_urlopen"))
        payload_dict = {"run_id": "run_" + "b" * 32, "status": "started", "replayed": False}
        payload = json.dumps(payload_dict, separators=(",", ":")).encode()
        head = (b"HTTP/1.1 202 Accepted\r\nContent-Type: application/json\r\n"
                b"Content-Length: " + str(len(payload)).encode() + b"\r\nConnection: close\r\n\r\n")
        full = head + payload
        GATE = object()

        class Wire:
            """One-connection raw TCP server that replays a scripted response."""
            def __init__(self, steps, gap):
                self.steps, self.gap = steps, gap
                self.sends, self.request, self.errors = [], None, []
                self.release, self.stop, self.done = threading.Event(), threading.Event(), threading.Event()
                self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self.listener.bind(("127.0.0.1", 0))
                self.listener.listen(1)
                self.config = TargetHttpConfig(f"http://127.0.0.1:{self.listener.getsockname()[1]}", "default")
                self.thread = threading.Thread(target=self.run)
                self.thread.start()

            def run(self):
                try:
                    self.listener.settimeout(20)
                    conn, _ = self.listener.accept()
                    with conn:
                        conn.settimeout(20)
                        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                        data = b""
                        while b"\r\n\r\n" not in data:
                            chunk = conn.recv(65536)
                            if not chunk:
                                return
                            data += chunk
                        raw_head, _, rest = data.partition(b"\r\n\r\n")
                        headers = parse_headers(io.BytesIO(raw_head.split(b"\r\n", 1)[1] + b"\r\n\r\n"))
                        while len(rest) < int(headers["Content-Length"]):
                            chunk = conn.recv(65536)
                            if not chunk:
                                return
                            rest += chunk
                        self.request = (rest, headers)
                        for step in self.steps:
                            if step is GATE:
                                if not self.release.wait(20):
                                    return
                                continue
                            try:
                                conn.sendall(step)
                            except OSError as exc:  # client already gave up
                                self.errors.append(type(exc).__name__)
                                return
                            self.sends.append((len(step), time.monotonic()))
                            self.stop.wait(self.gap)  # lets the client consume this segment alone
                except Exception as exc:
                    self.errors.append(type(exc).__name__)
                finally:
                    self.done.set()

            def close(self):
                self.release.set()
                self.stop.set()
                self.thread.join(30)
                self.listener.close()
                if self.thread.is_alive():
                    raise AssertionError("wire server worker did not finish")

        def split(data, sizes):
            out, at = [], 0
            for size in sizes:
                out.append(data[at:at + size]); at += size
            out.append(data[at:])
            return [piece for piece in out if piece]

        def pieces(data, n):
            return [data[i:i + n] for i in range(0, len(data), n)]

        def run_case(label, steps, *, gap, budget, fx, http, directory):
            wire = Wire(steps, gap)
            selector_log, chunks = [], []
            worker_done = threading.Event()
            worker_started = []
            original_worker = TargetHttpClient._admit_source_worker

            def observed_worker(client, **kwargs):
                worker_started.append(True)
                try:
                    return original_worker(client, **kwargs)
                finally:
                    worker_done.set()

            def settle_wire():
                wire.release.set()
                if not wire.done.wait(30):
                    raise AssertionError("N01 wire server did not settle during cleanup")
                wire.close()
                if worker_started and not worker_done.wait(30):
                    raise AssertionError("N01 admission worker did not settle during cleanup")

            class RecordingSelector(real_selectors.DefaultSelector):
                # Transparent call-through: records registered interest + the
                # readiness the REAL selector reported.
                def select(self, timeout=None):
                    interest = tuple(key.events for key in self.get_map().values())
                    ready = super().select(timeout)
                    selector_log.append((interest, tuple(mask for _key, mask in ready)))
                    return ready

            shim = types.SimpleNamespace(**{n: getattr(real_selectors, n) for n in dir(real_selectors)
                                            if not n.startswith("_")})
            shim.DefaultSelector = RecordingSelector
            original_recv = _SocketFacade.recv

            def recording_recv(facade, size, *args):
                data = original_recv(facade, size, *args)
                chunks.append(data)  # only bytes the facade actually returned
                return data

            path = Path(directory) / label
            outcome = {"wire": wire, "selector": selector_log, "chunks": chunks, "path": path}
            try:
                with closing(AdmissionReceiptStore(path)) as store, ExitStack() as cleanup, \
                        mock.patch.object(TargetHttpClient, "_admit_source_worker", new=observed_worker), \
                        mock.patch.object(target_http_io, "selectors", shim), \
                        mock.patch.object(_SocketFacade, "recv", recording_recv):
                    cleanup.callback(settle_wire)
                    source = _source_for(fx, wire.config)
                    args, _ = _args(fx, source, store, "n01-" + label, wire.config)
                    start = time.monotonic()
                    args["deadline"] = start + budget
                    outcome.update(args=args, store=store, start=start, deadline=args["deadline"])
                    try:
                        outcome["result"] = TargetHttpClient(wire.config).admit_run(**args)
                    except TargetHttpError as exc:
                        outcome["error"] = str(exc)
                    outcome["end"] = time.monotonic()
                    outcome["sends_at_return"] = len(wire.sends)
                    outcome["chunks_at_return"] = list(chunks)
                    lookup = _lookup(fx, types.SimpleNamespace(config=wire.config), args)
                    outcome["found"] = store.find(**lookup)
                    outcome["count"] = store.count()
                    if "result" in outcome:
                        outcome["receipt"] = _receipt_witness(
                            self, fx, types.SimpleNamespace(config=wire.config, response=payload_dict),
                            args, store, path)
                    wire.release.set()
                    self.assertTrue(wire.done.wait(30), label + ": wire server did not settle")
                    self.assertTrue(worker_started, label + ": physical admission worker was not reached")
                    self.assertTrue(worker_done.wait(30), label + ": physical admission worker did not settle")
                    outcome["worker_done"] = worker_done.is_set()
                    outcome["chunks_after_release"] = list(chunks)
                    outcome["verified"] = bool(
                        wire.request is not None
                        and _ACTIVE_LOOPBACK.verify_native(wire.request[0], wire.request[1])
                        and wire.request[0] == args["forwarded_body"])
                    settled_generation = fx["scope"].pairing_generation
                    self.assertIs(type(settled_generation), int)
                    outcome["settled_found"] = store.find(
                        scope=fx["scope"], generation=settled_generation,
                        profile=wire.config.profile, base_url=wire.config.base_url,
                        idempotency_key=args["idempotency_key"],
                        request_fingerprint=request_fingerprint(args["body"]))
                    outcome["settled_count"] = store.count()
                    if "result" in outcome:
                        self.assertEqual(outcome["settled_found"], outcome["found"],
                                         label + ": receipt changed after worker settlement")
                        self.assertEqual(outcome["settled_count"], outcome["count"],
                                         label + ": receipt count changed after worker settlement")
                    else:
                        self.assertIsNone(outcome["settled_found"],
                                          label + ": late receipt appeared after worker settlement")
                        self.assertEqual(outcome["settled_count"], 0,
                                         label + ": receipt count changed after timeout settlement")
            finally:
                wire.release.set()
                wire.close()
                if worker_started and not worker_done.wait(30):
                    raise AssertionError("N01 admission worker did not settle in final cleanup")
            return outcome

        def assert_success(case, o, label, expected_sends):
            wire = o["wire"]
            self.assertIsNone(o.get("error"), f"{label}: valid response rejected")
            self.assertTrue(o["verified"], label + ": genuine request not received intact")
            self.assertEqual(o["result"], payload_dict, label)
            self.assertLess(o["end"], o["deadline"], label + ": must finish inside the 5s budget")
            self.assertEqual([n for n, _t in wire.sends], [len(p) for p in expected_sends],
                             label + ": fixture did not write the scripted fragments")
            self.assertEqual(wire.errors, [])
            self.assertEqual(o["receipt"]["run_id"], payload_dict["run_id"])
            self.assertEqual(o["count"], 1)

        fx_holder = {}
        with genuine_source_fixture() as fx, _managed_loopback() as http, \
                tempfile.TemporaryDirectory() as directory:
            fx_holder["fx"] = fx
            common = dict(fx=fx, http=http, directory=directory)

            # ---- positive control: one unfragmented write -------------------
            with self.subTest("unfragmented-control"):
                steps = [full]
                o = run_case("control", steps, gap=0.0, budget=5, **common)
                assert_success(self, o, "control", steps)
                self.assertEqual(len([c for c in o["chunks"] if c]), 1)

            # ---- positive: fragmented headers / body / both, 5s budget ------
            cases = {
                "fragmented-headers": split(head, [7, 25, 60]) + [payload],
                "fragmented-body": [head] + split(payload, [9, 31]),
                "fragmented-both": split(head, [12, 40, 20]) + split(payload, [5, 27]),
            }
            for label, steps in cases.items():
                with self.subTest(label):
                    self.assertEqual(b"".join(steps), full)
                    self.assertGreaterEqual(len(steps), 4)
                    o = run_case(label, steps, gap=0.05, budget=5, **common)
                    assert_success(self, o, label, steps)
                    nonempty = [c for c in o["chunks"] if c]
                    # The client really saw the response arrive piecemeal.
                    self.assertGreaterEqual(len(nonempty), 3, label)
                    self.assertTrue(all(len(c) < len(full) for c in nonempty), label)
                    if label != "fragmented-body":
                        self.assertNotIn(b"\r\n\r\n", nonempty[0], label + ": headers were not split")
                    if label != "fragmented-headers":
                        self.assertTrue(all(payload not in c for c in nonempty), label + ": body was not split")
                    # Readiness contract: every wait for the response uses READ
                    # interest; WRITE interest is only for connect/send.
                    log = o["selector"]
                    write_waits = [e for e in log if e[0] == (real_selectors.EVENT_WRITE,)]
                    read_waits = [e for e in log if e[0] == (real_selectors.EVENT_READ,)]
                    self.assertLessEqual(len(write_waits), 3, label + ": receive waited on WRITE interest")
                    self.assertGreaterEqual(len(read_waits), 2, label)
                    self.assertTrue(all(real_selectors.EVENT_READ in e[1] for e in read_waits), label)
                    self.assertLess(len(log), 60, label + ": selector busy-spin")

            # ---- whole-budget: stall or dribble past ONE deadline ------------
            budget = 1.0
            over = {
                "stalled-headers": ([full[:20], GATE, full[20:]], 0.05),
                "stalled-body": ([head, payload[:10], GATE, payload[10:]], 0.05),
                "trickled-headers": (pieces(head, 3) + [payload], 0.1),
                "trickled-body": ([head] + pieces(payload, 2), 0.1),
            }
            for label, (steps, gap) in over.items():
                with self.subTest(label):
                    self.assertEqual(b"".join(s for s in steps if s is not GATE), full)
                    o = run_case(label, steps, gap=gap, budget=budget, **common)
                    wire = o["wire"]
                    self.assertIsNone(o.get("result"), label + ": late success acknowledged")
                    # Existing public classifications only: the executor's own deadline
                    # (target_admission_ambiguous) races the worker's TimeoutError
                    # mapping (target_unavailable); both are non-success.
                    self.assertIn(o["error"], ("target_unavailable", "target_admission_ambiguous"), label)
                    self.assertGreaterEqual(o["end"], o["deadline"] - 0.05, label + ": gave up before the budget")
                    self.assertLess(o["end"] - o["start"], budget + 1.5, label + ": ignored the absolute deadline")
                    total = len([s for s in steps if s is not GATE])
                    self.assertLess(o["sends_at_return"], total, label + ": response had already completed")
                    # Nothing was consumed after expiry, durable state is clean.
                    self.assertEqual(o["chunks_after_release"], o["chunks_at_return"], label)
                    consumed = b"".join(o["chunks_after_release"])
                    self.assertTrue(full.startswith(consumed) and len(consumed) < len(full), label)
                    self.assertIsNone(o["found"], label)
                    self.assertEqual(o["count"], 0, label)
                    self.assertTrue(o["verified"], label + ": request was not the genuine one")

        # ---- buffered-bytes-after-expiry witness (real socket + facade) -----
        left, right = socket.socketpair()
        selector = real_selectors.DefaultSelector()
        try:
            left.setblocking(False)
            selector.register(left, real_selectors.EVENT_WRITE)
            facade = _SocketFacade(left, selector, time.monotonic() + 5)
            right.sendall(b"line one\nbuffered-bytes")
            self.assertEqual(facade.readline(), b"line one\n")
            self.assertEqual(bytes(facade._buffer), b"buffered-bytes")
            facade._deadline = time.monotonic()  # budget now exhausted
            for label, call in (("read(n)", lambda: facade.read(3)), ("readline", facade.readline),
                                ("read()", facade.read), ("readinto", lambda: facade.readinto(bytearray(4)))):
                with self.assertRaises(TimeoutError, msg=label):
                    call()
            self.assertEqual(bytes(facade._buffer), b"buffered-bytes")  # never handed out
            right.sendall(b"late")
            with self.assertRaises(TimeoutError):
                facade.recv(16)  # socket bytes after expiry are never returned either
            # Fill contract with a live budget: exact amount returned, excess retained.
            facade._deadline = time.monotonic() + 5
            self.assertEqual(facade.read(4), b"buff")
            self.assertEqual(bytes(facade._buffer), b"ered-bytes")
        finally:
            selector.close(); left.close(); right.close()


class _managed_loopback:
    def __enter__(self):
        global _ACTIVE_LOOPBACK
        self.value = _Loopback()
        _ACTIVE_LOOPBACK = self.value
        return self.value
    def __exit__(self, *_args):
        global _ACTIVE_LOOPBACK
        self.value.close()
        _ACTIVE_LOOPBACK = None


if __name__ == "__main__":
    unittest.main()
