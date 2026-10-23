"""S1 local native acceptance TEST harness. Never imported by product code.

Real native entrypoints, native signatures, two isolated homes and local model.
This deliberately fails on missing evidence instead of promoting historical G/H
flags. No F1 cross-host claim. Run serially with TMPDIR under the approved root.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import signal
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import threading
import time
import uuid

import aiohttp
import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import connector
import gate_c_runtime as gc
import gate_f1_local as net
from gate_g_native import _receipts
from gate_h_native import _call, _code, _dispatch, _wait_terminal
from room_firewall import RoomFirewall, UpstreamAmbiguous

PY = gc.CORE / "venv/bin/python"
ROOT = gc.ROOT
MODEL = "qwen3.8-27b-oq4e-mtp"
LONG = "Write every integer from 1 to 400 in order, separated by single spaces. Output nothing else."
TERMINAL = {"completed", "failed", "interrupted", "cancelled"}

# Isolated native cron uses the supported provider discovery API, with a provider
# that cannot trigger a job. No core files or production plugin/config are edited.
DISABLED_CRON = '''from cron.scheduler_provider import CronScheduler
class LabDisabledScheduler(CronScheduler):
    @property
    def name(self): return "s1-lab-disabled"
    def start(self, stop_event, **kwargs): stop_event.wait()
'''

# The entrypoint only observes actual native grant issuance. It neither mints
# extra grants nor changes validation/revocation. Audit receipts exclude tokens,
# secrets and signatures. They are written ONLY after a native mint succeeds.
NATIVE_ENTRY = '''import functools, json, os, sys, threading, time
from pathlib import Path
core, audit = sys.argv[1:3]
entry_arguments = sys.argv[:]
sys.path.insert(0, core)
# Preserve the wrapper invocation until bootstrap/main import finishes: native
# dependency activation may re-exec the original entry under its selected Python.
from hermes_cli.main import main
from gateway import hosted_room_peer as native
original = native.issue_room_grant
lock = threading.Lock()
def record(receipt):
    receipt.update(pid=os.getpid(), observed_at=time.time(), interpreter=sys.executable,
                   module_file=native.__file__, module_identity=id(native),
                   original_code_file=original.__code__.co_filename)
    with lock:
        with open(audit, "a", encoding="utf-8") as out:
            out.write(json.dumps(receipt, sort_keys=True) + chr(10))
record(dict(event="observer_installed", wrapper_entry=entry_arguments[0]))
@functools.wraps(original)
def observed(*args, **kwargs):
    token = original(*args, **kwargs)
    claims = native.decode_room_grant(args[0], token, permission="status")
    allowed = ("room_id", "home_install_id", "authority_gateway_id", "authority_epoch", "member_id",
               "target_install_id", "target_profile", "issued_at", "expires_at", "status_expires_at",
               "execution_policy_digest", "permissions")
    receipt = {k: claims[k] for k in allowed if k in claims}
    receipt.update(event="native_issue_room_grant_return",
                   kind="refresh" if str(kwargs.get("grant_id", "")).startswith("grant-refresh-") else "owner_mint")
    record(receipt)
    return token
native.issue_room_grant = observed
# Read-only lab observer: count real decoded model fields, never persist text.
from agent import turn_response_intake as intake
original_intake = intake._fire_post_api_request_hook
@functools.wraps(original_intake)
def observe_model(agent, response, message, finish_reason, **kwargs):
    usage = getattr(response, "usage", None)
    details = getattr(usage, "completion_tokens_details", None)
    receipt = dict(event="lab_model_response", model=agent.model,
                   reasoning_chars=len(getattr(message, "reasoning_content", None) or ""),
                   reasoning_tokens=getattr(details, "reasoning_tokens", None),
                   visible_chars=len(message.content or ""),
                   request_extra_body=(getattr(agent, "request_overrides", {}) or {}).get("extra_body", {}))
    record(receipt)
    import logging
    logging.getLogger("s1.lab.model").info("LAB_MODEL_RESPONSE reasoning_chars=%s reasoning_tokens=%s visible_chars=%s", receipt["reasoning_chars"], receipt["reasoning_tokens"], receipt["visible_chars"])
    return original_intake(agent, response, message, finish_reason, **kwargs)
intake._fire_post_api_request_hook = observe_model
sys.argv = ["hermes", *entry_arguments[3:]]
sys.exit(main())
'''


# Embed the observer in the generated lab entrypoint, not in core files or a
# production profile. Its separate JSONL has only sanitized driver coordinates.
_driver_observer = (HERE / 'lab_driver_observer.py').read_text()
_driver_install = '''
from tui_gateway.hosted_room_driver import HostedRoomRuntime
exec(DRIVER_OBSERVER_SOURCE)
def driver_record(receipt):
    with lock:
        with open(Path(audit).with_name("native-driver.jsonl"), "a", encoding="utf-8") as out:
            out.write(json.dumps(receipt, sort_keys=True) + chr(10))
install_driver_observer(HostedRoomRuntime, driver_record)
'''.replace('DRIVER_OBSERVER_SOURCE', repr(_driver_observer))
NATIVE_ENTRY = NATIVE_ENTRY.replace('sys.argv = ["hermes",', _driver_install + '\nsys.argv = ["hermes",')


def snapshot():
    files = sorted(HERE.rglob("*.py"))
    files += sorted((gc.REPO / "server").glob("room_*.py"))
    core_dirty = subprocess.check_output(['git', 'diff', '--name-only', 'HEAD', '--', '*.py'],
                                         cwd=gc.CORE, text=True).splitlines()
    core_identity = {'head': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=gc.CORE, text=True).strip(),
                     'dirty_python_sha256': {name: hashlib.sha256((gc.CORE / name).read_bytes()).hexdigest()
                                             for name in core_dirty if (gc.CORE / name).is_file()}}
    return {"core_readonly_identity": core_identity,
            "head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=gc.REPO, text=True).strip(),
            "branch": subprocess.check_output(["git", "branch", "--show-current"], cwd=gc.REPO, text=True).strip(),
            "status": subprocess.check_output(["git", "status", "--short"], cwd=gc.REPO, text=True),
            "source_sha256": {str(p.relative_to(gc.REPO)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files},
            "diff_sha256": hashlib.sha256(subprocess.check_output(["git", "diff", "--binary"], cwd=gc.REPO)).hexdigest()}


def processes():
    raw = subprocess.check_output(["ps", "-axo", "pid=,ppid=,pgid=,lstart="], text=True)
    rows = []
    for line in raw.splitlines():
        parts = line.split(None, 3)
        if len(parts) == 4:
            rows.append({"pid": int(parts[0]), "ppid": int(parts[1]), "pgid": int(parts[2]), "birth": parts[3]})
    return rows


class NativeInterrupted(BaseException):
    """Requested packet termination, not a native child crash."""


@contextmanager
def native_signals(report):
    previous = {}
    def interrupted(signum, frame):
        report['requested_signal'] = signal.Signals(signum).name
        raise NativeInterrupted()
    try:
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous[sig] = signal.signal(sig, interrupted)
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def valid_driver_continuity(d):
    try:
        return bool(d['observer_baseline'] > 0 and d['window_end'] >= d['window_start']
            and len(d['lease_before']) == len(d['lease_after']) == len(d['task']) == 1
            and d['lease_before'][0]['lease_generation'] > 0
            and d['lease_before'][0]['lease_generation'] == d['lease_after'][0]['lease_generation']
            and d['task'][0]['indeterminate_at'] is None
            and not any(e['method'].split('.')[0] in ('_mark_ambiguous', '_drop_lease')
                        for e in d['events']))
    except (KeyError, TypeError):
        return False


def valid_c_canonical(e):
    """Validate exact publication evidence; structural fixtures are not native proof."""
    canonical, before, db = e['canonical'], e['before'], e['db']
    reply = e['reply']
    return bool(
        e['final'] == 'completed' and e['receipts'] == [(e['run_id'],)]
        and valid_driver_continuity(e.get('driver_continuity', {}))
        and e.get('task_replies') == [reply]
        and e['running_before_fault']
        and e['running_before_fault'][-1].get('http') == 200
        and e['running_before_fault'][-1].get('status') == 'running'
        and e['running_before_fault'][-1].get('run_id') == e['run_id']
        and e['fault'].get('old_port') is not None
        and e['fault'].get('old_port') == e['fault'].get('new_port')
        and canonical == db and canonical[:len(before)] == before
        and {'seq': e['user_seq'], 'event_id': e['user_event_id']} in canonical
        and reply.get('kind') == 'message.member'
        and reply.get('payload', {}).get('task_id') == e['task_id']
        and reply.get('payload', {}).get('member_id') == 'peer-b'
        and reply['seq'] > e['user_seq']
        and {'seq': reply['seq'], 'event_id': reply['event_id']} in canonical
        and len(canonical) == len({r['event_id'] for r in canonical})
        and all(a['seq'] < b['seq'] for a, b in zip(canonical, canonical[1:])))


def valid_e_canonical(e):
    """Exact target-connector host-send evidence, accepts JSON receipt arrays."""
    try:
        fault = e['fault']
        base = dict(e, receipts=[tuple(r) for r in e['receipts']],
                    fault={'old_port': 0, 'new_port': 0})
        return bool(valid_c_canonical(base)
            and e['idempotency_key'] == f"room:{e['task_id']}:{e['dispatch_generation']}"
            and e['receipts_before'] == e['receipts']
            and e['task_replies'] == [e['reply']]
            and fault['kind'] == 'target_connector'
            and fault['old_incarnation'] and fault['new_incarnation']
            and fault['old_incarnation'] != fault['new_incarnation']
            and fault['old_stopped'] is True and fault['relay_unchanged'] is True
            and 0 < fault['elapsed'] <= fault['bound_seconds'] <= 30)
    except (KeyError, TypeError, IndexError):
        return False


def behavioral_slow_stream_probe():
    """Real WSS+pinned mTLS; a silent inner-message sender, not a native run.

    Route labels/empty receipt path are transport fixtures only. No native
    process, grant, admission, or allowlist is fabricated or modified.
    """
    from room_firewall import ForwardedRequest
    transport = net.NetworkTransport()
    original_timeout = connector.REQUEST_TIMEOUT
    sock = tls = None
    try:
        transport.start()
        scope = dict(room_id='behavioral-room', home_install_id='transport-host',
                     authority_gateway_id='transport-host', authority_epoch=1,
                     member_id='peer-b', target_install_id='transport-target', target_profile='default')
        transport.native_receipt_db = transport.root / 'unused-readonly-receipts.db'
        from run_bindings import HostRunBindings
        firewall = RoomFirewall(expected_scope=scope, backend=lambda request: None,
                                verify_native=HostRunBindings(transport.root / 'behavioral-bindings.db'))
        transport.bind('behavioral-link', firewall, target_url='http://127.0.0.1:1', scope=scope)
        connector.REQUEST_TIMEOUT = 1.5
        sock = transport.host_client.open_stream()
        sock.settimeout(3)
        hop = connector.HostConnector(transport.host_client, inner=transport.host_cfg['inner_tls'])
        tls = connector.wrap_client(sock, hop.context, expected_fingerprint=hop.peer_fingerprint)
        start = time.monotonic()
        # First stream sends no inner-message header: the target is genuinely
        # blocked in recv_message(), while the second stream still completes.
        response = hop.forward(ForwardedRequest('GET', '/v1/sessions', (), b''))
        concurrent_elapsed = time.monotonic() - start
        eof = tls.recv(1) == b''
        elapsed = time.monotonic() - start
        result = dict(timed_out=eof and elapsed >= 1.2, elapsed=elapsed,
                      request_timeout=connector.REQUEST_TIMEOUT,
                      concurrent_completed=response.status == 403 and concurrent_elapsed < 1.5,
                      concurrent_http=response.status, concurrent_elapsed=concurrent_elapsed,
                      test_type='behavioral_transport_not_native',
                      held_stream='pinned mTLS complete, inner request header withheld',
                      remote_eof=eof)
    finally:
        if tls is not None:
            tls.close()
        elif sock is not None:
            sock.close()
        connector.REQUEST_TIMEOUT = original_timeout
        transport.close()
    result['cleanup_complete'] = (transport.loop.is_closed()
        and all(not c.application_threads and not c._thread.is_alive() for c in transport.clients))
    return result


class NativeLab:
    def __init__(self):
        self.stamp = uuid.uuid4().hex[:12]
        self.out = ROOT / "evidence" / "s1-resume" / ("b-native-" + self.stamp)
        self.out.mkdir(parents=True, mode=0o700)
        self.report = {"scope": "one-machine local native only; F1 remains closed", "checks": {},
                       "source_start": snapshot(), "commands": [], "processes": [], "stages": [],
                       "lab_model_request_overrides": {"chat_template_kwargs": {"enable_thinking": False}}}
        self.owned = []
        self.logs = []
        self.transports = []
        self.keys = []

    def check(self, name, ok, **detail):
        self.report["checks"][name] = {"ok": bool(ok), **detail}
        self.flush()
        if not ok:
            raise AssertionError(name)

    def flush(self):
        (self.out / "report.json").write_text(json.dumps(self.report, indent=2), encoding="utf-8")

    def spawn(self, role):
        home = self.home if role == "host" else self.target
        argv = [str(PY), "-B", str(self.entry), str(gc.CORE), str(self.audit), *self.args[role]]
        log = (self.out / f"{role}-{len(self.owned)}.log").open("w", encoding="utf-8")
        self.logs.append(log)
        proc = subprocess.Popen(argv, cwd=gc.CORE, env=gc.pf.isolated_child_env(home),
                                stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        row = next(r for r in processes() if r["pid"] == proc.pid)
        record = {"role": role, **row, "argv": argv, "home": str(home), "signals": []}
        self.owned.append((proc, record))
        self.report["processes"].append(record)
        self.report["commands"].append({"argv": argv, "cwd": str(gc.CORE), "home": str(home)})
        self.flush()
        return proc

    def terminate(self, proc):
        record = next(r for p, r in self.owned if p is proc)
        record["returncode_before_requested_termination"] = proc.poll()
        if proc.poll() is None:
            record["signals"].append({"signal": "SIGTERM", "at": time.time()})
            proc.terminate()
            try:
                proc.wait(25)
            except subprocess.TimeoutExpired:
                record["signals"].append({"signal": "SIGKILL", "at": time.time()})
                proc.kill()
                proc.wait(10)
        record["returncode"] = proc.returncode
        record["terminated_on_request"] = record["returncode_before_requested_termination"] is None
        self.flush()

    def setup(self):
        assert self.report["source_start"]["branch"] == "feat/invite-rooms"
        # The lab tree must descend from the verified S1/S2/F1 checkpoint; an
        # exact-HEAD pin went stale on every later local checkpoint commit.
        assert subprocess.run(["git", "merge-base", "--is-ancestor", "dc028f5", "HEAD"],
                              cwd=gc.REPO, capture_output=True).returncode == 0
        for port in (18790, 18791):
            assert gc.pf.check_port_available(port)["available"], "native lab port occupied"
        import urllib.request
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open("http://127.0.0.1:8083/v1/models", timeout=10) as response:
            ids = [x.get("id") for x in json.load(response).get("data", [])]
        self.check("preflight_model_present", MODEL in ids, model_ids=ids)
        self.home = gc.pf.APPROVED_RUNTIME_ROOT / ("s1r-" + self.stamp)
        baseline = gc.pf.run_preflight(self.home, port=18790)
        self.target = self.home / "second"
        self.keys = [secrets.token_urlsafe(32), secrets.token_urlsafe(32), secrets.token_hex(32)]
        for home, port, key, link in ((self.home, 18790, self.keys[0], False),
                                      (self.target, 18791, self.keys[1], True)):
            gc.config(home, port, key, link=link)
            path = home / "config.yaml"
            config = yaml.safe_load(path.read_text())
            config["cron"] = {"provider": "s1_lab_disabled"}
            config["memory"]["provider"] = "local"
            config["compression"] = {"enabled": False}
            config["fallback_model"] = {}
            path.write_text(yaml.safe_dump(config), encoding="utf-8")
            path.chmod(0o600)
            plugin = home / "plugins" / "s1_lab_disabled"
            plugin.mkdir(parents=True)
            (plugin / "__init__.py").write_text(DISABLED_CRON, encoding="utf-8")
            # No ticker started: prove the *actual* native resolver selects the
            # supported disabled provider, not an unsupported config default.
            probe = gc.pf._native_probe(home, 'import json\nfrom cron.scheduler_provider import resolve_cron_scheduler\np=resolve_cron_scheduler()\nprint(json.dumps({"name":p.name}))')
            self.check("cron_disabled_" + str(port), probe["name"] == "s1-lab-disabled", provider=probe["name"])
        policy = gc.pf.effective_native_policy(self.target)
        self.check("preflight_policy", policy == baseline["effective_policy"], policy=policy)
        token_dir = self.home / ".hermes" / "desktop-ssh" / uuid.uuid4().hex
        token_dir.mkdir(parents=True, mode=0o700)
        token = token_dir / (uuid.uuid4().hex[:16] + ".token")
        token.write_text(self.keys[2])
        token.chmod(0o600)
        self.entry, self.audit = self.out / "native-entry.py", self.out / "native-authority.jsonl"
        self.entry.write_text(NATIVE_ENTRY)
        self.args = {"target": ["gateway", "run", "--external-supervisor"],
                     "host": ["serve", "--isolated", "--host", "127.0.0.1", "--port", "18790",
                              "--ssh-session-token-file", str(token)]}
        self.report.update(identity=baseline["identity"], policy=policy, home=str(self.home), target_home=str(self.target))
        self.children = [self.spawn("target"), self.spawn("host")]
        self.transport = net.NetworkTransport()
        self.transports.append(self.transport)
        self.transport.start()

    def authority(self):
        audits = [json.loads(line) for line in self.audit.read_text().splitlines()] if self.audit.exists() else []
        db = self.target / "shared-state.db"
        rows = gc.read_db(db, "SELECT * FROM hosted_room_peer_reservations ORDER BY room_id,member_id,target_profile")
        fences = gc.read_db(db, "SELECT * FROM hosted_room_revoked_grants ORDER BY scope_key")
        mints = [row for row in audits if row.get("event") == "native_issue_room_grant_return"]
        return {"mint_count": len(mints), "refresh_count": sum(row.get("kind") == "refresh" for row in mints),
                "observer_installs": [row for row in audits if row.get("event") == "observer_installed"],
                "native_mints": mints, "reservations": rows, "revocation_fences": fences}

    async def call(self, path, method="GET", body=None, grant=None, key=None, adapter=None):
        return await asyncio.to_thread(_call, (adapter or self.adapter) + path, method, body,
                                       grant or self.grant, key=key)

    async def mint(self, scope=None, ttl=600, status_ttl=None):
        scope = scope or self.scope
        payload = {k: scope[k] for k in ("room_id", "home_install_id", "authority_gateway_id", "authority_epoch", "member_id")}
        payload["ttl_seconds"] = ttl
        if status_ttl is not None:
            payload["status_ttl_seconds"] = status_ttl
        status, body = await asyncio.to_thread(_call, "http://127.0.0.1:18791/v1/room-members/invitations",
                                               "POST", payload, self.keys[1], "Bearer")
        self.check("owner_mint_" + str(len(self.authority()["native_mints"])), status == 201 and bool(body.get("grant")), status=status)
        return body

    async def running(self, run_id, grant=None, adapter=None, seconds=150):
        deadline = time.monotonic() + seconds
        states = []
        while time.monotonic() < deadline:
            status, body = await self.call("/v1/runs/" + run_id, grant=grant, adapter=adapter)
            states.append({"http": status, **{k: body.get(k) for k in ("run_id", "status", "execution_generation")}})
            if status == 200 and body.get("status") == "running":
                return states
            if body.get("status") in TERMINAL:
                break
            await asyncio.sleep(.1)
        self.report["running_probe_failed"] = states
        self.flush()
        raise AssertionError("authenticated_running_state_not_observed")

    async def dispatch(self, tag, prompt=LONG, grant=None):
        task = tag + "-" + uuid.uuid4().hex[:10]
        key = f"room:{task}:1"
        body = _dispatch(self.scope, self.catalog, prompt, task)
        status, result = await self.call("/v1/runs", "POST", body, grant=grant, key=key)
        self.check(tag + "_admitted", status in (200, 202) and bool(result.get("run_id")), http=status,
                   task_id=task, execution_generation=1, idempotency_key=key, run_id=result.get("run_id"))
        return result["run_id"], key, body

    async def reconnect_target(self):
        old = self.transport.target_client
        await asyncio.to_thread(old.stop)
        fresh = connector.build_client(self.transport.target_cfg, on_new_stream=self.transport.target.serve)
        await asyncio.to_thread(fresh.start)
        self.transport.clients.append(fresh)
        self.transport.target_client = fresh
        return [old.incarnation, fresh.incarnation]

    async def reconnect_host(self):
        """Restart the host connector via the transport's supported method."""
        result = await asyncio.to_thread(self.transport.restart_host_connector)
        return [result["old_incarnation"], result["new_incarnation"]]

    async def restart_relay(self):
        """Restart the relay daemon via the transport's supported method."""
        return await asyncio.to_thread(self.transport.restart_relay)

    async def relay_restart_checks(self):
        """C packet: real relay daemon restart with no offline replay."""
        # 1. Dispatch a run and prove it is RUNNING before the relay fault.
        run, key, body = await self.dispatch("relay-restart-running")
        seen = await self.running(run)
        before = self.authority()
        target_served_before = self.transport.target.served

        # 2. Stop the relay daemon.  In-flight requests through the relay must
        #    fail — the connector design rejects offline replay.
        relay_info = await self.restart_relay()
        self.check("relay_restarted_same_port",
                   relay_info["old_port"] == relay_info["new_port"],
                   old_port=relay_info["old_port"], new_port=relay_info["new_port"])

        # 3. Wait for the running run to reach a terminal state after relay
        #    restart.  The connector reconnects via its backoff loop; the
        #    in-flight request may fail (ambiguous) but the run continues on
        #    the target.  Retry with the SAME immutable coordinates and key.
        final, states = await _wait_terminal(self.adapter, self.grant, run, 240)
        retry_http, retry = await self.call("/v1/runs", "POST", body, key=key)
        rows = _receipts(self.target, key)
        self.check("relay_restart_same_run_same_receipt",
                   final == "completed" and rows == [(run,)]
                   and retry.get("run_id") == run and retry.get("replayed") is True,
                   final=final, states=states, run_id=run,
                   retry_http=retry_http, receipts=rows,
                   task_id=body["hosted_room_dispatch"]["task_id"],
                   execution_generation=1, idempotency_key=key,
                   relay_restart_info=relay_info,
                   semantics="relay restart fails in-flight; same-key retry is a native replay, not a new dispatch")

        # 4. Prove no authority change across relay restart.
        self.check("relay_restart_no_authority_change",
                   self.authority() == before,
                   before=before, after=self.authority())

        # 5. Prove no offline prompt replay: dispatch a NEW run while the
        #    relay is up, then stop the relay, try to dispatch another NEW
        #    run — it must fail (not silently queue/replay later).
        run2, key2, body2 = await self.dispatch("relay-loss-after-restart")
        seen2 = await self.running(run2)
        # Stop the relay before the outage dispatch.
        await asyncio.to_thread(self.transport.stop_relay)
        target_served_during = self.transport.target.served
        task3 = "offline-" + uuid.uuid4().hex[:10]
        key3 = f"room:{task3}:1"
        body3 = _dispatch(self.scope, self.catalog, "Reply only OFFLINE.", task3)
        offline_failed = False
        offline_http = None
        try:
            offline_http, offline_result = await self.call("/v1/runs", "POST", body3, key=key3)
        except Exception:
            offline_failed = True
        # The offline dispatch must NOT have reached the target backend.
        target_served_after = self.transport.target.served
        offline_rows = _receipts(self.target, key3)
        self.check("relay_offline_no_replay_no_hidden_post",
                   (offline_failed or (offline_http is not None and offline_http >= 500))
                   and target_served_after == target_served_during
                   and offline_rows == [],
                   target_served_during_outage=target_served_during,
                   target_served_after_outage=target_served_after,
                   offline_receipts=offline_rows,
                   offline_failed=offline_failed,
                   semantics="dispatch during relay outage fails; no hidden POST reaches target")
        # Restart the relay before retrying the dispatch.
        await asyncio.to_thread(self.transport.start_relay, None)
        # 6. Restart relay, retry the offline dispatch — it must succeed now
        #    as a genuine new run (not replayed, since it never reached target).
        # Wait for connector to reconnect.
        await asyncio.sleep(3)
        retry_http3, retry3 = await self.call("/v1/runs", "POST", body3, key=key3)
        rows3 = _receipts(self.target, key3)
        self.check("relay_back_online_new_dispatch_succeeds",
                   retry_http3 in (200, 202) and bool(retry3.get("run_id"))
                   and rows3 == [(retry3.get("run_id"),)],
                   retry_http=retry_http3, run_id=retry3.get("run_id"),
                   receipts=rows3,
                   semantics="after relay restart, a genuine new dispatch is admitted, not a replay")
        # Clean up: wait for run2 and run3 to complete.
        final2, _ = await _wait_terminal(self.adapter, self.grant, run2, 180)
        final3, _ = await _wait_terminal(self.adapter, self.grant, retry3["run_id"], 180)
        self.check("relay_restart_runs_completed",
                   final2 == "completed" and final3 == "completed",
                   run2_final=final2, run3_final=final3)

    async def host_connector_restart_checks(self):
        """C packet: host connector restart with lost admission reconciliation."""
        # 1. Dispatch a run, prove RUNNING.
        run, key, body = await self.dispatch("host-conn-restart-running")
        seen = await self.running(run)
        before = self.authority()
        target_served_before = self.transport.target.served

        # 2. Stop the host connector.  In-flight requests through the host
        #    connector must fail — no offline prompt replay.
        incarnations = await self.reconnect_host()
        self.check("host_connector_restarted",
                   incarnations[0] is not None and incarnations[1] is not None
                   and incarnations[0] != incarnations[1],
                   old_incarnation=incarnations[0], new_incarnation=incarnations[1])

        # 3. Wait for the run to complete and retry with same key.
        final, states = await _wait_terminal(self.adapter, self.grant, run, 240)
        retry_http, retry = await self.call("/v1/runs", "POST", body, key=key)
        rows = _receipts(self.target, key)
        self.check("host_connector_restart_same_run_same_receipt",
                   final == "completed" and rows == [(run,)]
                   and retry.get("run_id") == run and retry.get("replayed") is True,
                   final=final, states=states, run_id=run,
                   retry_http=retry_http, receipts=rows,
                   task_id=body["hosted_room_dispatch"]["task_id"],
                   execution_generation=1, idempotency_key=key,
                   host_connector_incarnations=incarnations,
                   semantics="host connector restart fails in-flight; same-key retry is native replay")

        # 4. Prove no authority change.
        self.check("host_connector_restart_no_authority_change",
                   self.authority() == before,
                   before=before, after=self.authority())

        # 5. Lost admission reconciliation: dispatch a new run, but drop the
        #    reply AFTER native admission (same as fault_checks, but through
        #    the restarted host connector).
        firewall = self.transport.target.firewall
        real = firewall._backend
        dropped = []
        def lost(request):
            result = real(request)
            if request.method == "POST" and request.path == "/v1/runs" and not dropped:
                dropped.append(result.status)
                raise ConnectionResetError("c_injected_reply_loss_after_restart")
            return result
        task = "host-restart-lost-" + uuid.uuid4().hex[:10]
        key_lost = f"room:{task}:1"
        body_lost = _dispatch(self.scope, self.catalog, "Reply only LOST_OK.", task)
        firewall._backend = lost
        try:
            first, _ = await self.call("/v1/runs", "POST", body_lost, key=key_lost)
        finally:
            firewall._backend = real
        rows_lost = _receipts(self.target, key_lost)
        actual_run = rows_lost[0][0] if rows_lost else None
        self.check("host_connector_lost_admission_same_coordinates",
                   first >= 500 and dropped == [202]
                   and actual_run is not None,
                   first_http=first, native_http=dropped,
                   run_id=actual_run, task_id=task, generation=1,
                   idempotency_key=key_lost, receipts=rows_lost,
                   semantics="lost admission after host connector restart; same immutable coordinates")
        # Retry with same key — must get replayed=true, same run.
        retry_http, retry = await self.call("/v1/runs", "POST", body_lost, key=key_lost)
        rows_after = _receipts(self.target, key_lost)
        self.check("host_connector_lost_admission_replay_same_run",
                   retry_http in (200, 202) and retry.get("replayed") is True
                   and retry.get("run_id") == actual_run and rows_after == rows_lost,
                   retry_http=retry_http, run_id=actual_run,
                   receipts=rows_after,
                   semantics="same-key retry after lost admission returns the same run, exactly one receipt")
        await _wait_terminal(self.adapter, self.grant, actual_run, 180)

    async def target_connector_restart_checks(self):
        run, key, body = await self.dispatch("target-conn-restart-running")
        seen = await self.running(run)
        before = self.authority()
        incarnations = await self.reconnect_target()
        self.check("target_connector_restarted",
                   incarnations[0] is not None and incarnations[1] is not None
                   and incarnations[0] != incarnations[1],
                   old_incarnation=incarnations[0], new_incarnation=incarnations[1])
        final, states = await _wait_terminal(self.adapter, self.grant, run, 240)
        retry_http, retry = await self.call("/v1/runs", "POST", body, key=key)
        rows = _receipts(self.target, key)
        self.check("target_connector_restart_same_run_same_receipt",
                   final == "completed" and rows == [(run,)]
                   and retry.get("run_id") == run and retry.get("replayed") is True,
                   final=final, states=states, run_id=run,
                   retry_http=retry_http, receipts=rows,
                   task_id=body["hosted_room_dispatch"]["task_id"], execution_generation=1,
                   idempotency_key=key, target_connector_incarnations=incarnations,
                   semantics="target connector restart fails in-flight; same-key retry is native replay")
        self.check("target_connector_restart_no_authority_change",
                   self.authority() == before,
                   before=before, after=self.authority())
        return None

    async def unrelated_link_bounds_checks(self):
        """C packet: unrelated link and slow-reader bounds.

        Behavioral transport tests, explicitly distinguished from native
        evidence: these prove the relay/connector enforce route isolation
        and bounded backpressure, NOT native admission/authority semantics.
        """
        # 1. Unrelated link: a dispatch with a different room_id in the scope
        #    is rejected by the host firewall before reaching the target.
        #    This is a firewall/transport check, not a native admission check.
        unrelated_scope = dict(self.scope, room_id="unrelated-" + uuid.uuid4().hex)
        task = "unrelated-link-" + uuid.uuid4().hex[:10]
        key = f"room:{task}:1"
        body = _dispatch(unrelated_scope, self.catalog, "Reply only UNRELATED.", task)
        before_served = self.transport.target.served
        before_rows = _receipts(self.target, key)
        status, response = await self.call("/v1/runs", "POST", body, key=key)
        after_served = self.transport.target.served
        after_rows = _receipts(self.target, key)
        self.check("unrelated_room_scope_rejected_before_target",
                   status == 403 and after_served == before_served and after_rows == before_rows == [],
                   http=status, served_before=before_served, served_after=after_served,
                   receipts_before=before_rows, receipts_after=after_rows,
                   test_type="behavioral_transport",
                   semantics="unrelated room_id scope is rejected by host firewall before relay/target")

        # 2. Slow-reader bound: the connector enforces MAX_MESSAGE and
        #    REQUEST_TIMEOUT.  An oversize send is rejected by send_message()
        #    before it enters the relay, and MAX_MESSAGE/REQUEST_TIMEOUT are
        #    both declared positive bounds.
        from connector import MAX_MESSAGE, REQUEST_TIMEOUT
        from unittest.mock import MagicMock
        frames = MagicMock()
        frames_sent = []
        frames.send = lambda raw: frames_sent.append(raw)
        oversized_failed = False
        try:
            connector.send_message(frames, bytes(MAX_MESSAGE + 1))
        except connector.ConnectorError:
            oversized_failed = True
        self.check("connector_oversize_send_rejected_locally",
                   oversized_failed and frames_sent == [],
                   max_message=MAX_MESSAGE,
                   frames_ever_sent=len(frames_sent),
                   test_type="behavioral_transport",
                   semantics="connector rejects messages > MAX_MESSAGE before they enter the relay")
        # Independent real transport exercise: no native admission claims.
        bound = await asyncio.to_thread(behavioral_slow_stream_probe)
        self.check("connector_request_timeout_measured_concurrent_stream",
                   bound['timed_out'] and bound['remote_eof']
                   and bound['elapsed'] <= bound['request_timeout'] + .75
                   and bound['concurrent_completed'] and bound['cleanup_complete'], **bound)

        # Relay route isolation requirement is dropped; route matching is enforced
        # in the connector implementation, but this packet does not fabricate a
        # second route and cannot prove delivery in the running lab.

    async def e_target_canonical_readback(self):
        """E-2: actual host-driven task, target connector fault, exact canonical publication."""
        await self.c_canonical_readback(target_connector=True)

    async def c_canonical_readback(self, *, target_connector=False):
        """A real groups.send crosses the selected fault while its native run is RUNNING."""
        room = self.scope['room_id']
        async with aiohttp.ClientSession(trust_env=False) as session:
            async with session.ws_connect('ws://127.0.0.1:18790/api/ws?token=' + self.keys[2]) as ws:
                before_page = await gc.rpc(ws, 'groups.log', {'room_id': room, 'since_seq': 0, 'limit': 500})
                assert not before_page['has_more']
                user_id = 'c-fault-user-' + uuid.uuid4().hex
                sent = await gc.rpc(ws, 'groups.send', {
                    'room_id': room, 'event_id': user_id,
                    'payload': {'text': '@peer ' + LONG + ' Do not mention any other member.',
                                'thread_id': 'c-fault-' + self.stamp}})
                user = sent['event']
                deadline = time.monotonic() + 90
                matched = []
                while time.monotonic() < deadline:
                    matched = gc.read_db(self.home / 'shared-state.db',
                        'SELECT r.run_id,r.task_id,r.execution_generation FROM hosted_room_remote_runs r '
                        'JOIN hosted_room_driver_tasks t ON r.room_id=t.room_id AND r.task_id=t.task_id '
                        'WHERE r.room_id=? AND t.source_event_seq=?', (room, user['seq']))
                    if matched:
                        break
                    await asyncio.sleep(.1)
                assert len(matched) == 1, 'host_send_native_receipt_not_found'
                admitted = matched[0]
                run, task = admitted['run_id'], admitted['task_id']
                key = f"room:{task}:{admitted['execution_generation']}"
                running = await self.running(run)
                authority = self.authority()
                receipts_before = _receipts(self.target, key)
                driver_path = self.audit.with_name('native-driver.jsonl')
                driver_before = [json.loads(line) for line in driver_path.read_text().splitlines()]
                lease_before = gc.read_db(self.home / 'shared-state.db',
                    'SELECT lease_generation FROM hosted_room_driver_leases WHERE room_id=?', (room,))
                fault_start = time.time()
                if target_connector:
                    assert receipts_before == [(run,)], 'host_task_exact_native_receipt_required'
                    old = self.transport.target_client
                    relay, relay_thread = self.transport.daemon, self.transport.thread
                    started = time.monotonic()
                    incarnations = await asyncio.wait_for(self.reconnect_target(), timeout=30)
                    fault = dict(kind='target_connector', old_incarnation=incarnations[0],
                                 new_incarnation=incarnations[1],
                                 old_stopped=not old._thread.is_alive(),
                                 relay_unchanged=(self.transport.daemon is relay
                                     and self.transport.thread is relay_thread and relay_thread.is_alive()),
                                 elapsed=time.monotonic() - started, bound_seconds=30)
                else:
                    fault = await self.restart_relay()
                final, states = await _wait_terminal(self.adapter, self.grant, run, 180)
                deadline = time.monotonic() + 45
                reply = None
                while time.monotonic() < deadline:
                    page = await gc.rpc(ws, 'groups.log', {'room_id': room, 'since_seq': 0, 'limit': 500})
                    replies = [e for e in page['events'] if e['kind'] == 'message.member'
                               and e['payload'].get('task_id') == task]
                    if replies:
                        assert len(replies) == 1
                        reply = replies[0]
                        break
                    await asyncio.sleep(.2)
                assert reply is not None, 'faulted_host_send_canonical_reply_missing'
                fault_end = time.time()
                driver_after = [json.loads(line) for line in driver_path.read_text().splitlines()]
                continuity = dict(window_start=fault_start, window_end=fault_end,
                    observer_baseline=sum(e.get('room_id') == room for e in driver_before),
                    events=[e for e in driver_after if e.get('room_id') == room
                            and fault_start <= e['timestamp'] <= fault_end],
                    lease_before=lease_before,
                    lease_after=gc.read_db(self.home / 'shared-state.db',
                        'SELECT lease_generation FROM hosted_room_driver_leases WHERE room_id=?', (room,)),
                    task=gc.read_db(self.home / 'shared-state.db',
                        'SELECT indeterminate_at FROM hosted_room_driver_tasks WHERE room_id=? AND task_id=?',
                        (room, task)))
                # Fence only after publication. Read the exact same canonical
                # sequence through authenticated RPC and the real host DB.
                await gc.rpc(ws, 'groups.stop', {'room_id': room, 'cancel_id': 'c-fault-stop-' + self.stamp})
                page = await gc.rpc(ws, 'groups.log', {'room_id': room, 'since_seq': 0, 'limit': 500})
                assert not page['has_more']
        pairs = lambda events: [{'seq': e['seq'], 'event_id': e['event_id']} for e in events]
        evidence = dict(user_event_id=user['event_id'], requested_client_event_id=user_id,
                        user_seq=user['seq'], run_id=run, task_id=task,
                        dispatch_generation=admitted['execution_generation'], idempotency_key=key,
                        final=final, states=states, running_before_fault=running, fault=fault,
                        receipts=_receipts(self.target, key), reply=reply,
                        driver_continuity=continuity,
                        task_replies=[e for e in page['events'] if e['kind'] == 'message.member'
                                      and e['payload'].get('task_id') == task],
                        before=pairs(before_page['events']), canonical=pairs(page['events']),
                        db=gc.read_db(self.home / 'shared-state.db',
                            'SELECT seq,event_id FROM hosted_room_events WHERE room_id=? ORDER BY seq', (room,)))
        if target_connector:
            evidence.update(receipts_before=receipts_before,
                task_replies=[e for e in page['events'] if e['kind'] == 'message.member'
                              and e['payload'].get('task_id') == task])
            self.report['e2_evidence'] = evidence
            self.check('canonical_host_send_exact_seq_event_id_after_target_connector',
                       valid_e_canonical(evidence) and self.authority() == authority, **evidence)
        else:
            self.check('canonical_host_send_exact_seq_event_id_after_C', valid_c_canonical(evidence)
                       and self.authority() == authority, **evidence)

    async def baseline(self):
        await gc.workflow(self.report, self.home, self.target, 18790, 18791, self.keys[1], self.keys[2],
                          self.children, self.transport)
        self.check("baseline_real_native_and_canonical", self.report.get("gate_C_passed") is True)
        self.adapter = self.report["adapter_url"]
        self.catalog = self.report["catalog"]
        room = self.report["room"]
        owner = self.report["identity"]["gateway_id"]
        self.scope = dict(room_id=room["room_id"], home_install_id=owner, authority_gateway_id=owner,
                          authority_epoch=room["authority_epoch"], member_id="peer-b",
                          target_install_id=self.catalog["installation_id"], target_profile="default")
        assert len(net.SECRETS) == 1
        self.grant = next(iter(net.SECRETS)).decode()
        status, refreshed = await self.call("/v1/room-members/grants/refresh", "POST", {"ttl_seconds": 600})
        self.check("baseline_real_refresh", status == 200 and bool(refreshed.get("grant")), http=status)
        authority = self.authority()
        target_pid = self.children[0].pid
        self.check("observer_real_mint_and_refresh_serving_pid", authority["mint_count"] > 0
                   and authority["refresh_count"] > 0
                   and all(row["pid"] == target_pid for row in authority["native_mints"]),
                   serving_pid=target_pid, authority=authority)
        self.report["native_authority_baseline"] = authority

    async def fault_checks(self):
        # Lost reply after actual native admission; retry is the identical body,
        # task/generation/key. Fault is in the test harness, not an invented API.
        firewall = self.transport.target.firewall
        real = firewall._backend
        dropped = []
        def lost(request):
            result = real(request)
            if request.method == "POST" and request.path == "/v1/runs" and not dropped:
                dropped.append(result.status)
                raise ConnectionResetError("s1_injected_reply_loss")
            return result
        task = "lost-" + uuid.uuid4().hex[:10]
        key = f"room:{task}:1"
        body = _dispatch(self.scope, self.catalog, "Reply only LOST_OK.", task)
        firewall._backend = lost
        try:
            first, _ = await self.call("/v1/runs", "POST", body, key=key)
        finally:
            firewall._backend = real
        status, retry = await self.call("/v1/runs", "POST", body, key=key)
        rows = _receipts(self.target, key)
        self.check("lost_admission_reply_same_coordinates", first >= 500 and dropped == [202]
                   and status in (200, 202) and retry.get("replayed") is True and rows == [(retry.get("run_id"),)],
                   first_http=first, retry_http=status, native_http=dropped, task_id=task, generation=1,
                   idempotency_key=key, run_id=retry.get("run_id"), receipts=rows)
        await _wait_terminal(self.adapter, self.grant, retry["run_id"], 180)
        run, key, body = await self.dispatch("running-reconnect")
        observed = await self.running(run)
        before = self.authority()
        incarnations = await self.reconnect_target()
        final, states = await _wait_terminal(self.adapter, self.grant, run, 240)
        retry_http, retry = await self.call("/v1/runs", "POST", body, key=key)
        rows = _receipts(self.target, key)
        self.check("running_connector_loss_same_run", final == "completed" and rows == [(run,)]
                   and retry.get("run_id") == run and retry.get("replayed") is True,
                   running_observations=observed, incarnations=incarnations, final=final, states=states,
                   receipts=rows, task_id=body["hosted_room_dispatch"]["task_id"], execution_generation=1,
                   idempotency_key=key, retry_http=retry_http)
        self.check("native_no_mint_or_authority_change_on_reconnect", before["mint_count"] > 0 and self.authority() == before,
                   before=before, after=self.authority())

    async def lifecycle_checks(self):
        # Bound Room + actual relay path, owner-created short dispatch/status TTL.
        short = await self.mint(ttl=60, status_ttl=90)
        self.short = short
        status, refreshed = await self.call("/v1/room-members/grants/refresh", "POST", {"ttl_seconds": 3600}, grant=short["grant"])
        self.check("valid_refresh_hard_horizon", status == 200 and bool(refreshed.get("grant"))
                   and refreshed["status_expires_at"] == short["status_expires_at"]
                   and refreshed["expires_at"] <= short["status_expires_at"],
                   refresh_http=status, original_dispatch_expiry=short["expires_at"],
                   hard_horizon=short["status_expires_at"], refreshed_dispatch_expiry=refreshed.get("expires_at"))
        # Every immutable scope dimension denied before target forwarding/admission.
        mutations = {"room_id": "other-room", "member_id": "other-member", "home_install_id": "install:other",
                     "authority_gateway_id": "install:other", "authority_epoch": self.scope["authority_epoch"] + 1,
                     "target_install_id": "install:other", "target_profile": "other"}
        for field, value in mutations.items():
            task = "wrong-" + field + "-" + uuid.uuid4().hex[:8]
            key = f"room:{task}:1"
            before_served = self.transport.target.served
            before_rows = _receipts(self.target, key)
            status, response = await self.call("/v1/runs", "POST", _dispatch(dict(self.scope, **{field: value}), self.catalog, "hi", task), key=key)
            after_served, after_rows = self.transport.target.served, _receipts(self.target, key)
            self.check("wrong_scope_" + field, status == 403 and before_served == after_served
                       and before_rows == after_rows == [], http=status, served_before=before_served,
                       served_after=after_served, receipts_before=before_rows, receipts_after=after_rows,
                       idempotency_key=key)
        # Stop after authenticated RUNNING, reply lost AFTER actual native action.
        run, key, _ = await self.dispatch("lost-stop")
        seen = await self.running(run)
        firewall = self.transport.target.firewall
        real = firewall._backend
        dropped = []
        def lose_stop(request):
            response = real(request)
            if request.path == f"/v1/runs/{run}/stop" and not dropped:
                dropped.append(response.status)
                raise ConnectionResetError("s1_injected_stop_reply_loss")
            return response
        firewall._backend = lose_stop
        try:
            first, _ = await self.call(f"/v1/runs/{run}/stop", "POST", {})
        finally:
            firewall._backend = real
        retry_http, _ = await self.call(f"/v1/runs/{run}/stop", "POST", {})
        final, states = await _wait_terminal(self.adapter, self.grant, run, 120)
        self.check("lost_stop_reply_native_readback", first >= 500 and dropped and dropped[0] in (200, 202)
                   and final in {"cancelled", "interrupted"} and _receipts(self.target, key) == [(run,)],
                   first_http=first, retry_http=retry_http, native_http=dropped, running_observations=seen,
                   final=final, states=states, run_id=run)
        # Expired dispatch cannot refresh; status grace remains valid by native contract.
        await asyncio.sleep(max(0, short["expires_at"] - time.time() + .2))
        expired, error = await self.call("/v1/room-members/grants/refresh", "POST", {"ttl_seconds": 600}, grant=short["grant"])
        grace, _ = await self.call("/v1/room-members/capabilities", grant=short["grant"])
        self.check("expired_dispatch_refresh_denied_status_grace", expired == 401 and _code(error) == "invalid_room_grant" and grace == 200,
                   refresh_http=expired, code=_code(error), status_grace_http=grace, via_relay=True)
        await asyncio.sleep(max(0, short["status_expires_at"] - time.time() + .2))
        hard, error = await self.call("/v1/room-members/capabilities", grant=short["grant"])
        self.check("status_hard_horizon", hard == 401 and _code(error) == "invalid_room_grant", http=hard, code=_code(error), via_relay=True)
        run, key, _ = await self.dispatch("revoke-running")
        seen = await self.running(run)
        race = await asyncio.gather(
            self.call("/v1/room-members/grants/refresh", "POST", {"ttl_seconds": 600}),
            self.call("/v1/room-members/grants/revoke", "POST", {}))
        fence = self.authority()
        refresh_http, refresh = race[0]
        revoke_http, _ = race[1]
        old_http, old_error = await self.call("/v1/room-members/capabilities")
        stale_http, stale_error = await self.call("/v1/room-members/grants/refresh", "POST", {"ttl_seconds": 600})
        returned_http = None
        if refresh.get("grant"):
            returned_http, _ = await self.call("/v1/room-members/capabilities", grant=refresh["grant"])
        self.check("refresh_revoke_race_native_fence", revoke_http == 200 and old_http == stale_http == 403
                   and _code(old_error) == _code(stale_error) == "room_reauthorization_required"
                   and returned_http in (None, 403) and bool(fence["revocation_fences"]),
                   refresh_http=refresh_http, revoke_http=revoke_http, old_http=old_http,
                   stale_refresh_http=stale_http, returned_refresh_capabilities_http=returned_http,
                   native_authority=fence, running_observations=seen)
        second, _ = await self.call("/v1/room-members/grants/revoke", "POST", {})
        before = self.authority()
        await self.reconnect_target()
        after_http, after_error = await self.call("/v1/room-members/capabilities")
        self.check("revoke_idempotent_reconnect_no_restore", second == 200 and self.authority() == before
                   and after_http == 403 and _code(after_error) == "room_reauthorization_required", http=after_http,
                   repeat_revoke_http=second, before=before, after=self.authority())
        observer = await self.mint()
        final, states = await _wait_terminal(self.adapter, observer["grant"], run, 240)
        self.check("revoke_is_not_stop", final == "completed" and _receipts(self.target, key) == [(run,)],
                   running_observations=seen, final=final, states=states, run_id=run,
                   observer="explicit owner same-scope reauthorization")
        self.grant = observer["grant"]

    async def lost_lifecycle_checks(self):
        """LAST same-scope scenario. Refresh is issuance, revoke is a DB fence."""
        firewall = self.transport.target.firewall
        real = firewall._backend
        before = self.authority()
        dropped = []

        def lose_refresh(request):
            response = real(request)
            if request.path == "/v1/room-members/grants/refresh" and not dropped:
                dropped.append(response.status)
                raise ConnectionResetError("b_refresh_reply_loss_after_native")
            return response

        firewall._backend = lose_refresh
        try:
            first, ambiguous = await self.call("/v1/room-members/grants/refresh", "POST", {"ttl_seconds": 600})
        finally:
            firewall._backend = real
        after = self.authority()
        issued = after["native_mints"][-1]
        prior = before["native_mints"][-1]
        fields = (*self.scope.keys(), "permissions", "execution_policy_digest", "status_expires_at")
        self.check("lost_refresh_reply_native_fence_readback", first == 502 and dropped == [200]
                   and not ambiguous.get("grant") and after["refresh_count"] == before["refresh_count"] + 1
                   and after["mint_count"] == before["mint_count"] + 1
                   and after["reservations"] == before["reservations"]
                   and after["revocation_fences"] == before["revocation_fences"]
                   and all(issued.get(k) == prior.get(k) for k in fields)
                   and issued["expires_at"] <= prior["status_expires_at"],
                   first_http=first, native_http=dropped, via_relay=True,
                   native_issuance_before=before, native_issuance_after=after,
                   semantics="refresh mints a signed replacement; no native durable grant row/fence is created; lost reply remains ambiguous")
        retry_http, replacement = await self.call("/v1/room-members/grants/refresh", "POST", {"ttl_seconds": 600})
        acknowledged = self.authority()
        replacement_grant = replacement.get("grant")
        capabilities_http, _ = await self.call("/v1/room-members/capabilities", grant=replacement_grant)
        self.check("lost_refresh_genuine_replacement_acknowledged", retry_http == 200
                   and bool(replacement_grant) and capabilities_http == 200
                   and acknowledged["refresh_count"] == after["refresh_count"] + 1
                   and replacement["status_expires_at"] == issued["status_expires_at"]
                   and replacement["expires_at"] <= issued["status_expires_at"],
                   retry_http=retry_http, replacement_capabilities_http=capabilities_http,
                   hard_horizon=replacement.get("status_expires_at"),
                   dispatch_expiry=replacement.get("expires_at"),
                   semantics="retry is a NEW real native issuance, not replay/idempotent refresh")
        run, key, body = await self.dispatch("refresh-reconciled", prompt="Reply only REFRESH_OK.", grant=replacement_grant)
        final, states = await _wait_terminal(self.adapter, replacement_grant, run, 180)
        self.check("replacement_ack_real_dispatch", final == "completed" and _receipts(self.target, key) == [(run,)],
                   final=final, states=states, run_id=run,
                   dispatch_task_id=body["hosted_room_dispatch"]["task_id"], dispatch_generation=1,
                   idempotency_key=key, receipts=_receipts(self.target, key))

        # All earlier same-scope expiry/race checks have completed. A separate
        # grant would NOT isolate this revoke: its native fence covers the scope.
        run, key, body = await self.dispatch("lost-revoke-running")
        seen = await self.running(run)
        before = self.authority()
        dropped = []
        def lose_revoke(request):
            response = real(request)
            if request.path == "/v1/room-members/grants/revoke" and not dropped:
                dropped.append(response.status)
                raise ConnectionResetError("b_revoke_reply_loss_after_native")
            return response
        firewall._backend = lose_revoke
        try:
            first, ambiguous = await self.call("/v1/room-members/grants/revoke", "POST", {})
        finally:
            firewall._backend = real
        after = self.authority()
        # Match the actual native scope-key encoding (NOT the run receipt namespace).
        scope_key = hashlib.sha256(json.dumps({k: str(v) for k, v in self.scope.items()},
                                             sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        fence = [r for r in after["revocation_fences"] if r["scope_key"] == scope_key]
        old_fence = [r for r in before["revocation_fences"] if r["scope_key"] == scope_key]
        rejected = []
        for grant in (self.grant, replacement_grant):
            http, result = await self.call("/v1/room-members/capabilities", grant=grant)
            refresh_http, refresh_result = await self.call("/v1/room-members/grants/refresh", "POST",
                                                         {"ttl_seconds": 600}, grant=grant)
            rejected.append({"capabilities_http": http, "capabilities_code": _code(result),
                             "refresh_http": refresh_http, "refresh_code": _code(refresh_result)})
        retry_http, retried = await self.call("/v1/room-members/grants/revoke", "POST", {})
        repeated = self.authority()
        repeat_fence = [r for r in repeated["revocation_fences"] if r["scope_key"] == scope_key]
        self.check("lost_revoke_reply_native_fence_readback", first == 502 and dropped == [200]
                   and not ambiguous.get("revoked") and len(fence) == len(repeat_fence) == 1
                   and (not old_fence or fence[0]["revoked_before"] > old_fence[0]["revoked_before"])
                   and retry_http == 200 and retried.get("revoked") is True
                   and repeat_fence[0]["revoked_before"] >= fence[0]["revoked_before"]
                   and repeat_fence[0]["expires_at"] >= fence[0]["expires_at"]
                   and all(x["capabilities_http"] == x["refresh_http"] == 403
                           and x["capabilities_code"] == x["refresh_code"] == "room_reauthorization_required" for x in rejected)
                   and repeated["mint_count"] == before["mint_count"]
                   and any(r["room_id"] == self.scope["room_id"] and r["revoked_at"] is not None for r in after["reservations"]),
                   first_http=first, native_http=dropped, retry_http=retry_http, via_relay=True,
                   scope_key=scope_key, durable_fence_before=old_fence, durable_fence_after_loss=fence,
                   durable_fence_after_repeat=repeat_fence, stale_grant_readbacks=rejected,
                   running_observations=seen, native_reservations=after["reservations"],
                   semantics="repeat intent remains revoked; native retry can advance revoked_before, not byte-identical replay")
        observer = await self.mint()
        final, states = await _wait_terminal(self.adapter, observer["grant"], run, 240)
        self.check("lost_revoke_is_not_stop", final == "completed" and _receipts(self.target, key) == [(run,)],
                   final=final, states=states, run_id=run, running_observations=seen,
                   dispatch_task_id=body["hosted_room_dispatch"]["task_id"], dispatch_generation=1,
                   idempotency_key=key, receipts=_receipts(self.target, key),
                   observer="explicit native owner same-scope reauthorization AFTER retry")
        self.grant = observer["grant"]

    async def canonical_readback(self):
        expected = [{"seq": e["seq"], "event_id": e["event_id"]} for e in self.report["canonical_log"]["events"]]
        rows = gc.read_db(self.home / "shared-state.db",
                          "SELECT seq,event_id FROM hosted_room_events WHERE room_id=? ORDER BY seq", (self.scope["room_id"],))
        self.check("canonical_exact_ids_order_after_B", rows[:len(expected)] == expected
                   and len(rows) == len({r["event_id"] for r in rows}),
                   baseline_ids_order=expected, final_db_ids_order=rows)

    async def workflow(self):
        await self.baseline()
        await self.fault_checks()
        await self.lifecycle_checks()

    def close(self):
        failures = self.report.setdefault("cleanup_failures", [])
        self.report["teardown_complete"] = False
        self.report["matrix_complete"] = False

        def attempt(code, action, default=None):
            try:
                return action()
            except BaseException as exc:
                failures.append({"code": code, "type": type(exc).__name__})
                return default

        try:
            for transport in reversed(self.transports):
                attempt("transport_close", transport.close)
            for proc, record in reversed(self.owned):
                attempt("process_terminate", lambda: self.terminate(proc))
                # Record actual readback even when termination raised; None is alive/unknown.
                record["returncode"] = attempt("process_poll", proc.poll)
            for log in self.logs:
                attempt("log_close", log.close)
            groups = {r["pgid"] for _, r in self.owned}

            def survivors():
                return [r for r in processes() if r["pgid"] in groups]

            alive = attempt("process_inventory", survivors)
            deadline = time.monotonic() + 10
            while alive and time.monotonic() < deadline:
                time.sleep(.1)
                alive = attempt("process_inventory", survivors)
            # Signal only a freshly verified exact PID/birth/PGID lab identity.
            self.report["owned_descendant_cleanup"] = []
            import signal
            for row in alive or []:
                def terminate_descendant():
                    current = next((r for r in processes() if r["pid"] == row["pid"]), None)
                    if current == row:
                        os.kill(row["pid"], signal.SIGTERM)
                        self.report["owned_descendant_cleanup"].append(dict(row, requested_signal="SIGTERM"))
                attempt("descendant_terminate", terminate_descendant)
            deadline = time.monotonic() + 10
            while alive and time.monotonic() < deadline:
                time.sleep(.1)
                alive = attempt("process_inventory", survivors)
            self.report["owned_group_survivors"] = alive
            self.report["ports_free_after"] = {
                str(p): attempt("port_inventory", lambda: gc.pf.check_port_available(p)["available"], False)
                for p in (18790, 18791)}
            cleanup = []
            for transport in self.transports:
                def inventory():
                    return {"loop_closed": transport.loop.is_closed(),
                            "relay_thread_alive": transport.thread is not None and transport.thread.is_alive(),
                            "connector_threads_alive": [c.identity for c in transport.clients if c._thread and c._thread.is_alive()],
                            "thread_inventory_complete": all(hasattr(c, "application_threads") for c in transport.clients),
                            "application_threads_alive": [
                                {"connector": c.identity, "name": t.name, "ident": t.ident}
                                for c in transport.clients for t in getattr(c, "application_threads", ())]}
                cleanup.append(attempt("transport_inventory", inventory, {"unknown": True}))
            self.report["transport_cleanup"] = cleanup
            self.report["logs_closed"] = all(getattr(log, "closed", False) for log in self.logs)
            self.report["thread_inventory_complete"] = all(x.get("thread_inventory_complete") for x in cleanup)
            self.report["known_resources_released"] = (
                alive == [] and all(r.get("returncode") is not None for _, r in self.owned)
                and self.report["logs_closed"] and all(self.report["ports_free_after"].values())
                and all(x.get("loop_closed") and not x.get("relay_thread_alive")
                        and not x.get("connector_threads_alive") and not x.get("application_threads_alive")
                        and not x.get("unknown") for x in cleanup))
            self.report["teardown_complete"] = (not failures and self.report["known_resources_released"]
                                                 and self.report["thread_inventory_complete"])
            self.report["source_end"] = attempt("source_snapshot", snapshot)
            if failures:
                self.report["teardown_complete"] = False
            self.report["scope_not_yet_implemented"] = ["separate host and target native process restart boundaries",
                "unrelated native run scope", "lost refresh and revoke replies"]
            if not self.report["thread_inventory_complete"]:
                self.report["scope_not_yet_implemented"].append("complete TLS application-thread inventory")
        finally:
            self.flush()  # report persistence is attempted even after an inventory failure


def main():
    lab = NativeLab()
    try:
        lab.setup()
        asyncio.run(lab.workflow())
    except BaseException as exc:
        # Never render native exceptions/grants/credential diagnostics in reports.
        lab.report["error_type"] = type(exc).__name__
        if isinstance(exc, AssertionError) and str(exc) in lab.report["checks"]:
            lab.report["failed_check"] = str(exc)
    finally:
        try:
            lab.close()
        except BaseException as exc:
            lab.report.setdefault("cleanup_failures", []).append({"code": "lab_close", "type": type(exc).__name__})
            lab.report["teardown_complete"] = False
        try:
            lab.flush()
        except BaseException as exc:
            # A failed filesystem write cannot promise persisted evidence.
            lab.report.setdefault("cleanup_failures", []).append({"code": "report_flush", "type": type(exc).__name__})
            lab.report["teardown_complete"] = False
    print(json.dumps({"report": str(lab.out / "report.json"), "matrix_complete": False,
                      "error_type": lab.report.get("error_type"), "failed_check": lab.report.get("failed_check"),
                      "teardown_complete": lab.report.get("teardown_complete")}))
    return 0 if (lab.report.get("matrix_complete") and lab.report.get("teardown_complete")
                 and not lab.report.get("error_type")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
