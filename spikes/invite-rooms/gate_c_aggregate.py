"""Single serial C invocation. Only fresh reports created by this invocation qualify."""
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / 'tests'))
from test_s1_relay_native import CASE_CHECKS, required_checks
from gate_s1_native import ROOT, PY, snapshot, processes, gc


def validate_cases(reports, receipts, end):
    source = reports[0].get('source_start') if reports else None
    errors = []
    expected = list(CASE_CHECKS)
    if [r.get('c_case') for r in reports] != expected:
        errors.append('case_order_or_missing')
    if [r.get('case') for r in receipts] != expected:
        errors.append('receipt_order_or_missing')
    if source is None or end != source:
        errors.append('source_drift_at_end')
    covered = []
    override = {'chat_template_kwargs': {'enable_thinking': False}}
    for i, report in enumerate(reports):
        case = report.get('c_case')
        if case not in CASE_CHECKS:
            errors.append('unknown_case')
            continue
        if report.get('source_start') != source or report.get('source_end') != source:
            errors.append(case + ':source_drift')
        if (report.get('packet_complete') is not True or report.get('teardown_complete') is not True
                or report.get('cleanup_failures') != [] or report.get('owned_group_survivors') != []
                or report.get('ports_free_after') != {'18790': True, '18791': True}):
            errors.append(case + ':incomplete_cleanup_or_case')
        rows = report.get('lab_model_response_observations', [])
        if (report.get('lab_model_request_overrides') != override or not rows or not all(
                r.get('reasoning_chars') == 0 and r.get('reasoning_tokens') in (None, 0)
                and r.get('visible_chars', 0) > 0 and r.get('request_extra_body') == override for r in rows)):
            errors.append(case + ':model_observation')
        receipt = receipts[i] if i < len(receipts) else {}
        if receipt.get('exit_code') != 0 or not 0 < receipt.get('wall_seconds', 0) <= 360:
            errors.append(case + ':exit_or_bound')
        for name in CASE_CHECKS[case]:
            if report.get('checks', {}).get(name, {}).get('ok') is True:
                covered.append(name)
            else:
                errors.append(case + ':missing_or_false:' + name)
    if set(covered) != set(required_checks('all')):
        errors.append('required_coverage_incomplete')
    return dict(packet_complete=not errors, matrix_complete=False, errors=errors,
                covered_predicates=covered, required_predicates=required_checks('all'),
                source_start=source, source_end=end)


def persist(path, data):
    """Atomic, fsynced receipts stay inside the approved evidence directory."""
    tmp = path.with_suffix('.writing')
    with tmp.open('w') as stream:
        json.dump(data, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)


class ChildStopped(Exception):
    """Deadline or requested interruption: never launch the next case."""


def run_owned_child(argv, case, out, meta, stop, *, env=None, cwd=None,
                    timeout=295, cleanup_timeout=60):
    if stop.get('signal'):
        raise ChildStopped('controller_interrupted_before_spawn')
    t0 = time.monotonic()
    receipt = dict(case=case, argv=argv, requested_signal=None, outcome='running',
                   log_path=str(out / (case + '.log')))
    meta.setdefault('cases', []).append(receipt)
    proc = None
    identity = None
    try:
        with Path(receipt['log_path']).open('w') as log:
            # Handler only sets a flag, so signals during Popen cannot lose ownership.
            proc = subprocess.Popen(argv, cwd=cwd, env=env, stdout=log,
                                    stderr=subprocess.STDOUT, start_new_session=True)
            identity = next(r for r in processes() if r['pid'] == proc.pid)
            if identity['pgid'] != proc.pid:
                raise RuntimeError('child_not_session_leader')
            receipt['identity'] = identity
            persist(out / (case + '-start.json'), receipt)
            deadline = t0 + timeout
            while proc.poll() is None and not stop.get('signal') and time.monotonic() < deadline:
                time.sleep(.05)
            if proc.poll() is None:
                sig = stop.get('signal') or signal.SIGTERM
                receipt['requested_signal'] = signal.Signals(sig).name
                current = next((r for r in processes() if r['pid'] == proc.pid), None)
                if current != identity:
                    raise RuntimeError('child_identity_changed')
                os.killpg(identity['pgid'], sig)
                try:
                    proc.wait(timeout=cleanup_timeout)
                    receipt['outcome'] = 'terminated_on_request'
                except subprocess.TimeoutExpired:
                    # Verify birth as well as PID/PGID immediately before escalation.
                    current = next((r for r in processes() if r['pid'] == proc.pid), None)
                    if current != identity:
                        raise RuntimeError('child_identity_changed_before_kill')
                    os.killpg(identity['pgid'], signal.SIGKILL)
                    proc.wait(timeout=5)
                    receipt['outcome'] = 'killed'
            else:
                proc.wait()
                receipt['outcome'] = 'exited'
    finally:
        if proc is not None:
            if proc.poll() is None:
                current = next((r for r in processes() if r['pid'] == proc.pid), None)
                if identity is not None and current == identity:
                    os.killpg(identity['pgid'], signal.SIGKILL)
                    proc.wait(timeout=5)
                    receipt['outcome'] = 'killed'
            receipt['exit_code'] = proc.returncode
            receipt['owned_group_survivors'] = [r for r in processes()
                if r['pgid'] == (identity or {}).get('pgid')]
        receipt['wall_seconds'] = time.monotonic() - t0
        persist(out / (case + '-exit.json'), receipt)
        persist(out / 'report.json', meta)
    if (receipt['outcome'] != 'exited' or receipt.get('owned_group_survivors')
            or stop.get('signal')):
        raise ChildStopped(receipt['outcome'])
    return receipt


def controller_session(out, action):
    """Own-session controller envelope; signal flags are forwarded by child wait."""
    identity = next(r for r in processes() if r['pid'] == os.getpid())
    identity['sid'] = os.getsid(0)
    if identity['pid'] != identity['pgid'] or identity['pid'] != identity['sid']:
        raise RuntimeError('controller_requires_own_session')
    persist(out / 'controller-start.json', identity)
    meta = dict(packet_complete=False, matrix_complete=False, cases=[], controller=identity)
    stop, previous = {}, {}
    def interrupted(signum, frame):
        stop.setdefault('signal', signum)
    code = 1
    try:
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous[sig] = signal.signal(sig, interrupted)
        persist(out / 'report.json', meta)
        code = action(meta, stop)
    except BaseException as exc:
        meta['controller_error'] = type(exc).__name__
        code = 1
    finally:
        if stop.get('signal'):
            meta['requested_signal'] = signal.Signals(stop['signal']).name
            code = 128 + stop['signal']
        if code != 0:
            meta['packet_complete'] = False
        meta['controller_exit_code'] = code
        persist(out / 'report.json', meta)
        persist(out / 'controller-exit.json', dict(identity=identity, exit_code=code,
            requested_signal=meta.get('requested_signal'), packet_complete=meta['packet_complete'],
            cases_finished=len(meta['cases'])))
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return code


def aggregate_cases(out, meta, stop):
    reports = []
    started = snapshot()
    meta.update(case_order=list(CASE_CHECKS), source_invocation_start=started)
    persist(out / 'frozen-source.json', started)
    for case in CASE_CHECKS:
        if snapshot() != started:
            meta['controller_error'] = 'source_drift_before_case'
            break
        if not all(gc.pf.check_port_available(p)['available'] for p in (18790, 18791)):
            meta['controller_error'] = 'port_busy_before_case'
            break
        env = os.environ.copy()
        env.pop('HERMES_PROFILE', None)
        env.pop('MC_ROOM_READ_ALLOWLIST', None)
        env.update(S1_NATIVE_C_PACKET='1', S1_NATIVE_C_CASE=case,
                   TMPDIR=str(ROOT), PYTHONDONTWRITEBYTECODE='1')
        argv = [str(PY), '-B', '-m', 'unittest', 'discover', '-s', str(HERE / 'tests'),
                '-p', 'test_s1_relay_native.py', '-v']
        receipt = run_owned_child(argv, case, out, meta, stop, cwd=gc.REPO, env=env)
        paths = [line.split('=', 1)[1] for line in Path(receipt['log_path']).read_text().splitlines()
                 if line.startswith('C_RELAY_RESTART_REPORT=')]
        if len(paths) == 1:
            path = Path(paths[0]).resolve()
            if (not path.is_relative_to(ROOT.resolve())
                    or path.parent.name in {Path(r.get('path','')).parent.name for r in meta['cases'][:-1]}):
                raise RuntimeError('invalid_or_reused_report')
            data = path.read_bytes()
            report = json.loads(data)
            receipt.update(path=str(path), sha256=hashlib.sha256(data).hexdigest())
            reports.append(report)
        persist(out / (case + '-exit.json'), receipt)
        persist(out / 'report.json', meta)
        if receipt['exit_code'] != 0 or len(paths) != 1 or not reports[-1].get('packet_complete'):
            break
    loaded = []
    for receipt in meta['cases']:
        if 'path' not in receipt:
            continue
        data = Path(receipt['path']).read_bytes()
        if hashlib.sha256(data).hexdigest() != receipt['sha256']:
            meta['controller_error'] = 'case_report_hash_drift'
            break
        loaded.append(json.loads(data))
    meta.update(validate_cases(loaded, meta['cases'], snapshot()))
    if meta['source_end'] != started:
        meta['controller_error'] = 'invocation_source_drift'
    if meta.get('controller_error'):
        meta['packet_complete'] = False
    print('C_AGGREGATE_COMPLETE=' + str(meta['packet_complete']), flush=True)
    return 0 if meta['packet_complete'] else 1


def main():
    out = ROOT / 'evidence' / 's1-resume' / ('c-native-agg-' + uuid.uuid4().hex[:12])
    out.mkdir(mode=0o700)
    print('C_AGGREGATE_REPORT=' + str(out / 'report.json'), flush=True)
    return controller_session(out, lambda meta, stop: aggregate_cases(out, meta, stop))


if __name__ == '__main__':
    raise SystemExit(main())
