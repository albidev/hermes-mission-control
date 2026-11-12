import os
import tempfile
import unittest
from pathlib import Path

from invite_rooms.control.dispatcher import ControlDispatcher, ControlError
from invite_rooms.history_consent import HistoryConsentStore, HistoryConsentError
from invite_rooms.service import InviteRoomsService


class HistoryIntegratedRegressionTests(unittest.TestCase):
    def test_dispatcher_requires_pairing_generation_in_wire_scope(self):
        scope = {
            "room_id": "room-1", "home_install_id": "install:" + "a" * 32,
            "authority_gateway_id": "gateway-a", "authority_epoch": 1,
            "member_id": "member-b", "recipient_id": "peer-b",
            "pairing_generation": 3, "history_policy": "off", "from_seq_inclusive": 4,
        }
        accepted = {"op": "history_read_acceptance", "scope": scope,
                    "receipt": dict(scope, purpose="room-history-guest-read-v1"), "signature": "x" * 88}
        self.assertEqual(ControlDispatcher._request(accepted)[0], "history_read_acceptance")
        legacy = dict(accepted, scope={k: v for k, v in scope.items() if k != "pairing_generation"})
        with self.assertRaises(ControlError):
            ControlDispatcher._request(legacy)

    def test_history_store_refuses_symlinked_parent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            real = root / "real"
            real.mkdir(mode=0o700)
            link = root / "link"
            link.symlink_to(real, target_is_directory=True)
            with self.assertRaisesRegex(HistoryConsentError, "private_storage_required"):
                HistoryConsentStore(link / "history.sqlite", owner_public_key=b"a" * 32, guest_public_key=b"b" * 32)

    def test_core_boundary_page_uses_request_bound_room_and_event_room_ids(self):
        room = {"room_id": "room-1", "authority_gateway_id": "gateway-a", "authority_epoch": 4}
        page = {"authority": {"gateway_id": "gateway-a", "epoch": 4},
                "events": [{"room_id": "room-1", "seq": 1}],
                "cursor": 1, "latest_seq": 2, "has_more": True}
        self.assertTrue(InviteRoomsService._valid_history_boundary_page(page, room))
        foreign = dict(page, events=[{"room_id": "other", "seq": 1}])
        self.assertFalse(InviteRoomsService._valid_history_boundary_page(foreign, room))

    def test_internal_admission_identity_is_not_part_of_signed_wire_scope(self):
        from types import SimpleNamespace
        from invite_rooms.history_host import _SingleAuthorization
        from invite_rooms.room_log import RoomLogAuthorization, RoomLogDenied
        auth = RoomLogAuthorization(
            authorization_id='approval', generation=1, peer_id='peer', endpoint_id='endpoint',
            pairing_generation=2, room_id='room', home_install_id='home',
            authority_gateway_id='gateway', authority_epoch=1, member_id='member',
            admission_generation=7, history_policy='off', from_seq_inclusive=3,
            expires_at_monotonic=100.0, recipient_id='peer', invite_id='invite', lobby_id='lobby')
        admission = dict(lobby_id='lobby', invite_id='invite', member_id='member',
                         owner_installation_id='home', history_policy='off', from_seq_inclusive=3)
        class Store:
            def history_scope_for_peer(self, *args, **kwargs): return dict(admission)
            def history_admission_generation(self, *args): return 7
        class Ledger:
            def snapshot(self, scope):
                assert len(scope) == 9 and 'lobby_id' not in scope and 'invite_id' not in scope
                return dict(revoked=False, owner_receipt_json='owner', guest_receipt_json='guest')
        context = SimpleNamespace(readmit=lambda: True, peer_id='peer', binding_dict=lambda: {})
        authority = _SingleAuthorization(auth, Ledger(), Store())
        self.assertIs(authority.admit(context, 'room', 1.0), auth)
        admission['invite_id'] = 'replacement'
        with self.assertRaises(RoomLogDenied): authority.admit(context, 'room', 1.0)

    def test_boundary_page_requires_exact_authority_and_schema(self):
        room = {"room_id": "room-1", "authority_gateway_id": "gateway-a", "authority_epoch": 4}
        good = {"room_id": "room-1", "authority": {"gateway_id": "gateway-a", "epoch": 4},
                "events": [], "cursor": 0, "latest_seq": 9, "has_more": True}
        self.assertTrue(InviteRoomsService._valid_history_boundary_page(good, room))
        foreign = dict(good, authority={"gateway_id": "foreign", "epoch": 77})
        self.assertFalse(InviteRoomsService._valid_history_boundary_page(foreign, room))
        malformed = dict(good, events="MALFORMED")
        self.assertFalse(InviteRoomsService._valid_history_boundary_page(malformed, room))


if __name__ == "__main__":
    unittest.main()
