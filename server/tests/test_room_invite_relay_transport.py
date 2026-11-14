"""Behavioral tests for product-owned WSS relay byte streams and loopback bridge."""
from __future__ import annotations

import asyncio
import contextlib
import socket
import ssl
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

from invite_rooms.relay_transport import RelayBridge, RelayClient, RelayTransportError
from server.tests import lab_pki

ROOT = Path(__file__).resolve().parents[2]
SPIKE = ROOT / "spikes" / "invite-rooms"
sys.path.insert(0, str(SPIKE))
import relay_daemon  # test relay server only; product client/bridge come from invite_rooms


class _EchoServer:
    def __init__(self):
        self.listener = socket.socket()
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(8)
        self.listener.settimeout(.1)
        self.address = self.listener.getsockname()[:2]
        self.calls = []
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._accept, daemon=True)
        self.thread.start()

    def _accept(self):
        while not self.stop_event.is_set():
            try:
                conn, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        with conn:
            conn.settimeout(3)
            size = int.from_bytes(self._read_exact(conn, 4), "big")
            data = self._read_exact(conn, size)
            self.calls.append(data)
            response = b"echo:" + data
            conn.sendall(len(response).to_bytes(4, "big") + response)

    @staticmethod
    def _read_exact(conn, size):
        data = bytearray()
        while len(data) < size:
            chunk = conn.recv(size - len(data))
            if not chunk:
                raise OSError("short test frame")
            data.extend(chunk)
        return bytes(data)

    def close(self):
        self.stop_event.set()
        self.listener.close()
        self.thread.join(2)


class RelayTransportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="product-relay-transport-")
        self.root = Path(self.tmp.name)
        self.owner_a = lab_pki.init_owner(self.root / "a", "relay-host")
        self.owner_b = lab_pki.init_owner(self.root / "b", "relay-target")
        relay_ca = lab_pki.init_owner(self.root / "relay-ca", "relay")
        server = lab_pki.issue_server(self.root / "relay-ca", self.root / "relay-tls", "IP:127.0.0.1")
        provisioned = relay_daemon.provision(self.root / "routes.json", self.root / "creds",
            link_id="relay-product-test", room_id="relay-pair-test",
            source="relay-host", target="relay-target")
        self.daemon = relay_daemon.RelayDaemon(
            self.root / "routes.json", host="127.0.0.1", port=0,
            tls=relay_daemon._server_tls(server["cert_file"], server["key_file"]))
        self.loop = asyncio.new_event_loop()
        self.ready = threading.Event()
        def run():
            asyncio.set_event_loop(self.loop)
            self.loop.run_until_complete(self.daemon.run(self.ready.set, install_signals=False))
        self.daemon_thread = threading.Thread(target=run, daemon=True)
        self.daemon_thread.start()
        self.assertTrue(self.ready.wait(5), "test relay did not bind")
        self.echo = _EchoServer()
        self.bridge = RelayBridge({"control": self.echo.address})
        relay_url = f"wss://127.0.0.1:{self.daemon.bound_port}/data"
        relay_context = ssl.create_default_context(cafile=relay_ca["ca_file"])
        self.target = RelayClient(relay_url=relay_url, identity="relay-target", peer_identity="relay-host",
            link_id="relay-product-test", room_id="relay-pair-test",
            credential=Path(provisioned["credential_files"]["relay-target"]).read_text().strip(),
            ssl_context=relay_context, on_new_stream=self.bridge.serve_stream)
        self.host = RelayClient(relay_url=relay_url, identity="relay-host", peer_identity="relay-target",
            link_id="relay-product-test", room_id="relay-pair-test",
            credential=Path(provisioned["credential_files"]["relay-host"]).read_text().strip(),
            ssl_context=relay_context)
        self.target.start(wait=5)
        self.host.start(wait=5)

    def tearDown(self):
        for client in (getattr(self, "host", None), getattr(self, "target", None)):
            if client is not None:
                with contextlib.suppress(Exception):
                    client.stop()
        with contextlib.suppress(Exception):
            self.bridge.close()
        if getattr(self, "echo", None) is not None:
            self.echo.close()
        if getattr(self, "daemon", None) is not None and getattr(self.daemon, "stop_event", None) is not None:
            self.loop.call_soon_threadsafe(self.daemon.stop_event.set)
        if getattr(self, "daemon_thread", None) is not None:
            self.daemon_thread.join(5)
        if getattr(self, "loop", None) is not None and not self.loop.is_closed():
            self.loop.close()
        self.tmp.cleanup()

    def test_outbound_wss_stream_is_routed_only_to_fixed_loopback_service(self):
        payload = b"inner-tls-record-test"
        with self.host.open_stream(channel="control", timeout=5) as stream:
            stream.settimeout(5)
            stream.sendall(len(payload).to_bytes(4, "big") + payload)
            size = int.from_bytes(_EchoServer._read_exact(stream, 4), "big")
            answer = _EchoServer._read_exact(stream, size)
        self.assertEqual(answer, b"echo:" + payload)
        self.assertEqual(self.echo.calls, [payload])
        with self.assertRaises(RelayTransportError):
            self.host.open_stream(channel="admin", timeout=1)


class RelayStreamDeadlineTests(unittest.TestCase):
    def test_connection_wait_and_stream_open_share_one_timeout_budget(self):
        client = RelayClient(relay_url="wss://relay.example.test:443/data", identity="relay-local",
            peer_identity="relay-peer", link_id="relay-link", room_id="relay-room", credential="test-only")
        client_any = cast(Any, client)
        client_any._loop = object()  # run_coroutine_threadsafe is replaced below; no transport is opened.
        class FakeClock:
            now = 0.0
            def monotonic(self):
                return self.now
        clock = FakeClock()
        class DelayedConnected:
            def wait(self, timeout):
                clock.now += .06
                return True
        class DelayedOpen:
            observed_timeout = None
            def cancel(self):
                return True
            def result(self, timeout):
                self.observed_timeout = timeout
                if timeout < .05:
                    clock.now += max(0, timeout)
                    raise TimeoutError
                clock.now += .05
                return object()
        client_any._connected = DelayedConnected()
        future = DelayedOpen()
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch("invite_rooms.relay_transport.time.monotonic", clock.monotonic))
            stack.enter_context(patch.object(client, "_open_stream", new=lambda _channel: object()))
            stack.enter_context(patch("asyncio.run_coroutine_threadsafe", return_value=future))
            with self.assertRaises(RelayTransportError):
                client.open_stream(channel="control", timeout=.09)
        observed_timeout = future.observed_timeout
        if observed_timeout is None:
            raise AssertionError("event-loop timeout was not passed")
        self.assertLess(observed_timeout, .05)


if __name__ == "__main__":
    unittest.main()
