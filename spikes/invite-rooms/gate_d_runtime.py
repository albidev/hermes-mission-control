"""D-only process restart evidence. Uses real native lab APIs, never row writes."""
from __future__ import annotations
import asyncio
import hashlib
import json
import time
import uuid
import aiohttp
from gate_s1_native import NativeLab, LONG, TERMINAL, processes, gc, _receipts

CASE_CHECKS = {'host': ['host_gateway_process_restart_persistence'],
               'target_graceful_restart': ['target_graceful_restart_truthful_outcome'],
               'target_crash_restart': ['target_crash_restart_truthful_outcome']}


def target_process_restart_truthful_outcome(e):
    """Raw native ambiguity is evidence, a sanitized 502 alone is not."""
    try:
        raw = [r for r in e['raw_native'] if r.get('provenance') == 'native_backend_before_sanitizer']
        statuses = [r for r in raw if r['method'] == 'GET' and r['path'] == '/v1/runs/' + e['run_id']]
        replays = [r for r in raw if r['method'] == 'POST' and r['path'] == '/v1/runs'
                   and r.get('request_run_id') == e['run_id']]
        noncompleted = any((r['http'] == 200 and r.get('run_id') == e['run_id']
                            and r.get('status') in {'unknown', 'failed', 'interrupted', 'cancelled'})
                           or (r['http'] == 404 and r.get('error_code') == 'run_not_found') for r in statuses)
        replay_proven = bool(replays) and all(
            (r['http'] >= 500 and r.get('error_type') and r.get('error_code') and r.get('run_id') in (None, e['run_id']))
            or (r['http'] in (200, 202) and r.get('run_id') == e['run_id'] and r.get('replayed') is True)
            for r in replays)
        f = e['fresh']
        canonical = e['canonical']
        if e['outcome_class'] == 'blocked_core_cancelled_history':
            task_replies = [r for r in e['canonical_events'] if r.get('kind') == 'message.member'
                            and r.get('payload', {}).get('task_id') == e['task_id']]
            return bool(e.get('case') == 'target_graceful_restart'
                and any(r['http'] == 200 and r.get('run_id') == e['run_id']
                        and r.get('status') == 'cancelled' for r in statuses)
                and replays and all(r['http'] in (200, 202) and r.get('run_id') == e['run_id']
                                   and r.get('replayed') is True for r in replays)
                and e['native_before_replay'] == e['native_after_replay']
                and sum(r.get('run_id') == e['run_id'] for r in e['native_before_replay']) == 1
                and e['canonical_reply_count'] == len(task_replies) == 0
                and f['status'] == 'blocked_core_cancelled_history' and f['observed_seconds'] >= 55
                and f['driver_tasks'] == [] and f['original_driver'] == [dict(task_id=e['task_id'],
                    status='running', execution_generation=e['dispatch_generation'])]
                and f['user'] in canonical and canonical == e['db']
                and len(canonical) == len({r['event_id'] for r in canonical})
                and all(a['seq'] < b['seq'] for a, b in zip(canonical, canonical[1:])))
        reply = f['reply']
        task_replies = [r for r in e['canonical_events'] if r.get('kind') == 'message.member'
                        and r.get('payload', {}).get('task_id') == e['task_id']]
        completed = (e['outcome_class'] == 'completed'
            and any(r['http'] == 200 and r.get('run_id') == e['run_id']
                    and r.get('status') == 'completed' for r in statuses)
            and bool(replays) and all(r['http'] in (200, 202) and r.get('run_id') == e['run_id']
                                      and r.get('replayed') is True for r in replays)
            and e['canonical_reply_count'] == len(task_replies) == 1
            and task_replies[0].get('payload', {}).get('member_id') == 'peer-b'
            and dict(seq=task_replies[0]['seq'], event_id=task_replies[0]['event_id']) in canonical)
        ambiguous = (e['outcome_class'] in {'lost', 'indeterminate'} and noncompleted and replay_proven
                     and e['canonical_reply_count'] == len(task_replies) == 0)
        return bool((completed or ambiguous)
            and e['native_before_replay'] and e['native_before_replay'] == e['native_after_replay']
            and sum(r.get('run_id') == e['run_id'] for r in e['native_before_replay']) == 1
            and f['status'] == 'completed' and f['run_id'] != e['run_id'] and f['task_id'] != e['task_id']
            and f['receipts'] == [[f['run_id']]] and f['user'] in canonical
            and reply.get('kind') == 'message.member' and reply.get('payload', {}).get('task_id') == f['task_id']
            and reply.get('payload', {}).get('member_id') == 'peer-b' and reply['seq'] > f['user']['seq']
            and dict(seq=reply['seq'], event_id=reply['event_id']) in canonical
            and canonical == e['db'] and len(canonical) == len({r['event_id'] for r in canonical})
            and all(a['seq'] < b['seq'] for a, b in zip(canonical, canonical[1:])))
    except (KeyError, TypeError, IndexError):
        return False


def valid_restart(e):
    """Do not confuse a durable receipt with recovered computation success."""
    old, new = e['old_process'], e['new_process']
    case = e.get('case')
    sig = 'SIGKILL' if case == 'target_crash_restart' else 'SIGTERM'
    identity = old.get('fault_identity_verified', {})
    if (case not in CASE_CHECKS or old.get('fault_case') != case
            or e['role'] != ('host' if case == 'host' else 'target')
            or any(identity.get(k) != old.get(k) for k in ('pid', 'birth', 'pgid'))
            or old.get('pgid') != old.get('pid')
            or [s.get('signal') for s in old.get('signals', [])] != [sig]
            or (sig == 'SIGKILL' and old.get('returncode') != -9)):
        return False
    before, canonical, db = e['before'], e['canonical'], e['db']
    outcome = e['observed_outcome']
    observed = (outcome.get('http') == 200 and outcome.get('run_id') == e['run_id']
                and outcome.get('status') in TERMINAL)
    lost = (outcome.get('http') == 404 and outcome.get('classification') == 'lost'
            and bool(outcome.get('code')))
    indeterminate = (outcome.get('classification') == 'indeterminate'
                     and bool(outcome.get('observations')))
    return bool(e['role'] in {'host', 'target'} and e['running']
        and e['running'][-1].get('http') == 200
        and e['running'][-1].get('status') == 'running'
        and e['running'][-1].get('run_id') == e['run_id']
        and old.get('pid') != new.get('pid') and old.get('birth') and new.get('birth')
        and old.get('returncode') is not None and old.get('terminated_on_request') is True
        and new.get('alive') is True and new.get('pgid') == new.get('pid')
        and e['stable_before'] and e['stable_before'] == e['stable_after']
        and e['authority_before'] == e['authority_after']
        and e['receipts_before'] == e['receipts_after'] == [[e['run_id']]]
        and ((e['role'] == 'host' and e['replay_http'] in (200, 202)
              and e['replay'].get('run_id') == e['run_id'] and e['replay'].get('replayed') is True)
             or (e['role'] == 'target' and target_process_restart_truthful_outcome(e)))
        and before and canonical == db and canonical[:len(before)] == before
        and e['user'] in before
        and len(canonical) == len({p['event_id'] for p in canonical})
        and all(a['seq'] < b['seq'] for a, b in zip(canonical, canonical[1:]))
        and e['driver_tasks'] == [{'task_id': e['task_id'], 'execution_generation': e['dispatch_generation']}]
        and (observed or lost or indeterminate))


def raw_native_projection(request, response):
    """Project the actual backend return BEFORE firewall/adapter sanitization.

    No message, output, headers, request prompt, grant or credential is retained.
    """
    body = json.loads(response.body)
    error = body.get('error') if isinstance(body.get('error'), dict) else {}
    return dict(provenance='native_backend_before_sanitizer', method=request.method,
                path=request.path, http=response.status,
                **{k: body[k] for k in ('run_id', 'status', 'execution_generation', 'replayed') if k in body},
                error_type=error.get('type'), error_code=error.get('code'))


class RestartLab(NativeLab):
    def replay_inventory(self):
        """Read-only immutable native admission coordinates, all rows/counts.

        Dispatch generation (host DB) and admission generation (target state DB)
        are deliberately separate fields/domains; no equality is inferred.
        """
        return (gc.read_db(self.target/'runs_idempotency.db',
                    'SELECT run_id,idempotency_key FROM run_idempotency ORDER BY run_id')
                + gc.read_db(self.target/'state.db',
                    'SELECT seq,admission_id,request_id,target_session_id,generation AS admission_generation FROM session_admissions ORDER BY seq')
                + gc.read_db(self.home/'shared-state.db',
                    'SELECT task_id,execution_generation AS dispatch_generation FROM hosted_room_driver_tasks WHERE room_id=? ORDER BY task_id',
                    (self.scope['room_id'],)))

    async def fresh_control(self, ws, original_task, original_generation):
        """A NEW user intent after restart, never recovery of the old task."""
        room = self.scope['room_id']
        sent = await gc.rpc(ws, 'groups.send', dict(room_id=room, event_id='d-fresh-'+uuid.uuid4().hex,
            payload=dict(text='@peer Reply only D_FRESH_OK.', thread_id='fresh-'+self.stamp)))
        user = sent['event']
        started = time.monotonic()
        deadline = started + 55
        while time.monotonic() < deadline:
            rows = gc.read_db(self.home/'shared-state.db',
                'SELECT r.run_id,r.task_id,r.execution_generation FROM hosted_room_remote_runs r JOIN hosted_room_driver_tasks t '
                'ON r.room_id=t.room_id AND r.task_id=t.task_id WHERE r.room_id=? AND t.source_event_seq=?', (room,user['seq']))
            if rows:
                assert len(rows) == 1
                row = rows[0]
                status, result = await self.call('/v1/runs/'+row['run_id'])
                page = await gc.rpc(ws, 'groups.log', dict(room_id=room, since_seq=0, limit=500))
                assert not page['has_more']
                replies = [e for e in page['events'] if e['kind'] == 'message.member' and e['payload'].get('task_id') == row['task_id']]
                if status == 200 and result.get('status') == 'completed' and len(replies) == 1:
                    key = f"room:{row['task_id']}:{row['execution_generation']}"
                    return dict(run_id=row['run_id'], task_id=row['task_id'], status=result['status'],
                        receipts=_receipts(self.target,key), user=dict(seq=user['seq'],event_id=user['event_id']), reply=replies[0])
            await asyncio.sleep(.25)
        fresh = dict(status='not_completed', user=dict(seq=user['seq'], event_id=user['event_id']),
            observed_seconds=time.monotonic()-started,
            driver_tasks=gc.read_db(self.home/'shared-state.db',
                'SELECT task_id,status,execution_generation FROM hosted_room_driver_tasks WHERE room_id=? AND source_event_seq=?',
                (room,user['seq'])),
            original_driver=gc.read_db(self.home/'shared-state.db',
                'SELECT task_id,status,execution_generation FROM hosted_room_driver_tasks WHERE room_id=? AND task_id=?',
                (room,original_task)))
        self.report['d_fresh_control_readback'] = fresh
        self.flush()
        return fresh

    def fault_process(self, proc, case):
        """Signal only this owned PID, birth and PGID verified at fault time."""
        record = next(r for p, r in self.owned if p is proc)
        current = next((r for r in processes() if r['pid'] == proc.pid), None)
        if (current is None or any(current[k] != record[k] for k in ('pid', 'pgid', 'birth'))
                or current['pgid'] != proc.pid or proc.poll() is not None):
            raise RuntimeError('fault_identity_changed_or_process_not_running')
        record['fault_identity_verified'] = current
        record['returncode_before_requested_termination'] = None
        record['fault_case'] = case
        sig = 'SIGKILL' if case == 'target_crash_restart' else 'SIGTERM'
        record['signals'].append(dict(signal=sig, at=time.time(), purpose='requested_restart_fault'))
        if sig == 'SIGKILL': proc.kill()
        else: proc.terminate()
        # A graceful timeout FAILS the case, never silently escalates to KILL.
        proc.wait(10 if sig == 'SIGKILL' else 45)
        record['returncode'] = proc.returncode
        record['terminated_on_request'] = True
        self.flush()

    def terminate(self, proc):
        # close() revisits the already terminated fault process; preserve its
        # first requested-termination receipt instead of overwriting with -15.
        record = next(r for p, r in self.owned if p is proc)
        if record.get('returncode') is not None and proc.poll() is not None:
            return
        return super().terminate(proc)

    def spawn(self, role):
        if role == 'host':
            # Native serve consumes/unlinks this supported single-use handoff
            # file. Re-arm the SAME credential, never rotate a Room identity.
            from pathlib import Path
            token = Path(self.args['host'][-1])
            assert token.resolve().is_relative_to(self.home.resolve())
            if not token.exists():
                token.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
                with token.open('x') as stream:
                    token.chmod(0o600)
                    stream.write(self.keys[2])
        return super().spawn(role)

    async def connect(self, session):
        deadline = time.monotonic() + 50
        while time.monotonic() < deadline:
            try:
                ws = await session.ws_connect('ws://127.0.0.1:18790/api/ws?token=' + self.keys[2], timeout=3)
                caps = await gc.rpc(ws, 'groups.capabilities', {})
                if caps.get('driver'):
                    return ws
                await ws.close()
            except (aiohttp.ClientError, OSError, TimeoutError):
                pass
            await asyncio.sleep(.25)
        raise TimeoutError('restarted_host_rpc_not_ready')

    def authority_state(self):
        # New observer installation after process restart is expected. Actual
        # issuance/reservations/fences must be byte-for-byte unchanged.
        a = self.authority()
        return {k: a[k] for k in ('mint_count', 'refresh_count', 'native_mints', 'reservations', 'revocation_fences')}

    async def stable(self, ws):
        state = await gc.rpc(ws, 'groups.state', {'room_id': self.scope['room_id']})
        self.report.setdefault('native_state_readbacks', []).append(state)
        room = state['room']
        # Native link loader includes credentials: hash privately before output.
        route = gc.pf._native_probe(self.home, '''import json,hashlib,dataclasses
from gateway.hosted_room_links import load_room_links_tolerant
from gateway.hosted_rooms import default_db_path
links,errors=load_room_links_tolerant(default_db_path())
raw=json.dumps([dataclasses.asdict(x) for x in links],sort_keys=True,default=str).encode()
print(json.dumps({"links_sha256":hashlib.sha256(raw).hexdigest(),"count":len(links),"errors":errors}))
''')
        status, caps = await self.call('/v1/room-members/capabilities')
        assert status == 200
        return dict(host_installation=gc.pf.native_identity(self.home),
            target_installation=gc.pf.native_identity(self.target),
            host_policy=gc.pf.effective_native_policy(self.home),
            target_policy=gc.pf.effective_native_policy(self.target),
            room={k: room[k] for k in ('room_id', 'authority_gateway_id', 'authority_epoch', 'members')},
            route=route, target_capabilities=caps, target_profile=self.scope['target_profile'])

    async def restart_case(self, case):
        role = 'host' if case == 'host' else 'target'
        room = self.scope['room_id']
        pairs = lambda events: [dict(seq=e['seq'], event_id=e['event_id']) for e in events]
        async with aiohttp.ClientSession(trust_env=False) as session:
            ws = await self.connect(session)
            try:
                stable_before = await self.stable(ws)
                user_id = 'd-' + role + '-' + uuid.uuid4().hex
                sent = await gc.rpc(ws, 'groups.send', dict(room_id=room, event_id=user_id,
                    payload=dict(text='@peer ' + LONG + ' Do not mention any other member.', thread_id='d-' + self.stamp)))
                user = sent['event']
                deadline = time.monotonic() + 45
                matched = []
                while time.monotonic() < deadline:
                    matched = gc.read_db(self.home / 'shared-state.db',
                        'SELECT r.run_id,r.task_id,r.execution_generation,t.payload_json,t.source_event_seq '
                        'FROM hosted_room_remote_runs r JOIN hosted_room_driver_tasks t '
                        'ON r.room_id=t.room_id AND r.task_id=t.task_id WHERE r.room_id=? AND t.source_event_seq=?',
                        (room, user['seq']))
                    if matched: break
                    await asyncio.sleep(.1)
                assert len(matched) == 1, 'fault_send_exact_native_receipt_required'
                admitted = matched[0]
                run, task, generation = admitted['run_id'], admitted['task_id'], admitted['execution_generation']
                key = f'room:{task}:{generation}'
                prompt = json.loads(admitted['payload_json'])['prompt']
                dispatch = dict(protocol_version=2, **self.scope, task_id=task, execution_generation=generation,
                    source_event_seq=admitted['source_event_seq'], cancellation_scope_id='cancel-' + room,
                    prompt=prompt, prompt_digest=hashlib.sha256(prompt.encode()).hexdigest(),
                    capability_digest=self.catalog['catalog_digest'],
                    execution_policy_digest=self.catalog['execution_policy']['policy_digest'], trace_id='trace-' + room)
                body = dict(input=prompt, hosted_room_dispatch=dispatch)
                page = await gc.rpc(ws, 'groups.log', dict(room_id=room, since_seq=0, limit=500))
                assert not page['has_more']
                receipts_before = _receipts(self.target, key)
                authority_before = self.authority_state()
                running = await self.running(run, seconds=45)
                index = 1 if role == 'host' else 0
                old_proc = self.children[index]
                old_record = next(r for p, r in self.owned if p is old_proc)
                assert next(r for r in processes() if r['pid'] == old_proc.pid)['birth'] == old_record['birth']
                self.report['stages'].append('D ' + role + ' RUNNING proven; terminating exact owned gateway')
                self.report['d_progress'] = dict(run_id=run, task_id=task, dispatch_generation=generation,
                    running=running, receipts_before=receipts_before)
                self.flush()
                await asyncio.to_thread(self.fault_process, old_proc, case)
                if role == 'host': await ws.close()
                replacement = self.spawn(role)
                self.children[index] = replacement
                if role == 'host':
                    ws = await self.connect(session)
                else:
                    deadline = time.monotonic() + 50
                    while time.monotonic() < deadline:
                        try:
                            status, _ = await self.call('/v1/room-members/capabilities')
                            if status == 200: break
                        except Exception:
                            pass
                        await asyncio.sleep(.25)
                    else: raise TimeoutError('restarted_target_not_ready')
                stable_after = await self.stable(ws)
                raw_native = []
                firewall = self.transport.target.firewall
                backend = firewall._backend
                def observe_native(request):
                    response = backend(request)
                    original_post = (request.method == 'POST' and request.path == '/v1/runs'
                                     and json.loads(request.body) == body
                                     and dict(request.headers).get('idempotency-key') == key)
                    if request.path == '/v1/runs/'+run or original_post:
                        row = raw_native_projection(request, response)
                        if request.method == 'POST': row['request_run_id'] = run
                        raw_native.append(row)
                    return response
                firewall._backend = observe_native
                observations = []
                deadline = time.monotonic() + 55
                outcome = None
                while time.monotonic() < deadline:
                    status, result = await self.call('/v1/runs/' + run)
                    row = dict(http=status, **{k: result.get(k) for k in ('run_id', 'status', 'execution_generation')})
                    observations.append(row)
                    if status == 200 and result.get('status') in TERMINAL:
                        outcome = row
                        break
                    if status == 404:
                        from gate_h_native import _code
                        outcome = dict(http=status, code=_code(result), classification='lost')
                        break
                    await asyncio.sleep(.3)
                if outcome is None:
                    outcome = dict(classification='indeterminate', observations=observations)
                # Exact original POST and key only. Native durable replay must
                # NOT recreate the lost runtime execution under a new run ID.
                native_before_replay = self.replay_inventory()
                replay_http, replay = await self.call('/v1/runs', 'POST', body, key=key)
                native_after_replay = self.replay_inventory()
                firewall._backend = backend
                self.report['d_raw_restart_readback'] = dict(raw_native=raw_native,
                    native_before_replay=native_before_replay, native_after_replay=native_after_replay,
                    replay_http=replay_http, observed_outcome=outcome)
                self.flush()
                original_page = await gc.rpc(ws, 'groups.log', dict(room_id=room, since_seq=0, limit=500))
                assert not original_page['has_more']
                self.report['d_original_pre_fresh'] = dict(
                    canonical=original_page, db=gc.read_db(self.home/'shared-state.db',
                        'SELECT seq,event_id FROM hosted_room_events WHERE room_id=? ORDER BY seq', (room,)),
                    old_process=dict(old_record), stable_before=stable_before, stable_after=stable_after,
                    authority_before=authority_before, authority_after=self.authority_state(),
                    receipts_before=receipts_before, receipts_after=_receipts(self.target,key),
                    raw_native=raw_native, native_before_replay=native_before_replay, native_after_replay=native_after_replay)
                self.flush()
                fresh = await self.fresh_control(ws, task, generation) if role == 'target' else None
                if (case == 'target_graceful_restart' and outcome.get('status') == 'cancelled'
                        and fresh.get('status') == 'not_completed' and fresh.get('observed_seconds', 0) >= 55
                        and fresh.get('driver_tasks') == []
                        and fresh.get('original_driver') == [dict(task_id=task, status='running', execution_generation=generation)]):
                    # Only this exact wedge may use the known core limitation
                    # label. The final predicate still verifies raw/canonical evidence.
                    fresh['status'] = 'blocked_core_cancelled_history'
                # Give native host reconciliation a bounded window. Do not retry
                # driver work, re-register routes, mint, or replace any identity.
                deadline = time.monotonic() + 12
                while True:
                    final_page = await gc.rpc(ws, 'groups.log', dict(room_id=room, since_seq=0, limit=500))
                    reply = [e for e in final_page['events'] if e['kind'] == 'message.member' and e['payload'].get('task_id') == task]
                    if reply or time.monotonic() >= deadline: break
                    await asyncio.sleep(.3)
                assert not final_page['has_more']
                self.report['d_canonical_events'] = final_page
                new_record = next(r for p, r in self.owned if p is replacement)
                evidence = dict(role=role, case=case, running=running, run_id=run, task_id=task, dispatch_generation=generation,
                    old_process=dict(old_record), new_process=dict(new_record, alive=replacement.poll() is None),
                    stable_before=stable_before, stable_after=stable_after,
                    authority_before=authority_before, authority_after=self.authority_state(),
                    receipts_before=receipts_before, receipts_after=_receipts(self.target, key),
                    idempotency_key=key, original_request_sha256=hashlib.sha256(json.dumps(body,sort_keys=True).encode()).hexdigest(),
                    replay_http=replay_http, replay=replay, observed_outcome=outcome, outcome_observations=observations,
                    before=pairs(page['events']), canonical=pairs(final_page['events']),
                    db=gc.read_db(self.home/'shared-state.db', 'SELECT seq,event_id FROM hosted_room_events WHERE room_id=? ORDER BY seq', (room,)),
                    user=dict(seq=user['seq'], event_id=user['event_id']),
                    driver_tasks=gc.read_db(self.home/'shared-state.db', 'SELECT task_id,execution_generation FROM hosted_room_driver_tasks WHERE room_id=? AND source_event_seq=? ORDER BY task_id', (room,user['seq'])),
                    canonical_reply_count=len(reply), canonical_events=final_page['events'],
                    raw_native=raw_native, native_before_replay=native_before_replay, native_after_replay=native_after_replay,
                    fresh=fresh, outcome_class=('blocked_core_cancelled_history'
                        if case == 'target_graceful_restart' and outcome.get('status') == 'cancelled'
                        and fresh.get('status') == 'blocked_core_cancelled_history' else
                        'completed' if outcome.get('status') == 'completed' else
                        'lost' if outcome.get('classification') == 'lost' else 'indeterminate'),
                    canonical_publication='reply_observed' if reply else 'not_observed_within_bound')
                # SQLite tuples serialize to lists; predicate uses the wire form.
                evidence = json.loads(json.dumps(evidence))
                self.report['d_evidence'] = evidence
                self.check(CASE_CHECKS[case][0], valid_restart(evidence), evidence=evidence)
                await gc.rpc(ws, 'groups.stop', dict(room_id=room, cancel_id='d-cleanup-' + self.stamp))
            finally:
                await ws.close()
