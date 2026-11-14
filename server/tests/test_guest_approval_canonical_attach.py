"""Guest approval transport contract, not native gateway/model E2E.

Reuse the disposable owner socket fixture and unchanged Core ticket helpers.
No production profile, credential or model dispatch is involved.
"""
import json
import unittest

from aiohttp import web
from gateway.runtime_bootstrap import TicketStore
from hermes_cli.gateway_client import GATEWAY_WS_PROTOCOL, GATEWAY_WS_TICKET_PREFIX
import test_native_rpc_canonical_attach as attach_fixture
from invite_rooms.guest import GuestServiceError
from invite_rooms.guest_approvals import ApprovalHandles, GuestApprovalAdapter


class GuestCanonicalAttachTests(unittest.IsolatedAsyncioTestCase):
    # Reuse fixture lifecycle only, not its tests (counts stay independent).
    asyncSetUp = attach_fixture.CanonicalAttachTests.asyncSetUp
    asyncTearDown = attach_fixture.CanonicalAttachTests.asyncTearDown
    owner = attach_fixture.CanonicalAttachTests.owner

    async def websocket(self, request):
        self.assertNotIn('Authorization', request.headers)
        offered = [s.strip() for s in request.headers.get('Sec-WebSocket-Protocol', '').split(',')]
        tickets = [s[len(GATEWAY_WS_TICKET_PREFIX):] for s in offered if s.startswith(GATEWAY_WS_TICKET_PREFIX)]
        if len(tickets) != 1:
            raise web.HTTPForbidden()
        try:
            grant = self.ticket_store.redeem(tickets[0], profile_id=None, purpose='interactive')
        except PermissionError:
            raise web.HTTPForbidden() from None
        self.assertEqual((grant['profile_id'], grant['instance_id']), (str(self.home), self.instance))
        self.used.append(tickets[0])
        ws = web.WebSocketResponse(protocols=[self.protocol])
        await ws.prepare(request)
        await ws.send_json({'jsonrpc': '2.0', 'method': 'event', 'params': {
            'type': 'gateway.ready', 'payload': {'session_authority': True}}})
        async for frame in ws:
            value = json.loads(frame.data)
            self.calls.append(value)
            params, method = value['params'], value['method']
            self.assertEqual(params['profile'], 'research')
            if method == 'session.list':
                result = {'scope': 'live', 'sessions': [{'session_id': 'fixture-session',
                    'title': 'Group: fixture-room', 'source': 'bot_room', 'profile': 'research'}]}
            elif method == 'session.resume':
                result = {'session_id': 'fixture-session', 'subscription_id': 'fixture-sub',
                    'execution_generation': 7, 'prompts': [{'kind': 'approval', 'prompt_id': 'fixture-prompt',
                    'execution_generation': 7, 'choices': ['once', 'deny'], 'command': 'never-executed'}]}
                if getattr(self, 'after_resume', None):
                    self.after_resume()
            elif method == 'approval.respond':
                self.assertEqual(params, {'profile': 'research', 'session_id': 'fixture-session',
                    'prompt_id': 'fixture-prompt', 'execution_generation': 7, 'choice': 'deny'})
                result = {'status': 'resolved', 'prompt_id': 'fixture-prompt'}
            elif method == 'session.detach':
                result = {'detached': True}
            else:
                self.fail('unexpected RPC method: ' + method)
            await ws.send_json({'jsonrpc': '2.0', 'id': value['id'], 'result': result})
        return ws

    def adapter(self, config=None):
        return GuestApprovalAdapter(self.config if config is None else config, handles=ApprovalHandles())

    def context(self):
        return {'room_id': 'fixture-room', 'profile': 'research', 'import_id': 'fixture-import'}

    async def snapshot(self, adapter=None, context=None):
        return await (adapter or self.adapter()).run(context or self.context(), lambda: None)

    async def test_canonical_snapshot_then_exact_approval_fresh_ticket_no_token_file(self):
        adapter = self.adapter()
        rows = await self.snapshot(adapter)
        self.assertEqual(len(rows), 1)
        handle = rows[0]['request_id']
        self.assertTrue(handle.startswith('approval-'))
        result = await adapter.run(self.context(), lambda: None, request_id=handle, choice='deny')
        self.assertEqual(result, {'status': 'resolved', 'prompt_id': 'fixture-prompt'})
        self.assertEqual([f['method'] for f in self.calls], ['session.list', 'session.resume', 'session.detach',
            'session.list', 'session.resume', 'approval.respond', 'session.detach'])
        self.assertEqual(len(set(self.used)), 2)
        self.assertFalse(self.ticket_store.outstanding())

    async def test_canonical_not_ready_and_ticket_failures_are_native_unavailable(self):
        for state in ('starting', 'draining', 'conflict', 'absent'):
            self.state = state
            with self.subTest(state=state), self.assertRaisesRegex(GuestServiceError, '^native_unavailable$'):
                await self.snapshot()
        self.assertFalse(any(r['verb'] == 'session-ticket' for r in self.requests))
        self.state = 'ready'
        for mode in ('absent', 'reused'):
            self.ticket_mode = mode
            with self.subTest(mode=mode), self.assertRaisesRegex(GuestServiceError, '^native_unavailable$'):
                await self.snapshot()
        self.assertEqual(self.calls, [])

    async def test_canonical_negotiated_protocol_refused_before_rpc(self):
        self.protocol = 'wrong-protocol'
        with self.assertLogs('aiohttp.websocket', level='WARNING'), self.assertRaisesRegex(
                GuestServiceError, '^native_unavailable$'):
            await self.snapshot()
        self.assertEqual(self.calls, [])

    async def test_canonical_scope_binds_installation_profile_home_and_context(self):
        adapter = self.adapter()
        handle = (await self.snapshot(adapter))[0]['request_id']
        # All authority fields are part of the opaque key, not a ticket/token hash.
        from invite_rooms.guest_approvals import scope_key
        key = adapter.handles.get(handle)[0]
        authority = dict(profile_home=str(self.home), profile='research',
            installation_id=self.config['installation_id'], instance_id=self.instance, authority_epoch=1)
        self.assertEqual(key, scope_key({'confirmed': self.context(), 'native': authority}))
        for field, value in [('profile_home', str(self.root)), ('profile', 'other'),
                             ('installation_id', 'install:' + 'a' * 32), ('instance_id', 'other')]:
            self.assertNotEqual(key, scope_key({'confirmed': self.context(), 'native': dict(authority, **{field: value})}))
        before = len(self.calls)
        with self.assertRaisesRegex(GuestServiceError, '^request_expired$'):
            await adapter.run(dict(self.context(), room_id='other-room'), lambda: None,
                              request_id=handle, choice='deny')
        self.assertEqual(len(self.calls), before)

    async def test_changed_gateway_instance_expires_handle_before_rpc(self):
        adapter = self.adapter()
        handle = (await self.snapshot(adapter))[0]['request_id']
        self.instance = 'replacement-instance'
        self.ticket_store = TicketStore(self.instance, frozenset({str(self.home)}))
        before = len(self.calls)
        with self.assertRaisesRegex(GuestServiceError, '^request_expired$'):
            await adapter.run(self.context(), lambda: None, request_id=handle, choice='deny')
        self.assertEqual(len(self.calls), before)

    async def test_canonical_revalidates_home_install_and_mixed_mode_no_fallback(self):
        bad = [dict(self.config, installation_id='install:' + 'a' * 32),
               dict(self.config, endpoint=self.origin, token_path='/must-not-read'),
               dict(self.config, profile='default')]
        for config in bad:
            with self.subTest(config=config), self.assertRaisesRegex(GuestServiceError, '^native_unavailable$'):
                await self.snapshot(self.adapter(config))
        self.home.chmod(0o755)
        with self.assertRaisesRegex(GuestServiceError, '^native_unavailable$'):
            await self.snapshot()
        self.home.chmod(0o700)
        self.assertEqual(self.requests, [])
        self.assertEqual(self.calls, [])

    async def test_canonical_scope_revocation_still_blocks_final_native_write(self):
        adapter = self.adapter()
        handle = (await self.snapshot(adapter))[0]['request_id']
        live = True
        def revoked():
            if not live:
                raise GuestServiceError('forbidden')
        def revoke_after_resume():
            nonlocal live
            live = False
        self.after_resume = revoke_after_resume
        before = len(self.calls)
        with self.assertRaisesRegex(GuestServiceError, '^forbidden$'):
            await adapter.run(self.context(), revoked, request_id=handle, choice='deny')
        self.assertEqual([f['method'] for f in self.calls[before:]], ['session.list', 'session.resume'])

    async def test_no_config_and_nonexplicit_legacy_are_sanitized(self):
        for config in (None, {}, {'endpoint': self.origin}, {'token_path': '/must-not-read'}):
            adapter = GuestApprovalAdapter(config, handles=ApprovalHandles())
            with self.subTest(config=config), self.assertRaisesRegex(GuestServiceError, '^native_unavailable$'):
                await adapter.run(self.context(), lambda: None)
        self.assertEqual(self.requests, [])
        self.assertEqual(self.calls, [])


if __name__ == '__main__':
    unittest.main()
