"""Descriptor/task ownership tests, not native interoperability evidence."""
import asyncio
import socket
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from connector import RelayClient, _Stream


class ConnectorOwnershipTests(unittest.TestCase):
    def test_stop_drains_cancelled_stream_pump_before_closing_loop(self):
        ready = threading.Event()
        pumps = []
        sockets = []

        class Client(RelayClient):
            async def _run(self):
                stream = _Stream(self, "owned")
                sockets.extend((stream.relay_end, stream.app_end))
                self._streams[stream.stream_id] = stream
                stream.start()
                pumps.append(stream.pump)
                self._connected.set()
                ready.set()
                # A cancelled pump must finish its async finally block, not just
                # receive cancel(). The loop owns this operation until it drains.
                await asyncio.Future()

            async def _send(self, kind, stream_id, raw=None):
                await asyncio.sleep(0.05)

        client = Client(relay_url="ws://127.0.0.1:1/data", identity="host", peer_identity="target",
                        link_id="owned", room_id="owned", credential="test-only")
        try:
            client.start(wait=2)
            self.assertTrue(ready.wait(2))
            client.stop()
            self.assertFalse(client._thread.is_alive())
            self.assertTrue(client._loop.is_closed())
            self.assertTrue(all(task.done() for task in pumps), "stream pump destroyed pending")
            self.assertEqual(sockets[0].fileno(), -1)
            self.assertEqual(client._streams, {})
        finally:
            client.stop()
            for sock in sockets:
                sock.close()


class StreamCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_second_cancel_during_close_still_releases_descriptor(self):
        entered_close = asyncio.Event()

        class Client:
            def __init__(self):
                self._streams = {}

            async def _send(self, kind, stream_id, raw=None):
                entered_close.set()
                await asyncio.Future()

        client = Client()
        stream = _Stream(client, "cancel-close")
        client._streams[stream.stream_id] = stream
        stream.start()
        try:
            await asyncio.sleep(0)
            stream.app_end.close()
            await asyncio.wait_for(entered_close.wait(), 2)
            stream.pump.cancel()
            await asyncio.gather(stream.pump, return_exceptions=True)
            self.assertEqual(stream.relay_end.fileno(), -1)
            self.assertEqual(client._streams, {})
        finally:
            stream.relay_end.close()
            stream.app_end.close()


if __name__ == "__main__":
    unittest.main()
