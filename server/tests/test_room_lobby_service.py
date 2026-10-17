"""Control-path tests against real native persistence, with disposable DBs.

The RPC adapter here executes native create/state validation, not a model or
network transport. Real WebSocket/peer registration is covered by gate_p2_p4_local.
"""
from __future__ import annotations

import copy
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, "/Users/albi/.hermes/hermes-agent")
from gateway import hosted_rooms, hosted_room_discussion
from room_invite_store import InviteError, RoomInviteStore
from room_lobby_service import NativeRpcRejected, PeerMaterial, RoomLobbyService

ROSTER = [
    {"member_id": "local-a", "profile": "default", "handle": "local"},
    {"member_id": "peer-b", "profile": "default", "handle": "peer",
     "target": {"kind": "peer", "peer_id": "owner-b",
                "installation_id": "install:" + "b" * 32, "profile": "default"}},
]
BINDING = {"endpoint_id": "endpoint-b", "fingerprint": "b" * 64,
           "target_install_id": "install:" + "b" * 32, "target_profile": "default"}


class LobbyServiceTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="room-lobby-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = RoomInviteStore(self.root / "invites.db")
        self.native_db = self.root / "native.db"
        self.authority = "install:" + "a" * 32  # Unit fixture, not a discovered live gateway.
        self.lobby = self.store.create_lobby(name="Known owners", roster=copy.deepcopy(ROSTER))
        self.issued = self.store.issue_invite(self.lobby["lobby_id"], "peer-b", invitee_label="B")
        self.catalog = {"installation_id": BINDING["target_install_id"], "catalog_digest": "d" * 64,
                        "execution_policy": {"target_profile": "default"}}
        self.calls = []
        self.drop_create_reply = False
        self.service = RoomLobbyService(self.store, self.rpc, self.peer_material)

    async def peer_material(self, peer):
        return PeerMaterial(binding=copy.deepcopy(BINDING), catalog=copy.deepcopy(self.catalog),
                            grant="unit-fixture-no-native-grant", adapter_url="http://127.0.0.1:19001/links/unit-link")

    async def rpc(self, method, params):
        self.calls.append(method)
        if method == "groups.capabilities":
            return {"driver": True, "authority_gateway_id": self.authority}
        if method == "groups.create":
            normalized = hosted_room_discussion.validate_roster(params["members"], local_profiles=["default"])
            members = [{"member_id": m.member_id, "profile": m.profile, "handle": m.handle,
                        "target": dict(m.target), **({"display_name": m.display_name} if m.display_name else {})}
                       for m in normalized]
            try:
                room = hosted_rooms.create_room(self.native_db, room_id=params["room_id"], name=params["name"],
                                                members=members, authority_gateway_id=self.authority)
            except hosted_rooms.RoomConflictError:
                # In-process adapter equivalent of an explicit native RPC rejection.
                raise NativeRpcRejected("room_conflict") from None
            if self.drop_create_reply:
                self.drop_create_reply = False
                raise TimeoutError("lost reply")
            return {"room": room}
        if method == "groups.state":
            return {"room": hosted_rooms.room_state(self.native_db, room_id=params["room_id"])}
        raise AssertionError("Unexpected native call: " + method)

    def ready(self):
        lobby_id, invite_id = self.lobby["lobby_id"], self.issued.invite_id
        self.store.redeem(lobby_id, invite_id, self.issued.secret, binding=BINDING)
        self.store.decide_host(lobby_id, invite_id, binding=BINDING, approved=True)
        self.store.consent_joining(lobby_id, invite_id, binding=BINDING, consented=True)

    async def test_create_requires_consent_before_any_native_call(self):
        with self.assertRaisesRegex(InviteError, "^consent_required$"):
            await self.service.create_room(self.lobby["lobby_id"])
        self.assertEqual(self.calls, [])
        self.assertFalse(self.native_db.exists())

    async def test_native_creation_is_idempotent_and_preserves_exact_roster(self):
        self.ready()
        first = await self.service.create_room(self.lobby["lobby_id"])
        second = await self.service.create_room(self.lobby["lobby_id"])
        self.assertEqual(first["room_id"], self.lobby["planned_room_id"])
        self.assertEqual(second["room_id"], first["room_id"])
        self.assertEqual(first["members"], second["members"])
        self.assertEqual(first["members"][1]["target"]["capability_digest"], "d" * 64)
        self.assertEqual(self.store.get_lobby(self.lobby["lobby_id"])["state"], "created")
        with sqlite3.connect(self.native_db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM hosted_rooms").fetchone()[0], 1)
        self.assertNotIn("groups.send", self.calls)
        self.assertNotIn("groups.peer.invite", self.calls)
        for file in self.root.iterdir():
            if file.is_file():
                self.assertNotIn(b"unit-fixture-no-native-grant", file.read_bytes())


    async def test_lost_create_reply_retries_only_same_pinned_room(self):
        self.ready()
        self.drop_create_reply = True
        with self.assertRaisesRegex(InviteError, "^creation_ambiguous$"):
            await self.service.create_room(self.lobby["lobby_id"])
        self.assertEqual(self.store.get_lobby(self.lobby["lobby_id"])["state"], "sealed")
        restarted = RoomLobbyService(RoomInviteStore(self.store.path), self.rpc, self.peer_material)
        recovered = await restarted.create_room(self.lobby["lobby_id"])
        self.assertEqual(recovered["room_id"], self.lobby["planned_room_id"])
        with sqlite3.connect(self.native_db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM hosted_rooms").fetchone()[0], 1)

    async def test_existing_foreign_room_conflict_never_replaces_native_state(self):
        self.ready()
        hosted_rooms.create_room(self.native_db, room_id=self.lobby["planned_room_id"], name="Existing foreign room",
                                 members=[ROSTER[0]], authority_gateway_id=self.authority)
        with self.assertRaisesRegex(InviteError, "^room_conflict$"):
            await self.service.create_room(self.lobby["lobby_id"])
        native = hosted_rooms.room_state(self.native_db, room_id=self.lobby["planned_room_id"])
        self.assertEqual(native["name"], "Existing foreign room")
        self.assertEqual(native["members"], [ROSTER[0]])
        self.assertIsNone(self.store.get_lobby(self.lobby["lobby_id"])["room_id"])

    async def test_changed_catalog_or_authority_cannot_rewrite_frozen_spec(self):
        self.ready()
        await self.service.create_room(self.lobby["lobby_id"])
        original = copy.deepcopy(self.catalog)
        self.catalog["catalog_digest"] = "e" * 64
        with self.assertRaisesRegex(InviteError, "^native_spec_conflict$"):
            await self.service.create_room(self.lobby["lobby_id"])
        self.catalog = original
        self.authority = "install:" + "c" * 32
        with self.assertRaisesRegex(InviteError, "^native_spec_conflict$"):
            await self.service.create_room(self.lobby["lobby_id"])

    async def test_registration_rejects_request_controlled_urls_before_native_mutation(self):
        self.ready()
        for url in ("https://attacker.invalid/links/a", "http://127.0.0.1:8765/api/local/config",
                    "http://localhost:19001/links/a", "http://user@127.0.0.1:19001/links/a",
                    "http://127.0.0.1:19001/links/a?next=evil", "http://127.0.0.1:19001/links/../v1/runs"):
            async def poisoned(peer):
                return PeerMaterial(binding=BINDING, catalog=self.catalog, grant="do-not-log", adapter_url=url)
            service = RoomLobbyService(self.store, self.rpc, poisoned)
            with self.subTest(url=url), self.assertRaisesRegex(InviteError, "^invalid_adapter$"):
                await service.activate(self.lobby["lobby_id"])
        self.assertFalse(self.native_db.exists())


    async def test_native_labels_are_canonical_before_freeze_and_restart(self):
        roster = copy.deepcopy(ROSTER)
        roster[0]["display_name"] = " Local bot "
        roster[1]["display_name"] = " Remote bot "
        self.lobby = self.store.create_lobby(name=" Known owners ", roster=roster)
        self.issued = self.store.issue_invite(self.lobby["lobby_id"], "peer-b", invitee_label="B")
        self.ready()
        first = await self.service.create_room(self.lobby["lobby_id"])
        restarted = RoomLobbyService(RoomInviteStore(self.store.path), self.rpc, self.peer_material)
        second = await restarted.create_room(self.lobby["lobby_id"])
        self.assertEqual(first["name"], "Known owners")
        self.assertEqual([m["display_name"] for m in first["members"]], ["Local bot", "Remote bot"])
        self.assertEqual(second["members"], first["members"])
        self.assertEqual(self.store.get_lobby(self.lobby["lobby_id"])["state"], "created")

    async def test_non_timeout_lost_create_reply_is_ambiguous_without_secret_diagnostic(self):
        import traceback
        import uuid
        self.ready()
        original = self.rpc
        marker = uuid.uuid4().hex
        async def lost(method, params):
            result = await original(method, params)
            if method == "groups.create":
                raise ConnectionResetError(marker)
            return result
        service = RoomLobbyService(self.store, lost, self.peer_material)
        try:
            await service.create_room(self.lobby["lobby_id"])
        except InviteError as exc:
            self.assertEqual(str(exc), "creation_ambiguous")
            self.assertNotIn(marker, traceback.format_exc())
        else:
            self.fail("lost native create reply must not be reported as success")
        recovered = await self.service.create_room(self.lobby["lobby_id"])
        self.assertEqual(recovered["room_id"], self.lobby["planned_room_id"])

    async def test_invalid_readback_after_create_is_ambiguous_not_an_attribute_error(self):
        self.ready()
        original = self.rpc
        for malformed in (None, [], {}, {"room": None}, {"room": {}}):
            async def broken_state(method, params):
                if method == "groups.state":
                    return malformed
                return await original(method, params)
            service = RoomLobbyService(self.store, broken_state, self.peer_material)
            with self.subTest(malformed=malformed), self.assertRaisesRegex(InviteError, "^creation_ambiguous$"):
                await service.create_room(self.lobby["lobby_id"])
        self.assertEqual(self.store.get_lobby(self.lobby["lobby_id"])["state"], "sealed")
        await self.service.create_room(self.lobby["lobby_id"])


    async def test_definitive_create_rejection_is_distinct_from_lost_reply(self):
        from room_lobby_service import NativeRpcRejected
        self.ready()
        async def rejected(method, params):
            if method == "groups.create":
                raise NativeRpcRejected("room_conflict")
            return await self.rpc(method, params)
        service = RoomLobbyService(self.store, rejected, self.peer_material)
        with self.assertRaisesRegex(InviteError, "^room_conflict$"):
            await service.create_room(self.lobby["lobby_id"])
        self.assertFalse(self.native_db.exists())


    async def test_post_register_malformed_readback_is_sanitized_without_local_success(self):
        self.ready()
        original = self.rpc
        registered = False
        malformed = "state"
        async def broken_after_register(method, params):
            nonlocal registered
            if method == "groups.peer.register":
                registered = True
                return {"registered": True, "target_install_id": BINDING["target_install_id"], "target_profile": "default"}
            result = await original(method, params)
            if method == "groups.state" and registered:
                if malformed == "state":
                    return None
                result["driver_status"] = None if malformed == "driver" else {"peer_routes": None}
            return result
        service = RoomLobbyService(self.store, broken_after_register, self.peer_material)
        for malformed in ("state", "driver", "routes"):
            registered = False
            with self.subTest(malformed=malformed), self.assertRaisesRegex(InviteError, "^registration_ambiguous$"):
                await service.activate(self.lobby["lobby_id"])
        with sqlite3.connect(self.store.path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM peer_links").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
