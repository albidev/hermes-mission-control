import asyncio
import unittest
from invite_rooms.guest import GuestRoomService, GuestServiceError
from invite_rooms.joining_store import JoiningStore


import tempfile
from pathlib import Path
from unittest.mock import patch
from invite_rooms.consent import canonical


class Store(JoiningStore):
    """Real durable joining fixture; no invented session fields in the scope."""
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='guest-operation-')
        super().__init__(Path(self.tmp.name) / 'joining')
        iid = self.create_import(lobby_id='lobby-one', invite_id='invite-one',
                                 secret='x' * 43, host_peer_id='host-a')
        scope = {"room_id": "invite-room-r", "binding": {"target_profile": "default"}}
        self.confirm_scope(iid, scope)
        self.mark_redeemed(iid)
        self.begin_consent(iid, receipt={'scope': scope}, signature='sig')
        self.mark_consent_delivered(iid)
        key = canonical(dict(scope, invite_id='invite-one')).decode()
        self.mint_begin(key)
        self.mint_complete(key, b'{"grant":"grant-local"}')


class Channel:
    def __init__(self): self.calls = []
    def call(self, request):
        self.calls.append(request)
        if request["op"] == "room_log":
            return {"events": [{"seq": 2, "event_id": "e2"}, {"seq": 3, "event_id": "e3"}],
                    "cursor": 3, "latest_seq": 3, "has_more": False, "authority": {"epoch": 1}}
        return {"ok": True}


class Rpc:
    def __init__(self): self.calls = []
    def __call__(self, method, params):
        self.calls.append((method, params))
        if method == "approval.pending":
            return {"approvals": [{"request_id": "req-1", "command": "x"}]}
        return {"resolved": 1, "revoked": True}


class GuestOperationsTests(unittest.TestCase):
    def setUp(self):
        self.channel, self.rpc = Channel(), Rpc()
        store = Store()
        self.addCleanup(store.tmp.cleanup)
        self.service = GuestRoomService(store, channel=self.channel, rpc=self.rpc)
        # The old groups callable is never promoted to a canonical adapter.

    def test_log_is_verbatim_and_channel_only(self):
        page = self.service.log("invite-room-r", since_seq=2, limit=200)
        self.assertEqual(page["events"][0]["event_id"], "e2")
        self.assertEqual(self.channel.calls[0], {"op": "room_log", "room_id": "invite-room-r", "since_seq": 2, "limit": 200})
        self.assertNotIn("groups.create", [c["op"] for c in self.channel.calls])

    def test_groups_callable_cannot_discover_native_pending(self):
        with self.assertRaisesRegex(GuestServiceError, '^not_supported$'):
            self.service.my_bot('invite-room-r')
        self.assertEqual(self.rpc.calls, [])

    def test_groups_callable_cannot_approve(self):
        with self.assertRaisesRegex(GuestServiceError, '^not_supported$'):
            self.service.decide_approval('invite-room-r', 'req-1', 'approve')
        self.assertEqual(self.rpc.calls, [])

    def test_groups_callable_cannot_reject(self):
        with self.assertRaisesRegex(GuestServiceError, '^not_supported$'):
            self.service.decide_approval('invite-room-r', 'req-1', 'reject')
        self.assertEqual(self.rpc.calls, [])

    def test_other_session_request_has_no_native_respond(self):
        with self.assertRaisesRegex(GuestServiceError, '^not_supported$'):
            self.service.decide_approval('invite-room-r', 'other', 'approve')
        self.assertEqual(self.rpc.calls, [])

    def test_invalid_choice_is_rejected(self):
        with self.assertRaisesRegex(GuestServiceError, "invalid_choice"):
            self.service.decide_approval("invite-room-r", "req-1", "yes")

    def test_leave_revokes_local_grant_and_notifies(self):
        self.service.leave("invite-room-r")
        self.assertEqual(self.rpc.calls[-1], ("groups.peer.revoke", {"profile": "default", "grant": "grant-local"}))
        self.assertEqual(self.channel.calls[-1], {"op": "member_left", "room_id": "invite-room-r"})
        self.assertEqual(self.service.list_rooms(), [])

    def test_leave_is_idempotent(self):
        self.service.leave("invite-room-r")
        count = len(self.rpc.calls)
        self.service.leave("invite-room-r")
        self.assertEqual(len(self.rpc.calls), count)

    def test_leave_survives_host_notification_failure(self):
        self.channel.call = lambda request: (_ for _ in ()).throw(ConnectionError()) if request["op"] == "member_left" else {"ok": True}
        self.service.leave("invite-room-r")
        self.assertEqual(self.rpc.calls[-1][0], "groups.peer.revoke")


if __name__ == "__main__":
    unittest.main()
