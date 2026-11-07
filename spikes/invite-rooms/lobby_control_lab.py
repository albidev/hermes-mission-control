"""Explicit manual-preprovision bootstrap harness, isolated S2 lab only.

No synthetic native installation identities are created here: caller supplies
native IDs for native gates. Unit transport probes use owner endpoint IDs only
and cannot establish native interoperability.
"""
from __future__ import annotations
import asyncio
import base64
import hashlib
import ssl
import threading

import connector
import pki
import relay
import relay_daemon
from lobby_control import PinnedControl


class ControlLab:
    def __init__(self, root, *, host_id=None, target_id=None, owner_a=None, owner_b=None,
                 target_catalog=None, require_target_catalog=False):
        self.root = root
        self.host_id, self.target_id = host_id, target_id
        self.owner_a, self.owner_b = owner_a, owner_b
        self._require_target_catalog = require_target_catalog
        self._target_catalog = target_catalog
        if require_target_catalog and target_catalog is None:
            raise ValueError('target_catalog_required')
        if target_catalog is None:
            self._target_catalog = {
                'catalog_digest': hashlib.sha256(b'unit-control-lab-catalog').hexdigest(),
                'execution_policy': {'policy_digest': hashlib.sha256(b'unit-control-lab-policy').hexdigest()},
            }
        self._validate_target_catalog(self._target_catalog)
        self.clients = []
        self.observed = []
        self.loop = asyncio.new_event_loop()
        self.thread = None
        self.original = None
        self.daemon = None
        self._binding_override = None

    @staticmethod
    def _validate_target_catalog(catalog):
        if (not isinstance(catalog, dict) or not isinstance(catalog.get('catalog_digest'), str)
                or not isinstance(catalog.get('execution_policy'), dict)
                or not isinstance(catalog['execution_policy'].get('policy_digest'), str)
                or len(catalog['catalog_digest']) != 64 or len(catalog['execution_policy']['policy_digest']) != 64
                or set(catalog['catalog_digest']) - set('0123456789abcdef')
                or set(catalog['execution_policy']['policy_digest']) - set('0123456789abcdef')):
            raise ValueError('invalid_target_catalog')

    @property
    def binding(self):
        if self._binding_override is not None:
            return dict(self._binding_override)
        return {'endpoint_id': 'target-b', 'fingerprint': self.owner_b['fingerprint'],
                'target_install_id': self.target_id, 'target_profile': 'default',
                'catalog_digest': self._target_catalog['catalog_digest'],
                'policy_digest': self._target_catalog['execution_policy']['policy_digest']}

    def set_binding(self, binding):
        self._binding_override = dict(binding)

    @property
    def roster(self):
        return [{'member_id': 'local-a', 'profile': 'default', 'handle': 'local'},
                {'member_id': 'peer-b', 'profile': 'default', 'handle': 'peer',
                 'target': {'kind': 'peer', 'peer_id': 'target-b', 'installation_id': self.target_id, 'profile': 'default'}}]

    def __enter__(self):
        try:
            self.start()
            return self
        except BaseException:
            self.close()
            raise

    def start(self):
        # Even non-native transport tests obtain real fresh native IDs via the
        # accepted no-launch preflight, never constants masquerading as identities.
        if self.host_id is None or self.target_id is None:
            import preflight
            import uuid
            home = preflight.APPROVED_RUNTIME_ROOT / ('s2-control-' + uuid.uuid4().hex[:12])
            identity = preflight.run_preflight(home)['identity']
            self.host_id = identity['gateway_id']
            self.target_id = identity['second_gateway_id']
        self.owner_a = self.owner_a or pki.init_owner(self.root / 'host-tls', 'host')
        self.owner_b = self.owner_b or pki.init_owner(self.root / 'target-tls', 'target')
        ca = pki.init_owner(self.root / 'relay-ca', 'relay')
        server = pki.issue_server(self.root / 'relay-ca', self.root / 'relay-tls', 'IP:127.0.0.1')
        prov = relay_daemon.provision(self.root / 'routes.json', self.root / 'creds',
                                      link_id='lobby-control', room_id='lobby-control', source=self.target_id, target=self.host_id)
        self.daemon = relay_daemon.RelayDaemon(self.root / 'routes.json', host='127.0.0.1', port=0,
                          tls=relay_daemon._server_tls(server['cert_file'], server['key_file']))
        ready = threading.Event()
        def run():
            asyncio.set_event_loop(self.loop)
            self.loop.run_until_complete(self.daemon.run(ready.set, install_signals=False))
        self.original = relay.Relay.route
        original = self.original
        async def observe(instance, connection, frame):
            if isinstance(frame, dict) and frame.get('type') == 'DATA':
                self.observed.append(base64.b64decode(frame['payload']))
            return await original(instance, connection, frame)
        relay.Relay.route = observe
        self.thread = threading.Thread(target=run, name='s2-control-relay', daemon=True)
        self.thread.start()
        if not ready.wait(15):
            raise RuntimeError('control_relay_unavailable')
        outer = ssl.create_default_context(cafile=ca['ca_file'])
        for identity, peer, own, other in ((self.host_id, self.target_id, self.owner_a, self.owner_b),
                                            (self.target_id, self.host_id, self.owner_b, self.owner_a)):
            client = connector.RelayClient(relay_url=f'wss://127.0.0.1:{self.daemon.bound_port}/data', identity=identity,
                peer_identity=peer, link_id='lobby-control', room_id='lobby-control',
                credential=connector._read_secret(prov['credential_files'][identity]), ssl_context=outer)
            self.clients.append(client)
            client.start()
            inner = {'peer_ca_file': other['ca_file'], 'peer_fingerprint': other['fingerprint'],
                     'cert_file': own['cert_file'], 'key_file': own['key_file']}
            control = PinnedControl(client, inner)
            if identity == self.host_id:
                self.host = control
            else:
                self.joining = control
        return self

    def serve(self, handler):
        self.host.serve(handler)

    def call(self, payload):
        return self.joining.call(payload)

    def close(self):
        failures = []
        for client in self.clients:
            try:
                client.stop()
            except Exception:
                failures.append('connector_teardown_failed')
        if self.thread is not None:
            if self.daemon.stop_event is not None and self.thread.is_alive():
                self.loop.call_soon_threadsafe(self.daemon.stop_event.set)
            self.thread.join(15)
            if self.thread.is_alive():
                failures.append('relay_teardown_failed')
        if self.thread is None or not self.thread.is_alive():
            if not self.loop.is_closed():
                self.loop.run_until_complete(self.loop.shutdown_asyncgens())
                self.loop.close()
        if self.original is not None:
            relay.Relay.route = self.original
            self.original = None
        if failures:
            raise RuntimeError(','.join(failures))

    def __exit__(self, *args):
        self.close()
