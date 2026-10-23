"""S2.C TEST-ONLY fault observer/worker, not a product control operation.

Kill the real owner process after durable intent and after real native reply but
before journal acknowledgement. No fabricated native mint/admission/material.
"""
import asyncio
import concurrent.futures
import json
import os
from pathlib import Path
import signal
import ssl
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parents[1] / 'server')]


def private_json(path, value):
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w') as out:
        json.dump(value, out)
        out.flush()
        os.fsync(out.fileno())


async def worker(manifest):
    import aiohttp
    from connector import RelayClient
    from lobby_control import PinnedControl
    from lobby_owner import JoiningOwner, OwnerSurface
    from lobby_native_rpc import BoundedNativeRpc
    from lobby_material import TargetMint, MintJournal
    data = json.loads(Path(manifest).read_text())
    root = Path(data['root'])
    client = RelayClient(**data['relay'], ssl_context=ssl.create_default_context(cafile=data['outer_ca']))
    client.start()
    control = PinnedControl(client, data['inner'])
    join = JoiningOwner(Path(data['joining_root']), control, data['scope']['lobby_id'], data['scope']['binding'], data['scope']['host_id'])
    def boundary(phase):
        private_json(root / 'phase.json', {'phase': phase, 'pid': os.getpid()})
        threading.Event().wait()  # Test observer holds actual boundary until exact PID SIGKILL.
    class FaultJournal(MintJournal):
        def begin(self, scope):
            grant_id = super().begin(scope)
            if data['fault'] == 'after_intent':
                boundary('after_intent')
            return grant_id
        def complete(self, scope, envelope):
            if data['fault'] == 'after_native_reply':
                boundary('after_native_reply')
            return super().complete(scope, envelope)
    try:
        async with aiohttp.ClientSession(trust_env=False) as session:
            token = Path(data['native_token_file']).read_text().strip()
            async with session.ws_connect(f"ws://127.0.0.1:{data['native_port']}/api/ws?token={token}") as ws:
                rpc = BoundedNativeRpc(ws)
                caps = await rpc('groups.capabilities', {})
                if caps['authority_gateway_id'] != data['scope']['binding']['target_install_id']:
                    raise RuntimeError('wrong_native_installation')
                mint = TargetMint(join, FaultJournal(root / 'journal'), rpc)
                loop = asyncio.get_running_loop()
                def local_mint(payload):
                    return asyncio.run_coroutine_threadsafe(mint.mint(payload), loop).result(60)
                with OwnerSurface(root / 'owner', {'mint': local_mint}) as owner:
                    private_json(root / 'ready.json', {'base': owner.base})
                    await asyncio.Event().wait()
    finally:
        client.stop()


def run_faults(lab, rpc, loop):
    from connector import RelayClient
    from lobby_control import PinnedControl, HostLobbyControl
    from lobby_control_lab import ControlLab
    from lobby_owner import JoiningOwner, HostOwner, OwnerSurface
    from lobby_material import MintJournal, TargetMint
    from room_invite_store import RoomInviteStore
    from gate_s1_native import processes, PY
    from gate_s2b_native import post, native_inventory
    from gate_c_runtime import pf
    lab.report['mint_faults'] = []
    for fault in ('after_intent', 'after_native_reply'):
        root = lab.out / fault
        root.mkdir(mode=0o700)
        with ControlLab(root / 'network', host_id=lab.report['identity']['gateway_id'], target_id=lab.report['identity']['second_gateway_id']) as network:
            store = RoomInviteStore(root / 'mc.sqlite')
            lobby = store.create_lobby(name='S2.C killed local owner ' + fault, roster=network.roster)
            lid = lobby['lobby_id']
            invite = store.issue_invite(lid, 'peer-b', invitee_label='Known owner')
            join = JoiningOwner(root / 'joining', network.joining, lid, network.binding, network.host_id)
            join.confirm_scope(lobby['planned_room_id'], 'peer-b')
            network.serve(HostLobbyControl(store, lid, network.binding, joining_owner_key=join.public_key, host_id=network.host_id))
            payload = {'lobby_id': lid, 'invite_id': invite.invite_id}
            with OwnerSurface(root / 'host-owner', {'approve': HostOwner(store, lid, network.binding).approve}) as host, \
                    OwnerSurface(root / 'joining-owner', {'consent': join.consent}) as target:
                network.call({'op': 'redeem', 'invite_id': invite.invite_id, 'secret': invite.secret})
                assert post(host, dict(payload, op='approve'))[0] == 200
                assert post(target, dict(payload, op='consent')) == (200, {'status': 'ready'})
            scope = dict(json.loads(next(iter([r[0] for r in _consent_rows(join)]))), purpose='joining-owner-consent-v1')
            scope.pop('purpose')
            before = native_inventory(lab)['mint_count']
            old = network.joining.client
            old.stop()  # Single actual connector incarnation; worker takes the provisioned route.
            relay = {'relay_url': old.relay_url, 'identity': old.identity, 'peer_identity': old.peer_identity,
                     'link_id': old.link_id, 'room_id': old.room_id, 'credential': old._credential}
            inner = {'peer_ca_file': network.owner_a['ca_file'], 'peer_fingerprint': network.owner_a['fingerprint'],
                     'cert_file': network.owner_b['cert_file'], 'key_file': network.owner_b['key_file']}
            manifest = root / 'worker-private.json'
            private_json(manifest, {'root': str(root), 'joining_root': str(root / 'joining'), 'scope': scope, 'fault': fault,
                'relay': relay, 'inner': inner, 'outer_ca': str(root / 'network/relay-ca/ca.pem'),
                'native_port': lab.target_tui_port, 'native_token_file': str(lab.target_token_file)})
            argv = [str(PY), '-B', str(HERE / 's2c_mint_faults.py'), str(manifest)]
            log = (root / 'worker.log').open('w')
            lab.logs.append(log)
            proc = subprocess.Popen(argv, cwd=HERE.parents[1], env=pf.isolated_child_env(lab.target),
                                    stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            identity = next(r for r in processes() if r['pid'] == proc.pid)
            record = dict(identity, role='s2c-owner-' + fault, argv=argv, home=str(lab.target), signals=[])
            lab.owned.append((proc, record))
            lab.report['processes'].append(record)
            lab.report['commands'].append({'argv': argv, 'cwd': str(HERE.parents[1]), 'home': str(lab.target)})
            lab.flush()
            wait_file(root / 'ready.json', proc)
            owner = SimpleNamespace(base=json.loads((root / 'ready.json').read_text())['base'], token=(root / 'owner/owner.token').read_text())
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(post, owner, dict(payload, op='mint'))
                wait_file(root / 'phase.json', proc)
                phase = json.loads((root / 'phase.json').read_text())
                lab.check(fault + '_actual_boundary', phase == {'phase': fault, 'pid': proc.pid})
                current = next(r for r in processes() if r['pid'] == proc.pid)
                assert current == identity
                record['returncode_before_requested_termination'] = proc.poll()
                record['signals'].append({'signal': 'SIGKILL', 'at': time.time(), 'boundary': fault})
                proc.kill()
                proc.wait(10)
                record['returncode'] = proc.returncode
                record['terminated_on_request'] = True
                try:
                    pending.result(15)
                except Exception as exc:
                    record['http_interrupted_type'] = type(exc).__name__
            restored = RelayClient(**relay, ssl_context=ssl.create_default_context(cafile=str(root / 'network/relay-ca/ca.pem')))
            network.clients.append(restored)
            restored.start()
            network.joining = PinnedControl(restored, inner)
            resumed_join = JoiningOwner(root / 'joining', network.joining, lid, network.binding, network.host_id)
            resumed = TargetMint(resumed_join, MintJournal(root / 'journal'), rpc)
            def retry(payload):
                return asyncio.run_coroutine_threadsafe(resumed.mint(payload), loop).result(35)
            with OwnerSurface(root / 'resumed-owner', {'mint': retry}) as owner:
                response = post(owner, dict(payload, op='mint'))
            after = native_inventory(lab)['mint_count']
            expected_delta = 1 if fault == 'after_native_reply' else 0
            lab.check(fault + '_restart_typed_no_replay', response == (409, {'error': 'mint_ambiguous'})
                      and after - before == expected_delta and resumed.journal.state(scope) == 'intent',
                      mint_delta=after - before, local_error=response[1])
            reservations = []
            db = lab.target / 'shared-state.db'
            if db.exists():
                from gate_c_runtime import read_db
                reservations = read_db(db, 'SELECT room_id,member_id FROM hosted_room_peer_reservations WHERE room_id=?', (scope['room_id'],))
            lab.check(fault + '_native_reservation_readback', len(reservations) == expected_delta)
            lab.report['mint_faults'].append({'boundary': fault, 'scope': scope, 'mint_count_before': before,
                'mint_count_after': after, 'journal_state': resumed.journal.state(scope), 'reservations': reservations,
                'killed_pid': proc.pid, 'kill_exit': proc.returncode})
            if hasattr(lab, 'secret_values'):  # S2.E-only observer; no operation semantics changed.
                values = [invite.secret, host.token, target.token, owner.token, relay['credential'], *lab.keys]
                lab.secret_values.extend(values)
                blobs = [b''.join(network.observed)] + [p.read_bytes() for p in root.glob('mc.sqlite*')]
                lab.check(fault + '_fault_secrets_absent_mc_relay', bool(network.observed)
                          and all(v.encode() not in blob for v in values for blob in blobs))
        lab.check(fault + '_network_teardown', network.loop.is_closed() and not network.thread.is_alive()
                  and all(not c._thread.is_alive() and not c.application_threads for c in network.clients))


def _consent_rows(join):
    with join._db() as db:
        return db.execute('SELECT receipt FROM consents').fetchall()


def wait_file(path, proc):
    deadline = time.monotonic() + 45
    while not path.exists():
        if proc.poll() is not None or time.monotonic() > deadline:
            raise RuntimeError('fault_boundary_unavailable')
        time.sleep(.05)


if __name__ == '__main__':
    try:
        asyncio.run(worker(sys.argv[1]))
    except BaseException as exc:
        print(json.dumps({'error_type': type(exc).__name__}))
        raise SystemExit(1)
