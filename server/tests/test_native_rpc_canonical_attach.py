"""Attach contract: real private owner socket + loopback WS, not native E2E.

Core discovery, ticket acquisition and WS target helpers run unchanged. Only the
owner responder is a disposable protocol fixture; no gateway/model is started.
"""
from __future__ import annotations
import asyncio
import json
import os
from pathlib import Path

import socket
import tempfile
import threading

import unittest
from unittest.mock import patch

from aiohttp import web
from hermes_cli import gateway_runtime as gr
from hermes_cli.gateway_client import GATEWAY_WS_PROTOCOL, GATEWAY_WS_TICKET_PREFIX
from gateway.runtime_bootstrap import TicketStore
from invite_rooms.service import ServiceError
from invite_rooms import routes, runtime_config as rc
from invite_rooms.native_rpc import BoundedNativeRpc
from invite_rooms.native_rpc import NativeRpcDenied
from room_lobby_service import REGISTER_ADMISSION, AdmissionDenied


class CanonicalAttachTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='g1-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.root.chmod(0o700)
        (self.root / 'config.yaml').write_text('{}')
        (self.root / 'install_id').write_text('b' * 32 + '\n')
        (self.root / 'install_id').chmod(0o600)
        self.home = self.root / 'profiles' / 'research'
        self.home.mkdir(parents=True, mode=0o700)
        self.config = {'profile_home': self.home, 'profile': 'research', 'installation_id': 'install:' + 'b' * 32}
        self.state = 'ready'
        self.instance = 'fixture-instance-1'
        self.runtime_protocol = 1
        self.profile_id = str(self.home)
        self.protocol = GATEWAY_WS_PROTOCOL
        self.bad_ticket = False
        self.ticket_mode = 'fresh'
        self.grant_mismatch = False
        self.redirect = False
        self.calls, self.requests, self.used = [], [], []
        self.ticket_store = TicketStore(self.instance, frozenset({str(self.home)}))
        self.stop = threading.Event()
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.bind(str(self.root / 'gateway.sock'))
        (self.root / 'gateway.sock').chmod(0o600)
        self.sock.listen()
        self.sock.settimeout(0.1)
        self.thread = threading.Thread(target=self.owner, daemon=True)
        self.thread.start()
        app = web.Application()
        app.router.add_get('/api/ws', self.websocket)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, '127.0.0.1', 0)
        await site.start()
        self.origin = 'http://127.0.0.1:' + str(site._server.sockets[0].getsockname()[1])
        self.ensure = patch.object(gr, 'ensure_gateway_runtime', side_effect=AssertionError('must never ensure'))
        self.ensure_mock = self.ensure.start()
        self.addCleanup(self.ensure.stop)

    def owner(self):
        while not self.stop.is_set():
            try:
                peer, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with peer:
                peer.settimeout(1)
                data = b''
                while b'\n' not in data:
                    chunk = peer.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                if not data:
                    continue
                req = json.loads(data)
                self.requests.append(req)
                if req['verb'] == 'identify':
                    result = {'runtime_protocol': self.runtime_protocol, 'state': self.state,
                              'served_profiles': [{'home': str(self.home), 'profile_id': self.profile_id}],
                              'instance_id': self.instance, 'authority_epoch': 1, 'api_origin': self.origin,
                              'supervisor': 'external', 'capabilities': ['session-authority-v1']}
                else:
                    params = req['params']
                    self.ticket_store.TTL_SECONDS = 0 if self.bad_ticket else 30
                    ticket = self.ticket_store.mint(profile_id=params['profile_id'], subject='fixture-owner', purpose=params['purpose'])
                    if self.ticket_mode == 'reused':
                        self.ticket_store.redeem(ticket, profile_id=None, purpose='interactive')
                    elif self.ticket_mode == 'absent':
                        ticket = ''
                    result = {'runtime_protocol': 1, 'profile_id': params['profile_id'],
                              'instance_id': 'changed' if self.grant_mismatch else self.instance, 'ticket': ticket}
                peer.sendall(json.dumps({'protocol': 1, 'id': 1, 'ok': True, 'result': result}).encode() + b'\n')

    async def websocket(self, request):
        self.assertNotIn('Authorization', request.headers)
        offered = [s.strip() for s in request.headers.get('Sec-WebSocket-Protocol', '').split(',')]
        values = [s[len(GATEWAY_WS_TICKET_PREFIX):] for s in offered if s.startswith(GATEWAY_WS_TICKET_PREFIX)]
        if len(values) != 1:
            raise web.HTTPForbidden()
        if self.redirect:
            raise web.HTTPFound(self.redirect_origin + '/redirect-must-not-be-followed')
        ticket = values[0]
        try:
            grant = self.ticket_store.redeem(ticket, profile_id=None, purpose='interactive')
        except PermissionError:
            raise web.HTTPForbidden() from None
        if (grant['profile_id'], grant['instance_id']) != (str(self.home), self.instance):
            raise web.HTTPForbidden()
        self.used.append(ticket)
        ws = web.WebSocketResponse(protocols=[self.protocol])
        await ws.prepare(request)
        async for frame in ws:
            value = json.loads(frame.data)
            self.calls.append(value)
            await ws.send_json({'jsonrpc': '2.0', 'id': value['id'], 'result': {'profile': 'research'}})
        return ws

    async def asyncTearDown(self):
        await self.runner.cleanup()
        self.stop.set()
        self.sock.close()
        await asyncio.to_thread(self.thread.join, 2)
        self.assertFalse(self.thread.is_alive())
        self.ensure_mock.assert_not_called()

    async def attach(self):
        async with routes._connect_native_rpc(self.config) as rpc:
            self.assertIsInstance(rpc, BoundedNativeRpc)
            return await rpc('groups.capabilities', {'profile': 'research'})

    async def test_exact_nondefault_profile_fresh_single_use_ticket_each_attach(self):
        discovery = await asyncio.to_thread(gr.discover_gateway_endpoint, self.home, timeout=2)
        self.assertEqual(discovery.state, 'ready', repr(discovery))
        for _ in range(2):
            self.assertEqual(await self.attach(), {'profile': 'research'})
        self.assertEqual(len(set(self.used)), 2)
        reqs = [r['params'] for r in self.requests if r['verb'] == 'session-ticket']
        self.assertEqual(reqs, [{'profile_id': str(self.home), 'instance_id': self.instance, 'purpose': 'interactive'}] * 2)
        self.assertEqual(len(self.calls), 2)
        self.assertFalse(self.ticket_store.outstanding())

    async def test_not_ready_states_refuse_before_ticket_and_effect(self):
        for state in ('starting', 'draining', 'conflict', 'absent'):
            with self.subTest(state=state):
                self.state = state
                with self.assertRaises(Exception):
                    await self.attach()
        self.assertFalse(any(r['verb'] == 'session-ticket' for r in self.requests))
        self.assertEqual(self.calls, [])

    async def test_default_profile_uses_its_explicit_installation_root(self):
        self.home = self.root
        self.profile_id = str(self.root)
        self.ticket_store = TicketStore(self.instance, frozenset({str(self.root)}))
        config = dict(self.config, profile_home=self.root, profile='default')
        async with routes._connect_native_rpc(config) as rpc:
            await rpc('groups.capabilities', {'profile': 'default'})
        self.assertEqual(self.calls[0]['params'], {'profile': 'default'})
        self.assertEqual(self.requests[-1]['params']['profile_id'], str(self.root))

    async def test_canonical_transport_preserves_final_invite_and_register_admission(self):
        async with routes._connect_native_rpc(self.config) as rpc:
            register = dict(room_id='fixture-room', member_id='member', target_url='http://127.0.0.1:1/links/fixture',
                            target_profile='research', grant='fixture-not-a-grant', catalog={})
            with self.assertRaises(NativeRpcDenied):
                await rpc('groups.peer.register', register)
            checks = []

            def prepare():
                checks.append('worker')

                def final():
                    checks.append('final')
                    raise AdmissionDenied()

                return final

            token = REGISTER_ADMISSION.set(prepare)
            try:
                with self.assertRaises(AdmissionDenied):
                    await rpc('groups.peer.register', register)
            finally:
                REGISTER_ADMISSION.reset(token)
            self.assertEqual(checks, ['worker', 'final'])
            invite = dict(room_id='fixture-room', home_install_id='fixture-home', authority_gateway_id='fixture-owner',
                          authority_epoch=1, member_id='member')
            with self.assertRaises(AdmissionDenied):
                await rpc('groups.peer.invite', invite, invite_admission=lambda: False)
            self.assertEqual(self.calls, [])
            # A pre-send denial does not poison or replace the bounded stream.
            await rpc('groups.capabilities', {'profile': 'research'})
            self.assertEqual(len(self.calls), 1)

    async def test_wrong_profile_and_runtime_protocol_refuse(self):
        for field, value in [('profile_id', str(self.root)), ('runtime_protocol', 2)]:
            old = getattr(self, field)
            setattr(self, field, value)
            with self.assertRaises(Exception):
                await self.attach()
            setattr(self, field, old)
        self.assertFalse(any(r['verb'] == 'session-ticket' for r in self.requests))
        self.assertEqual(self.calls, [])

    async def test_nonloopback_and_noncanonical_origins_refuse(self):
        for value in ('http://192.0.2.1:1234', 'http://localhost:1234', 'http://127.0.0.1:1234/path', 'http://user@127.0.0.1:1234'):
            self.origin = value
            with self.assertRaises(Exception):
                await self.attach()
        self.assertEqual(self.calls, [])

    async def test_changed_instance_ticket_and_expired_ticket_refuse(self):
        self.grant_mismatch = True
        with self.assertRaises(Exception):
            await self.attach()
        self.grant_mismatch = False
        self.bad_ticket = True
        with self.assertRaises(Exception):
            await self.attach()
        self.assertEqual(self.calls, [])

    async def test_wrong_negotiated_protocol_refuses_without_rpc(self):
        self.protocol = 'not-the-native-protocol'
        with self.assertLogs('aiohttp.websocket', level='WARNING'), self.assertRaisesRegex(ServiceError, '^native_unavailable$'):
            await self.attach()
        self.assertEqual(self.calls, [])

    async def test_absent_and_reused_native_tickets_refuse_without_effect(self):
        for mode in ('absent', 'reused'):
            self.ticket_mode = mode
            with self.subTest(mode=mode), self.assertRaises(Exception):
                await self.attach()
        self.assertEqual(self.calls, [])

    async def test_redirect_is_not_followed_or_downgraded(self):
        followed = []

        async def redirected(request):
            followed.append(request.path)
            return web.Response()

        # A second owner-owned loopback listener is the redirect destination;
        # even that endpoint may never receive the native credential.
        app = web.Application()
        app.router.add_get('/redirect-must-not-be-followed', redirected)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        self.addAsyncCleanup(runner.cleanup)
        site = web.TCPSite(runner, '127.0.0.1', 0)
        await site.start()
        destination = 'http://127.0.0.1:' + str(site._server.sockets[0].getsockname()[1])
        self.redirect = True
        # Keep discovery pointed at the original WS; redirect target set separately.
        self.redirect_origin = destination
        with self.assertRaisesRegex(ServiceError, '^native_unavailable$'):
            await self.attach()
        self.assertEqual(followed, [])
        self.assertEqual(self.calls, [])

    async def test_home_or_install_id_permissions_changed_after_load_refuse(self):
        self.home.chmod(0o755)
        with self.assertRaisesRegex(ServiceError, '^native_unavailable$'):
            await self.attach()
        self.home.chmod(0o700)
        (self.root / 'install_id').unlink()
        with self.assertRaisesRegex(ServiceError, '^native_unavailable$'):
            await self.attach()
        self.assertEqual(self.requests, [])

    async def test_unsafe_and_overlay_locks_refuse_without_chmod_or_alias(self):
        lock = self.root / 'gateway.lock'
        lock.write_text('fixture-lock')
        lock.chmod(0o644)
        with self.assertRaisesRegex(ServiceError, '^native_unavailable$'):
            await self.attach()
        self.assertEqual(lock.stat().st_mode & 0o777, 0o644)
        lock.chmod(0o600)
        (self.home / 'gateway.lock').symlink_to(lock)
        with self.assertRaisesRegex(ServiceError, '^native_unavailable$'):
            await self.attach()
        self.assertEqual(self.requests, [])

    async def test_refused_owner_socket_no_spawn_or_legacy_fallback(self):
        self.sock.close()
        with self.assertRaises(Exception):
            await self.attach()
        self.assertEqual(self.calls, [])

    async def test_wrong_home_profile_installation_and_symlink_fail_closed(self):
        bad = [dict(self.config, profile='default'), dict(self.config, installation_id='install:' + 'a' * 32),
               dict(self.config, profile_home=self.root / 'missing')]
        alias = self.root / 'alias'
        alias.symlink_to(self.home, target_is_directory=True)
        bad.append(dict(self.config, profile_home=alias))
        for config in bad:
            with self.subTest(config=config), self.assertRaises(Exception):
                async with routes._connect_native_rpc(config):
                    self.fail('must not yield')
        self.assertEqual(self.requests, [])

    def test_private_loader_accepts_only_mutually_exclusive_home_mode(self):
        cfg = self.root / 'runtime.json'
        peers = self.root / 'peers.json'
        peers.write_text('[]')
        peers.chmod(0o600)
        base = {'store_path': str(self.root / 'invites.sqlite'), 'installation_id': self.config['installation_id'],
                'profile': 'research', 'peer_registry_path': str(peers)}
        for raw, valid in [({'profile_home': str(self.home)}, True),
                           ({'profile_home': str(self.home), 'endpoint': self.origin}, False),
                           ({'profile_home': 'relative'}, False), ({'profile_home': str(self.root)}, False)]:
            cfg.write_text(json.dumps(dict(base, native_rpc=raw)))
            cfg.chmod(0o600)
            loaded = rc._load_config_file(str(cfg))
            self.assertEqual(loaded is not None, valid)
            if valid:
                self.assertEqual(loaded.native_rpc, self.config)


if __name__ == '__main__':
    unittest.main()
