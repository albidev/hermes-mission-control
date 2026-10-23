"""Criterion-4 journal safety; unit storage tests do not mint native authority."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(HERE), str(HERE.parents[1] / 'server')]
ROOT = Path('/Users/albi/.hermes/cache/scratch/room-network-spike-20261011-164930')


class MintConsentTests(unittest.TestCase):
    def test_local_authenticated_ambiguous_state_is_typed_not_success(self):
        from lobby_material import MintAmbiguous
        from lobby_owner import OwnerSurface
        from test_s2b_owner import post
        def blocked(payload):
            raise MintAmbiguous()
        with tempfile.TemporaryDirectory(prefix='s2c-typed-', dir=ROOT) as tmp:
            with OwnerSurface(Path(tmp), {'mint': blocked}) as owner:
                self.assertEqual(post(owner, {'op': 'mint'}), (409, {'error': 'mint_ambiguous'}))
                self.assertEqual(post(owner, {'op': 'mint'}, 'wrong'), (403, {'error': 'owner_required'}))

    def test_mint_requires_both_consents_and_own_local_owner_auth(self):
        import asyncio
        from lobby_material import TargetMint, MintJournal
        from lobby_control import HostLobbyControl
        from lobby_control_lab import ControlLab
        from lobby_owner import JoiningOwner, HostOwner, OwnerSurface
        from room_invite_store import RoomInviteStore
        from test_s2b_owner import post
        with tempfile.TemporaryDirectory(prefix='s2c-consent-', dir=ROOT) as tmp:
            root = Path(tmp)
            with ControlLab(root / 'network') as lab:
                store = RoomInviteStore(root / 'mc.sqlite')
                lobby = store.create_lobby(name='Mint preconditions only', roster=lab.roster)
                lid = lobby['lobby_id']
                invite = store.issue_invite(lid, 'peer-b', invitee_label='Owner')
                join = JoiningOwner(root / 'joining', lab.joining, lid, lab.binding, lab.host_id)
                join.confirm_scope(lobby['planned_room_id'], 'peer-b')
                lab.serve(HostLobbyControl(store, lid, lab.binding, joining_owner_key=join.public_key, host_id=lab.host_id))
                calls = []
                async def forbidden_rpc(*args):
                    calls.append(args[0])
                    raise AssertionError('native must not be called')
                journal = MintJournal(root / 'journal')
                mint = TargetMint(join, journal, forbidden_rpc)
                def local_mint(payload):
                    return asyncio.run(mint.mint(payload))
                with OwnerSurface(root / 'host-owner', {'approve': HostOwner(store, lid, lab.binding).approve}) as host, \
                        OwnerSurface(root / 'target-owner', {'consent': join.consent, 'mint': local_mint}) as target:
                    payload = {'lobby_id': lid, 'invite_id': invite.invite_id}
                    for stage in ('unredeemed', 'redeemed', 'approved'):
                        if stage == 'redeemed':
                            lab.call({'op': 'redeem', 'invite_id': invite.invite_id, 'secret': invite.secret})
                        if stage == 'approved':
                            self.assertEqual(post(host, dict(payload, op='approve'))[0], 200)
                        self.assertEqual(post(target, dict(payload, op='mint'))[0], 409)
                        self.assertEqual(calls, [])
                    self.assertEqual(post(target, dict(payload, op='mint'), host.token)[0], 403)
                    self.assertEqual(calls, [])


class JournalTests(unittest.TestCase):
    def test_durable_intent_cannot_be_reclaimed_after_restart(self):
        from lobby_material import MintJournal, MintAmbiguous
        with tempfile.TemporaryDirectory(prefix='s2c-journal-', dir=ROOT) as tmp:
            scope = {'unit_scope': 'not-native-authority'}
            journal = MintJournal(Path(tmp))
            grant_id = journal.begin(scope)
            self.assertTrue(grant_id.startswith('grant-'))
            with self.assertRaisesRegex(MintAmbiguous, '^mint_ambiguous$'):
                journal.begin(scope)
            resumed = MintJournal(Path(tmp))
            with self.assertRaisesRegex(MintAmbiguous, '^mint_ambiguous$'):
                resumed.begin(scope)
            self.assertEqual(journal.state(scope), 'intent')
            self.assertEqual(os.stat(journal.path).st_mode & 0o777, 0o600)

    def test_completed_material_is_encrypted_immutable_and_scope_bound(self):
        from lobby_material import MintJournal
        from lobby_control import ControlError
        with tempfile.TemporaryDirectory(prefix='s2c-storage-', dir=ROOT) as tmp:
            journal = MintJournal(Path(tmp))
            scope = {'unit_scope': 'not-native-authority'}
            journal.begin(scope)
            value = {'sentinel': 'UNIT_STORAGE_SECRET_NOT_A_GRANT'}
            journal.complete(scope, value)
            self.assertEqual(MintJournal(Path(tmp)).load(scope), value)
            self.assertEqual(journal.state(scope), 'ready')
            self.assertNotIn(value['sentinel'].encode(), journal.path.read_bytes())
            with self.assertRaisesRegex(ControlError, '^material_unavailable$'):
                journal.load({'unit_scope': 'wrong'})
            with self.assertRaises(ControlError):
                journal.complete(scope, {'sentinel': 'changed'})
