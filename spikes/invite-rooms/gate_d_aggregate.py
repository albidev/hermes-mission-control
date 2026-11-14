"""One owned D aggregate: serial fresh homes, exact source and report hashes."""
import hashlib
import json
import os
from pathlib import Path
import sys
import uuid
from gate_c_aggregate import controller_session, run_owned_child, persist
from gate_s1_native import ROOT, PY, HERE, snapshot, gc
from gate_d_runtime import CASE_CHECKS, valid_restart


def validate_cases(reports, receipts, source, end):
    errors = []
    if [r.get('d_case') for r in reports] != list(CASE_CHECKS): errors.append('missing_or_reordered_case')
    if [r.get('case') for r in receipts] != list(CASE_CHECKS): errors.append('missing_receipt')
    if end != source: errors.append('final_source_drift')
    covered = []
    for report, receipt in zip(reports, receipts):
        case = report.get('d_case')
        if case not in CASE_CHECKS:
            errors.append('unknown_case')
            continue
        if report.get('source_start') != source or report.get('source_end') != source:
            errors.append(case + ':source_drift')
        if (report.get('packet_complete') is not True or report.get('teardown_complete') is not True
            or report.get('cleanup_failures') != [] or report.get('owned_group_survivors') != []
            or report.get('ports_free_after') != {'18790': True, '18791': True}):
            errors.append(case + ':case_or_cleanup_incomplete')
        if receipt.get('exit_code') != 0 or not 0 < receipt.get('wall_seconds', 0) <= 360:
            errors.append(case + ':child_exit_or_bound')
        try:
            raw_valid = (report.get('d_evidence', {}).get('case') == case
                         and valid_restart(report.get('d_evidence', {})))
        except (KeyError, TypeError, IndexError):
            raw_valid = False
        if not raw_valid:
            errors.append(case + ':invalid_raw_restart_evidence')
        for name in CASE_CHECKS[case]:
            if report.get('checks', {}).get(name, {}).get('ok') is True: covered.append(name)
            else: errors.append(case + ':missing_predicate:' + name)
        rows = report.get('lab_model_response_observations', [])
        override = {'chat_template_kwargs': {'enable_thinking': False}}
        if not rows or report.get('lab_model_request_overrides') != override or not all(
            r.get('reasoning_chars') == 0 and r.get('reasoning_tokens') in (None, 0)
            and r.get('visible_chars', 0) > 0 and r.get('request_extra_body') == override for r in rows):
            errors.append(case + ':model_observation_missing')
    required = [n for names in CASE_CHECKS.values() for n in names]
    if covered != required: errors.append('required_coverage_incomplete')
    limitations = [dict(code='cancelled_history_omits_terminal', case=r.get('d_case'),
        outcome_class='blocked_core_cancelled_history', fresh_send_completed=False,
        upstream_fix_owner='Albi') for r in reports
        if r.get('d_evidence', {}).get('outcome_class') == 'blocked_core_cancelled_history']
    return dict(packet_complete=not errors, matrix_complete=False, errors=errors,
        known_core_limitations=limitations,
        outcome_classes={r.get('d_case'): r.get('d_evidence', {}).get('outcome_class') for r in reports},
        required_predicates=required, covered_predicates=covered, source_start=source, source_end=end)


def aggregate(out, meta, stop):
    source = snapshot()
    persist(out/'frozen-source.json', source)
    meta['source_invocation_start'] = source
    loaded = []
    for case in CASE_CHECKS:
        if snapshot() != source: raise RuntimeError('source_drift_before_case')
        if not all(gc.pf.check_port_available(p)['available'] for p in (18790,18791)):
            raise RuntimeError('port_busy_before_case')
        env = os.environ.copy()
        env.pop('HERMES_PROFILE', None)
        env.pop('MC_ROOM_READ_ALLOWLIST', None)
        env.update(S1_NATIVE_D_PACKET='1', S1_NATIVE_D_CASE=case, TMPDIR=str(ROOT), PYTHONDONTWRITEBYTECODE='1')
        argv = [str(PY), '-B', '-m', 'unittest', 'discover', '-s', str(HERE/'tests'), '-p', 'test_s1_gateway_native.py', '-v']
        receipt = run_owned_child(argv, case, out, meta, stop, env=env, cwd=gc.REPO)
        paths = [line.split('=',1)[1] for line in Path(receipt['log_path']).read_text().splitlines()
                 if line.startswith('D_GATEWAY_REPORT=')]
        if len(paths) != 1: raise RuntimeError('one_case_report_required')
        path = Path(paths[0]).resolve()
        if not path.is_relative_to(ROOT.resolve()) or any(r.get('path') == str(path) for r in meta['cases'][:-1]):
            raise RuntimeError('invalid_report_path')
        data = path.read_bytes()
        receipt.update(path=str(path), sha256=hashlib.sha256(data).hexdigest())
        report = json.loads(data)
        loaded.append(report)
        persist(out/(case+'-exit.json'), receipt)
        persist(out/'report.json',meta)
        if receipt['exit_code'] != 0 or not report.get('packet_complete'): break
    for receipt in meta['cases']:
        if hashlib.sha256(Path(receipt['path']).read_bytes()).hexdigest() != receipt['sha256']:
            raise RuntimeError('report_hash_drift')
    meta.update(validate_cases(loaded,meta['cases'],source,snapshot()))
    print('D_AGGREGATE_COMPLETE=' + str(meta['packet_complete']),flush=True)
    return 0 if meta['packet_complete'] else 1


def main():
    out = ROOT/'evidence'/'s1-resume'/('d-native-agg-' + uuid.uuid4().hex[:12])
    out.mkdir(mode=0o700)
    print('D_AGGREGATE_REPORT=' + str(out/'report.json'),flush=True)
    return controller_session(out,lambda meta,stop: aggregate(out,meta,stop))


if __name__ == '__main__': raise SystemExit(main())
