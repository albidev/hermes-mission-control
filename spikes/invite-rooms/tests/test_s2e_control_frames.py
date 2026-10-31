"""Additional real control-channel counterexamples; no mocked native admission."""
import json
from pathlib import Path
import struct
import sys
import tempfile
import time
import unittest

HERE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(HERE), str(HERE.parents[1] / 'server')]
ROOT = Path('/Users/albi/.hermes/cache/scratch/room-network-spike-20261011-164930')


class ControlFrameTests(unittest.TestCase):
    def test_same_ca_wrong_leaf_and_sequence_frame_replays_never_redeem(self):
        from lobby_control_lab import ControlLab
        from lobby_control import HostLobbyControl, ControlError
        from room_invite_store import RoomInviteStore
        from tls_stream import wrap_client, client_context, SequencedTLS
        import pki
        with tempfile.TemporaryDirectory(dir=ROOT,prefix='s2e-frames-') as tmp:
            root = Path(tmp)
            with ControlLab(root/'network') as n:
                store = RoomInviteStore(root/'mc.sqlite')
                lobby = store.create_lobby(name='Wire counterexamples',roster=n.roster)
                invite = store.issue_invite(lobby['lobby_id'],'peer-b',invitee_label='Known owner')
                n.serve(HostLobbyControl(store,lobby['lobby_id'],n.binding))
                # Valid CA chain, wrong actual leaf: isolates PIN from unknown-CA rejection.
                alternate = root/'alternate'
                alternate.mkdir(mode=0o700)
                key,csr,leaf = (alternate/name for name in ('leaf.key','leaf.csr','leaf.pem'))
                pki._openssl('req','-new','-nodes','-newkey','rsa:2048','-keyout',str(key),'-out',str(csr),'-subj','/CN=alternate-known-ca')
                key.chmod(0o600)
                ca = Path(n.owner_b['ca_file'])
                pki._openssl('x509','-req','-in',str(csr),'-CA',str(ca),'-CAkey',str(ca.with_name('ca.key')),
                             '-CAcreateserial','-out',str(leaf),'-days','1')
                original = n.joining.client_tls
                other = client_context(ca_file=n.owner_a['ca_file'],cert_file=str(leaf),key_file=str(key))
                requests = [dict(op='preview',invite_id=invite.invite_id),
                            dict(op='redeem',invite_id=invite.invite_id,secret=invite.secret)]
                for request in requests:
                    n.joining.client_tls = other
                    try:
                        with self.assertRaisesRegex(ControlError,'^control_ambiguous$'):
                            n.call(request)
                    finally:
                        n.joining.client_tls = original
                    payload=json.dumps(request).encode()
                    wires=[]
                    for sequence in (1,-1,True,'0'):
                        raw=json.dumps(dict(sequence=sequence,payload=payload.hex())).encode()
                        wires.append(struct.pack('!I',len(raw))+raw)
                    wires += [struct.pack('!I',0),struct.pack('!I',65537),struct.pack('!I',1)+b'{']
                    for wire in wires:
                        with self.subTest(op=request['op'],wire_length=len(wire)):
                            sock=n.joining.client.open_stream()
                            sock.settimeout(10)
                            try:
                                with wrap_client(sock,original,expected_fingerprint=n.joining.pin) as tls:
                                    tls.sendall(wire)
                                    frames=SequencedTLS(tls,deadline=time.monotonic()+10)
                                    self.assertEqual(json.loads(frames.receive()[1]),{'error':'control_ambiguous'})
                            finally:
                                sock.close()
                # Replaying an application envelope on the SAME TLS stream does not
                # open a second operation: this protocol intentionally permits one.
                sock=n.joining.client.open_stream()
                sock.settimeout(10)
                try:
                    with wrap_client(sock,original,expected_fingerprint=n.joining.pin) as tls:
                        def envelope(payload):
                            raw=json.dumps(dict(sequence=0,payload=json.dumps(payload).encode().hex())).encode()
                            return struct.pack('!I',len(raw))+raw
                        tls.sendall(envelope(requests[0])+envelope(requests[1]))
                        frames=SequencedTLS(tls,deadline=time.monotonic()+10)
                        self.assertEqual(json.loads(frames.receive()[1])['result']['name'],lobby['name'])
                        self.assertEqual(tls.recv(1),b'')
                finally:
                    sock.close()
                with store._connection() as db:
                    self.assertEqual(db.execute('SELECT count(*) FROM redemptions').fetchone()[0],0)
                self.assertNotIn(invite.secret.encode(),b''.join(n.observed))
                self.assertEqual(n.call(requests[1]),{'status':'awaiting_host_approval'})
                with self.assertRaisesRegex(ControlError,'^control_unavailable$'):
                    n.call(requests[1])


if __name__=='__main__':
    unittest.main()
