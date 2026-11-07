import asyncio
import base64
import sys
import unittest
from typing import Any
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
from relay import (
    MAX_FRAME_BYTES,
    MAX_QUEUED_BYTES,
    MAX_STREAMS,
    REQUEST_TIMEOUT,
    Relay,
    RelayError,
    RelayWebSocketServer,
)


class FakeWebSocket:
    def __init__(self, incoming):
        self.incoming = incoming
        self.sent = []
        self.closed = None

    async def recv(self):
        return self.incoming

    async def send(self, value):
        self.sent.append(value)

    async def close(self, code, reason):
        self.closed = (code, reason)


class RelayRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.relay = Relay()
        self.credential = self.relay.provision(
            source="install-a", target="install-b", room_id="room-a", link_id="link-a"
        )
        self.a = await self.relay.connect(identity="install-a", link_id="link-a", credential=self.credential)
        self.b = await self.relay.connect(identity="install-b", link_id="link-a", credential=self.credential)
        self.frame = {
            "type": "DATA", "source": "install-a", "target": "install-b", "room_id": "room-a",
            "link_id": "link-a", "incarnation": self.a.incarnation, "stream_id": "stream-1",
            "payload": base64.b64encode(b"opaque-ciphertext").decode("ascii"),
        }

    async def asyncTearDown(self):
        await self.relay.disconnect(self.a)
        await self.relay.disconnect(self.b)

    async def test_authenticated_route_delivers_opaque_payload(self):
        await self.relay.route(self.a, self.frame)
        self.assertEqual(await self.b.receive(), self.frame)

    async def test_unprovisioned_duplicate_and_sender_spoof_are_rejected(self):
        with self.assertRaises(RelayError):
            await self.relay.connect(identity="install-a", link_id="link-a", credential=self.credential)
        with self.assertRaises(RelayError):
            await self.relay.connect(identity="attacker", link_id="link-a", credential=self.credential)
        spoof = dict(self.frame, source="install-b")
        with self.assertRaises(RelayError):
            await self.relay.route(self.a, spoof)

    async def test_wrong_room_link_offline_and_stale_incarnation_fail_closed(self):
        for bad in (dict(self.frame, room_id="wrong"), dict(self.frame, link_id="wrong")):
            with self.subTest(bad=bad):
                with self.assertRaises(RelayError):
                    await self.relay.route(self.a, bad)
        await self.relay.disconnect(self.b)
        with self.assertRaises(RelayError):
            await self.relay.route(self.a, self.frame)
        replacement = await self.relay.connect(identity="install-b", link_id="link-a", credential=self.credential)
        self.b = replacement
        await self.relay.disconnect(self.a)
        with self.assertRaises(RelayError):
            await self.relay.route(self.a, self.frame)

    async def test_cross_link_target_binding_rejects_without_queue_or_stream_mutation(self):
        await self.relay.disconnect(self.b)
        other_credential = self.relay.provision(
            source="install-c", target="install-b", room_id="room-b", link_id="link-b"
        )
        c = await self.relay.connect(identity="install-c", link_id="link-b", credential=other_credential)
        self.b = await self.relay.connect(identity="install-b", link_id="link-b", credential=other_credential)
        try:
            with self.assertRaisesRegex(RelayError, "target route binding mismatch"):
                await self.relay.route(self.a, self.frame)
            self.assertEqual(self.b.queue.qsize(), 0)
            self.assertEqual(self.b.queued_bytes, 0)
            self.assertEqual(self.a.streams, set())
        finally:
            await self.relay.disconnect(c)

    async def test_malformed_oversize_and_stream_aggregate_limits(self):
        for bad in (
            {},
            dict(self.frame, payload="not-base64!"),
            dict(self.frame, payload=base64.b64encode(b"x" * (MAX_FRAME_BYTES + 1)).decode()),
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(RelayError):
                    await self.relay.route(self.a, bad)
        self.assertEqual(self.a.streams, set())
        for index in range(MAX_STREAMS):
            await self.relay.route(self.a, dict(self.frame, stream_id=f"stream-{index}"))
        with self.assertRaises(RelayError):
            await self.relay.route(self.a, dict(self.frame, stream_id="stream-over-quota"))
        self.b.queued_bytes = MAX_QUEUED_BYTES
        with self.assertRaises(RelayError):
            await self.relay.route(self.a, self.frame)

    async def test_full_target_queue_fails_fast_and_does_not_consume_stream(self):
        for index in range(self.b.queue.maxsize):
            self.b.queue.put_nowait((dict(self.frame, stream_id=f"queued-{index}"), 1))
        frame = dict(self.frame, stream_id="backpressured")
        with self.assertRaisesRegex(RelayError, "target backpressure"):
            await asyncio.wait_for(self.relay.route(self.a, frame), REQUEST_TIMEOUT)
        self.assertNotIn("backpressured", self.a.streams)

    async def test_invalid_payload_does_not_consume_stream_quota(self):
        bad = dict(self.frame, stream_id="invalid-new-stream", payload="not-base64!")
        with self.assertRaises(RelayError):
            await self.relay.route(self.a, bad)
        self.assertNotIn("invalid-new-stream", self.a.streams)

    async def test_wrong_type_data_coordinates_are_rejected_without_state_change(self):
        for field, value in (("link_id", []), ("target", {}), ("room_id", 7), ("incarnation", True)):
            with self.subTest(field=field):
                with self.assertRaises(RelayError):
                    await self.relay.route(self.a, dict(self.frame, **{field: value}))
        self.assertEqual(self.a.streams, set())

    async def test_wrong_type_auth_coordinates_are_rejected(self):
        for field, value in (("identity", []), ("link_id", {}), ("credential", 9)):
            with self.subTest(field=field):
                identity: Any = value if field == "identity" else "install-a"
                link_id: Any = value if field == "link_id" else "link-a"
                credential: Any = value if field == "credential" else self.credential
                with self.assertRaises(RelayError):
                    await self.relay.connect(
                        identity=identity,
                        link_id=link_id,
                        credential=credential,
                    )
        self.assertEqual(set(self.relay._connections), {"install-a", "install-b"})

    async def test_malformed_auth_frame_uses_bounded_protocol_error(self):
        server = RelayWebSocketServer(self.relay)
        websocket = FakeWebSocket(
            '{"type":"AUTH","identity":[],"link_id":"link-a","credential":"%s"}' % self.credential
        )
        await server.handler(websocket, "/data")
        self.assertEqual(websocket.closed, (1008, "relay rejected request"))
        self.assertIn('"type": "ERROR"', websocket.sent[0])
        self.assertEqual(set(self.relay._connections), {"install-a", "install-b"})

    async def test_health_is_separate_from_data_path_readiness(self):
        self.assertEqual(self.relay.health(), {
            "status": "ok", "listener": False, "data_path_ready": False, "active_identities": 2,
        })
        self.relay.data_listener_ready = True
        self.assertTrue(self.relay.health()["listener"])
        self.assertTrue(self.relay.health()["data_path_ready"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
