"""Joining-owner product boundary tests (MVP.2b-core) — NO HTTP, NO transport
promotion. Verifies: local-only invitation parsing (no network/navigation),
pre-approved-host-only resolution, non-consuming preview, idempotent/atomic
redeem, owner-confirmed-scope-gated signed consent, target-side mint scope
checks, durable schema-versioned joining-side state with 0700/0600
enforcement, and credential distinctness (invitation secret vs consent key
vs transport channel). One real-lab parity test drives the ACTUAL
spikes/invite-rooms control classes (imported only inside this test) against
this product implementation.
"""
from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import json
import os
import stat
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gateway.hosted_room_peer import catalog_mapping

from invite_rooms.consent import ConsentError, ConsentKey, verify as verify_consent
from invite_rooms.invitation import (
    ApprovedHostRegistry,
    InvitationError,
    parse_invitation,
)
from invite_rooms.joining import JoiningError, JoiningOwnerService
from invite_rooms.joining_store import JoiningStore, JoiningStoreError
from invite_rooms.joining_material import MaterialError, MintAmbiguous, TargetMaterialMint, validate_material
from room_invite_store import RoomInviteStore

HOST_INSTALL = "install:" + "a" * 32
SELF_INSTALL = "install:" + "b" * 32
FINGERPRINT = "f" * 64
ORIGIN = "https://peer-a.example"

_POLICY_UNSIGNED = {"version": 1, "target_profile": "default", "enabled_toolsets": ["bot_room"],
                     "approval_mode": "manual", "max_iterations": 10}
POLICY_DIGEST = hashlib.sha256(json.dumps(_POLICY_UNSIGNED, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
_POLICY = {**_POLICY_UNSIGNED, "policy_digest": POLICY_DIGEST}
CATALOG = catalog_mapping(
    installation_id=SELF_INSTALL, protocol_versions=(2,), link_modes=("direct",), persistent_process=True,
    text=True, attachments=False,
    endpoint={"available": True, "url": "http://127.0.0.1:19001/links/unit-link", "transport_security": "loopback"},
    target_profile="default", execution_policy=_POLICY,
)
CATALOG_DIGEST = CATALOG["catalog_digest"]
POLICY_DIGEST = CATALOG["execution_policy"]["policy_digest"]

SELF_BINDING = {
    "endpoint_id": "endpoint-b", "fingerprint": FINGERPRINT,
    "target_install_id": SELF_INSTALL, "target_profile": "default",
    "catalog_digest": CATALOG_DIGEST, "policy_digest": POLICY_DIGEST,
}

ROSTER = [
    {"member_id": "local-a", "profile": "default", "handle": "local"},
    {"member_id": "peer-b", "profile": "default", "handle": "peer",
     "target": {"kind": "peer", "peer_id": "host-a", "installation_id": SELF_INSTALL,
                "profile": "default", "capability_digest": CATALOG_DIGEST}},
]


def _invitation_text(lobby_id, invite_id, secret, *, origin=ORIGIN):
    return f"{origin}/invite/{lobby_id}/{invite_id}#{secret}"


class FakeHostChannel:
    """Test-only stand-in for the pinned control channel. Reuses the real
    RoomInviteStore and this package's own consent.verify to replicate
    exactly the request/response shapes the real HostLobbyControl exposes,
    without any TLS/relay transport. Labeled: never imported by product code."""

    def __init__(self, store, lobby_id, binding, *, joining_owner_key, host_id):
        self.store, self.lobby_id, self.binding = store, lobby_id, dict(binding)
        self.joining_owner_key, self.host_id = joining_owner_key, host_id
        self.calls = []
        self.material = None

    def call(self, request):
        self.calls.append(copy.deepcopy(request))
        op = request.get("op")
        if op == "preview":
            with self.store._connection() as conn:
                row = conn.execute(
                    "SELECT l.name,i.expires_at FROM invitations i JOIN lobbies l USING(lobby_id) "
                    "WHERE i.lobby_id=? AND invite_id=? AND i.state='open' AND i.expires_at>?",
                    (self.lobby_id, request["invite_id"], self.store.clock()),
                ).fetchone()
            if row is None:
                raise RuntimeError("invite_unavailable")
            return dict(row)
        if op == "redeem":
            result = self.store.redeem(self.lobby_id, request["invite_id"], request["secret"], binding=self.binding)
            return {"status": result["status"]}
        if op == "status":
            row = self.store.get_redemption(self.lobby_id, request["invite_id"])
            return {"status": row["status"], "scope": self._scope(request["invite_id"], row)}
        if op == "joining_consent":
            row = self.store.get_redemption(self.lobby_id, request["invite_id"])
            scope = self._scope(request["invite_id"], row)
            verify_consent(self.joining_owner_key, request["receipt"], request["signature"],
                            dict(scope, purpose="joining-owner-consent-v1"))
            if row["host_decision"] != "approved":
                raise RuntimeError("host_approval_required")
            self.store.consent_joining(self.lobby_id, request["invite_id"], binding=self.binding, consented=True)
            return {"status": "ready"}
        if op == "material":
            row = self.store.get_redemption(self.lobby_id, request["invite_id"])
            scope = self._scope(request["invite_id"], row)
            if row["status"] != "ready":
                raise RuntimeError("both_consents_required")
            envelope = request["envelope"]
            unsigned = {k: v for k, v in envelope.items() if k != "signature"}
            signed = dict(unsigned, purpose="joining-owner-material-v1")
            if envelope["scope"] != scope:
                raise RuntimeError("invalid_control")
            verify_consent(self.joining_owner_key, signed, envelope["signature"], signed)
            self.material = envelope
            return {"status": "material_ready"}
        raise RuntimeError("invalid_control")

    def _scope(self, invite_id, row):
        lobby = self.store.get_lobby(self.lobby_id)
        return {"lobby_id": self.lobby_id, "invite_id": invite_id, "room_id": lobby["planned_room_id"],
                "member_id": row["member_id"], "binding": self.binding, "host_id": self.host_id}


class DropReplyOnceChannel:
    """Wraps a real channel: forwards the request (so the underlying host
    effect REALLY happens), then raises once per matching op, simulating a
    reply that never reaches the caller. Labeled test-only; never imported
    by product code."""

    def __init__(self, inner, *, drop_op, times=1):
        self._inner = inner
        self._drop_op = drop_op
        self._remaining = times

    def call(self, request):
        result = self._inner.call(request)
        if request.get("op") == self._drop_op and self._remaining > 0:
            self._remaining -= 1
            raise RuntimeError("simulated_dropped_reply")
        return result

    @property
    def calls(self):
        return self._inner.calls

    @property
    def material(self):
        return self._inner.material


def make_fixture(tmp_root):
    store = RoomInviteStore(tmp_root / "host.sqlite")
    lobby = store.create_lobby(name="Joining lab", roster=copy.deepcopy(ROSTER))
    invite = store.issue_invite(lobby["lobby_id"], "peer-b", invitee_label="Known owner")
    consent_key = ConsentKey(tmp_root / "consent")
    registry = ApprovedHostRegistry()
    registry.register(peer_id="host-a", installation_id=HOST_INSTALL, profile="default",
                       endpoint_id="endpoint-b", fingerprint=FINGERPRINT, policy_digest=POLICY_DIGEST,
                       catalog_digest=CATALOG_DIGEST, consent_public_key=consent_key.public_bytes,
                       pairing_generation=1)
    channel = FakeHostChannel(store, lobby["lobby_id"], SELF_BINDING,
                               joining_owner_key=consent_key.public_bytes, host_id=HOST_INSTALL)
    joining_store = JoiningStore(tmp_root / "joining")
    service = JoiningOwnerService(joining_store, registry, host_peer_id="host-a", origin=ORIGIN,
                                   consent_key=consent_key, channel=channel, self_binding=SELF_BINDING)
    return {
        "store": store, "lobby": lobby, "invite": invite, "registry": registry,
        "consent_key": consent_key, "channel": channel, "joining_store": joining_store, "service": service,
    }


class ParseInvitationTests(unittest.TestCase):
    def test_valid_parse(self):
        secret = "x" * 43
        parsed = parse_invitation(_invitation_text("lobby-a", "invite-b", secret), origin=ORIGIN)
        self.assertEqual((parsed.lobby_id, parsed.invite_id, parsed.secret), ("lobby-a", "invite-b", secret))

    def test_secret_never_in_repr(self):
        secret = "x" * 43
        parsed = parse_invitation(_invitation_text("lobby-a", "invite-b", secret), origin=ORIGIN)
        self.assertNotIn(secret, repr(parsed))

    def test_foreign_origin_rejected(self):
        secret = "x" * 43
        with self.assertRaisesRegex(InvitationError, "^invalid_invitation$"):
            parse_invitation("https://evil.example/invite/lobby-a/invite-b#" + secret, origin=ORIGIN)

    def test_query_string_rejected(self):
        secret = "x" * 43
        url = f"{ORIGIN}/invite/lobby-a/invite-b?x=1#" + secret
        with self.assertRaisesRegex(InvitationError, "^invalid_invitation$"):
            parse_invitation(url, origin=ORIGIN)

    def test_userinfo_rejected(self):
        secret = "x" * 43
        url = f"https://user@peer-a.example/invite/lobby-a/invite-b#" + secret
        with self.assertRaisesRegex(InvitationError, "^invalid_invitation$"):
            parse_invitation(url, origin=ORIGIN)

    def test_control_chars_rejected(self):
        with self.assertRaisesRegex(InvitationError, "^invalid_invitation$"):
            parse_invitation(_invitation_text("lobby-a", "invite-b", "x" * 43) + "\n", origin=ORIGIN)

    def test_wrong_secret_length_rejected(self):
        with self.assertRaisesRegex(InvitationError, "^invalid_invitation$"):
            parse_invitation(_invitation_text("lobby-a", "invite-b", "x" * 42), origin=ORIGIN)

    def test_oversized_text_rejected(self):
        with self.assertRaisesRegex(InvitationError, "^invalid_invitation$"):
            parse_invitation("https://peer-a.example/" + "a" * 600, origin=ORIGIN)


class ApprovedHostRegistryTests(unittest.TestCase):
    def test_unknown_host_rejected(self):
        reg = ApprovedHostRegistry()
        with self.assertRaisesRegex(InvitationError, "^unknown_host$"):
            reg.binding_for("nope")

    def test_duplicate_registration_rejected(self):
        reg = ApprovedHostRegistry()
        reg.register(peer_id="host-a", installation_id=HOST_INSTALL, profile="default",
                      endpoint_id="endpoint-b", fingerprint=FINGERPRINT, policy_digest=POLICY_DIGEST,
                      catalog_digest=CATALOG_DIGEST, consent_public_key=bytes(32), pairing_generation=1)
        with self.assertRaisesRegex(InvitationError, "^host_already_registered$"):
            reg.register(peer_id="host-a", installation_id=HOST_INSTALL, profile="default",
                          endpoint_id="endpoint-b", fingerprint=FINGERPRINT, policy_digest=POLICY_DIGEST,
                      catalog_digest=CATALOG_DIGEST, consent_public_key=bytes(32), pairing_generation=1)

    def test_binding_is_a_copy(self):
        reg = ApprovedHostRegistry()
        reg.register(peer_id="host-a", installation_id=HOST_INSTALL, profile="default",
                      endpoint_id="endpoint-b", fingerprint=FINGERPRINT, policy_digest=POLICY_DIGEST,
                      catalog_digest=CATALOG_DIGEST, consent_public_key=bytes(32), pairing_generation=1)
        b1 = reg.binding_for("host-a")
        b2 = reg.binding_for("host-a")
        self.assertEqual(b1, b2)


class ConsentKeyTests(unittest.TestCase):
    def test_pre_refactor_receipt_fixture_remains_verifiable(self):
        fixture = json.loads((Path(__file__).parent / "fixtures" / "invite_consent_legacy.json").read_text())
        verify_consent(base64.b64decode(fixture["public_key"]), fixture["receipt"],
                       fixture["signature"], fixture["receipt"])

    def test_sign_and_verify(self):
        with tempfile.TemporaryDirectory() as tmp:
            key = ConsentKey(Path(tmp) / "k")
            receipt = {"a": 1}
            sig = key.sign(receipt)
            verify_consent(key.public_bytes, receipt, sig, receipt)

    def test_tampered_receipt_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            key = ConsentKey(Path(tmp) / "k")
            receipt = {"a": 1}
            sig = key.sign(receipt)
            with self.assertRaisesRegex(ConsentError, "^invalid_consent$"):
                verify_consent(key.public_bytes, receipt, sig, {"a": 2})

    def test_wrong_key_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            key = ConsentKey(Path(tmp) / "k1")
            other = ConsentKey(Path(tmp) / "k2")
            receipt = {"a": 1}
            sig = key.sign(receipt)
            with self.assertRaisesRegex(ConsentError, "^invalid_consent$"):
                verify_consent(other.public_bytes, receipt, sig, receipt)

    def test_persists_across_instances(self):
        with tempfile.TemporaryDirectory() as tmp:
            k1 = ConsentKey(Path(tmp) / "k")
            k2 = ConsentKey(Path(tmp) / "k")
            self.assertEqual(k1.public_bytes, k2.public_bytes)

    def test_wrong_file_perms_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "k"
            ConsentKey(root)
            os.chmod(root / "consent.key", 0o644)
            with self.assertRaisesRegex(ConsentError, "^private_storage_required$"):
                ConsentKey(root)

    def test_existing_parent_with_wrong_mode_rejected_unchanged(self):
        """R6: a pre-existing parent dir with a non-private mode is rejected,
        never silently chmod'd to 0700."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "k"
            root.mkdir(mode=0o755)
            os.chmod(root, 0o755)
            with self.assertRaisesRegex(ConsentError, "^private_storage_required$"):
                ConsentKey(root)
            self.assertEqual(stat.S_IMODE(os.stat(root).st_mode), 0o755)


class JoiningStoreTests(unittest.TestCase):
    def test_schema_version_enforced(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "s"
            JoiningStore(root)  # creates version 1
            # Simulate an unknown future schema version.
            conn = sqlite3.connect(root / "joining.sqlite")
            conn.execute("UPDATE schema_version SET version=99")
            conn.commit()
            conn.close()
            with self.assertRaisesRegex(JoiningStoreError, "^schema_version_unsupported$"):
                JoiningStore(root)

    def test_write_rechecks_schema_version_on_already_open_object(self):
        """R5: an object opened BEFORE an external version bump must still
        refuse to write, not just a freshly constructed one."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "s"
            store = JoiningStore(root)
            import_id = store.create_import(
                lobby_id="lobby-x", invite_id="invite-y", secret="s" * 43, host_peer_id="host-a"
            )
            conn = sqlite3.connect(root / "joining.sqlite")
            conn.execute("UPDATE schema_version SET version=99")
            conn.commit()
            conn.close()
            with self.assertRaisesRegex(JoiningStoreError, "^schema_version_unsupported$"):
                store.mark_redeemed(import_id)

    def test_unversioned_nonempty_db_rejected_not_adopted(self):
        """R5: a DB with OTHER tables but no schema_version must be rejected
        outright, never silently adopted as a fresh version-1 store, and the
        original bytes/tables must be preserved."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "s"
            root.mkdir(mode=0o700)
            db_path = root / "joining.sqlite"
            conn = sqlite3.connect(db_path)
            conn.execute("CREATE TABLE some_other_layout(x INTEGER)")
            conn.execute("INSERT INTO some_other_layout VALUES (42)")
            conn.commit()
            conn.close()
            os.chmod(db_path, 0o600)
            before = db_path.read_bytes()
            with self.assertRaisesRegex(JoiningStoreError, "^schema_version_unsupported$"):
                JoiningStore(root)
            self.assertEqual(db_path.read_bytes(), before)

    def test_db_file_perms_are_0600(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "s"
            store = JoiningStore(root)
            self.assertEqual(stat.S_IMODE(os.stat(store.path).st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(os.stat(root / "joining-secret.key").st_mode), 0o600)

    def test_create_import_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JoiningStore(Path(tmp) / "s")
            i1 = store.create_import(lobby_id="lobby-x", invite_id="invite-y", secret="s" * 43, host_peer_id="host-a")
            i2 = store.create_import(lobby_id="lobby-x", invite_id="invite-y", secret="s" * 43, host_peer_id="host-a")
            self.assertEqual(i1, i2)

    def test_secret_roundtrip_and_not_in_repr(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JoiningStore(Path(tmp) / "s")
            secret = "s" * 43
            import_id = store.create_import(lobby_id="lobby-x", invite_id="invite-y", secret=secret, host_peer_id="host-a")
            self.assertEqual(store.load_secret(import_id), secret)
            view = store.get_import(import_id)
            self.assertNotIn(secret, repr(view))
            self.assertNotIn(secret, str(view))
            stored_bytes = (Path(tmp) / "s" / "joining.sqlite").read_bytes()
            self.assertNotIn(secret.encode(), stored_bytes)

    def test_confirm_scope_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JoiningStore(Path(tmp) / "s")
            import_id = store.create_import(lobby_id="lobby-x", invite_id="invite-y", secret="s" * 43, host_peer_id="host-a")
            store.confirm_scope(import_id, {"room_id": "invite-room-1"})
            with self.assertRaisesRegex(JoiningStoreError, "^consent_scope_conflict$"):
                store.confirm_scope(import_id, {"room_id": "invite-room-2"})

    def test_record_consent_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JoiningStore(Path(tmp) / "s")
            import_id = store.create_import(lobby_id="lobby-x", invite_id="invite-y", secret="s" * 43, host_peer_id="host-a")
            store.begin_redeem(import_id)
            store.mark_redeemed(import_id)
            store.begin_consent(import_id, receipt={"a": 1}, signature="sig-1")
            with self.assertRaisesRegex(JoiningStoreError, "^consent_scope_conflict$"):
                store.begin_consent(import_id, receipt={"a": 2}, signature="sig-2")

    def test_mint_journal_ambiguous_on_duplicate_begin(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JoiningStore(Path(tmp) / "s")
            store.mint_begin("scope-1")
            with self.assertRaisesRegex(JoiningStoreError, "^mint_ambiguous$"):
                store.mint_begin("scope-1")

    def test_mint_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JoiningStore(Path(tmp) / "s")
            store.mint_begin("scope-1")
            store.mint_complete("scope-1", b'{"x":1}')
            self.assertEqual(store.mint_load("scope-1"), b'{"x":1}')

    def test_existing_parent_with_wrong_mode_rejected_unchanged(self):
        """R6: a pre-existing parent dir with a non-private mode is rejected,
        never silently chmod'd to 0700."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "s"
            root.mkdir(mode=0o755)
            os.chmod(root, 0o755)  # mkdir mode is subject to umask; force it
            with self.assertRaisesRegex(JoiningStoreError, "^private_storage_required$"):
                JoiningStore(root)
            self.assertEqual(stat.S_IMODE(os.stat(root).st_mode), 0o755)

    def test_secret_deleted_after_confirmed_redeem(self):
        """R9: the encrypted secret is deleted in the SAME transaction as a
        confirmed redeem; load_secret fails after success."""
        with tempfile.TemporaryDirectory() as tmp:
            store = JoiningStore(Path(tmp) / "s")
            import_id = store.create_import(lobby_id="lobby-x", invite_id="invite-y", secret="s" * 43, host_peer_id="host-a")
            store.begin_redeem(import_id)
            store.mark_redeemed(import_id)
            with self.assertRaisesRegex(JoiningStoreError, "^import_unavailable$"):
                store.load_secret(import_id)

    def test_redeem_ambiguous_then_owner_resolves_without_resend(self):
        """R4: a lost redeem reply becomes visible as redeem_ambiguous; the
        only recovery is the explicit resolve_redeem_ambiguous owner action,
        never an automatic retry."""
        with tempfile.TemporaryDirectory() as tmp:
            store = JoiningStore(Path(tmp) / "s")
            import_id = store.create_import(lobby_id="lobby-x", invite_id="invite-y", secret="s" * 43, host_peer_id="host-a")
            store.begin_redeem(import_id)
            store.mark_redeem_ambiguous(import_id)
            self.assertEqual(store.get_import(import_id)["state"], "redeem_ambiguous")
            store.resolve_redeem_ambiguous(import_id, redeemed=True)
            self.assertEqual(store.get_import(import_id)["state"], "redeemed")
            with self.assertRaisesRegex(JoiningStoreError, "^import_unavailable$"):
                store.load_secret(import_id)  # R9: deleted on confirmed redeem, even via recovery

    def test_begin_consent_persists_before_delivery_and_reuses_bytes(self):
        """R3: begin_consent persists receipt+signature BEFORE any send and
        moves state to consent_pending_delivery; a retry reuses the exact
        same bytes, and a restart (new Store instance) sees the same thing."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "s"
            store = JoiningStore(root)
            import_id = store.create_import(lobby_id="lobby-x", invite_id="invite-y", secret="s" * 43, host_peer_id="host-a")
            store.begin_redeem(import_id)
            store.mark_redeemed(import_id)
            receipt = {"a": 1}
            persisted = store.begin_consent(import_id, receipt=receipt, signature="sig-1")
            self.assertEqual(store.get_import(import_id)["state"], "consent_pending_delivery")
            # Retry before any ack is observed: same bytes, no re-signing.
            again = store.begin_consent(import_id, receipt=receipt, signature="sig-1")
            self.assertEqual(again["signature"], persisted["signature"])
            # Restart: a fresh Store instance over the same root sees the
            # exact same durable receipt.
            restarted = JoiningStore(root)
            self.assertEqual(restarted.get_consent(import_id), {"receipt": receipt, "signature": "sig-1"})
            # A transmitted/persisted receipt can never become a local refusal.
            with self.assertRaisesRegex(JoiningStoreError, "^import_state_conflict$"):
                restarted.mark_refused(import_id)

    def test_mark_consent_delivered_requires_prior_begin(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JoiningStore(Path(tmp) / "s")
            import_id = store.create_import(lobby_id="lobby-x", invite_id="invite-y", secret="s" * 43, host_peer_id="host-a")
            with self.assertRaisesRegex(JoiningStoreError, "^import_state_conflict$"):
                store.mark_consent_delivered(import_id)


class JoiningOwnerServiceTests(unittest.TestCase):
    def test_unapproved_host_rejected_at_construction(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = RoomInviteStore(root / "host.sqlite")
            registry = ApprovedHostRegistry()
            consent_key = ConsentKey(root / "consent")
            channel = FakeHostChannel(store, "lobby-x", SELF_BINDING, joining_owner_key=consent_key.public_bytes, host_id=HOST_INSTALL)
            joining_store = JoiningStore(root / "joining")
            with self.assertRaisesRegex(InvitationError, "^unknown_host$"):
                JoiningOwnerService(joining_store, registry, host_peer_id="host-a", origin=ORIGIN,
                                    consent_key=consent_key, channel=channel, self_binding=SELF_BINDING)

    def test_full_happy_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = make_fixture(Path(tmp))
            text = _invitation_text(fx["lobby"]["lobby_id"], fx["invite"].invite_id, fx["invite"].secret)
            preview = fx["service"].preview(text)
            self.assertNotIn(fx["invite"].secret, json.dumps(preview))
            metadata = fx["service"].refresh_metadata(preview["import_id"])
            self.assertEqual(metadata["name"], "Joining lab")
            redeemed = fx["service"].redeem(preview["import_id"])
            self.assertEqual(redeemed["status"], "awaiting_host_approval")
            # Host approves (this is the host's own local operation, not in this packet's scope).
            fx["store"].decide_host(fx["lobby"]["lobby_id"], fx["invite"].invite_id, binding=SELF_BINDING, approved=True)
            fx["service"].confirm_scope(preview["import_id"], room_id=fx["lobby"]["planned_room_id"], member_id="peer-b")
            result = fx["service"].consent(preview["import_id"], approve=True)
            self.assertEqual(result["status"], "ready")
            self.assertEqual(fx["store"].get_redemption(fx["lobby"]["lobby_id"], fx["invite"].invite_id)["status"], "ready")

    def test_consent_before_host_approval_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = make_fixture(Path(tmp))
            text = _invitation_text(fx["lobby"]["lobby_id"], fx["invite"].invite_id, fx["invite"].secret)
            preview = fx["service"].preview(text)
            fx["service"].redeem(preview["import_id"])
            fx["service"].confirm_scope(preview["import_id"], room_id=fx["lobby"]["planned_room_id"], member_id="peer-b")
            with self.assertRaisesRegex(JoiningError, "^host_approval_required$"):
                fx["service"].consent(preview["import_id"], approve=True)

    def test_consent_for_different_binding_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = make_fixture(Path(tmp))
            text = _invitation_text(fx["lobby"]["lobby_id"], fx["invite"].invite_id, fx["invite"].secret)
            preview = fx["service"].preview(text)
            fx["service"].redeem(preview["import_id"])
            fx["store"].decide_host(fx["lobby"]["lobby_id"], fx["invite"].invite_id, binding=SELF_BINDING, approved=True)
            # Owner confirms the WRONG member_id for this import.
            fx["service"].confirm_scope(preview["import_id"], room_id=fx["lobby"]["planned_room_id"], member_id="wrong-member")
            with self.assertRaisesRegex(JoiningError, "^consent_scope_conflict$"):
                fx["service"].consent(preview["import_id"], approve=True)

    def test_tampered_policy_digest_breaks_scope_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = make_fixture(Path(tmp))
            text = _invitation_text(fx["lobby"]["lobby_id"], fx["invite"].invite_id, fx["invite"].secret)
            preview = fx["service"].preview(text)
            fx["service"].redeem(preview["import_id"])
            fx["store"].decide_host(fx["lobby"]["lobby_id"], fx["invite"].invite_id, binding=SELF_BINDING, approved=True)
            fx["service"].confirm_scope(preview["import_id"], room_id=fx["lobby"]["planned_room_id"], member_id="peer-b")
            # Tamper the channel's recorded binding AFTER redemption, simulating
            # a host-side policy/binding change: the status scope no longer
            # matches what the owner confirmed.
            fx["channel"].binding = dict(SELF_BINDING, policy_digest="0" * 64)
            with self.assertRaisesRegex(JoiningError, "^consent_scope_conflict$"):
                fx["service"].consent(preview["import_id"], approve=True)

    def test_replayed_redeem_is_idempotent_and_single_network_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = make_fixture(Path(tmp))
            text = _invitation_text(fx["lobby"]["lobby_id"], fx["invite"].invite_id, fx["invite"].secret)
            preview = fx["service"].preview(text)
            fx["service"].redeem(preview["import_id"])
            redeem_calls_before = sum(1 for c in fx["channel"].calls if c["op"] == "redeem")
            second = fx["service"].redeem(preview["import_id"])
            redeem_calls_after = sum(1 for c in fx["channel"].calls if c["op"] == "redeem")
            self.assertEqual(second["status"], "already_redeemed")
            self.assertEqual(redeem_calls_before, redeem_calls_after)

    def test_refusal_is_local_only_no_remote_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = make_fixture(Path(tmp))
            text = _invitation_text(fx["lobby"]["lobby_id"], fx["invite"].invite_id, fx["invite"].secret)
            preview = fx["service"].preview(text)
            fx["service"].redeem(preview["import_id"])
            calls_before = len(fx["channel"].calls)
            result = fx["service"].consent(preview["import_id"], approve=False)
            self.assertEqual(result["status"], "refused")
            self.assertEqual(len(fx["channel"].calls), calls_before)

    def test_consent_without_confirm_scope_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = make_fixture(Path(tmp))
            text = _invitation_text(fx["lobby"]["lobby_id"], fx["invite"].invite_id, fx["invite"].secret)
            preview = fx["service"].preview(text)
            fx["service"].redeem(preview["import_id"])
            fx["store"].decide_host(fx["lobby"]["lobby_id"], fx["invite"].invite_id, binding=SELF_BINDING, approved=True)
            with self.assertRaisesRegex(JoiningError, "^owner_scope_required$"):
                fx["service"].consent(preview["import_id"], approve=True)

    def test_consent_private_key_never_in_exceptions_or_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = make_fixture(Path(tmp))
            key_bytes = (Path(tmp) / "consent" / "consent.key").read_bytes()
            text = _invitation_text(fx["lobby"]["lobby_id"], fx["invite"].invite_id, fx["invite"].secret)
            preview = fx["service"].preview(text)
            fx["service"].redeem(preview["import_id"])
            with self.assertRaises(JoiningError) as ctx:
                fx["service"].consent(preview["import_id"], approve=True)
            self.assertNotIn(key_bytes, str(ctx.exception).encode())

    def test_credentials_are_distinct(self):
        """Invitation secret, consent signing key and transport auth never
        share storage or derivation: compromising one file does not expose
        the others."""
        with tempfile.TemporaryDirectory() as tmp:
            fx = make_fixture(Path(tmp))
            text = _invitation_text(fx["lobby"]["lobby_id"], fx["invite"].invite_id, fx["invite"].secret)
            preview = fx["service"].preview(text)
            secret_key_bytes = (Path(tmp) / "joining" / "joining-secret.key").read_bytes()
            consent_key_bytes = (Path(tmp) / "consent" / "consent.key").read_bytes()
            self.assertNotEqual(secret_key_bytes, consent_key_bytes)
            # The transport/control channel object carries no key material at all.
            self.assertFalse(hasattr(fx["channel"], "private_key"))
            self.assertFalse(hasattr(fx["channel"], "key"))

    def test_preview_makes_zero_channel_calls(self):
        """R8: preview() is local-only — parse + registry check + encrypted
        persist, ZERO channel calls. Metadata comes only from the separate,
        explicit refresh_metadata() action."""
        with tempfile.TemporaryDirectory() as tmp:
            fx = make_fixture(Path(tmp))
            text = _invitation_text(fx["lobby"]["lobby_id"], fx["invite"].invite_id, fx["invite"].secret)
            preview = fx["service"].preview(text)
            self.assertEqual(fx["channel"].calls, [])
            self.assertEqual(set(preview), {"import_id", "lobby_id", "invite_id", "host"})
            metadata = fx["service"].refresh_metadata(preview["import_id"])
            self.assertEqual(len(fx["channel"].calls), 1)
            self.assertEqual(fx["channel"].calls[0]["op"], "preview")
            self.assertEqual(metadata["name"], "Joining lab")

    def test_lost_redeem_reply_becomes_ambiguous_then_owner_resolves(self):
        """R4: the host really redeems the invite, but the reply is lost. A
        retry must NOT blindly resend the secret; the import is visibly
        redeem_ambiguous, and the only recovery is the explicit owner action."""
        with tempfile.TemporaryDirectory() as tmp:
            fx = make_fixture(Path(tmp))
            flaky = DropReplyOnceChannel(fx["channel"], drop_op="redeem")
            fx["service"]._channel = flaky
            text = _invitation_text(fx["lobby"]["lobby_id"], fx["invite"].invite_id, fx["invite"].secret)
            preview = fx["service"].preview(text)
            with self.assertRaisesRegex(JoiningError, "^control_unavailable$"):
                fx["service"].redeem(preview["import_id"])
            # The host-side effect DID happen.
            self.assertEqual(
                fx["store"].get_redemption(fx["lobby"]["lobby_id"], fx["invite"].invite_id)["status"],
                "awaiting_host_approval",
            )
            # A second call never resends the secret; it stays ambiguous.
            with self.assertRaisesRegex(JoiningError, "^redeem_ambiguous$"):
                fx["service"].redeem(preview["import_id"])
            redeem_calls = [c for c in fx["channel"].calls if c["op"] == "redeem"]
            self.assertEqual(len(redeem_calls), 1)
            # Explicit owner-directed recovery after an authenticated readback.
            resolved = fx["service"].resolve_redeem_ambiguous(preview["import_id"], confirmed_redeemed=True)
            self.assertEqual(resolved["status"], "redeemed")
            with self.assertRaisesRegex(JoiningStoreError, "^import_unavailable$"):
                fx["joining_store"].load_secret(preview["import_id"])  # R9

    def test_lost_consent_ack_persists_before_send_retry_reuses_bytes_refusal_rejected(self):
        """R3: receipt+signature are durable BEFORE the send. A lost ack
        leaves state consent_pending_delivery (not refused-able); a retry
        reuses the exact same signature and eventually completes."""
        with tempfile.TemporaryDirectory() as tmp:
            fx = make_fixture(Path(tmp))
            flaky = DropReplyOnceChannel(fx["channel"], drop_op="joining_consent")
            fx["service"]._channel = flaky
            text = _invitation_text(fx["lobby"]["lobby_id"], fx["invite"].invite_id, fx["invite"].secret)
            preview = fx["service"].preview(text)
            fx["service"].redeem(preview["import_id"])
            fx["store"].decide_host(fx["lobby"]["lobby_id"], fx["invite"].invite_id, binding=SELF_BINDING, approved=True)
            fx["service"].confirm_scope(preview["import_id"], room_id=fx["lobby"]["planned_room_id"], member_id="peer-b")
            with self.assertRaisesRegex(JoiningError, "^control_unavailable$"):
                fx["service"].consent(preview["import_id"], approve=True)
            persisted = fx["joining_store"].get_consent(preview["import_id"])
            self.assertIsNotNone(persisted)
            self.assertEqual(fx["joining_store"].get_import(preview["import_id"])["state"], "consent_pending_delivery")
            # A transmitted/persisted receipt can never become a local refusal.
            with self.assertRaisesRegex(JoiningError, "^import_state_conflict$"):
                fx["service"].consent(preview["import_id"], approve=False)
            # Retry reuses the exact same bytes (no re-signing) and completes.
            result = fx["service"].consent(preview["import_id"], approve=True)
            self.assertEqual(result["status"], "ready")
            retried = fx["joining_store"].get_consent(preview["import_id"])
            self.assertEqual(retried["signature"], persisted["signature"])
            self.assertEqual(fx["joining_store"].get_import(preview["import_id"])["state"], "consent_given")


class TargetMaterialMintTests(unittest.TestCase):
    def test_validate_material_rejects_zero_digest_binding(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = self._approved_fixture(Path(tmp))
            scope = {
                "room_id": fx["lobby"]["planned_room_id"], "host_id": HOST_INSTALL,
                "member_id": "peer-b", "binding": dict(SELF_BINDING,
                    catalog_digest="0" * 64, policy_digest="0" * 64),
            }
            envelope = {"scope": scope, "grant": self._fake_grant(scope),
                        "catalog": CATALOG, "signature": "0" * 43}
            with self.assertRaisesRegex(MaterialError, "^material_unavailable$"):
                validate_material(scope, envelope)

    def _approved_fixture(self, tmp):
        fx = make_fixture(tmp)
        text = _invitation_text(fx["lobby"]["lobby_id"], fx["invite"].invite_id, fx["invite"].secret)
        preview = fx["service"].preview(text)
        fx["service"].redeem(preview["import_id"])
        fx["store"].decide_host(fx["lobby"]["lobby_id"], fx["invite"].invite_id, binding=SELF_BINDING, approved=True)
        fx["service"].confirm_scope(preview["import_id"], room_id=fx["lobby"]["planned_room_id"], member_id="peer-b")
        fx["service"].consent(preview["import_id"], approve=True)
        fx["payload"] = {"lobby_id": fx["lobby"]["lobby_id"], "invite_id": fx["invite"].invite_id}
        return fx

    def _fake_grant(self, scope):
        claims = {
            "room_id": scope["room_id"], "home_install_id": scope["host_id"],
            "authority_gateway_id": scope["host_id"], "authority_epoch": 1,
            "member_id": scope["member_id"], "target_install_id": scope["binding"]["target_install_id"],
            "target_profile": scope["binding"]["target_profile"],
            "issued_at": time.time(), "expires_at": time.time() + 600,
            "status_expires_at": time.time() + 600, "execution_policy_digest": POLICY_DIGEST,
        }
        payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
        signature = base64.urlsafe_b64encode(b"0" * 32).rstrip(b"=").decode()
        return f"{payload}.{signature}"

    def test_wrong_scope_payload_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = self._approved_fixture(Path(tmp))
            mint = TargetMaterialMint(fx["joining_store"], consent_key=fx["consent_key"], channel=fx["channel"],
                                       rpc=None, host_peer_id="host-a")
            with self.assertRaisesRegex(MaterialError, "^invalid_owner_scope$"):
                mint.approved_scope({"lobby_id": "lobby-nope", "invite_id": "invite-nope"})

    def test_mint_rejects_endpoint_as_url_string_like_old_native_bug(self):
        """R1 regression guard: the OLD code compared result['endpoint'] to
        result['catalog']['endpoint']['url'] (a string); the REAL native
        contract returns the same mapping at both places. A stub that
        reproduces the old buggy shape must still be rejected (ambiguous),
        proving we no longer accept the wrong shape either way."""
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                fx = self._approved_fixture(Path(tmp))

                async def rpc(method, params):
                    scope = mint.approved_scope(fx["payload"])
                    return {"target_profile": params["profile"],
                            "endpoint": CATALOG["endpoint"]["url"],  # wrong: bare string, not the mapping
                            "catalog": CATALOG, "grant": self._fake_grant(scope)}

                mint = TargetMaterialMint(fx["joining_store"], consent_key=fx["consent_key"], channel=fx["channel"],
                                           rpc=rpc, host_peer_id="host-a")
                with self.assertRaises(MintAmbiguous):
                    await mint.mint(fx["payload"])
                self.assertIsNone(fx["channel"].material)

        asyncio.run(run())

    def test_mint_rejects_policy_catalog_drift_from_owner_confirmed_digests(self):
        """R2: a catalog+policy that changed TOGETHER, consistently (so the
        native catalog_digest/policy_digest self-check still passes), must
        still be rejected because it no longer matches the owner-confirmed
        digests pinned in self_binding. Nothing gets signed or delivered."""
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                fx = self._approved_fixture(Path(tmp))
                drifted_policy_unsigned = {**_POLICY_UNSIGNED, "max_iterations": 11}
                drifted_policy_digest = hashlib.sha256(
                    json.dumps(drifted_policy_unsigned, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
                drifted_policy = {**drifted_policy_unsigned, "policy_digest": drifted_policy_digest}
                drifted_catalog = catalog_mapping(
                    installation_id=SELF_INSTALL, protocol_versions=(2,), link_modes=("direct",),
                    persistent_process=True, text=True, attachments=False,
                    endpoint={"available": True, "url": "http://127.0.0.1:19001/links/unit-link",
                              "transport_security": "loopback"},
                    target_profile="default", execution_policy=drifted_policy,
                )

                async def rpc(method, params):
                    scope = mint.approved_scope(fx["payload"])
                    # The grant's OWN claims are internally consistent with
                    # the drifted policy (so only the owner-confirmed-digest
                    # pin in self_binding can catch this, not the pre-existing
                    # claims-vs-catalog check).
                    claims = {
                        "room_id": scope["room_id"], "home_install_id": scope["host_id"],
                        "authority_gateway_id": scope["host_id"], "authority_epoch": 1,
                        "member_id": scope["member_id"], "target_install_id": scope["binding"]["target_install_id"],
                        "target_profile": scope["binding"]["target_profile"],
                        "issued_at": time.time(), "expires_at": time.time() + 600,
                        "status_expires_at": time.time() + 600, "execution_policy_digest": drifted_policy_digest,
                    }
                    payload_b64 = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
                    signature_b64 = base64.urlsafe_b64encode(b"0" * 32).rstrip(b"=").decode()
                    grant = f"{payload_b64}.{signature_b64}"
                    return {"target_profile": params["profile"], "endpoint": drifted_catalog["endpoint"],
                            "catalog": drifted_catalog, "grant": grant}

                mint = TargetMaterialMint(fx["joining_store"], consent_key=fx["consent_key"], channel=fx["channel"],
                                           rpc=rpc, host_peer_id="host-a")
                with self.assertRaises(MintAmbiguous):
                    await mint.mint(fx["payload"])
                self.assertIsNone(fx["channel"].material)

        asyncio.run(run())

    def test_mint_full_round_trip(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                fx = self._approved_fixture(Path(tmp))

                async def rpc(method, params):
                    self.assertEqual(method, "groups.peer.invite")
                    scope = mint.approved_scope(fx["payload"])
                    # Matches the REAL native contract exactly
                    # (tui_gateway/methods_groups.py:275-277):
                    # `"endpoint": catalog["endpoint"]` — the SAME mapping,
                    # never a bare URL string (R1 parity fixture).
                    return {"target_profile": params["profile"], "endpoint": CATALOG["endpoint"],
                            "catalog": CATALOG, "grant": self._fake_grant(scope)}

                mint = TargetMaterialMint(fx["joining_store"], consent_key=fx["consent_key"], channel=fx["channel"],
                                           rpc=rpc, host_peer_id="host-a")
                result = await mint.mint(fx["payload"])
                self.assertEqual(result["status"], "material_ready")
                self.assertIsNotNone(fx["channel"].material)

        asyncio.run(run())

    def test_mint_ambiguous_on_crash_recovery_never_remints(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                fx = self._approved_fixture(Path(tmp))

                async def rpc(method, params):
                    raise RuntimeError("simulated crash mid-mint")

                mint = TargetMaterialMint(fx["joining_store"], consent_key=fx["consent_key"], channel=fx["channel"],
                                           rpc=rpc, host_peer_id="host-a")
                with self.assertRaises(MintAmbiguous):
                    await mint.mint(fx["payload"])
                # A retry after the crash stays blocked; it never mints again
                # with a fresh grant_id.
                with self.assertRaises(MintAmbiguous):
                    await mint.mint(fx["payload"])

        asyncio.run(run())


class RealLabParityTest(unittest.TestCase):
    """The ONE test allowed to drive the real lab transport (tests may import
    spikes via explicit sys.path inside the test only; product code may not).
    Proves JoiningOwnerService's consent protocol interoperates with the
    ACTUAL accepted lobby_control.HostLobbyControl/PinnedControl classes."""

    def test_real_pinned_control_accepts_product_consent(self):
        spikes_dir = Path(__file__).resolve().parents[2] / "spikes" / "invite-rooms"
        sys.path.insert(0, str(spikes_dir))
        import preflight
        # The lab refuses to run outside its approved runtime root and managed Hermes
        # interpreter (by design), so this parity proof only runs on a provisioned lab host.
        if not (preflight.APPROVED_RUNTIME_ROOT.exists() and preflight.MANAGED_PYTHON.exists()):
            self.skipTest("invite-rooms lab is not provisioned on this host")
        from lobby_control_lab import ControlLab
        import lobby_control as lab_control

        with tempfile.TemporaryDirectory(prefix="mvp2b-parity-") as tmp:
            tmp = Path(tmp)
            with ControlLab(tmp) as lab:
                store = RoomInviteStore(tmp / "host.sqlite")
                lobby = store.create_lobby(name="Parity lab", roster=lab.roster)
                invite = store.issue_invite(lobby["lobby_id"], "peer-b", invitee_label="Known owner")

                parity_catalog = catalog_mapping(
                    installation_id=lab.target_id, protocol_versions=(2,), link_modes=("direct",),
                    persistent_process=True, text=True, attachments=False,
                    endpoint=CATALOG["endpoint"], target_profile="default", execution_policy=_POLICY,
                )
                parity_catalog_digest = parity_catalog["catalog_digest"]
                consent_key = ConsentKey(tmp / "consent")
                registry = ApprovedHostRegistry()
                registry.register(peer_id="host-a", installation_id=lab.host_id, profile="default",
                                   endpoint_id="target-b", fingerprint=lab.owner_a["fingerprint"],
                                   policy_digest=POLICY_DIGEST, catalog_digest=parity_catalog_digest,
                                   consent_public_key=consent_key.public_bytes, pairing_generation=1)
                joining_store = JoiningStore(tmp / "joining")
                self_binding = {
                    "endpoint_id": "target-b", "fingerprint": lab.owner_b["fingerprint"],
                    "target_install_id": lab.target_id, "target_profile": "default",
                    "catalog_digest": parity_catalog_digest, "policy_digest": POLICY_DIGEST,
                }
                lab.set_binding(self_binding)
                service = JoiningOwnerService(joining_store, registry, host_peer_id="host-a", origin=ORIGIN,
                                               consent_key=consent_key, channel=lab.joining, self_binding=self_binding)

                lab.serve(lab_control.HostLobbyControl(store, lobby["lobby_id"], lab.binding,
                                                        joining_owner_key=consent_key.public_bytes,
                                                        host_id=lab.host_id))

                text = _invitation_text(lobby["lobby_id"], invite.invite_id, invite.secret)
                preview = service.preview(text)
                metadata = service.refresh_metadata(preview["import_id"])
                self.assertEqual(metadata["name"], "Parity lab")
                redeemed = service.redeem(preview["import_id"])
                self.assertEqual(redeemed["status"], "awaiting_host_approval")
                store.decide_host(lobby["lobby_id"], invite.invite_id, binding=lab.binding, approved=True)
                service.confirm_scope(preview["import_id"], room_id=lobby["planned_room_id"], member_id="peer-b")
                result = service.consent(preview["import_id"], approve=True)
                self.assertEqual(result["status"], "ready")
                self.assertEqual(store.get_redemption(lobby["lobby_id"], invite.invite_id)["status"], "ready")

                class MaterialRegistry:
                    def __init__(self):
                        self.envelope = None
                    def accept(self, scope, envelope):
                        self.envelope = copy.deepcopy(envelope)
                        return {"status": "material_ready"}

                material_registry = MaterialRegistry()
                # Re-bind the real lab control with its material adapter; the
                # control remains the actual pinned TLS path.
                lab.serve(lab_control.HostLobbyControl(
                    store, lobby["lobby_id"], lab.binding,
                    joining_owner_key=consent_key.public_bytes, host_id=lab.host_id,
                    material_registry=material_registry))

                def grant_for(scope):
                    now = time.time()
                    claims = {"room_id": scope["room_id"], "home_install_id": scope["host_id"],
                              "authority_gateway_id": scope["host_id"], "authority_epoch": 1,
                              "member_id": scope["member_id"],
                              "target_install_id": scope["binding"]["target_install_id"],
                              "target_profile": scope["binding"]["target_profile"],
                              "issued_at": now, "expires_at": now + 600,
                              "status_expires_at": now + 600,
                              "execution_policy_digest": POLICY_DIGEST}
                    raw = json.dumps(claims).encode()
                    payload = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
                    sig = base64.urlsafe_b64encode(b"0" * 32).rstrip(b"=").decode()
                    return payload + "." + sig

                async def rpc(method, params):
                    self.assertEqual(method, "groups.peer.invite")
                    scope = {
                        "lobby_id": lobby["lobby_id"], "invite_id": invite.invite_id,
                        "room_id": lobby["planned_room_id"], "member_id": "peer-b",
                        "binding": lab.binding, "host_id": lab.host_id,
                    }
                    return {"grant": grant_for(scope), "target_profile": params["profile"],
                            "catalog": parity_catalog, "endpoint": parity_catalog["endpoint"]}

                mint = TargetMaterialMint(joining_store, consent_key=consent_key,
                                          channel=lab.joining, rpc=rpc, host_peer_id="host-a")
                material = asyncio.run(mint.mint({"lobby_id": lobby["lobby_id"], "invite_id": invite.invite_id}))
                self.assertEqual(material["status"], "material_ready")
                self.assertIsNotNone(material_registry.envelope)
                self.assertNotIn(invite.secret.encode(), b"".join(lab.observed))


if __name__ == "__main__":
    unittest.main()
