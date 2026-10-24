import asyncio
import unittest
from invite_rooms.control.dispatcher import ControlDispatcher, ControlError, LinkState

class Authority:
    def __init__(self, state): self.state = state
    def current(self, endpoint): return self.state

class RoomLogTests(unittest.IsolatedAsyncioTestCase):
    async def test_room_log_is_bounded_and_rechecked_after_handler(self):
        state = LinkState({"endpoint_id": "ep"}, 1, "acknowledged")
        calls = []
        async def log(request):
            calls.append(request)
            return {"events": [{"seq": 1, "event_id": "e"}], "cursor": None,
                    "latest_seq": 1, "has_more": False,
                    "authority": {"gateway_id": "g", "epoch": 1}}
        dispatcher = ControlDispatcher(Authority(state), handlers={"room_log": log},
                                       owner_verify=lambda *_: True,
                                       admit=lambda *_: True)
        try:
            page = await dispatcher.dispatch("ep", 1, {"op": "room_log", "room_id": "r",
                                                        "since_seq": 0, "limit": 200})
            self.assertEqual(page["events"][0]["event_id"], "e")
            self.assertEqual(calls[0]["limit"], 200)
        finally:
            await dispatcher.close()

    async def test_room_log_generation_cutover_after_await_is_forbidden(self):
        state = LinkState({"endpoint_id": "ep"}, 1, "acknowledged")
        valid = True
        async def log(_):
            nonlocal valid
            valid = False
            return {"events": []}
        def admit(*_): return valid
        dispatcher = ControlDispatcher(Authority(state), handlers={"room_log": log},
                                       owner_verify=lambda *_: True, admit=admit)
        try:
            with self.assertRaises(ControlError) as caught:
                await dispatcher.dispatch("ep", 1, {"op": "room_log", "room_id": "r",
                                                    "since_seq": 0, "limit": 1})
            self.assertIn(caught.exception.code, {"forbidden", "control_unavailable"})
        finally:
            await dispatcher.close()

if __name__ == "__main__": unittest.main()
