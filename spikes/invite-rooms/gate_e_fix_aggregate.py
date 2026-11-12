"""One owned serial current-source B/C/E-2 packet. Never full S1 acceptance."""
import hashlib
import json
import os
from pathlib import Path
import sys
import uuid
from gate_c_aggregate import controller_session, run_owned_child, persist, validate_cases as validate_c
from gate_s1_native import ROOT, PY, HERE, snapshot, gc, valid_e_canonical

CASES = [('B', 'test_s1_lifecycle_native.py', 'B_LIFECYCLE_REPORT=',
          {'S1_NATIVE_B_PACKET': '1'}, 900)] + [
    ('C-' + c, 'test_s1_relay_native.py', 'C_RELAY_RESTART_REPORT=',
     {'S1_NATIVE_C_PACKET': '1', 'S1_NATIVE_C_CASE': c}, 295)
    for c in ('canonical', 'relay', 'host', 'target', 'bounds')] + [
    ('E2', 'test_s1_target_canonical_native.py', 'E2_CANONICAL_REPORT=',
     {'S1_NATIVE_E2_PACKET': '1'}, 295)]


def validate(reports, receipts, source, end):
    errors = []
    if [r['case'] for r in receipts] != [c[0] for c in CASES] or len(reports) != len(CASES):
        errors.append('missing_or_reordered_cases')
    if source != end: errors.append('source_drift')
    for report, receipt in zip(reports, receipts):
        case = receipt['case']
        if report.get('source_start') != source or report.get('source_end') != source:
            errors.append(case + ':source_drift')
        if (report.get('packet_complete') is not True or report.get('teardown_complete') is not True
            or report.get('cleanup_failures') != [] or report.get('owned_group_survivors') != []
            or report.get('ports_free_after') != {'18790': True, '18791': True}
            or not report.get('checks') or not all(c.get('ok') is True for c in report['checks'].values())):
            errors.append(case + ':case_or_cleanup_incomplete')
        bound = 1000 if case == 'B' else 360
        if receipt.get('exit_code') != 0 or not 0 < receipt.get('wall_seconds', 0) <= bound:
            errors.append(case + ':exit_or_bound')
    if len(reports) == len(CASES):
        b = reports[0]
        if not b.get('b_exercise_complete') or not all(b['checks'].get(n, {}).get('ok') is True for n in (
            'lost_refresh_reply_native_fence_readback', 'lost_revoke_reply_native_fence_readback',
            'native_no_mint_or_authority_change_on_reconnect', 'status_hard_horizon',
            'refresh_revoke_race_native_fence', 'revoke_is_not_stop', 'lost_revoke_is_not_stop',
            'canonical_exact_ids_order_after_B')):
            errors.append('B:required_coverage')
        c_receipts = [dict(r, case=r['case'][2:]) for r in receipts[1:6]]
        c = validate_c(reports[1:6], c_receipts, end)
        errors.extend('C:' + e for e in c['errors'])
        if not valid_e_canonical(reports[-1].get('e2_evidence', {})):
            errors.append('E2:invalid_raw_canonical_evidence')
    return dict(packet_complete=not errors, matrix_complete=False, errors=errors,
                source_start=source, source_end=end)


def aggregate(out, meta, stop):
    source = snapshot()
    persist(out/'frozen-source.json', source)
    meta.update(source_invocation_start=source, case_order=[c[0] for c in CASES])
    reports = []
    for case, pattern, marker, variables, timeout in CASES:
        if snapshot() != source: raise RuntimeError('source_drift_before_case')
        if not all(gc.pf.check_port_available(p)['available'] for p in (18790, 18791)):
            raise RuntimeError('port_busy_before_case')
        env = os.environ.copy()
        for key in ('HERMES_PROFILE', 'MC_ROOM_READ_ALLOWLIST', 'S1_NATIVE_B_PACKET',
                    'S1_NATIVE_C_PACKET', 'S1_NATIVE_D_PACKET', 'S1_NATIVE_E2_PACKET'):
            env.pop(key, None)
        env.update(variables, TMPDIR=str(ROOT), PYTHONDONTWRITEBYTECODE='1')
        argv = [str(PY), '-B', '-m', 'unittest', 'discover', '-s', str(HERE/'tests'), '-p', pattern, '-v']
        print('START_CASE=' + case, flush=True)
        receipt = run_owned_child(argv, case, out, meta, stop, env=env, cwd=gc.REPO,
                                  timeout=timeout, cleanup_timeout=90 if case == 'B' else 60)
        paths = [line.split('=', 1)[1] for line in Path(receipt['log_path']).read_text().splitlines()
                 if line.startswith(marker)]
        if len(paths) != 1: raise RuntimeError('one_fresh_report_required')
        path = Path(paths[0]).resolve()
        if not path.is_relative_to(ROOT.resolve()) or any(r.get('path') == str(path) for r in meta['cases'][:-1]):
            raise RuntimeError('invalid_or_reused_report')
        data = path.read_bytes()
        receipt.update(path=str(path), sha256=hashlib.sha256(data).hexdigest())
        reports.append(json.loads(data))
        persist(out/(case+'-exit.json'), receipt)
        persist(out/'report.json', meta)
        print('FINISH_CASE=' + case + ':exit=' + str(receipt['exit_code']), flush=True)
        if receipt['exit_code'] != 0 or not reports[-1].get('packet_complete'): break
    for receipt in meta['cases']:
        if hashlib.sha256(Path(receipt['path']).read_bytes()).hexdigest() != receipt['sha256']:
            raise RuntimeError('case_report_hash_drift')
    meta.update(validate(reports, meta['cases'], source, snapshot()))
    meta['ports_free_after'] = {str(p): gc.pf.check_port_available(p)['available'] for p in (18790, 18791)}
    if not all(meta['ports_free_after'].values()): meta['packet_complete'] = False
    return 0 if meta['packet_complete'] else 1


def main():
    out = ROOT/'evidence'/'s1-resume'/('e-fix-native-agg-' + uuid.uuid4().hex[:12])
    out.mkdir(mode=0o700)
    print('E_FIX_AGGREGATE_REPORT=' + str(out/'report.json'), flush=True)
    return controller_session(out, lambda meta, stop: aggregate(out, meta, stop))


if __name__ == '__main__': raise SystemExit(main())
