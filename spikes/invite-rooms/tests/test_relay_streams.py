"""Relay request/response lifecycle needed by a real cross-host connector.

Gate D proved one-way routing only. A networked connector also needs the
target to answer on the same stream, the stream quota to be released when a
request ends, per-endpoint relay credentials and bounded backpressure that does
not tear down the sender's whole connection on a momentary burst.
"""
import asyncio
import base64
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import relay as relay_module
from relay import MAX_STREAMS, Relay, RelayError


def _payload(raw: bytes = b"ciphertext") -> str:
    return base64.b64encode(raw).decode("ascii")


class RelayStreamLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.relay = Relay()
        creds = self.relay.provision_link(source="install-a", target="install-b", room_id="room-a", link_id="link-a")
        self.creds = creds
        self.a = await self.relay.connect(identity="install-a", link_id="link-a", credential=creds["install-a"])
        self.b = await self.relay.connect(identity="install-b", link_id="link-a", credential=creds["install-b"])

    async def asyncTearDown(self):
        await self.relay.disconnect(self.a)
        await self.relay.disconnect(self.b)

    def frame(self, sender, stream="s-1", kind="DATA"):
        conn = self.a if sender == "install-a" else self.b
        frame = {"type": kind, "source": sender,
                 "target": "install-b" if sender == "install-a" else "install-a",
                 "room_id": "room-a", "link_id": "link-a", "incarnation": conn.incarnation,
                 "stream_id": stream}
        if kind == "DATA":
            frame["payload"] = _payload()
        return frame

    async def test_target_can_reply_on_the_same_stream(self):
        await self.relay.route(self.a, self.frame("install-a"))
        self.assertEqual((await self.b.receive())["stream_id"], "s-1")
        await self.relay.route(self.b, self.frame("install-b"))
        reply = await self.a.receive()
        self.assertEqual((reply["source"], reply["stream_id"]), ("install-b", "s-1"))

    async def test_close_releases_quota_for_both_sides(self):
        for index in range(MAX_STREAMS):
            await self.relay.route(self.a, self.frame("install-a", f"s-{index}"))
        with self.assertRaisesRegex(RelayError, "stream quota exceeded"):
            await self.relay.route(self.a, self.frame("install-a", "s-over"))
        await self.relay.route(self.b, self.frame("install-b", "s-0", kind="CLOSE"))
        await self.relay.route(self.a, self.frame("install-a", "s-new"))
        self.assertNotIn("s-0", self.a.streams | self.b.streams)

    async def test_close_for_unknown_stream_is_rejected(self):
        with self.assertRaisesRegex(RelayError, "unknown stream"):
            await self.relay.route(self.a, self.frame("install-a", "never-opened", kind="CLOSE"))

    async def test_close_frame_must_not_carry_payload(self):
        await self.relay.route(self.a, self.frame("install-a"))
        bad = dict(self.frame("install-a", kind="CLOSE"), payload=_payload())
        with self.assertRaisesRegex(RelayError, "malformed frame"):
            await self.relay.route(self.a, bad)

    async def test_disconnect_closes_open_streams_towards_peer(self):
        await self.relay.route(self.a, self.frame("install-a", "s-1"))
        await self.b.receive()
        await self.relay.disconnect(self.a)
        notice = await self.b.receive()
        self.assertEqual((notice["type"], notice["stream_id"], notice["source"]), ("CLOSE", "s-1", "install-a"))
        self.assertEqual(self.b.streams, set())
        self.a = await self.relay.connect(identity="install-a", link_id="link-a", credential=self.creds["install-a"])
        await self.relay.route(self.a, self.frame("install-a", "s-1"))

    async def test_endpoint_credentials_are_not_interchangeable(self):
        await self.relay.disconnect(self.b)
        with self.assertRaisesRegex(RelayError, "unprovisioned identity"):
            await self.relay.connect(identity="install-b", link_id="link-a", credential=self.creds["install-a"])
        self.assertNotEqual(self.creds["install-a"], self.creds["install-b"])

    async def test_backpressure_waits_bounded_then_delivers(self):
        for index in range(self.a.queue.maxsize):
            self.a.queue.put_nowait(({"filler": index}, 1))
        pending = asyncio.create_task(self.relay.route(self.b, self.frame("install-b", "slow")))
        await asyncio.sleep(0)
        self.assertFalse(pending.done())
        await self.a.receive()
        await asyncio.wait_for(pending, 2)
        self.assertIn("slow", self.b.streams)

    async def test_backpressure_that_never_drains_fails_without_consuming_stream(self):
        original = relay_module.BACKPRESSURE_TIMEOUT
        relay_module.BACKPRESSURE_TIMEOUT = 0.05
        try:
            for index in range(self.a.queue.maxsize):
                self.a.queue.put_nowait(({"filler": index}, 1))
            with self.assertRaisesRegex(RelayError, "target backpressure"):
                await self.relay.route(self.b, self.frame("install-b", "stuck"))
            self.assertNotIn("stuck", self.b.streams)
        finally:
            relay_module.BACKPRESSURE_TIMEOUT = original


if __name__ == "__main__":
    unittest.main(verbosity=2)
