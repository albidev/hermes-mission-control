"""S2.D local native tracer; actual control/owner/native boundaries, never F1."""
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
from gate_s1_native import NativeLab, gc, native_signals
from gate_s2b_native import post, hashes, native_inventory
from gate_p2_p4_local import ready_socket
from lobby_control import HostLobbyControl, ControlError, parse_invitation
from lobby_control_lab import ControlLab
from lobby_owner import JoiningOwner, HostOwner, OwnerSurface
from lobby_native_rpc import BoundedNativeRpc
from lobby_material import MintJournal, TargetMint, PrivateRegistry
from room_invite_store import RoomInviteStore


def flow(lab, host_rpc, target_rpc, loop):
    from lobby_activation import OnceActivation, OperationJournal
    def wait(coro):
        return asyncio.run_coroutine_threadsafe(coro, loop).result(40)
    with ControlLab(lab.out / 'control', host_id=lab.report['identity']['gateway_id'],
                    target_id=lab.report['identity']['second_gateway_id'],
                    owner_a=lab.transport.owner_a, owner_b=lab.transport.owner_b) as network:
        store = RoomInviteStore(lab.out / 'mc.sqlite')
        lobby = store.create_lobby(name='S2.D real native', roster=network.roster)
        lid = lobby['lobby_id']
        invite = store.issue_invite(lid, 'peer-b', invitee_label='Manually confirmed known owner')
        join = JoiningOwner(lab.out / 'joining', network.joining, lid, network.binding, network.host_id)
        join.confirm_scope(lobby['planned_room_id'], 'peer-b')
        scope = dict(lobby_id=lid, invite_id=invite.invite_id, room_id=lobby['planned_room_id'],
                     member_id='peer-b', binding=network.binding, host_id=network.host_id)
        # Fixed owner-provisioned local adapter listener; binding occurs only after mint.
        from adapter import LoopbackAdapter
        adapter = LoopbackAdapter()
        adapter.start(host='127.0.0.1')
        try:
            url = adapter.url('s2d')
            registry = PrivateRegistry(lab.out / 'home-private', scope, url)
            network.serve(HostLobbyControl(store, lid, network.binding, joining_owner_key=join.public_key,
                          host_id=network.host_id, material_registry=registry))
            mint = TargetMint(join, MintJournal(lab.out / 'target-private'), target_rpc)
            activation = OnceActivation(store, scope, registry, OperationJournal(lab.out / 'activation'), host_rpc)
            payload = {'lobby_id': lid, 'invite_id': invite.invite_id}
            with OwnerSurface(lab.out / 'host-owner', {'approve': HostOwner(store, lid, network.binding).approve,
                    'activate': lambda p: wait(activation.activate(p)),
                    'send': lambda p: wait(activation.first_send(p))}) as host, \
                 OwnerSurface(lab.out / 'target-owner', {'consent': join.consent, 'mint': lambda p: wait(mint.mint(p))}) as target:
                parsed = parse_invitation('https://rooms.lab/invite/' + lid + '/' + invite.invite_id + '#' + invite.secret)
                lab.check('fragment_parser_exact', parsed == (lid, invite.invite_id, invite.secret))
                preview = network.call({'op': 'preview', 'invite_id': invite.invite_id})
                lab.check('pinned_remote_preview', preview['name'] == lobby['name'] and invite.secret not in json.dumps(preview))
                lab.check('activation_before_consents_blocked', post(host, dict(payload, op='activate'))[0] == 409)
                def redeem(_):
                    try:
                        return network.call({'op': 'redeem', 'invite_id': invite.invite_id, 'secret': invite.secret})['status']
                    except ControlError as exc:
                        return str(exc)
                with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
                    replies = list(pool.map(redeem, range(20)))
                lab.check('remote_redemption_one_winner', replies.count('awaiting_host_approval') == 1 and replies.count('control_unavailable') == 19)
                lab.check('local_host_approval', post(host, dict(payload, op='approve')) == (200, {'status': 'consent_required'}))
                lab.check('host_only_activation_blocked', post(host, dict(payload, op='activate'))[0] == 409)
                lab.check('local_joining_consent', post(target, dict(payload, op='consent')) == (200, {'status': 'ready'}))
                lab.check('no_native_mint_before_explicit_operation', native_inventory(lab)['mint_count'] == 0)
                lab.check('target_native_mint_delivery', post(target, dict(payload, op='mint')) == (200, {'status': 'material_ready'}))
                peer = dict(network.binding, lobby_id=lid, room_id=scope['room_id'], member_id='peer-b')
                material = wait(registry.resolve(peer))
                data_scope = dict(room_id=scope['room_id'], home_install_id=network.host_id,
                    authority_gateway_id=network.host_id, authority_epoch=1, member_id='peer-b',
                    target_install_id=network.target_id, target_profile='default')
                from room_firewall import RoomFirewall
                from run_bindings import HostRunBindings
                lab.transport.native_receipt_db = lab.target / 'runs_idempotency.db'
                bindings = HostRunBindings(lab.out / 'host-bindings.sqlite')
                network_url = lab.transport.bind('s2d-native', RoomFirewall(expected_scope=data_scope,
                    backend=gc.native_backend('http://127.0.0.1:18791', material.grant),
                    verify_native=bindings, on_admitted=bindings.record_ack),
                    target_url='http://127.0.0.1:18791', scope=data_scope)
                # The dedicated fixed adapter proxies only through this provisioned data-plane firewall.
                adapter.bind('s2d', lab.transport.host_firewall, target_url='relay:s2d-native', scope=data_scope)
                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                    activated = list(pool.map(lambda _: post(host, dict(payload, op='activate')), range(2)))
                lab.check('once_only_activation', any(r == (200, {'status': 'active', 'room_id': scope['room_id']}) for r in activated), responses=activated)
                resumed = OnceActivation(RoomInviteStore(store.path), scope, registry, OperationJournal(lab.out / 'activation'), host_rpc)
                lab.check('activation_resume_readback', wait(resumed.activate(payload)) == {'status': 'active', 'room_id': scope['room_id']})
                state = wait(host_rpc('groups.state', {'room_id': scope['room_id']}))
                rooms = gc.read_db(lab.home / 'shared-state.db', 'SELECT room_id FROM hosted_rooms')
                lab.check('exact_one_native_room', rooms == [{'room_id': scope['room_id']}])
                spec_rows = gc.read_db(store.path, 'SELECT native_spec_json FROM lobbies WHERE lobby_id=?', (lid,))
                lab.check('exact_fixed_native_roster', state['room']['members'] == json.loads(spec_rows[0]['native_spec_json'])['members'])
                log = wait(host_rpc('groups.log', {'room_id': scope['room_id'], 'since_seq': 0, 'limit': 500}))
                lab.check('activation_never_sends', log['events'] == [])
                send = dict(payload, op='send', text='@peer Reply only LAB_READY, without mentioning other members. For any subsequent round reply PASS.')
                lab.check('wrong_local_owner_send_blocked', post(host, send, target.token)[0] == 403)
                sent = post(host, send)
                lab.check('explicit_first_owner_send', sent[0] == 200 and sent[1].get('status') == 'sent', response=sent)
                replay = wait(resumed.first_send({k: v for k, v in send.items() if k != 'op'}))
                lab.check('send_resume_same_event', replay == sent[1])
                deadline = time.monotonic() + 180
                while time.monotonic() < deadline:
                    page = wait(host_rpc('groups.log', {'room_id': scope['room_id'], 'since_seq': 0, 'limit': 500}))
                    replies = [e for e in page['events'] if e['kind'] == 'message.member' and e['payload'].get('member_id') == 'peer-b']
                    if replies:
                        break
                    if any(e['kind'] in {'turn.failed', 'turn.deferred'} for e in page['events']):
                        raise RuntimeError('native_turn_failed')
                    time.sleep(.5)
                lab.check('real_remote_LAB_READY', len(replies) == 1 and replies[0]['payload']['text'] == 'LAB_READY')
                from gate_g_native import _receipts
                tasks = gc.read_db(lab.home / 'shared-state.db', 'SELECT task_id,execution_generation,run_id FROM hosted_room_remote_runs WHERE room_id=?', (scope['room_id'],))
                lab.check('one_native_remote_run', len(tasks) == 1, native_runs=tasks)
                key = 'room:' + tasks[0]['task_id'] + ':' + str(tasks[0]['execution_generation'])
                receipts = _receipts(lab.target, key)
                lab.check('one_real_target_receipt', len(receipts) == 1 and receipts[0][0] == tasks[0]['run_id'], receipts=receipts)
                canonical = [{'seq': e['seq'], 'event_id': e['event_id']} for e in page['events']]
                db_events = gc.read_db(lab.home / 'shared-state.db', 'SELECT seq,event_id FROM hosted_room_events WHERE room_id=? ORDER BY seq', (scope['room_id'],))
                lab.check('same_canonical_db_ids_order', canonical == db_events and not page['has_more'] and len(canonical) == len({e['event_id'] for e in canonical}))
                lab.report['canonical_log'] = page
                lab.report['successful_scope'] = scope
                lab.report['activation_native_state'] = state
                lab.report['network_adapter_url'] = network_url
                lab.check('single_target_mint', native_inventory(lab)['mint_count'] == 1)
                raw = b''.join(network.observed)
                blobs = [p.read_bytes() for p in lab.out.glob('mc.sqlite*')]
                values = [invite.secret, material.grant, host.token, target.token, *lab.keys]
                lab.check('secrets_absent_mc_relay', bool(raw) and all(v.encode() not in b for v in values for b in [raw, *blobs]))
        finally:
            adapter.close()
    lab.check('control_teardown', network.loop.is_closed() and not network.thread.is_alive()
              and all(not c._thread.is_alive() and not c.application_threads for c in network.clients))


async def native(lab):
    async with aiohttp.ClientSession(trust_env=False, timeout=aiohttp.ClientTimeout(total=200)) as session:
        ws = await ready_socket(session, 'http://127.0.0.1:18791', 18790, lab.keys[1], lab.keys[2], lab.children)
        deadline = time.monotonic() + 150
        while True:
            try:
                target = await session.ws_connect(f'ws://127.0.0.1:{lab.target_tui_port}/api/ws?token={lab.target_ws_key}')
                break
            except (OSError, aiohttp.ClientError):
                if time.monotonic() > deadline or lab.target_tui.poll() is not None:
                    await ws.close()
                    raise RuntimeError('target_native_readiness') from None
                await asyncio.sleep(.5)
        async with ws, target:
            host_rpc, target_rpc = BoundedNativeRpc(ws), BoundedNativeRpc(target)
            caps = await target_rpc('groups.capabilities', {})
            lab.check('real_target_identity', caps['authority_gateway_id'] == lab.report['identity']['second_gateway_id'])
            await asyncio.to_thread(flow, lab, host_rpc, target_rpc, asyncio.get_running_loop())


def main():
    lab = NativeLab()
    lab.out = gc.ROOT / 'evidence/s2' / ('s2d-native-' + lab.stamp)
    lab.out.mkdir(parents=True, mode=0o700)
    lab.report['scope'] = 'S2.D one-machine real network only; F1 remains closed'
    lab.report['s2d_source_start'] = hashes()
    try:
        with native_signals(lab.report):
            lab.setup()
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                lab.target_tui_port = sock.getsockname()[1]
            lab.target_ws_key = secrets.token_hex(32)
            token = lab.target / '.hermes/desktop-ssh' / secrets.token_hex(16) / (secrets.token_hex(8) + '.token')
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
    lab.report['s2d_source_end'] = hashes()
    lab.report['packet_native_checks_passed'] = not lab.report.get('error_type') and lab.report['teardown_complete'] and all(v['ok'] for v in lab.report['checks'].values())
    lab.flush()
    print(json.dumps({'report': str(lab.out / 'report.json'), 'passed': lab.report['packet_native_checks_passed']}))
    return 0 if lab.report['packet_native_checks_passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
