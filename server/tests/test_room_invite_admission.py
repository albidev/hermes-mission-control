"""P3b.0-F R-01 (R1-05 admission seam / TOCTOU) and R-05 (receipt+route capability gate).

The activation runs through the PRODUCTION ``BoundedNativeRpc`` over an in-memory socket
whose idle drain is held open (barrier), while the authoritative ``peers.json`` is changed
on disk.  Nothing here claims native admission or a live mutation; it proves the register
frame is not emitted once the on-disk registry no longer matches the admitted request.
"""
from __future__ import annotations

import asyncio
import copy
import json
import os
import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from invite_rooms import runtime_config
from invite_rooms.native_rpc import BoundedNativeRpc, NativeRpcDenied
from invite_rooms.peer_registry import MaterialScope, PeerRegistryError
from invite_rooms.service import ServiceError

try:
    from server.tests.test_room_invite_operations import ROSTER, CATALOG, INSTALL_A, GRANT
    from server.tests.test_room_invite_routes import _peer_registry_payload, target_signed_envelope
    from server.tests.test_room_invite_final_regressions import RecordingRpc
except ModuleNotFoundError:
    from test_room_invite_operations import ROSTER, CATALOG, INSTALL_A, GRANT
    from test_room_invite_routes import _peer_registry_payload, target_signed_envelope
    from test_room_invite_final_regressions import RecordingRpc


class MemorySocket:
    """In-memory native socket. The idle drain blocks until ``release`` is set (the barrier)."""
    def __init__(self, ordinary):
        self.sent, self.reply, self.ordinary = None, None, ordinary
        self.draining, self.release = asyncio.Event(), asyncio.Event()

    async def receive(self, **kwargs):
        if self.sent is None:
            self.draining.set()
            await self.release.wait()
            raise TimeoutError()
        return SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=json.dumps(self.reply))

    async def send_str(self, raw):
        self.sent = json.loads(raw)
        result = await self.ordinary(self.sent["method"], self.sent["params"])
        self.reply = dict(jsonrpc="2.0", id=self.sent["id"], result=result)


class Harness:
    """A real load_service() build: on-disk peers.json is the authoritative registry source."""
    def __init__(self, case):
        tmp = tempfile.TemporaryDirectory(prefix="admission-")
        case.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "cfg"
        self.root.mkdir(mode=0o700)
        os.chmod(self.root, 0o700)
        self.peers = self.root / "peers.json"
        self._write(self.peers, _peer_registry_payload())
        config = self.root / "config.json"
        self._write(config, json.dumps({"store_path": str(self.root / "invites.db"), "installation_id": INSTALL_A,
                                        "profile": "default", "peer_registry_path": str(self.peers)}))
        self.env = {runtime_config._CONFIG_ENV: str(config)}
        loaded = runtime_config.load_service(self.env)
        case.assertIsNotNone(loaded)
        self.svc, self.ctx, self.config = loaded
        self.reg, self.store = self.svc.registry, self.svc.store
        lobby = self.svc.create_lobby(self.ctx, name="Admission", roster=copy.deepcopy(ROSTER))
        self.lobby_id = lobby["lobby_id"]
        inv = self.svc.issue_invite(self.ctx, self.lobby_id, "peer-b", invitee_label="B")
        binding = self.reg.binding_for("owner-b")
        self.store.redeem(self.lobby_id, inv.invite_id, inv.secret, binding=binding)
        self.store.decide_host(self.lobby_id, inv.invite_id, binding=binding, approved=True)
        self.store.consent_joining(self.lobby_id, inv.invite_id, binding=binding, consented=True)
        self.scope = MaterialScope(owner_installation_id=INSTALL_A, owner_profile="default", lobby_id=self.lobby_id,
                                   planned_room_id=lobby["planned_room_id"], member_id="peer-b",
                                   invite_id=inv.invite_id, peer_id="owner-b", **binding)
        self.reg.admit_signed_material(self.scope, target_signed_envelope(self.scope, GRANT, CATALOG))

    @staticmethod
    def _write(path, text):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(text)

    def rewrite_peers(self, mutate):
        """Plain atomic file replacement by 'someone else' (no registry API involved)."""
        data = json.loads(self.peers.read_text())
        changed = mutate(data)
        data = data if changed is None else changed
        new = self.peers.with_name("peers.json.new")
        self._write(new, json.dumps(data))
        os.replace(new, self.peers)

    def with_route(self):
        self.reg._route_provider = lambda _scope: 19001


def bump_generation(data):
    data[0]["pairing_generation"] = 2


def change_endpoint(data):
    data[0]["endpoint_id"] = "endpoint-evil"


def change_fingerprint(data):
    data[0]["fingerprint"] = "c" * 64


def remove_peer(data):
    return []


class AdmissionSeamTests(unittest.IsolatedAsyncioTestCase):
    async def run_native_activation(self, h, change=None):
        ordinary = RecordingRpc(INSTALL_A)
        sock = MemorySocket(ordinary)
        native = BoundedNativeRpc(sock)

        async def dispatch(method, params):
            if method == "groups.peer.register":
                return await native(method, params)
            return await ordinary(method, params)

        h.svc._rpc = dispatch
        task = asyncio.create_task(h.svc.activate(h.ctx, h.lobby_id))
        await asyncio.wait_for(sock.draining.wait(), 5)
        if change is not None:
            change()  # lands while the production adapter is awaiting its idle drain
        sock.release.set()
        try:
            return ordinary, sock, await task, None
        except ServiceError as exc:
            return ordinary, sock, None, str(exc)

    async def test_control_unchanged_authoritative_registry_is_admitted_and_register_is_sent(self):
        h = Harness(self); h.with_route()
        ordinary, sock, result, err = await self.run_native_activation(h)
        self.assertIsNone(err)
        self.assertEqual(result["status"], "activated")
        self.assertEqual(sock.sent["method"], "groups.peer.register")
        self.assertEqual(ordinary.mutations(), ["groups.create", "groups.peer.register"])

    async def test_cutover_during_native_idle_drain_never_sends_register(self):
        for label, mutate in (("generation", bump_generation), ("endpoint", change_endpoint),
                              ("binding_fingerprint", change_fingerprint), ("peer_removed", remove_peer)):
            with self.subTest(change=label):
                h = Harness(self); h.with_route()
                ordinary, sock, result, err = await self.run_native_activation(h, lambda: h.rewrite_peers(mutate))
                self.assertIsNone(result)
                self.assertIn(err, {"registration_ambiguous", "activation_failed"})  # generic code, no detail
                self.assertIsNone(sock.sent, "the register frame must never be emitted")
                self.assertEqual(ordinary.mutations(), ["groups.create"])

    async def test_admission_reads_the_disk_not_the_in_memory_registry_of_the_request(self):
        h = Harness(self); h.with_route()
        ordinary, sock, result, err = await self.run_native_activation(h, lambda: h.rewrite_peers(bump_generation))
        self.assertIsNotNone(err)
        self.assertEqual(h.reg.lookup("owner-b").pairing_generation, 1)  # the instance in memory never saw it
        other = runtime_config.build_peer_registry(h.peers)  # a registry rebuilt independently from disk
        self.assertEqual(other.lookup("owner-b").pairing_generation, 2)
        self.assertIsNone(sock.sent)

    async def test_cutover_during_groups_create_is_seen_through_the_disk_registry(self):
        h = Harness(self); h.with_route()

        class BumpingRpc(RecordingRpc):
            async def __call__(inner, method, params):
                result = await super(BumpingRpc, inner).__call__(method, params)
                if method == "groups.create":
                    h.rewrite_peers(bump_generation)  # re-pair lands mid-activation, on disk only
                return result

        rpc = BumpingRpc(INSTALL_A)
        h.svc._rpc = rpc
        with self.assertRaises(ServiceError):
            await h.svc.activate(h.ctx, h.lobby_id)
        self.assertNotIn("groups.peer.register", [m for m, _ in rpc.calls])

    async def test_missing_authoritative_source_fails_closed(self):
        h = Harness(self); h.with_route()
        h.reg._source = None
        ordinary, sock, result, err = await self.run_native_activation(h)
        self.assertIsNone(result)
        self.assertIsNone(sock.sent)

    async def test_register_without_admission_is_denied_when_required_by_the_transport(self):
        ordinary = RecordingRpc(INSTALL_A)
        sock = MemorySocket(ordinary); sock.release.set()
        native = BoundedNativeRpc(sock, require_register_admission=True)
        params = {"room_id": "room-r1", "member_id": "m", "target_url": "http://127.0.0.1:19001/links/x",
                  "target_profile": "default", "grant": GRANT, "catalog": copy.deepcopy(CATALOG)}
        with self.assertRaises(NativeRpcDenied):
            await native("groups.peer.register", params)
        self.assertIsNone(sock.sent)

    async def test_routes_build_the_production_transport_with_required_admission(self):
        from invite_rooms import routes
        import inspect
        self.assertIn("require_register_admission=True", inspect.getsource(routes._connect_native_rpc))

    def test_admission_check_serializes_with_the_cutover_lock(self):
        h = Harness(self)
        held, done = threading.Event(), threading.Event()

        def cutover():
            with h.reg._source.write_lock():
                held.set()
                time.sleep(0.4)
                h.rewrite_peers(bump_generation)
            done.set()

        t = threading.Thread(target=cutover); t.start()
        self.assertTrue(held.wait(5))
        started = time.monotonic()
        with self.assertRaises(PeerRegistryError):
            h.reg.check_admission("owner-b", 1, h.reg.binding_for("owner-b"))
        self.assertGreaterEqual(time.monotonic() - started, 0.3)  # waited for the in-flight cutover, then saw it
        t.join(5)
        self.assertTrue(done.is_set())

    def test_registry_source_replace_is_atomic_private_and_takes_the_same_lock(self):
        h = Harness(self)
        entries = json.loads(h.peers.read_text()); entries[0]["pairing_generation"] = 3
        h.reg._source.replace(entries)
        self.assertEqual(stat.S_IMODE(h.peers.stat().st_mode), 0o600)
        self.assertEqual(runtime_config.build_peer_registry(h.peers).lookup("owner-b").pairing_generation, 3)
        lock = [p for p in h.root.iterdir() if "lock" in p.name]
        self.assertEqual(len(lock), 1)
        self.assertEqual(stat.S_IMODE(lock[0].stat().st_mode), 0o600)
        self.assertEqual([p.name for p in h.root.iterdir() if p.name.endswith((".new", ".tmp"))], [])


class ActivationCapabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_receipt_and_route_without_any_material_resolver_activates(self):
        h = Harness(self); h.with_route()
        self.assertIsNone(h.svc._material_resolver)
        rpc = RecordingRpc(INSTALL_A); h.svc._rpc = rpc
        result = await h.svc.activate(h.ctx, h.lobby_id)
        self.assertEqual(result["status"], "activated")
        self.assertEqual(rpc.mutations(), ["groups.create", "groups.peer.register"])

    async def test_missing_route_provider_fails_generically_with_zero_rpc_calls(self):
        h = Harness(self)  # receipt admitted, but no route provider installed
        rpc = RecordingRpc(INSTALL_A); h.svc._rpc = rpc
        with self.assertRaisesRegex(ServiceError, "^material_channel_unavailable$"):
            await h.svc.activate(h.ctx, h.lobby_id)
        self.assertEqual(rpc.calls, [])

    async def test_missing_receipt_store_fails_generically_with_zero_rpc_calls(self):
        h = Harness(self); h.with_route()
        h.reg._receipt_store = None
        rpc = RecordingRpc(INSTALL_A); h.svc._rpc = rpc
        with self.assertRaisesRegex(ServiceError, "^material_channel_unavailable$"):
            await h.svc.activate(h.ctx, h.lobby_id)
        self.assertEqual(rpc.calls, [])

    async def test_a_legacy_resolver_is_never_consulted_by_the_receipt_path(self):
        h = Harness(self); h.with_route()
        consulted = []

        async def resolver(_):
            consulted.append(1)
            raise AssertionError("legacy resolver must not be consulted")

        h.svc._material_resolver = resolver
        rpc = RecordingRpc(INSTALL_A); h.svc._rpc = rpc
        self.assertEqual((await h.svc.activate(h.ctx, h.lobby_id))["status"], "activated")
        self.assertEqual(consulted, [])


if __name__ == "__main__":
    unittest.main()
