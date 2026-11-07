"""Relay daemon: the only component exposed beyond a participant's own host.

It serves the Gate D relay core over WebSocket. On the VPS it binds loopback
and sits behind a TLS-terminating reverse proxy (Caddy) on :443; for loopback
labs it can terminate TLS itself. Plain ``ws://`` on a non-loopback address is
refused. The route file holds only SHA-256 digests of endpoint credentials.

  python relay_daemon.py provision --routes ROUTES.json --out-dir DIR \
      --link-id L --room-id R --source ID_A --target ID_B
  python relay_daemon.py serve --routes ROUTES.json [--host 127.0.0.1] [--port 8443] \
      [--tls-cert server.pem --tls-key server.key]

``serve`` reloads the route file on SIGHUP. Logs carry identities, link ids and
counters only: never credentials or payloads.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import ipaddress
import json
import logging
import os
import signal
import ssl
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from relay import MAX_FRAME_BYTES, Relay, RelayWebSocketServer, Route

log = logging.getLogger("rooms-relay")


def credential_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(tmp, path)


def load_routes(path: Path) -> list[Route]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("route file must be a JSON list")
    routes = []
    for entry in raw:
        if not isinstance(entry, dict) or set(entry) != {"link_id", "room_id", "source", "target", "credential_sha256"}:
            raise ValueError("route entry has unexpected fields")
        digests = entry["credential_sha256"]
        if not isinstance(digests, dict) or any(not isinstance(v, str) or len(v) != 64 for v in digests.values()):
            raise ValueError("route credential digests are invalid")
        routes.append(Route(entry["source"], entry["target"], entry["room_id"], entry["link_id"], dict(digests)))
    return routes


def provision(routes_path: Path, out_dir: Path, *, link_id: str, room_id: str, source: str, target: str) -> dict:
    """Mint per-endpoint credentials; store digests; write one secret file per endpoint."""
    relay = Relay(credential_digest=credential_digest)
    credentials = relay.provision_link(source=source, target=target, room_id=room_id, link_id=link_id)
    existing = json.loads(routes_path.read_text(encoding="utf-8")) if routes_path.exists() else []
    if any(entry.get("link_id") == link_id for entry in existing):
        raise ValueError(f"link {link_id!r} already provisioned")
    existing.append({"link_id": link_id, "room_id": room_id, "source": source, "target": target,
                     "credential_sha256": {k: credential_digest(v) for k, v in credentials.items()}})
    _write_private(routes_path, json.dumps(existing, indent=2))
    files = {}
    for identity, secret in credentials.items():
        secret_path = out_dir / f"{link_id}.{identity}.credential"
        _write_private(secret_path, secret)
        files[identity] = str(secret_path)
    return {"link_id": link_id, "room_id": room_id, "credential_files": files}


def _server_tls(cert: str | None, key: str | None) -> ssl.SSLContext | None:
    if not cert and not key:
        return None
    if not (cert and key):
        raise ValueError("--tls-cert and --tls-key go together")
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert, key)
    return context


class RelayDaemon:
    def __init__(self, routes_path: Path, *, host: str, port: int, tls: ssl.SSLContext | None) -> None:
        if tls is None and not ipaddress.ip_address(host).is_loopback:
            raise ValueError("plaintext ws:// is allowed on loopback only (put Caddy/TLS in front)")
        self.routes_path = routes_path
        self.host, self.port, self.tls = host, port, tls
        self.relay = Relay(credential_digest=credential_digest)
        self.reload()

    def reload(self) -> int:
        routes = load_routes(self.routes_path)
        self.relay.load_routes(routes)
        log.info(json.dumps({"event": "routes_loaded", "count": len(routes)}))
        return len(routes)

    async def run(self, on_listening=None, *, install_signals: bool = True) -> None:
        from websockets.asyncio.server import serve

        handler = RelayWebSocketServer(self.relay).handler
        self.stop_event = stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        if install_signals:
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(sig, stop.set)
            def reload_from_signal():
                try:
                    self.reload()
                except (ValueError, RuntimeError):
                    log.error(json.dumps({"event": "route_reload_rejected", "action": "restart_required_or_invalid_routes"}))
            loop.add_signal_handler(signal.SIGHUP, reload_from_signal)
        async with serve(handler, self.host, self.port, ssl=self.tls, max_size=MAX_FRAME_BYTES + 1024,
                         ping_interval=20, ping_timeout=20, close_timeout=5) as server:
            bound = next(iter(server.sockets)).getsockname()
            print(json.dumps({"event": "listening", "host": bound[0], "port": bound[1],
                              "tls": self.tls is not None}), flush=True)
            self.bound_port = bound[1]
            if on_listening is not None:
                on_listening()
            await stop.wait()
        print(json.dumps({"event": "stopped"}), flush=True)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    prov = sub.add_parser("provision")
    prov.add_argument("--routes", required=True, type=Path)
    prov.add_argument("--out-dir", required=True, type=Path)
    for name in ("link-id", "room-id", "source", "target"):
        prov.add_argument(f"--{name}", required=True)
    srv = sub.add_parser("serve")
    srv.add_argument("--routes", required=True, type=Path)
    srv.add_argument("--host", default="127.0.0.1")
    srv.add_argument("--port", type=int, default=8443)
    srv.add_argument("--tls-cert")
    srv.add_argument("--tls-key")
    args = parser.parse_args(argv)
    if args.command == "provision":
        print(json.dumps(provision(args.routes, args.out_dir, link_id=args.link_id, room_id=args.room_id,
                                   source=args.source, target=args.target), indent=2))
        return 0
    daemon = RelayDaemon(args.routes, host=args.host, port=args.port, tls=_server_tls(args.tls_cert, args.tls_key))
    asyncio.run(daemon.run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
