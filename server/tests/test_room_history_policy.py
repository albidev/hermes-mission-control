from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import tempfile
import unittest

from room_invite_store import InviteError, RoomInviteStore


ROSTER = [
    {"member_id": "local-a", "profile": "default", "handle": "local"},
    {"member_id": "peer-b", "profile": "default", "handle": "peer",
     "target": {"kind": "peer", "peer_id": "owner-b",
                "installation_id": "install:" + "b" * 32, "profile": "default"}},
]
BINDING = {"endpoint_id": "endpoint-b", "fingerprint": "b" * 64,
           "target_install_id": "install:" + "b" * 32, "target_profile": "default",
           "catalog_digest": "c" * 64, "policy_digest": "d" * 64}


class RoomHistoryPolicyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="room-history-policy-")
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "invites.sqlite"
        self.store = RoomInviteStore(self.path)

    def _ready(self, lobby):
        invite = self.store.issue_invite(lobby["lobby_id"], "peer-b", invitee_label="Peer")
        self.store.redeem(lobby["lobby_id"], invite.invite_id, invite.secret, binding=BINDING)
        self.store.decide_host(lobby["lobby_id"], invite.invite_id, binding=BINDING, approved=True)
        self.store.consent_joining(lobby["lobby_id"], invite.invite_id, binding=BINDING, consented=True)
        return invite

    def test_creation_persists_immutable_policy_and_canonical_room_binding(self):
        lobby = self.store.create_lobby(name="Room", roster=ROSTER, history_policy="full", creation_id="create-1")
        self.assertEqual(self.store.get_history_policy(lobby["lobby_id"]), {
            "mode": "full", "lobby_id": lobby["lobby_id"], "room_id": lobby["planned_room_id"],
            "creation_id": "create-1",
        })
        self.assertEqual(self.store.bind_room_history_policy(lobby["lobby_id"], lobby["planned_room_id"]),
                         self.store.get_history_policy(lobby["lobby_id"]))
        with self.assertRaisesRegex(InviteError, "^history_policy_conflict$"):
            self.store.bind_room_history_policy(lobby["lobby_id"], "invite-room-" + "c" * 32)

    def test_duplicate_creation_is_idempotent_only_for_exact_policy_and_payload(self):
        first = self.store.create_lobby(name="Room", roster=ROSTER, grant_ttl_hint_seconds=600, history_policy="postjoin", creation_id="create-1")
        again = self.store.create_lobby(name="Room", roster=ROSTER, grant_ttl_hint_seconds=600, history_policy="postjoin", creation_id="create-1")
        self.assertEqual(again["lobby_id"], first["lobby_id"])
        with self.assertRaisesRegex(InviteError, "^history_policy_conflict$"):
            self.store.create_lobby(name="Room", roster=ROSTER, history_policy="full", creation_id="create-1")
        with self.assertRaisesRegex(InviteError, "^history_policy_conflict$"):
            self.store.create_lobby(name="Other", roster=ROSTER, history_policy="postjoin", creation_id="create-1")
        with self.assertRaisesRegex(InviteError, "^history_policy_conflict$"):
            self.store.create_lobby(name="Room", roster=ROSTER, grant_ttl_hint_seconds=601, history_policy="postjoin", creation_id="create-1")

    def test_postjoin_boundary_is_distinct_per_recipient_and_immutable(self):
        lobby = self.store.create_lobby(name="Room", roster=ROSTER, history_policy="postjoin")
        invite = self._ready(lobby)
        admitted = self.store.record_history_admission(lobby["lobby_id"], "peer-b", invite.invite_id, 7)
        self.assertEqual(admitted["from_seq_inclusive"], 7)
        self.assertEqual(self.store.record_history_admission(lobby["lobby_id"], "peer-b", invite.invite_id, 7), admitted)
        with self.assertRaisesRegex(InviteError, "^history_admission_conflict$"):
            self.store.record_history_admission(lobby["lobby_id"], "peer-b", invite.invite_id, 8)

    def test_history_scope_requires_registration_and_keeps_admission_identity_separate_from_cursor(self):
        self.store.bind_owner_scope("install:" + "a" * 32, "default")
        lobby = self.store.create_lobby(name="Room", roster=ROSTER, history_policy="postjoin")
        invite = self._ready(lobby)
        self.store.seal_lobby(lobby["lobby_id"])
        self.store.pin_native_spec(lobby["lobby_id"], {"room_id": lobby["planned_room_id"]})
        self.store.mark_created(lobby["lobby_id"], lobby["planned_room_id"])
        self.store.record_history_admission(lobby["lobby_id"], "peer-b", invite.invite_id, 7)
        with self.assertRaisesRegex(InviteError, "^invite_unavailable$"):
            self.store.history_scope_for_peer(lobby["planned_room_id"], peer_id="owner-b", binding=BINDING)
        self.store.mark_peer_registered(lobby["lobby_id"], "peer-b")
        full_binding = dict(BINDING, catalog_digest="c" * 64, policy_digest="d" * 64)
        scope = self.store.history_scope_for_peer(lobby["planned_room_id"], peer_id="owner-b", binding=full_binding)
        self.assertEqual(scope["from_seq_inclusive"], 7)
        admission = self.store.get_history_admission(lobby["lobby_id"], "peer-b")
        self.assertEqual(self.store.history_admission_generation(
            lobby["lobby_id"], "peer-b", invite.invite_id), admission["admission_generation"])
        self.assertNotEqual(admission["admission_generation"], admission["from_seq_inclusive"])

    def test_missing_or_malformed_legacy_policy_fails_closed_without_inference(self):
        lobby = self.store.create_lobby(name="Legacy", roster=ROSTER)
        self.assertIsNone(self.store.get_history_policy(lobby["lobby_id"]))
        invite = self._ready(lobby)
        with self.assertRaisesRegex(InviteError, "^history_policy_unavailable$"):
            self.store.record_history_admission(lobby["lobby_id"], "peer-b", invite.invite_id, 1)
        malformed = self.store.create_lobby(name="Malformed", roster=ROSTER, history_policy="full")
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE history_policies SET canonical_room_id='' WHERE lobby_id=?", (malformed["lobby_id"],))
        with self.assertRaisesRegex(InviteError, "^history_policy_unavailable$"):
            self.store.get_history_policy(malformed["lobby_id"])

    def test_cross_room_binding_collision_fails_closed(self):
        first = self.store.create_lobby(name="First", roster=ROSTER, history_policy="full")
        second = self.store.create_lobby(name="Second", roster=ROSTER)
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE history_policies SET canonical_room_id=? WHERE lobby_id=?", (second["planned_room_id"], first["lobby_id"]))
        with self.assertRaisesRegex(InviteError, "^history_policy_unavailable$"):
            self.store.get_history_policy(first["lobby_id"])
        invite = self._ready(first)
        with self.assertRaisesRegex(InviteError, "^history_policy_unavailable$"):
            self.store.record_history_admission(first["lobby_id"], "peer-b", invite.invite_id, 1)

        lobby = self.store.create_lobby(name="Legacy", roster=ROSTER)
        with sqlite3.connect(self.path) as conn:
            conn.execute("DROP TABLE history_admissions")
            conn.execute("DROP TABLE history_policies")
            conn.execute("UPDATE schema_version SET version=2")
        resumed = RoomInviteStore(self.path)
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute("SELECT version FROM schema_version").fetchone(), (3,))
        self.assertIsNone(resumed.get_history_policy(lobby["lobby_id"]))

        lobby = self.store.create_lobby(name="Room", roster=ROSTER, history_policy="postjoin")
        invite = self._ready(lobby)
        def attempt(boundary):
            store = RoomInviteStore(self.path)
            try:
                return store.record_history_admission(lobby["lobby_id"], "peer-b", invite.invite_id, boundary)
            except InviteError as exc:
                return str(exc)
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(attempt, (7, 8)))
        self.assertEqual(sum(isinstance(item, dict) for item in outcomes), 1)
        self.assertEqual(sum(item == "history_admission_conflict" for item in outcomes), 1)
        resumed = RoomInviteStore(self.path)
        self.assertEqual(resumed.get_history_admission(lobby["lobby_id"], "peer-b"),
                         next(item for item in outcomes if isinstance(item, dict)))


if __name__ == "__main__":
    unittest.main()
