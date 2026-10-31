"""Remaining preview/redeem adversaries over actual outbound relay + inner mTLS."""
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest

HERE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(HERE), str(HERE.parents[1] / 'server')]
ROOT = Path('/Users/albi/.hermes/cache/scratch/room-network-spike-20261011-164930')


class RemainingControlTests(unittest.TestCase):
    def test_preview_redemption_certificate_route_and_bounds_matrix(self):
        from lobby_control_lab import ControlLab
        from lobby_control import HostLobbyControl, ControlError
        from room_invite_store import RoomInviteStore
        from tls_stream import wrap_client, SequencedTLS, client_context
        import pki
        with tempfile.TemporaryDirectory(dir=ROOT, prefix='s2d-adversary-') as tmp:
            root = Path(tmp)
            with ControlLab(root / 'network') as network:
                store = RoomInviteStore(root / 'mc.sqlite')
                lobby = store.create_lobby(name='Adversarial preview/redemption', roster=network.roster)
                invite = store.issue_invite(lobby['lobby_id'], 'peer-b', invitee_label='Known owner')
                network.serve(HostLobbyControl(store, lobby['lobby_id'], network.binding))
                requests = [{'op': 'preview', 'invite_id': invite.invite_id},
                            {'op': 'redeem', 'invite_id': invite.invite_id, 'secret': invite.secret}]
                foreign = pki.init_owner(root / 'foreign', 'foreign')
                # Certificate chains to an unknown owner, rejected by server CA trust.
                original = network.joining.client_tls
                foreign_context = client_context(ca_file=network.owner_a['ca_file'], cert_file=foreign['cert_file'], key_file=foreign['key_file'])
                for request in requests:
                    with self.subTest(op=request['op'], fault='wrong_presented_cert'):
                        network.joining.client_tls = foreign_context
                        try:
                            with self.assertRaisesRegex(ControlError, '^control_ambiguous$'):
                                network.call(request)
                        finally:
                            network.joining.client_tls = original
                    for field in ('link_id', 'room_id', 'peer_identity'):
                        with self.subTest(op=request['op'], fault='routing_' + field):
                            client = network.joining.client
                            old = getattr(client, field)
                            setattr(client, field, old + '-wrong')
                            try:
                                with self.assertRaises(ControlError) as raised:
                                    network.call(request)
                                self.assertIn(str(raised.exception), {'control_unavailable', 'control_ambiguous'})
                            finally:
                                setattr(client, field, old)
                                # Invalid OPEN tears down the authenticated relay session.
                                # Use a NEW connector, not an old backoff/READY observation.
                                client.stop()
                                from connector import RelayClient
                                from lobby_control import PinnedControl
                                fresh = RelayClient(relay_url=client.relay_url, identity=client.identity,
                                    peer_identity=client.peer_identity, link_id=client.link_id, room_id=client.room_id,
                                    credential=client._credential, ssl_context=client.ssl_context)
                                network.clients.append(fresh)
                                fresh.start()
                                network.joining = PinnedControl(fresh, {'peer_ca_file': network.owner_a['ca_file'],
                                    'peer_fingerprint': network.owner_a['fingerprint'], 'cert_file': network.owner_b['cert_file'],
                                    'key_file': network.owner_b['key_file']})
                    raws = [json.dumps(dict(request, pad='x' * (25 * 1024))).encode(),
                            json.dumps(request)[:-1].encode() + b',"op":"redeem"}',
                            json.dumps(dict(request, url='http://127.0.0.1:9119/')).encode()]
                    for raw in raws:
                        with self.subTest(op=request['op'], fault='bounds_or_duplicate_or_url'):
                            sock = network.joining.client.open_stream()
                            sock.settimeout(10)
                            try:
                                with wrap_client(sock, original, expected_fingerprint=network.joining.pin) as tls:
                                    frames = SequencedTLS(tls, deadline=time.monotonic() + 10)
                                    frames.send(raw)
                                    self.assertEqual(json.loads(frames.receive()[1]), {'error': 'control_unavailable'})
                            finally:
                                sock.close()
                with store._connection() as db:
                    self.assertEqual(db.execute('SELECT count(*) FROM redemptions').fetchone()[0], 0)
                self.assertEqual(store.get_lobby(lobby['lobby_id'])['state'], 'open')
                self.assertNotIn(invite.secret.encode(), b''.join(network.observed))
                # Untampered legitimate request still works, then semantic redemption replay fails.
                self.assertEqual(network.call(requests[0])['name'], lobby['name'])
                self.assertEqual(network.call(requests[1]), {'status': 'awaiting_host_approval'})
                with self.assertRaisesRegex(ControlError, '^control_unavailable$'):
                    network.call(requests[1])
            self.assertTrue(network.loop.is_closed())


if __name__ == '__main__':
    unittest.main()
