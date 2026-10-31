"""S2.C criterion-4 only; real target mint and private pinned delivery, no activation."""
import asyncio
import concurrent.futures
import json
from pathlib import Path
import secrets
import socket
import sys
import time
import aiohttp

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parents[1] / 'server')]
from gate_s1_native import NativeLab, gc
from gate_s2b_native import post, hashes, native_inventory
from gate_p2_p4_local import ready_socket
from lobby_control import HostLobbyControl, ControlError
from lobby_control_lab import ControlLab
from lobby_owner import JoiningOwner, HostOwner, OwnerSurface
from lobby_native_rpc import BoundedNativeRpc
from room_invite_store import RoomInviteStore


def flow(lab, rpc, loop):
    from lobby_material import MintJournal, TargetMint, PrivateRegistry
    with ControlLab(lab.out / 'control', host_id=lab.report['identity']['gateway_id'],
                target_id=lab.report['identity']['second_gateway_id'],
                target_catalog=lab.target_catalog, require_target_catalog=True) as network:
        store = RoomInviteStore(lab.out / 'mc.sqlite')
        lobby = store.create_lobby(name='S2.C no activation', roster=network.roster)
        lid = lobby['lobby_id']
        invite = store.issue_invite(lid, 'peer-b', invitee_label='Manually preprovisioned target owner')
        join = JoiningOwner(lab.out / 'joining-private', network.joining, lid, network.binding, network.host_id)
        join.confirm_scope(lobby['planned_room_id'], 'peer-b')
        scope = {'lobby_id': lid, 'invite_id': invite.invite_id, 'room_id': lobby['planned_room_id'],
                 'member_id': 'peer-b', 'binding': network.binding, 'host_id': network.host_id}
        # Owner-provisioned address only, not bound/activated in this packet.
        registry = PrivateRegistry(lab.out / 'home-private', scope, 'http://127.0.0.1:1/links/s2c-unactivated')
        network.serve(HostLobbyControl(store, lid, network.binding, joining_owner_key=join.public_key,
                                      host_id=network.host_id, material_registry=registry))
        journal = MintJournal(lab.out / 'target-private')
        mint = TargetMint(join, journal, rpc)
        def local_mint(payload):
            return asyncio.run_coroutine_threadsafe(mint.mint(payload), loop).result(35)
        with OwnerSurface(lab.out / 'host-owner', {'approve': HostOwner(store, lid, network.binding).approve}) as host, \
                OwnerSurface(lab.out / 'target-owner', {'consent': join.consent, 'mint': local_mint}) as target:
            payload = {'lobby_id': lid, 'invite_id': invite.invite_id}
            request = dict(payload, op='mint')
            lab.check('unredeemed_mint_blocked', post(target, request)[0] == 409)
            network.call({'op': 'redeem', 'invite_id': invite.invite_id, 'secret': invite.secret})
            lab.check('redeemed_no_consents_mint_blocked', post(target, request)[0] == 409)
            lab.check('local_host_approve', post(host, dict(payload, op='approve')) == (200, {'status': 'consent_required'}))
            lab.check('host_only_mint_blocked', post(target, request)[0] == 409)
            lab.check('wrong_local_owner_mint_blocked', post(target, request, host.token)[0] == 403)
            lab.check('preconsent_no_native_mint', native_inventory(lab)['mint_count'] == 0)
            lab.check('local_joining_consent', post(target, dict(payload, op='consent')) == (200, {'status': 'ready'}))
            before = store.get_redemption(lid, invite.invite_id)
            lab.check('both_consents_do_not_automint', native_inventory(lab)['mint_count'] == 0)
            # Concurrent local authenticated requests race on the real durable intent.
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _: post(target, request), range(2)))
            lab.check('local_mint_delivers_material', any(r == (200, {'status': 'material_ready'}) for r in results), statuses=[r[0] for r in results])
            lab.check('replayed_local_mint_idempotent', post(target, request) == (200, {'status': 'material_ready'}))
            peer = dict(network.binding, member_id='peer-b', lobby_id=lid, room_id=lobby['planned_room_id'])
            material = asyncio.run(registry.resolve(peer))
            lab.check('private_resolver_native_material', material.binding == network.binding
                      and material.catalog['installation_id'] == network.target_id and bool(material.grant))
            for field, value in [('member_id', 'other'), ('target_install_id', network.host_id),
                                 ('fingerprint', '0' * 64), ('lobby_id', 'lobby-other')]:
                blocked = False
                try:
                    asyncio.run(registry.resolve(dict(peer, **{field: value})))
                except ControlError:
                    blocked = True
                lab.check('resolver_wrong_' + field, blocked)
            # Real pinned channel rejects tamper/known-owner wrong scope; no extra native mint.
            envelope = journal.load(scope)
            for name, bad in [('tampered', dict(envelope, grant=material.grant[:-1] + '!')),
                              ('scope', dict(envelope, scope=dict(scope, member_id='other'))),
                              ('oversized', dict(envelope, grant='X' * (25 * 1024)))]:
                blocked = False
                try:
                    network.call({'op': 'material', 'invite_id': invite.invite_id, 'envelope': bad})
                except ControlError:
                    blocked = True
                lab.check('remote_material_' + name + '_blocked', blocked)
            # Known signing owner cannot substitute native scope/catalog; signed tamper still fails.
            import base64
            import copy
            from lobby_consent import canonical
            for name, changed in [('signed_scope', dict(envelope, scope=dict(scope, room_id='invite-room-other'))),
                                  ('signed_catalog', copy.deepcopy(envelope))]:
                if name == 'signed_catalog':
                    changed['catalog']['installation_id'] = network.host_id
                unsigned = {k: v for k, v in changed.items() if k != 'signature'}
                changed['signature'] = base64.b64encode(join._key.sign(canonical(dict(unsigned, purpose='joining-owner-material-v1')))).decode()
                blocked = False
                try:
                    network.call({'op': 'material', 'invite_id': invite.invite_id, 'envelope': changed})
                except ControlError:
                    blocked = True
                lab.check('remote_' + name + '_blocked', blocked)
            # Bypass the honest-client encode budget: actual oversized application frame over mTLS.
            from tls_stream import wrap_client, SequencedTLS, client_context
            sock = network.joining.client.open_stream()
            sock.settimeout(10)
            try:
                with wrap_client(sock, network.joining.client_tls, expected_fingerprint=network.joining.pin) as tls:
                    frames = SequencedTLS(tls, deadline=time.monotonic() + 10)
                    frames.send(json.dumps({'op': 'material', 'invite_id': invite.invite_id, 'envelope': envelope, 'pad': 'X' * (25 * 1024)}).encode())
                    lab.check('real_socket_oversized_material_blocked', json.loads(frames.receive()[1]) == {'error': 'control_unavailable'})
            finally:
                sock.close()
            import pki
            foreign = pki.init_owner(lab.out / 'foreign-client', 'foreign')
            original_context = network.joining.client_tls
            network.joining.client_tls = client_context(ca_file=network.owner_a['ca_file'], cert_file=foreign['cert_file'], key_file=foreign['key_file'])
            blocked = False
            try:
                network.call({'op': 'material', 'invite_id': invite.invite_id, 'envelope': envelope})
            except ControlError:
                blocked = True
            finally:
                network.joining.client_tls = original_context
            lab.check('actual_wrong_presented_client_cert_blocked', blocked)
            lab.check('no_registry_change_after_adversaries', asyncio.run(registry.resolve(peer)) == material)
            # Actual connector reconnect, not just a reconstructed Python workflow.
            from connector import RelayClient
            from lobby_control import PinnedControl
            old = network.joining.client
            old.stop()
            reconnected = RelayClient(relay_url=old.relay_url, identity=old.identity, peer_identity=old.peer_identity,
                link_id=old.link_id, room_id=old.room_id, credential=old._credential, ssl_context=old.ssl_context)
            network.clients.append(reconnected)
            reconnected.start()
            network.joining = PinnedControl(reconnected, {'peer_ca_file': network.owner_a['ca_file'],
                'peer_fingerprint': network.owner_a['fingerprint'], 'cert_file': network.owner_b['cert_file'],
                'key_file': network.owner_b['key_file']})
            lab.check('material_connector_reconnected', old.incarnation != reconnected.incarnation)
            # Resume fresh workflow and registry; never renew consent/grant.
            resumed = TargetMint(JoiningOwner(lab.out / 'joining-private', network.joining, lid, network.binding, network.host_id),
                                 MintJournal(lab.out / 'target-private'), rpc)
            reply = asyncio.run_coroutine_threadsafe(resumed.mint(payload), loop).result(35)
            again = asyncio.run(PrivateRegistry(lab.out / 'home-private', scope, registry.adapter_url).resolve(peer))
            lab.check('restart_material_same_no_authority_renewal', reply == {'status': 'material_ready'}
                      and again == material and store.get_redemption(lid, invite.invite_id) == before)
            secrets_to_check = [host.token, target.token, invite.secret, material.grant, *lab.keys]
            secrets_to_check += [p.read_text().strip() for p in (lab.out / 'control/creds').rglob('*') if p.is_file()]
            files = list(lab.out.glob('mc.sqlite*')) + list((lab.out / 'target-private').glob('*.sqlite*')) + list((lab.out / 'home-private').glob('*.sqlite*'))
            data = [p.read_bytes() for p in files] + [b''.join(network.observed)]
            lab.check('secrets_absent_db_relay', bool(network.observed) and all(s.encode() not in b for s in secrets_to_check for b in data))
            lab.report['material_scope'] = scope
            lab.report['relay_frames'] = len(network.observed)
            lab.report['private_journal_state'] = journal.state(scope)
            lab.check('no_mc_activation', store.get_lobby(lid)['room_id'] is None and store.get_lobby(lid)['state'] == 'open')
    lab.check('control_teardown', network.loop.is_closed() and not network.thread.is_alive()
              and all(not c._thread.is_alive() and not c.application_threads for c in network.clients))


async def native(lab):
    async with aiohttp.ClientSession(trust_env=False, timeout=aiohttp.ClientTimeout(total=150)) as session:
        # Readiness of existing target gateway, then actual owner-private target TUI socket.
        host_ws = await ready_socket(session, 'http://127.0.0.1:18791', 18790, lab.keys[1], lab.keys[2], lab.children)
        await host_ws.close()
        deadline = time.monotonic() + 150
        while True:
            try:
                ws = await session.ws_connect(f'ws://127.0.0.1:{lab.target_tui_port}/api/ws?token={lab.target_ws_key}')
                break
            except (OSError, aiohttp.ClientError):
                if time.monotonic() > deadline or lab.target_tui.poll() is not None:
                    raise RuntimeError('target_native_readiness') from None
                await asyncio.sleep(.5)
        async with ws:
            rpc = BoundedNativeRpc(ws)
            caps = await rpc('groups.capabilities', {})
            lab.target_catalog = caps['room_link']['catalog']
            lab.check('target_rpc_actual_native_identity', caps['authority_gateway_id'] == lab.report['identity']['second_gateway_id'])
            await asyncio.to_thread(flow, lab, rpc, asyncio.get_running_loop())
            audit = native_inventory(lab)
            lab.report['native_after'] = audit
            lab.check('exactly_one_native_mint', audit['mint_count'] == 1)
            reservations = gc.read_db(lab.target / 'shared-state.db', 'SELECT room_id,member_id,target_profile FROM hosted_room_peer_reservations')
            lab.report['native_reservations'] = reservations
            scope = lab.report['material_scope']
            lab.check('exact_native_reservation_readback', reservations == [{'room_id': scope['room_id'], 'member_id': 'peer-b', 'target_profile': 'default'}])
            lab.check('no_native_rooms_or_runs', all(t['rows'] in (None, 0) for h in audit['homes'] for name,t in h['tables'].items() if name != 'hosted_room_peer_reservations'))
            runs = []
            for home in (lab.home, lab.target):
                path = home / 'runs_idempotency.db'
                exists = path.exists()
                rows = gc.read_db(path, 'SELECT count(*) AS n FROM run_idempotency') if exists else None
                runs.append({'database_exists': exists, 'rows': rows})
            lab.report['native_run_counts'] = runs
            lab.check('zero_real_native_run_rows', all(r['rows'] is None or r['rows'] == [{'n': 0}] for r in runs))
            from s2c_mint_faults import run_faults
            await asyncio.to_thread(run_faults, lab, rpc, asyncio.get_running_loop())
            final = native_inventory(lab)
            lab.report['native_final'] = final
            receipts = [json.loads(line) for line in lab.audit.read_text().splitlines()]
            mints = [r for r in receipts if r.get('event') == 'native_issue_room_grant_return']
            counts = {r['room_id']: sum(m['room_id'] == r['room_id'] for m in mints) for r in mints}
            lab.report['native_mint_counts_by_room'] = counts
            lab.check('all_scenarios_no_duplicate_native_mint', final['mint_count'] == 2 and len(counts) == 2 and set(counts.values()) == {1})
            lab.check('all_scenarios_zero_native_activation', all(t['rows'] in (None, 0) for h in final['homes'] for name,t in h['tables'].items() if name != 'hosted_room_peer_reservations'))


def main():
    lab = NativeLab()
    lab.out = gc.ROOT / 'evidence/s2' / ('s2c-native-' + lab.stamp)
    lab.out.mkdir(parents=True, mode=0o700)
    lab.report['scope'] = 'S2.C criterion 4 ONLY; one-machine real network, no activation/F1'
    lab.report['s2c_source_start'] = hashes()
    try:
        lab.setup()
        # Additional target-local native serve: no production listener or synthetic RPC.
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            lab.target_tui_port = sock.getsockname()[1]
        lab.target_ws_key = secrets.token_hex(32)
        token = lab.target / '.hermes' / 'desktop-ssh' / secrets.token_hex(16) / (secrets.token_hex(8) + '.token')
        token.parent.mkdir(parents=True, mode=0o700)
        token.write_text(lab.target_ws_key)
        token.chmod(0o600)
        lab.target_token_file = lab.out / 'target-native-private.token'
        lab.target_token_file.write_text(lab.target_ws_key)
        lab.target_token_file.chmod(0o600)
        lab.args['target-tui'] = ['serve', '--isolated', '--host', '127.0.0.1', '--port', str(lab.target_tui_port), '--ssh-session-token-file', str(token)]
        lab.target_tui = lab.spawn('target-tui')
        asyncio.run(native(lab))
    except BaseException as exc:
        lab.report['error_type'] = type(exc).__name__
        if isinstance(exc, AssertionError) and str(exc) in lab.report['checks']:
            lab.report['failed_check'] = str(exc)
    finally:
        lab.close()
    lab.report['target_native_rpc_port'] = getattr(lab, 'target_tui_port', None)
    if hasattr(lab, 'target_tui_port'):
        free = gc.pf.check_port_available(lab.target_tui_port)['available']
        lab.report['target_native_rpc_port_free_after'] = free
        lab.report['teardown_complete'] = lab.report['teardown_complete'] and free
    lab.report['s2c_source_end'] = hashes()
    lab.report['packet_native_checks_passed'] = not lab.report.get('error_type') and lab.report['teardown_complete'] and all(c['ok'] for c in lab.report['checks'].values()) and lab.report['s2c_source_start'] == lab.report['s2c_source_end']
    lab.flush()
    print(json.dumps({'report': str(lab.out / 'report.json'), 'native_checks_passed': lab.report['packet_native_checks_passed']}))
    return 0 if lab.report['packet_native_checks_passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
