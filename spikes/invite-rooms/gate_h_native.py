"""Gate H (native): RoomLink grant lifecycle through the networked transport.

Runs the gate F1-local native flow (real host + target Hermes, relay daemon,
outbound connectors, inner mTLS, both-side firewalls) and then, on the same
live Room, exercises the native grant lifecycle *through the relay hop*:

  H1 capabilities with the invitation grant
  H2 refresh → replacement grant authorizes capabilities
  H3 wrong scope: dispatch scoped to another Room never leaves the host;
     a grant minted for another Room is refused natively for this Room
  H4 stop: an admitted run is stopped through the link (native body ``{}``)
  H5 revoke: revocation is idempotent, later calls get the machine code
     ``room_reauthorization_required``, and an already-admitted run is NOT
     stopped by the revocation (observed owner-side on the target)
  H6 expiry: a 60 s grant is refused with ``invalid_room_grant`` after expiry

The report lands next to the F1-local summary (``gate-h-native-*.json``).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import gate_c_runtime as gc  # noqa: E402
import gate_f1_local  # noqa: E402

LONG_PROMPT = "Write every integer from 1 to 400 in order, separated by single spaces. Output nothing else."
RESULT: dict = {"checks": {}}


def _call(url, method="GET", body=None, auth=None, scheme="HermesRoom", key=None):
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if auth:
        headers["Authorization"] = f"{scheme} {auth}"
    if key:
        headers["Idempotency-Key"] = key
    data = None if body is None else json.dumps(body, separators=(",", ":")).encode()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(urllib.request.Request(url, data=data, method=method, headers=headers), timeout=90) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, {"raw": raw[:200].decode(errors="replace")}


def _code(body):
    error = body.get("error") if isinstance(body, dict) else None
    return error.get("code") if isinstance(error, dict) else None


def _dispatch(scope, catalog, prompt, task_id):
    dispatch = dict(scope, protocol_version=2, task_id=task_id, execution_generation=1, source_event_seq=1,
                    cancellation_scope_id="cancel-" + scope["room_id"], prompt=prompt,
                    prompt_digest=hashlib.sha256(prompt.encode()).hexdigest(),
                    capability_digest=catalog["catalog_digest"],
                    execution_policy_digest=catalog["execution_policy"]["policy_digest"],
                    trace_id="trace-h-" + task_id)
    return {"input": prompt, "hosted_room_dispatch": dispatch}


def _check(name, ok, **detail):
    RESULT["checks"][name] = {"ok": bool(ok), **detail}


async def _link_run_status(adapter, grant, run_id):
    # Room runs are owned by their grant scope: the owner's Bearer key gets 404 by design.
    status, body = await asyncio.to_thread(_call, f"{adapter}/v1/runs/{run_id}", auth=grant)
    return status, (body.get("status") if status == 200 and isinstance(body, dict) else f"http_{status}")


async def _wait_terminal(adapter, grant, run_id, deadline_s=240):
    seen = []
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        _, state = await _link_run_status(adapter, grant, run_id)
        if not seen or seen[-1] != state:
            seen.append(state)
        if state in {"completed", "failed", "interrupted", "cancelled"}:
            return state, seen
        await asyncio.sleep(1)
    return None, seen


async def lifecycle(report, owner_key, target_port):
    base = f"http://127.0.0.1:{target_port}"
    adapter = report["adapter_url"]
    room, catalog = report["room"], report["catalog"]
    owner_id = report["groups_capabilities"]["authority_gateway_id"]
    scope = {"room_id": room["room_id"], "home_install_id": owner_id, "authority_gateway_id": owner_id,
             "authority_epoch": room["authority_epoch"], "member_id": "peer-b",
             "target_install_id": catalog["installation_id"], "target_profile": "default"}
    grants = sorted(gate_f1_local.SECRETS)
    if not grants:
        raise RuntimeError("no room grant observed on the host hop")
    grant = grants[0].decode()
    RESULT["host_grants_observed"] = len(grants)

    # H6 setup first so expiry overlaps the other checks: a 60 s grant for a sibling Room.
    other_room = "h-expiry-" + uuid.uuid4().hex[:8]
    status, minted = await asyncio.to_thread(_call, base + "/v1/room-members/invitations", "POST",
        {"room_id": other_room, "home_install_id": owner_id, "authority_gateway_id": owner_id,
         "authority_epoch": 1, "member_id": "peer-b", "ttl_seconds": 60}, owner_key, "Bearer")
    short_grant, minted_at = minted.get("grant"), time.monotonic()
    _check("H6_setup_short_grant_minted", status == 201 and short_grant, status=status)

    # H1
    status, body = await asyncio.to_thread(_call, adapter + "/v1/room-members/capabilities", auth=grant)
    _check("H1_capabilities_via_relay", status == 200 and body.get("room_id") == scope["room_id"], status=status)

    # H2
    status, body = await asyncio.to_thread(_call, adapter + "/v1/room-members/grants/refresh", "POST",
                                           {"ttl_seconds": 3600}, grant)
    refreshed = body.get("grant") if isinstance(body, dict) else None
    status2, body2 = await asyncio.to_thread(_call, adapter + "/v1/room-members/capabilities", auth=refreshed)
    _check("H2_refresh_via_relay", status == 200 and refreshed and refreshed != grant and status2 == 200,
           refresh_status=status, replacement_present=bool(refreshed), replacement_capabilities_status=status2,
           code=_code(body2))
    if not refreshed:
        refreshed = grant  # keep exercising H4/H5 with the invitation grant

    # H3a: dispatch scoped to another Room is denied by the host firewall before the relay.
    served_before = gate_f1_local.EVIDENCE.get("target_streams_served_live", None)
    bad_scope = dict(scope, room_id="not-this-room")
    status, body = await asyncio.to_thread(_call, adapter + "/v1/runs", "POST",
        _dispatch(bad_scope, catalog, "hi", "h3a-" + uuid.uuid4().hex[:8]), refreshed, key="room:h3a:1")
    _check("H3a_foreign_scope_denied_at_host", status == 403, status=status, served_before=served_before)
    # H3b: grant for another Room presented for this Room's dispatch → refused natively, no run.
    task = "h3b-" + uuid.uuid4().hex[:8]
    status, body = await asyncio.to_thread(_call, adapter + "/v1/runs", "POST",
        _dispatch(scope, catalog, "hi", task), short_grant, key=f"room:{task}:1")
    _check("H3b_foreign_grant_refused_natively", 400 <= status < 500 and not body.get("run_id"),
           status=status, code=_code(body))

    # H4: stop an admitted run through the link.
    task = "h4-" + uuid.uuid4().hex[:8]
    status, body = await asyncio.to_thread(_call, adapter + "/v1/runs", "POST",
        _dispatch(scope, catalog, LONG_PROMPT, task), refreshed, key=f"room:{task}:1")
    stop_run = body.get("run_id")
    await asyncio.sleep(2)
    s_status, s_body = await asyncio.to_thread(_call, f"{adapter}/v1/runs/{stop_run}/stop", "POST", {}, refreshed)
    final, seen = await _wait_terminal(adapter, refreshed, stop_run, 120)
    _check("H4_stop_via_relay", status in (200, 202) and s_status in (200, 202) and final in {"cancelled", "interrupted"},
           dispatch_status=status, stop_status=s_status, final=final, states=seen)

    # H5: revoke while a run is admitted; the run must still finish.
    task = "h5-" + uuid.uuid4().hex[:8]
    status, body = await asyncio.to_thread(_call, adapter + "/v1/runs", "POST",
        _dispatch(scope, catalog, LONG_PROMPT, task), refreshed, key=f"room:{task}:1")
    live_run = body.get("run_id")
    await asyncio.sleep(1)
    _, state_at_revoke = await _link_run_status(adapter, refreshed, live_run)
    r1, _ = await asyncio.to_thread(_call, adapter + "/v1/room-members/grants/revoke", "POST", {}, refreshed)
    r2, _ = await asyncio.to_thread(_call, adapter + "/v1/room-members/grants/revoke", "POST", {}, refreshed)
    c_status, c_body = await asyncio.to_thread(_call, adapter + "/v1/room-members/capabilities", auth=grant)
    d_status, d_body = await asyncio.to_thread(_call, adapter + "/v1/runs", "POST",
        _dispatch(scope, catalog, "hi", "h5-after-" + uuid.uuid4().hex[:6]), refreshed, key="room:h5-after:1")
    # Observe the admitted run after revocation: the target owner re-invites the same scope
    # (owner-local action on the target), which yields a post-fence grant for status reads.
    status_re, reinvite = await asyncio.to_thread(_call, base + "/v1/room-members/invitations", "POST",
        {k: scope[k] for k in ("room_id", "home_install_id", "authority_gateway_id", "authority_epoch", "member_id")}
        | {"ttl_seconds": 600}, owner_key, "Bearer")
    observer = reinvite.get("grant")
    final, seen = await _wait_terminal(adapter, observer, live_run, 300)
    RESULT["H5_reinvite_status"] = status_re
    _check("H5_revoke_idempotent", r1 == 200 and r2 == 200, first=r1, retry=r2)
    _check("H5_post_revoke_machine_code", c_status in (401, 403) and _code(c_body) == "room_reauthorization_required",
           status=c_status, code=_code(c_body))
    _check("H5_post_revoke_dispatch_refused", 400 <= d_status < 500 and not d_body.get("run_id"),
           status=d_status, code=_code(d_body))
    _check("H5_admitted_run_survives_revoke", final == "completed",
           dispatch_status=status, state_at_revoke=state_at_revoke, final=final, states=seen)

    # H6: expiry.
    await asyncio.sleep(max(0.0, 62 - (time.monotonic() - minted_at)))
    status, body = await asyncio.to_thread(_call, base + "/v1/room-members/capabilities", auth=short_grant)
    via_relay = None
    _check("H6_expired_grant_refused", status == 401 and _code(body) == "invalid_room_grant",
           status=status, code=_code(body), via_relay=via_relay,
           note="checked on target loopback: the H6 grant belongs to another Room, the link is bound to this one")


def main() -> int:
    original = gc.workflow

    async def workflow(report, home, target, host_port, target_port, owner_key, ws_key, children, adapter):
        await original(report, home, target, host_port, target_port, owner_key, ws_key, children, adapter)
        try:
            await lifecycle(report, owner_key, target_port)
        except Exception as exc:
            RESULT["error"] = f"{type(exc).__name__}: {exc}"[:500]
        report["gate_H"] = RESULT

    gc.workflow = workflow
    code = gate_f1_local.main()
    passed = code == 0 and "error" not in RESULT and RESULT["checks"] and all(c["ok"] for c in RESULT["checks"].values())
    summary = {"gate_H_native_passed": bool(passed), "f1_local_exit": code, **RESULT,
               "relay_plaintext_leaks": gate_f1_local.EVIDENCE.get("plaintext_leaks"),
               "relay_data_frames": gate_f1_local.EVIDENCE.get("relay_data_frames")}
    out = gc.ROOT / f"gate-h-native-{uuid.uuid4().hex[:8]}.json"
    out.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps({"GATE_H_SUMMARY": summary, "path": str(out)}, indent=2), flush=True)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
