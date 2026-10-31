"""Canonical bounded native RPC — protocol tests over real loopback WebSockets.

Verifies the promoted server/invite_rooms/native_rpc.py preserves the accepted
spike behavior: method/field allowlist, strict JSON, byte/frame/time budget,
poison semantics, explicit rejection vs post-admission ambiguity, cancellation,
and normal/hostile idle backlog.  The spike shim must re-export the same classes.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
import unittest
from pathlib import Path

import aiohttp
from aiohttp import web

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from invite_rooms.native_rpc import (
    BoundedNativeRpc,
    NativeRpcAmbiguous,
    NativeRpcDenied,
    NativeRpcRejected,
    MAX_REQUEST,
    MAX_RESPONSE,
    _FIELDS,
)
from room_lobby_service import AdmissionDenied


class CanonicalImportTests(unittest.TestCase):
    """The canonical module is the single implementation; spike shim re-exports it."""

    def test_spike_shim_reexports_same_objects(self):
        # Ensure the spike path is available for the shim import.
        spike_dir = Path(__file__).resolve().parents[2] / "spikes" / "invite-rooms"
        server_dir = Path(__file__).resolve().parents[1]
        orig = sys.path[:]
        sys.path.insert(0, str(spike_dir))
        sys.path.insert(0, str(server_dir))
        try:
            import lobby_native_rpc as shim
            self.assertIs(shim.BoundedNativeRpc, BoundedNativeRpc)
            self.assertIs(shim.NativeRpcAmbiguous, NativeRpcAmbiguous)
            self.assertIs(shim.NativeRpcDenied, NativeRpcDenied)
        finally:
            sys.path[:] = orig

    def test_allowlist_is_fixed_native_methods(self):
        expected = {
            "groups.capabilities", "groups.state", "groups.log",
            "groups.create", "groups.peer.invite", "groups.peer.register",
            "groups.peer.revoke", "groups.send", "groups.disband",
        }
        self.assertEqual(set(_FIELDS), expected)

    def test_rejected_inherits_invite_error(self):
        from room_invite_store import InviteError
        self.assertTrue(issubclass(NativeRpcRejected, InviteError))

    def test_grant_is_not_in_readonly_fields(self):
        # grant is only required/allowed for mutation methods, never read-only ones.
        readonly = ("groups.capabilities", "groups.state", "groups.log")
        for method in readonly:
            required, allowed = _FIELDS[method]
            self.assertNotIn("grant", required)
            self.assertNotIn("grant", allowed)
        # grant IS required for groups.peer.register (native material push)
        self.assertIn("grant", _FIELDS["groups.peer.register"][0])


class BoundedRpcProtocolTests(unittest.IsolatedAsyncioTestCase):
    """Core protocol: rejection vs ambiguity, bounds, poison, allowlist."""

    async def _serve(self, handler):
        runner = web.AppRunner(web.Application(), access_log=None)
        runner.app.router.add_get("/api/ws", handler)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        self.addAsyncCleanup(runner.cleanup)
        return site._server.sockets[0].getsockname()[1]

    async def test_explicit_rejection_is_not_ambiguity(self):
        calls = []
        mode = {"v": "ok"}

        async def handler(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            async for frame in ws:
                val = json.loads(frame.data)
                calls.append(val["method"])
                if mode["v"] == "ok":
                    await ws.send_json({"jsonrpc": "2.0", "id": val["id"], "result": {"driver": True}})
                elif mode["v"] == "reject":
                    await ws.send_json({"jsonrpc": "2.0", "id": val["id"],
                                        "error": {"code": 4100, "message": "SECRET", "data": {"t": "SECRET"}}})
                else:
                    await ws.send_json({"jsonrpc": "2.0", "id": "wrong", "result": {}})
            return ws

        port = await self._serve(handler)
        async with aiohttp.ClientSession(trust_env=False) as session:
            async with session.ws_connect(f"http://127.0.0.1:{port}/api/ws") as ws:
                rpc = BoundedNativeRpc(ws, timeout=1)
                self.assertEqual(await rpc("groups.capabilities", {}), {"driver": True})
                mode["v"] = "reject"
                with self.assertRaisesRegex(NativeRpcRejected, "^native_rejected$") as raised:
                    await rpc("groups.state", {"room_id": "x"})
                self.assertNotIn("SECRET", repr(raised.exception))
                # After rejection (not poison), adapter is still usable.
                mode["v"] = "ok"
                self.assertEqual(await rpc("groups.capabilities", {}), {"driver": True})
                self.assertEqual(calls, ["groups.capabilities", "groups.state", "groups.capabilities"])

    async def test_wrong_id_response_poisons_and_blocks_future_calls(self):
        async def handler(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            async for frame in ws:
                val = json.loads(frame.data)
                await ws.send_json({"jsonrpc": "2.0", "id": "stale", "result": {}})
            return ws

        port = await self._serve(handler)
        async with aiohttp.ClientSession(trust_env=False) as session:
            async with session.ws_connect(f"http://127.0.0.1:{port}/api/ws") as ws:
                rpc = BoundedNativeRpc(ws, timeout=1)
                with self.assertRaisesRegex(NativeRpcAmbiguous, "^native_rpc_ambiguous$"):
                    await rpc("groups.capabilities", {})
                # Poisoned: subsequent calls also fail without sending.
                with self.assertRaisesRegex(NativeRpcAmbiguous, "^native_rpc_ambiguous$"):
                    await rpc("groups.capabilities", {})

    async def test_denied_for_unknown_method_and_bad_fields(self):
        async def handler(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            async for frame in ws:
                pass
            return ws

        port = await self._serve(handler)
        async with aiohttp.ClientSession(trust_env=False) as session:
            async with session.ws_connect(f"http://127.0.0.1:{port}/api/ws") as ws:
                rpc = BoundedNativeRpc(ws, timeout=0.5)
                for method, params in [
                    ("admin.shutdown", {}),
                    ("groups.state", {"url": "http://evil/"}),
                    ("groups.state", {"room_id": "x" * 25000}),
                    ("groups.state", {"room_id": "ok", "extra": "bad"}),
                    ("groups.capabilities", {"extra": "bad"}),
                ]:
                    with self.assertRaisesRegex(NativeRpcDenied, "^native_rpc_denied$"):
                        await rpc(method, params)

    async def test_oversize_response_poisons(self):
        async def handler(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            async for frame in ws:
                val = json.loads(frame.data)
                await ws.send_json({"jsonrpc": "2.0", "id": val["id"], "result": {"d": "x" * (256 * 1024)}})
            return ws

        port = await self._serve(handler)
        async with aiohttp.ClientSession(trust_env=False) as session:
            async with session.ws_connect(f"http://127.0.0.1:{port}/api/ws") as ws:
                rpc = BoundedNativeRpc(ws, timeout=0.5)
                with self.assertRaisesRegex(NativeRpcAmbiguous, "^native_rpc_ambiguous$"):
                    await rpc("groups.state", {"room_id": "x"})
                with self.assertRaisesRegex(NativeRpcAmbiguous, "^native_rpc_ambiguous$"):
                    await rpc("groups.state", {"room_id": "x"})

    async def test_timeout_poisons_after_send(self):
        async def handler(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            async for frame in ws:
                await asyncio.sleep(2)  # exceed timeout
            return ws

        port = await self._serve(handler)
        async with aiohttp.ClientSession(trust_env=False) as session:
            async with session.ws_connect(f"http://127.0.0.1:{port}/api/ws") as ws:
                rpc = BoundedNativeRpc(ws, timeout=0.15)
                with self.assertRaisesRegex(NativeRpcAmbiguous, "^native_rpc_ambiguous$"):
                    await rpc("groups.capabilities", {})
                with self.assertRaisesRegex(NativeRpcAmbiguous, "^native_rpc_ambiguous$"):
                    await rpc("groups.capabilities", {})

    async def test_cancellation_after_send_poisons(self):
        async def handler(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            async for frame in ws:
                await asyncio.sleep(10)
            return ws

        port = await self._serve(handler)
        async with aiohttp.ClientSession(trust_env=False) as session:
            async with session.ws_connect(f"http://127.0.0.1:{port}/api/ws") as ws:
                rpc = BoundedNativeRpc(ws, timeout=5)
                with self.assertRaises(asyncio.CancelledError):
                    task = asyncio.create_task(rpc("groups.capabilities", {}))
                    await asyncio.sleep(0.1)
                    task.cancel()
                    await task
                with self.assertRaisesRegex(NativeRpcAmbiguous, "^native_rpc_ambiguous$"):
                    await rpc("groups.capabilities", {})

    async def test_strict_json_no_nan_no_duplicates(self):
        async def handler(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            async for frame in ws:
                val = json.loads(frame.data)
                # Reply with a duplicate-key frame to test strict parsing.
                await ws.send_str('{"jsonrpc":"2.0","id":"' + val["id"] + '","result":{},"result":{}}')
            return ws

        port = await self._serve(handler)
        async with aiohttp.ClientSession(trust_env=False) as session:
            async with session.ws_connect(f"http://127.0.0.1:{port}/api/ws") as ws:
                rpc = BoundedNativeRpc(ws, timeout=0.5)
                with self.assertRaisesRegex(NativeRpcAmbiguous, "^native_rpc_ambiguous$"):
                    await rpc("groups.capabilities", {})


class IdleBacklogTests(unittest.IsolatedAsyncioTestCase):
    """Normal and hostile idle backlog behavior."""

    BACKLOG = 40

    async def _serve(self, preamble):
        self.calls = []
        self.pushed = asyncio.Event()

        async def handler(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            for f in preamble:
                await ws.send_str(f)
            self.pushed.set()
            async for frame in ws:
                val = json.loads(frame.data)
                self.calls.append(val["method"])
                await ws.send_json({"jsonrpc": "2.0", "id": val["id"], "result": {"driver": True}})
            return ws

        self.runner = web.AppRunner(web.Application(), access_log=None)
        self.runner.app.router.add_get("/api/ws", handler)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.addAsyncCleanup(self.runner.cleanup)
        return site._server.sockets[0].getsockname()[1]

    async def _idle_until_buffered(self, ws, count):
        await asyncio.wait_for(self.pushed.wait(), 5)
        for _ in range(200):
            if len(getattr(ws._reader, "_buffer", ())) >= count:
                return
            await asyncio.sleep(0.01)
        self.fail("backlog never reached the client reader")

    async def test_normal_idle_backlog_drained_without_consuming_budget(self):
        note = json.dumps({"jsonrpc": "2.0", "method": "event", "params": {"type": "x", "payload": {}}})
        port = await self._serve([note] * self.BACKLOG)
        async with aiohttp.ClientSession(trust_env=False) as session:
            async with session.ws_connect(f"http://127.0.0.1:{port}/api/ws") as ws:
                await self._idle_until_buffered(ws, self.BACKLOG)
                rpc = BoundedNativeRpc(ws, timeout=2)
                self.assertEqual(await rpc("groups.capabilities", {}), {"driver": True})
                self.assertEqual(rpc.drained_notifications, self.BACKLOG)
                # Adapter stays usable.
                self.assertEqual(await rpc("groups.capabilities", {}), {"driver": True})
                self.assertEqual(self.calls, ["groups.capabilities", "groups.capabilities"])

    async def test_stale_response_before_request_poisons(self):
        stale = json.dumps({"jsonrpc": "2.0", "id": "late", "result": {}})
        port = await self._serve([stale])
        async with aiohttp.ClientSession(trust_env=False) as session:
            async with session.ws_connect(f"http://127.0.0.1:{port}/api/ws") as ws:
                await self._idle_until_buffered(ws, 1)
                rpc = BoundedNativeRpc(ws, timeout=2)
                with self.assertRaisesRegex(NativeRpcAmbiguous, "^native_rpc_ambiguous$"):
                    await rpc("groups.capabilities", {})
                self.assertEqual(self.calls, [])

    async def test_flood_idle_backlog_trips_drain_budget(self):
        # 300 notifications exceeds _DRAIN_MAX_FRAMES (256).
        note = json.dumps({"jsonrpc": "2.0", "method": "flood", "params": {}})
        port = await self._serve([note] * 300)
        async with aiohttp.ClientSession(trust_env=False) as session:
            async with session.ws_connect(f"http://127.0.0.1:{port}/api/ws") as ws:
                await self._idle_until_buffered(ws, 300)
                rpc = BoundedNativeRpc(ws, timeout=2)
                with self.assertRaisesRegex(NativeRpcAmbiguous, "^native_rpc_ambiguous$"):
                    await rpc("groups.capabilities", {})
                self.assertEqual(self.calls, [])


class InviteAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_invite_gate_runs_after_idle_drain_and_blocks_frame(self):
        sent = []
        class WS:
            async def receive(self, timeout=None):
                raise TimeoutError()
            async def send_str(self, value):
                sent.append(value)
            async def close(self):
                return None
        rpc = BoundedNativeRpc(WS(), timeout=1, invite_admission=lambda: False)
        from room_lobby_service import AdmissionDenied
        with self.assertRaises(AdmissionDenied):
            await rpc("groups.peer.invite", {"profile": "p", "room_id": "r",
                                              "home_install_id": "h", "authority_gateway_id": "h",
                                              "authority_epoch": 1, "member_id": "m",
                                              "grant_id": "g", "ttl_seconds": 1})
        self.assertEqual(sent, [])


class LiveSocketCutoverTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_websocket_lock_drain_cutover_sends_no_invite(self):
        observed = []
        app = web.Application()

        async def handler(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            async for message in ws:
                observed.append(message.data)
                await ws.send_json({"jsonrpc": "2.0", "id": json.loads(message.data)["id"],
                                    "result": {"ok": True}})
            return ws

        app.router.add_get("/api/ws", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            async with aiohttp.ClientSession(trust_env=False) as session:
                async with session.ws_connect(f"http://127.0.0.1:{port}/api/ws") as ws:
                    admitted = True
                    rpc = BoundedNativeRpc(ws, timeout=1, invite_admission=lambda: admitted)
                    await rpc._lock.acquire()
                    pending = asyncio.create_task(rpc("groups.peer.invite", {
                        "room_id": "r", "home_install_id": "h",
                        "authority_gateway_id": "h", "authority_epoch": 1,
                        "member_id": "m", "grant_id": "g", "ttl_seconds": 1,
                    }))
                    await asyncio.sleep(0)
                    # Cutover occurs while the real client is queued on its lock;
                    # the final admission is checked after lock/idle-drain and
                    # before the websocket frame is emitted.
                    admitted = False
                    rpc._lock.release()
                    with self.assertRaises(AdmissionDenied):
                        await pending
                    self.assertEqual(observed, [])
        finally:
            await runner.cleanup()


if __name__ == "__main__":
    unittest.main()