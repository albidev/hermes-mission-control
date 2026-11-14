"""Closed peer after a hostile route must not cause a second send failure."""
import asyncio
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from relay import Relay, RelayWebSocketServer
from websockets.exceptions import ConnectionClosedError
from websockets.frames import Close


class ClosedRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_error_on_closed_socket_is_expected_disconnect(self):
        # Transport boundary unit; actual socket adversaries live in test_s2d_control.
        ws = AsyncMock()
        ws.path = '/data'
        ws.request = None
        ws.recv.return_value = '{"type":"hostile"}'
        ws.send.side_effect = ConnectionClosedError(Close(1008, 'closed'), Close(1008, 'closed'), True)
        await RelayWebSocketServer(Relay()).handler(ws)
        self.assertEqual(ws.send.await_count, 1)

    async def test_other_send_failure_is_not_suppressed(self):
        ws = AsyncMock()
        ws.path = '/data'
        ws.request = None
        ws.recv.return_value = '{"type":"hostile"}'
        ws.send.side_effect = RuntimeError('non-connection-failure')
        with self.assertRaisesRegex(RuntimeError, 'non-connection-failure'):
            await RelayWebSocketServer(Relay()).handler(ws)


if __name__ == '__main__':
    unittest.main()
