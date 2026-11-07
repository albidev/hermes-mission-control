"""Gate F1-local: the real native Room flow through the *networked* transport.

Same native workflow as gate_c_runtime (isolated host `hermes serve` + isolated
target gateway, real groups RPC, real native invitation/grant, real model turn),
but the host→target hop no longer goes through the in-process loopback adapter:

  host gateway → loopback adapter → host firewall → host connector ─WSS→ relay
  daemon ←WSS─ target connector → target firewall → native target API

Connectors are outbound-only; inner mTLS (per-owner CA + pinned leaf) runs
inside relay streams. A spy on the relay records every DATA payload so the
report can prove the relay never saw grant, prompt or reply plaintext.

This is still one Mac (loopback); cross-host is F1 proper with the deploy kit.
"""
from __future__ import annotations

import asyncio
import base64
import json
import shutil
import sys
import tempfile
import threading
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import connector  # noqa: E402
import gate_c_runtime as gc  # noqa: E402
import pki  # noqa: E402
import relay as relay_mod  # noqa: E402
import relay_daemon  # noqa: E402

CURRENT: dict = {}  # live NetworkTransport, for fault-injection gates (G)
OBSERVED: list[bytes] = []
SECRETS: set[bytes] = set()
EVIDENCE: dict = {}


class NetworkTransport:
    """Drop-in for LoopbackAdapter: bind() provisions a relay link per Room."""

    def __init__(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="rooms-f1local-"))
        self.loop = asyncio.new_event_loop()
        self.daemon = None
        self.clients: list[connector.RelayClient] = []
        self.host_adapter = None
        self.thread = None
        self._original_route = None

    def start(self, host: str = "127.0.0.1") -> None:
        original = self._original_route = relay_mod.Relay.route

        async def spy(relay, connection, frame):
            if isinstance(frame, dict) and frame.get("type") == "DATA":
                OBSERVED.append(base64.b64decode(frame["payload"]))
            return await original(relay, connection, frame)

        relay_mod.Relay.route = spy
        self.owner_a = pki.init_owner(self.root / "owner-a", "host")
        self.owner_b = pki.init_owner(self.root / "owner-b", "target")
        self.relay_ca = pki.init_owner(self.root / "relay-ca", "relay")
        self.server = pki.issue_server(self.root / "relay-ca", self.root / "relay-tls", "IP:127.0.0.1")
        (self.root / "routes.json").write_text("[]")

    def bind(self, link_id: str, host_firewall, *, target_url: str, scope: dict) -> str:
        prov = relay_daemon.provision(self.root / "routes.json", self.root / "creds", link_id=link_id,
                                      room_id=scope["room_id"], source=scope["home_install_id"],
                                      target=scope["target_install_id"])
        tls = relay_daemon._server_tls(self.server["cert_file"], self.server["key_file"])
        daemon = self.daemon = relay_daemon.RelayDaemon(self.root / "routes.json", host="127.0.0.1", port=0, tls=tls)
        ready = threading.Event()

        def run() -> None:
            asyncio.set_event_loop(self.loop)
            self.loop.run_until_complete(daemon.run(ready.set, install_signals=False))

        self.thread = threading.Thread(target=run, name="relay-daemon", daemon=True)
        self.thread.start()
        if not ready.wait(15):
            raise TimeoutError("relay daemon did not listen")
        common = {"relay_url": f"wss://127.0.0.1:{self.daemon.bound_port}/data",
                  "relay_ca_file": self.relay_ca["ca_file"], "link_id": link_id,
                  "room_id": scope["room_id"], "scope": scope}
        target_cfg = dict(common, identity=scope["target_install_id"], peer_identity=scope["home_install_id"],
                          credential_file=prov["credential_files"][scope["target_install_id"]],
                          target_base=target_url,
                          native_receipt_db=str(self.native_receipt_db),
                          inner_tls={"cert_file": self.owner_b["cert_file"], "key_file": self.owner_b["key_file"],
                                     "peer_ca_file": self.owner_a["ca_file"],
                                     "peer_fingerprint": self.owner_a["fingerprint"]})
        host_cfg = dict(common, identity=scope["home_install_id"], peer_identity=scope["target_install_id"],
                        credential_file=prov["credential_files"][scope["home_install_id"]],
                        inner_tls={"cert_file": self.owner_a["cert_file"], "key_file": self.owner_a["key_file"],
                                   "peer_ca_file": self.owner_b["ca_file"],
                                   "peer_fingerprint": self.owner_b["fingerprint"]})
        target_client, self.target = connector.run_target(target_cfg)
        self.clients.append(target_client)
        self.target_cfg, self.target_client = target_cfg, target_client
        CURRENT["transport"] = self
        # The host side reuses the native firewall built by the gate (same scope); the
        # connector's host role puts the relay hop behind it.
        host_client = connector.build_client(host_cfg)
        host_client.start()
        self.clients.append(host_client)
        self.host_cfg, self.host_client = host_cfg, host_client
        hop = connector.HostConnector(host_client, inner=host_cfg["inner_tls"])

        def forward(request):
            auth = dict(request.headers).get("authorization", "")
            if auth:
                SECRETS.add(auth.split(" ", 1)[-1].encode())
            return hop.forward(request)

        host_firewall._backend = forward  # same firewall instance/scope the gate built
        self.host_firewall = host_firewall
        from adapter import LoopbackAdapter

        self.host_adapter = LoopbackAdapter()
        self.host_adapter.start(host="127.0.0.1")
        url = self.host_adapter.bind(link_id, host_firewall, target_url="relay:" + link_id, scope=scope)
        EVIDENCE.update({"transport": "relay_daemon+connectors", "relay_tls": True,
                         "relay_port": self.daemon.bound_port, "link_id": link_id,
                         "host_incarnation": host_client.incarnation, "target_incarnation": target_client.incarnation,
                         "inner_fingerprints": [self.owner_a["fingerprint"], self.owner_b["fingerprint"]]})
        return url

    def stop_relay(self) -> None:
        if self.daemon is not None and getattr(self.daemon, "stop_event", None) is not None:
            self.loop.call_soon_threadsafe(self.daemon.stop_event.set)
        if self.thread is not None:
            self.thread.join(15)
        if self.thread is not None and self.thread.is_alive():
            raise RuntimeError("relay thread did not stop within 15s")

    def start_relay(self, port=None) -> None:
        old_port = port or (self.daemon.bound_port if self.daemon else None)
        assert old_port is not None
        tls = relay_daemon._server_tls(self.server["cert_file"], self.server["key_file"])
        self.daemon = relay_daemon.RelayDaemon(self.root / "routes.json", host="127.0.0.1", port=old_port, tls=tls)
        ready = threading.Event()

        def run() -> None:
            asyncio.set_event_loop(self.loop)
            self.loop.run_until_complete(self.daemon.run(ready.set, install_signals=False))

        self.thread = threading.Thread(target=run, name="relay-daemon-restarted", daemon=True)
        self.thread.start()
        if not ready.wait(15):
            raise TimeoutError("restarted relay daemon did not listen")

    def restart_relay(self) -> dict:
        """Stop the relay daemon and start a fresh one on the same port.
        and new incarnation/port metadata so callers can prove a genuine
        restart (not a no-op) and observe connector reconnection.
        """
        old_port = self.daemon.bound_port if self.daemon else None
        old_incarnations = [c.incarnation for c in self.clients]
        # Signal stop on the relay event loop and join the daemon thread.
        if self.daemon is not None and getattr(self.daemon, "stop_event", None) is not None:
            self.loop.call_soon_threadsafe(self.daemon.stop_event.set)
        if self.thread is not None:
            self.thread.join(15)
        if self.thread is not None and self.thread.is_alive():
            raise RuntimeError("relay thread did not stop within 15s")
        # Start a fresh daemon on the same port reusing the existing event loop.
        tls = relay_daemon._server_tls(self.server["cert_file"], self.server["key_file"])
        self.daemon = relay_daemon.RelayDaemon(self.root / "routes.json", host="127.0.0.1", port=old_port, tls=tls)
        ready = threading.Event()

        def run() -> None:
            asyncio.set_event_loop(self.loop)
            self.loop.run_until_complete(self.daemon.run(ready.set, install_signals=False))

        self.thread = threading.Thread(target=run, name="relay-daemon-restarted", daemon=True)
        self.thread.start()
        if not ready.wait(15):
            raise TimeoutError("restarted relay daemon did not listen")
        return {"old_port": old_port, "new_port": self.daemon.bound_port,
                "old_incarnations": old_incarnations}

    def restart_host_connector(self) -> dict:
        """Stop the host connector and start a fresh one with the same config.

        The host firewall backend is rewired to the new HostConnector so that
        subsequent dispatches go through the restarted connector.  Returns
        old and new incarnation for proof of genuine restart.
        """
        old = self.host_client
        old_incarnation = old.incarnation
        # Stop the old host connector (thread-safe).
        old.stop()
        # Build and start a fresh client with the same config.
        fresh = connector.build_client(self.host_cfg)
        fresh.start()
        self.clients.append(fresh)
        self.host_client = fresh
        # Rewire the host firewall backend to the new HostConnector.
        hop = connector.HostConnector(fresh, inner=self.host_cfg["inner_tls"])

        def forward(request):
            auth = dict(request.headers).get("authorization", "")
            if auth:
                SECRETS.add(auth.split(" ", 1)[-1].encode())
            return hop.forward(request)

        self.host_firewall._backend = forward
        return {"old_incarnation": old_incarnation, "new_incarnation": fresh.incarnation}

    def close(self) -> None:
        EVIDENCE["target_streams_served"] = getattr(getattr(self, "target", None), "served", None)
        failures = []

        def attempt(code, action):
            try:
                action()
            except BaseException as exc:
                failures.append({"code": code, "type": type(exc).__name__})

        try:
            if self.host_adapter is not None:
                attempt("adapter_close", self.host_adapter.close)
            for client in self.clients:
                attempt("client_stop", client.stop)
            if self.daemon is not None and getattr(self.daemon, "stop_event", None) is not None and not self.loop.is_closed():
                attempt("relay_stop", lambda: self.loop.call_soon_threadsafe(self.daemon.stop_event.set))
            if self.thread is not None:
                attempt("relay_join", lambda: self.thread.join(15))
            relay_alive = self.thread is not None and self.thread.is_alive()
            if relay_alive:
                failures.append({"code": "relay_thread_alive", "type": "TimeoutError"})
            elif not self.loop.is_closed():
                attempt("relay_asyncgens", lambda: self.loop.run_until_complete(
                    asyncio.wait_for(self.loop.shutdown_asyncgens(), 1)))
                attempt("relay_loop_close", self.loop.close)
        finally:
            if self._original_route is not None:
                relay_mod.Relay.route = self._original_route
                self._original_route = None
            EVIDENCE["relay_loop_closed"] = self.loop.is_closed()
            EVIDENCE["connector_threads_stopped"] = all(c._thread is None or not c._thread.is_alive() for c in self.clients)
            EVIDENCE["thread_inventory_complete"] = all(hasattr(c, "application_threads") for c in self.clients)
            EVIDENCE["application_threads_alive"] = [
                {"connector": c.identity, "name": t.name, "ident": t.ident}
                for c in self.clients for t in getattr(c, "application_threads", ())]
            EVIDENCE["transport_cleanup_failures"] = failures
            if (EVIDENCE["relay_loop_closed"] and EVIDENCE["connector_threads_stopped"]
                    and EVIDENCE["thread_inventory_complete"] and not EVIDENCE["application_threads_alive"]):
                attempt("scratch_remove", lambda: shutil.rmtree(self.root))
        if failures:
            raise RuntimeError("network_teardown_failed:" + ",".join(x["code"] + "/" + x["type"] for x in failures))


def main() -> int:
    gc.LoopbackAdapter = NetworkTransport
    report_holder: dict = {}
    original_workflow = gc.workflow

    async def workflow(report, *args):
        report_holder["report"] = report
        try:
            await original_workflow(report, *args)
        finally:
            blob = b"".join(OBSERVED)
            reply = (report.get("remote_reply") or {}).get("payload", {}).get("text", "")
            markers = {"prompt_marker": b"LAB_READY", "dispatch_field": b"hosted_room_dispatch",
                       "auth_scheme": b"HermesRoom "}
            leaks = {name: marker in blob for name, marker in markers.items()}
            leaks["grant"] = bool(SECRETS) and any(secret in blob for secret in SECRETS)
            EVIDENCE["grant_seen_by_host_hop"] = bool(SECRETS)
            if reply:
                leaks["reply_text"] = reply.encode() in blob
            EVIDENCE.update({"relay_data_frames": len(OBSERVED), "relay_data_bytes": len(blob),
                             "plaintext_leaks": leaks, "relay_saw_ciphertext_only": bool(OBSERVED) and not any(leaks.values())})
            report["gate_F1_local"] = dict(EVIDENCE)
            report["gate_F1_local_passed"] = False  # set by the caller after gate C criteria

    gc.workflow = workflow
    code = gc.main()
    report = report_holder.get("report", {})
    passed = code == 0 and EVIDENCE.get("relay_saw_ciphertext_only", False)
    summary = {"gate_F1_local_passed": passed, "gate_C_criteria_passed": code == 0,
               "evidence": EVIDENCE, "report_path": report.get("report_path"),
               "error": report.get("error"), "run_id": str(uuid.uuid4())[:8]}
    out = gc.ROOT / f"gate-f1-local-{summary['run_id']}.json"
    out.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps({"F1_LOCAL_SUMMARY": summary, "summary_path": str(out)}, indent=2), flush=True)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
