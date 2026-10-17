"""Loopback-only fixed-route HTTP adapter for native RoomLink."""
from __future__ import annotations

import ipaddress
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable
from urllib.parse import urlsplit

from room_firewall import FirewallDenied, Request, RoomFirewall, UpstreamAmbiguous, UpstreamResponse
from state import FixedLinkStore


class _IPv6Server(ThreadingHTTPServer):
    address_family = 10  # socket.AF_INET6; kept local so the module has no platform dependency.


class LoopbackAdapter:
    """Expose only owner-registered `/links/<opaque-id>/...` routes on loopback."""

    def __init__(self, *, state: FixedLinkStore | None = None) -> None:
        self.state = state
        self._routes: dict[str, RoomFirewall] = {}
        self._lock = threading.RLock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def bind(self, link_id: str, firewall: RoomFirewall, *, target_url: str, scope: dict) -> str:
        if not link_id or "/" in link_id or "\\" in link_id:
            raise ValueError("link id must be opaque and path-safe")
        with self._lock:
            server = self._server
            if server is None:
                raise RuntimeError("adapter is not started")
            if self.state is not None:
                self.state.bind(link_id, target_url=target_url, scope=scope)
            self._routes[link_id] = firewall
            return self.url(link_id)

    def restore(self, firewall_factory: Callable[[dict], RoomFirewall]) -> tuple[str, ...]:
        """Restore only persisted bindings, using an owner-supplied firewall factory."""
        if self.state is None:
            return ()
        if self._server is None:
            raise RuntimeError("adapter is not started")
        restored = []
        for record in self.state.all():
            link_id = record.get("link_id")
            if not isinstance(link_id, str) or not link_id:
                raise RuntimeError("persisted link state has no valid link id")
            firewall = firewall_factory(dict(record))
            if not isinstance(firewall, RoomFirewall):
                raise TypeError("firewall factory returned an invalid firewall")
            with self._lock:
                self._routes[link_id] = firewall
            restored.append(link_id)
        return tuple(restored)

    def url(self, link_id: str) -> str:
        if self._server is None:
            raise RuntimeError("adapter is not started")
        host, port = self._server.server_address[:2]
        host = str(host)
        rendered = f"[{host}]" if ":" in host else host
        return f"http://{rendered}:{port}/links/{link_id}"

    def start(self, *, host: str = "127.0.0.1", port: int = 0) -> None:
        address = ipaddress.ip_address(host)
        if not address.is_loopback:
            raise ValueError("adapter may bind only to IPv4/IPv6 loopback")
        server_cls = _IPv6Server if address.version == 6 else ThreadingHTTPServer
        adapter = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format, *args):
                return

            def do_GET(self):
                self._forward("GET")

            def do_POST(self):
                self._forward("POST")

            def _forward(self, method: str):
                parts = urlsplit(self.path)
                if parts.query or parts.fragment or not parts.path.startswith("/links/"):
                    self._reply(404, b'{"error":"unknown link"}')
                    return
                remainder = parts.path[len("/links/"):]
                link_id, separator, child = remainder.partition("/")
                if not separator or not link_id or not child:
                    self._reply(404, b'{"error":"unknown link"}')
                    return
                with adapter._lock:
                    firewall = adapter._routes.get(link_id)
                if firewall is None:
                    self._reply(404, b'{"error":"unknown link"}')
                    return
                length = int(self.headers.get("Content-Length", "0") or "0")
                body = self.rfile.read(length) if length else b""
                # The HTTP server necessarily receives Host/Connection/Content-Length from
                # the native client.  They are transport metadata, never forwarded upstream.
                forwarded_headers = tuple(
                    (name, value) for name, value in self.headers.items()
                    if name.lower() in {"authorization", "accept", "content-type", "idempotency-key"}
                )
                request = Request(method, "/" + child, forwarded_headers, body)
                try:
                    result = firewall.forward(request)
                except UpstreamAmbiguous:
                    # Forwarded, outcome unknown: 5xx keeps native admission ambiguous (retry
                    # with the same Idempotency-Key reconciles); a 4xx would claim non-admission.
                    self._reply(502, b'{"error":{"message":"room link outcome unknown","type":"room_error","code":null}}')
                    return
                except FirewallDenied:
                    self._reply(403, b'{"error":"room request denied"}')
                    return
                self._reply(result.status, result.body, result.headers)

            def _reply(self, status: int, body: bytes, headers=()):
                self.send_response(status)
                seen = set()
                for key, value in headers:
                    lower = key.lower()
                    if lower not in seen:
                        self.send_header(key, value)
                        seen.add(lower)
                if "content-length" not in seen:
                    self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)

        self._server = server_cls((host, port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, name="room-loopback-adapter", daemon=True)
        self._thread.start()

    def close(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
