"""P6-B.3: real HTTP, private runtime config and durable joining fixtures.

Only transport connections are stand-ins; no auth or store mock. Core currently
has no target-session lookup API: native approvals are explicitly unsupported.
"""
import contextlib
import copy
import json
import logging
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import test_room_invite_routes as owner_http
from test_room_invite_routes import _write_private_file
from invite_rooms import routes, runtime_config
from invite_rooms.consent import canonical
from invite_rooms.guest import GuestRoomService, GuestServiceError
from invite_rooms.joining_store import JoiningStore
from invite_rooms.control.dispatcher import ControlError

BASE = '/api/local/room-invites/guest-rooms'
ROOM = 'invite-room-real'
GRANT = 'private-grant-marker'
_NATIVE_CONNECT = routes._connect_native_rpc


class Native:
    def __init__(self):
        self.calls = []
        self.result = {'revoked': True}
    async def __call__(self, method, params):
        self.calls.append((method, params))
        return self.result


class Host:
    def __init__(self):
        self.calls = []
        self.error = None
        self.page = {'events': [{'event_id': f'event-{i}', 'seq': i,
                                'payload': {'text': 'private-body-marker'}} for i in range(1, 206)],
                     'cursor': 205, 'latest_seq': 205, 'has_more': False,
                     'authority': {'gateway_id': 'host', 'epoch': 1}}
    def call(self, request):
        self.calls.append(request)
        if self.error:
            raise self.error
        if request['op'] == 'member_left':
            return {'recorded': True}
        events = [e for e in self.page['events'] if e['seq'] > request['since_seq']]
        selected = events[:request['limit']]
        return {**self.page, 'events': selected,
                'cursor': selected[-1]['seq'] if selected else request['since_seq'],
                'has_more': len(events) > len(selected)}


class GuestHttpTests(unittest.TestCase):
    # Reuse the established real Handler/server fixture, not its owner tests.
    setUp = owner_http.RoomInviteRoutesHttpTest.setUp
    _shutdown_server = owner_http.RoomInviteRoutesHttpTest._shutdown_server
    _restore_env = owner_http.RoomInviteRoutesHttpTest._restore_env
    _write_config = owner_http.RoomInviteRoutesHttpTest._write_config
    _request = owner_http.RoomInviteRoutesHttpTest._request

    def setUp(self):
        owner_http.RoomInviteRoutesHttpTest.setUp(self)
        self.store = JoiningStore(Path(self.tmp.name) / 'joining')
        self.native = Native()
        self.host = Host()
        self.connector_calls = []
        @contextlib.asynccontextmanager
        async def connect(config):
            self.connector_calls.append(config)
            yield self.native
        self.enterContext(patch.object(routes, '_connect_native_rpc', connect))
        # The sidecar owns this factory. A link connector is a later packet.
        self.enterContext(patch.object(routes, '_HOST_CHANNEL_FACTORY', lambda config: self.host, create=True))
        config = json.loads(self.config_path.read_text())
        self.token_path = self.config_path.parent / 'native-token'
        _write_private_file(self.token_path, 'private-native-token')
        config['native_rpc'] = {'endpoint': 'ws://127.0.0.1:19999', 'token_path': str(self.token_path)}
        config['joining'] = {'store_dir': str(self.store.root)}
        _write_private_file(self.config_path, json.dumps(config))
        self.import_id = self.activate(ROOM, 'one')

    def activate(self, room, suffix, state='consent_given'):
        iid = self.store.create_import(lobby_id='lobby-' + suffix,
            invite_id='invite-' + suffix, secret='x' * 43, host_peer_id='host-a')
        scope = {'room_id': room, 'binding': {'target_profile': 'default'}}
        self.store.confirm_scope(iid, scope)
        if state != 'previewed':
            self.store.mark_redeemed(iid)
            self.store.begin_consent(iid, receipt={'scope': scope}, signature='sig')
            if state == 'consent_given':
                self.store.mark_consent_delivered(iid)
        key = canonical(dict(scope, invite_id='invite-' + suffix)).decode()
        self.store.mint_begin(key)
        self.store.mint_complete(key, canonical({'grant': GRANT}))
        return iid

    def get(self, suffix=''):
        return self._request('GET', BASE + suffix)

    def test_valid_config_activates_guest_routes_without_global_transport(self):
        status, data, headers = self.get()
        self.assertEqual(status, 200)
        self.assertEqual(data['rooms'][0]['room_id'], ROOM)
        self.assertEqual(headers['Cache-Control'], 'no-store')

    def test_feature_off_and_missing_config_are_404(self):
        os.environ.pop('MISSION_CONTROL_ROOM_INVITES_CONFIG')
        self.assertEqual(self.get()[0], 404)
        os.environ['MISSION_CONTROL_ROOM_INVITES_CONFIG'] = str(self.config_path) + '.missing'
        self.assertEqual(self.get()[0], 404)

    def test_config_removal_after_success_disables_routes(self):
        self.assertEqual(self.get()[0], 200)
        os.environ.pop('MISSION_CONTROL_ROOM_INVITES_CONFIG')
        self.assertEqual(self.get()[0], 404)

    def test_auth_and_origin_use_sidecar_policy(self):
        self.assertEqual(self._request('GET', BASE, token=None)[0], 401)
        self.assertEqual(self._request('GET', BASE, token='wrong')[0], 401)
        self.assertEqual(self._request('GET', BASE, origin='https://evil.invalid')[0], 403)
        os.environ['MISSION_CONTROL_ALLOWED_ORIGIN'] = 'http://localhost:5174'
        self.assertEqual(self._request('GET', BASE, origin='http://localhost:5174')[0], 200)
        self.assertEqual(self._request('POST', BASE + '/' + ROOM + '/leave', {}, origin='https://evil.invalid')[0], 403)
        self.assertEqual(self.native.calls, [])

    def test_only_consent_given_not_pending_previewed_or_left(self):
        self.activate('pending-room', 'two', 'pending')
        self.activate('preview-room', 'three', 'previewed')
        status, data, _ = self.get()
        self.assertEqual(status, 200)
        self.assertEqual([r['room_id'] for r in data['rooms']], [ROOM])
        self.assertEqual(self._request('POST', BASE + '/' + ROOM + '/leave', {})[0], 200)
        self.assertEqual(self.get()[1]['rooms'], [])

    def test_log_verbatim_since_seq_cap_200_and_has_more(self):
        status, page, headers = self.get('/' + ROOM + '/log?since_seq=2&limit=200')
        self.assertEqual(status, 200)
        self.assertEqual(page['events'], self.host.page['events'][2:202])
        self.assertTrue(page['has_more'])
        self.assertEqual(page['cursor'], 202)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        status, page, _ = self.get('/' + ROOM + '/log?since_seq=202&limit=200')
        self.assertEqual(status, 200)
        self.assertEqual(page['events'], self.host.page['events'][202:])
        self.assertFalse(page['has_more'])
        self.assertEqual(self.get('/' + ROOM + '/log?limit=201')[0], 400)
        self.assertEqual(self.get('/' + ROOM + '/log?since_seq=-1')[0], 400)
        self.assertEqual(self.native.calls, [])

    def test_other_peer_room_forbidden_without_channel_or_native_call(self):
        self.assertEqual(self.get('/another-peer-room/log')[0], 403)
        self.assertEqual(self.host.calls, [])
        self.assertEqual(self.native.calls, [])

    def test_revoked_link_is_forbidden_not_offline(self):
        self.host.error = ControlError('forbidden')
        self.assertEqual(self.get('/' + ROOM + '/log')[:2], (403, {'code': 'forbidden'}))

    def test_host_offline_is_503(self):
        self.host.error = ConnectionError('private-grant-marker')
        status, data, headers = self.get('/' + ROOM + '/log')
        self.assertEqual((status, data), (503, {'code': 'host_offline', 'last_seen_at': None}))
        self.assertEqual(headers['Cache-Control'], 'no-store')

    def test_absent_channel_keeps_local_routes_active(self):
        with patch.object(routes, '_HOST_CHANNEL_FACTORY', None, create=True):
            self.assertEqual(self.get()[0], 200)
            self.assertEqual(self.get('/' + ROOM + '/log')[0], 503)
            self.assertEqual(self.get('/' + ROOM + '/my-bot')[:2], (501, {'code': 'not_supported'}))
            self.assertEqual(self._request('POST', BASE + '/' + ROOM + '/leave', {})[0], 200)
        self.assertEqual(self.native.calls, [('groups.peer.revoke', {'profile': 'default', 'grant': GRANT})])

    def test_real_joining_session_unavailable_is_explicit_501(self):
        self.assertEqual(self.get('/' + ROOM + '/my-bot')[:2], (501, {'code': 'not_supported'}))
        self.assertEqual(self._request('POST', BASE + '/' + ROOM + '/approvals/req', {'choice': 'approve'})[:2],
                         (501, {'code': 'not_supported'}))
        self.assertEqual(self.native.calls, [])

    def test_session_like_scope_keys_never_authorize_approvals(self):
        # Even poisoned legacy/private metadata is not a native identity proof.
        original = self.store.confirmed_scope(self.import_id)
        original['session_id'] = 'private-session-marker'
        with self.store._write() as conn:
            conn.execute('UPDATE confirmed_scope SET scope_json=? WHERE import_id=?',
                         (json.dumps(original), self.import_id))
        self.assertEqual(self.get('/' + ROOM + '/my-bot')[0], 501)
        self.assertEqual(self.native.calls, [])

    def test_leave_is_durable_restart_idempotent_and_uses_local_grant(self):
        self.assertEqual(self._request('POST', BASE + '/' + ROOM + '/leave', {})[0], 200)
        self.assertEqual(JoiningStore(self.store.root).get_import(self.import_id)['state'], 'left')
        self.store = JoiningStore(self.store.root)
        self.assertEqual(self.get()[1]['rooms'], [])
        self.assertEqual(self._request('POST', BASE + '/' + ROOM + '/leave', {})[0], 200)
        self.assertEqual(self.native.calls, [('groups.peer.revoke', {'profile': 'default', 'grant': GRANT})])

    def test_leave_rejects_browser_grant_and_session(self):
        for payload in ({'grant': 'attacker'}, {'session_id': 'attacker'}):
            self.assertEqual(self._request('POST', BASE + '/' + ROOM + '/leave', payload)[0], 400)
        self.assertEqual(self.native.calls, [])

    def test_failed_native_revoke_does_not_mark_left(self):
        self.native.result = {'revoked': False}
        self.assertEqual(self._request('POST', BASE + '/' + ROOM + '/leave', {})[0], 503)
        self.assertEqual(self.store.get_import(self.import_id)['state'], 'consent_given')

    def test_speculative_material_fallback_is_rejected(self):
        scope = self.store.confirmed_scope(self.import_id)
        scope['material'] = {'grant': 'attacker'}
        with self.store._write() as conn:
            conn.execute('UPDATE confirmed_scope SET scope_json=? WHERE import_id=?', (json.dumps(scope), self.import_id))
        self.assertEqual(self._request('POST', BASE + '/' + ROOM + '/leave', {})[0], 403)
        self.assertEqual(self.native.calls, [])

    def test_generation_change_after_await_refuses_page_over_http(self):
        import asyncio
        from invite_rooms.control.dispatcher import ControlDispatcher, LinkState
        class Authority:
            def current(self, endpoint):
                return LinkState({'endpoint_id': 'ep'}, 1, 'acknowledged')
        async def call(request):
            admitted = True
            async def log(_):
                nonlocal admitted
                await asyncio.sleep(0)
                admitted = False
                return self.host.page
            dispatcher = ControlDispatcher(Authority(), handlers={'room_log': log},
                direction='B_to_A', owner_verify=lambda *_: True, admit=lambda *_: admitted)
            try:
                return await dispatcher.dispatch('ep', 1, request)
            finally:
                await dispatcher.close()
        with patch.object(self.host, 'call', call):
            self.assertEqual(self.get('/' + ROOM + '/log')[:2], (403, {'code': 'forbidden'}))
        self.assertEqual(self.native.calls, [])

    def test_real_dispatcher_revoked_link_is_403_over_http(self):
        from invite_rooms.control.dispatcher import ControlDispatcher
        class Authority:
            def current(self, endpoint): return None
        async def call(request):
            dispatcher = ControlDispatcher(Authority(), handlers={}, owner_verify=lambda *_: True)
            try:
                return await dispatcher.dispatch('ep', 1, request)
            finally:
                await dispatcher.close()
        with patch.object(self.host, 'call', call):
            self.assertEqual(self.get('/' + ROOM + '/log')[:2], (403, {'code': 'forbidden'}))
        self.assertEqual(self.native.calls, [])

    def test_http_leave_reaches_real_bounded_rpc_over_websocket(self):
        import asyncio
        import threading
        from aiohttp import web
        loop = asyncio.new_event_loop()
        thread = threading.Thread(target=loop.run_forever, daemon=True)
        thread.start()
        frames = []
        auth = []
        async def native_handler(request):
            auth.append(request.headers.get('Authorization'))
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            async for frame in ws:
                value = json.loads(frame.data)
                frames.append(value)
                await ws.send_json({'jsonrpc': '2.0', 'id': value['id'], 'result': {'revoked': True}})
            return ws
        async def start():
            app = web.Application()
            app.router.add_get('/api/ws', native_handler)
            runner = web.AppRunner(app, access_log=None)
            await runner.setup()
            site = web.TCPSite(runner, '127.0.0.1', 0)
            await site.start()
            return runner, site._server.sockets[0].getsockname()[1]
        runner = None
        try:
            runner, port = asyncio.run_coroutine_threadsafe(start(), loop).result(timeout=5)
            config = json.loads(self.config_path.read_text())
            config['native_rpc']['endpoint'] = f'ws://127.0.0.1:{port}/api/ws'
            _write_private_file(self.config_path, json.dumps(config))
            with patch.object(routes, '_connect_native_rpc', _NATIVE_CONNECT):
                self.assertEqual(self._request('POST', BASE + '/' + ROOM + '/leave', {})[0], 200)
            self.assertEqual(auth, ['Bearer private-native-token'])
            self.assertEqual([(f['method'], f['params']) for f in frames],
                             [('groups.peer.revoke', {'profile': 'default', 'grant': GRANT})])
            self.assertNotIn('groups.create', [f['method'] for f in frames])
            self.assertEqual(self.store.get_import(self.import_id)['state'], 'left')
        finally:
            if runner is not None:
                asyncio.run_coroutine_threadsafe(runner.cleanup(), loop).result(timeout=5)
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=5)
            loop.close()

    def test_no_body_grant_session_in_captured_logs(self):
        records = []
        class Capture(logging.Handler):
            def emit(self, record): records.append(record.getMessage())
        logger = logging.getLogger()
        previous_level = logger.level
        logger.setLevel(logging.DEBUG)
        self.addCleanup(logger.setLevel, previous_level)
        capture = Capture()
        logger.addHandler(capture)
        self.addCleanup(logger.removeHandler, capture)
        self.assertEqual(self.get('/' + ROOM + '/log')[0], 200)
        self.assertEqual(self._request('POST', BASE + '/' + ROOM + '/approvals/req',
            {'choice': 'approve', 'session_id': 'private-session-marker'})[0], 400)
        self.assertEqual(self._request('POST', BASE + '/' + ROOM + '/leave', {})[0], 200)
        self.assertNotIn('groups.create', [m for m, _ in self.native.calls])
        text = '\n'.join(records)
        for marker in (GRANT, 'private-body-marker', 'private-session-marker', 'private-native-token'):
            self.assertNotIn(marker, text)

    def test_other_session_request_is_403_without_native_call(self):
        # Isolate ownership guard from missing core lookup: a test-only known
        # target session and foreign request must never reach approval.respond.
        with patch.object(GuestRoomService, '_pending', return_value=(
                {'session_id': 'private-session-marker'}, {}, [])):
            status, data, _ = self._request('POST', BASE + '/' + ROOM + '/approvals/foreign', {'choice': 'approve'})
        self.assertEqual((status, data), (403, {'code': 'forbidden'}))
        self.assertEqual(self.native.calls, [])
        # A matched request id explicitly carrying another session is also
        # forbidden, before any native response mutation.
        with patch.object(GuestRoomService, '_pending', return_value=(
                {}, {}, [{'request_id': 'foreign', 'session_id': 'other-session'}])), \
             patch.object(GuestRoomService, '_session_id', return_value='private-session-marker'):
            status, data, _ = self._request('POST', BASE + '/' + ROOM + '/approvals/foreign', {'choice': 'approve'})
        self.assertEqual((status, data), (403, {'code': 'forbidden'}))
        self.assertEqual(self.native.calls, [])


class GuestDirectRegressionTests(unittest.TestCase):
    """Isolate defects from the base's dead HTTP loader for meaningful REDs."""
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory(prefix='guest-direct-')
        self.addCleanup(self.tmp.cleanup)
        self.store = JoiningStore(Path(self.tmp.name) / 'joining')
        self.iid = self.store.create_import(lobby_id='lobby-one', invite_id='invite-one',
            secret='x' * 43, host_peer_id='host-a')
        # Explicit invite_id here makes the old and new journal key identical,
        # isolating persistence from the separate mint-key regression.
        self.scope = {'room_id': ROOM, 'invite_id': 'invite-one',
                      'binding': {'target_profile': 'default'}}
        self.store.confirm_scope(self.iid, self.scope)
        self.store.mark_redeemed(self.iid)
        self.store.begin_consent(self.iid, receipt={}, signature='sig')
        self.store.mark_consent_delivered(self.iid)
        key = canonical(self.scope).decode()
        self.store.mint_begin(key)
        self.store.mint_complete(key, canonical({'grant': GRANT}))
        self.native = Native()
        self.service = GuestRoomService(self.store, channel=Host(), rpc=self.native)

    def test_leave_reopens_as_terminal_left_without_in_memory_tombstone(self):
        self.service.leave(ROOM)
        reopened = JoiningStore(self.store.root)
        self.assertEqual(reopened.get_import(self.iid)['state'], 'left')
        fresh = GuestRoomService(reopened, channel=Host(), rpc=self.native)
        self.assertEqual(fresh.list_rooms(), [])
        fresh.leave(ROOM)
        self.assertEqual(len(self.native.calls), 1)

    def test_material_never_falls_back_to_scope_embedded_grant(self):
        scope = {**self.scope, 'material': {'grant': 'attacker'}}
        with self.assertRaisesRegex(GuestServiceError, '^forbidden$'):
            self.service._material(self.store.get_import(self.iid), scope)
        self.assertEqual(self.native.calls, [])

    def test_unknown_native_session_is_unsupported_not_forbidden(self):
        with self.assertRaisesRegex(GuestServiceError, '^not_supported$'):
            self.service.my_bot(ROOM)
        self.assertEqual(self.native.calls, [])

    def test_local_scope_session_key_is_not_native_evidence(self):
        with self.assertRaisesRegex(GuestServiceError, '^not_supported$'):
            self.service._session_id({'session_id': 'private-session-marker'})

    def test_false_revoke_reply_has_no_leave_or_notification_effect(self):
        self.native.result = {'revoked': False}
        with self.assertRaisesRegex(GuestServiceError, '^host_offline$'):
            self.service.leave(ROOM)
        self.assertEqual(self.store.get_import(self.iid)['state'], 'consent_given')
        self.assertEqual(self.service.channel.calls, [])


if __name__ == '__main__':
    unittest.main()
