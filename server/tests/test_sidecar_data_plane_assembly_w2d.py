"""W2d Card2-A2b3-1b: inert sidecar data-plane dependency assembly.

Real scratch owner config, identities, pairing journals, RoomInviteStore, JoiningStore and peer records.  Only the
external Core/WebSocket response boundary is ever stubbed, and assembly itself must never reach it.
"""
from __future__ import annotations

import ast
import asyncio
import contextlib
import dataclasses
import inspect
import json
import os
import shutil
import socket
import ssl  # noqa: F401  (imported BEFORE any socket instrumentation)
import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import MappingProxyType
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pairing_support as ps  # noqa: E402
from invite_rooms import pairing_journal as pj  # noqa: E402
from invite_rooms import runtime_config, runtime_joining as rj  # noqa: E402
from invite_rooms.admission_receipts import AdmissionReceiptStore  # noqa: E402
from invite_rooms.control.adapter import LoopbackAdapter  # noqa: E402
from invite_rooms.control.connector import HostConnector, LinkBinding, TargetConnector  # noqa: E402
from invite_rooms.control.dispatcher import ControlError  # noqa: E402
from invite_rooms.control.listener import ControlListener  # noqa: E402
from invite_rooms.control.scoped_registry import ScopedAdapterRegistry  # noqa: E402
from invite_rooms.joining_material import TargetMaterialMint  # noqa: E402
from invite_rooms.joining_store import JoiningStore  # noqa: E402
from invite_rooms.material_control import (HostConnectorMaterialTransport, MaterialControl,  # noqa: E402
                                           MaterialControlChannel)
from invite_rooms.material_receipts import MaterialReceiptStore  # noqa: E402
from invite_rooms.native_rpc import NativeRpcDenied  # noqa: E402
from invite_rooms.pairing_journal import Journal, JournalLinkAuthority, MaterialPeerContext  # noqa: E402
from invite_rooms.peer_registry import MaterialScope, PeerRegistry, adapter_id_for  # noqa: E402
from invite_rooms.target_material import TargetMaterialSource  # noqa: E402

MODULE_PATH = Path(__file__).resolve().parents[1] / "invite_rooms" / "sidecar_data_plane.py"
CONFIG_ENV = "MISSION_CONTROL_ROOM_INVITES_CONFIG"

# ---- network / thread instrumentation (installed once, only records while armed) ------------------------------
_ARMED = threading.Event()
_EVENTS: list = []


def _audit(event, args):
    if _ARMED.is_set() and event in {"socket.__new__", "socket.bind", "socket.connect", "socket.listen",
                                     "socket.getaddrinfo", "socket.gethostbyname"}:
        _EVENTS.append(event)


sys.addaudithook(_audit)


def sdp():
    from invite_rooms import sidecar_data_plane
    return sidecar_data_plane


def setUpModule():
    global TEMPLATES
    TEMPLATES = {i: ps.identity_template(i) for i in (ps.INSTALL_A, ps.INSTALL_B)}


def tearDownModule():
    for path in TEMPLATES.values():
        shutil.rmtree(path.parent, ignore_errors=True)


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def ep(port, host="127.0.0.1"):
    return {"host": host, "port": port}


def private_json(path: Path, value) -> None:
    ps.private_dir(path.parent)
    path.write_text(json.dumps(value))
    path.chmod(0o600)


class Base(unittest.TestCase):
    """Host A paired with joining B (both ``acknowledged``), each with a complete owner config."""

    def setUp(self):
        _EVENTS.clear()
        self.addCleanup(_ARMED.clear)
        self.tmp = Path(tempfile.mkdtemp(prefix="w2d-a2b3-1b-"))
        self.tmp.chmod(0o700)
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.clock = ps.Clock(time.time())
        self.A = ps.Side(self.tmp, "a", ps.INSTALL_A, template=TEMPLATES[ps.INSTALL_A])
        self.B = ps.Side(self.tmp, "b", ps.INSTALL_B, template=TEMPLATES[ps.INSTALL_B])
        self.a, self.b = self.A.service(self.clock), self.B.service(self.clock)
        self.paired = ps.drive_pairing(self.a, self.b)
        req = self.a.ack_request(self.paired["a_id"])
        self.a.complete_ack(self.paired["a_id"], self.b.handle_pair_ack(req.request))
        self.endpoint, self.generation = req.endpoint_id, req.generation
        self.link_a = self.a.journal.snapshot().links[self.endpoint]
        self.link_b = self.b.journal.snapshot().links[self.endpoint]
        self.peer_b = self.link_a["peer"]["peer_id"]      # how A knows B
        self.peer_a = self.link_b["peer"]["peer_id"]      # how B knows A
        self.binding = pj.binding_for(self.link_a)
        self.scope = MaterialScope(
            owner_installation_id=ps.INSTALL_A, owner_profile="default", lobby_id="lobby-1", planned_room_id="room-1",
            member_id="member-1", invite_id="invite-1", peer_id=self.peer_b, pairing_generation=self.generation,
            **self.binding)
        self.adapter_id = adapter_id_for(self.scope)
        runtime_config._warned_once.clear()
        self.addCleanup(runtime_config._warned_once.clear)
        self.planes = []
        self.addCleanup(self._close_planes)

    def _close_planes(self):
        for plane in self.planes:
            with contextlib.suppress(Exception):
                plane.close()

    # -- config -----------------------------------------------------------------------------------------
    def config_dict(self, side, remote_peer, *, omit=(), data_dest=None, adapter_id=None, joining_dir=None):
        adapter_id = adapter_id or self.adapter_id
        ports = {name: free_port() for name in ("control", "data", "target", "dest", "joining")}
        token = side.root / "cfg" / "native.token"
        token.write_text("tok")
        token.chmod(0o600)
        ps.private_dir(side.root / "invites")
        data = {
            "store_path": str(side.root / "invites" / "invites.sqlite"),
            "installation_id": side.installation_id,
            "profile": "default",
            "peer_registry_path": str(side.peers_path),
            "native_rpc": {"endpoint": "ws://127.0.0.1:1", "token_path": str(token)},
            "target_http": {"base_url": f"http://127.0.0.1:{free_port()}", "profile": "default"},
            "data_listen": ep(ports["data"]),
            "joining": {"store_dir": str(joining_dir or side.root / "joining"), "identity_dir": str(side.identity_dir),
                        "hosts_path": str(side.root / "cfg" / "hosts.json"),
                        "origins": {remote_peer: "https://remote.example"},
                        "control_endpoints": {remote_peer: ep(ports["joining"])}},
            "pairing": {"identity_dir": str(side.identity_dir), "journal_dir": str(side.journal_dir),
                        "peers_path": str(side.peers_path), "control_listen": ep(ports["control"]),
                        "target_listeners": {adapter_id: ep(ports["target"])},
                        "data_endpoints": {remote_peer: {adapter_id: data_dest or ep(ports["dest"])}}},
        }
        for dotted in omit:
            node = data
            *parents, leaf = dotted.split(".")
            for key in parents:
                node = node[key]
            del node[leaf]
        return data

    def load(self, side, remote_peer, **kw):
        path = side.root / "cfg" / "runtime.json"
        private_json(path, self.config_dict(side, remote_peer, **kw))
        runtime_config._warned_once.clear()
        loaded = runtime_config.load_service({CONFIG_ENV: str(path)})
        self.assertIsNotNone(loaded, "scratch owner config must be accepted by the canonical loader")
        return loaded

    def plane(self, side=None, remote_peer=None, **kw):
        side = side or self.A
        remote_peer = remote_peer or (self.peer_b if side is self.A else self.peer_a)
        plane = sdp().build_sidecar_data_plane(self.load(side, remote_peer, **kw))
        self.assertIsNotNone(plane, "complete owner config must assemble a data plane bundle")
        self.planes.append(plane)
        return plane

    # -- contexts ---------------------------------------------------------------------------------------
    def context(self, plane, *, side=None, role="host", operation="material_receipt", generation=None,
                readmit=lambda: True, deadline=None, **forged):
        side = side or self.A
        journal = side.service(self.clock).journal
        authority = JournalLinkAuthority(journal)
        generation = self.generation if generation is None else generation
        trust = authority.peer_trust(self.endpoint, generation, role=role, states=("acknowledged",))
        link = journal.snapshot().links[self.endpoint]
        import base64
        key = base64.b64decode(link["peer"]["consent_public_key_b64"], validate=True)
        fields = dict(
            endpoint_id=self.endpoint, generation=generation, role=role, protocol_state="acknowledged",
            peer_id=trust.peer_id, binding=trust.binding, ca_pem=trust.ca_pem, fingerprint=trust.fingerprint,
            consent_public_key=key, operation=operation, deadline=deadline or time.monotonic() + 5)
        fields.update(forged)
        return MaterialPeerContext._issue(readmit, **fields)

    def seed_ready_scope(self, store):
        roster = [{"member_id": "local-a", "profile": "default", "handle": "local"},
                  {"member_id": "peer-b", "profile": "default", "handle": "peerb",
                   "target": {"kind": "peer", "peer_id": self.peer_b, "installation_id": ps.INSTALL_B,
                              "profile": "default"}}]
        lobby = store.create_lobby(name="Known owners", roster=roster)
        issued = store.issue_invite(lobby["lobby_id"], "peer-b", invitee_label="b", ttl_seconds=3600)
        store.redeem(lobby["lobby_id"], issued.invite_id, issued.secret, binding=self.binding,
                     peer_id=self.peer_b, before_effect=lambda: True)
        store.decide_host(lobby["lobby_id"], issued.invite_id, binding=self.binding, approved=True)
        store.consent_joining(lobby["lobby_id"], issued.invite_id, binding=self.binding, consented=True,
                              peer_id=self.peer_b, before_effect=lambda: True)
        return lobby, issued


class AssemblyBuilderTests(Base):
    def test_card3_bundle_owns_inert_scope_manager_and_private_ack_store(self):
        plane = self.plane()
        self.assertTrue(hasattr(plane, "scope_lifecycle"), "bundle must own the scope manager")
        self.assertTrue(hasattr(plane, "host_ack_store"), "bundle must own host run bindings")
        from invite_rooms.scope_lifecycle import ScopeLifecycle
        from invite_rooms.run_bindings import HostAckBindingStore
        self.assertIs(type(plane.scope_lifecycle), ScopeLifecycle)
        self.assertIs(type(plane.host_ack_store), HostAckBindingStore)
        self.assertEqual(plane.scope_lifecycle.snapshot(), ())
        self.assertEqual(plane.host_ack_store.path.parent, plane.config.store_path.parent / "host-ack-bindings")
        with self.assertRaises(Exception):
            plane.scope_lifecycle.ensure_target(endpoint_id=self.endpoint, generation=self.generation,
                lobby_id="lobby-1", invite_id="invite-1", deadline=time.monotonic()+2)

    def test_builder_returns_none_for_feature_off_or_invalid_owner_config(self):
        module = sdp()
        # Positive control: the very same fixture assembles when complete.
        self.plane()
        # Feature off.
        self.assertIsNone(module.build_sidecar_data_plane(None))
        self.assertIsNone(module.build_from_env({}))
        self.assertIsNone(module.build_from_env({CONFIG_ENV: ""}))
        # Misconfigured file (world readable) is the same sanitized feature-off.
        path = self.A.root / "cfg" / "runtime.json"
        private_json(path, self.config_dict(self.A, self.peer_b))
        path.chmod(0o644)
        self.assertIsNone(module.build_from_env({CONFIG_ENV: str(path)}))
        # Wrong shapes of the loader result.
        loaded = self.load(self.A, self.peer_b)
        for bad in (object(), (), loaded[:2], loaded + (None,), (loaded[2], loaded[1], loaded[0]),
                    (loaded[0], loaded[1], object()), loaded[2], "x", 7):
            self.assertIsNone(module.build_sidecar_data_plane(bad), repr(type(bad)))
        # Each required piece missing => the WHOLE bundle is absent (never partial), and nothing is created.
        for omit in ("data_listen", "pairing.control_listen", "pairing.target_listeners", "pairing.data_endpoints",
                     "target_http", "native_rpc", "joining"):
            with self.subTest(omit=omit):
                shutil.rmtree(self.A.root / "invites", ignore_errors=True)
                shutil.rmtree(self.A.root / "joining", ignore_errors=True)
                incomplete = self.load(self.A, self.peer_b, omit=(omit,))
                self.assertIsNone(module.build_sidecar_data_plane(incomplete))
                self.assertFalse((self.A.root / "invites" / "admission-receipts").exists())
        # A config that aliases a data destination with the same peer's control destination fails closed too.
        loaded = self.load(self.A, self.peer_b)
        service, ctx, config = loaded
        pairing = config.pairing
        control = pairing.data_endpoints[self.peer_b][self.adapter_id]
        aliased = dataclasses.replace(config, pairing=dataclasses.replace(
            pairing, control_endpoints=MappingProxyType({self.peer_b: control})))
        self.assertIsNone(module.build_sidecar_data_plane((service, ctx, aliased)))
        same_bind = dataclasses.replace(config, data_listen=pairing.target_listeners[self.adapter_id])
        self.assertIsNone(module.build_sidecar_data_plane((service, ctx, same_bind)))

    def test_builder_constructs_concrete_unstarted_loopback_registry_and_control_listener(self):
        plane = self.plane()
        config = plane.config
        self.assertIs(type(plane.adapter), LoopbackAdapter)
        self.assertEqual(plane.adapter_bind, config.data_listen)
        self.assertFalse(plane.adapter.is_running())
        self.assertIsNone(plane.adapter._server)
        self.assertIs(type(plane.scoped_registry), ScopedAdapterRegistry)
        self.assertIs(plane.scoped_registry.adapter, plane.adapter)
        self.assertIs(plane.scoped_registry._authority, plane.authority)
        self.assertIs(type(plane.authority), JournalLinkAuthority)
        self.assertEqual(plane.adapter.authority, plane.authority.current)
        listener = plane.control_listener
        self.assertIs(type(listener), ControlListener)
        self.assertEqual(listener.address, config.pairing.control_listen)
        self.assertFalse(listener._started)
        self.assertFalse(listener._closed)
        self.assertIsNone(listener._listener)
        self.assertIsNone(listener._accept_thread)
        self.assertIsNone(listener._loop_thread)
        self.assertIs(type(listener.authority), JournalLinkAuthority)
        self.assertEqual(listener.authority.journal.directory, plane.authority.journal.directory)
        self.assertIsNot(listener.authority, plane.authority)
        self.assertIsNot(listener.authority.journal, plane.authority.journal)
        # The service/store/registry stay the caller-owned canonical loader objects.
        self.assertIs(plane.registry, plane.owner_service.registry)
        self.assertIs(plane.store, plane.owner_service.store)
        self.assertIsNone(plane.registry._route_provider)
        # Binds are still free: nothing was bound by assembly.
        for host, port in (config.data_listen, config.pairing.control_listen):
            probe = socket.socket()
            self.addCleanup(probe.close)
            probe.bind((host, port))
        # Single owner of the resources it opened: close is idempotent.
        plane.close()
        plane.close()

    def test_disabled_history_does_not_load_owner_identity_or_create_consent_store(self):
        with mock.patch.object(sdp().identity_module, 'load_identity', side_effect=AssertionError('history identity must stay lazy')) as load_identity:
            plane = self.plane()
        load_identity.assert_not_called()
        self.assertNotIn('room_log', plane.control_listener._host_handlers)
        self.assertFalse((self.A.root / 'invites' / 'history-consent.sqlite').exists())

    def test_history_handlers_require_explicit_runtime_gate(self):
        from dataclasses import replace
        service, owner, config = self.load(self.A, self.peer_b)
        plane = sdp().build_sidecar_data_plane((service, owner, replace(config, history_enabled=True)))
        self.assertIsNotNone(plane)
        self.planes.append(plane)
        self.assertEqual(set(plane.control_listener._host_handlers), {
            'preview', 'redeem', 'status', 'joining_consent',
            'history_read_scope', 'history_read_acceptance', 'room_log'})

    def test_control_listener_has_only_frozen_pair_ack_host_and_material_handler_maps(self):
        plane = self.plane()
        listener = plane.control_listener
        self.assertEqual(set(listener._host_handlers), {"preview", "redeem", "status", "joining_consent"})
        self.assertEqual(set(listener._material_handlers), {"material_request", "material_receipt"})
        self.assertEqual(dict(listener._host_handlers), dict(plane.host_handlers))
        self.assertEqual(dict(listener._material_handlers), dict(plane.material_handlers))
        for name, handler in listener._host_handlers.items():
            expected_file = "history_host.py" if name.startswith("history_") or name == "room_log" else "host_control.py"
            self.assertTrue(handler.__code__.co_filename.endswith(expected_file), name)
        control = plane.material_control
        self.assertIs(type(control), MaterialControl)
        request = listener._material_handlers["material_request"]
        receipt = listener._material_handlers["material_receipt"]
        # B1 owns ingress work in the coordinator, rather than exposing raw bound
        # methods that would lose cancellation/drain ownership.
        for handler, method in ((request, MaterialControl.target_handler),
                                (receipt, MaterialControl.receipt_handler)):
            closure = inspect.getclosurevars(handler).nonlocals
            self.assertIs(closure['self'], plane.material_coordinator)
            self.assertIs(closure['callback'].__self__, control)
            self.assertIs(closure['callback'].__func__, method)
            with self.assertRaises(ControlError):
                asyncio.run(handler(object(), {}))
        # No optional seam is left None and the durable origin journal is the bundle's own.
        for seam in ("_target_resolver", "_receipt_resolver", "_scope_status_resolver"):
            self.assertTrue(callable(getattr(control, seam)), seam)
        self.assertIs(control._journal, plane.journal)
        self.assertTrue(control._request_bound)
        self.assertTrue(callable(listener._pair_ack))
        self.assertEqual(listener._pair_ack.__module__, "invite_rooms.control.listener")
        for out_of_scope in ("member_left",):
            self.assertNotIn(out_of_scope, listener._host_handlers)
            self.assertNotIn(out_of_scope, listener._material_handlers)
        # Host handlers are the real contextual ones: a non-context caller is refused generically.
        for op, handler in listener._host_handlers.items():
            with self.assertRaises(ControlError, msg=op):
                result = handler(object(), {"op": op, "room_id": "x", "since_seq": 0, "limit": 1})
                if inspect.isawaitable(result):
                    asyncio.run(result)

    def test_data_endpoint_resolution_uses_journal_peer_and_adapter_id_only(self):
        plane = self.plane()
        expected = plane.config.pairing.data_endpoints[self.peer_b][self.adapter_id]
        trust = plane.authority.peer_trust(self.endpoint, self.generation, role="host", states=("acknowledged",))
        self.assertEqual(plane.data_endpoint_resolver(trust, self.adapter_id), expected)
        self.assertEqual(plane.data_endpoint_for_scope(self.scope), expected)
        error = sdp().DataPlaneError
        # Missing / foreign scope id.
        for bad_id in ("link-" + "9" * 32, "", None, self.adapter_id.upper(), self.peer_b):
            with self.assertRaises(error, msg=repr(bad_id)):
                plane.data_endpoint_resolver(trust, bad_id)
        # Forged / stale trust: only an exact FRESH journal read is accepted.
        for forged in (dataclasses.replace(trust, generation=trust.generation + 1),
                       dataclasses.replace(trust, peer_id="peer-" + "f" * 32),
                       dataclasses.replace(trust, fingerprint="0" * 64),
                       dataclasses.replace(trust, protocol_state="confirmed"),
                       {"peer_id": self.peer_b}, self.peer_b, None):
            with self.assertRaises(error, msg=repr(forged)):
                plane.data_endpoint_resolver(forged, self.adapter_id)
        # A scope whose peer id differs from the journal peer is refused.
        other = dataclasses.replace(self.scope, peer_id="peer-" + "e" * 32)
        with self.assertRaises(error):
            plane.data_endpoint_for_scope(other)
        with self.assertRaises(error):
            plane.data_endpoint_for_scope(object())
        # The registry projection file is never membership/endpoint authority.
        self.A.peers_path.write_text("garbage-not-json")
        self.assertEqual(plane.data_endpoint_resolver(trust, self.adapter_id), expected)
        # Unknown peer in the owner map: no endpoint, even though the journal peer is paired.
        service, ctx, config = plane.owner_service, plane.owner, plane.config
        other_map = MappingProxyType({"peer-" + "d" * 32: config.pairing.data_endpoints[self.peer_b]})
        stranger = sdp().build_sidecar_data_plane(
            (service, ctx, dataclasses.replace(config, pairing=dataclasses.replace(
                config.pairing, data_endpoints=other_map))))
        self.assertIsNotNone(stranger)
        self.planes.append(stranger)
        s_trust = stranger.authority.peer_trust(self.endpoint, self.generation, role="host", states=("acknowledged",))
        with self.assertRaises(error):
            stranger.data_endpoint_resolver(s_trust, self.adapter_id)
        # Durable unpair cuts the endpoint off at once.
        self.a.unpair(self.peer_b)
        with self.assertRaises(error):
            plane.data_endpoint_resolver(trust, self.adapter_id)
        with self.assertRaises(error):
            plane.data_endpoint_for_scope(self.scope)

    def test_target_dispatch_backend_factory_uses_real_target_source_and_separate_admission_store(self):
        plane = self.plane()
        config = plane.config
        closure = inspect.getclosurevars(plane.target_dispatch_backend_factory).nonlocals
        self.assertIs(closure["joining_store"], plane.joining_store)
        self.assertIs(type(closure["joining_store"]), JoiningStore)
        self.assertIs(closure["authority"], plane.authority)
        self.assertEqual(closure["target_http"], config.target_http)
        self.assertIs(closure["admission_store"], plane.admission_store)
        self.assertIs(type(plane.admission_store), AdmissionReceiptStore)
        self.assertNotIsInstance(plane.admission_store, (MaterialReceiptStore, JoiningStore))
        self.assertEqual(plane.admission_store.root, config.store_path.parent / "admission-receipts")
        roots = {plane.admission_store.root, config.store_path.parent / "material-receipts",
                 Path(config.joining_store_dir), Path(config.pairing.journal_dir)}
        self.assertEqual(len(roots), 4)
        self.assertEqual(stat.S_IMODE(os.stat(plane.admission_store.root).st_mode), 0o700)
        self.assertEqual(Path(plane.joining_store.root), Path(config.joining_store_dir))
        # identity_reader / catalog_reader are the owner's own sources (identity dir; native catalog lazily).
        bundle = closure["identity_reader"](deadline=time.monotonic() + 3)
        self.assertEqual((bundle.installation_id, bundle.profile), (ps.INSTALL_A, "default"))
        catalog = ps.make_catalog(ps.INSTALL_A)

        class FakeRpc:
            async def __call__(self, method, params):
                assert (method, params) == ("groups.capabilities", {"profile": config.profile})
                return {"room_link": {"enabled": True, "catalog": catalog}}

        @contextlib.asynccontextmanager
        async def fake_connect(rpc_config):
            assert rpc_config["endpoint"] == config.native_rpc["endpoint"]
            yield FakeRpc()

        with mock.patch("invite_rooms.routes._connect_native_rpc", fake_connect):
            self.assertEqual(closure["catalog_reader"](deadline=time.monotonic() + 3), catalog)
            for bad in (time.monotonic() - 1, float("nan"), None):
                with self.assertRaises(Exception, msg=repr(bad)):
                    closure["catalog_reader"](deadline=bad)
        # Instantiating the factory for a real binding builds the REAL source/client, still inert.
        binding = LinkBinding(self.endpoint, self.generation, ("127.0.0.1", 1), None, self.binding["fingerprint"],
                              dict(self.binding), peer_ca_data="CA")
        expected_scope = {"room_id": "room-1", "home_install_id": ps.INSTALL_A, "authority_gateway_id": "gw",
                          "authority_epoch": 1, "member_id": "member-1", "target_install_id": ps.INSTALL_B,
                          "target_profile": "default"}
        _ARMED.set()
        try:
            native = plane.target_dispatch_backend_factory(binding, expected_scope)
        finally:
            _ARMED.clear()
        self.assertEqual(_EVENTS, [])
        self.assertTrue(callable(native))
        cells = dict(zip(native.__code__.co_freevars, (c.cell_contents for c in native.__closure__)))
        self.assertIs(type(cells["source"]), TargetMaterialSource)
        self.assertEqual((cells["source"]._endpoint_id, cells["source"]._generation), (self.endpoint, self.generation))
        self.assertIs(cells["source"]._store, plane.joining_store)
        self.assertIs(cells["source"]._authority, plane.authority)
        self.assertIs(cells["joining_store"], plane.joining_store)
        self.assertIs(cells["admission_store"], plane.admission_store)

    def test_material_resolvers_are_request_bound_and_not_module_global(self):
        plane = self.plane()
        control = plane.material_control
        # -- host receipt/scope-status resolvers over the real owner store + receipt registry
        lobby, issued = self.seed_ready_scope(plane.store)
        context = self.context(plane, role="host", operation="material_receipt")
        request = {"lobby_id": lobby["lobby_id"], "invite_id": issued.invite_id}

        async def resolve(context=context, request=request, deadline=None):
            return await control._receipt_resolver(context=context, request=request,
                                                    deadline=deadline or time.monotonic() + 5)

        registry, scope = asyncio.run(resolve())
        self.assertIsNot(registry, plane.registry)
        self.assertIs(type(registry), PeerRegistry)
        self.addCleanup(control._receipt_registry_release, registry)
        self.assertIs(registry._authority, plane.authority)
        self.assertEqual(registry._source.path, plane.config.peer_registry_path)
        self.assertEqual(registry._route_provider, plane.scoped_registry.lookup)
        self.assertIsNot(registry._receipt_store, plane.registry._receipt_store)
        again, again_scope = asyncio.run(resolve())
        self.assertIsNot(again, registry)
        self.assertEqual(again_scope, scope)
        self.assertIsNot(again._receipt_store, registry._receipt_store)
        receipt_fd = again._receipt_store._fd
        control._receipt_registry_release(again)
        self.assertIsNone(again._receipt_store._fd)
        with self.assertRaises(OSError):
            os.fstat(receipt_fd)
        self.assertIsNotNone(registry._receipt_store._fd)
        self.assertEqual(scope, MaterialScope(
            owner_installation_id=ps.INSTALL_A, owner_profile="default", lobby_id=lobby["lobby_id"],
            planned_room_id=lobby["planned_room_id"], member_id="peer-b", invite_id=issued.invite_id,
            peer_id=self.peer_b, pairing_generation=self.generation, **self.binding))
        status = asyncio.run(control._scope_status_resolver(
            context=context, request=request, scope=scope, deadline=time.monotonic() + 5))
        self.assertEqual(status, {"status": "ready", "scope": {
            "host_id": ps.INSTALL_A, "room_id": lobby["planned_room_id"], "member_id": "peer-b",
            "lobby_id": lobby["lobby_id"], "invite_id": issued.invite_id, "binding": self.binding}})
        # Identity comes from the journal-issued context, never from the request: foreign ids are refused.
        for bad in ({"lobby_id": lobby["lobby_id"], "invite_id": "invite-other"},
                    {"lobby_id": "lobby-other", "invite_id": issued.invite_id}):
            with self.assertRaises(Exception):
                asyncio.run(resolve(request=bad))
        # Wrong operation / wrong role / expired deadline / unusable readmit are refused.
        with self.assertRaises(Exception):
            asyncio.run(resolve(context=self.context(plane, role="host", operation="material_request")))
        with self.assertRaises(Exception):
            asyncio.run(resolve(context=self.context(plane, role="joining", operation="material_receipt")))
        with self.assertRaises(Exception):
            asyncio.run(resolve(deadline=time.monotonic() - 1))
        with self.assertRaises(Exception):
            asyncio.run(resolve(context=self.context(plane, readmit=lambda: False)))
        with self.assertRaises(Exception):
            asyncio.run(resolve(context=object()))
        # A context whose journal-derived fields disagree with a FRESH journal read is refused.
        for forged in ({"peer_id": "peer-" + "f" * 32}, {"fingerprint": "0" * 64},
                       {"binding": tuple(sorted({**self.binding, "policy_digest": "1" * 64}.items()))}):
            with self.assertRaises(ControlError, msg=repr(forged)):
                asyncio.run(resolve(context=self.context(plane, **forged)))
            with self.assertRaises(ControlError, msg=repr(forged)):
                asyncio.run(control._scope_status_resolver(
                    context=self.context(plane, **forged), request=request, scope=scope,
                    deadline=time.monotonic() + 5))
        # Request-bound: a durable unpair after the context was issued is observed by the very next resolve.
        self.a.unpair(self.peer_b)
        with self.assertRaises(ControlError):
            asyncio.run(resolve())
        # -- target resolver (joining side) builds a real, lazy TargetMaterialMint per request
        plane_b = self.plane(self.B)
        control_b = plane_b.material_control
        self.assertIsNot(control_b, control)
        self.assertIsNot(control_b._target_resolver, control._target_resolver)
        b_context = self.context(plane_b, side=self.B, role="joining", operation="material_request")
        b_request = {"op": "material_request", "lobby_id": "lobby-1", "invite_id": "invite-1"}
        loop = asyncio.new_event_loop()      # its self-pipe socketpair is created OUTSIDE the instrumented window
        self.addCleanup(loop.close)
        _ARMED.set()
        try:
            mint = loop.run_until_complete(control_b._target_resolver(
                context=b_context, request=b_request, deadline=time.monotonic() + 5))
        finally:
            _ARMED.clear()
        self.assertEqual(_EVENTS, [])
        self.assertIs(type(mint), TargetMaterialMint)
        self.assertIs(mint._store, plane_b.joining_store)
        self.assertEqual(mint._host_peer_id, f"{self.endpoint}:{self.generation}")
        channel = mint._channel
        self.assertIs(type(channel), MaterialControlChannel)
        self.assertIs(type(channel._connector), HostConnectorMaterialTransport)
        connector = channel._connector._connector
        self.assertIs(type(connector), HostConnector)
        self.assertEqual((connector.binding.endpoint_id, connector.binding.generation),
                         (self.endpoint, self.generation))
        self.assertEqual(connector.binding.address,
                         tuple(plane_b.config.joining_owner["control_endpoints"][self.peer_a].values()))
        self.assertEqual((channel._lobby_id, channel._invite_id), ("lobby-1", "invite-1"))
        self.assertEqual(mint._consent_key.public_bytes, self.B_consent_public())
        # The native RPC wrapper preserves bind_request(invite_admission, deadline) and never connects by itself.
        rpc = mint._rpc
        bound = rpc.bind_request(invite_admission=lambda: True, deadline=time.monotonic() + 3)
        self.assertTrue(callable(bound))
        self.assertIs(getattr(bound, "_request_bound_native_rpc", False), True)
        for bad_admission, bad_deadline in ((None, time.monotonic() + 3), (lambda: True, None),
                                            (lambda: True, float("nan")), (lambda: True, "soon")):
            with self.assertRaises(NativeRpcDenied):
                rpc.bind_request(invite_admission=bad_admission, deadline=bad_deadline)
        with self.assertRaises(NativeRpcDenied):
            asyncio.run(rpc("groups.peer.invite", {}))      # no unbound/lab escape hatch
        # Target resolver refuses the wrong role / operation / stale generation.
        for bad in (self.context(plane_b, side=self.B, role="joining", operation="material_receipt"),
                    self.context(plane_b, side=self.B, role="joining", operation="material_request",
                                 peer_id="peer-" + "f" * 32),
                    object()):
            with self.assertRaises(ControlError):
                asyncio.run(control_b._target_resolver(context=bad, request=b_request,
                                                       deadline=time.monotonic() + 5))
        # No module-global resource owner / cache in the assembly module.
        module = sdp()
        owners = (Journal, JournalLinkAuthority, JoiningStore, AdmissionReceiptStore, LoopbackAdapter,
                  ScopedAdapterRegistry, MaterialControl, ControlListener, PeerRegistry, dict, list, set)
        for name, value in vars(module).items():
            if name.startswith("__"):
                continue
            self.assertNotIsInstance(value, owners, name)
        tree = ast.parse(MODULE_PATH.read_text())
        self.assertFalse([n for n in ast.walk(tree) if isinstance(n, (ast.Global, ast.Nonlocal))
                          and isinstance(n, ast.Global)])
        for node in tree.body:
            self.assertIsInstance(node, (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.AsyncFunctionDef,
                                         ast.ClassDef, ast.Assign, ast.AnnAssign, ast.Expr), ast.dump(node)[:80])
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                value = node.value
                self.assertTrue(isinstance(value, (ast.Constant, ast.Tuple, ast.Name, ast.Attribute))
                                or (isinstance(value, ast.Call) and getattr(value.func, "id", getattr(
                                    value.func, "attr", "")) in {"frozenset", "getLogger", "compile"}),
                                ast.dump(node)[:120])

    def B_consent_public(self):
        from invite_rooms import identity as identity_module
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
        bundle = identity_module.load_identity(self.B.identity_dir, installation_id=ps.INSTALL_B, profile="default")
        return bundle.consent_key().public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)

    def test_bundle_construction_opens_no_socket_starts_no_thread_and_registers_no_route(self):
        loaded = self.load(self.A, self.peer_b)
        service = loaded[0]
        route_provider_before = service.registry._route_provider
        calls = []

        def recorder(label):
            def record(*args, **kwargs):
                calls.append(label)
            return record

        targets = [(LoopbackAdapter, "start"), (LoopbackAdapter, "register"), (LoopbackAdapter, "register_link"),
                   (ControlListener, "start"), (TargetConnector, "start"), (ScopedAdapterRegistry, "register"),
                   (PeerRegistry, "register")]
        import aiohttp
        patches = [mock.patch.object(cls, name, recorder(f"{cls.__name__}.{name}")) for cls, name in targets]
        patches.append(mock.patch.object(aiohttp.ClientSession, "ws_connect", recorder("ws_connect")))
        patches.append(mock.patch("invite_rooms.routes._connect_native_rpc", recorder("_connect_native_rpc")))
        thread_starts = []
        original_start = threading.Thread.start

        def counting_start(thread, *args, **kwargs):
            thread_starts.append(thread.name)
            return original_start(thread, *args, **kwargs)

        patches.append(mock.patch.object(threading.Thread, "start", counting_start))
        threads_before = {t.ident for t in threading.enumerate()}
        with contextlib.ExitStack() as stack:
            for patcher in patches:
                stack.enter_context(patcher)
            _EVENTS.clear()
            _ARMED.set()
            try:
                plane = sdp().build_sidecar_data_plane(loaded)
            finally:
                _ARMED.clear()
        self.assertIsNotNone(plane)
        self.planes.append(plane)
        self.assertEqual(_EVENTS, [], "assembly opened a socket / resolved a name")
        self.assertEqual(thread_starts, [], "assembly started a thread")
        self.assertEqual(calls, [], "assembly started/registered something")
        self.assertEqual({t.ident for t in threading.enumerate()} - threads_before, set())
        self.assertIs(service.registry._route_provider, route_provider_before)
        self.assertIsNone(plane.registry._route_provider)
        self.assertFalse(plane.adapter.is_running())
        self.assertEqual(plane.scoped_registry._entries, {})
        self.assertFalse(plane.control_listener._started)

    def test_distinct_journal_authority_instances_observe_the_same_generation_cutover(self):
        plane = self.plane()
        loaded_registry_authority = plane.registry._authority
        authorities = {"bundle": plane.authority, "listener": plane.control_listener.authority,
                       "registry": loaded_registry_authority,
                       "fresh": JournalLinkAuthority(Journal(plane.config.pairing.journal_dir,
                                                             anchor=plane.config.pairing.journal_anchor))}
        for first in authorities.values():
            self.assertIs(type(first), JournalLinkAuthority)
        values = list(authorities.values())
        for i, left in enumerate(values):
            for right in values[i + 1:]:
                self.assertIsNot(left, right)
                self.assertIsNot(left.journal, right.journal)

        def observe():
            return {name: (a.peer_trust(self.endpoint, self.generation, role="host", states=("acknowledged",)),
                           a.current(self.endpoint), a.admit(self.endpoint, self.generation, "data_native"),
                           a.active_trusts(), a.journal.snapshot().peer_gen.get(self.peer_b, 0))
                    for name, a in authorities.items()}

        before = observe()
        self.assertEqual(len({repr(v) for v in before.values()}), 1)
        trust, current, admitted, active, peer_gen = before["bundle"]
        self.assertIsNotNone(trust)
        self.assertEqual(current.generation, self.generation)
        self.assertIs(admitted, True)
        self.assertEqual(len(active), 1)
        # A durable cutover written by a SEPARATE writer instance (the pairing service).
        result = self.a.unpair(self.peer_b)
        self.assertEqual(result["generation"], peer_gen + 1)
        after = observe()
        self.assertEqual(len({repr(v) for v in after.values()}), 1, after)
        for name, (trust, current, admitted, active, gen) in after.items():
            self.assertIsNone(trust, name)
            self.assertIs(admitted, False, name)
            self.assertEqual(active, (), name)
            self.assertEqual(gen, result["generation"], name)
            self.assertTrue(current is None or current.generation != self.generation, name)
        # Fail closed downstream: the owner endpoint resolver and the adapter authority both refuse the old link.
        with self.assertRaises(sdp().DataPlaneError):
            plane.data_endpoint_for_scope(self.scope)
        state = plane.adapter.authority(self.endpoint)
        self.assertTrue(state is None or state.generation != self.generation)


class ConnectorFactoryTests(Base):
    """The per-scope TargetConnector factory: never from request data, never started, fail closed."""

    @contextlib.contextmanager
    def managed_ready_target(self):
        import test_target_material_source_w2d as tms
        with tms.genuine_source_fixture() as fx:
            side, scope = fx["flow"].b_side, fx["scope"]
            remote = side.service(self.clock).journal.snapshot().links[scope.endpoint_id]["peer"]["peer_id"]
            plane = self.plane(side, remote, adapter_id=adapter_id_for(scope),
                               joining_dir=fx["fx"]["joining_store"].root)
            assert plane is not None

            @contextlib.asynccontextmanager
            async def native_catalog(_config):
                class Rpc:
                    async def __call__(_self, method, params):
                        self.assertEqual((method, params), ("groups.capabilities", {"profile": plane.config.profile}))
                        return {"room_link": {"enabled": True, "catalog": side.catalog}}
                yield Rpc()

            # All native effects, including stop/revoke, are forbidden in reconciliation.
            # Catalog is the only permitted native read; no daemon/HTTP endpoint is used.
            with mock.patch("invite_rooms.routes._connect_native_rpc", native_catalog), \
                    mock.patch("invite_rooms.target_dispatch_backend.native_http_backend") as http_factory, \
                    mock.patch("invite_rooms.target_http.TargetHttpClient.admit_run") as admit_run:
                http_factory.return_value.side_effect = AssertionError("unexpected native effect")
                admit_run.side_effect = AssertionError("unexpected native admission")
                try:
                    plane.start()
                    args = dict(endpoint_id=scope.endpoint_id, generation=scope.pairing_generation,
                                lobby_id=scope.lobby_id, invite_id=scope.invite_id)
                    prepared = plane.scope_lifecycle.ensure_target(**args, deadline=time.monotonic()+10)
                    self.assertTrue(prepared.connector._thread.is_alive())
                    native_calls = list(fx["producer"]["native_calls"])
                    yield plane, prepared, args, fx
                    self.assertEqual(fx["producer"]["native_calls"], native_calls)
                    http_factory.return_value.assert_not_called()
                    admit_run.assert_not_called()
                finally:
                    # Release while genuine fixture stores/identity still exist, even on RED.
                    plane.close()

    def test_card3_final_admission_contention_preserves_same_target_and_retry(self):
        with self.managed_ready_target() as (plane, prepared, args, fx):
            scope = prepared.scope
            listener, thread = prepared.connector._listener, prepared.connector._thread
            owned = plane.scope_lifecycle.snapshot()
            journal_file = plane.journal.directory / pj.JOURNAL_FILE
            journal_bytes = journal_file.read_bytes()
            journal_state = vars(plane.journal.snapshot())
            material = plane.joining_store.mint_load(fx["scope_key"])
            trust = plane.authority.peer_trust_strict(scope.endpoint_id, scope.pairing_generation,
                                                    role="joining", states=("acknowledged",))
            original_admit = plane.authority.admit
            calls = []

            def contend_at_final_gate(endpoint, generation, operation):
                self.assertEqual((endpoint, generation, operation),
                                 (scope.endpoint_id, scope.pairing_generation, "data_native"))
                # Earlier strict/source reads have completed. Only the final ORIGINAL
                # bool admit runs under a real exclusive disposable journal lock.
                with plane.journal.transaction():
                    answer = original_admit(endpoint, generation, operation)
                    calls.append(answer)
                    self.assertIs(answer, False)
                return answer

            with mock.patch.object(plane.authority, "admit", side_effect=contend_at_final_gate), \
                    mock.patch.object(prepared.connector, "close", wraps=prepared.connector.close) as close:
                with self.assertRaisesRegex(sdp().DataPlaneError, "^data_plane_unavailable$"):
                    plane.scope_lifecycle.reconcile(deadline=time.monotonic()+10)
                close.assert_not_called()
            self.assertEqual(calls, [False])
            self.assertEqual(plane.scope_lifecycle.snapshot(), owned)
            self.assertIs(prepared.connector._listener, listener)
            self.assertIs(prepared.connector._thread, thread)
            self.assertTrue(thread.is_alive())
            self.assertEqual(prepared.connector.address, prepared.bind)
            self.assertEqual(journal_file.read_bytes(), journal_bytes)
            self.assertEqual(vars(plane.journal.snapshot()), journal_state)
            self.assertEqual(plane.authority.peer_trust_strict(scope.endpoint_id, scope.pairing_generation,
                role="joining", states=("acknowledged",)), trust)
            self.assertIs(original_admit(scope.endpoint_id, scope.pairing_generation, "data_native"), True)
            plane.scope_lifecycle.reconcile(deadline=time.monotonic()+10)
            self.assertIs(plane.scope_lifecycle.ensure_target(**args, deadline=time.monotonic()+10), prepared)
            self.assertEqual(plane.scope_lifecycle.snapshot(), owned)
            self.assertIs(prepared.connector._listener, listener)
            self.assertIs(prepared.connector._thread, thread)
            self.assertTrue(thread.is_alive())
            self.assertEqual(plane.joining_store.mint_load(fx["scope_key"]), material)

    def test_card3_durable_unpair_retires_transport_without_native_effects(self):
        with self.managed_ready_target() as (plane, prepared, args, fx):
            scope = prepared.scope
            listener, thread = prepared.connector._listener, prepared.connector._thread
            material = plane.joining_store.mint_load(fx["scope_key"])
            side = fx["flow"].b_side
            writer = side.service(self.clock)
            peer = writer.journal.snapshot().links[scope.endpoint_id]["peer"]["peer_id"]
            writer.unpair(peer)
            self.assertIsNone(plane.authority.peer_trust_strict(scope.endpoint_id, scope.pairing_generation,
                role="joining", states=("acknowledged",)))
            self.assertIs(plane.authority.admit(scope.endpoint_id, scope.pairing_generation, "data_native"), False)
            with mock.patch.object(prepared.connector, "close", wraps=prepared.connector.close) as close:
                plane.scope_lifecycle.reconcile(deadline=time.monotonic()+5)
                close.assert_called_once_with()
            self.assertEqual(plane.scope_lifecycle.snapshot(), ())
            self.assertIsNone(prepared.connector._listener)
            self.assertIsNone(prepared.connector._thread)
            self.assertFalse(thread.is_alive())
            self.assertEqual(listener.fileno(), -1)
            self.assertEqual(plane.joining_store.mint_load(fx["scope_key"]), material)
            with self.assertRaises(sdp().DataPlaneError):
                plane.scope_lifecycle.ensure_target(**args, deadline=time.monotonic()+5)
            self.assertEqual(plane.scope_lifecycle.snapshot(), ())

    def test_card3_confirmed_left_retires_transport_without_native_effects(self):
        with self.managed_ready_target() as (plane, prepared, args, fx):
            scope = prepared.scope
            listener, thread = prepared.connector._listener, prepared.connector._thread
            journal_file = plane.journal.directory / pj.JOURNAL_FILE
            journal_bytes = journal_file.read_bytes()
            material = plane.joining_store.mint_load(fx["scope_key"])
            # This is the durable acknowledged-leave transition, not a failed or
            # guessed revoke. Its owner-authorized native revoke is a separate path.
            writer = fx["fx"]["joining_store"]
            record = writer.find_import(scope.lobby_id, scope.invite_id)
            writer.mark_left(record["import_id"])
            self.assertEqual(plane.joining_store.find_import(scope.lobby_id, scope.invite_id)["state"], "left")
            with mock.patch.object(prepared.connector, "close", wraps=prepared.connector.close) as close:
                plane.scope_lifecycle.reconcile(deadline=time.monotonic()+5)
                close.assert_called_once_with()
            self.assertEqual(plane.scope_lifecycle.snapshot(), ())
            self.assertIsNone(prepared.connector._listener)
            self.assertIsNone(prepared.connector._thread)
            self.assertFalse(thread.is_alive())
            self.assertEqual(listener.fileno(), -1)
            self.assertEqual(journal_file.read_bytes(), journal_bytes)
            self.assertIs(plane.authority.admit(scope.endpoint_id, scope.pairing_generation, "data_native"), True)
            self.assertEqual(plane.joining_store.mint_load(fx["scope_key"]), material)
            with self.assertRaises(sdp().DataPlaneError):
                plane.scope_lifecycle.ensure_target(**args, deadline=time.monotonic()+5)
            self.assertEqual(plane.scope_lifecycle.snapshot(), ())

    def test_card3_managed_target_uses_receipt_verifier_fixed_bind_and_cutover(self):
        import test_target_material_source_w2d as tms
        from invite_rooms.run_bindings import TargetReceiptVerifier
        with tms.genuine_source_fixture() as fx:
            flow, scope = fx["flow"], fx["scope"]
            side = flow.b_side
            remote = side.service(self.clock).journal.snapshot().links[scope.endpoint_id]["peer"]["peer_id"]
            plane = self.plane(side, remote, adapter_id=adapter_id_for(scope),
                               joining_dir=fx["fx"]["joining_store"].root)

            @contextlib.asynccontextmanager
            async def native_catalog(_config):
                class Rpc:
                    async def __call__(self, method, params):
                        self_outer.assertEqual((method, params), ("groups.capabilities", {"profile": plane.config.profile}))
                        return {"room_link": {"enabled": True, "catalog": side.catalog}}
                yield Rpc()

            self_outer = self
            plane.start()
            args = dict(endpoint_id=scope.endpoint_id, generation=scope.pairing_generation,
                        lobby_id=scope.lobby_id, invite_id=scope.invite_id)
            with mock.patch("invite_rooms.routes._connect_native_rpc", native_catalog):
                prepared = plane.scope_lifecycle.ensure_target(**args, deadline=time.monotonic()+10)
                self.assertEqual(prepared.connector.address, prepared.bind)
                verifier = prepared.connector.firewall._verify_native
                self.assertIs(type(verifier), TargetReceiptVerifier)
                self.assertFalse(verifier("run_"+"1"*32, prepared.expected_scope))
                self.assertIs(plane.scope_lifecycle.ensure_target(**args, deadline=time.monotonic()+10), prepared)
                # Separate durable writer cuts off admission; reconciliation closes only this resource.
                side.service(self.clock).unpair(remote)
                self.assertFalse(verifier("run_"+"1"*32, prepared.expected_scope))
                plane.scope_lifecycle.reconcile(deadline=time.monotonic()+5)
                self.assertEqual(plane.scope_lifecycle.snapshot(), ())
                self.assertIsNone(prepared.connector._thread)
                self.assertIsNone(prepared.connector._listener)
            plane.close()

    def test_target_connector_factory_sources_authority_coordinates_verbatim_from_signed_grant(self):
        import test_target_material_source_w2d as tms
        from gateway.hosted_room_peer import decode_room_grant
        module = sdp()
        with tms.genuine_source_fixture() as fx:
            flow, scope = fx["flow"], fx["scope"]
            side = flow.b_side
            adapter_id = adapter_id_for(scope)
            remote = side.service(self.clock).journal.snapshot().links[scope.endpoint_id]["peer"]["peer_id"]
            path = side.root / "cfg" / "runtime.json"
            private_json(path, self.config_dict(side, remote, adapter_id=adapter_id,
                                                joining_dir=fx["fx"]["joining_store"].root))
            runtime_config._warned_once.clear()
            loaded = runtime_config.load_service({CONFIG_ENV: str(path)})
            self.assertIsNotNone(loaded)
            plane = module.build_sidecar_data_plane(loaded)
            self.assertIsNotNone(plane)
            self.planes.append(plane)
            claims = decode_room_grant(fx["native_secret"], fx["grant"], permission="dispatch")
            args = dict(endpoint_id=scope.endpoint_id, generation=scope.pairing_generation,
                        lobby_id=scope.lobby_id, invite_id=scope.invite_id,
                        deadline=time.monotonic() + 10, verify_native=lambda *_: True)
            @contextlib.asynccontextmanager
            async def fake_connect(rpc_config):
                class Rpc:
                    async def __call__(self, method, params):
                        self_outer.assertEqual((method, params),
                                               ("groups.capabilities", {"profile": plane.config.profile}))
                        return {"room_link": {"enabled": True, "catalog": flow.b_side.catalog}}
                yield Rpc()
            self_outer = self
            base = {"room_id": scope.planned_room_id, "home_install_id": scope.owner_installation_id,
                    "member_id": scope.member_id, "target_install_id": scope.target_install_id,
                    "target_profile": scope.target_profile}
            starts = []
            with mock.patch("invite_rooms.routes._connect_native_rpc", fake_connect), \
                    mock.patch.object(TargetConnector, "start", lambda *a, **k: starts.append(1)):
                _EVENTS.clear()
                _ARMED.set()
                try:
                    prepared = plane.target_connector_factory(**args)
                finally:
                    _ARMED.clear()
                # The catalog source runs ``asyncio.run`` per read, whose loop self-pipe is a socketpair
                # (socket.__new__ only): no network operation of any kind may happen.
                self.assertEqual([e for e in _EVENTS if e != "socket.__new__"], [])
                self.assertEqual(starts, [])
                self.assertIs(type(prepared.connector), TargetConnector)
                self.assertIsNone(prepared.connector._listener)
                self.assertEqual(prepared.adapter_id, adapter_id)
                self.assertEqual(prepared.scope, scope)
                self.assertEqual(prepared.bind, plane.config.pairing.target_listeners[adapter_id])
                self.assertEqual(dict(prepared.expected_scope), {
                    **base, "authority_gateway_id": claims["authority_gateway_id"],
                    "authority_epoch": claims["authority_epoch"]})
                self.assertEqual(prepared.connector.firewall._scope, dict(prepared.expected_scope))
                self.assertEqual((prepared.connector.admission.binding.endpoint_id,
                                  prepared.connector.admission.binding.generation,
                                  prepared.connector.admission.binding.address),
                                 (scope.endpoint_id, scope.pairing_generation, prepared.bind))
                # Discriminator: the coordinates are READ from the signed grant claims, never derived.
                real = module._claim_payload
                def altered(token):
                    return dict(real(token), authority_gateway_id="gw-from-grant", authority_epoch=7)
                with mock.patch.object(module, "_claim_payload", altered):
                    other = plane.target_connector_factory(**args)
                self.assertEqual(other.expected_scope["authority_gateway_id"], "gw-from-grant")
                self.assertEqual(other.expected_scope["authority_epoch"], 7)
                # Claims that do not describe this exact scope / lack coordinates fail closed.
                for mutated in (dict(authority_epoch=0), dict(authority_epoch=True), dict(authority_gateway_id=""),
                                dict(authority_gateway_id=None), dict(room_id="other-room"),
                                dict(member_id="other"), dict(home_install_id=ps.INSTALL_C)):
                    with mock.patch.object(module, "_claim_payload",
                                           lambda token, m=mutated: dict(real(token), **m)):
                        with self.assertRaises(module.DataPlaneError, msg=repr(mutated)):
                            plane.target_connector_factory(**args)
                # No configured target bind for this scope => no connector.
                bare = dataclasses.replace(plane.config.pairing, target_listeners=MappingProxyType(
                    {"link-" + "9" * 32: ("127.0.0.1", free_port())}))
                no_bind = module.build_sidecar_data_plane(
                    (plane.owner_service, plane.owner, dataclasses.replace(plane.config, pairing=bare)))
                self.planes.append(no_bind)
                with self.assertRaises(module.DataPlaneError):
                    no_bind.target_connector_factory(**args)

    def test_target_connector_factory_refuses_without_ready_material_and_never_binds(self):
        plane = self.plane(self.B)
        deadline = time.monotonic() + 5
        error = sdp().DataPlaneError
        calls = []
        with mock.patch.object(TargetConnector, "start", lambda *a, **k: calls.append("start")):
            for kwargs in (
                    dict(endpoint_id=self.endpoint, generation=self.generation, lobby_id="lobby-1",
                         invite_id="invite-1", deadline=deadline, verify_native=lambda *_: True),   # no READY material
                    dict(endpoint_id=self.endpoint, generation=self.generation, lobby_id="lobby-1",
                         invite_id="invite-1", deadline=deadline, verify_native=None),
                    dict(endpoint_id=self.endpoint, generation=self.generation + 1, lobby_id="lobby-1",
                         invite_id="invite-1", deadline=deadline, verify_native=lambda *_: True),
                    dict(endpoint_id=self.endpoint, generation=self.generation, lobby_id="lobby-1",
                         invite_id="invite-1", deadline=time.monotonic() - 1, verify_native=lambda *_: True),
                    dict(endpoint_id=self.endpoint, generation=self.generation, lobby_id="lobby-1",
                         invite_id="invite-1", deadline=None, verify_native=lambda *_: True)):
                with self.assertRaises(error, msg=repr(kwargs)):
                    plane.target_connector_factory(**kwargs)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
