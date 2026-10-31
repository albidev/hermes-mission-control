"""Socket/serving-thread ownership; no native process or model calls."""
import asyncio
import base64
import threading
import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from connector import ConnectorError, RelayClient


class ServingOwnership(unittest.TestCase):
    def make_client(self, callback):
        class Client(RelayClient):
            async def _run(self):
                self._connected.set()
                await asyncio.Future()

            async def _send(self, kind, stream_id, raw=None):
                pass

        return Client(relay_url="ws://127.0.0.1:1/data", identity="target", peer_identity="host",
                      link_id="owned", room_id="room", credential="test-only", on_new_stream=callback)

    def dispatch(self, client):
        frame = dict(type="DATA", source="host", target="target", link_id="owned", room_id="room",
                     stream_id="stream", payload=base64.b64encode(b"x").decode())
        asyncio.run_coroutine_threadsafe(client._dispatch(frame), client._loop).result(2)

    def test_stop_joins_blocked_serving_thread_and_retains_complete_inventory(self):
        entered = threading.Event()
        finished = threading.Event()
        sockets = []

        def serve(sock, stream_id):
            sockets.append(sock)
            entered.set()
            try:
                while sock.recv(1):
                    pass
            finally:
                sock.close()
                finished.set()

        client = self.make_client(serve)
        try:
            client.start(wait=2)
            self.dispatch(client)
            self.assertTrue(entered.wait(2))
            self.assertEqual(len(client.application_threads), 1)
            client.stop()
            self.assertTrue(finished.is_set())
            self.assertEqual(client.application_threads, ())
            self.assertEqual(sockets[0].fileno(), -1)
        finally:
            client.stop()
            self.assertTrue(finished.wait(2))

    def test_blocked_native_callback_is_reported_until_owner_releases_it(self):
        entered = threading.Event()
        release = threading.Event()

        def serve(sock, stream_id):
            entered.set()
            release.wait(10)

        client = self.make_client(serve)
        try:
            client.start(wait=2)
            self.dispatch(client)
            self.assertTrue(entered.wait(2))
            with self.assertRaisesRegex(ConnectorError, "teardown_incomplete"):
                client.stop()
            self.assertEqual(len(client.application_threads), 1)
            self.assertTrue(client.application_threads[0].is_alive())
        finally:
            release.set()
            client.stop()
        self.assertEqual(client.application_threads, ())

    def test_callback_exception_closes_owned_socket_without_secret_diagnostic(self):
        release = threading.Event()
        sockets = []

        def serve(sock, stream_id):
            sockets.append(sock)
            release.wait(3)
            raise RuntimeError("SENSITIVE callback text")

        client = self.make_client(serve)
        try:
            client.start(wait=2)
            self.dispatch(client)
            with self.assertLogs("rooms-connector", level="INFO") as logs:
                release.set()
                client.stop()
            self.assertTrue(any("serving_failed" in x for x in logs.output))
            self.assertNotIn("SENSITIVE", " ".join(logs.output))
            self.assertEqual(sockets[0].fileno(), -1)
            self.assertEqual(client.application_threads, ())
        finally:
            release.set()
            client.stop()


if __name__ == "__main__":
    unittest.main()
