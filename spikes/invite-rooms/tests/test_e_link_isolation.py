"""Behavioral two-link isolation, real loopback WSS/mTLS, NOT native proof.

No native process, identity, admission or receipt is created by these fixtures.
Each link has its own active Room scope and backend. Deadlines, observed EOF,
backend delivery and successful independent exchanges establish isolation.
"""
import json
from pathlib import Path
import socket
import sys
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import connector
import gate_f1_local as net
from room_firewall import RoomFirewall, Request, ForwardedRequest
from run_bindings import HostRunBindings
from test_net_loopback import StubNative, SCOPE


class BehavioralTwoLinkIsolation(unittest.TestCase):
    def test_stalled_and_lost_target_on_a_does_not_block_or_cross_deliver_b(self):
        transports, backends, firewalls, hops = [], [], [], []
        tls = raw = None
        old_timeout = connector.REQUEST_TIMEOUT
        connector.REQUEST_TIMEOUT = 1.5
        try:
            for label in ('a', 'b'):
                backend = StubNative()
                backends.append(backend)
                transport = net.NetworkTransport()
                transports.append(transport)
                transport.start()
                scope = dict(SCOPE, room_id='behavioral-room-' + label,
                             home_install_id='behavioral-host-' + label,
                             authority_gateway_id='behavioral-host-' + label,
                             target_install_id='behavioral-target-' + label)
                transport.native_receipt_db = transport.root / 'unused-receipts.db'
                firewall = RoomFirewall(expected_scope=scope, backend=lambda _: None,
                    verify_native=HostRunBindings(transport.root / 'host-bindings.db'))
                transport.bind('behavioral-link-' + label, firewall,
                               target_url=backend.base, scope=scope)
                firewalls.append(firewall)
                hops.append(connector.HostConnector(transport.host_client,
                                                   inner=transport.host_cfg['inner_tls']))

            def exchange(index):
                # Fixture auth markers are not native grants and are never minted.
                marker = 'HermesRoom behavioral-link-' + ('a' if index == 0 else 'b')
                request = Request('GET', '/v1/room-members/capabilities',
                                  (('authorization', marker),), b'')
                start = time.monotonic()
                response = firewalls[index].forward(request)
                elapsed = time.monotonic() - start
                self.assertEqual(response.status, 200)
                self.assertEqual(json.loads(response.body)['installation_id'], 'install:target')
                self.assertLess(elapsed, 1.5, 'independent exchange exceeded its bound')

            exchange(0)
            exchange(1)
            raw = transports[0].host_client.open_stream()
            raw.settimeout(3)
            tls = connector.wrap_client(raw, hops[0].context,
                                         expected_fingerprint=hops[0].peer_fingerprint)
            # Silent stall: inner mTLS is established, but send no request header.
            started = time.monotonic()
            exchange(1)
            self.assertEqual(tls.recv(1), b'', 'silent A must fail closed with EOF')
            elapsed = time.monotonic() - started
            self.assertGreaterEqual(elapsed, 1.2)
            self.assertLess(elapsed, 2.5, 'silent A exceeded request timeout + allowance')
            tls.close()
            tls = raw = None
            exchange(0)  # A recovered after its failed stream, before connector loss.
            transports[0].target_client.stop()
            self.assertFalse(transports[0].target_client._thread.is_alive())
            exchange(1)  # B completes while A's target connector is absent.
            started = time.monotonic()
            try:
                response = hops[0].forward(ForwardedRequest('GET', '/v1/room-members/capabilities',
                    (('authorization', 'HermesRoom behavioral-link-a'),), b''))
                self.assertGreaterEqual(response.status, 500)
            except (OSError, TimeoutError, ConnectionError, RuntimeError):
                pass  # Fail closed, never fabricate a successful exchange.
            self.assertLess(time.monotonic() - started, 3.0)
            exchange(1)
            for label, backend, expected_count in zip(('a', 'b'), backends, (2, 4)):
                self.assertEqual(len(backend.seen), expected_count)
                self.assertTrue(all(row == ('GET', '/v1/room-members/capabilities',
                    'HermesRoom behavioral-link-' + label) for row in backend.seen),
                    'cross-link delivery or hidden replay observed')
        finally:
            if tls is not None:
                tls.close()
            elif raw is not None:
                raw.close()
            # Restore route-spy stacking in reverse ownership order.
            for transport in reversed(transports):
                transport.close()
            for backend in backends:
                backend.close()
            connector.REQUEST_TIMEOUT = old_timeout
        self.assertTrue(all(t.loop.is_closed() and all(not c.application_threads
            and not c._thread.is_alive() for c in t.clients) for t in transports))


if __name__ == '__main__':
    unittest.main()
