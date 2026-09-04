import asyncio
import os
import socket
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import terminal_server

try:
    from websockets.asyncio.client import connect
except ImportError:  # pragma: no cover - optional in system test Python
    connect = None


@unittest.skipUnless(connect is not None, "websockets is required for PTY integration tests")
class TerminalServerIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["MISSION_CONTROL_TOKEN"] = "terminal-e2e-token"
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            cls.port = sock.getsockname()[1]
        cls.server_thread = terminal_server.start_terminal_server(port=cls.port)

    @classmethod
    def tearDownClass(cls):
        cls.server_thread.stop()  # type: ignore[attr-defined]
        cls.server_thread.join(timeout=2)

    def test_bash_input_output_and_resize(self):
        async def run():
            ticket = terminal_server.issue_ticket("terminal-e2e-token")
            self.assertIsNotNone(ticket)
            async with connect(f"ws://127.0.0.1:{self.port}/?ticket={ticket}") as ws:
                await ws.send("\x1b[RESIZE:120;40]")
                await ws.send(b"printf 'TERMINAL_E2E_OK\\n'")
                output = bytearray()
                for _ in range(20):
                    data = await asyncio.wait_for(ws.recv(), timeout=1)
                    output.extend(data if isinstance(data, bytes) else data.encode())
                    if b"TERMINAL_E2E_OK" in output:
                        break
                self.assertIn(b"TERMINAL_E2E_OK", output)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
