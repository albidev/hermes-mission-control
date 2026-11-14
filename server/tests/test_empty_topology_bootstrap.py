"""Cold-start bootstrap with real empty journals, listeners, pairing and deny-all data.

No native process/model. Pairing catalog is a labelled local fixture; native RPC
must never run for bootstrap. Config maps are destinations, not consent/READY.
"""
from __future__ import annotations
import copy
import json
import socket
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

import pairing_support as ps
from invite_rooms import runtime_config as rc
from invite_rooms import sidecar_data_plane as sdp
from invite_rooms.pair_ack_sender import pair_ack_sender


def port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def ep(p):
    return {'host': '127.0.0.1', 'port': p}


class EmptyTopologyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='g2-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.root.chmod(0o700)
        self.A = ps.Side(self.root, 'a', ps.INSTALL_A)
        self.B = ps.Side(self.root, 'b', ps.INSTALL_B)
        self.ports = {name: port() for name in ('ac', 'ad', 'bc', 'bd', 'target', 'dest')}
        self.planes, self.loaded = [], []
        self.addCleanup(self.close_all)

    def close_all(self):
        for plane in reversed(self.planes):
            plane.close()
        for service, _, _ in self.loaded:
            service.registry._receipt_store.close()
            # RoomInviteStore uses per-operation connections; it owns no close handle.

    def config(self, side, other, control, data, remote_control):
        native = ps.private_dir(side.root / 'native')
        (native / 'install_id').write_text(side.installation_id.removeprefix('install:') + '\n')
        (native / 'install_id').chmod(0o600)
        (native / 'config.yaml').write_text('{}')
        ps.private_dir(side.root / 'invites')
        peer = 'peer-' + other.installation_id.removeprefix('install:')
        return {'store_path': str(side.root / 'invites' / 'invites.sqlite'),
                'installation_id': side.installation_id, 'profile': 'default',
                'peer_registry_path': str(side.peers_path), 'native_rpc': {'profile_home': str(native)},
                'target_http': {'base_url': 'http://127.0.0.1:1', 'profile': 'default'},
                'data_listen': ep(data),
                'joining': {'store_dir': str(side.root / 'joining'), 'identity_dir': str(side.identity_dir),
                            'hosts_path': str(side.root / 'cfg' / 'hosts.json'),
                            'origins': {peer: 'https://lab.example'}, 'control_endpoints': {peer: ep(remote_control)}},
                'pairing': {'identity_dir': str(side.identity_dir), 'journal_dir': str(side.journal_dir),
                            'peers_path': str(side.peers_path), 'control_listen': ep(control),
                            'control_endpoints': {peer: ep(remote_control)},
                            'target_listeners': {}, 'data_endpoints': {}}}

    def data(self):
        return self.config(self.A, self.B, self.ports['ac'], self.ports['ad'], self.ports['bc'])

    def load(self, side, data):
        path = side.root / 'cfg' / 'runtime.json'
        path.write_text(json.dumps(data))
        path.chmod(0o600)
        loaded = rc.load_service({'MISSION_CONTROL_ROOM_INVITES_CONFIG': str(path)})
        if loaded is not None:
            self.loaded.append(loaded)
        return loaded

    def plane(self, side, data):
        loaded = self.load(side, data)
        self.assertIsNotNone(loaded, 'explicit empty topology must load')
        plane = sdp.build_sidecar_data_plane(loaded)
        self.assertIsNotNone(plane, 'empty topology must assemble')
        self.planes.append(plane)
        return plane

    def test_explicit_empty_maps_are_immutable_and_missing_stays_incomplete(self):
        for parser in (rc.parse_target_listeners, rc.parse_data_endpoints):
            parsed = parser({})
            self.assertEqual(dict(parsed), {})
            with self.assertRaises(TypeError):
                parsed['x'] = ()
        for key in ('target_listeners', 'data_endpoints'):
            data = self.data()
            del data['pairing'][key]
            loaded = self.load(self.A, data)
            self.assertIsNotNone(loaded)
            self.assertIsNone(sdp.build_sidecar_data_plane(loaded))

    def test_empty_bootstrap_owns_control_data_and_pair_ack_after_real_confirmations(self):
        a = self.plane(self.A, self.data())
        b = self.plane(self.B, self.config(self.B, self.A, self.ports['bc'], self.ports['bd'], self.ports['ac']))
        self.assertEqual(a.journal.snapshot().links, {})
        self.assertEqual(b.journal.snapshot().links, {})
        with patch('invite_rooms.routes._connect_native_rpc', side_effect=AssertionError('no native bootstrap')):
            a.start()
            b.start()
            self.assertTrue(a.adapter.is_running())
            for name in ('ac', 'bc'):
                with socket.create_connection(('127.0.0.1', self.ports[name]), timeout=1):
                    pass
            self.assertEqual(a.scope_lifecycle.snapshot(), ())
            self.assertEqual(b.scope_lifecycle.snapshot(), ())
            # Real owner decisions in a disposable test, with fixture-only catalogs.
            with ps.fast_rsa():
                sa, sb = self.A.service(), self.B.service()
                paired = ps.drive_pairing(sa, sb)
            result = sa.send_pair_ack(paired['a_id'], pair_ack_sender(a.config, timeout=3))
            self.assertIsNotNone(result)
            self.assertTrue(all(link['protocol_state'] == 'acknowledged' for link in a.journal.snapshot().links.values()))
            self.assertTrue(all(link['protocol_state'] == 'acknowledged' for link in b.journal.snapshot().links.values()))
            for plane in (a, b):
                self.assertEqual(plane.scope_lifecycle.snapshot(), ())
                for aid in ('link-' + '0' * 32, 'link-' + 'f' * 32):
                    url = f'http://127.0.0.1:{plane.adapter_bind[1]}/links/{aid}/v1/runs'
                    request = urllib.request.Request(url, data=b'{}', headers={'Content-Type': 'application/json'})
                    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                    with self.assertRaises(urllib.error.HTTPError) as refused:
                        opener.open(request, timeout=2)
                    self.assertIn(refused.exception.code, (403, 404))
                    refused.exception.close()
            a.close()
            b.close()
        for name in ('ac', 'ad', 'bc', 'bd'):
            with self.assertRaises(OSError):
                socket.create_connection(('127.0.0.1', self.ports[name]), timeout=0.2)

    def test_populated_maps_do_not_create_unapproved_route_or_ready_scope(self):
        data = self.data()
        aid, peer = 'link-' + '1' * 32, 'peer-' + 'b' * 32
        data['pairing']['target_listeners'] = {aid: ep(self.ports['target'])}
        data['pairing']['data_endpoints'] = {peer: {aid: ep(self.ports['dest'])}}
        plane = self.plane(self.A, data)
        plane.start()
        self.assertEqual(plane.scope_lifecycle.snapshot(), ())
        self.assertEqual(plane.journal.snapshot().links, {})
        with self.assertRaises(Exception):
            plane.scope_lifecycle.ensure_target(endpoint_id='unknown', generation=1,
                lobby_id='unknown', invite_id='unknown', deadline=time.monotonic() + 1)
        with self.assertRaises(OSError):
            socket.create_connection(('127.0.0.1', self.ports['target']), timeout=0.2)

    def test_nonempty_validation_and_aliases_still_fail_closed(self):
        aid, peer = 'link-' + '1' * 32, 'peer-' + 'b' * 32
        for parser, values in ((rc.parse_target_listeners, [None, [], {'*': ep(1234)}, {aid: ep(1234) | {'host': '0.0.0.0'}},
                {aid: ep(1234), 'link-' + '2' * 32: ep(1234)}]),
                (rc.parse_data_endpoints, [None, [], {peer: {}}, {'*': {aid: ep(1234)}},
                {peer: {aid: ep(1234), 'link-' + '2' * 32: ep(1234)}}])):
            for value in values:
                with self.subTest(value=value), self.assertRaises(rc.RuntimeConfigError):
                    parser(value)
        data = self.data()
        data['pairing']['data_endpoints'] = {peer: {aid: ep(self.ports['bc'])}}
        self.assertIsNone(self.load(self.A, data))
        path = self.A.root / 'cfg' / 'runtime.json'
        path.write_text(json.dumps(self.data()).replace('"target_listeners": {}', '"target_listeners": {}, "target_listeners": {}'))
        self.assertIsNone(rc._load_config_file(str(path)))


if __name__ == '__main__':
    unittest.main()
