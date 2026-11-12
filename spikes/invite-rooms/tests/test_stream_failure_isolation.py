"""A rejected per-request socket must not break the shared relay session."""
import asyncio
import base64
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from connector import RelayClient, _Stream


class ClosedStreamIsolation(unittest.IsolatedAsyncioTestCase):
    async def test_closed_app_socket_does_not_abort_dispatch_for_other_streams(self):
        sent = []

        class Client(RelayClient):
            async def _send(self, kind, stream_id, raw=None):
                sent.append((kind, stream_id))

        client = Client(relay_url='ws://127.0.0.1:1/data', identity='target',
                        peer_identity='host', link_id='link', room_id='room', credential='test-only')
        rejected = _Stream(client, 'rejected')
        healthy = _Stream(client, 'healthy')
        client._streams.update(rejected=rejected, healthy=healthy)
        rejected.app_end.close()
        frame = dict(type='DATA', source='host', target='target', link_id='link', room_id='room',
                     stream_id='rejected', payload=base64.b64encode(b'late TLS data').decode())
        try:
            await client._dispatch(frame)
            self.assertIn(('CLOSE', 'rejected'), sent)
            frame.update(stream_id='healthy', payload=base64.b64encode(b'valid next stream').decode())
            await client._dispatch(frame)
            healthy.app_end.settimeout(1)
            self.assertEqual(healthy.app_end.recv(128), b'valid next stream')
            self.assertIs(client._streams['healthy'], healthy)
        finally:
            for stream in (rejected, healthy):
                stream.app_end.close()
                stream.abort()


if __name__ == '__main__':
    unittest.main()
