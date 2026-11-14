"""Real isolated RoomLink API-server admission/idempotency baseline.
No mocks: starts one supported native gateway, issues a real grant, dispatches the
actual HostedMemberDispatch wire shape, retries the same request, polls the run,
and records the durable receipt evidence without secrets.
"""
from __future__ import annotations
import hashlib, importlib.util, json, secrets, subprocess, time, urllib.error, urllib.request, uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]  # checkout or worktree that holds this spike
CORE = Path('/Users/albi/.hermes/hermes-agent')
ROOT = Path('/Users/albi/.hermes/cache/scratch/room-network-spike-20261011-164930')
spec = importlib.util.spec_from_file_location('approved_preflight', REPO / 'spikes/invite-rooms/preflight.py')
pf = importlib.util.module_from_spec(spec); spec.loader.exec_module(pf)


def request(base, path, token=None, payload=None, scheme='Bearer', method=None):
    headers = {'Content-Type': 'application/json'}
    if token: headers['Authorization'] = scheme + ' ' + token
    data = json.dumps(payload, separators=(',', ':')).encode() if payload is not None else None
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method or ('POST' if data else 'GET'))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=90 if path == '/v1/runs' else 15) as response:
            return response.status, dict(response.headers), json.load(response)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors='replace')
        raise RuntimeError(f'HTTP {exc.code} {path}: {body[:500]}') from exc


def main():
    stamp = uuid.uuid4().hex
    home = pf.APPROVED_RUNTIME_ROOT / ('gate-a-' + stamp)
    port = 19400 + (int(stamp[:4], 16) % 300)
    preflight = pf.run_preflight(home, port=port)
    api_key = secrets.token_urlsafe(32)
    config = home / 'config.yaml'
    config.write_text(config.read_text() + f'''\nmodel:\n  provider: custom\n  default: qwen3.8-27b-oq4e-mtp\n  base_url: http://127.0.0.1:8083/v1\n  api_key: local-lab-no-secret\nmodel_options:\n  reasoning:\n    enabled: false\nmemory:\n  memory_enabled: false\n  user_profile_enabled: false\n  provider: local\nhoncho:\n  enabled: false\nkanban:\n  enabled: false\ngateway:\n  multiplex_profiles: false\n  api_server:\n    enabled: true\n    host: 127.0.0.1\n    port: {port}\n    key: {api_key}\n''', encoding='utf-8')
    log_path = ROOT / ('gate-a-' + stamp + '.log')
    report_path = ROOT / ('gate-a-runtime-report-' + stamp + '.json')
    report = {'gate_A_passed': False, 'home': str(home), 'api_port': port,
              'startup': 'hermes gateway run --external-supervisor',
              'policy': preflight['effective_policy'], 'identity': preflight['identity'],
              'model': 'qwen3.8-27b-oq4e-mtp', 'log_path': str(log_path),
              'stages': []}
    child = None
    with log_path.open('w', encoding='utf-8') as log:
      try:
        child = subprocess.Popen([str(CORE / 'venv/bin/hermes'), 'gateway', 'run', '--external-supervisor'], cwd=CORE, env=pf.isolated_child_env(home), stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        base = f'http://127.0.0.1:{port}'
        deadline = time.monotonic() + 75
        while time.monotonic() < deadline:
          if child.poll() is not None: raise RuntimeError(f'gateway exited {child.returncode}')
          try:
            status, _, models = request(base, '/v1/models', api_key); break
          except (OSError, urllib.error.URLError, RuntimeError): time.sleep(.5)
        else: raise TimeoutError('authenticated /v1/models readiness deadline')
        report['stages'].append({'name':'authenticated_capability_readiness','http':status,'models': [x.get('id') for x in models.get('data', [])]})
        room_id = 'gate-a-' + stamp
        owner_id = preflight['identity']['gateway_id']
        invitation = {'room_id':room_id, 'home_install_id':owner_id, 'authority_gateway_id':preflight['identity']['second_gateway_id'], 'authority_epoch':1, 'member_id':'member-b', 'ttl_seconds':600}
        status, _, issued = request(base, '/v1/room-members/invitations', api_key, invitation)
        grant = issued['grant']
        status, _, caps = request(base, '/v1/room-members/capabilities', grant, scheme='HermesRoom')
        catalog = caps['catalog']
        report['stages'].append({'name':'real_target_grant_capability','invitation_http':status,'capabilities_http':status,'object':caps.get('object'),'room_id':caps.get('room_id'),'catalog':catalog})
        prompt = 'Reply only LAB_READY'
        task_id = 'task-' + stamp
        generation = 1
        dispatch = {'protocol_version':2,'room_id':room_id,'home_install_id':owner_id,'authority_gateway_id':preflight['identity']['second_gateway_id'],'authority_epoch':1,'member_id':'member-b','target_install_id':catalog['installation_id'],'target_profile':'default','task_id':task_id,'execution_generation':generation,'source_event_seq':1,'cancellation_scope_id':'cancel-'+stamp,'prompt':prompt,'prompt_digest':hashlib.sha256(prompt.encode()).hexdigest(),'capability_digest':catalog['catalog_digest'],'execution_policy_digest':catalog['execution_policy']['policy_digest'],'trace_id':'trace-'+stamp}
        body = {'input':prompt, 'hosted_room_dispatch':dispatch}
        headers = {'Idempotency-Key':f'room:{task_id}:{generation}'}
        def run_post():
          h={'Content-Type':'application/json','Authorization':'HermesRoom '+grant,**headers}
          req=urllib.request.Request(base+'/v1/runs', data=json.dumps(body,separators=(',',':')).encode(), headers=h, method='POST')
          with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=90) as r: return r.status, dict(r.headers), json.load(r)
        first_status, first_headers, first = run_post(); second_status, second_headers, second = run_post()
        report['dispatch']={'task_id':task_id,'execution_generation':generation,'idempotency_key':headers['Idempotency-Key'],'first_http':first_status,'second_http':second_status,'first':first,'second':second,'same_run_id':first.get('run_id') == second.get('run_id'),'replayed_header':second_headers.get('Idempotency-Replayed')}
        run_id=first['run_id']; final=None
        for _ in range(90):
          _, _, current=request(base, '/v1/runs/'+run_id, grant, scheme='HermesRoom')
          final=current
          if current.get('status') in {'completed','failed','cancelled','interrupted'}: break
          time.sleep(1)
        report['run']={'run_id':run_id,'status':final.get('status'),'output':final.get('output'),'task_id':task_id,'execution_generation':generation}
        # The native store is in the scoped home; record only table/count and matching identity.
        dbs=sorted(str(p.relative_to(home)) for p in home.rglob('*.db'))
        report['durable_db_files']=dbs
        report['duplicate_receipt_evidence']={'same_run_id':report['dispatch']['same_run_id'],'native_replayed':bool(second.get('replayed')),'persistent_store_present':bool(dbs)}
        # Admission-only smoke cannot pass the full groups workflow acceptance gate.
        report['admission_passed'] = bool(final and final.get('status') == 'completed' and final.get('output') and report['dispatch']['same_run_id'] and second.get('replayed') and dbs)
      except Exception as exc:
        report.update(error_type=type(exc).__name__, error=str(exc))
      finally:
        if child is not None and child.poll() is None:
          child.terminate()
          try: child.wait(timeout=20)
          except subprocess.TimeoutExpired: child.kill(); child.wait(timeout=10)
        report['child_exit_code'] = child.returncode if child else None
        report['teardown_complete'] = child is None or child.poll() is not None
        report_path.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report, indent=2))
    return 0 if report.get('admission_passed') and report['teardown_complete'] else 1

if __name__ == '__main__': raise SystemExit(main())
