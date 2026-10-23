"""S2 control boundary tests, never native interoperability substitutes."""
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1] / 'server'))


class InviteFragmentTests(unittest.TestCase):
    def test_fragment_is_only_source_of_secret(self):
        import lobby_control as c
        secret = 'x' * 43
        parsed = c.parse_invitation('https://rooms.lab/invite/lobby-a/invite-b#' + secret)
        self.assertEqual(parsed, ('lobby-a', 'invite-b', secret))
        for url in ('https://rooms.lab/invite/lobby-a/invite-b?secret=' + secret,
                    'https://evil.lab/invite/lobby-a/invite-b#' + secret,
                    'https://user@rooms.lab/invite/lobby-a/invite-b#' + secret,
                    'https://rooms.lab/invite/lobby-a/invite-b?q=x#' + secret):
            with self.assertRaisesRegex(c.ControlError, '^invalid_invitation$'):
                c.parse_invitation(url)


class LocalOwnerTests(unittest.TestCase):
    def test_only_private_local_owner_token_can_approve(self):
        import tempfile
        import json
        import urllib.request
        import urllib.error
        from lobby_owner import OwnerSurface
        root = Path('/Users/albi/.hermes/cache/scratch/room-network-spike-20261011-164930')
        calls = []
        with tempfile.TemporaryDirectory(prefix='s2-owner-', dir=root) as tmp:
            with OwnerSurface(Path(tmp), {'approve': lambda payload: calls.append(payload) or {'approved': True}}) as owner:
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                def request(token, path='/owner', origin=None):
                    headers = {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'}
                    if origin:
                        headers['Origin'] = origin
                    req = urllib.request.Request(owner.base + path, data=b'{"op":"approve","invite_id":"invite-a"}', headers=headers)
                    try:
                        with opener.open(req, timeout=5) as response:
                            return response.status, json.load(response)
                    except urllib.error.HTTPError as exc:
                        try:
                            return exc.code, json.load(exc)
                        finally:
                            exc.close()
                self.assertEqual(request('wrong'), (403, {'error': 'owner_required'}))
                self.assertEqual(request(owner.token, '/owner?token=' + owner.token)[0], 403)
                self.assertEqual(request(owner.token, origin='https://evil.lab')[0], 403)
                self.assertEqual(calls, [])
                self.assertEqual(request(owner.token), (200, {'approved': True}))
                self.assertEqual(calls, [{'invite_id': 'invite-a'}])
                self.assertEqual((Path(tmp) / 'owner.token').stat().st_mode & 0o777, 0o600)
                self.assertNotIn(owner.token, repr(owner))


class RealControlTests(unittest.TestCase):
    def test_remote_redemption_is_atomic_and_certificate_bound(self):
        import tempfile
        import concurrent.futures
        import lobby_control as c
        from lobby_control_lab import ControlLab
        from room_invite_store import RoomInviteStore
        root = Path('/Users/albi/.hermes/cache/scratch/room-network-spike-20261011-164930')
        with tempfile.TemporaryDirectory(prefix='s2-race-', dir=root) as tmp:
            with ControlLab(Path(tmp)) as lab:
                store = RoomInviteStore(Path(tmp) / 'mc.sqlite')
                lobby = store.create_lobby(name='Race lab', roster=lab.roster)
                invite = store.issue_invite(lobby['lobby_id'], 'peer-b', invitee_label='Known owner')
                lab.serve(c.HostLobbyControl(store, lobby['lobby_id'], lab.binding))
                def redeem(_):
                    try:
                        return lab.call({'op': 'redeem', 'invite_id': invite.invite_id, 'secret': invite.secret})['status']
                    except c.ControlError as exc:
                        return str(exc)
                with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
                    results = list(pool.map(redeem, range(20)))
                self.assertEqual(results.count('awaiting_host_approval'), 1, results)
                self.assertEqual(results.count('control_unavailable'), 19, results)
                result = store.get_redemption(lobby['lobby_id'], invite.invite_id)
                self.assertEqual({k: result[k] for k in lab.binding}, lab.binding)
                stored = b''.join(p.read_bytes() for p in Path(tmp).glob('mc.sqlite*'))
                self.assertNotIn(invite.secret.encode(), stored)
                self.assertNotIn(invite.secret.encode(), b''.join(lab.observed))

    def test_real_socket_negatives_are_sanitized_and_never_enroll(self):
        import tempfile
        import time
        import json
        import lobby_control as c
        from lobby_control_lab import ControlLab
        from room_invite_store import RoomInviteStore
        from tls_stream import wrap_client, SequencedTLS
        root = Path('/Users/albi/.hermes/cache/scratch/room-network-spike-20261011-164930')
        with tempfile.TemporaryDirectory(prefix='s2-negative-', dir=root) as tmp:
            with ControlLab(Path(tmp)) as lab:
                store = RoomInviteStore(Path(tmp) / 'mc.sqlite')
                lobby = store.create_lobby(name='Negative lab', roster=lab.roster)
                lab.serve(c.HostLobbyControl(store, lobby['lobby_id'], lab.binding))
                invite = store.issue_invite(lobby['lobby_id'], 'peer-b', invitee_label='Known owner')
                attempts = [
                    {'op': 'redeem', 'invite_id': invite.invite_id, 'secret': 'x' * 43},
                    {'op': 'redeem', 'invite_id': invite.invite_id, 'secret': invite.secret, 'binding': lab.binding},
                    {'op': 'redeem', 'invite_id': invite.invite_id, 'secret': invite.secret, 'lobby_id': 'lobby-wrong'},
                    {'op': 'groups.peer.invite', 'invite_id': invite.invite_id},
                    {'op': 'approve', 'invite_id': invite.invite_id},
                    {'op': 'consent', 'invite_id': invite.invite_id, 'consented': True},
                    {'op': 'preview', 'invite_id': invite.invite_id, 'url': 'http://127.0.0.1:9119/'},
                ]
                for payload in attempts:
                    with self.assertRaisesRegex(c.ControlError, '^control_unavailable$'):
                        lab.call(payload)
                wrong = RoomInviteStore(Path(tmp) / 'wrong.sqlite')
                wrong_lobby = wrong.create_lobby(name='Other lab', roster=lab.roster)
                wrong_invite = wrong.issue_invite(wrong_lobby['lobby_id'], 'peer-b', invitee_label='Known owner')
                with self.assertRaisesRegex(c.ControlError, '^control_unavailable$'):
                    lab.call({'op': 'redeem', 'invite_id': wrong_invite.invite_id, 'secret': wrong_invite.secret})
                # Pin failure occurs before ANY application request is sent.
                old_pin = lab.joining.pin
                lab.joining.pin = '0' * 64
                with self.assertRaisesRegex(c.ControlError, '^control_ambiguous$'):
                    lab.call({'op': 'redeem', 'invite_id': invite.invite_id, 'secret': invite.secret})
                lab.joining.pin = old_pin
                for raw in (b'{"op":"preview","op":"redeem"}', b'[]', b'{"op":NaN}',
                            b'{"op":"preview","invite_id":{}}', b'\xff', b'{'):
                    sock = lab.joining.client.open_stream()
                    sock.settimeout(10)
                    try:
                        with wrap_client(sock, lab.joining.client_tls, expected_fingerprint=old_pin) as tls:
                            frames = SequencedTLS(tls, deadline=time.monotonic() + 10)
                            frames.send(raw)
                            response = json.loads(frames.receive()[1])
                            self.assertEqual(response, {'error': 'control_unavailable'})
                    finally:
                        sock.close()
                store.revoke_invite(lobby['lobby_id'], invite.invite_id)
                with self.assertRaisesRegex(c.ControlError, '^control_unavailable$'):
                    lab.call({'op': 'redeem', 'invite_id': invite.invite_id, 'secret': invite.secret})
                expiring = store.issue_invite(lobby['lobby_id'], 'peer-b', invitee_label='Known owner', ttl_seconds=1)
                store.clock = lambda: time.time() + 2
                with self.assertRaisesRegex(c.ControlError, '^control_unavailable$'):
                    lab.call({'op': 'redeem', 'invite_id': expiring.invite_id, 'secret': expiring.secret})
                with store._connection() as db:
                    self.assertEqual(db.execute('SELECT count(*) FROM redemptions').fetchone()[0], 0)
                self.assertNotIn(invite.secret.encode(), b''.join(lab.observed))

    def test_preview_over_wss_and_pinned_mtls_has_no_native_effect(self):
        import tempfile
        import lobby_control as c
        from lobby_control_lab import ControlLab
        from room_invite_store import RoomInviteStore
        root = Path('/Users/albi/.hermes/cache/scratch/room-network-spike-20261011-164930')
        with tempfile.TemporaryDirectory(prefix='s2-unit-', dir=root) as tmp:
            with ControlLab(Path(tmp)) as lab:
                store = RoomInviteStore(Path(tmp) / 'mc.sqlite')
                lobby = store.create_lobby(name='Preview lab', roster=lab.roster)
                invite = store.issue_invite(lobby['lobby_id'], 'peer-b', invitee_label='Known owner')
                lab.serve(c.HostLobbyControl(store, lobby['lobby_id'], lab.binding))
                for wrong in (dict(lab.binding, fingerprint='0' * 64),
                              dict(lab.binding, target_install_id=lab.host_id)):
                    with self.assertRaisesRegex(c.ControlError, '^peer_binding_mismatch$'):
                        lab.serve(c.HostLobbyControl(store, lobby['lobby_id'], wrong))
                preview = lab.call({'op': 'preview', 'invite_id': invite.invite_id})
                self.assertEqual(preview['name'], 'Preview lab')
                self.assertNotIn(invite.secret, str(preview))
                self.assertEqual(store.get_lobby(lobby['lobby_id'])['state'], 'open')
                self.assertIsNone(store.get_lobby(lobby['lobby_id'])['room_id'])
                self.assertTrue(lab.observed)
                self.assertNotIn(b'Preview lab', b''.join(lab.observed))


if __name__ == '__main__':
    unittest.main()
