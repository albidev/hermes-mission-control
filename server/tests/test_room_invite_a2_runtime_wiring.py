"""Focused Card2-A2a tests for caller-owned scoped route injection.

The positive loader path uses the real private config, real PeerRegistry,
RoomInviteStore, MaterialReceiptStore and RegistrySource. The registry itself
is a concrete A1 ScopedAdapterRegistry owned by the test and is never closed
by the loader or route entrypoint. Native Core is intentionally not claimed by
these tests; the existing service/coordinator remains the material consumer.
"""
from __future__ import annotations

import asyncio
import copy
import contextlib
import io
import json
import os
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from invite_rooms import routes, runtime_config
from invite_rooms.control.adapter import LoopbackAdapter
from invite_rooms.control.connector import LinkBinding, LinkIdentity, TargetConnector
from invite_rooms.control.scoped_registry import ScopedAdapterRegistry
from invite_rooms.identity import load_identity
from invite_rooms.pairing_journal import JournalLinkAuthority
from invite_rooms.peer_registry import MaterialScope, adapter_id_for
from room_invite_store import RoomInviteStore

import pairing_support

REAL_PEER_ID = "peer-" + "b" * 32

try:
    from test_room_invite_routes import _peer_registry_payload, _write_private_file
except ModuleNotFoundError:
    from server.tests.test_room_invite_routes import _peer_registry_payload, _write_private_file


class _Handler:
    def __init__(self):
        self.headers = {}
        self.responses = []

    def _unauthorized(self):
        self.responses.append((401, {"error": "unauthorized"}))

    def _json(self, status, body):
        self.responses.append((status, body))


class _RequestHandler(_Handler):
    def __init__(self, body=b"{}"):
        super().__init__()
        self.path = routes.PREFIX + "/lobbies/lobby/activate"
        self.headers = {"Content-Length": str(len(body)), "Content-Type": "application/json"}
        self.rfile = io.BytesIO(body)


class _Authority:
    def current(self, _endpoint):
        return None


class RuntimeWiringTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="room-invite-a2a-")
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.store_path = root / "owner" / "invites.db"
        self.config_path = root / "owner" / "config.json"
        self.peers_path = root / "owner" / "peers.json"
        _write_private_file(self.peers_path, _peer_registry_payload())
        _write_private_file(self.config_path, json.dumps({
            "store_path": str(self.store_path),
            "installation_id": "install:" + "a" * 32,
            "profile": "default",
            "peer_registry_path": str(self.peers_path),
        }))
        self.env = {"MISSION_CONTROL_ROOM_INVITES_CONFIG": str(self.config_path)}

    def _owned_registry(self):
        adapter = LoopbackAdapter()
        adapter.start(host="127.0.0.1", port=0)
        registry = ScopedAdapterRegistry(adapter, _Authority())
        self.addCleanup(adapter.close)
        self.addCleanup(registry.close)
        return registry, adapter

    def test_real_private_loader_installs_concrete_registry_port_provider(self):
        registry, adapter = self._owned_registry()
        port = adapter._server.server_address[1]
        with patch.object(registry, "port_for", return_value=port) as port_for:
            loaded = runtime_config.load_service(self.env, scoped_adapter_registry=registry)
            self.assertIsNotNone(loaded)
            assert loaded is not None
            service, ctx, config = loaded
            self.assertEqual(ctx.installation_id, "install:" + "a" * 32)
            self.assertEqual(ctx.profile, "default")
            self.assertIsNotNone(config)
            self.assertTrue(service.registry.material_capability())
            scope = object()
            self.assertEqual(service.registry._route_provider(scope), port)
            port_for.assert_called_once_with(scope)
            self.assertTrue(adapter.is_running())

    def test_real_receipt_resolution_uses_exact_scoped_ipv4_link(self):
        registry, adapter = self._owned_registry()
        port = adapter._server.server_address[1]
        with patch.object(registry, "port_for", return_value=port):
            loaded = runtime_config.load_service(self.env, scoped_adapter_registry=registry)
            self.assertIsNotNone(loaded)
            assert loaded is not None
            service, ctx, _config = loaded
            peer = service.registry.lookup("owner-b")
            binding = service.registry.binding_for("owner-b")
            scope = MaterialScope(
                owner_installation_id=ctx.installation_id,
                owner_profile=ctx.profile,
                lobby_id="lobby-" + "1" * 32,
                planned_room_id="room-" + "2" * 32,
                member_id="peer-b",
                invite_id="invite-" + "3" * 32,
                peer_id="owner-b",
                pairing_generation=peer.pairing_generation,
                **binding,
            )
            service.registry._receipt_store.admit(scope, {
                "binding": binding,
                "catalog": service.registry._peers["owner-b"].catalog,
                "grant": "fixture-grant",
            })
            material = asyncio.run(service.registry.resolve_scoped_material(scope))
            self.assertEqual(material.grant, "fixture-grant")
            self.assertEqual(material.catalog, service.registry._peers["owner-b"].catalog)
            self.assertEqual(
                material.adapter_url,
                f"http://127.0.0.1:{port}/links/{adapter_id_for(scope)}",
            )
            self.assertTrue(adapter.is_running())

    def test_missing_injection_preserves_fail_closed_capability_and_does_not_use_legacy(self):
        legacy_called = []

        async def legacy(_scope):
            legacy_called.append(True)

        loaded = runtime_config.load_service(self.env, material_resolver=legacy)
        self.assertIsNotNone(loaded)
        service, _, _ = loaded
        self.assertFalse(service.registry.material_capability())
        self.assertEqual(legacy_called, [])

    def test_default_builder_provider_is_preserved_without_scoped_injection(self):
        sentinel = object()
        original = runtime_config.build_peer_registry

        def builder(path):
            registry = original(path)
            registry._route_provider = lambda _scope: sentinel
            return registry

        with patch.object(runtime_config, "build_peer_registry", side_effect=builder):
            loaded = runtime_config.load_service(self.env)
        self.assertIsNotNone(loaded)
        assert loaded is not None
        service, _ctx, _config = loaded
        self.assertIs(service.registry._route_provider(self), sentinel)

    def test_invalid_injection_is_rejected_before_duck_typed_method(self):
        class FakeRegistry:
            def __init__(self):
                self.called = False

            def port_for(self, _scope):
                self.called = True
                raise AssertionError("invalid registry was invoked")

        fake = FakeRegistry()
        self.assertIsNone(runtime_config.load_service(self.env, scoped_adapter_registry=fake))
        self.assertFalse(fake.called)
        self.assertIsNone(runtime_config.load_service(self.env, scoped_adapter_registry={"port": 1}))
        self.assertIsNone(runtime_config.load_service(self.env, scoped_adapter_registry=lambda _: 1))

    def test_uninitialized_subclass_is_rejected_before_port_for(self):
        class Subclass(ScopedAdapterRegistry):
            def __init__(self):
                self.calls = []

            def port_for(self, scope):
                self.calls.append(scope)
                return 49123

        registry = Subclass()
        self.assertIsNone(runtime_config.load_service(self.env, scoped_adapter_registry=registry))
        self.assertEqual(registry.calls, [])

    def test_spoofed_class_reference_is_rejected_without_reading_it(self):
        class Spoof:
            def __getattribute__(self, name):
                if name == "__class__":
                    raise AssertionError("spoofed __class__ must not be read")
                return object.__getattribute__(self, name)

            def port_for(self, _scope):
                raise AssertionError("spoofed registry must not be invoked")

        self.assertIsNone(runtime_config.load_service(self.env, scoped_adapter_registry=Spoof()))

    def test_route_forwards_only_explicit_injection_and_preserves_default_loader_shape(self):
        calls = []
        registry, _adapter = self._owned_registry()

        def loader(**kwargs):
            calls.append(kwargs)
            return None

        handler = _Handler()
        with patch.object(routes.runtime_config, "load_service", side_effect=loader):
            self.assertTrue(routes.handle_request(
                handler, "GET", routes.PREFIX + "/unknown", {}, lambda _handler: True,
                scoped_adapter_registry=registry,
            ))
        self.assertEqual(calls, [{
            "material_resolver": routes._MATERIAL_RESOLVER,
            "scoped_adapter_registry": registry,
        }])

        calls.clear()
        handler = _Handler()
        with patch.object(routes.runtime_config, "load_service", side_effect=loader):
            self.assertTrue(routes.handle_request(
                handler, "GET", routes.PREFIX + "/unknown", {}, lambda _handler: True,
            ))
        self.assertEqual(calls, [{"material_resolver": routes._MATERIAL_RESOLVER}])

    def test_auth_rejection_does_not_load_or_touch_injected_registry(self):
        registry, _adapter = self._owned_registry()
        with patch.object(routes.runtime_config, "load_service") as loader:
            handler = _Handler()
            self.assertTrue(routes.handle_request(
                handler, "GET", routes.PREFIX + "/unknown", {}, lambda _handler: False,
                scoped_adapter_registry=registry,
            ))
        loader.assert_not_called()
        self.assertEqual(handler.responses, [(401, {"error": "unauthorized"})])


class RealPairedActivationWitness(unittest.TestCase):
    """Original route -> loader -> service -> real coordinator witness.

    Pairing, journal authority, receipt scope and A1 route are real.  Only the
    native Core RPC is a recording transport, which is the permitted seam for
    this MC consumer test.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="room-invite-a2a-real-")
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.a = pairing_support.Side(
            root, "a", pairing_support.INSTALL_A,
            template=pairing_support.identity_template(pairing_support.INSTALL_A),
        )
        self.b = pairing_support.Side(
            root, "b", pairing_support.INSTALL_B,
            template=pairing_support.identity_template(pairing_support.INSTALL_B),
        )
        self.a_pair, self.b_pair = self.a.service(), self.b.service()
        paired = pairing_support.drive_pairing(self.a_pair, self.b_pair)
        ack = self.a_pair.ack_request(paired["a_id"])
        self.a_pair.complete_ack(paired["a_id"], self.b_pair.handle_pair_ack(ack.request))

        self.store_path = root / "owner" / "invites.db"
        self.config_path = root / "owner" / "config.json"
        self.token_path = root / "owner" / "native.token"
        _write_private_file(self.token_path, "fixture-native-token")
        _write_private_file(self.config_path, json.dumps({
            "store_path": str(self.store_path),
            "installation_id": pairing_support.INSTALL_A,
            "profile": "default",
            "peer_registry_path": str(self.a.peers_path),
            "native_rpc": {"endpoint": "ws://127.0.0.1:1", "token_path": str(self.token_path)},
            "pairing": {
                "identity_dir": str(self.a.identity_dir),
                "journal_dir": str(self.a.journal_dir),
                "peers_path": str(self.a.peers_path),
            },
        }))
        self.env = {"MISSION_CONTROL_ROOM_INVITES_CONFIG": str(self.config_path)}
        self._env = patch.dict(os.environ, self.env, clear=False)
        self._env.start()
        self.addCleanup(self._env.stop)
        self.authority = JournalLinkAuthority(self.a_pair.journal)
        self.adapter = LoopbackAdapter(authority=self.authority.current)
        self.adapter.start(host="127.0.0.1", port=0)
        self.registry = ScopedAdapterRegistry(self.adapter, self.authority)
        self.addCleanup(self.adapter.close)
        self.addCleanup(self.registry.close)
        from test_room_invite_data_connector import Native
        self.native = Native()
        self.addCleanup(self.native.close)

    def _fixture(self, *, admit=True):
        self._fixture_admit = admit
        loaded = runtime_config.load_service(self.env, scoped_adapter_registry=self.registry)
        self.assertIsNotNone(loaded)
        assert loaded is not None
        service, ctx, config = loaded
        binding = service.registry.binding_for(REAL_PEER_ID)
        lobby = service.create_lobby(ctx, name="real-room", roster=[
            {"member_id": "owner", "profile": "default", "handle": "owner"},
            {"member_id": "peer-b", "profile": "default", "handle": "target", "target": {
                "kind": "peer", "peer_id": REAL_PEER_ID, "installation_id": pairing_support.INSTALL_B,
                "profile": "default", "capability_digest": binding["catalog_digest"],
            }},
        ])
        issued = service.issue_invite(ctx, lobby["lobby_id"], "peer-b", invitee_label="fixture")
        store = RoomInviteStore(self.store_path)
        store.redeem(lobby["lobby_id"], issued.invite_id, issued.secret, binding=binding)
        store.decide_host(lobby["lobby_id"], issued.invite_id, binding=binding, approved=True)
        store.consent_joining(lobby["lobby_id"], issued.invite_id, binding=binding, consented=True)
        peer = service.registry.lookup(REAL_PEER_ID)
        snapshot = self.a_pair.journal.snapshot()
        endpoint = peer.endpoint_id
        generation = snapshot.link_generation(snapshot.links[endpoint])
        trust = self.authority.peer_trust(endpoint, generation, role="host")
        self.assertIsNotNone(trust)
        assert trust is not None
        a_bundle = load_identity(self.a.identity_dir, installation_id=pairing_support.INSTALL_A, profile="default")
        b_bundle = load_identity(self.b.identity_dir, installation_id=pairing_support.INSTALL_B, profile="default")
        target_trust = self.b_pair.authority.peer_trust(endpoint, generation, role="joining")
        self.assertIsNotNone(target_trust)
        assert target_trust is not None
        target_binding = LinkBinding(endpoint, generation, ("127.0.0.1", 1),
                                     str(a_bundle.ca_file), a_bundle.leaf_fingerprint,
                                     target_trust.binding_dict())
        target = TargetConnector(binding=target_binding,
                                 identity=LinkIdentity(str(b_bundle.leaf_cert_file), str(b_bundle.leaf_key_file)),
                                 authority=self.b_pair.authority, expected_scope={
                                     "room_id": lobby["planned_room_id"],
                                     "home_install_id": ctx.installation_id,
                                     "authority_gateway_id": ctx.installation_id,
                                     "authority_epoch": 1,
                                     "member_id": "peer-b",
                                     "target_install_id": pairing_support.INSTALL_B,
                                     "target_profile": "default",
                                 }, verify_native=lambda *_: True, target_base=self.native.url)
        target.start()
        self.addCleanup(target.close)
        binding_obj = LinkBinding(endpoint, generation, target.address,
                                  b_bundle.ca_file, trust.fingerprint, trust.binding_dict())
        scope = MaterialScope(owner_installation_id=ctx.installation_id, owner_profile=ctx.profile,
                              lobby_id=lobby["lobby_id"], planned_room_id=lobby["planned_room_id"],
                              member_id="peer-b", invite_id=issued.invite_id, peer_id=REAL_PEER_ID,
                              pairing_generation=generation, **binding)
        expected_scope = {"room_id": lobby["planned_room_id"], "home_install_id": ctx.installation_id,
                          "authority_gateway_id": ctx.installation_id, "authority_epoch": 1,
                          "member_id": "peer-b", "target_install_id": pairing_support.INSTALL_B,
                          "target_profile": "default"}
        route = self.registry.register(scope=scope, binding=binding_obj, identity=LinkIdentity(str(a_bundle.leaf_cert_file), str(a_bundle.leaf_key_file)),
                                       expected_scope=expected_scope, verify_native=lambda *_: True)
        catalog = copy.deepcopy(service.registry._peers[REAL_PEER_ID].catalog)
        if admit:
            service.registry._receipt_store.admit(scope, {"binding": binding, "catalog": catalog, "grant": "fixture-grant-real"})
        return service, ctx, config, lobby, route, binding, catalog, scope, target

    @staticmethod
    def _direct_run(route, scope, key):
        body = json.dumps({"input": "run", "hosted_room_dispatch": {
            "room_id": scope.planned_room_id, "home_install_id": scope.owner_installation_id,
            "authority_gateway_id": scope.owner_installation_id, "authority_epoch": 1,
            "member_id": scope.member_id, "target_install_id": scope.target_install_id,
            "target_profile": scope.target_profile, "protocol_version": 2,
            "task_id": "task-a2a", "execution_generation": 1, "source_event_seq": 1,
            "cancellation_scope_id": "cancel-a2a", "prompt": "run",
            "prompt_digest": "p" * 64, "capability_digest": "c" * 64,
            "execution_policy_digest": "d" * 64, "trace_id": "trace-a2a",
        }}).encode()
        return urllib.request.Request(route.url + "/v1/runs", data=body,
                                      headers={"Content-Type": "application/json",
                                               "Authorization": "HermesRoom " + "x" * 32,
                                               "Idempotency-Key": key})

    def test_two_actual_scopes_share_one_host_adapter_without_material_mixing(self):
        """Metadata-only route witness: both routes are backed by one host listener."""
        first = self._fixture()
        second = self._fixture()
        first_service, _, _, _, first_route, _, _, first_scope, _ = first
        second_service, _, _, _, second_route, _, _, second_scope, _ = second
        self.assertNotEqual(first_route.url, second_route.url)
        self.assertEqual(first_route.port, second_route.port)
        first_material = asyncio.run(first_service.registry.resolve_scoped_material(first_scope))
        second_material = asyncio.run(second_service.registry.resolve_scoped_material(second_scope))
        self.assertEqual(first_material.adapter_url, first_route.url)
        self.assertEqual(second_material.adapter_url, second_route.url)
        self.assertNotEqual(first_material.adapter_url, second_material.adapter_url)

    def test_host_journal_unpair_refuses_route_before_native_effect(self):
        _service, _ctx, _config, _lobby, route, _binding, _catalog, scope, target = self._fixture()
        request = self._direct_run(route, scope, "a2a-unpair")
        self.a_pair.unpair(REAL_PEER_ID)
        with self.assertRaises(urllib.error.HTTPError) as denied:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(denied.exception.code, 404)
        self.assertEqual(target.served, 0)

    def test_revoked_concrete_route_cannot_be_replaced_by_subclass_receipt_provider(self):
        _service, _ctx, _config, _lobby, route, _binding, _catalog, scope, _target = self._fixture()
        self.assertTrue(self.registry.revoke(scope))
        self.assertIsNone(self.registry.port_for(scope))

        class Subclass(ScopedAdapterRegistry):
            def __init__(self):
                self.calls = []

            def port_for(self, scope):
                self.calls.append(scope)
                return route.port

        registry = Subclass()
        self.assertIsNone(runtime_config.load_service(self.env, scoped_adapter_registry=registry))
        self.assertEqual(registry.calls, [])

    def test_direct_transport_preserves_exact_native_call_and_revocation_effect(self):
        _service, _ctx, _config, _lobby, route, _binding, _catalog, scope, target = self._fixture()
        request = self._direct_run(route, scope, "a2a-exact-native")
        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertEqual(response.status, 202)
        self.assertEqual(len(self.native.calls), 1)
        method, path, headers, body = self.native.calls[0]
        self.assertEqual((method, path), ("POST", "/v1/runs"))
        self.assertEqual(body, request.data)
        lower = {key.lower(): value for key, value in headers.items()}
        self.assertEqual(lower["idempotency-key"], "a2a-exact-native")
        self.assertEqual(lower["authorization"], request.get_header("Authorization"))
        self.assertEqual(target.served, 1)
        self.assertTrue(self.registry.revoke(scope))
        with self.assertRaises(urllib.error.HTTPError) as denied:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(denied.exception.code, 404)
        self.assertEqual(len(self.native.calls), 1)

    def test_original_route_reaches_exact_native_peer_registration_and_is_idempotent(self):
        service, _ctx, _config, lobby, route, binding, catalog, _scope, target = self._fixture()
        calls = []
        spec = {}

        class RecordingRpc:
            async def __call__(self, method, params):
                calls.append((method, copy.deepcopy(params)))
                if method == "groups.capabilities":
                    return {"driver": True, "authority_gateway_id": pairing_support.INSTALL_A}
                if method == "groups.create":
                    spec.update(params)
                    return {}
                if method == "groups.peer.register":
                    return {"registered": True, "target_install_id": pairing_support.INSTALL_B, "target_profile": "default"}
                if method == "groups.state":
                    return {"room": {**spec, "authority_gateway_id": pairing_support.INSTALL_A,
                                     "authority_epoch": 1, "disbanded_at": None},
                            "driver_status": {"peer_routes": [{"member_id": "peer-b", "room_id": spec["room_id"], "status": "ready"}]}}
                raise AssertionError(method)

        @contextlib.asynccontextmanager
        async def connect(_config):
            yield RecordingRpc()

        handler = _RequestHandler()
        handler.path = routes.PREFIX + "/lobbies/" + lobby["lobby_id"] + "/activate"
        with patch.object(routes, "_connect_native_rpc", connect):

            self.assertTrue(routes.handle_request(handler, "POST", handler.path, {}, lambda _h: True,
                                                  scoped_adapter_registry=self.registry))
        self.assertEqual(handler.responses[0][0], 200, handler.responses)
        self.assertEqual([method for method, _ in calls], ["groups.capabilities", "groups.create", "groups.state",
                                                           "groups.peer.register", "groups.state"])
        register = next(params for method, params in calls if method == "groups.peer.register")
        self.assertEqual(register["target_url"], route.url)
        self.assertEqual(register["member_id"], "peer-b")
        self.assertEqual(register["grant"], "fixture-grant-real")
        self.assertEqual(register["catalog"], catalog)
        request_body = json.dumps({"input": "run", "hosted_room_dispatch": {
            "room_id": lobby["planned_room_id"], "home_install_id": pairing_support.INSTALL_A,
            "authority_gateway_id": pairing_support.INSTALL_A, "authority_epoch": 1,
            "member_id": "peer-b", "target_install_id": pairing_support.INSTALL_B,
            "target_profile": "default", "protocol_version": 2, "task_id": "task-a2a",
            "execution_generation": 1, "source_event_seq": 1, "cancellation_scope_id": "cancel-a2a",
            "prompt": "run", "prompt_digest": "p" * 64, "capability_digest": "c" * 64,
            "execution_policy_digest": "d" * 64, "trace_id": "trace-a2a",
        }}).encode()
        direct = urllib.request.Request(route.url + "/v1/runs", data=request_body,
                                        headers={"Content-Type": "application/json",
                                                 "Authorization": "HermesRoom " + "x" * 32,
                                                 "Idempotency-Key": "a2a-direct-1"})
        with urllib.request.urlopen(direct, timeout=5) as response:
            self.assertEqual(response.status, 202)
        self.assertEqual(target.served, 1)
        before_effects = target.served
        self.assertTrue(self.registry.revoke(_scope))
        with self.assertRaises(urllib.error.HTTPError) as denied:
            urllib.request.urlopen(direct, timeout=5)
        self.assertEqual(denied.exception.code, 404)
        self.assertEqual(target.served, before_effects)
        before = len(calls)
        duplicate = _RequestHandler()
        duplicate.path = handler.path
        with patch.object(routes, "_connect_native_rpc", connect):
            routes.handle_request(duplicate, "POST", duplicate.path, {}, lambda _h: True,
                                   scoped_adapter_registry=self.registry)
        self.assertEqual(duplicate.responses[0][0], 200, duplicate.responses)
        self.assertEqual(len(calls), before)

    def test_missing_receipt_refuses_before_native_mutation(self):
        _service, _ctx, _config, lobby, _route, _binding, _catalog, _scope, _target = self._fixture(admit=False)
        native_calls = []

        @contextlib.asynccontextmanager
        async def connect(_config):
            class NoMutationRpc:
                async def __call__(self, method, _params):
                    native_calls.append(method)
                    if method == "groups.capabilities":
                        return {"driver": True, "authority_gateway_id": pairing_support.INSTALL_A}
                    raise AssertionError("native mutation must not be attempted")
            yield NoMutationRpc()

        handler = _RequestHandler()
        handler.path = routes.PREFIX + "/lobbies/" + lobby["lobby_id"] + "/activate"
        with patch.object(routes, "_connect_native_rpc", connect):
            routes.handle_request(handler, "POST", handler.path, {}, lambda _h: True,
                                  scoped_adapter_registry=self.registry)
        self.assertEqual(handler.responses[0][0], 503)
        self.assertIn(handler.responses[0][1]["code"], {"activation_failed", "creation_ambiguous"})
        self.assertEqual(native_calls, ["groups.capabilities"])

    def test_revoked_route_refuses_before_native_mutation(self):
        _service, _ctx, _config, lobby, _route, _binding, _catalog, scope, _target = self._fixture()
        self.assertTrue(self.registry.revoke(scope))
        native_calls = []

        @contextlib.asynccontextmanager
        async def connect(_config):
            class NoMutationRpc:
                async def __call__(self, method, _params):
                    native_calls.append(method)
                    if method == "groups.capabilities":
                        return {"driver": True, "authority_gateway_id": pairing_support.INSTALL_A}
                    raise AssertionError("native mutation must not be attempted")
            yield NoMutationRpc()

        handler = _RequestHandler()
        handler.path = routes.PREFIX + "/lobbies/" + lobby["lobby_id"] + "/activate"
        with patch.object(routes, "_connect_native_rpc", connect):
            routes.handle_request(handler, "POST", handler.path, {}, lambda _h: True,
                                  scoped_adapter_registry=self.registry)
        self.assertEqual(handler.responses[0][0], 503)
        self.assertIn(handler.responses[0][1]["code"], {"activation_failed", "creation_ambiguous"})
        self.assertEqual(native_calls, ["groups.capabilities"])


if __name__ == "__main__":
    unittest.main()
