"""Focused Card2-A1 scoped registry behaviour tests."""
from __future__ import annotations

import contextlib
import concurrent.futures
import tempfile
import threading
import unittest
import urllib.request
import json
from dataclasses import replace
from typing import Any
from pathlib import Path
from unittest.mock import patch

from invite_rooms.control.adapter import LoopbackAdapter
from invite_rooms.control.connector import LinkBinding, LinkIdentity, TargetConnector
from invite_rooms.control.dispatcher import LinkState
from invite_rooms.control.scoped_registry import ScopedAdapterRegistry, ScopedRegistryError
from invite_rooms.pairing_journal import JournalLinkAuthority
from invite_rooms.peer_registry import MaterialScope, adapter_id_for

try:
    from server.tests import lab_pki
    from server.tests.test_room_invite_data_connector import Native
except ModuleNotFoundError:
    import lab_pki
    from test_room_invite_data_connector import Native
import pairing_support as pairing_support


class Authority:
    def __init__(self, binding, generation=1, state="acknowledged"):
        self.lock = threading.RLock()
        self.state = LinkState(binding, generation, state)

    def current(self, endpoint):
        with self.lock:
            return self.state if endpoint == "ep" else None

    def admit(self, endpoint, generation, operation):
        with self.lock:
            return self.current(endpoint) is not None and self.state.generation == generation


class ScopedRegistryTests(unittest.TestCase):
    def scope(self, *, lobby="lobby", generation=1, fingerprint="a" * 64):
        return MaterialScope(
            owner_installation_id="install:" + "1" * 32, owner_profile="default",
            lobby_id=lobby, planned_room_id="room", member_id="member", invite_id="invite",
            peer_id="peer", endpoint_id="ep", fingerprint=fingerprint,
            target_install_id="install:" + "2" * 32, target_profile="default",
            catalog_digest="c" * 64, policy_digest="d" * 64,
            pairing_generation=generation,
        )

    @contextlib.contextmanager
    def ready(self, *, host="127.0.0.1"):
        with tempfile.TemporaryDirectory(prefix="scoped-registry-") as root:
            a = lab_pki.init_owner(Path(root) / "a", "a")
            b = lab_pki.init_owner(Path(root) / "b", "b")
            scope = self.scope(fingerprint=b["fingerprint"])
            authority = Authority(scope.binding)
            target_binding = LinkBinding("ep", 1, ("127.0.0.1", 1), a["ca_file"],
                                         a["fingerprint"], {"endpoint_id": "ep", "fingerprint": a["fingerprint"]})
            target_authority = Authority(target_binding.expected_binding)
            native = Native()
            target = TargetConnector(
                binding=target_binding,
                identity=LinkIdentity(b["cert_file"], b["key_file"]),
                authority=target_authority, expected_scope=self.expected(scope),
                verify_native=lambda *_: True,
                target_base=native.url,
            )
            target.start()
            binding = LinkBinding("ep", 1, target.address, b["ca_file"],
                                  b["fingerprint"], scope.binding)
            adapter = LoopbackAdapter(authority=authority.current)
            adapter.start(host=host)
            registry = ScopedAdapterRegistry(adapter, authority)
            try:
                yield registry, adapter, authority, scope, binding, LinkIdentity(a["cert_file"], a["key_file"])
            finally:
                registry.close()
                adapter.close()
                target.close()
                native.close()

    def expected(self, scope):
        return {"room_id": scope.planned_room_id, "home_install_id": scope.owner_installation_id,
                "authority_gateway_id": "gateway", "authority_epoch": 1,
                "member_id": scope.member_id, "target_install_id": scope.target_install_id,
                "target_profile": scope.target_profile}

    def register(self, registry, scope, binding, identity):
        return registry.register(scope=scope, binding=binding, identity=identity,
                                 expected_scope=self.expected(scope), verify_native=lambda *_: True)

    def test_before_start_and_after_close_lookup_refuse_without_lazy_bind(self):
        adapter = LoopbackAdapter()
        scope = self.scope()
        authority = Authority(scope.binding)
        registry = ScopedAdapterRegistry(adapter, authority)
        self.assertIsNone(registry.lookup(scope))
        self.assertIsNone(registry.port_for(scope))
        registry.close()
        self.assertIsNone(registry.lookup(scope))
        self.assertFalse(adapter.is_running())

    def test_two_scopes_share_one_adapter_and_have_deterministic_ids(self):
        with self.ready() as (registry, adapter, _, first, binding, identity):
            second = self.scope(lobby="lobby-2", fingerprint=first.fingerprint)
            first_route = self.register(registry, first, binding, identity)
            second_route = self.register(registry, second, binding, identity)
            self.assertEqual(first_route.adapter_id, adapter_id_for(first))
            self.assertEqual(second_route.adapter_id, adapter_id_for(second))
            self.assertNotEqual(first_route.adapter_id, second_route.adapter_id)
            self.assertEqual(first_route.port, second_route.port)
            self.assertTrue(adapter.is_running())

    def test_mismatch_and_failed_registration_publish_nothing(self):
        with self.ready() as (registry, adapter, authority, scope, binding, identity):
            with self.assertRaises(ScopedRegistryError):
                self.register(registry, self.scope(fingerprint="b" * 64), binding, identity)
            self.assertIsNone(registry.lookup(scope))
            authority.state = LinkState(scope.binding, 2, "unpaired")
            with self.assertRaises(ScopedRegistryError):
                self.register(registry, scope, binding, identity)
            self.assertIsNone(registry.lookup(scope))
            self.assertEqual(adapter.route_descriptor(adapter_id_for(scope)), None)

    def test_identical_retry_is_idempotent_and_conflict_does_not_overwrite(self):
        with self.ready() as (registry, adapter, _, scope, binding, identity):
            original = adapter.register_link
            with patch.object(adapter, "register_link", wraps=original) as registered:
                first = self.register(registry, scope, binding, identity)
                retry = self.register(registry, scope, binding, identity)
            self.assertEqual(first, retry)
            self.assertEqual(registered.call_count, 1)
            with self.assertRaises(ScopedRegistryError):
                registry.register(scope=scope, binding=binding, identity=identity,
                                  expected_scope={"room_id": "wrong"}, verify_native=lambda *_: True)
            self.assertEqual(registry.lookup(scope), first)

    def test_external_deregister_and_journal_revocation_invalidate_cached_route(self):
        with self.ready() as (registry, adapter, authority, scope, binding, identity):
            route = self.register(registry, scope, binding, identity)
            adapter.deregister_route(route.adapter_id, endpoint_id=scope.endpoint_id, generation=1)
            self.assertIsNone(registry.lookup(scope))
            self.register(registry, scope, binding, identity)
            authority.state = LinkState(scope.binding, 2, "unpaired")
            self.assertIsNone(registry.lookup(scope))

    def test_unavailable_retry_retains_old_owner_for_close(self):
        with self.ready() as (registry, adapter, authority, scope, binding, identity):
            route = self.register(registry, scope, binding, identity)
            authority.state = LinkState(scope.binding, 2, "unpaired")
            with self.assertRaisesRegex(ScopedRegistryError, "route_unavailable"):
                self.register(registry, scope, binding, identity)
            self.assertTrue(registry.revoke(scope))
            self.assertIsNone(adapter.route_descriptor(route.adapter_id))

    def test_native_presence_read_does_not_lock_out_sibling_revoke(self):
        with self.ready() as (registry, adapter, _, scope, binding, identity):
            sibling = replace(scope, lobby_id="lock-sibling")
            self.register(registry, scope, binding, identity)
            self.register(registry, sibling, binding, identity)
            entered, release, started, done = (threading.Event() for _ in range(4))
            original_descriptor = adapter.route_descriptor
            original_owned = adapter.owned_route_descriptor

            def owned(*args, **kwargs):
                if threading.current_thread().name.startswith("retry"):
                    return None
                return original_owned(*args, **kwargs)

            def descriptor(*args, **kwargs):
                if threading.current_thread().name.startswith("retry"):
                    entered.set()
                    if not release.wait(5):
                        raise AssertionError("native descriptor gate timeout")
                return original_descriptor(*args, **kwargs)

            def revoke():
                started.set()
                value = registry.revoke(sibling)
                done.set()
                return value

            with patch.object(adapter, "owned_route_descriptor", side_effect=owned), \
                    patch.object(adapter, "route_descriptor", side_effect=descriptor), \
                    concurrent.futures.ThreadPoolExecutor(
                        1, thread_name_prefix="retry") as retry_pool, \
                    concurrent.futures.ThreadPoolExecutor(1) as revoke_pool:
                retried = retry_pool.submit(self.register, registry, scope, binding, identity)
                try:
                    self.assertTrue(entered.wait(5), "native read not reached")
                    revoked = revoke_pool.submit(revoke)
                    self.assertTrue(started.wait(5))
                    completed_independently = done.wait(0.5)
                finally:
                    release.set()
                with self.assertRaises(ScopedRegistryError):
                    retried.result(5)
                self.assertTrue(revoked.result(5))
            self.assertTrue(completed_independently,
                            "native route presence read executes while registry lock held")

    def test_descriptor_read_failure_retains_old_owner_but_external_loss_repairs(self):
        with self.ready() as (registry, adapter, _, scope, binding, identity):
            route = self.register(registry, scope, binding, identity)
            with patch.object(adapter, "owned_route_descriptor", return_value=None):
                with self.assertRaisesRegex(ScopedRegistryError, "route_unavailable"):
                    self.register(registry, scope, binding, identity)
            self.assertTrue(registry.revoke(scope))
            self.assertIsNone(adapter.route_descriptor(route.adapter_id))

    def test_invalid_scope_shape_is_reported_by_native_validator(self):
        class NotAScope:
            pass
        with self.ready() as (registry, *_):
            with self.assertRaisesRegex(ScopedRegistryError, "invalid_material_scope"):
                registry.lookup(NotAScope())

    def test_concurrent_register_lookup_revoke_has_no_partial_entry(self):
        with self.ready() as (registry, adapter, _, scope, binding, identity):
            original = adapter.register_link
            with patch.object(adapter, "register_link", wraps=original) as registered:
                with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                    routes = list(pool.map(
                        lambda _: self.register(registry, scope, binding, identity), range(8)
                    ))
            self.assertEqual({route.adapter_id for route in routes}, {adapter_id_for(scope)})
            self.assertEqual(registered.call_count, 1)
            self.assertIsNotNone(registry.lookup(scope))
            self.assertTrue(registry.revoke(scope))
            self.assertIsNone(registry.lookup(scope))

    def test_registered_route_traverses_real_pinned_http_path(self):
        with self.ready() as (registry, _, _, scope, binding, identity):
            route = self.register(registry, scope, binding, identity)
            request = urllib.request.Request(route.url + "/v1/room-members/capabilities",
                                              headers={"Authorization": "HermesRoom " + "x" * 32})
            with urllib.request.urlopen(request, timeout=5) as response:
                self.assertEqual(response.status, 202)

    def test_scope_revoke_preserves_sibling_and_close_is_idempotent(self):
        with self.ready() as (registry, adapter, _, first, binding, identity):
            second = self.scope(lobby="lobby-2", fingerprint=first.fingerprint)
            self.register(registry, first, binding, identity)
            self.register(registry, second, binding, identity)
            self.assertTrue(registry.revoke(first))
            self.assertIsNone(registry.lookup(first))
            self.assertIsNotNone(registry.lookup(second))
            registry.close()
            registry.close()
            self.assertTrue(adapter.is_running())
            self.assertIsNone(adapter.route_descriptor(adapter_id_for(second)))

    def test_ipv6_route_is_structured_but_never_reported_as_ipv4_port(self):
        with self.ready(host="::1") as (registry, _, _, scope, binding, identity):
            route = self.register(registry, scope, binding, identity)
            self.assertEqual(route.host, "::1")
            self.assertIsNone(registry.port_for(scope))

    def test_failed_reservation_wakes_all_duplicates_before_native_publication(self):
        with self.ready() as (registry, adapter, _, scope, binding, identity):
            entered = threading.Event()
            release = threading.Event()

            def fail_before_publish(*args, **kwargs):
                entered.set()
                release.wait(5)
                raise RuntimeError("insertion failed")

            with patch.object(adapter, "register_link", side_effect=fail_before_publish):
                with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
                    first = pool.submit(self.register, registry, scope, binding, identity)
                    self.assertTrue(entered.wait(5))
                    duplicates = [pool.submit(self.register, registry, scope, binding, identity)
                                  for _ in range(4)]
                    release.set()
                    with self.assertRaises(RuntimeError):
                        first.result(timeout=5)
                    for duplicate in duplicates:
                        with self.assertRaises(RuntimeError):
                            duplicate.result(timeout=5)
            self.assertIsNone(registry.lookup(scope))

    def test_stale_absence_observation_cannot_replace_pending_repair(self):
        with self.ready() as (registry, adapter, _, scope, binding, identity):
            base: dict[str, Any] = dict(
                scope=scope, binding=binding, identity=identity,
                expected_scope=self.expected(scope), verify_native=lambda *_: True,
            )
            old = registry.register(**base)
            adapter.deregister_route(old.adapter_id, endpoint_id=scope.endpoint_id, generation=1)
            b_observing = threading.Event()
            release_b_observation = threading.Event()
            a_inserting = threading.Event()
            release_a = threading.Event()
            c_waiting = threading.Event()
            b_decided = threading.Event()
            calls = []
            results = {}
            captured_pending = None
            real_presence = adapter.route_descriptor
            real_insert = adapter.register_link

            def presence(*args, **kwargs):
                if threading.current_thread().name == "repair-B":
                    b_observing.set()
                    if not release_b_observation.wait(5):
                        raise AssertionError("controlled observation timeout")
                return real_presence(*args, **kwargs)

            def insert(*args, **kwargs):
                who = threading.current_thread().name
                calls.append(who)
                if who == "repair-B":
                    b_decided.set()
                if who == "repair-A":
                    a_inserting.set()
                    if not release_a.wait(5):
                        raise AssertionError("controlled insertion timeout")
                return real_insert(*args, **kwargs)

            def register(who):
                try:
                    results[who] = registry.register(**base)
                except BaseException as exc:
                    results[who] = exc

            threads = [threading.Thread(target=register, args=(name,), name=name)
                       for name in ("repair-B", "repair-A", "duplicate-C")]
            b, a, c = threads
            try:
                with patch.object(adapter, "route_descriptor", side_effect=presence), \
                        patch.object(adapter, "register_link", side_effect=insert):
                    b.start()
                    self.assertTrue(b_observing.wait(5))
                    a.start()
                    self.assertTrue(a_inserting.wait(5))
                    captured_pending = registry._pending[old.adapter_id]
                    real_wait = captured_pending.wait

                    def observed_wait(*args, **kwargs):
                        if threading.current_thread().name == "duplicate-C":
                            c_waiting.set()
                        elif threading.current_thread().name == "repair-B":
                            b_decided.set()
                        return real_wait(*args, **kwargs)

                    with patch.object(captured_pending, "wait", side_effect=observed_wait):
                        c.start()
                        self.assertTrue(c_waiting.wait(5))
                        release_b_observation.set()
                        # Hold A until B chooses the real pending wait or a wrong insertion.
                        self.assertTrue(b_decided.wait(5))
                        release_a.set()
                        for thread in (a, b, c):
                            thread.join(5)
                    self.assertFalse([thread.name for thread in threads if thread.is_alive()])
                self.assertEqual(calls, ["repair-A"])
                self.assertTrue(all(not isinstance(results.get(name), BaseException)
                                    for name in ("repair-A", "repair-B", "duplicate-C")))
            finally:
                release_b_observation.set()
                release_a.set()
                if captured_pending is not None:
                    captured_pending.set()
                for thread in threads:
                    if thread.ident is not None:
                        thread.join(5)
                self.assertFalse([thread.name for thread in threads if thread.is_alive()])

    def test_failed_partial_publication_retains_cleanup_owner_and_close_wakes_queue(self):
        with self.ready() as (registry, adapter, _, scope, binding, identity):
            entered = threading.Event()
            release = threading.Event()
            original_register = adapter.register_link
            original_remove = adapter.deregister_route

            def publish_then_fail(*args, **kwargs):
                entered.set()
                release.wait(5)
                original_register(*args, **kwargs)
                raise RuntimeError("post-publication failure")

            def fail_cleanup(*args, **kwargs):
                raise RuntimeError("cleanup failed")

            with patch.object(adapter, "register_link", side_effect=publish_then_fail), \
                    patch.object(adapter, "deregister_route", side_effect=fail_cleanup):
                with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
                    first = pool.submit(self.register, registry, scope, binding, identity)
                    self.assertTrue(entered.wait(5))
                    duplicates = [pool.submit(
                        self.register, registry, scope, binding, identity) for _ in range(4)]
                    close_result = pool.submit(registry.close)
                    release.set()
                    with self.assertRaises(ExceptionGroup):
                        first.result(timeout=5)
                    for duplicate in duplicates:
                        with self.assertRaises(ScopedRegistryError) as refused:
                            duplicate.result(timeout=5)
                        self.assertIn(str(refused.exception), {"registry_closed", "route_cleanup_pending"})
                    with self.assertRaises(ExceptionGroup):
                        close_result.result(timeout=5)
            self.assertIsNotNone(adapter.route_descriptor(adapter_id_for(scope)))
            adapter.deregister_route = original_remove
            registry.close()
            self.assertIsNone(adapter.route_descriptor(adapter_id_for(scope)))

    def test_real_journal_registry_runs_and_generation_cutover_stops_effect(self):
        """Positive witness: production Journal authority plus the real /v1/runs path."""
        from invite_rooms import identity as identity_module
        with tempfile.TemporaryDirectory(prefix="scoped-journal-") as root:
            root = Path(root)
            a_side = pairing_support.Side(root, "a", pairing_support.INSTALL_A,
                                          template=pairing_support.identity_template(pairing_support.INSTALL_A))
            b_side = pairing_support.Side(root, "b", pairing_support.INSTALL_B,
                                          template=pairing_support.identity_template(pairing_support.INSTALL_B))
            a_service, b_service = a_side.service(), b_side.service()
            paired = pairing_support.drive_pairing(a_service, b_service)
            ack = a_service.ack_request(paired["a_id"])
            a_service.complete_ack(paired["a_id"], b_service.handle_pair_ack(ack.request))
            journal_authority = JournalLinkAuthority(a_service.journal)
            endpoint = next(iter(a_service.journal.snapshot().links))
            generation = a_service.journal.snapshot().link_generation(
                a_service.journal.snapshot().links[endpoint])
            trust = journal_authority.peer_trust(endpoint, generation, role="host")
            self.assertIsNotNone(trust)
            trust_binding = trust.binding_dict()
            target_trust = b_service.authority.peer_trust(endpoint, generation, role="joining")
            self.assertIsNotNone(target_trust)
            target_binding_dict = target_trust.binding_dict()
            a_bundle = identity_module.load_identity(a_side.identity_dir,
                                                      installation_id=pairing_support.INSTALL_A,
                                                      profile="default")
            b_bundle = identity_module.load_identity(b_side.identity_dir,
                                                      installation_id=pairing_support.INSTALL_B,
                                                      profile="default")
            native = Native()
            target_binding = LinkBinding(endpoint, generation, ("127.0.0.1", 1),
                                         a_bundle.ca_file, a_bundle.leaf_fingerprint,
                                         target_binding_dict)
            target = TargetConnector(binding=target_binding,
                                     identity=LinkIdentity(b_bundle.leaf_cert_file, b_bundle.leaf_key_file),
                                     authority=b_service.authority,
                                     expected_scope={"room_id": "room", "home_install_id": "install:" + "a" * 32,
                                                     "authority_gateway_id": "gateway", "authority_epoch": 1,
                                                     "member_id": "member", "target_install_id": trust_binding["target_install_id"],
                                                     "target_profile": "default"},
                                     verify_native=lambda *_: True, target_base=native.url)
            target.start()
            binding = LinkBinding(endpoint, generation, target.address, b_bundle.ca_file,
                                  trust.fingerprint, trust_binding)
            scope = MaterialScope(owner_installation_id="install:" + "a" * 32,
                                  owner_profile="default", lobby_id="lobby-" + "1" * 32,
                                  planned_room_id="room", member_id="member", invite_id="invite",
                                  peer_id=trust.peer_id, endpoint_id=endpoint,
                                  fingerprint=trust.fingerprint,
                                  target_install_id=trust_binding["target_install_id"],
                                  target_profile="default", catalog_digest=trust_binding["catalog_digest"],
                                  policy_digest=trust_binding["policy_digest"],
                                  pairing_generation=generation)
            expected = {"room_id": "room", "home_install_id": "install:" + "a" * 32,
                        "authority_gateway_id": "gateway", "authority_epoch": 1,
                        "member_id": "member", "target_install_id": trust_binding["target_install_id"],
                        "target_profile": "default"}
            adapter = LoopbackAdapter(authority=journal_authority.current)
            adapter.start()
            registry = ScopedAdapterRegistry(adapter, journal_authority)
            try:
                route = registry.register(scope=scope, binding=binding,
                                          identity=LinkIdentity(a_bundle.leaf_cert_file, a_bundle.leaf_key_file),
                                          expected_scope=expected, verify_native=lambda *_: True)
                dispatch = dict(expected, protocol_version=2, task_id="task-1",
                                execution_generation=1, source_event_seq=1,
                                cancellation_scope_id="cancel-1", prompt="run", prompt_digest="p" * 64,
                                capability_digest="c" * 64, execution_policy_digest="d" * 64,
                                trace_id="trace-1")
                body = json.dumps({"input": "run", "hosted_room_dispatch": dispatch},
                                  sort_keys=True).encode()
                request = urllib.request.Request(route.url + "/v1/runs", data=body,
                                                 headers={"Content-Type": "application/json",
                                                          "Authorization": "HermesRoom " + "x" * 32,
                                                          "Idempotency-Key": "idem-1"})
                with urllib.request.urlopen(request, timeout=5) as response:
                    self.assertEqual(response.status, 202)
                self.assertEqual(len(native.calls), 1)
                self.assertEqual(native.calls[-1][0:2], ("POST", "/v1/runs"))
                forwarded = {key.lower(): value for key, value in native.calls[-1][2].items()}
                self.assertEqual(forwarded["authorization"], "HermesRoom " + "x" * 32)
                self.assertEqual(forwarded["content-type"], "application/json")
                self.assertEqual(forwarded["idempotency-key"], "idem-1")
                self.assertEqual(native.calls[-1][3], body)
                before = len(native.calls)
                b_service.unpair(target_trust.peer_id)
                with self.assertRaises(urllib.error.HTTPError) as target_denied:
                    urllib.request.urlopen(request, timeout=5)
                self.assertEqual(target_denied.exception.code, 502)
                self.assertEqual(len(native.calls), before)
                a_service.unpair(trust.peer_id)
                with self.assertRaises(urllib.error.HTTPError) as denied:
                    urllib.request.urlopen(request, timeout=5)
                self.assertEqual(denied.exception.code, 404)
                self.assertEqual(len(native.calls), before)
            finally:
                registry.close(); adapter.close(); target.close(); native.close()

    def _r04_callback_case(self, mode, *, failure_mode=None):
        original_native_init = Native.__init__

        def native_init(native):
            original_native_init(native)
            native.body = b'{"run_id":"run-1","status":"started","replayed":false}'

        patcher = patch.object(Native, "__init__", native_init)
        patcher.start()
        self.addCleanup(patcher.stop)
        with self.ready() as (registry, adapter, _, scope, binding, identity):
            seen = []

            def register(label, **extra):
                return registry.register(
                    scope=scope, binding=binding, identity=identity,
                    expected_scope=self.expected(scope),
                    verify_native=lambda *_: seen.append(label + "_verify") or True,
                    on_admitted=lambda *_: seen.append(label + "_admit"), **extra)

            initial = mode.startswith("initial")
            if not initial:
                old = register("old")
                adapter.deregister_route(old.adapter_id, endpoint_id=scope.endpoint_id, generation=1)
            insertion_mode = failure_mode or mode
            if insertion_mode in {"before", "after", "initial-before", "initial-after"}:
                real = adapter.register_link

                def fail(*args, **kwargs):
                    if insertion_mode in {"after", "initial-after"}:
                        real(*args, **kwargs)
                    raise RuntimeError("repair insertion fault")

                with patch.object(adapter, "register_link", side_effect=fail):
                    with self.assertRaisesRegex(RuntimeError, "repair insertion fault"):
                        register("failed")
            if mode == "conflict":
                with patch.object(adapter, "register_link", wraps=adapter.register_link) as factory:
                    with self.assertRaisesRegex(ScopedRegistryError, "scoped_route_conflict"):
                        register("conflict", backend_timeout_seconds=2)
                    factory.assert_not_called()
            elif mode == "revoke":
                self.assertTrue(registry.revoke(scope))

            route = register("new")
            dispatch = dict(
                self.expected(scope), protocol_version=2, task_id="task-1",
                execution_generation=1, source_event_seq=1, cancellation_scope_id="cancel-1",
                prompt="run", prompt_digest="p" * 64, capability_digest="c" * 64,
                execution_policy_digest="d" * 64, trace_id="trace-1",
            )
            body = json.dumps({"input": "run", "hosted_room_dispatch": dispatch}).encode()
            request = urllib.request.Request(
                route.url + "/v1/runs", data=body,
                headers={"Authorization": "HermesRoom " + "x" * 32,
                         "Content-Type": "application/json", "Idempotency-Key": "idem-r04"},
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                self.assertEqual(response.status, 202)
            request = urllib.request.Request(
                route.url + "/v1/runs/run-1",
                headers={"Authorization": "HermesRoom " + "x" * 32},
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                self.assertEqual(response.status, 202)
            expected = "new" if initial or mode == "revoke" else "old"
            self.assertEqual(seen, [expected + "_admit", expected + "_verify"])

    def test_r04_failed_repair_before_insertion_retains_callbacks(self):
        for mode in ("before", "after"):
            with self.subTest(mode=mode):
                self._r04_callback_case(mode)

    def test_r04_failed_repair_preserves_conflict_and_first_callbacks(self):
        for mode in ("before", "after"):
            with self.subTest(mode=mode):
                self._r04_callback_case("conflict", failure_mode=mode)

    def test_r04_explicit_revoke_after_failed_repair_allows_new_callbacks(self):
        for mode in ("before", "after"):
            with self.subTest(mode=mode):
                self._r04_callback_case("revoke", failure_mode=mode)

    def test_r04_failed_initial_attempt_does_not_commit_callbacks(self):
        for mode in ("before", "after"):
            with self.subTest(mode=mode):
                self._r04_callback_case("initial-" + mode)


if __name__ == "__main__":
    unittest.main()
