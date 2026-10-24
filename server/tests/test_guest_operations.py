import asyncio
import unittest
from invite_rooms.guest import GuestRoomService, GuestServiceError
from invite_rooms.joining_store import JoiningStore


class Store(JoiningStore):
    def __init__(self):
        pass
    def list_imports(self):
        return [{"import_id": "import-1", "state": "consent_given"}]
    def confirmed_scope(self, _):
        return {"room_id": "invite-room-r", "session_id": "sid-b", "binding": {"target_profile": "default"}}
    def mint_load(self, _):
        return b'{"grant":"grant-local"}'


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
        self.service = GuestRoomService(Store(), channel=self.channel, rpc=self.rpc)

    def test_log_is_verbatim_and_channel_only(self):
        page = self.service.log("invite-room-r", since_seq=2, limit=200)
        self.assertEqual(page["events"][0]["event_id"], "e2")
        self.assertEqual(self.channel.calls[0], {"op": "room_log", "room_id": "invite-room-r", "since_seq": 2, "limit": 200})
        self.assertNotIn("groups.create", [c["op"] for c in self.channel.calls])

    def test_pending_reads_b_session(self):
        result = self.service.my_bot("invite-room-r")
        self.assertEqual(result["approvals"][0]["request_id"], "req-1")
        self.assertEqual(self.rpc.calls[0], ("approval.pending", {"session_id": "sid-b"}))

    def test_approve_maps_to_once_and_all_false(self):
        self.service.decide_approval("invite-room-r", "req-1", "approve")
        self.assertEqual(self.rpc.calls[-1], ("approval.respond", {"session_id": "sid-b", "request_id": "req-1", "choice": "once", "all": False}))

    def test_reject_maps_to_deny(self):
        self.service.decide_approval("invite-room-r", "req-1", "reject")
        self.assertEqual(self.rpc.calls[-1][1]["choice"], "deny")

    def test_other_session_request_is_forbidden_without_respond(self):
        self.rpc.calls.clear()
        with self.assertRaises(GuestServiceError):
            self.service.decide_approval("invite-room-r", "other", "approve")
        self.assertEqual([m for m, _ in self.rpc.calls], ["approval.pending"])

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
