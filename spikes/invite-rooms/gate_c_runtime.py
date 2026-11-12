"""Real host groups RPC + remote native RoomLink acceptance probe. No mocks."""
import asyncio
import importlib.util
import json
from pathlib import Path
import secrets
import sqlite3
import subprocess
import time
import uuid
import aiohttp
import urllib.request
import urllib.error
from adapter import LoopbackAdapter
from room_firewall import RoomFirewall, UpstreamResponse

REPO = Path(__file__).resolve().parents[2]  # checkout or worktree that holds this spike
CORE = Path('/Users/albi/.hermes/hermes-agent')
ROOT = Path('/Users/albi/.hermes/cache/scratch/room-network-spike-20261011-164930')
spec = importlib.util.spec_from_file_location('gate_a_helpers', REPO / 'spikes/invite-rooms/gate_a_runtime.py')
assert spec is not None and spec.loader is not None, 'native admission helper is required'
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)
pf = helper.pf


def config(home, port, key, link=False):
    path = home / 'config.yaml'
    path.write_text(path.read_text() + f'''
model:
  provider: custom
  default: qwen3.8-27b-oq4e-mtp
  base_url: http://127.0.0.1:8083/v1
  api_key: local-lab-no-secret
custom_providers:
  - name: s1-lab-omlx
    base_url: http://127.0.0.1:8083/v1
    extra_body:
      chat_template_kwargs:
        enable_thinking: false
model_options:
  reasoning:
    enabled: false
memory:
  memory_enabled: false
  user_profile_enabled: false
honcho:
  enabled: false
kanban:
  enabled: false
gateway:
  multiplex_profiles: false
  room_link_url: http://127.0.0.1:{port}
  api_server:
    enabled: {str(link).lower()}
    host: 127.0.0.1
    port: {port}
    key: {key}
''', encoding='utf-8')
    secret_file = home / '.env'
    secret_file.write_text('API_SERVER_KEY=' + key + '\n', encoding='utf-8')
    secret_file.chmod(0o600)


async def rpc(ws, name, params):
    rid = uuid.uuid4().hex
    await ws.send_json({'jsonrpc': '2.0', 'id': rid, 'method': name, 'params': params})
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        msg = await ws.receive(timeout=max(.1, deadline-time.monotonic()))
        if msg.type != aiohttp.WSMsgType.TEXT:
            raise RuntimeError('RPC socket stopped while calling ' + name + ': ' + str(msg.type))
        data = json.loads(msg.data)
        if data.get('id') != rid:
            continue
        if 'error' in data:
            raise RuntimeError(name + ': ' + json.dumps(data['error']))
        return data['result']
    raise TimeoutError(name + ' RPC deadline')


def read_db(path, query, parameters=()):
    with sqlite3.connect('file:' + str(path) + '?mode=ro', uri=True, timeout=5) as connection:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute(query, parameters)]


def native_backend(target_base, expected_grant):
    def forward(request):
        auth = dict(request.headers).get('authorization', '')
        if auth != 'HermesRoom ' + expected_grant:
            print(f"ADAPTER_AUTH_MISMATCH expected_len={len(expected_grant)} actual_prefix={auth[:20]!r} actual_len={len(auth)}", flush=True)
        native = urllib.request.Request(
            target_base + request.path, data=request.body or None, method=request.method,
            headers={name: value for name, value in request.headers})
        try:
            with urllib.request.urlopen(native, timeout=25) as response:
                payload = response.read()
                headers = tuple((name, value) for name, value in response.headers.items()
                                if name.lower() in {'content-type', 'content-length'})
                return UpstreamResponse(response.status, headers, payload)
        except urllib.error.HTTPError as exc:
            headers = tuple((name, value) for name, value in exc.headers.items()
                            if name.lower() in {'content-type', 'content-length'})
            return UpstreamResponse(exc.code, headers, exc.read())
    return forward


async def workflow(report, home, target, host_port, target_port, owner_key, ws_key, children, adapter):
    base = f'http://127.0.0.1:{target_port}'
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if any(p.poll() is not None for p in children):
            raise RuntimeError('owned lab process exited before native readiness')
        try:
            status, _, _ = await asyncio.to_thread(helper.request, base, '/v1/models', owner_key)
            break
        except (OSError, RuntimeError):
            await asyncio.sleep(.5)
    else:
        raise TimeoutError('native target HTTP readiness')
    report['target_readiness_http'] = status
    async with aiohttp.ClientSession(trust_env=False) as session:
        url = f'ws://127.0.0.1:{host_port}/api/ws?token={ws_key}'
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            try:
                ws = await asyncio.wait_for(session.ws_connect(url), timeout=5)
                break
            except (aiohttp.ClientError, OSError):
                if any(p.poll() is not None for p in children):
                    raise RuntimeError('owned lab process exited before host RPC readiness')
                await asyncio.sleep(.5)
        else:
            raise TimeoutError('host authenticated WebSocket readiness')
        async with ws:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                caps = await rpc(ws, 'groups.capabilities', {})
                if caps.get('driver'):
                    break
                await asyncio.sleep(.5)
            else:
                raise TimeoutError('groups driver never ready')
            report['groups_capabilities'] = {k: caps[k] for k in ('driver', 'authority_gateway_id', 'methods')}
            owner_id = caps['authority_gateway_id']
            assert owner_id == report['identity']['gateway_id']
            room_id = 'real-gate-' + uuid.uuid4().hex
            _, _, invitation = await asyncio.to_thread(helper.request, base,
                '/v1/room-members/invitations', owner_key,
                {'room_id': room_id, 'home_install_id': owner_id, 'authority_gateway_id': owner_id,
                 'authority_epoch': 1, 'member_id': 'peer-b', 'ttl_seconds': 600})
            grant = invitation['grant']
            catalog = invitation['catalog']
            assert catalog['installation_id'] == report['identity']['second_gateway_id']
            report['catalog'] = catalog
            report['native_installations_distinct'] = owner_id != catalog['installation_id']
            scope = {'room_id': room_id, 'home_install_id': owner_id,
                     'authority_gateway_id': owner_id, 'authority_epoch': 1,
                     'member_id': 'peer-b', 'target_install_id': catalog['installation_id'],
                     'target_profile': 'default'}
            from run_bindings import HostRunBindings
            bindings = HostRunBindings(home / 'room-run-bindings.db')
            adapter.native_receipt_db = target / 'runs_idempotency.db'
            adapter_url = adapter.bind(
                'real-' + uuid.uuid4().hex,
                RoomFirewall(expected_scope=scope, backend=native_backend(base, grant),
                             verify_native=bindings, on_admitted=bindings.record_ack),
                target_url=base, scope=scope)
            report['adapter_url'] = adapter_url
            roster = [
                {'member_id': 'local-a', 'profile': 'default', 'handle': 'local'},
                {'member_id': 'peer-b', 'profile': 'default', 'handle': 'peer',
                 'target': {'kind': 'peer', 'peer_id': 'target-b',
                            'installation_id': catalog['installation_id'], 'profile': 'default',
                            'capability_digest': catalog['catalog_digest']}}]
            created = await rpc(ws, 'groups.create', {'room_id': room_id, 'name': 'Isolated real Gate A', 'members': roster})
            report['room'] = created['room']
            registered = await rpc(ws, 'groups.peer.register',
                {'room_id': room_id, 'member_id': 'peer-b', 'target_url': adapter_url, 'target_profile': 'default',
                 'catalog': catalog, 'grant': grant, 'cancellation_scope_id': 'cancel-' + room_id,
                 'trace_id': 'trace-' + room_id})
            report['peer_registration'] = registered
            report['native_probe_catalog_equality'] = bool(registered['registered'])
            event_id = 'user-' + uuid.uuid4().hex
            send_params = {'room_id': room_id, 'event_id': event_id,
                           'payload': {'text': '@peer Reply only LAB_READY, without mentioning other members. For any subsequent round reply PASS.',
                                       'thread_id': 'thread-1'}}
            first = await rpc(ws, 'groups.send', send_params)
            replay = await rpc(ws, 'groups.send', send_params)
            report['user_send'] = {'event_id': first['event']['event_id'],
                                   'seq': first['event']['seq'],
                                   'duplicate_same_event_id': replay['event']['event_id'] == first['event']['event_id'],
                                   'duplicate_same_seq': replay['event']['seq'] == first['event']['seq']}
            deadline = time.monotonic() + 150
            page = None
            while time.monotonic() < deadline:
                page = await rpc(ws, 'groups.log', {'room_id': room_id, 'since_seq': 0, 'limit': 500})
                replies = [event for event in page['events'] if event['kind'] == 'message.member'
                           and event['payload'].get('member_id') == 'peer-b']
                if replies:
                    break
                if any(event['kind'] in {'turn.failed', 'turn.deferred'} for event in page['events']):
                    report['canonical_log'] = page
                    raise RuntimeError('native Room turn failed or deferred')
                await asyncio.sleep(.5)
            else:
                report['canonical_log'] = page
                raise TimeoutError('real addressed remote member reply deadline')
            await rpc(ws, 'groups.stop', {'room_id': room_id, 'cancel_id': 'bounded-' + room_id})
            replay_page = await rpc(ws, 'groups.log', {'room_id': room_id, 'since_seq': 0, 'limit': 500})
            report['canonical_log'] = replay_page
            assert not replay_page['has_more']
            report['remote_reply'] = replies[0]
            original = [(e['seq'], e['event_id']) for e in page['events']]
            second = [(e['seq'], e['event_id']) for e in replay_page['events']]
            report['canonical_original_ids_order_preserved'] = second[:len(original)] == original
            report['canonical_no_duplicate_event_ids'] = len(second) == len({i for _, i in second})
            host_db = home / 'shared-state.db'
            receipt = read_db(host_db, 'SELECT run_id, task_id, execution_generation FROM hosted_room_remote_runs WHERE room_id=?', (room_id,))
            report['host_receipts'] = receipt
            # Reconstruct the native, immutable request from the actual persisted driver payload.
            chosen = next(r for r in receipt if r['task_id'] == replies[0]['payload']['task_id'])
            row = read_db(host_db, 'SELECT payload_json, execution_generation, source_event_seq FROM hosted_room_driver_tasks WHERE room_id=? AND task_id=?', (room_id, chosen['task_id']))[0]
            payload = json.loads(row['payload_json'])
            prompt = payload['prompt']
            import hashlib
            dispatch = {'protocol_version': 2, 'room_id': room_id, 'home_install_id': owner_id,
                        'authority_gateway_id': owner_id, 'authority_epoch': created['room']['authority_epoch'],
                        'member_id': 'peer-b', 'target_install_id': catalog['installation_id'], 'target_profile': 'default',
                        'task_id': chosen['task_id'], 'execution_generation': chosen['execution_generation'],
                        'source_event_seq': row['source_event_seq'], 'cancellation_scope_id': 'cancel-' + room_id,
                        'prompt': prompt, 'prompt_digest': hashlib.sha256(prompt.encode()).hexdigest(),
                        'capability_digest': catalog['catalog_digest'], 'execution_policy_digest': catalog['execution_policy']['policy_digest'],
                        'trace_id': 'trace-' + room_id}
            key = f"room:{chosen['task_id']}:{chosen['execution_generation']}"
            body = {'input': prompt, 'hosted_room_dispatch': dispatch}
            async with session.post(base + '/v1/runs', json=body,
                                    headers={'Authorization': 'HermesRoom ' + grant, 'Idempotency-Key': key},
                                    timeout=aiohttp.ClientTimeout(total=90)) as response:
                replayed = await response.json()
                report['duplicate_run_http'] = response.status
                report['duplicate_run'] = replayed
            assert replayed['run_id'] == chosen['run_id'] and replayed.get('replayed') is True
            rows = read_db(target / 'runs_idempotency.db', 'SELECT idempotency_key, run_id FROM run_idempotency WHERE idempotency_key=?', (key,))
            report['target_matching_durable_receipts'] = rows
            report['exactly_one_admitted_run_for_duplicate'] = len(rows) == 1 and rows[0]['run_id'] == chosen['run_id']
            _, _, run = await asyncio.to_thread(helper.request, base, '/v1/runs/' + chosen['run_id'], grant, None, 'HermesRoom')
            report['target_run'] = {k: run.get(k) for k in ('run_id', 'status', 'output')}
            report['gate_C_passed'] = all((registered['registered'], report['native_installations_distinct'],
                run.get('status') == 'completed', replies[0]['payload']['text'] == run.get('output'),
                report['canonical_original_ids_order_preserved'], report['canonical_no_duplicate_event_ids'],
                report['exactly_one_admitted_run_for_duplicate'], report['user_send']['duplicate_same_event_id'],
                report['user_send']['duplicate_same_seq']))


def main():
    branch = subprocess.check_output(['git', 'branch', '--show-current'], cwd=REPO, text=True).strip()
    if branch != 'feat/invite-rooms':
        raise RuntimeError('wrong checkout branch: ' + branch)
    head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=REPO, text=True).strip()
    stamp = uuid.uuid4().hex[:12]
    home = pf.APPROVED_RUNTIME_ROOT / ('rpc-' + stamp)
    host_port, target_port = 18790, 18791
    baseline = pf.run_preflight(home, port=host_port)
    target = home / 'second'
    assert pf.check_port_available(target_port)['available']
    keys = [secrets.token_urlsafe(32), secrets.token_urlsafe(32), secrets.token_hex(32)]
    config(home, host_port, keys[0])
    config(target, target_port, keys[1], link=True)
    policy_a, policy_b = pf.effective_native_policy(home), pf.effective_native_policy(target)
    assert policy_a['policy_digest'] == policy_b['policy_digest'] == baseline['effective_policy']['policy_digest']
    token_dir = home / '.hermes' / 'desktop-ssh' / uuid.uuid4().hex
    token_dir.mkdir(parents=True, mode=0o700)
    token_dir.chmod(0o700)
    token_file = token_dir / (uuid.uuid4().hex[:16] + '.token')
    token_file.write_text(keys[2], encoding='utf-8')
    token_file.chmod(0o600)
    report = {'gate_C_passed': False, 'branch': branch, 'head': head, 'home': str(home), 'target_home': str(target),
              'host_port': host_port, 'target_port': target_port, 'identity': baseline['identity'],
              'policy': policy_b, 'policy_unchanged': True}
    children, logs = [], []
    adapter = None
    try:
        commands = [
            (target, [str(CORE / 'venv/bin/hermes'), 'gateway', 'run', '--external-supervisor']),
            (home, [str(CORE / 'venv/bin/hermes'), 'serve', '--isolated', '--host', '127.0.0.1', '--port', str(host_port),
                    '--ssh-session-token-file', str(token_file)])]
        for scope, argv in commands:
            logfile = scope / 'owned-runtime.log'
            log = logfile.open('w', encoding='utf-8')
            logs.append(log)
            children.append(subprocess.Popen(argv, cwd=CORE, env=pf.isolated_child_env(scope), stdout=log,
                                              stderr=subprocess.STDOUT, start_new_session=True))
        adapter = LoopbackAdapter()
        adapter.start(host='127.0.0.1')
        asyncio.run(workflow(report, home, target, host_port, target_port, keys[1], keys[2], children, adapter))
    except Exception as exc:
        report['error_type'] = type(exc).__name__
        message = str(exc)
        for key in keys:
            message = message.replace(key, '[LAB_SECRET_REDACTED]')
        report['error'] = message
    finally:
        if adapter is not None:
            adapter.close()
        for child in reversed(children):
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=10)
        for log in logs:
            log.close()
        report['child_exit_codes'] = [p.returncode for p in children]
        report['teardown_complete'] = all(p.poll() is not None for p in children)
        path = ROOT / ('parent-groups-gate-' + stamp + '.json')
        path.write_text(json.dumps(report, indent=2), encoding='utf-8')
        report['report_path'] = str(path)
        print(json.dumps(report, indent=2), flush=True)
    return 0 if report['gate_C_passed'] and report['teardown_complete'] else 1

if __name__ == '__main__':
    raise SystemExit(main())
