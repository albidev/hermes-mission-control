"""Criterion-3 real local-owner / WSS relay / pinned mTLS boundary."""
import sys
import json
import tempfile
import unittest
import urllib.request
import urllib.error
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(HERE), str(HERE.parents[1] / 'server')]
ROOT = Path('/Users/albi/.hermes/cache/scratch/room-network-spike-20261011-164930')


def post(owner, payload, token=None):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(owner.base + '/owner', data=json.dumps(payload).encode(),
        headers={'Authorization': 'Bearer ' + (owner.token if token is None else token), 'Content-Type': 'application/json'})
    try:
        with opener.open(request, timeout=15) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.load(exc)
        finally:
            exc.close()


class OwnerConsentTests(unittest.TestCase):
    def test_host_approval_is_local_authenticated_and_does_not_enroll(self):
        from lobby_control import HostLobbyControl, ControlError
        from lobby_control_lab import ControlLab
        from lobby_owner import OwnerSurface, HostOwner
        from room_invite_store import RoomInviteStore
        with tempfile.TemporaryDirectory(prefix='s2b-host-', dir=ROOT) as tmp:
            root = Path(tmp)
            with ControlLab(root) as lab:
                store = RoomInviteStore(root / 'mc.sqlite')
                lobby = store.create_lobby(name='Two owner lab', roster=lab.roster)
                lid = lobby['lobby_id']
                invite = store.issue_invite(lid, 'peer-b', invitee_label='Known owner')
                lab.serve(HostLobbyControl(store, lid, lab.binding))
                local = HostOwner(store, lid, lab.binding)
                with OwnerSurface(root / 'host-owner', {'approve': local.approve}) as host:
                    payload = {'op': 'approve', 'lobby_id': lid, 'invite_id': invite.invite_id}
                    self.assertEqual(post(host, payload, 'wrong')[0], 403)
                    self.assertEqual(post(host, payload)[0], 409)  # not redeemed
                    lab.call({'op': 'redeem', 'invite_id': invite.invite_id, 'secret': invite.secret})
                    for bad in (dict(payload, lobby_id='lobby-wrong'), dict(payload, invite_id='invite-wrong'),
                                dict(payload, approved=True), dict(payload, binding=lab.binding)):
                        self.assertEqual(post(host, bad), (409, {'error': 'owner_operation_blocked'}))
                    with self.assertRaisesRegex(ControlError, '^control_unavailable$'):
                        lab.call({'op': 'approve', 'invite_id': invite.invite_id})
                    self.assertEqual(post(host, payload), (200, {'status': 'consent_required'}))
                    before = store.get_redemption(lid, invite.invite_id)
                    self.assertEqual(post(host, payload)[0], 200)
                    self.assertEqual(store.get_redemption(lid, invite.invite_id), before)
                    self.assertEqual(before['joining_consent'], 'pending')
                    self.assertEqual(store.get_lobby(lid)['state'], 'open')
                    with store._connection() as db:
                        self.assertEqual(db.execute('SELECT count(*) FROM peer_links').fetchone()[0], 0)
                    self.assertNotIn(host.token.encode(), store.path.read_bytes())
                    self.assertNotIn(host.token.encode(), b''.join(lab.observed))

    def test_joining_consent_needs_own_local_auth_and_signed_exact_scope(self):
        from lobby_control import HostLobbyControl, ControlError
        from lobby_control_lab import ControlLab
        from lobby_owner import OwnerSurface, HostOwner, JoiningOwner
        from room_invite_store import RoomInviteStore, InviteError
        with tempfile.TemporaryDirectory(prefix='s2b-join-', dir=ROOT) as tmp:
            root = Path(tmp)
            with ControlLab(root) as lab:
                store = RoomInviteStore(root / 'mc.sqlite')
                lobby = store.create_lobby(name='Separate joining owner', roster=lab.roster)
                lid = lobby['lobby_id']
                invite = store.issue_invite(lid, 'peer-b', invitee_label='Known owner')
                join = JoiningOwner(root / 'joining-private', lab.joining, lid, lab.binding, lab.host_id)
                # Public Room/member scope must be owner-confirmed before local consent.
                join.confirm_scope(lobby['planned_room_id'], 'peer-b')
                lab.serve(HostLobbyControl(store, lid, lab.binding, joining_owner_key=join.public_key, host_id=lab.host_id))
                with OwnerSurface(root / 'host-owner', {'approve': HostOwner(store, lid, lab.binding).approve}) as host, \
                        OwnerSurface(root / 'joining-owner', {'consent': join.consent}) as target:
                    self.assertNotEqual(host.token, target.token)
                    payload = {'lobby_id': lid, 'invite_id': invite.invite_id}
                    consent = dict(payload, op='consent')
                    self.assertEqual(post(target, consent)[0], 409)  # no redemption
                    lab.call({'op': 'redeem', 'invite_id': invite.invite_id, 'secret': invite.secret})
                    self.assertEqual(post(target, consent)[0], 409)  # host not approved
                    self.assertEqual(post(target, consent, host.token)[0], 403)
                    self.assertFalse(join.has_consent(invite.invite_id))
                    self.assertEqual(post(host, dict(payload, op='approve'))[0], 200)
                    unconfirmed = JoiningOwner(root / 'unconfirmed-private', lab.joining, lid, lab.binding, lab.host_id)
                    with self.assertRaisesRegex(ControlError, '^owner_scope_required$'):
                        unconfirmed.consent(payload)
                    unconfirmed.confirm_scope('invite-room-wrong', 'peer-b')
                    with self.assertRaisesRegex(ControlError, '^host_approval_required$'):
                        unconfirmed.consent(payload)
                    self.assertFalse(unconfirmed.has_consent(invite.invite_id))
                    for bad in (dict(consent, lobby_id='lobby-wrong'), dict(consent, invite_id='invite-wrong'),
                                dict(consent, consented=True)):
                        self.assertEqual(post(target, bad)[0], 409)
                    for raw in ({'op': 'joining_consent', 'invite_id': invite.invite_id, 'consented': True},
                                {'op': 'joining_consent', 'invite_id': invite.invite_id, 'receipt': {}, 'signature': 'forged'}):
                        with self.assertRaisesRegex(ControlError, '^control_unavailable$'):
                            lab.call(raw)
                    # Contract: revocation is supported only BEFORE redemption.
                    with self.assertRaisesRegex(InviteError, '^invite_unavailable$'):
                        store.revoke_invite(lid, invite.invite_id)
                    self.assertEqual(store.get_redemption(lid, invite.invite_id)['status'], 'consent_required')
                    self.assertEqual(post(target, consent), (200, {'status': 'ready'}))
                    before = store.get_redemption(lid, invite.invite_id)
                    self.assertEqual(post(target, consent), (200, {'status': 'ready'}))
                    self.assertEqual(store.get_redemption(lid, invite.invite_id), before)
                    self.assertTrue(join.has_consent(invite.invite_id))
                    import base64
                    from lobby_consent import canonical
                    with join._db() as db:
                        receipt_raw, signature = db.execute('SELECT receipt,signature FROM consents WHERE invite_id=?', (invite.invite_id,)).fetchone()
                    receipt = json.loads(receipt_raw)
                    for key, value in [('room_id', 'invite-room-other'), ('lobby_id', 'lobby-other'),
                                       ('host_id', lab.target_id), ('member_id', 'other'),
                                       ('invite_id', 'invite-other')]:
                        changed = dict(receipt, **{key: value})
                        # Even a valid signature by the known owner cannot alter approved scope.
                        signed = base64.b64encode(join._key.sign(canonical(changed))).decode()
                        with self.assertRaisesRegex(ControlError, '^control_unavailable$'):
                            lab.call({'op': 'joining_consent', 'invite_id': invite.invite_id,
                                      'receipt': changed, 'signature': signed})
                    with self.assertRaisesRegex(ControlError, '^control_unavailable$'):
                        lab.call({'op': 'joining_consent', 'invite_id': invite.invite_id,
                                  'receipt': receipt, 'signature': 'A' * 88})
                    self.assertEqual(lab.call({'op': 'joining_consent', 'invite_id': invite.invite_id,
                                              'receipt': receipt, 'signature': signature}), {'status': 'ready'})
                    self.assertEqual(store.get_redemption(lid, invite.invite_id), before)
                    # Fresh local workflow instance resumes its own stable signed receipt.
                    resumed = JoiningOwner(root / 'joining-private', lab.joining, lid, lab.binding, lab.host_id)
                    self.assertEqual(resumed.consent(payload), {'status': 'ready'})
                    self.assertEqual(store.get_redemption(lid, invite.invite_id), before)
                    self.assertEqual(store.get_lobby(lid)['state'], 'open')
                    self.assertIsNone(store.get_lobby(lid)['room_id'])
                    with store._connection() as db:
                        self.assertEqual(db.execute('SELECT count(*) FROM peer_links').fetchone()[0], 0)
                    for token in (host.token, target.token):
                        self.assertNotIn(token.encode(), b''.join(lab.observed))
                        for db in list(root.glob('*.sqlite*')) + list((root / 'joining-private').glob('*.sqlite*')):
                            self.assertNotIn(token.encode(), db.read_bytes())
