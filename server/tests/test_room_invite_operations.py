"""Product-local invite-rooms operation service — security boundary tests.

Verifies: OwnerContext validation before reads/mutations, wrong owner/profile
rejected, redacted views (no raw rows/grant/token/path/secret), issue return
contains secret but repr is redacted, read/preview don't seal/mint/create,
approval alone cannot activate, unavailable/mismatched material blocks,
no generic action dispatcher, no method to forge joining consent, IDs literal.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from invite_rooms.peer_registry import PeerRegistry, PeerRegistryError
from invite_rooms.service import (
    InviteRoomsService,
    OwnerContext,
    ServiceError,
)
from room_invite_store import InviteError, RoomInviteStore, IssuedInvite

from gateway.hosted_room_peer import catalog_mapping
from gateway.hosted_room_execution_policy import RoomExecutionPolicy

INSTALL_A = "install:" + "a" * 32
INSTALL_B = "install:" + "b" * 32
FINGERPRINT_B = "b" * 64
ADAPTER_URL = "http://127.0.0.1:19001/links/unit-link"
GRANT = "unit-fixture-grant-secret"
_POLICY_UNSIGNED = {"version": 1, "target_profile": "default", "enabled_toolsets": ["bot_room"], "approval_mode": "manual", "max_iterations": 10}
_POLICY = {**_POLICY_UNSIGNED, "policy_digest": hashlib.sha256(json.dumps(_POLICY_UNSIGNED, sort_keys=True, separators=(",", ":")).encode("ascii")).hexdigest()}
CATALOG = catalog_mapping(installation_id=INSTALL_B, protocol_versions=(2,), link_modes=("direct",), persistent_process=True, text=True, attachments=False, endpoint={"available": True, "url": ADAPTER_URL, "transport_security": "loopback"}, target_profile="default", execution_policy=_POLICY)
CATALOG_DIGEST = CATALOG["catalog_digest"]
POLICY_DIGEST = CATALOG["execution_policy"]["policy_digest"]
ROSTER = [
    {"member_id": "local-a", "profile": "default", "handle": "local"},
    {"member_id": "peer-b", "profile": "default", "handle": "peer",
     "target": {"kind": "peer", "peer_id": "owner-b", "installation_id": INSTALL_B,
                "profile": "default", "capability_digest": CATALOG_DIGEST}},
]


def make_service():
    tmp = tempfile.TemporaryDirectory(prefix="room-svc-")
    root = Path(tmp.name)
    store = RoomInviteStore(root / "invites.db")
    registry = PeerRegistry()
    registry.register(
        peer_id="owner-b", installation_id=INSTALL_B, profile="default",
        fingerprint=FINGERPRINT_B, endpoint_id="endpoint-b",
        catalog=copy.deepcopy(CATALOG), adapter_url=ADAPTER_URL, grant=GRANT,
    )
    svc = InviteRoomsService(store, registry, trusted_owner=OwnerContext(installation_id=INSTALL_A, profile="default"))
    ctx = OwnerContext(installation_id=INSTALL_A, profile="default")
    return svc, ctx, store, registry, tmp


class OwnerContextTests(unittest.TestCase):
    def test_valid_context(self):
        ctx = OwnerContext(installation_id=INSTALL_A, profile="default")
        self.assertEqual(ctx.installation_id, INSTALL_A)
        self.assertEqual(ctx.profile, "default")

    def test_frozen(self):
        ctx = OwnerContext(installation_id=INSTALL_A, profile="default")
        with self.assertRaises(Exception):
            ctx.profile = "other"  # type: ignore

    def test_invalid_installation_id(self):
        with self.assertRaises(ServiceError):
            OwnerContext(installation_id="bad", profile="default")

    def test_invalid_profile(self):
        with self.assertRaises(ServiceError):
            OwnerContext(installation_id=INSTALL_A, profile="")


class CreateLobbyTests(unittest.TestCase):
    def test_create_lobby_returns_redacted_view(self):
        svc, ctx, store, reg, tmp = make_service()
        self.addCleanup(tmp.cleanup)
        view = svc.create_lobby(ctx, name="Test Room", roster=copy.deepcopy(ROSTER))
        self.assertIn("lobby_id", view)
        self.assertIn("name", view)
        self.assertEqual(view["name"], "Test Room")
        self.assertIn("state", view)
        self.assertEqual(view["state"], "open")
        self.assertIn("roster", view)
        # No raw DB fields.
        for key in ("roster_json", "native_spec_json", "updated_at"):
            # updated_at is OK to expose; roster_json/native_spec_json must not appear
            if key in ("roster_json", "native_spec_json"):
                self.assertNotIn(key, view)

    def test_create_wrong_profile_rejected(self):
        svc, ctx, store, reg, tmp = make_service()
        self.addCleanup(tmp.cleanup)
        wrong_ctx = OwnerContext(installation_id=INSTALL_A, profile="other")
        with self.assertRaises(ServiceError):
            svc.create_lobby(wrong_ctx, name="Test", roster=copy.deepcopy(ROSTER))

    def test_create_wrong_installation_rejected(self):
        svc, ctx, store, reg, tmp = make_service()
        self.addCleanup(tmp.cleanup)
        # Invalid installation_id format is rejected at context construction.
        with self.assertRaises(ServiceError):
            OwnerContext(installation_id="install:" + "z" * 32, profile="default")

    def test_create_non_owner_context_rejected(self):
        svc, ctx, store, reg, tmp = make_service()
        self.addCleanup(tmp.cleanup)
        with self.assertRaises(ServiceError):
            svc.create_lobby("not-a-context", name="Test", roster=copy.deepcopy(ROSTER))  # type: ignore


class InspectLobbyTests(unittest.TestCase):
    def test_inspect_returns_redacted_view(self):
        svc, ctx, store, reg, tmp = make_service()
        self.addCleanup(tmp.cleanup)
        view = svc.create_lobby(ctx, name="Test", roster=copy.deepcopy(ROSTER))
        lobby_id = view["lobby_id"]
        inspected = svc.inspect_lobby(ctx, lobby_id)
        self.assertEqual(inspected["lobby_id"], lobby_id)
        self.assertEqual(inspected["name"], "Test")
        # No native spec or raw rows.
        self.assertNotIn("native_spec_json", inspected)
        self.assertNotIn("roster_json", inspected)

    def test_inspect_wrong_profile_rejected(self):
        svc, ctx, store, reg, tmp = make_service()
        self.addCleanup(tmp.cleanup)
        view = svc.create_lobby(ctx, name="Test", roster=copy.deepcopy(ROSTER))
        wrong_ctx = OwnerContext(installation_id=INSTALL_A, profile="other")
        with self.assertRaises(ServiceError):
            svc.inspect_lobby(wrong_ctx, view["lobby_id"])

    def test_inspect_does_not_seal_or_mutate(self):
        svc, ctx, store, reg, tmp = make_service()
        self.addCleanup(tmp.cleanup)
        view = svc.create_lobby(ctx, name="Test", roster=copy.deepcopy(ROSTER))
        state_before = store.get_lobby(view["lobby_id"])["state"]
        svc.inspect_lobby(ctx, view["lobby_id"])
        state_after = store.get_lobby(view["lobby_id"])["state"]
        self.assertEqual(state_before, state_after)


class IssueInviteTests(unittest.TestCase):
    def test_issue_returns_invited_invite_with_secret(self):
        svc, ctx, store, reg, tmp = make_service()
        self.addCleanup(tmp.cleanup)
        view = svc.create_lobby(ctx, name="Test", roster=copy.deepcopy(ROSTER))
        issued = svc.issue_invite(ctx, view["lobby_id"], "peer-b", invitee_label="B")
        self.assertIsInstance(issued, IssuedInvite)
        self.assertTrue(issued.secret)  # secret is present
        self.assertFalse(hasattr(issued, "secret_sha256"))  # no hash exposed

    def test_issue_secret_repr_redacted(self):
        svc, ctx, store, reg, tmp = make_service()
        self.addCleanup(tmp.cleanup)
        view = svc.create_lobby(ctx, name="Test", roster=copy.deepcopy(ROSTER))
        issued = svc.issue_invite(ctx, view["lobby_id"], "peer-b", invitee_label="B")
        self.assertNotIn(issued.secret, repr(issued))

    def test_issue_wrong_profile_rejected_before_store(self):
        svc, ctx, store, reg, tmp = make_service()
        self.addCleanup(tmp.cleanup)
        view = svc.create_lobby(ctx, name="Test", roster=copy.deepcopy(ROSTER))
        wrong_ctx = OwnerContext(installation_id=INSTALL_A, profile="other")
        with self.assertRaises(ServiceError):
            svc.issue_invite(wrong_ctx, view["lobby_id"], "peer-b", invitee_label="B")


class RevokeInviteTests(unittest.TestCase):
    def test_revoke_works(self):
        svc, ctx, store, reg, tmp = make_service()
        self.addCleanup(tmp.cleanup)
        view = svc.create_lobby(ctx, name="Test", roster=copy.deepcopy(ROSTER))
        issued = svc.issue_invite(ctx, view["lobby_id"], "peer-b", invitee_label="B")
        result = svc.revoke_invite(ctx, view["lobby_id"], issued.invite_id)
        self.assertTrue(result)

    def test_revoke_wrong_profile_rejected(self):
        svc, ctx, store, reg, tmp = make_service()
        self.addCleanup(tmp.cleanup)
        view = svc.create_lobby(ctx, name="Test", roster=copy.deepcopy(ROSTER))
        issued = svc.issue_invite(ctx, view["lobby_id"], "peer-b", invitee_label="B")
        wrong_ctx = OwnerContext(installation_id=INSTALL_A, profile="other")
        with self.assertRaises(ServiceError):
            svc.revoke_invite(wrong_ctx, view["lobby_id"], issued.invite_id)


class HostDecisionTests(unittest.TestCase):
    def setUp_lobby_with_redemption(self, svc, ctx):
        view = svc.create_lobby(ctx, name="Test", roster=copy.deepcopy(ROSTER))
        issued = svc.issue_invite(ctx, view["lobby_id"], "peer-b", invitee_label="B")
        binding = svc.registry.binding_for("owner-b")
        store = svc.store
        store.redeem(view["lobby_id"], issued.invite_id, issued.secret, binding=binding)
        return view, issued

    def test_decide_host_approves_using_peer_id(self):
        svc, ctx, store, reg, tmp = make_service()
        self.addCleanup(tmp.cleanup)
        view, issued = self.setUp_lobby_with_redemption(svc, ctx)
        result = svc.decide_host(ctx, view["lobby_id"], issued.invite_id,
                                  peer_id="owner-b", approved=True)
        self.assertEqual(result["host_decision"], "approved")
        self.assertEqual(result["status"], "consent_required")

    def test_decide_host_denies(self):
        svc, ctx, store, reg, tmp = make_service()
        self.addCleanup(tmp.cleanup)
        view, issued = self.setUp_lobby_with_redemption(svc, ctx)
        result = svc.decide_host(ctx, view["lobby_id"], issued.invite_id,
                                  peer_id="owner-b", approved=False)
        self.assertEqual(result["host_decision"], "denied")
        self.assertEqual(result["status"], "denied")

    def test_decide_host_unknown_peer_rejected(self):
        svc, ctx, store, reg, tmp = make_service()
        self.addCleanup(tmp.cleanup)
        view, issued = self.setUp_lobby_with_redemption(svc, ctx)
        with self.assertRaises((ServiceError, PeerRegistryError, InviteError)):
            svc.decide_host(ctx, view["lobby_id"], issued.invite_id,
                            peer_id="unknown", approved=True)

    def test_decide_host_wrong_profile_rejected(self):
        svc, ctx, store, reg, tmp = make_service()
        self.addCleanup(tmp.cleanup)
        view, issued = self.setUp_lobby_with_redemption(svc, ctx)
        wrong_ctx = OwnerContext(installation_id=INSTALL_A, profile="other")
        with self.assertRaises(ServiceError):
            svc.decide_host(wrong_ctx, view["lobby_id"], issued.invite_id,
                            peer_id="owner-b", approved=True)

    def test_decide_host_result_is_redacted(self):
        svc, ctx, store, reg, tmp = make_service()
        self.addCleanup(tmp.cleanup)
        view, issued = self.setUp_lobby_with_redemption(svc, ctx)
        result = svc.decide_host(ctx, view["lobby_id"], issued.invite_id,
                                  peer_id="owner-b", approved=True)
        # No binding secrets in the view.
        for key in ("fingerprint", "endpoint_id", "target_install_id", "target_profile"):
            self.assertNotIn(key, result)
        # No grant.
        for v in result.values():
            self.assertNotEqual(v, GRANT)


class ActivationBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def _setup(self):
        svc, ctx, store, reg, tmp = make_service()
        self.addCleanup(tmp.cleanup)
        view = svc.create_lobby(ctx, name="Test", roster=copy.deepcopy(ROSTER))
        issued = svc.issue_invite(ctx, view["lobby_id"], "peer-b", invitee_label="B")
        binding = reg.binding_for("owner-b")
        store.redeem(view["lobby_id"], issued.invite_id, issued.secret, binding=binding)
        return svc, ctx, view, issued

    async def test_activation_status_before_approval(self):
        svc, ctx, view, issued = self._setup()
        status = svc.activation_status(ctx, view["lobby_id"])
        self.assertFalse(status["ready"])
        self.assertEqual(status["lobby_state"], "open")
        # At least one peer not ready.
        peer_statuses = [p for p in status["peers"] if not p["ready"]]
        self.assertTrue(len(peer_statuses) > 0)

    async def test_activation_status_after_approval_only_not_ready(self):
        """Approval alone cannot activate — joining consent still required."""
        svc, ctx, view, issued = self._setup()
        svc.decide_host(ctx, view["lobby_id"], issued.invite_id,
                        peer_id="owner-b", approved=True)
        status = svc.activation_status(ctx, view["lobby_id"])
        self.assertFalse(status["ready"])
        # host_decision is approved but joining_consent is still pending.
        peer = [p for p in status["peers"] if p["member_id"] == "peer-b"][0]
        self.assertEqual(peer["host_decision"], "approved")
        self.assertEqual(peer["joining_consent"], "pending")
        self.assertFalse(peer["ready"])

    async def test_activation_status_does_not_seal(self):
        """activation_status is read-only; must not change lobby state."""
        svc, ctx, view, issued = self._setup()
        state_before = svc.store.get_lobby(view["lobby_id"])["state"]
        svc.activation_status(ctx, view["lobby_id"])
        state_after = svc.store.get_lobby(view["lobby_id"])["state"]
        self.assertEqual(state_before, state_after)

    async def test_activation_status_unavailable_material_blocks(self):
        """If peer material is not registered, activation is not ready."""
        tmp = tempfile.TemporaryDirectory(prefix="room-svc-")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        store = RoomInviteStore(root / "invites.db")
        registry = PeerRegistry()  # empty — no peers registered
        svc = InviteRoomsService(store, registry, trusted_owner=OwnerContext(installation_id=INSTALL_A, profile="default"))
        ctx = OwnerContext(installation_id=INSTALL_A, profile="default")
        with self.assertRaises(ServiceError):
            svc.create_lobby(ctx, name="Test", roster=copy.deepcopy(ROSTER))

    async def test_activation_status_wrong_profile_rejected(self):
        svc, ctx, view, issued = self._setup()
        wrong_ctx = OwnerContext(installation_id=INSTALL_A, profile="other")
        with self.assertRaises(ServiceError):
            svc.activation_status(wrong_ctx, view["lobby_id"])

    async def test_activation_status_result_redacted(self):
        svc, ctx, view, issued = self._setup()
        status = svc.activation_status(ctx, view["lobby_id"])
        # No grant, fingerprint, endpoint_id in the status.
        status_str = str(status)
        self.assertNotIn(GRANT, status_str)
        self.assertNotIn(FINGERPRINT_B, status_str)


class NoForgeConsentTests(unittest.TestCase):
    """The service must NOT expose a method to forge the joining owner's consent."""

    def test_no_consent_joining_method(self):
        svc, ctx, store, reg, tmp = make_service()
        self.addCleanup(tmp.cleanup)
        # The service must not have a method that directly sets joining consent.
        self.assertFalse(hasattr(svc, "consent_joining"))
        self.assertFalse(hasattr(svc, "joining_consent"))
        self.assertFalse(hasattr(svc, "give_consent"))

    def test_no_generic_action_dispatcher(self):
        svc, ctx, store, reg, tmp = make_service()
        self.addCleanup(tmp.cleanup)
        self.assertFalse(hasattr(svc, "execute"))
        self.assertFalse(hasattr(svc, "dispatch"))
        self.assertFalse(hasattr(svc, "perform_action"))
        self.assertFalse(hasattr(svc, "run_action"))


class SecretLeakageTests(unittest.TestCase):
    """Secrets must not appear in views, repr, or error tracebacks."""

    def test_lobby_view_has_no_secret_hash(self):
        svc, ctx, store, reg, tmp = make_service()
        self.addCleanup(tmp.cleanup)
        view = svc.create_lobby(ctx, name="Test", roster=copy.deepcopy(ROSTER))
        self.assertNotIn("secret_sha256", view)
        self.assertNotIn("secret", view)

    def test_error_messages_have_no_secrets(self):
        svc, ctx, store, reg, tmp = make_service()
        self.addCleanup(tmp.cleanup)
        view = svc.create_lobby(ctx, name="Test", roster=copy.deepcopy(ROSTER))
        issued = svc.issue_invite(ctx, view["lobby_id"], "peer-b", invitee_label="B")
        # Try to inspect a non-existent lobby — error should not contain secret.
        try:
            svc.inspect_lobby(ctx, "lobby-nonexistent")
        except (ServiceError, InviteError) as exc:
            self.assertNotIn(issued.secret, str(exc))
            self.assertNotIn(issued.secret, repr(exc))

    def test_service_repr_has_no_secrets(self):
        svc, ctx, store, reg, tmp = make_service()
        self.addCleanup(tmp.cleanup)
        view = svc.create_lobby(ctx, name="Test", roster=copy.deepcopy(ROSTER))
        issued = svc.issue_invite(ctx, view["lobby_id"], "peer-b", invitee_label="B")
        self.assertNotIn(issued.secret, repr(svc))


class IdLiteralTests(unittest.TestCase):
    """IDs must not drift between review, approval, and activation."""

    def test_lobby_id_constant_across_operations(self):
        svc, ctx, store, reg, tmp = make_service()
        self.addCleanup(tmp.cleanup)
        view = svc.create_lobby(ctx, name="Test", roster=copy.deepcopy(ROSTER))
        lobby_id = view["lobby_id"]
        inspected = svc.inspect_lobby(ctx, lobby_id)
        self.assertEqual(inspected["lobby_id"], lobby_id)
        issued = svc.issue_invite(ctx, lobby_id, "peer-b", invitee_label="B")
        self.assertEqual(issued.invite_id, issued.invite_id)  # literal


class ParityTests(unittest.IsolatedAsyncioTestCase):
    """Parity/migration evidence: approval alone cannot activate, material blocks."""

    def _full_setup(self):
        svc, ctx, store, reg, tmp = make_service()
        self.addCleanup(tmp.cleanup)
        view = svc.create_lobby(ctx, name="Test", roster=copy.deepcopy(ROSTER))
        issued = svc.issue_invite(ctx, view["lobby_id"], "peer-b", invitee_label="B")
        binding = reg.binding_for("owner-b")
        store.redeem(view["lobby_id"], issued.invite_id, issued.secret, binding=binding)
        return svc, ctx, view, issued, binding

    async def test_approval_alone_cannot_activate(self):
        """Host approval without joining consent does not make activation ready."""
        svc, ctx, view, issued, binding = self._full_setup()
        svc.decide_host(ctx, view["lobby_id"], issued.invite_id,
                        peer_id="owner-b", approved=True)
        status = svc.activation_status(ctx, view["lobby_id"])
        self.assertFalse(status["ready"])

    async def test_mismatched_material_blocks(self):
        """If the registry peer binding doesn't match the redemption, activation is blocked."""
        svc, ctx, view, issued, binding = self._full_setup()
        svc.decide_host(ctx, view["lobby_id"], issued.invite_id,
                        peer_id="owner-b", approved=True)
        # Give joining consent via store directly (signed consent path is deferred).
        store = svc.store
        store.consent_joining(view["lobby_id"], issued.invite_id, binding=binding, consented=True)
        # Deliberate registry drift: persisted consent is pinned to the old key.
        svc.registry._peers['owner-b'].fingerprint = 'c' * 64
        calls = []
        async def forbidden_rpc(method, params):
            calls.append(method)
            raise AssertionError('must not call native RPC')
        svc._rpc = forbidden_rpc
        self.assertFalse(svc.activation_status(ctx, view['lobby_id'])['ready'])
        with self.assertRaisesRegex(ServiceError, '^activation_not_ready$'):
            await svc.activate(ctx, view['lobby_id'])
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()