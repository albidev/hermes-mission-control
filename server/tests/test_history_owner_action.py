import asyncio
import tempfile
import unittest
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from invite_rooms.consent import canonical, verify
from invite_rooms.history_consent import HistoryConsentStore, PURPOSE_OWNER
from invite_rooms.history_host import OwnerHistoryHandlers, _Signer


class Store:
    def history_scope_for_peer(self, room_id, *, peer_id, binding):
        assert room_id == "room-1"
        assert peer_id == "peer-1"
        assert binding["target_install_id"] == "install:" + "b" * 32
        assert binding["target_profile"] == "default"
        assert binding["catalog_digest"] == "c" * 64
        assert binding["policy_digest"] == "d" * 64
        return {"member_id": "member-1", "history_policy": "on", "from_seq_inclusive": 1}


class OwnerHistoryActionTests(unittest.TestCase):
    def test_explicit_owner_action_persists_signed_approval_before_guest_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            owner_private = Ed25519PrivateKey.generate()
            guest_private = Ed25519PrivateKey.generate()
            owner = _Signer(owner_private)

            async def native(method, params, **kwargs):
                self.assertEqual(method, "groups.state")
                self.assertEqual(params, {"room_id": "room-1"})
                return {"room": {
                    "room_id": "room-1", "home_install_id": "install:" + "a" * 32,
                    "status": "live", "authority_gateway_id": "gw-1", "authority_epoch": 4,
                    "members": [{"member_id": "member-1", "target": {"kind": "peer", "peer_id": "peer-1", "installation_id": "install:" + "b" * 32, "profile": "default", "capability_digest": "c" * 64}}],
                }}

            handlers = OwnerHistoryHandlers(
                store=Store(), owner_installation_id="install:" + "a" * 32,
                owner_key=owner, consent_db=Path(tmp) / "history.sqlite", native_rpc=native,
            )
            result = asyncio.run(handlers.owner_approve_for_owner(
                room_id="room-1", recipient_id="peer-1", guest_public_key=guest_private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw),
                binding={"endpoint_id": "ep-1", "fingerprint": "f" * 64, "target_install_id": "install:" + "b" * 32,
                         "target_profile": "default", "catalog_digest": "c" * 64, "policy_digest": "d" * 64},
                owner_installation_id="install:" + "a" * 32,
                owner_profile="default", pairing_generation=2,
            ))
            self.assertEqual(result["status"], "owner_approved")
            scope = result["scope"]
            receipt = result["owner_receipt"]
            self.assertEqual(receipt, dict(scope, purpose=PURPOSE_OWNER))
            verify(owner.public_bytes, receipt, result["owner_signature"], receipt)
            ledger = HistoryConsentStore(Path(tmp) / "history.sqlite", owner_public_key=owner.public_bytes,
                                         guest_public_key=guest_private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))
            self.assertTrue(ledger.snapshot(scope)["owner_receipt_json"])
            self.assertFalse(ledger.authorized(scope), "guest acceptance is still required")

    def test_guest_key_or_recipient_mismatch_cannot_approve(self):
        with tempfile.TemporaryDirectory() as tmp:
            owner_private = Ed25519PrivateKey.generate()
            async def native(method, params, **kwargs):
                return {"room": {"room_id": "room-1", "home_install_id": "install:" + "a" * 32,
                                  "status": "live", "authority_gateway_id": "gw-1", "authority_epoch": 1,
                                  "members": [{"member_id": "member-1"}]}}
            handlers = OwnerHistoryHandlers(
                store=Store(), owner_installation_id="install:" + "a" * 32,
                owner_key=_Signer(owner_private), consent_db=Path(tmp) / "history.sqlite", native_rpc=native,
            )
            with self.assertRaises(Exception):
                asyncio.run(handlers.owner_approve_for_owner(
                    room_id="room-1", recipient_id="peer-evil", guest_public_key=b"x" * 32,
                    owner_installation_id="install:" + "a" * 32, owner_profile="default"))


if __name__ == "__main__":
    unittest.main()
