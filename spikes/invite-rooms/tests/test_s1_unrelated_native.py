"""Explicit opt-in serial native RED repro. Not part of ordinary unit discovery."""
import asyncio
import os
from pathlib import Path
import sys
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gate_s1_native import NativeLab
from gate_h_native import _call, _dispatch, _wait_terminal

@unittest.skipUnless(os.environ.get('S1_NATIVE_SCOPE_REPRO') == '1', 'explicit serial native lab only')
class UnrelatedNativeScopeTests(unittest.TestCase):
    def test_unrelated_real_native_run_rejected_before_backend_forward(self):
        lab = NativeLab()
        try:
            lab.setup()
            asyncio.run(self.exercise(lab))
        finally:
            lab.close()
            lab.flush()
            print('UNRELATED_NATIVE_SCOPE_REPORT=' + str(lab.out / 'report.json'), flush=True)

    async def exercise(self, lab):
        await lab.baseline()
        scope = dict(lab.scope, room_id='unrelated-' + uuid.uuid4().hex)
        invitation = await lab.mint(scope=scope)
        task = 'unrelated-' + uuid.uuid4().hex[:10]
        key = f'room:{task}:1'
        base = 'http://127.0.0.1:18791'
        body = _dispatch(scope, invitation['catalog'], 'Reply only UNRELATED.', task)
        status, admitted = await asyncio.to_thread(_call, base + '/v1/runs', 'POST', body,
                                                   invitation['grant'], 'HermesRoom', key)
        lab.check('unrelated_real_native_admitted', status in (200, 202) and bool(admitted.get('run_id')),
                  http=status, run_id=admitted.get('run_id'), task_id=task,
                  dispatch_generation=body['hosted_room_dispatch']['execution_generation'], idempotency_key=key)
        run = admitted['run_id']
        final, states = await _wait_terminal(base, invitation['grant'], run, 150)
        lab.check('unrelated_real_native_completed', final == 'completed', final=final, states=states)
        from room_firewall import Request, ForwardedRequest, FirewallDenied
        from run_bindings import HostRunBindings, NativeReceiptVerifier, scope_digest
        host = lab.transport.host_firewall
        bindings = HostRunBindings(lab.home / 'room-run-bindings.db')
        owner_run = lab.report['host_receipts'][0]['run_id']
        lab.check('owner_native_and_host_persisted_bindings', bindings(owner_run, lab.scope)
                  and NativeReceiptVerifier(lab.target / 'runs_idempotency.db')(owner_run, lab.scope),
                  run_id=owner_run, scope_digest=scope_digest(lab.scope))
        firewall = lab.transport.target.firewall
        real = firewall._backend
        forwarded = []
        def observe(request):
            forwarded.append({'method': request.method, 'path': request.path})
            return real(request)
        firewall._backend = observe
        try:
            for action, method, suffix, body_bytes in [('status','GET','',b''),('stop','POST','/stop',b'{}')]:
                path = '/v1/runs/' + run + suffix
                before_served = lab.transport.target.served
                status, _ = await lab.call(path, method, {} if method == 'POST' else None)
                lab.check('unrelated_host_' + action + '_pre_forward', status == 403
                          and lab.transport.target.served == before_served and not forwarded,
                          http=status, target_stream_delta=lab.transport.target.served-before_served,
                          native_backend_calls=len(forwarded), run_id=run)
                request = ForwardedRequest(method, path, (('authorization','HermesRoom '+lab.grant),), body_bytes)
                response = await asyncio.to_thread(host._backend, request)
                lab.check('unrelated_target_' + action + '_pre_forward', response.status == 403 and not forwarded,
                          http=response.status, native_backend_calls=len(forwarded), run_id=run)
        finally:
            firewall._backend = real
        # Admission reply lost AFTER actual native reservation. Only the immutable
        # same-key retry may produce the host binding, from a genuine replay ack.
        lost = []
        def drop(request):
            result = real(request)
            if request.path == '/v1/runs' and not lost:
                lost.append(result.status)
                raise ConnectionResetError('bounded_lost_ack')
            return result
        task = 'binding-lost-' + uuid.uuid4().hex[:10]
        key = f'room:{task}:1'
        body = _dispatch(lab.scope, lab.catalog, 'Reply only BINDING_OK.', task)
        firewall._backend = drop
        try:
            first, _ = await lab.call('/v1/runs', 'POST', body, key=key)
        finally:
            firewall._backend = real
        rows = __import__('gate_g_native')._receipts(lab.target, key)
        actual_run = rows[0][0]
        lab.check('lost_ack_not_host_enrolled', first >= 500 and not bindings(actual_run, lab.scope), http=first, run_id=actual_run)
        status, replay = await lab.call('/v1/runs', 'POST', body, key=key)
        lab.check('lost_ack_retry_same_run_enrolled', status in (200,202) and replay.get('replayed') is True
                  and replay.get('run_id') == actual_run and bindings(actual_run, lab.scope)
                  and __import__('gate_g_native')._receipts(lab.target,key) == rows and len(rows)==1,
                  http=status, run_id=actual_run, dispatch_task_id=task, dispatch_generation=1, idempotency_key=key, receipts=rows)
        status, stopped = await lab.call('/v1/runs/'+actual_run+'/stop', 'POST', {})
        lab.check('legitimate_replayed_stop', status == 200, http=status, run_id=actual_run)
        status, runtime = await lab.call('/v1/runs/'+actual_run)
        lab.check('native_runtime_coordinate_readback', status == 200 and runtime.get('run_id') == actual_run,
                  http=status, run_id=actual_run, dispatch_task_id=task, dispatch_generation=1,
                  idempotency_key=key, native_runtime_execution_generation=runtime.get('execution_generation'))
        final, states = await _wait_terminal(lab.adapter,lab.grant,actual_run,150)
        lab.check('legitimate_replayed_status', final in {'completed','interrupted','cancelled'}, final=final, states=states)
        lab.report['matrix_complete'] = False
        lab.report['packet_scope'] = 'B run binding only; lifecycle lost refresh/revoke and C/D/E incomplete'


if __name__ == '__main__':
    unittest.main()
