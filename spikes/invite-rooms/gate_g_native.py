"""Gate G (native): fault injection on the networked Room transport.

Same live lab as gate F1-local (real host + target Hermes, relay daemon,
outbound connectors, inner mTLS), then on that Room:

  G1 lost reply AFTER admission → host sees 5xx (ambiguous, not "not admitted");
     retry with the same task/generation/Idempotency-Key → same run_id,
     ``replayed=true``, exactly one durable receipt on the target
  G2 target offline BEFORE admission → request fails, no run, no queued replay;
     after the target reconnects (new incarnation) the retry is admitted once
  G3 target reconnect MID-RUN → the admitted run keeps running natively; after
     reconnect the host reads its terminal status through the link; one receipt
  G4 stale incarnation / authority renewal: reconnecting never renews or mints a
     grant (grant count on the host hop unchanged) and never replays prompts

Relay restart, slow readers/backpressure, quota and stale-incarnation frames are
covered by tests/test_relay*.py and tests/test_net_loopback.py (real processes).
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import connector  # noqa: E402
import gate_c_runtime as gc  # noqa: E402
import gate_f1_local  # noqa: E402
from gate_h_native import _call, _code, _dispatch  # noqa: E402

LONG_PROMPT = "Write every integer from 1 to 400 in order, separated by single spaces. Output nothing else."
RESULT: dict = {"checks": {}}


def _check(name, ok, **detail):
    RESULT["checks"][name] = {"ok": bool(ok), **detail}


def _receipts(target_home: Path, key: str) -> list:
    db = target_home / "runs_idempotency.db"
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5) as conn:
        return conn.execute("SELECT run_id FROM run_idempotency WHERE idempotency_key=?", (key,)).fetchall()


async def _poll_link(adapter, grant, run_id, deadline_s=300):
    states, deadline = [], time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        status, body = await asyncio.to_thread(_call, f"{adapter}/v1/runs/{run_id}", auth=grant)
        state = body.get("status") if status == 200 else f"http_{status}:{_code(body) or body.get('error')}"
        if not states or states[-1] != state:
            states.append(state)
        if state in {"completed", "failed", "interrupted", "cancelled"}:
            return state, states
        await asyncio.sleep(1)
    return None, states


def _restart_target_client(transport, *, offline_seconds: float = 0.0):
    old = transport.target_client
    old_incarnation = old.incarnation
    old.stop()
    time.sleep(offline_seconds)
    fresh = connector.build_client(transport.target_cfg, on_new_stream=transport.target.serve)
    fresh.start()
    transport.clients.append(fresh)
    transport.target_client = fresh
    return old_incarnation, fresh.incarnation


async def faults(report, target_home: Path):
    transport = gate_f1_local.CURRENT["transport"]
    adapter = report["adapter_url"]
    room, catalog = report["room"], report["catalog"]
    owner_id = report["groups_capabilities"]["authority_gateway_id"]
    scope = {"room_id": room["room_id"], "home_install_id": owner_id, "authority_gateway_id": owner_id,
             "authority_epoch": room["authority_epoch"], "member_id": "peer-b",
             "target_install_id": catalog["installation_id"], "target_profile": "default"}
    grant = sorted(gate_f1_local.SECRETS)[0].decode()
    grants_before = len(gate_f1_local.SECRETS)

    # G1: lose the reply after native admission.
    firewall = transport.target.firewall
    real_forward = firewall.forward
    dropped = []

    def lossy(request):
        response = real_forward(request)
        if request.method == "POST" and request.target == "/v1/runs" and not dropped:
            dropped.append(response.status)
            raise ConnectionResetError("fault: reply lost after admission")
        return response

    firewall.forward = lossy
    task = "g1-" + uuid.uuid4().hex[:8]
    key = f"room:{task}:1"
    body = _dispatch(scope, catalog, "Reply only G1_OK.", task)
    first_status, first_body = await asyncio.to_thread(_call, adapter + "/v1/runs", "POST", body, grant, key=key)
    firewall.forward = real_forward
    retry_status, retry_body = await asyncio.to_thread(_call, adapter + "/v1/runs", "POST", body, grant, key=key)
    rows = _receipts(target_home, key)
    _check("G1_lost_reply_is_ambiguous_then_replayed",
           bool(dropped) and first_status >= 500 and retry_status in (200, 202)
           and retry_body.get("replayed") is True and len(rows) == 1 and rows[0][0] == retry_body.get("run_id"),
           native_status_behind_fault=dropped, first_status=first_status, first_code=_code(first_body),
           retry_status=retry_status, replayed=retry_body.get("replayed"), receipts=len(rows))

    # G2: target offline before admission.
    transport.target_client.stop()
    task = "g2-" + uuid.uuid4().hex[:8]
    key = f"room:{task}:1"
    body = _dispatch(scope, catalog, "Reply only G2_OK.", task)
    offline_status, offline_body = await asyncio.to_thread(_call, adapter + "/v1/runs", "POST", body, grant, key=key)
    rows_offline = _receipts(target_home, key)
    old_inc = transport.target_client.incarnation
    fresh = connector.build_client(transport.target_cfg, on_new_stream=transport.target.serve)
    fresh.start()
    transport.clients.append(fresh)
    transport.target_client = fresh
    await asyncio.sleep(2)
    rows_after_reconnect = _receipts(target_home, key)  # no offline replay on reconnect
    retry_status, retry_body = await asyncio.to_thread(_call, adapter + "/v1/runs", "POST", body, grant, key=key)
    rows_final = _receipts(target_home, key)
    _check("G2_offline_before_admission_no_run_no_replay",
           offline_status >= 500 and not rows_offline and not rows_after_reconnect
           and retry_status in (200, 202) and not retry_body.get("replayed") and len(rows_final) == 1
           and fresh.incarnation == (old_inc or 0) + 1,
           offline_status=offline_status, offline_code=_code(offline_body), receipts_offline=len(rows_offline),
           receipts_after_reconnect=len(rows_after_reconnect), retry_status=retry_status,
           replayed=retry_body.get("replayed"), incarnations=[old_inc, fresh.incarnation])

    # Lab-only probe: keep raw native non-2xx bodies (codes the firewall withholds) for diagnosis.
    native_rejects = RESULT.setdefault("native_rejects", [])
    real_backend = firewall._backend

    import room_firewall as rf

    def _shape(value, depth=0):
        if isinstance(value, dict):
            return {k: _shape(v, depth + 1) for k, v in list(value.items())[:40]} if depth < 4 else "dict"
        if isinstance(value, list):
            return [f"list[{len(value)}]"] + ([_shape(value[0], depth + 1)] if value else [])
        return f"{type(value).__name__}:{len(value)}" if isinstance(value, str) else type(value).__name__

    def probing_backend(request):
        response = real_backend(request)
        try:
            rf._sanitize_response(response, rf._ROUTE_RESPONSE_FIELDS.get(getattr(request, "path", ""), frozenset()))
        except rf.FirewallDenied as exc:
            try:
                shape = _shape(json.loads(response.body))
            except ValueError:
                shape = "non-json"
            native_rejects.append({"kind": "sanitizer", "reason": str(exc), "status": response.status, "shape": shape})
        if response.status >= 400 and len(native_rejects) < 10:
            native_rejects.append({"method": request.method, "path": str(getattr(request, "path", getattr(request, "target", "")))[:80],
                                   "status": response.status, "body": response.body[:400].decode(errors="replace")})
        return response

    firewall._backend = probing_backend

    # G3: target reconnect mid-run.
    task = "g3-" + uuid.uuid4().hex[:8]
    key = f"room:{task}:1"
    status, body = await asyncio.to_thread(_call, adapter + "/v1/runs", "POST",
                                           _dispatch(scope, catalog, LONG_PROMPT, task), grant, key=key)
    run_id = body.get("run_id")
    await asyncio.sleep(2)
    mid_http, mid_state = await asyncio.to_thread(_call, f"{adapter}/v1/runs/{run_id}", auth=grant)
    RESULT["G3_mid_probe"] = {"http": mid_http, "body": mid_state}
    incarnations = await asyncio.to_thread(_restart_target_client, transport, offline_seconds=3)
    final, states = await _poll_link(adapter, grant, run_id)
    rows = _receipts(target_home, key)
    _check("G3_reconnect_mid_run_keeps_single_run",
           status in (200, 202) and final == "completed" and len(rows) == 1,
           dispatch_status=status, state_before_restart=mid_state.get("status"), final=final, states=states,
           incarnations=list(incarnations), receipts=len(rows))

    withheld = [r for r in native_rejects if r.get("kind") == "sanitizer"]
    _check("G5_no_native_response_withheld_by_firewall", not withheld, withheld=withheld[:2])

    # G4: reconnects never renewed authority.
    _check("G4_no_reconnect_authority_renewal", len(gate_f1_local.SECRETS) == grants_before,
           grants_before=grants_before, grants_after=len(gate_f1_local.SECRETS))


def main() -> int:
    original = gc.workflow

    async def workflow(report, home, target, host_port, target_port, owner_key, ws_key, children, adapter):
        await original(report, home, target, host_port, target_port, owner_key, ws_key, children, adapter)
        try:
            await faults(report, target)
        except Exception as exc:
            RESULT["error"] = f"{type(exc).__name__}: {exc}"[:500]
        report["gate_G"] = RESULT

    gc.workflow = workflow
    code = gate_f1_local.main()
    passed = code == 0 and "error" not in RESULT and RESULT["checks"] and all(c["ok"] for c in RESULT["checks"].values())
    summary = {"gate_G_native_passed": bool(passed), "f1_local_exit": code, **RESULT,
               "relay_plaintext_leaks": gate_f1_local.EVIDENCE.get("plaintext_leaks")}
    out = gc.ROOT / f"gate-g-native-{uuid.uuid4().hex[:8]}.json"
    out.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps({"GATE_G_SUMMARY": summary, "path": str(out)}, indent=2), flush=True)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
