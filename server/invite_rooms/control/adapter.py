"""Loopback RoomLink adapter backed by an owner-registered pinned control link.

The listener is deliberately inert until :meth:`start` is called. Product routes
use the existing deterministic MaterialScope adapter id and retain the pairing
endpoint/generation separately. Request data never selects an upstream. Each
request uses the real pinned connector exactly once. Explicit injected
connectors and endpoint-shaped routes are retained for the older spike fixtures.
"""
from __future__ import annotations

import asyncio
import inspect
import ipaddress
import json
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

from .firewall import FirewallDenied, Request, RoomFirewall, UpstreamAmbiguous, UpstreamResponse
from .state import FixedLinkStore


class _IPv6Server(ThreadingHTTPServer):
    address_family = 10


_GENERIC_404 = b'{"error":"unavailable"}'
_GENERIC_DENIED = b'{"error":"room request denied"}'
_GENERIC_AMBIGUOUS = b'{"error":{"message":"room link outcome unknown","type":"room_error","code":null}}'


def _invoke(connector: Callable[..., Any], request: Request, admit: Callable[[], None]) -> Any:
    """Call either the one-argument or admission-aware connector once."""
    try:
        parameters = inspect.signature(connector).parameters.values()
        accepts_two = any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in parameters) or len(list(parameters)) >= 2
    except (TypeError, ValueError):
        accepts_two = True
    return connector(request, admit) if accepts_two else connector(request)


@dataclass
class _Link:
    endpoint_id: str
    generation: int
    firewall: RoomFirewall
    connector: Callable[..., Any]
    closed: bool = False

    def admit(self) -> None:
        if self.closed:
            raise FirewallDenied("link generation changed")

    def forward(self, request: Request) -> UpstreamResponse:
        # This check is intentionally adjacent to connector invocation.  A
        # connector may also accept the callback and repeat it immediately
        # before its first TLS frame, which is the durable R1-05 boundary.
        self.admit()
        try:
            result = _invoke(self.connector, request, self.admit)
            if inspect.isawaitable(result):
                result = asyncio.run(result)
        except UpstreamAmbiguous:
            raise
        except FirewallDenied:
            raise
        except Exception as exc:
            raise UpstreamAmbiguous("pinned link failed") from exc
        self.admit()
        if not isinstance(result, UpstreamResponse):
            raise UpstreamAmbiguous("invalid pinned response")
        return result


class LoopbackAdapter:
    """Expose fixed ``/links/<adapter_id>/...`` routes on loopback only."""

    def __init__(self, *, authority: Callable[[str], Any] | None = None,
                 state: FixedLinkStore | None = None,
                 max_body_bytes: int = 16 * 1024 * 1024) -> None:
        if type(max_body_bytes) is not int or max_body_bytes < 1:
            raise ValueError("invalid adapter body limit")
        self.authority = authority
        self.state = state
        self.max_body_bytes = max_body_bytes
        self._routes: dict[str, _Link] = {}
        self._lock = threading.RLock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self, *, host: str = "127.0.0.1", port: int = 0) -> None:
        address = ipaddress.ip_address(host)
        if not address.is_loopback:
            raise ValueError("adapter may bind only to IPv4/IPv6 loopback")
        if self._server is not None:
            raise RuntimeError("adapter already started")
        server_cls = _IPv6Server if address.version == 6 else ThreadingHTTPServer
        adapter = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            def log_message(self, format, *args):
                return
            def do_GET(self): self._forward("GET")
            def do_POST(self): self._forward("POST")

            def _forward(self, method: str):
                parts = urlsplit(self.path)
                if parts.query or parts.fragment or not parts.path.startswith("/links/"):
                    self._reply(404, _GENERIC_404); return
                endpoint, sep, child = parts.path[len("/links/"):].partition("/")
                if not sep or not endpoint or not child:
                    self._reply(404, _GENERIC_404); return
                with adapter._lock:
                    link = adapter._routes.get(endpoint)
                if link is None or link.closed:
                    self._reply(404, _GENERIC_404); return
                try:
                    length = int(self.headers.get("Content-Length", "0") or "0")
                except (TypeError, ValueError):
                    self._reply(403, _GENERIC_DENIED); return
                if length < 0 or length > adapter.max_body_bytes:
                    self._reply(403, _GENERIC_DENIED); return
                body = self.rfile.read(length) if length else b""
                forwarded = tuple((n, v) for n, v in self.headers.items()
                                  if n.lower() in {"authorization", "accept", "content-type", "idempotency-key"})
                request = Request(method, "/" + child, forwarded, body)
                try:
                    # Authority is read on every request, not cached at route
                    # creation.  Any generation mismatch is generic 404.
                    if adapter.authority is not None:
                        current = adapter.authority(link.endpoint_id)
                        generation = getattr(current, "generation", None)
                        if isinstance(current, Mapping): generation = current.get("generation")
                        if type(generation) is not int or generation != link.generation:
                            adapter.deregister(link.endpoint_id, generation=link.generation)
                            self._reply(404, _GENERIC_404); return
                    result = link.firewall.forward(request)
                except UpstreamAmbiguous:
                    self._reply(502, _GENERIC_AMBIGUOUS); return
                except FirewallDenied:
                    self._reply(403, _GENERIC_DENIED); return
                except Exception:
                    self._reply(404, _GENERIC_404); return
                self._reply(result.status, result.body, result.headers)

            def _reply(self, status: int, body: bytes, headers=()):
                self.send_response(status)
                seen = set()
                for key, value in headers:
                    lower = key.lower()
                    if lower not in seen:
                        self.send_header(key, value); seen.add(lower)
                if "content-length" not in seen:
                    self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers(); self.wfile.write(body)

        self._server = server_cls((host, port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever,
                                         name="room-loopback-adapter", daemon=True)
        self._thread.start()

    def register(self, endpoint_id: str, generation: int, *,
                 firewall: RoomFirewall, connector: Callable[..., Any],
                 adapter_id: str | None = None) -> str:
        if not isinstance(endpoint_id, str) or not endpoint_id or "/" in endpoint_id or "\\" in endpoint_id:
            raise ValueError("endpoint id must be path-safe")
        if type(generation) is not int or generation < 1:
            raise ValueError("invalid pairing generation")
        if not isinstance(firewall, RoomFirewall) or not callable(connector):
            raise TypeError("invalid link registration")
        route_id = adapter_id if adapter_id is not None else endpoint_id
        if not isinstance(route_id, str) or not route_id or "/" in route_id or "\\" in route_id:
            raise ValueError("adapter id must be path-safe")
        # The connector supplied at registration is authoritative. Rebuild the
        # firewall around it so a caller cannot accidentally retain a second
        # upstream backend on the same route.
        scope = dict(getattr(firewall, "_scope", {}))
        verifier = getattr(firewall, "_verify_native", None)
        timeout = getattr(firewall, "_backend_timeout_seconds", 30.0)
        admitted = getattr(firewall, "_on_admitted", None)
        link_ref: list[_Link] = []
        def backend(request):
            if not link_ref:
                raise FirewallDenied("link unavailable")
            link = link_ref[0]
            link.admit()
            try:
                result = _invoke(connector, request, link.admit)
                if inspect.isawaitable(result):
                    result = asyncio.run(result)
            except (FirewallDenied, UpstreamAmbiguous):
                raise
            except Exception as exc:
                raise UpstreamAmbiguous("pinned link failed") from exc
            link.admit()
            if not isinstance(result, UpstreamResponse):
                raise UpstreamAmbiguous("invalid pinned response")
            return result
        firewall = RoomFirewall(expected_scope=scope, backend=backend,
                                verify_native=verifier, backend_timeout_seconds=timeout,
                                on_admitted=admitted)
        with self._lock:
            if self._server is None:
                raise RuntimeError("adapter is not started")
            old = self._routes.get(route_id)
            if old is not None and adapter_id is not None:
                raise ValueError("scoped route already registered")
            if old is not None:
                old.closed = True
            link = _Link(endpoint_id, generation, firewall, connector)
            link_ref.append(link)
            self._routes[route_id] = link
            return self.url(route_id)

    def register_link(self, endpoint_id: str, generation: int, *,
                      expected_scope: Mapping[str, Any],
                      verify_native: Callable[[str, Mapping[str, Any]], bool],
                      connector: Callable[..., Any] | None = None,
                      binding=None, identity=None, authority=None, material_scope=None,
                      backend_timeout_seconds: float = 30.0,
                      on_admitted: Callable[[str, Mapping[str, Any]], Any] | None = None) -> str:
        """Register a real fixed link, or an explicit transport fixture connector.

        Product callers supply binding/identity/authority. No network destination
        or identity is ever inferred from an inbound request.
        """
        adapter_id = None
        if connector is None:
            from .connector import make_link_connector
            from invite_rooms.peer_registry import MaterialScope, adapter_id_for
            if binding is None or binding.endpoint_id != endpoint_id or binding.generation != generation:
                raise ValueError("link binding does not match route")
            if (not isinstance(material_scope, MaterialScope)
                    or material_scope.endpoint_id != endpoint_id
                    or material_scope.pairing_generation != generation
                    or material_scope.fingerprint != binding.peer_fingerprint
                    or any(expected_scope.get(key) != value for key, value in {
                        "room_id": material_scope.planned_room_id,
                        "member_id": material_scope.member_id,
                        "home_install_id": material_scope.owner_installation_id,
                        "target_install_id": material_scope.target_install_id,
                        "target_profile": material_scope.target_profile,
                    }.items())):
                raise ValueError("material scope does not match route")
            adapter_id = adapter_id_for(material_scope)
            connector = make_link_connector(binding, identity, authority)
        elif any(value is not None for value in (binding, identity, authority)):
            raise ValueError("ambiguous connector registration")
        firewall = RoomFirewall(expected_scope=expected_scope,
                                backend=lambda _request: (_ for _ in ()).throw(
                                    FirewallDenied("unregistered backend")),
                                verify_native=verify_native,
                                backend_timeout_seconds=backend_timeout_seconds,
                                on_admitted=on_admitted)
        return self.register(endpoint_id, generation, firewall=firewall, connector=connector,
                             adapter_id=adapter_id)

    def bind(self, endpoint_id: str, firewall: RoomFirewall, *, target_url: str | None = None,
             scope: Mapping[str, Any] | None = None, generation: int = 1,
             connector: Callable[..., Any] | None = None) -> str:
        """Compatibility spelling; product callers must provide ``connector``."""
        if connector is None:
            # Spike compatibility only. Product registrations use register()
            # with the pinned-link connector and never target a URL supplied by
            # request data.
            connector = getattr(firewall, "_backend", None)
            if not callable(connector):
                raise ValueError("pinned connector is required")
            if self.state is not None:
                self.state.bind(endpoint_id, target_url=target_url or "http://127.0.0.1:1",
                                scope=scope or {})
        return self.register(endpoint_id, generation, firewall=firewall, connector=connector)

    def restore(self, firewall_factory: Callable[[dict], RoomFirewall]) -> tuple[str, ...]:
        if self.state is None:
            return ()
        if self._server is None:
            raise RuntimeError("adapter is not started")
        restored = []
        for record in self.state.all():
            endpoint_id = record.get("link_id")
            firewall = firewall_factory(dict(record))
            if not isinstance(endpoint_id, str) or not endpoint_id or not isinstance(firewall, RoomFirewall):
                raise RuntimeError("persisted link state is invalid")
            connector = getattr(firewall, "_backend", None)
            if not callable(connector):
                raise RuntimeError("persisted link connector is invalid")
            self.register(endpoint_id, 1, firewall=firewall, connector=connector)
            restored.append(endpoint_id)
        return tuple(restored)

    def deregister(self, endpoint_id: str, *, generation: int | None = None) -> None:
        with self._lock:
            for route_id, link in tuple(self._routes.items()):
                if link.endpoint_id == endpoint_id and (generation is None or link.generation == generation):
                    link.closed = True
                    self._routes.pop(route_id, None)

    def observe_generation(self, endpoint_id: str, generation: int) -> None:
        with self._lock:
            for route_id, link in tuple(self._routes.items()):
                if link.endpoint_id == endpoint_id and link.generation != generation:
                    link.closed = True
                    self._routes.pop(route_id, None)

    def url(self, endpoint_id: str) -> str:
        if self._server is None:
            raise RuntimeError("adapter is not started")
        host, port = self._server.server_address[:2]
        rendered = f"[{host}]" if ":" in str(host) else str(host)
        return f"http://{rendered}:{port}/links/{endpoint_id}"

    def close(self) -> None:
        with self._lock:
            for link in self._routes.values(): link.closed = True
            self._routes.clear()
        if self._server is not None:
            self._server.shutdown(); self._server.server_close(); self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2); self._thread = None
