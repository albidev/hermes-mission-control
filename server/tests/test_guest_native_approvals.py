"""Canonical local protocol server: real HTTP -> authenticated WS -> exact waiters.

No core daemon, model, production profile or peer is contacted.
"""
import asyncio
import copy
import json
import threading
from pathlib import Path
from unittest.mock import patch

from aiohttp import web
import unittest
import test_guest_routes_http as guest_http
from test_guest_routes_http import BASE, ROOM
from test_room_invite_routes import _write_private_file
from invite_rooms import routes
from invite_rooms.consent import canonical


class ProtocolServer:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        self.frames, self.auth, self.closed = [], [], []
        self.rows = [{'session_id': 'native-room-session', 'title': 'Group: ' + ROOM, 'source': 'bot_room'}]
        self.generation = 7
        self.prompts = [dict(kind='approval', prompt_id=p, execution_generation=7,
                             command='synthetic-no-exec', description='test', choices=['once', 'deny'])
                        for p in ('first', 'second')]
        self.waiters = {p: threading.Event() for p in ('first', 'second')}
        self.decisions = {}
        self.error = None
        self.reply = None
        self.ready_canonical = True
        self.after_list = None
        self.response_error = None
        self.drop_after_resume = False
        self.after_resume = None
        self.drop = False
        self.connections = 0
        self.runner, self.port = self.run(self.start())

    def run(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout=10)

    async def start(self):
        app = web.Application()
        app.router.add_get('/api/ws', self.handler)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 0)
        await site.start()
        return runner, site._server.sockets[0].getsockname()[1]

    async def handler(self, request):
        self.auth.append(request.headers.get('Authorization'))
        if self.auth[-1] != 'Bearer private-native-token':
            return web.Response(status=401)
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.connections += 1
        connection = self.connections
        attached = None
        await ws.send_json({'jsonrpc': '2.0', 'method': 'event', 'params': {
            'type': 'gateway.ready', 'payload': {'session_authority': self.ready_canonical}}})
        try:
            async for frame in ws:
                value = json.loads(frame.data)
                self.frames.append((connection, value))
                params, method = value['params'], value['method']
                error = self.response_error if method == 'approval.respond' else self.error
                if self.drop:
                    await ws.close()
                    break
                if params.get('profile') != 'target-b':
                    error = {'code': 4001, 'message': 'profile_mismatch', 'data': {'reason': 'profile_mismatch'}}
                if error:
                    await ws.send_json({'jsonrpc': '2.0', 'id': value['id'], 'error': error})
                    continue
                if method == 'session.list':
                    assert params == {'profile': 'target-b', 'include_hidden': True, 'limit': 200}
                    result = {'sessions': self.rows, 'scope': 'live'}
                    if self.after_list:
                        self.after_list()
                elif method == 'session.resume':
                    assert params['session_id'] == 'native-room-session'
                    attached = params['session_id']
                    result = {'session_id': attached, 'subscription_id': 'subscription',
                              'execution_generation': self.generation, 'prompts': copy.deepcopy(self.prompts)}
                    if self.after_resume:
                        self.after_resume()
                    if self.drop_after_resume:
                        await ws.close()
                        break
                elif method == 'approval.respond':
                    assert attached == params['session_id']
                    assert set(params) == {'profile', 'session_id', 'execution_generation', 'prompt_id', 'choice'}
                    assert params['choice'] in ('once', 'deny')
                    prompt = next((p for p in self.prompts if p['prompt_id'] == params['prompt_id'] and
                                   p['execution_generation'] == params['execution_generation']), None)
                    if prompt:
                        self.prompts.remove(prompt)
                        self.waiters[params['prompt_id']].set()
                        self.decisions[params['prompt_id']] = params['choice']
                    result = self.reply if self.reply is not None else {
                        'status': 'resolved' if prompt else 'already_resolved', 'prompt_id': params['prompt_id']}
                elif method == 'session.detach':
                    assert set(params) == {'profile', 'session_id', 'subscription_id'}
                    assert attached == params['session_id']
                    attached = None
                    result = {'detached': True}
                else:
                    raise AssertionError('unbounded method: ' + method)
                await ws.send_json({'jsonrpc': '2.0', 'id': value['id'], 'result': result})
        finally:
            self.closed.append(connection)
        return ws

    def close(self):
        self.run(self.runner.cleanup())
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=5)
        self.loop.close()


class NativeApprovalsHttpTests(unittest.TestCase):
    _shutdown_server = guest_http.GuestHttpTests._shutdown_server
    _restore_env = guest_http.GuestHttpTests._restore_env
    _write_config = guest_http.GuestHttpTests._write_config
    _request = guest_http.GuestHttpTests._request
    activate = guest_http.GuestHttpTests.activate
    get = guest_http.GuestHttpTests.get

    def setUp(self):
        guest_http.GuestHttpTests.setUp(self)
        self.protocol = ProtocolServer()
        self.addCleanup(self.protocol.close)
        scope = self.store.confirmed_scope(self.import_id)
        scope.update(host_id='install:' + 'b' * 32, member_id='member-b', lobby_id='lobby-one')
        scope['binding'] = dict(endpoint_id='local-b', fingerprint='a' * 64,
            target_install_id='install:' + 'a' * 32, target_profile='target-b',
            catalog_digest='c' * 64, policy_digest='d' * 64)
        with self.store._write() as conn:
            conn.execute('UPDATE confirmed_scope SET scope_json=? WHERE import_id=?',
                         (json.dumps(scope), self.import_id))

        config = json.loads(self.config_path.read_text())
        config['native_rpc']['endpoint'] = f'ws://127.0.0.1:{self.protocol.port}/api/ws'
        _write_private_file(self.config_path, json.dumps(config))
        # Runtime host registry lookup is exercised through a real private file.
        peers = json.loads(self.peer_registry_path.read_text())
        scope['host_id'] = peers[0]['installation_id']
        scope['binding']['target_install_id'] = config['installation_id']
        from gateway.hosted_room_peer import catalog_mapping
        from hashlib import sha256
        policy = {'version': 1, 'target_profile': 'target-b', 'enabled_toolsets': ['bot_room'],
                  'approval_mode': 'manual', 'max_iterations': 10}
        policy['policy_digest'] = sha256(canonical(policy)).hexdigest()
        self.local_catalog = catalog_mapping(installation_id=config['installation_id'],
            protocol_versions=(2,), link_modes=('direct',), persistent_process=True,
            text=True, attachments=False, target_profile='target-b', execution_policy=policy,
            endpoint={'available': True, 'url': 'http://127.0.0.1:19001/links/synthetic',
                      'transport_security': 'loopback'})
        scope['binding']['catalog_digest'] = self.local_catalog['catalog_digest']
        scope['binding']['policy_digest'] = policy['policy_digest']
        with self.store._write() as conn:
            conn.execute('UPDATE imports SET host_peer_id=? WHERE import_id=?', ('owner-b', self.import_id))
        self.approve_fixture(self.import_id, scope, 'invite-one')
        from test_guest_routes_http import _NATIVE_CONNECT
        self.enterContext(patch.object(routes, '_connect_native_rpc', _NATIVE_CONNECT))

    def approve_fixture(self, iid, scope, invite_id):
        """Synthetic target-mint shape, signed by fixture keys; never a live grant."""
        import base64
        import time
        from test_room_invite_routes import _CONSENT_SIGNER
        material_scope = dict(scope, invite_id=invite_id)
        now = time.time()
        claims = {'room_id': scope['room_id'], 'home_install_id': scope['host_id'],
                  'authority_gateway_id': scope['host_id'], 'authority_epoch': 1,
                  'member_id': scope['member_id'], 'target_install_id': scope['binding']['target_install_id'],
                  'target_profile': 'target-b', 'issued_at': now, 'expires_at': now + 600,
                  'status_expires_at': now + 600,
                  'execution_policy_digest': scope['binding']['policy_digest']}
        def b64(value):
            return base64.urlsafe_b64encode(value).rstrip(b'=').decode()
        # Deliberately not a native authority signature: this fixture exercises
        # persisted local material hygiene, not cryptographic grant admission.
        grant = b64(canonical(claims)) + '.' + b64(bytes(32))
        envelope = {'scope': material_scope, 'grant': grant, 'catalog': self.local_catalog}
        envelope['signature'] = base64.b64encode(_CONSENT_SIGNER.sign(canonical(
            dict(envelope, purpose='joining-owner-material-v1')))).decode()
        receipt = dict(material_scope, purpose='joining-owner-consent-v1')
        signature = base64.b64encode(_CONSENT_SIGNER.sign(canonical(receipt))).decode()
        with self.store._write() as conn:
            conn.execute('UPDATE confirmed_scope SET scope_json=? WHERE import_id=?', (json.dumps(scope), iid))
            conn.execute('UPDATE consents SET receipt_json=?,signature=? WHERE import_id=?',
                         (json.dumps(receipt), signature, iid))
        key = canonical(material_scope).decode()
        self.store.mint_begin(key)
        self.store.mint_complete(key, canonical(envelope))

    def bot(self):
        return self.get('/' + ROOM + '/my-bot')

    def decide(self, handle, choice='approve', **extra):
        return self._request('POST', BASE + '/' + ROOM + '/approvals/' + handle, {'choice': choice, **extra})

    def handles(self):
        status, data, _ = self.bot()
        self.assertEqual(status, 200, data)
        return [p['request_id'] for p in data['approvals']]

    def test_native_hidden_profile_and_once_exact_waiter(self):
        first, second = self.handles()
        self.assertNotEqual(first, 'first')
        self.assertEqual(self.decide(first)[0], 200)
        self.assertTrue(self.protocol.waiters['first'].is_set())
        self.assertFalse(self.protocol.waiters['second'].is_set())
        self.assertEqual(self.protocol.decisions, {'first': 'once'})
        frames = [(n, f['method']) for n, f in self.protocol.frames]
        self.assertEqual([m for n, m in frames if n == 2],
                         ['session.list', 'session.resume', 'approval.respond', 'session.detach'])
        self.assertEqual(self.protocol.auth, ['Bearer private-native-token'] * 2)

    def test_native_deny_exact_waiter(self):
        first, second = self.handles()
        self.assertEqual(self.decide(second, 'reject')[0], 200)
        self.assertFalse(self.protocol.waiters['first'].is_set())
        self.assertEqual(self.protocol.decisions, {'second': 'deny'})

    def test_native_discovery_unavailable_ambiguity_and_saturation(self):
        row = self.protocol.rows[0]
        for rows in ([], [dict(row, title='Group: other')], [dict(row, source='cli')],
                     [row, dict(row, session_id='another')],
                     [row] + [dict(row, session_id='other-' + str(i), title='Group: other-' + str(i))
                              for i in range(199)]):
            with self.subTest(rows=len(rows)):
                self.protocol.rows = rows
                self.assertEqual(self.bot()[0], 503)
        self.assertTrue(all(f['method'] == 'session.list' for _, f in self.protocol.frames))

    def test_native_cold_then_available(self):
        row = self.protocol.rows.pop()
        self.assertEqual(self.bot()[0], 503)
        self.protocol.rows.append(row)
        self.assertEqual(len(self.handles()), 2)

    def test_native_stale_generation_or_expired_never_substitutes(self):
        for mutation in ('generation', 'expired'):
            with self.subTest(mutation=mutation):
                first, second = self.handles()
                if mutation == 'generation':
                    self.protocol.generation = 8
                    for prompt in self.protocol.prompts:
                        prompt['execution_generation'] = 8
                else:
                    self.protocol.prompts.pop(0)
                self.assertEqual(self.decide(first)[0], 409)
                self.assertFalse(self.protocol.waiters['second'].is_set())
                self.protocol.generation = 7
                self.protocol.prompts = [dict(kind='approval', prompt_id=p, execution_generation=7,
                    command='synthetic-no-exec', choices=['once', 'deny']) for p in ('first', 'second')]
        self.assertFalse(any(f['method'] == 'approval.respond' for _, f in self.protocol.frames))

    def test_native_leave_between_snapshot_and_send(self):
        handle = self.handles()[0]
        self.protocol.after_resume = lambda: self.store.mark_left(self.import_id)
        self.assertEqual(self.decide(handle)[0], 403)
        self.assertEqual(self.protocol.decisions, {})

    def test_native_material_revoked_between_snapshot_and_send(self):
        handle = self.handles()[0]
        def revoke():
            with self.store._write() as conn:
                conn.execute("UPDATE material SET state='revoked'")
        self.protocol.after_resume = revoke
        self.assertEqual(self.decide(handle)[0], 403)
        self.assertEqual(self.protocol.decisions, {})

    def test_native_forged_handle_and_http_scope_injection(self):
        self.assertEqual(self.decide('first')[0], 409)
        handle = self.handles()[0]
        for key in ('session_id', 'profile', 'execution_generation', 'url', 'all'):
            self.assertEqual(self.decide(handle, **{key: 'other'})[0], 400)
        self.assertEqual(self.protocol.decisions, {})

    def test_native_auth_denied_never_legacy_fallback(self):
        self.protocol.error = {'code': 4001, 'message': 'permission_denied', 'data': {'reason': 'permission_denied'}}
        self.assertEqual(self.bot()[0], 403)
        self.assertEqual([f['method'] for _, f in self.protocol.frames], ['session.list'])

    def test_native_unsupported_not_offline(self):
        self.protocol.error = {'code': -32601, 'message': 'unknown method'}
        self.assertEqual(self.bot()[:2], (501, {'code': 'not_supported'}))

    def test_native_offline_reconnect_cleanup(self):
        self.protocol.drop = True
        self.assertEqual(self.bot()[0], 503)
        self.protocol.drop = False
        self.assertEqual(len(self.handles()), 2)
        self.assertEqual(self.protocol.closed, [1, 2])

    def test_native_invalid_result_not_success(self):
        for reply in ({}, {'status': 'resolved', 'prompt_id': 'other'}, {'resolved': 1}):
            with self.subTest(reply=reply):
                handle = self.handles()[0]
                self.protocol.reply = reply
                self.assertEqual(self.decide(handle)[0], 503)
                # Restore the synthetic queue, never a real command.
                self.protocol.prompts.insert(0, dict(kind='approval', prompt_id='first', execution_generation=7,
                    command='synthetic-no-exec', choices=['once', 'deny']))

    def test_native_already_resolved_contract(self):
        handle = self.handles()[0]
        self.protocol.after_resume = lambda: self.protocol.prompts.pop(0)
        self.assertEqual(self.decide(handle)[0], 200)
        self.assertEqual(self.protocol.decisions, {})

    def test_native_profile_mismatch_is_forbidden(self):
        self.protocol.error = {'code': 4001, 'message': 'profile_mismatch', 'data': {'reason': 'profile_mismatch'}}
        self.assertEqual(self.bot()[0], 403)
        self.assertEqual([f['method'] for _, f in self.protocol.frames], ['session.list'])

    def test_native_wrong_row_profile_has_no_resume(self):
        self.protocol.rows[0]['profile'] = 'launch-other'
        self.assertEqual(self.bot()[0], 503)
        self.assertEqual([f['method'] for _, f in self.protocol.frames], ['session.list'])

    def test_native_legacy_ready_is_501_before_any_method(self):
        self.protocol.ready_canonical = False
        self.assertEqual(self.bot()[:2], (501, {'code': 'not_supported'}))
        self.assertEqual(self.protocol.frames, [])

    def test_native_peer_unpair_after_snapshot_blocks_send(self):
        handle = self.handles()[0]
        self.protocol.after_resume = lambda: _write_private_file(self.peer_registry_path, '[]')
        self.assertEqual(self.decide(handle)[0], 403)
        self.assertEqual(self.protocol.decisions, {})

    def test_native_peer_generation_after_list_blocks_resume(self):
        def cutover():
            peers = json.loads(self.peer_registry_path.read_text())
            peers[0]['pairing_generation'] += 1
            _write_private_file(self.peer_registry_path, json.dumps(peers))
        self.protocol.after_list = cutover
        self.assertEqual(self.bot()[0], 403)
        self.assertEqual([f['method'] for _, f in self.protocol.frames], ['session.list'])

    def test_native_material_expiry_after_snapshot_blocks_send(self):
        import time
        handle = self.handles()[0]
        clock = patch('invite_rooms.joining_material.time.time', return_value=time.time() + 601)
        self.addCleanup(clock.stop)
        self.protocol.after_resume = clock.start
        self.assertEqual(self.decide(handle)[0], 403)
        self.assertEqual(self.protocol.decisions, {})
        self.assertFalse(any(f['method'] == 'approval.respond' for _, f in self.protocol.frames))

    def test_native_consent_must_match_confirmed_scope(self):
        with self.store._write() as conn:
            conn.execute("UPDATE consents SET receipt_json='{}'")
        self.assertEqual(self.bot()[0], 403)
        self.assertEqual(self.protocol.connections, 0)

    def test_native_consent_change_after_snapshot_blocks_send(self):
        handle = self.handles()[0]
        def change():
            with self.store._write() as conn:
                conn.execute("UPDATE consents SET signature='changed'")
        self.protocol.after_resume = change
        self.assertEqual(self.decide(handle)[0], 403)
        self.assertEqual(self.protocol.decisions, {})

    def test_native_stale_after_snapshot_native_error_is_409(self):
        handle = self.handles()[0]
        self.protocol.response_error = {'code': 4001, 'message': 'stale_generation',
                                        'data': {'reason': 'stale_generation'}}
        self.assertEqual(self.decide(handle)[:2], (409, {'code': 'request_expired'}))
        self.assertEqual(self.protocol.decisions, {})

    def test_native_disconnect_after_resume_does_not_respond_and_can_reconnect(self):
        handle = self.handles()[0]
        self.protocol.drop_after_resume = True
        self.assertEqual(self.decide(handle)[0], 503)
        self.assertEqual(self.protocol.decisions, {})
        self.protocol.drop_after_resume = False
        self.assertEqual(self.decide(handle)[0], 200)
        self.assertEqual(self.protocol.decisions, {'first': 'once'})

    def test_native_expired_handle_requires_new_snapshot(self):
        handle = self.handles()[0]
        from invite_rooms.guest_approvals import HANDLES
        with HANDLES._lock:
            _, value = HANDLES._items[handle]
            HANDLES._items[handle] = (0, value)
        self.assertEqual(self.decide(handle)[0], 409)
        self.assertEqual(self.protocol.connections, 1)
        self.assertNotEqual(self.handles()[0], handle)

    def test_native_handle_cannot_cross_room_or_import(self):
        handle = self.handles()[0]
        other = 'invite-room-other'
        iid = self.activate(other, 'other')
        scope = self.store.confirmed_scope(self.import_id)
        scope['room_id'] = other
        with self.store._write() as conn:
            conn.execute('UPDATE imports SET host_peer_id=? WHERE import_id=?', ('owner-b', iid))
        self.approve_fixture(iid, scope, 'invite-other')
        status, data, _ = self._request('POST', BASE + '/' + other + '/approvals/' + handle, {'choice': 'approve'})
        self.assertEqual((status, data), (409, {'code': 'request_expired'}))
        self.assertEqual(self.protocol.connections, 1)

    def test_native_no_guess_from_poisoned_scope(self):
        scope = self.store.confirmed_scope(self.import_id)
        scope['session_id'] = 'roomlink-untrusted'
        self.approve_fixture(self.import_id, scope, 'invite-one')
        self.assertEqual(len(self.handles()), 2)
        resumed = [f['params']['session_id'] for _, f in self.protocol.frames if f['method'] == 'session.resume']
        self.assertEqual(resumed, ['native-room-session'])

    def test_native_invalid_material_or_scope_is_denied_before_connect(self):
        scope = self.store.confirmed_scope(self.import_id)
        scope['binding']['target_install_id'] = 'install:' + 'e' * 32
        with self.store._write() as conn:
            conn.execute('UPDATE confirmed_scope SET scope_json=? WHERE import_id=?', (json.dumps(scope), self.import_id))
        self.assertEqual(self.bot()[0], 403)
        self.assertEqual(self.protocol.connections, 0)

    def test_native_http_auth_origin_feature_off_before_connect(self):
        import os
        self.assertEqual(self._request('GET', BASE + '/' + ROOM + '/my-bot', token=None)[0], 401)
        self.assertEqual(self._request('GET', BASE + '/' + ROOM + '/my-bot', origin='https://evil.invalid')[0], 403)
        os.environ.pop('MISSION_CONTROL_ROOM_INVITES_CONFIG')
        self.assertEqual(self.bot()[0], 404)
        self.assertEqual(self.protocol.connections, 0)

    def test_native_handshake_auth_failure_is_forbidden_no_fallback(self):
        _write_private_file(self.token_path, 'invalid-token')
        self.assertEqual(self.bot()[0], 403)
        self.assertEqual(self.protocol.auth, ['Bearer invalid-token'])
        self.assertEqual(self.protocol.frames, [])

    def test_native_invalid_body_has_single_http_response(self):
        import socket
        body = b'{"choice":"approve","session_id":"attacker"}'
        request = (b'POST ' + (BASE + '/' + ROOM + '/approvals/foreign').encode() + b' HTTP/1.0\r\n'
                   b'Host: localhost\r\nAuthorization: Bearer test-token\r\nContent-Type: application/json\r\n'
                   b'Content-Length: ' + str(len(body)).encode() + b'\r\n\r\n' + body)
        with socket.create_connection(self.httpd.server_address, timeout=5) as client:
            client.sendall(request)
            response = bytearray()
            while chunk := client.recv(65536):
                response.extend(chunk)
        self.assertEqual(response.count(b'HTTP/1.0 '), 1)
        self.assertIn(b'400 Bad Request', response)
        self.assertEqual(self.protocol.connections, 0)

    def test_native_no_config_is_unavailable_not_unsupported(self):
        config = json.loads(self.config_path.read_text())
        config.pop('native_rpc')
        _write_private_file(self.config_path, json.dumps(config))
        self.assertEqual(self.bot()[:2], (503, {'code': 'native_unavailable'}))
        self.assertEqual(self.protocol.connections, 0)

    def test_native_empty_prompt_snapshot_is_online_no_requests(self):
        self.protocol.prompts = []
        status, data, _ = self.bot()
        self.assertEqual(status, 200)
        self.assertEqual(data['approvals'], [])
        self.assertEqual(data['status'], 'online')
        self.assertEqual([f['method'] for _, f in self.protocol.frames],
                         ['session.list', 'session.resume', 'session.detach'])

    def test_native_duplicate_confirmed_imports_are_forbidden(self):
        self.activate(ROOM, 'duplicate')
        self.assertEqual(self.bot()[0], 403)
        self.assertEqual(self.protocol.connections, 0)

    def test_native_ui_fields_and_no_native_identity(self):
        status, data, _ = self.bot()
        self.assertEqual(status, 200)
        self.assertEqual(data['approvals'][0]['title'], 'synthetic-no-exec')
        self.assertEqual(data['approvals'][0]['detail'], 'test')
        text = json.dumps(data)
        for private in ('native-room-session', 'execution_generation', 'confirmed-local-grant', 'prompt_id'):
            self.assertNotIn(private, text)

    def test_native_guest_no_control_routes(self):
        for verb in ('submit', 'steer', 'interrupt', 'rpc'):
            self.assertEqual(self._request('POST', BASE + '/' + ROOM + '/' + verb, {})[0], 404)
        self.assertEqual(self.protocol.frames, [])


