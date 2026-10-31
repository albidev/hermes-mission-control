"""Invite-only lobby persistence; all data is disposable, never production state."""
from __future__ import annotations

import hashlib
import importlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from room_invite_store import InviteError, RoomInviteStore


ROSTER = [
    {"member_id": "local-a", "profile": "default", "handle": "local"},
    {"member_id": "peer-b", "profile": "default", "handle": "peer",
     "target": {"kind": "peer", "peer_id": "owner-b",
                "installation_id": "install:" + "b" * 32, "profile": "default"}},
]
BINDING = {"endpoint_id": "endpoint-b", "fingerprint": "b" * 64,
           "target_install_id": "install:" + "b" * 32, "target_profile": "default"}


class InviteStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="room-invites-")
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "invites.sqlite"
        self.now = [1000.0]
        self.store = RoomInviteStore(self.path, clock=lambda: self.now[0])
        self.lobby = self.store.create_lobby(name="Known owners", roster=ROSTER)

    def issue(self, **kwargs):
        return self.store.issue_invite(self.lobby["lobby_id"], "peer-b", invitee_label="Owner B", **kwargs)

    def test_issue_returns_secret_once_but_preview_and_storage_keep_only_digest(self):
        issued = self.issue()
        preview = self.store.preview(issued.invite_id, issued.secret)
        self.assertEqual(preview["name"], "Known owners")
        self.assertEqual(preview["expires_at"], 1000 + 86400)
        self.assertNotIn(issued.secret, repr(issued))
        self.assertNotIn(issued.secret, json.dumps(preview))
        with sqlite3.connect(self.path) as conn:
            row = conn.execute("SELECT secret_sha256, state FROM invitations").fetchone()
            self.assertEqual(row, (hashlib.sha256(issued.secret.encode()).hexdigest(), "open"))
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        for file in self.path.parent.iterdir():
            if file.is_file():
                self.assertNotIn(issued.secret.encode(), file.read_bytes())
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)


    def test_twenty_concurrent_redemptions_admit_exactly_one_identity(self):
        from concurrent.futures import ThreadPoolExecutor
        import threading
        issued = self.issue()
        barrier = threading.Barrier(20)

        def redeem(index):
            store = RoomInviteStore(self.path, clock=lambda: self.now[0])
            barrier.wait(timeout=10)
            try:
                return store.redeem(self.lobby["lobby_id"], issued.invite_id, issued.secret,
                                    binding=BINDING | {"endpoint_id": f"endpoint-{index}"})
            except InviteError as exc:
                return str(exc)

        with ThreadPoolExecutor(max_workers=20) as pool:
            outcomes = list(pool.map(redeem, range(20)))
        winners = [item for item in outcomes if isinstance(item, dict)]
        self.assertEqual(len(winners), 1)
        self.assertEqual(outcomes.count("invite_unavailable"), 19)
        self.assertEqual(winners[0]["status"], "awaiting_host_approval")
        resumed = RoomInviteStore(self.path, clock=lambda: self.now[0])
        self.assertEqual(resumed.get_redemption(self.lobby["lobby_id"], issued.invite_id), winners[0])
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM redemptions").fetchone()[0], 1)


    def test_unavailable_cases_have_one_error_without_secret_in_logs(self):
        import logging
        from io import StringIO
        stream = StringIO()
        handler = logging.StreamHandler(stream)
        logging.getLogger().addHandler(handler)
        self.addCleanup(logging.getLogger().removeHandler, handler)
        for reason in ("expired", "revoked", "replayed", "bad-secret", "wrong-room"):
            with self.subTest(reason=reason):
                self.lobby = self.store.create_lobby(name="Known owners", roster=ROSTER)
                issued = self.issue(ttl_seconds=60)
                secret, lobby_id = issued.secret, self.lobby["lobby_id"]
                if reason == "expired":
                    self.now[0] += 60
                if reason == "revoked":
                    self.store.revoke_invite(lobby_id, issued.invite_id)
                if reason == "replayed":
                    self.store.redeem(lobby_id, issued.invite_id, secret, binding=BINDING)
                if reason == "bad-secret":
                    secret = "x" * 43
                if reason == "wrong-room":
                    lobby_id = "another-lobby"
                with self.assertRaisesRegex(InviteError, "^invite_unavailable$") as caught:
                    self.store.redeem(lobby_id, issued.invite_id, secret, binding=BINDING)
                self.assertNotIn(issued.secret, str(caught.exception))
                self.assertNotIn(issued.secret, stream.getvalue())

    def test_wrong_peer_cannot_consume_invite(self):
        issued = self.issue()
        for key, value in (("target_install_id", "install:" + "c" * 32),
                           ("target_profile", "other"), ("fingerprint", "bad-pin"),
                           ("endpoint_id", "https://attacker.invalid")):
            with self.subTest(key=key):
                with self.assertRaises(InviteError):
                    self.store.redeem(self.lobby["lobby_id"], issued.invite_id, issued.secret,
                                      binding=BINDING | {key: value})
        self.assertEqual(self.store.redeem(self.lobby["lobby_id"], issued.invite_id, issued.secret,
                                          binding=BINDING)["endpoint_id"], "endpoint-b")

    def test_invalid_ttl_or_unknown_member_is_rejected(self):
        for ttl in (0, -1, 604801, True, float("nan"), float("inf"), "3600"):
            with self.subTest(ttl=ttl), self.assertRaisesRegex(InviteError, "^invalid_invite$"):
                self.issue(ttl_seconds=ttl)
        with self.assertRaises(InviteError):
            self.store.issue_invite(self.lobby["lobby_id"], "not-a-member", invitee_label="X")


    def test_host_and_joining_consent_are_separate_bound_and_persistent(self):
        issued = self.issue(ttl_seconds=1)
        lobby_id = self.lobby["lobby_id"]
        self.store.redeem(lobby_id, issued.invite_id, issued.secret, binding=BINDING)
        with self.assertRaisesRegex(InviteError, "^host_approval_required$"):
            self.store.consent_joining(lobby_id, issued.invite_id, binding=BINDING, consented=True)
        for key in BINDING:
            wrong = BINDING | {key: ("c" * 64 if key == "fingerprint" else "other")}
            with self.subTest(key=key), self.assertRaises(InviteError):
                self.store.decide_host(lobby_id, issued.invite_id, binding=wrong, approved=True)
        approved = self.store.decide_host(lobby_id, issued.invite_id, binding=BINDING, approved=True)
        self.assertEqual(approved["status"], "consent_required")
        self.now[0] += 100  # Redemption TTL is NOT the lifetime of consent or a grant.
        ready = self.store.consent_joining(lobby_id, issued.invite_id, binding=BINDING, consented=True)
        self.assertEqual(ready["status"], "ready")
        self.assertEqual(RoomInviteStore(self.path).get_redemption(lobby_id, issued.invite_id), ready)
        with self.assertRaises(InviteError):
            self.store.decide_host(lobby_id, issued.invite_id, binding=BINDING, approved=False)
        with self.assertRaises(InviteError):
            self.store.revoke_invite(lobby_id, issued.invite_id)  # Native revoke, NOT S1 revoke, after redemption.

    def test_denied_or_refused_consent_is_terminal(self):
        lobby_id = self.lobby["lobby_id"]
        for denial in ("host", "joining"):
            with self.subTest(denial=denial):
                issued = self.issue()
                self.store.redeem(lobby_id, issued.invite_id, issued.secret, binding=BINDING)
                self.store.decide_host(lobby_id, issued.invite_id, binding=BINDING, approved=denial != "host")
                if denial == "joining":
                    self.store.consent_joining(lobby_id, issued.invite_id, binding=BINDING, consented=False)
                with self.assertRaises(InviteError):
                    self.store.consent_joining(lobby_id, issued.invite_id, binding=BINDING, consented=True)

    def test_one_invitation_per_member_cannot_create_two_enrollments(self):
        first = self.issue()
        second = self.issue()  # Reissue invalidates still-open first invitation.
        with self.assertRaisesRegex(InviteError, "^invite_unavailable$"):
            self.store.redeem(self.lobby["lobby_id"], first.invite_id, first.secret, binding=BINDING)
        self.store.redeem(self.lobby["lobby_id"], second.invite_id, second.secret, binding=BINDING)
        with self.assertRaisesRegex(InviteError, "^member_already_redeemed$"):
            self.issue()


    def test_future_schema_is_read_only_and_never_downgraded(self):
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE schema_version SET version=999")
        before = self.path.read_bytes()
        future = RoomInviteStore(self.path)
        self.assertTrue(future.read_only)
        self.assertEqual(future.get_lobby(self.lobby["lobby_id"])["name"], "Known owners")
        with self.assertRaisesRegex(InviteError, "^schema_too_new$"):
            future.create_lobby(name="No downgrade", roster=ROSTER)
        self.assertEqual(self.path.read_bytes(), before)

    def test_lobby_fields_are_allowlisted_and_bounded_before_persistence(self):
        bad = [[], ROSTER * 4, [ROSTER[0], ROSTER[0]],
               [ROSTER[0], ROSTER[1] | {"grant": "never-store-grants"}],
               [ROSTER[0], ROSTER[1] | {"target": ROSTER[1]["target"] | {"url": "http://attacker.invalid"}}],
               [ROSTER[0], ROSTER[1] | {"handle": "bad\u202ehandle"}]]
        for roster in bad:
            with self.subTest(roster=roster), self.assertRaisesRegex(InviteError, "^invalid_lobby$"):
                self.store.create_lobby(name="Bad", roster=roster)
        for name in ("", "x" * 129, "Hidden\nname"):
            with self.subTest(name=name), self.assertRaises(InviteError):
                self.store.create_lobby(name=name, roster=ROSTER)
        with self.assertRaises(InviteError):
            self.store.issue_invite(self.lobby["lobby_id"], "peer-b", invitee_label="x" * 129)
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM lobbies").fetchone()[0], 1)

    def test_sealing_requires_both_consents_and_freezes_fixed_roster(self):
        lobby_id = self.lobby["lobby_id"]
        with self.assertRaisesRegex(InviteError, "^consent_required$"):
            self.store.seal_lobby(lobby_id)
        issued = self.issue()
        self.store.redeem(lobby_id, issued.invite_id, issued.secret, binding=BINDING)
        self.store.decide_host(lobby_id, issued.invite_id, binding=BINDING, approved=True)
        with self.assertRaisesRegex(InviteError, "^consent_required$"):
            self.store.seal_lobby(lobby_id)
        self.store.consent_joining(lobby_id, issued.invite_id, binding=BINDING, consented=True)
        sealed = self.store.seal_lobby(lobby_id)
        self.assertEqual(sealed["state"], "sealed")
        self.assertEqual(sealed["roster"], ROSTER)
        self.assertEqual(sealed["peers"][0]["fingerprint"], BINDING["fingerprint"])
        self.assertEqual(self.store.seal_lobby(lobby_id), sealed)
        with self.assertRaises(InviteError):
            self.issue()
        with self.assertRaises(InviteError):
            self.store.consent_joining(lobby_id, issued.invite_id, binding=BINDING, consented=False)
        self.assertEqual(self.store.get_lobby(lobby_id)["roster"], ROSTER)


    def test_concurrent_first_open_initializes_one_schema(self):
        from concurrent.futures import ThreadPoolExecutor
        import threading
        path = Path(self.tmp.name) / "first-open.sqlite"
        barrier = threading.Barrier(12)
        def open_store(_):
            barrier.wait(timeout=10)
            return RoomInviteStore(path).read_only
        with ThreadPoolExecutor(max_workers=12) as pool:
            self.assertEqual(list(pool.map(open_store, range(12))), [False] * 12)
        with sqlite3.connect(path) as conn:
            self.assertEqual(conn.execute("SELECT version FROM schema_version").fetchall(), [(2,)])

    def test_live_store_cannot_write_after_another_process_advances_schema(self):
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE schema_version SET version=3")
        with self.assertRaisesRegex(InviteError, "^schema_too_new$"):
            self.issue()

    def test_reserved_handles_duplicate_profiles_or_target_profile_mismatch_are_refused(self):
        for roster in (
            [ROSTER[0] | {"handle": "all"}, ROSTER[1]],
            [ROSTER[0], ROSTER[0] | {"member_id": "different", "handle": "other"}, ROSTER[1]],
            [ROSTER[0], ROSTER[1] | {"profile": "another"}],
            [ROSTER[0], ROSTER[1] | {"member_id": "LOCAL-A"}],
        ):
            with self.subTest(roster=roster), self.assertRaisesRegex(InviteError, "^invalid_lobby$"):
                self.store.create_lobby(name="Bad native roster", roster=roster)


if __name__ == "__main__":
    unittest.main()
