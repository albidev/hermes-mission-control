"""TEST ONLY real owner-process faults. Native implementation files remain read-only."""
import asyncio
import concurrent.futures
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parents[1] / 'server')]
from s2c_mint_faults import private_json, wait_file


OBSERVER = '''
from tui_gateway import server as observed_server
def observe_method(name, original):
    def observed(rid, params):
        safe = {k: params[k] for k in ('room_id', 'member_id') if isinstance(params.get(k), str)}
        record(dict(event='s2e_native_method', method=name, phase='enter', **safe))
        result = original(rid, params)
        record(dict(event='s2e_native_method', method=name, phase='return', native_rejected='error' in result, **safe))
        return result
    return observed
for observed_name in ('groups.create', 'groups.peer.register', 'groups.send', 'groups.peer.invite'):
    observed_server._methods[observed_name] = observe_method(observed_name, observed_server._methods[observed_name])
record(dict(event='s2e_method_observer_installed', methods=['groups.create','groups.peer.register','groups.send','groups.peer.invite']))
'''


def install_native_observer(gs):
    marker = 'sys.argv = ["hermes",'
    assert gs.NATIVE_ENTRY.count(marker) == 1
    gs.NATIVE_ENTRY = gs.NATIVE_ENTRY.replace(marker, OBSERVER + '\n' + marker)


async def worker(manifest):
    import aiohttp
    from lobby_activation import OnceActivation, OperationJournal
    from lobby_material import PrivateRegistry
    from lobby_owner import OwnerSurface
    from lobby_native_rpc import BoundedNativeRpc
    from room_invite_store import RoomInviteStore
    data = json.loads(Path(manifest).read_text())
    root = Path(data['root'])
    def boundary(phase):
        private_json(root / 'phase.json', {'phase':phase, 'operation':data['operation'], 'pid':os.getpid()})
        threading.Event().wait()
    class FaultJournal(OperationJournal):
        def begin(self, scope, operation):
            result = super().begin(scope, operation)
            if operation == data['operation'] and data['fault'] == 'after_intent':
                boundary('after_intent')
            return result
        def complete(self, scope, operation, result):
            if operation == data['operation'] and data['fault'] == 'after_native_reply':
                boundary('after_native_reply')
            return super().complete(scope, operation, result)
    token = Path(data['native_token_file']).read_text().strip()
    async with aiohttp.ClientSession(trust_env=False) as session:
        async with session.ws_connect(f"ws://127.0.0.1:18790/api/ws?token={token}") as ws:
            rpc = BoundedNativeRpc(ws)
            caps = await rpc('groups.capabilities', {})
            if caps['authority_gateway_id'] != data['scope']['host_id']:
                raise RuntimeError('wrong_native_installation')
            registry = PrivateRegistry(Path(data['registry']), data['scope'], data['url'])
            activation = OnceActivation(RoomInviteStore(data['mc']), data['scope'], registry, FaultJournal(root / 'journal'), rpc)
            loop = asyncio.get_running_loop()
            def activate(payload):
                return asyncio.run_coroutine_threadsafe(activation.activate(payload), loop).result(60)
            with OwnerSurface(root / 'owner', {'activate':activate}) as owner:
                private_json(root / 'ready.json', {'base':owner.base})
                await asyncio.Event().wait()


def run_activation_faults(lab, host_rpc, target_rpc, loop, Scenario):
    from gate_s1_native import processes, PY
    from gate_c_runtime import pf
    from gate_s2b_native import post
    from gate_s2e_native import await_on, method_rows, projected_links
    from lobby_activation import OnceActivation, OperationJournal
    lab.report['activation_faults'] = []
    for operation in ('create', 'register:peer-b'):
        for fault in ('after_intent', 'after_native_reply'):
            name = operation.split(':')[0] + '-' + fault
            with Scenario(lab, name, host_rpc, target_rpc, loop) as s:
                s.prepare()
                root = s.root / 'fault-owner'
                root.mkdir(mode=0o700)
                manifest = root / 'manifest-private.json'
                private_json(manifest, dict(root=str(root),scope=s.scope, operation=operation,fault=fault,
                    native_token_file=str(lab.host_token_file),registry=str(s.root/'home-private'),url=s.url,mc=str(s.store.path)))
                argv = [str(PY),'-B',str(HERE/'s2e_activation_faults.py'),str(manifest)]
                log = (root/'worker.log').open('w')
                lab.logs.append(log)
                proc = subprocess.Popen(argv,cwd=HERE.parents[1],env=pf.isolated_child_env(lab.home),
                    stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
                record = dict(role='s2e-owner-'+name,argv=argv,home=str(lab.home),signals=[])
                lab.owned.append((proc,record))
                identity = next(r for r in processes() if r['pid'] == proc.pid)
                record.update(identity)
                lab.report['processes'].append(record)
                lab.report['commands'].append(dict(argv=argv,cwd=str(HERE.parents[1]),home=str(lab.home)))
                lab.flush()
                wait_file(root/'ready.json',proc)
                owner = SimpleNamespace(base=json.loads((root/'ready.json').read_text())['base'],token=(root/'owner/owner.token').read_text())
                lab.secret_values.append(owner.token)
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    pending = pool.submit(post,owner,dict(s.payload,op='activate'))
                    wait_file(root/'phase.json',proc)
                    s.check('actual_boundary',json.loads((root/'phase.json').read_text()) == dict(phase=fault,operation=operation,pid=proc.pid))
                    s.check('kill_exact_owned_identity', next(r for r in processes() if r['pid']==proc.pid) == identity and identity['pgid']==proc.pid)
                    record['returncode_before_requested_termination'] = proc.poll()
                    record['signals'].append(dict(signal='SIGKILL',at=time.time(),boundary=fault,operation=operation))
                    proc.kill()
                    proc.wait(10)
                    record.update(returncode=proc.returncode,terminated_on_request=True)
                    try:
                        pending.result(15)
                    except Exception as exc:
                        record['http_interrupted_type']=type(exc).__name__
                before = method_rows(lab,s.scope['room_id'])
                resumed = OnceActivation(s.store,s.scope,s.registry,OperationJournal(root/'journal'),host_rpc)
                from lobby_owner import OwnerSurface
                with OwnerSurface(root/'resumed-owner',{'activate':lambda p:await_on(loop,resumed.activate(p))}) as restarted:
                    lab.secret_values.append(restarted.token)
                    retries=[post(restarted,dict(s.payload,op='activate')) for _ in range(2)]
                code = 'creation_ambiguous' if operation=='create' else 'registration_ambiguous'
                s.check('restart_typed_no_replay', retries == [(409,{'error':code})]*2 and method_rows(lab,s.scope['room_id'])==before, responses=retries)
                expected_create = 0 if operation=='create' and fault=='after_intent' else 1
                expected_register = int(operation.startswith('register') and fault=='after_native_reply')
                counts={m:sum(r['phase']=='enter' and r['method']==m for r in before) for m in ('groups.create','groups.peer.register')}
                s.check('native_entries_zero_or_one', counts=={'groups.create':expected_create,'groups.peer.register':expected_register},counts=counts,methods=before)
                rooms=gs_read(lab.home/'shared-state.db','SELECT room_id FROM hosted_rooms WHERE room_id=?',(s.scope['room_id'],))
                links=projected_links(lab,s.scope,s.material,s.url)
                s.check('native_db_zero_or_one',len(rooms)==expected_create and len(links)==expected_register
                    and all(r['exact_private_material'] for r in links),rooms=rooms,links=links)
                events=gs_read(lab.home/'shared-state.db','SELECT event_id FROM hosted_room_events WHERE room_id=?',(s.scope['room_id'],))
                s.check('no_fault_path_send',events==[])
                lab.report['activation_faults'].append(dict(scope=s.scope,operation=operation,boundary=fault,killed_pid=proc.pid,
                    exit=proc.returncode,native_counts=counts,rooms=rooms,links=links,typed_retries=retries))


def q5_recovery(lab, host_rpc, target_rpc, loop, Scenario):
    from s2c_mint_faults import run_faults
    from gate_s2e_native import await_on, method_rows, projected_links
    from lobby_material import MintJournal, PrivateRegistry
    from lobby_activation import OnceActivation, OperationJournal
    from lobby_owner import OwnerSurface
    from gate_s2b_native import post
    from room_invite_store import RoomInviteStore
    run_faults(lab, target_rpc, loop)  # BOTH real mint-kill boundaries, typed authenticated retries.
    old = list(lab.report['mint_faults'])
    lab.report['q5_recovery'] = []
    def old_stays_blocked(when):
        for row in old:
            scope = row['scope']
            root = lab.out / row['boundary']
            journal = MintJournal(root / 'journal')
            registry = PrivateRegistry(root / 'home-private', scope, 'http://127.0.0.1:1/links/q5-unactivated')
            activation = OnceActivation(RoomInviteStore(root / 'mc.sqlite'), scope, registry, OperationJournal(root / 'activation'), host_rpc)
            with OwnerSurface(root / ('blocked-owner-' + when), {'activate':lambda p:await_on(loop,activation.activate(p))}) as owner:
                lab.secret_values.append(owner.token)
                response = post(owner,dict(op='activate',lobby_id=scope['lobby_id'],invite_id=scope['invite_id']))
            rooms = gs_read(lab.home / 'shared-state.db', 'SELECT room_id FROM hosted_rooms WHERE room_id=?', (scope['room_id'],))
            lab.check('q5_' + row['boundary'] + '_' + when + '_terminal_never_activated',
                journal.state(scope) == 'intent' and response[0] == 409 and rooms == [] and projected_links(lab,scope) == []
                and not any(r['method'] in ('groups.create','groups.peer.register') for r in method_rows(lab,scope['room_id'])),
                journal='intent',response=response,rooms=rooms)
    old_stays_blocked('before')
    with Scenario(lab,'q5-new-invite',host_rpc,target_rpc,loop) as recovery:
        recovery.check('new_owner_confirmed_scope', all(recovery.scope[k] != row['scope'][k]
            for row in old for k in ('lobby_id','invite_id','room_id')), scope=recovery.scope)
        recovery.prepare()
        recovery.succeed()
        lab.report['q5_recovery'].append(dict(old_scopes=[row['scope'] for row in old],new_scope=recovery.scope))
    old_stays_blocked('after')
    # Count actual native grants by exact Room, not the aggregate total.
    mints = [r for r in map(json.loads, lab.audit.read_text().splitlines()) if r.get('event')=='native_issue_room_grant_return']
    counts = {r['room_id']:sum(m['room_id']==r['room_id'] for m in mints) for r in mints}
    lab.check('all_scopes_no_duplicate_mint',bool(counts) and set(counts.values())=={1},counts=counts)


def gs_read(*args):
    from gate_c_runtime import read_db
    return read_db(*args)


if __name__=='__main__':
    try:
        asyncio.run(worker(sys.argv[1]))
    except BaseException as exc:
        print(json.dumps({'error_type':type(exc).__name__}))
        raise SystemExit(1)
