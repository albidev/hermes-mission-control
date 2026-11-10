import tempfile
import unittest
from pathlib import Path

from invite_rooms.consent import ConsentKey
from invite_rooms.history_consent import HistoryConsentStore, HistoryConsentError


class HistoryConsentStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.owner = ConsentKey(root / "owner-key")
        self.guest = ConsentKey(root / "guest-key")
        self.scope = {
            "room_id": "invite-room-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "home_install_id": "install:" + "a" * 32,
            "authority_gateway_id": "gateway-a", "authority_epoch": 7,
            "member_id": "member-b", "recipient_id": "peer-b", "pairing_generation": 3,
            "history_policy": "off", "from_seq_inclusive": 4,
        }
        self.db = root / "history-consent.sqlite"
        self.store = HistoryConsentStore(self.db, owner_public_key=self.owner.public_bytes,
                                         guest_public_key=self.guest.public_bytes)

    def tearDown(self):
        self.tmp.cleanup()

    def test_both_purpose_separated_signatures_are_required_and_survive_reopen(self):
        owner_receipt = dict(self.scope, purpose="room-history-owner-read-v1")
        guest_receipt = dict(self.scope, purpose="room-history-guest-read-v1")
        self.store.owner_approve(self.scope, owner_receipt, self.owner.sign(owner_receipt))
        self.assertFalse(self.store.authorized(self.scope))
        self.store.guest_accept(self.scope, guest_receipt, self.guest.sign(guest_receipt))
        self.assertTrue(self.store.authorized(self.scope))
        reopened = HistoryConsentStore(self.db, owner_public_key=self.owner.public_bytes,
                                       guest_public_key=self.guest.public_bytes)
        self.assertTrue(reopened.authorized(self.scope))

    def test_missing_ledger_path_is_not_authorized(self):
        self.assertFalse(self.store.authorized(self.scope))

    def test_wrong_purpose_and_scope_and_revoke_fail_closed(self):
        bad = dict(self.scope, purpose="joining-owner-consent-v1")
        with self.assertRaisesRegex(HistoryConsentError, "invalid_owner_read_approval"):
            self.store.owner_approve(self.scope, bad, self.owner.sign(bad))
        owner = dict(self.scope, purpose="room-history-owner-read-v1")
        guest = dict(self.scope, purpose="room-history-guest-read-v1")
        self.store.owner_approve(self.scope, owner, self.owner.sign(owner))
        self.store.guest_accept(self.scope, guest, self.guest.sign(guest))
        self.store.revoke(self.scope)
        self.assertFalse(self.store.authorized(self.scope))
        with self.assertRaisesRegex(HistoryConsentError, "history_read_revoked"):
            self.store.guest_accept(self.scope, guest, self.guest.sign(guest))


if __name__ == "__main__":
    unittest.main()
