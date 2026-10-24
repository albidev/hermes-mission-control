from __future__ import annotations

import asyncio
import ssl
import unittest
from dataclasses import dataclass

from invite_rooms.control.dispatcher import (
    ControlDispatcher,
    LinkState,
    TransportRegistry,
)


class Authority:
    def __init__(self, state):
        self.state = state

    def current(self, endpoint_id):
        return self.state


class ControlTransportTests(unittest.IsolatedAsyncioTestCase):
    def _state(self, protocol_state="acknowledged", generation=3):
        return LinkState(
            binding={"endpoint_id": "ep-1", "fingerprint": "a" * 64},
            generation=generation,
            protocol_state=protocol_state,
        )

    async def test_dispatch_maps_consent_and_calls_admission_after_queue(self):
        state = self._state()
        calls = []
        async def handler(request):
            calls.append(request)
            return {"ok": True}
        dispatcher = ControlDispatcher(
            Authority(state),
            handlers={"joining_consent": handler},
            owner_verify=lambda endpoint, request: True,
            admit=lambda endpoint, generation, op: calls.append((endpoint, generation, op)) or True,
        )
        result = await dispatcher.dispatch("ep-1", 3, {"op": "consent", "lobby_id": "l", "invite_id": "i", "receipt": {}, "signature": "s"})
        self.assertEqual(result, {"ok": True})
        self.assertEqual(calls[0], ("ep-1", 3, "joining_consent"))
        self.assertEqual(calls[1], {"op": "joining_consent", "lobby_id": "l", "invite_id": "i", "receipt": {}, "signature": "s"})

    async def test_confirmed_only_accepts_pair_ack(self):
        state = self._state("confirmed")
        dispatcher = ControlDispatcher(Authority(state), handlers={"pair_ack": lambda request: {"ok": True}})
        with self.assertRaisesRegex(Exception, "control_unavailable"):
            await dispatcher.dispatch("ep-1", 3, {"op": "preview", "lobby_id": "l", "invite_id": "i"})

    def test_registry_closes_connections_on_generation_change(self):
        registry = TransportRegistry()
        closed = []
        registry.register("ep-1", 3, lambda: closed.append(True))
        registry.register("ep-1", 3, lambda: closed.append(True))
        registry.observe_generation("ep-1", 4)
        self.assertEqual(len(closed), 2)


if __name__ == "__main__":
    unittest.main()
