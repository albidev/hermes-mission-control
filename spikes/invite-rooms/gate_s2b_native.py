"""S2.B criterion-3 only: two real local owners, network consent, native RPC.

One machine with actual WSS relay + pinned inner mTLS. No grant mint, material
resolver, native create/register/send or LAB_READY (criteria 4/5 are NOT run).
"""
import asyncio
import hashlib
import json
from pathlib import Path
import sys
import urllib.request
import urllib.error
import aiohttp

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parents[1] / 'server')]
from gate_s1_native import NativeLab, gc
from gate_p2_p4_local import ready_socket
from lobby_control import HostLobbyControl, ControlError
from lobby_control_lab import ControlLab
from lobby_owner import OwnerSurface, HostOwner, JoiningOwner
from lobby_native_rpc import BoundedNativeRpc, MAX_RESPONSE
from room_invite_store import RoomInviteStore
from room_lobby_service import NativeRpcRejected


def hashes():
    return {str(p.relative_to(HERE.parents[1])): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(HERE.rglob('*.py'))}


def post(owner, payload, token=None):
    request = urllib.request.Request(owner.base + '/owner', data=json.dumps(payload).encode(),
        headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + (owner.token if token is None else token)})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=15) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.load(exc)
        finally:
            exc.close()


def two_owners(lab):
    with ControlLab(lab.out / 'control', host_id=lab.report['identity']['gateway_id'],
                    target_id=lab.report['identity']['second_gateway_id']) as network:
        store = RoomInviteStore(lab.out / 'mc.sqlite')
        lobby = store.create_lobby(name='S2.B owner control only', roster=network.roster)
        lid = lobby['lobby_id']
        invite = store.issue_invite(lid, 'peer-b', invitee_label='Preprovisioned lab owner')
        join = JoiningOwner(lab.out / 'joining-private', network.joining, lid, network.binding, network.host_id)
        join.confirm_scope(lobby['planned_room_id'], 'peer-b')
        network.serve(HostLobbyControl(store, lid, network.binding, joining_owner_key=join.public_key, host_id=network.host_id))
        with OwnerSurface(lab.out / 'host-owner', {'approve': HostOwner(store, lid, network.binding).approve}) as host, \
                OwnerSurface(lab.out / 'joining-owner', {'consent': join.consent}) as target:
            payload = {'lobby_id': lid, 'invite_id': invite.invite_id}
            approve, consent = dict(payload, op='approve'), dict(payload, op='consent')
            lab.check('distinct_local_owner_credentials', host.token != target.token)
            lab.check('preview_pinned_network', network.call({'op': 'preview', 'invite_id': invite.invite_id})['name'] == lobby['name'])
            lab.check('redemption_pinned_network', network.call({'op': 'redeem', 'invite_id': invite.invite_id, 'secret': invite.secret}) == {'status': 'awaiting_host_approval'})
            lab.check('join_before_host_blocked', post(target, consent) == (409, {'error': 'owner_operation_blocked'}) and not join.has_consent(invite.invite_id))
            lab.check('wrong_host_credential_blocked', post(host, approve, target.token) == (403, {'error': 'owner_required'}))
            lab.check('host_local_approval', post(host, approve) == (200, {'status': 'consent_required'}))
            lab.check('host_alone_no_joining_consent', store.get_redemption(lid, invite.invite_id)['joining_consent'] == 'pending')
            lab.check('wrong_joining_credential_blocked', post(target, consent, host.token) == (403, {'error': 'owner_required'}))
            lab.check('joining_local_consent', post(target, consent) == (200, {'status': 'ready'}))
            before = store.get_redemption(lid, invite.invite_id)
            lab.check('consent_replay_immutable', post(target, consent) == (200, {'status': 'ready'}) and store.get_redemption(lid, invite.invite_id) == before)
            stored = b''.join(p.read_bytes() for p in lab.out.glob('mc.sqlite*'))
            relay_bytes = b''.join(network.observed)
            lab.check('no_secrets_db_or_relay', all(secret.encode() not in data for secret in [host.token, target.token, invite.secret]
                                                       for data in [stored, relay_bytes]))
            lab.check('no_native_enrollment_in_mc', store.get_lobby(lid)['room_id'] is None)
            lab.report['consent_scope'] = {'lobby_id': lid, 'invite_id': invite.invite_id, 'planned_room_id': lobby['planned_room_id'],
                                           'binding': network.binding, 'host_id': network.host_id}
    lab.check('control_teardown', network.loop.is_closed() and not network.thread.is_alive()
              and all(not c._thread.is_alive() and not c.application_threads for c in network.clients))


def native_inventory(lab):
    """Read only; absence of a lazily-created DB/table is recorded explicitly."""
    import sqlite3
    audits = [json.loads(line) for line in lab.audit.read_text().splitlines()]
    homes = []
    for home in (lab.home, lab.target):
        path = home / 'shared-state.db'
        row = {'home': str(home), 'database_exists': path.exists(), 'tables': {}}
        if path.exists():
            from contextlib import closing
            with closing(sqlite3.connect('file:' + str(path) + '?mode=ro', uri=True)) as db:
                tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                for table in ('hosted_rooms', 'hosted_room_remote_runs', 'hosted_room_peer_reservations'):
                    row['tables'][table] = {'exists': table in tables,
                        'rows': db.execute('SELECT count(*) FROM ' + table).fetchone()[0] if table in tables else None}
        homes.append(row)
    return {'mint_count': sum(r.get('event') == 'native_issue_room_grant_return' for r in audits),
            'observer_pids': sorted({r['pid'] for r in audits if r.get('event') == 'observer_installed'}), 'homes': homes}


async def native(lab):
    base = 'http://127.0.0.1:18791'
    async with aiohttp.ClientSession(trust_env=False, timeout=aiohttp.ClientTimeout(total=150)) as session:
        ws = await ready_socket(session, base, 18790, lab.keys[1], lab.keys[2], lab.children)
        async with ws:
            rpc = BoundedNativeRpc(ws)
            caps = await rpc('groups.capabilities', {})
            lab.check('real_native_capabilities', caps['authority_gateway_id'] == lab.report['identity']['gateway_id'] and caps['driver'] is True)
            rejected = False
            try:
                await rpc('groups.state', {'room_id': 's2b-missing-room'})
            except NativeRpcRejected as exc:
                rejected = str(exc) == 'native_rejected'
            lab.check('real_native_explicit_rejection', rejected)
            lab.check('real_native_socket_reusable_after_rejection', (await rpc('groups.capabilities', {}))['authority_gateway_id'] == caps['authority_gateway_id'])
            before = native_inventory(lab)
            await asyncio.to_thread(two_owners, lab)
            after = native_inventory(lab)
            lab.report['native_before'], lab.report['native_after'] = before, after
            lab.check('both_consents_no_native_mint', before['mint_count'] == after['mint_count'] == 0
                      and len(after['observer_pids']) >= 2)
            lab.check('no_native_rooms_or_runs', all(table['rows'] in (None, 0)
                                                    for home in after['homes'] for table in home['tables'].values()))


def main():
    lab = NativeLab()
    lab.out = gc.ROOT / 'evidence/s2' / ('s2b-native-' + lab.stamp)
    lab.out.mkdir(parents=True, mode=0o700)
    lab.report['scope'] = 'S2.B criterion 3 ONLY; actual one-machine boundaries, not F1, no criterion 4/5'
    lab.report['s2b_source_start'] = hashes()
    try:
        lab.setup()
        asyncio.run(native(lab))
    except BaseException as exc:
        lab.report['error_type'] = type(exc).__name__
        if isinstance(exc, AssertionError) and str(exc) in lab.report['checks']:
            lab.report['failed_check'] = str(exc)
    finally:
        lab.close()
    lab.report['s2b_source_end'] = hashes()
    lab.report['packet_native_checks_passed'] = (not lab.report.get('error_type') and lab.report['teardown_complete']
        and all(c['ok'] for c in lab.report['checks'].values()) and lab.report['s2b_source_start'] == lab.report['s2b_source_end'])
    lab.flush()
    print(json.dumps({'report': str(lab.out / 'report.json'), 'native_checks_passed': lab.report['packet_native_checks_passed']}))
    return 0 if lab.report['packet_native_checks_passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
