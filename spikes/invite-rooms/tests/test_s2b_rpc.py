"""Private RPC adapter counterexamples over actual loopback WebSockets.

Fixture responses test protocol classification, NOT native interoperability.
The separate native gate proves compatibility against real isolated Hermes.
"""
import asyncio
import json
import sys
import time
import unittest
from pathlib import Path
import aiohttp
from aiohttp import web
HERE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(HERE), str(HERE.parents[1] / 'server')]


class NativeRpcTests(unittest.IsolatedAsyncioTestCase):
    async def test_correlated_explicit_native_error_is_only_rejection(self):
        from lobby_native_rpc import BoundedNativeRpc, NativeRpcAmbiguous
        from room_lobby_service import NativeRpcRejected
        calls = []
        mode = {'value': 'ok'}
        async def serve(request):
            if request.headers.get('Authorization') != 'Bearer fixture-private':
                return web.Response(status=403)
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            async for frame in ws:
                value = json.loads(frame.data)
                calls.append(value['method'])
                if mode['value'] == 'ok':
                    await ws.send_json({'jsonrpc': '2.0', 'id': value['id'], 'result': {'driver': True}})
                elif mode['value'] == 'reject':
                    await ws.send_json({'jsonrpc': '2.0', 'id': value['id'], 'error': {'code': 4100, 'message': 'SECRET diagnostic', 'data': {'token': 'SECRET'}}})
                else:
                    await ws.send_json({'jsonrpc': '2.0', 'id': 'unrelated', 'error': {'code': 4100, 'message': 'SECRET'}})
            return ws
        runner = web.AppRunner(web.Application(), access_log=None)
        runner.app.router.add_get('/api/ws', serve)
        await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            async with aiohttp.ClientSession(trust_env=False) as session:
                async with session.ws_connect(f'http://127.0.0.1:{port}/api/ws', headers={'Authorization': 'Bearer fixture-private'}) as ws:
                    rpc = BoundedNativeRpc(ws, timeout=.5)
                    self.assertEqual(await rpc('groups.capabilities', {}), {'driver': True})
                    mode['value'] = 'reject'
                    with self.assertRaisesRegex(NativeRpcRejected, '^native_rejected$') as raised:
                        await rpc('groups.state', {'room_id': 'missing'})
                    self.assertNotIn('SECRET', repr(raised.exception))
                    mode['value'] = 'wrong-id'
                    with self.assertRaisesRegex(NativeRpcAmbiguous, '^native_rpc_ambiguous$'):
                        await rpc('groups.state', {'room_id': 'missing'})
                    self.assertEqual(calls, ['groups.capabilities', 'groups.state', 'groups.state'])
        finally:
            await runner.cleanup()

    async def test_bounds_protocol_failures_and_no_automatic_retry(self):
        from lobby_native_rpc import BoundedNativeRpc, NativeRpcAmbiguous, NativeRpcDenied
        calls = []
        mode = {'value': 'oversize'}
        async def serve(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            async for frame in ws:
                value = json.loads(frame.data)
                calls.append(value['method'])
                kind = mode['value']
                if kind == 'slow':
                    await asyncio.sleep(.3)
                elif kind == 'drip':
                    for n in range(10):
                        if ws.closed:
                            break
                        try:
                            await ws.send_json({'jsonrpc': '2.0', 'method': 'event', 'params': {}})
                        except ConnectionError:
                            break
                        await asyncio.sleep(.04)
                elif kind == 'oversize':
                    await ws.send_json({'jsonrpc': '2.0', 'id': value['id'], 'result': {'data': 'x' * (256 * 1024)}})
                elif kind == 'binary':
                    await ws.send_bytes(b'SECRET')
                elif kind == 'malformed-error':
                    await ws.send_json({'jsonrpc': '2.0', 'id': value['id'], 'error': {'code': True, 'message': 'SECRET'}})
                elif kind == 'duplicate':
                    await ws.send_str('{"jsonrpc":"2.0","id":"' + value['id'] + '","result":{},"result":{}}')
                elif kind == 'close':
                    await ws.close()
            return ws
        runner = web.AppRunner(web.Application(), access_log=None)
        runner.app.router.add_get('/api/ws', serve)
        await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            async with aiohttp.ClientSession(trust_env=False) as session:
                async with session.ws_connect(f'http://127.0.0.1:{port}/api/ws') as ws:
                    rpc = BoundedNativeRpc(ws, timeout=.12)
                    for method, params in [('admin.shutdown', {}), ('groups.state', {'url': 'http://evil/'}),
                                           ('groups.state', {'room_id': 'x' * 25000})]:
                        with self.assertRaisesRegex(NativeRpcDenied, '^native_rpc_denied$'):
                            await rpc(method, params)
                    self.assertEqual(calls, [])
                for kind in ('oversize', 'slow', 'drip', 'binary', 'malformed-error', 'duplicate', 'close'):
                    mode['value'] = kind
                    async with session.ws_connect(f'http://127.0.0.1:{port}/api/ws') as ws:
                        rpc = BoundedNativeRpc(ws, timeout=.12)
                        before = len(calls)
                        started = time.monotonic()
                        with self.assertRaisesRegex(NativeRpcAmbiguous, '^native_rpc_ambiguous$'):
                            await rpc('groups.state', {'room_id': 'missing'})
                        self.assertLess(time.monotonic() - started, .8)
                        with self.assertRaisesRegex(NativeRpcAmbiguous, '^native_rpc_ambiguous$'):
                            await rpc('groups.state', {'room_id': 'missing'})
                        self.assertEqual(len(calls), before + 1)
        finally:
            await runner.cleanup()
