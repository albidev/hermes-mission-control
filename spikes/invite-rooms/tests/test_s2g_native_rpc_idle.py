"""Idle backlog on the bounded native RPC socket (found by gate_s2g q5 after a 10 min wait).

The native /api/ws pushes surface-global notifications (sessions.changed,
platforms.changed, ...) to every connected client. An owner adapter that sits
idle accumulates them in the client reader; they predate the next request and
must not consume its 16-frame response budget. Anything that is not a
notification before a request is still a protocol violation that poisons.
"""
import asyncio
import json
import sys
import unittest
from pathlib import Path
import aiohttp
from aiohttp import web
HERE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(HERE), str(HERE.parents[1] / 'server')]

BACKLOG = 40


class IdleBacklogTests(unittest.IsolatedAsyncioTestCase):
    async def serve(self, preamble):
        self.calls = []
        self.pushed = asyncio.Event()

        async def handler(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            for frame in preamble:
                await ws.send_str(frame)
            self.pushed.set()
            async for frame in ws:
                value = json.loads(frame.data)
                self.calls.append(value['method'])
                await ws.send_json({'jsonrpc': '2.0', 'id': value['id'], 'result': {'driver': True}})
            return ws
        self.runner = web.AppRunner(web.Application(), access_log=None)
        self.runner.app.router.add_get('/api/ws', handler)
        await self.runner.setup()
        site = web.TCPSite(self.runner, '127.0.0.1', 0)
        await site.start()
        self.addAsyncCleanup(self.runner.cleanup)
        return site._server.sockets[0].getsockname()[1]

    async def idle_until_buffered(self, ws, count):
        await asyncio.wait_for(self.pushed.wait(), 5)
        # Let the client protocol move every pushed frame into its reader queue.
        for _ in range(200):
            if len(getattr(ws._reader, '_buffer', ())) >= count:
                return
            await asyncio.sleep(.01)
        self.fail('backlog never reached the client reader')

    async def test_idle_notification_backlog_does_not_consume_response_budget(self):
        from lobby_native_rpc import BoundedNativeRpc
        note = json.dumps({'jsonrpc': '2.0', 'method': 'event', 'params': {'type': 'platforms.changed', 'payload': {}}})
        port = await self.serve([note] * BACKLOG)
        async with aiohttp.ClientSession(trust_env=False) as session:
            async with session.ws_connect(f'http://127.0.0.1:{port}/api/ws') as ws:
                await self.idle_until_buffered(ws, BACKLOG)
                rpc = BoundedNativeRpc(ws, timeout=2)
                self.assertEqual(await rpc('groups.capabilities', {}), {'driver': True})
                self.assertEqual(rpc.drained_notifications, BACKLOG)
                # Adapter stays usable: the next call is a normal round trip.
                self.assertEqual(await rpc('groups.capabilities', {}), {'driver': True})
                self.assertEqual(self.calls, ['groups.capabilities', 'groups.capabilities'])

    async def test_stale_response_before_request_still_poisons_without_sending(self):
        from lobby_native_rpc import BoundedNativeRpc, NativeRpcAmbiguous
        stale = json.dumps({'jsonrpc': '2.0', 'id': 'late-from-earlier-call', 'result': {}})
        port = await self.serve([stale])
        async with aiohttp.ClientSession(trust_env=False) as session:
            async with session.ws_connect(f'http://127.0.0.1:{port}/api/ws') as ws:
                await self.idle_until_buffered(ws, 1)
                rpc = BoundedNativeRpc(ws, timeout=2)
                with self.assertRaisesRegex(NativeRpcAmbiguous, '^native_rpc_ambiguous$'):
                    await rpc('groups.capabilities', {})
                with self.assertRaisesRegex(NativeRpcAmbiguous, '^native_rpc_ambiguous$'):
                    await rpc('groups.capabilities', {})
                self.assertEqual(self.calls, [])  # nothing was ever admitted natively

    async def test_malformed_idle_frame_poisons_without_sending(self):
        from lobby_native_rpc import BoundedNativeRpc, NativeRpcAmbiguous
        port = await self.serve(['{"jsonrpc":"2.0","method":"event","method":"dup"}'])
        async with aiohttp.ClientSession(trust_env=False) as session:
            async with session.ws_connect(f'http://127.0.0.1:{port}/api/ws') as ws:
                await self.idle_until_buffered(ws, 1)
                rpc = BoundedNativeRpc(ws, timeout=2)
                with self.assertRaisesRegex(NativeRpcAmbiguous, '^native_rpc_ambiguous$'):
                    await rpc('groups.capabilities', {})
                self.assertEqual(self.calls, [])


if __name__ == '__main__':
    unittest.main()
