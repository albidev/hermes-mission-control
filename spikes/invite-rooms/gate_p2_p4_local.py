"""P2/P4 LOCAL integration: private store -> native Room -> first remote reply.

Uses isolated native host/target and the real F1-local WSS + mTLS transport.
NOT cross-host acceptance. Invitation/consent actions call the unimported local
store directly: remote lobby-control ingress and UI are deliberately not wired.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
import time
import uuid

import aiohttp

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1] / "server"))
import gate_f1_local as net
from room_invite_store import InviteError, RoomInviteStore
from room_lobby_service import PeerMaterial, RoomLobbyService
from room_firewall import RoomFirewall

gc = net.gc
REPORT = {}


async def ready_socket(session, base, host_port, owner_key, ws_key, children):
    deadline = time.monotonic() + 150
    while time.monotonic() < deadline:
        if any(p.poll() is not None for p in children):
            raise RuntimeError("owned lab exited before readiness")
        try:
            await asyncio.to_thread(gc.helper.request, base, "/v1/models", owner_key)
            ws = await session.ws_connect(f"ws://127.0.0.1:{host_port}/api/ws?token={ws_key}")
            return ws
        except (OSError, RuntimeError, aiohttp.ClientError):
            await asyncio.sleep(.5)
    raise TimeoutError("native readiness")


async def workflow(report, home, target, host_port, target_port, owner_key, ws_key, children, adapter):
    REPORT.update({"report": report})
    report["gate_P2_P4_local_passed"] = False
    report["limitations"] = ["one machine / loopback, not F1 cross-host", "no public/admin HTTP routes or UI wired",
                              "lobby-control invitation and consent actions are direct private store calls"]
    checks = report["product_checks"] = {}
    base = f"http://127.0.0.1:{target_port}"
    # S1's target-side firewall verifies durable native receipts. This private
    # path is owned by this fresh lab target, never a worker/production DB.
    adapter.native_receipt_db = target / "runs_idempotency.db"
    try:
        async with aiohttp.ClientSession(trust_env=False, timeout=aiohttp.ClientTimeout(total=120)) as session:
            ws = await ready_socket(session, base, host_port, owner_key, ws_key, children)
            async with ws:
                async def rpc(method, params):
                    return await gc.rpc(ws, method, params)

                caps = await rpc("groups.capabilities", {})
                assert caps["driver"] is True
                authority = caps["authority_gateway_id"]
                target_id = report["identity"]["second_gateway_id"]
                assert authority == report["identity"]["gateway_id"] and authority != target_id
                store = RoomInviteStore(home / "mc-invites.sqlite")
                roster = [
                    {"member_id": "local-a", "profile": "default", "handle": "local"},
                    {"member_id": "peer-b", "profile": "default", "handle": "peer",
                     "target": {"kind": "peer", "peer_id": "target-b", "installation_id": target_id, "profile": "default"}},
                ]
                lobby = store.create_lobby(name="P2/P4 local native integration", roster=roster)
                lobby_id, room_id = lobby["lobby_id"], lobby["planned_room_id"]
                binding = {"endpoint_id": "target-b", "fingerprint": adapter.owner_b["fingerprint"],
                           "target_install_id": target_id, "target_profile": "default"}
                issued = store.issue_invite(lobby_id, "peer-b", invitee_label="Isolated target owner")
                preview = store.preview(issued.invite_id, issued.secret)
                checks["preview_reveals_no_secret"] = issued.secret not in json.dumps(preview)
                material = {}
                native_mutations = []

                async def private_material(peer):
                    assert peer["fingerprint"] == binding["fingerprint"]
                    return material["peer"]

                async def counted_rpc(method, params):
                    if method in {"groups.create", "groups.peer.register", "groups.peer.invite", "groups.send"}:
                        native_mutations.append(method)
                    return await rpc(method, params)

                service = RoomLobbyService(store, counted_rpc, private_material)
                for stage in ("preview", "redeemed", "host_approved"):
                    if stage == "redeemed":
                        store.redeem(lobby_id, issued.invite_id, issued.secret, binding=binding)
                    if stage == "host_approved":
                        store.decide_host(lobby_id, issued.invite_id, binding=binding, approved=True)
                    try:
                        await service.activate(lobby_id)
                    except InviteError as exc:
                        assert str(exc) == "consent_required"
                    else:
                        raise AssertionError("native mutation before explicit consent")
                    checks[stage + "_no_native_mutation"] = not native_mutations
                store.consent_joining(lobby_id, issued.invite_id, binding=binding, consented=True)
                assert store.get_redemption(lobby_id, issued.invite_id)["status"] == "ready"
                # This is the target's OWNER-authenticated API, after both explicit consents.
                _, _, invitation = await asyncio.to_thread(gc.helper.request, base, "/v1/room-members/invitations", owner_key,
                    {"room_id": room_id, "home_install_id": authority, "authority_gateway_id": authority,
                     "authority_epoch": 1, "member_id": "peer-b", "ttl_seconds": 600})
                grant, catalog = invitation["grant"], invitation["catalog"]
                assert catalog["installation_id"] == target_id
                scope = {"room_id": room_id, "home_install_id": authority, "authority_gateway_id": authority,
                         "authority_epoch": 1, "member_id": "peer-b", "target_install_id": target_id, "target_profile": "default"}
                url = adapter.bind("product-" + uuid.uuid4().hex,
                    RoomFirewall(expected_scope=scope, backend=gc.native_backend(base, grant), verify_native=lambda *_: True),
                    target_url=base, scope=scope)
                material["peer"] = PeerMaterial(binding=binding, catalog=catalog, grant=grant, adapter_url=url)
                room = await service.activate(lobby_id)
                resumed = RoomLobbyService(RoomInviteStore(store.path), counted_rpc, private_material)
                repeated = await resumed.activate(lobby_id)
                checks["restart_retains_room_id_and_roster"] = repeated["room_id"] == room["room_id"] == room_id and repeated["members"] == room["members"]
                native_rows = gc.read_db(home / "shared-state.db", "SELECT room_id FROM hosted_rooms WHERE room_id=?", (room_id,))
                checks["one_native_room"] = len(native_rows) == 1
                registered = gc.read_db(store.path, "SELECT member_id,state FROM peer_links WHERE lobby_id=?", (lobby_id,))
                checks["registration_readback_persisted"] = registered == [{"member_id": "peer-b", "state": "registered"}]
                before = await rpc("groups.log", {"room_id": room_id, "since_seq": 0, "limit": 500})
                checks["no_run_or_message_on_activation"] = before["events"] == [] and not gc.read_db(home / "shared-state.db", "SELECT run_id FROM hosted_room_remote_runs WHERE room_id=?", (room_id,))
                event_id = "product-owner-" + uuid.uuid4().hex
                params = {"room_id": room_id, "event_id": event_id,
                          "payload": {"text": "@peer Reply only LAB_READY, without mentioning other members. For any subsequent round reply PASS.", "thread_id": "product-1"}}
                sent = await rpc("groups.send", params)
                replay = await rpc("groups.send", params)
                checks["send_idempotency"] = (sent["event"]["event_id"], sent["event"]["seq"]) == (replay["event"]["event_id"], replay["event"]["seq"])
                deadline = time.monotonic() + 180
                while time.monotonic() < deadline:
                    page = await rpc("groups.log", {"room_id": room_id, "since_seq": 0, "limit": 500})
                    replies = [e for e in page["events"] if e["kind"] == "message.member" and e["payload"].get("member_id") == "peer-b"]
                    if replies:
                        break
                    if any(e["kind"] in {"turn.failed", "turn.deferred"} for e in page["events"]):
                        raise RuntimeError("native turn failed/deferred")
                    await asyncio.sleep(.5)
                else:
                    raise TimeoutError("first native reply")
                await rpc("groups.stop", {"room_id": room_id, "cancel_id": "bounded-" + room_id})
                latest = await rpc("groups.log", {"room_id": room_id, "since_seq": 0, "limit": 500})
                original = [(e["seq"], e["event_id"]) for e in page["events"]]
                final = [(e["seq"], e["event_id"]) for e in latest["events"]]
                checks["canonical_ids_order_preserved"] = final[:len(original)] == original and len(final) == len({i for _, i in final}) and not latest["has_more"]
                checks["real_remote_reply"] = replies[0]["payload"]["text"] == "LAB_READY"
                blob = b"".join(net.OBSERVED)
                checks["relay_observed_ciphertext_without_secrets"] = bool(blob) and not any(m in blob for m in (b"LAB_READY", b"hosted_room_dispatch", b"HermesRoom ", grant.encode(), issued.secret.encode()))
                stored = b"".join(p.read_bytes() for p in store.path.parent.glob(store.path.name + "*") if p.is_file())
                checks["mc_store_has_neither_invite_secret_nor_native_grant"] = grant.encode() not in stored and issued.secret.encode() not in stored
                report.update({"lobby_id": lobby_id, "room_id": room_id, "native_mutations": native_mutations,
                               "canonical_log": latest, "relay_data_frames": len(net.OBSERVED), "target_install_id": target_id})
                report["gate_P2_P4_local_passed"] = all(checks.values())
                # Existing isolated launcher uses this return flag; this is a new product-local gate.
                report["gate_C_passed"] = report["gate_P2_P4_local_passed"]
    except Exception as exc:
        report["product_error_type"] = type(exc).__name__
        raise RuntimeError("product-local gate failed: " + type(exc).__name__) from None


def main():
    gc.workflow, gc.LoopbackAdapter = workflow, net.NetworkTransport
    code = gc.main()
    report = REPORT.get("report", {})
    passed = code == 0 and report.get("gate_P2_P4_local_passed") is True
    summary = {"gate_P2_P4_local_passed": passed, "checks": report.get("product_checks", {}),
               "teardown_complete": report.get("teardown_complete"), "report_path": report.get("report_path"),
               "error_type": report.get("product_error_type"), "limitations": report.get("limitations")}
    path = gc.ROOT / ("gate-p2-p4-local-" + uuid.uuid4().hex[:8] + ".json")
    path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps({"PRODUCT_LOCAL_SUMMARY": summary, "path": str(path)}, indent=2), flush=True)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
