"""One-request pinned mTLS data connections between two owner-fixed sidecars.

No relay, proxy discovery, retry, offline queue, or request-selected destination.
``authority.admit(endpoint_id, generation, operation)`` must atomically check the
journal generation/state, serialized with unpair. It releases its transaction
before returning; no database lock is held across network I/O.
"""
from __future__ import annotations

import base64
import ipaddress
import json
import logging
import math
import socket
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

from .firewall import (
    FirewallDenied, ForwardedRequest, Request, RoomFirewall,
    UpstreamAmbiguous, UpstreamResponse,
)
from .framing import SequencedTLS, TLSIdentityError, TLSStreamClosed
from .tls import client_context, server_context, wrap_client, wrap_server

log = logging.getLogger("rooms-connector")
MESSAGE_CHUNK = 24 * 1024
MAX_MESSAGE = 1024 * 1024
REQUEST_TIMEOUT = 60.0
_DENIED = UpstreamResponse(403, (("content-type", "application/json"),),
                           b'{"error":"room request denied"}')
_AMBIGUOUS = UpstreamResponse(502, (("content-type", "application/json"),),
    b'{"error":{"message":"room link outcome unknown","type":"room_error","code":null}}')


class ConnectorError(ValueError):
    def __init__(self):
        super().__init__("room link unavailable")


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ConnectorError()
        value[key] = item
    return value


def _json(raw):
    try:
        return json.loads(raw, object_pairs_hook=_unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(ConnectorError()))
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ConnectorError() from None


def send_message(frames: SequencedTLS, data: bytes) -> None:
    if not isinstance(data, bytes) or not 0 < len(data) <= MAX_MESSAGE:
        raise ConnectorError()
    count = -(-len(data) // MESSAGE_CHUNK)
    frames.send(json.dumps({"chunks": count, "length": len(data)}).encode())
    for offset in range(0, len(data), MESSAGE_CHUNK):
        frames.send(data[offset:offset + MESSAGE_CHUNK])


def recv_message(frames: SequencedTLS) -> bytes:
    header = _json(frames.receive()[1])
    if not isinstance(header, dict) or set(header) != {"chunks", "length"}:
        raise ConnectorError()
    count, length = header["chunks"], header["length"]
    if (type(count) is not int or type(length) is not int or not 0 < length <= MAX_MESSAGE
            or count != -(-length // MESSAGE_CHUNK)):
        raise ConnectorError()
    data = bytearray()
    for index in range(count):
        chunk = frames.receive()[1]
        if len(chunk) != min(MESSAGE_CHUNK, length - index * MESSAGE_CHUNK):
            raise ConnectorError()
        data.extend(chunk)
    return bytes(data)


def _headers(value):
    if not isinstance(value, (tuple, list)) or len(value) > 8:
        raise ConnectorError()
    result = []
    for pair in value:
        if not isinstance(pair, (tuple, list)) or len(pair) != 2:
            raise ConnectorError()
        name, item = pair
        if (not isinstance(name, str) or not name or len(name) > 128
                or not isinstance(item, str) or len(item) > 4096
                or any(char in name + item for char in "\r\n\x00")):
            raise ConnectorError()
        result.append((name, item))
    return tuple(result)


def _body(value):
    if not isinstance(value, str):
        raise ConnectorError()
    try:
        return base64.b64decode(value, validate=True)
    except ValueError:
        raise ConnectorError() from None


def encode_request(request: ForwardedRequest) -> bytes:
    if not isinstance(request.body, bytes) or len(request.body) > MAX_MESSAGE // 2:
        raise FirewallDenied("room request denied")
    payload = json.dumps({"method": request.method, "path": request.path,
                          "headers": _headers(request.headers),
                          "body": base64.b64encode(request.body).decode("ascii")},
                         separators=(",", ":")).encode()
    if len(payload) > MAX_MESSAGE:
        raise FirewallDenied("room request denied")
    return payload


def decode_request(raw: bytes) -> Request:
    value = _json(raw)
    if not isinstance(value, dict) or set(value) != {"method", "path", "headers", "body"}:
        raise ConnectorError()
    if not isinstance(value["method"], str) or not isinstance(value["path"], str):
        raise ConnectorError()
    return Request(value["method"], value["path"], _headers(value["headers"]), _body(value["body"]))


def encode_response(response: UpstreamResponse) -> bytes:
    return json.dumps({"status": response.status, "headers": _headers(response.headers),
                       "body": base64.b64encode(response.body).decode("ascii")},
                      separators=(",", ":")).encode()


def decode_response(raw: bytes) -> UpstreamResponse:
    value = _json(raw)
    if (not isinstance(value, dict) or set(value) != {"status", "headers", "body"}
            or type(value["status"]) is not int or not 200 <= value["status"] <= 599):
        raise ConnectorError()
    return UpstreamResponse(value["status"], _headers(value["headers"]), _body(value["body"]))


def _loopback(host):
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        raise ValueError("literal loopback address required") from None
    if not address.is_loopback:
        raise ValueError("literal loopback address required")
    return address


@dataclass(frozen=True)
class LinkIdentity:
    cert_file: str
    key_file: str


@dataclass(frozen=True)
class LinkBinding:
    endpoint_id: str
    generation: int
    address: tuple[str, int]
    peer_ca_file: str | None
    peer_fingerprint: str
    expected_binding: Mapping[str, Any]
    timeout: float = 10.0
    max_inflight: int = 8
    #: In-memory peer CA PEM (product path).  Mutually exclusive with ``peer_ca_file``; never written to disk.
    peer_ca_data: str | None = None

    def __post_init__(self):
        _loopback(self.address[0])
        if (self.peer_ca_file is None) == (self.peer_ca_data is None):
            raise ValueError("invalid link binding")
        if (type(self.address[1]) is not int or not 0 < self.address[1] <= 65535
                or type(self.generation) is not int or self.generation < 1
                or not isinstance(self.endpoint_id, str) or not self.endpoint_id
                or type(self.max_inflight) is not int or self.max_inflight < 1
                or not math.isfinite(self.timeout) or self.timeout <= 0):
            raise ValueError("invalid link binding")
        if (self.expected_binding.get("endpoint_id") != self.endpoint_id
                or self.expected_binding.get("fingerprint") != self.peer_fingerprint):
            raise ValueError("invalid link binding")
        object.__setattr__(self, "expected_binding", MappingProxyType(dict(self.expected_binding)))


class _Admission:
    #: Operation label handed to ``authority.admit`` immediately before the first application frame.
    operation = "data_send"

    def __init__(self, binding: LinkBinding, authority):
        if not callable(getattr(authority, "current", None)) or not callable(getattr(authority, "admit", None)):
            raise ValueError("durable link authority required")
        self.binding, self.authority = binding, authority

    def check(self):
        state = self.authority.current(self.binding.endpoint_id)
        if (state is None or state.generation != self.binding.generation
                or state.protocol_state != "acknowledged"
                or dict(state.binding) != dict(self.binding.expected_binding)):
            raise FirewallDenied("room request denied")

    def admit(self, operation):
        self.check()
        if self.authority.admit(self.binding.endpoint_id, self.binding.generation, operation) is not True:
            raise FirewallDenied("room request denied")


class _PairAckAdmission(_Admission):
    """Admission for the ONE operation a ``confirmed`` host link may perform: ``pair_ack``.

    Every check re-reads the journal peer record (one non-waiting shared read, released before returning) and
    requires: local role ``host``, state ``confirmed`` or ``acknowledged``, the exact generation, and the exact
    binding / leaf pin / CA the connector was built with.  A different operation label is never admitted.
    """
    operation = "pair_ack"

    def __init__(self, binding: LinkBinding, authority):
        super().__init__(binding, authority)
        if not callable(getattr(authority, "peer_trust", None)):
            raise ValueError("durable link authority required")

    def check(self):
        trust = self.authority.peer_trust(self.binding.endpoint_id, self.binding.generation, role="host",
                                          states=("confirmed", "acknowledged"))
        ca = self.binding.peer_ca_data
        if (trust is None or trust.generation != self.binding.generation or trust.role != "host"
                or trust.protocol_state not in ("confirmed", "acknowledged")
                or trust.fingerprint != self.binding.peer_fingerprint
                or (trust.ca_pem.decode("ascii") if isinstance(trust.ca_pem, bytes) else trust.ca_pem) != ca
                or trust.binding_dict() != dict(self.binding.expected_binding)):
            raise FirewallDenied("room request denied")

    def admit(self, operation):
        if operation != self.operation:
            raise FirewallDenied("room request denied")
        super().admit(operation)


class _DirectClient:
    def __init__(self, address):
        self.address = address

    def open_stream(self, timeout):
        return socket.create_connection(self.address, timeout=timeout)


class HostConnector:
    """A: bounded, single-attempt request over a new pinned mTLS connection.

    ``client``/``inner`` are the transport-only spike compatibility seam. Product
    construction uses ``make_link_connector`` and cannot omit durable admission.
    """
    def __init__(self, client=None, *, inner=None, binding=None, identity=None, authority=None):
        if binding is not None:
            self.admission = _Admission(binding, authority)
            self.binding = binding
            self.client = _DirectClient(binding.address)
            self.context = client_context(ca_file=binding.peer_ca_file, ca_data=binding.peer_ca_data,
                cert_file=identity.cert_file, key_file=identity.key_file)
            self.peer_fingerprint = binding.peer_fingerprint
            self.timeout = binding.timeout
            self._slots = threading.BoundedSemaphore(binding.max_inflight)
        else:
            # This is not used by the adapter factory. The spike owns relay retries.
            self.client = client
            self.context = client_context(ca_file=inner["peer_ca_file"],
                cert_file=inner["cert_file"], key_file=inner["key_file"])
            self.peer_fingerprint = inner["peer_fingerprint"]
            self.admission = None
            self.timeout = REQUEST_TIMEOUT
            self._slots = threading.BoundedSemaphore(8)
        self._lock = threading.Lock()
        self.active = 0

    def _check(self, callback=None):
        if callback is not None:
            # Legacy callbacks may raise on denial or return None.  A material
            # request predicate may also return a literal False, which is an
            # explicit denial and must never be discarded before send().
            if callback() is False:
                raise FirewallDenied("room request denied")
        admission = getattr(self, "admission", None)
        if admission is not None:
            admission.check()

    def exchange(self, payload, deadline, *, admit=None, wrap=wrap_client,
                 send=send_message, receive=recv_message, frames_factory=SequencedTLS):
        """One stream, one reader/writer, one send, no retry (also used by spike)."""
        self._check(admit)
        if time.monotonic() >= deadline:
            raise UpstreamAmbiguous("room link outcome unknown")
        sock = tls = None
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise UpstreamAmbiguous("room link outcome unknown")
            sock = self.client.open_stream(timeout=remaining)
            self._check(admit)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise UpstreamAmbiguous("room link outcome unknown")
            sock.settimeout(remaining)
            tls = wrap(sock, self.context, expected_fingerprint=self.peer_fingerprint)
            self._check(admit)
            frames = frames_factory(tls, deadline=deadline)
            admission = getattr(self, "admission", None)
            if admission is not None:
                admission.admit(admission.operation)
            # Final request-local gate: after durable admission succeeds, deny a
            # withdrawn request or expired budget before the first application
            # frame. Keep this check directly adjacent to send(); a post-send
            # check cannot revoke a receipt that may already be durable.
            if admit is not None and admit() is False:
                raise FirewallDenied("room request denied")
            if time.monotonic() >= deadline:
                raise UpstreamAmbiguous("room link outcome unknown")
            send(frames, payload)
            self._check(admit)
            response = decode_response(receive(frames))
            self._check(admit)
            return response
        finally:
            if tls is not None or sock is not None:
                (tls if tls is not None else sock).close()

    def forward(self, request: ForwardedRequest, admit=None, *, deadline=None) -> UpstreamResponse:
        payload = encode_request(request)
        if self.admission is not None:
            payload = json.dumps({"endpoint_id": self.binding.endpoint_id,
                                  "generation": self.binding.generation,
                                  "request": _json(payload)}, separators=(",", ":")).encode()
            if len(payload) > MAX_MESSAGE:
                raise FirewallDenied("room request denied")
        if deadline is None:
            deadline = time.monotonic() + self.timeout
        if time.monotonic() >= deadline:
            raise UpstreamAmbiguous("room link outcome unknown")
        self._check(admit)
        if not self._slots.acquire(blocking=False):
            raise UpstreamAmbiguous("room link unavailable")
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not self._lock.acquire(timeout=remaining):
            self._slots.release()
            raise UpstreamAmbiguous("room link unavailable")
        self.active += 1
        self._lock.release()
        try:
            # Generation is re-read after capacity acquisition and every I/O wait.
            return self.exchange(payload, deadline, admit=admit, wrap=wrap_client)
        except Exception:
            # Even generation changes after send are ambiguous, never a 4xx.
            raise UpstreamAmbiguous("room link outcome unknown") from None
        finally:
            with self._lock:
                self.active -= 1
            self._slots.release()

    def __call__(self, request, admit=None, *, deadline=None):
        return self.forward(request, admit, deadline=deadline)


def make_link_connector(binding: LinkBinding, identity: LinkIdentity, authority) -> HostConnector:
    """Build the real connector for exactly one acknowledged endpoint/generation."""
    if not isinstance(binding, LinkBinding) or not isinstance(identity, LinkIdentity):
        raise TypeError("fixed link binding and identity required")
    connector = HostConnector(binding=binding, identity=identity, authority=authority)
    connector.admission.check()
    return connector


PAIR_ACK_PATH = "/control/joining"  # the fixed control path (same constant the W1 client and W2a listener use)


class PairAckConnector(HostConnector):
    """Host A -> peer B, ``pair_ack`` ONLY, over the same bounded pinned one-request exchange.

    The operation is fixed by construction (``_PairAckAdmission``): preflight/current and final admission accept a
    ``confirmed`` link for ``pair_ack`` alone, and the generic ``forward`` (data) entry point is closed, so this object
    can never carry a data/preview/redeem/status/material request on a confirmed link.
    """
    def __init__(self, *, binding: LinkBinding, identity: LinkIdentity, authority):
        super().__init__(binding=binding, identity=identity, authority=authority)
        self.admission = _PairAckAdmission(binding, authority)

    def forward(self, request, admit=None, *, deadline=None):
        raise FirewallDenied("room request denied")

    def forward_pair_ack(self, body: Mapping[str, Any], admit=None) -> UpstreamResponse:
        ack = body.get("ack") if isinstance(body, dict) else None
        if (not isinstance(body, dict) or set(body) != {"op", "ack"} or body["op"] != "pair_ack"
                or not isinstance(ack, dict) or ack.get("endpoint_id") != self.binding.endpoint_id):
            raise FirewallDenied("room request denied")
        try:
            payload = json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        except (TypeError, ValueError):
            raise FirewallDenied("room request denied") from None
        return HostConnector.forward(self, ForwardedRequest("POST", PAIR_ACK_PATH, (), payload), admit)


def make_pair_ack_connector(binding: LinkBinding, identity: LinkIdentity, authority) -> PairAckConnector:
    """Build the host pair_ack connector for exactly one confirmed/acknowledged endpoint/generation."""
    if not isinstance(binding, LinkBinding) or not isinstance(identity, LinkIdentity):
        raise TypeError("fixed link binding and identity required")
    if binding.peer_ca_data is None:
        raise ValueError("in-memory peer CA required")
    connector = PairAckConnector(binding=binding, identity=identity, authority=authority)
    connector.admission.check()
    return connector


class _NoCredentialRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def native_http_backend(target_base: str, *, timeout: float = 25.0,
                        max_response_bytes: int = MAX_MESSAGE // 2) -> Callable:
    parts = urlsplit(target_base)
    _loopback(parts.hostname or "")
    if (parts.scheme != "http" or parts.username is not None or parts.password is not None
            or parts.path not in ("", "/") or parts.query or parts.fragment
            or not math.isfinite(timeout) or timeout <= 0):
        raise ValueError("fixed native loopback origin required")
    # Validate port eagerly; no DNS name, proxy environment, or URL join escape.
    if parts.port is None or not 0 < parts.port <= 65535:
        raise ValueError("fixed native loopback origin required")
    origin = target_base.rstrip("/")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoCredentialRedirect())

    def forward(request: ForwardedRequest, *, deadline: float | None = None) -> UpstreamResponse:
        if (not request.path.startswith("/") or request.path.startswith("//")
                or any(char in request.path for char in "?#\\\r\n")):
            raise FirewallDenied("room request denied")
        native = urllib.request.Request(origin + request.path, data=request.body or None,
                                        method=request.method, headers=dict(request.headers))
        def read(response):
            with response:
                body = response.read(max_response_bytes + 1)
                if len(body) > max_response_bytes:
                    raise UpstreamAmbiguous("room link outcome unknown")
                headers = tuple((key, value) for key, value in response.headers.items()
                                if key.lower() in {"content-type", "content-length"})
                return UpstreamResponse(response.code, headers, body)
        try:
            remaining = timeout if deadline is None else min(timeout, deadline - time.monotonic())
            if remaining <= 0:
                raise UpstreamAmbiguous("room link outcome unknown")
            return read(opener.open(native, timeout=remaining))
        except urllib.error.HTTPError as response:
            return read(response)
        except Exception:
            raise UpstreamAmbiguous("room link outcome unknown") from None
    return forward


class TargetConnector:
    """B: pinned peer, fixed firewall, fixed local native origin, bounded serving."""
    def __init__(self, *, inner=None, firewall=None, binding=None, identity=None,
                 authority=None, expected_scope=None, verify_native=None, target_base=None):
        self.admission = None
        if binding is not None:
            self.admission = _Admission(binding, authority)
            self.context = server_context(ca_file=binding.peer_ca_file, ca_data=binding.peer_ca_data,
                cert_file=identity.cert_file, key_file=identity.key_file)
            self.peer_fingerprint = binding.peer_fingerprint
            self.timeout = binding.timeout
            capacity = binding.max_inflight
            native = native_http_backend(target_base, timeout=binding.timeout)
            def backend(request, deadline=None):
                # RoomFirewall runs its backend in a worker: re-admit IN that
                # worker, not before queueing it, immediately before native I/O.
                if deadline is not None and time.monotonic() >= deadline:
                    raise UpstreamAmbiguous("room link outcome unknown")
                self.admission.admit("data_native")
                response = native(request, deadline=deadline)
                try:
                    self.admission.check()
                except Exception:
                    raise UpstreamAmbiguous("room link outcome unknown") from None
                return response
            self._native_backend = backend
            self.firewall = RoomFirewall(expected_scope=expected_scope, backend=backend,
                verify_native=verify_native, backend_timeout_seconds=binding.timeout)
        else:
            self.context = server_context(ca_file=inner["peer_ca_file"],
                cert_file=inner["cert_file"], key_file=inner["key_file"])
            self.peer_fingerprint = inner["peer_fingerprint"]
            self.timeout = REQUEST_TIMEOUT
            capacity = 8
            self.firewall = firewall
        self.served = 0
        self.accepted = 0
        self.peak_active = 0
        self._slots = threading.BoundedSemaphore(capacity)
        self._lock = threading.Lock()
        self._workers = set()
        self._sockets = set()
        self._listener = None
        self._thread = None
        self._stopping = threading.Event()

    def respond(self, request: Request, *, deadline=None) -> UpstreamResponse:
        try:
            firewall = self.firewall
            if getattr(self, "admission", None) is not None:
                remaining = self.timeout if deadline is None else deadline - time.monotonic()
                if remaining <= 0:
                    raise UpstreamAmbiguous("room link outcome unknown")
                # Per-request deadline closure: no mutable shared request state,
                # and no queue timeout can admit a worker after its deadline.
                firewall = RoomFirewall(expected_scope=firewall._scope,
                    verify_native=firewall._verify_native,
                    backend=lambda forwarded: self._native_backend(forwarded, deadline),
                    backend_timeout_seconds=remaining)
            return firewall.forward(request)
        except UpstreamAmbiguous:
            return _AMBIGUOUS
        except FirewallDenied:
            return _DENIED

    def serve(self, sock: socket.socket, stream_id: str = "") -> None:
        deadline = time.monotonic() + self.timeout
        sock.settimeout(self.timeout)
        tls = None
        try:
            tls = wrap_server(sock, self.context, expected_fingerprint=self.peer_fingerprint)
            if self.admission is not None:
                self.admission.check()
            frames = SequencedTLS(tls, deadline=deadline)
            raw = recv_message(frames)
            if self.admission is not None:
                envelope = _json(raw)
                binding = self.admission.binding
                if (not isinstance(envelope, dict)
                        or set(envelope) != {"endpoint_id", "generation", "request"}
                        or envelope["endpoint_id"] != binding.endpoint_id
                        or type(envelope["generation"]) is not int
                        or envelope["generation"] != binding.generation):
                    raise ConnectorError()
                raw = json.dumps(envelope["request"], separators=(",", ":")).encode()
            request = decode_request(raw)
            if self.admission is not None:
                self.admission.check()
            response = self.respond(request, deadline=deadline)
            if self.admission is not None:
                try:
                    self.admission.check()
                except Exception:
                    response = _AMBIGUOUS
            send_message(frames, encode_response(response))
            with self._lock:
                self.served += 1
        except Exception:
            # No peer text, auth header, path, certificate, or exception message.
            log.info("room_data_unavailable")
        finally:
            (tls if tls is not None else sock).close()

    @property
    def address(self):
        if self._listener is None:
            raise RuntimeError("connector not started")
        return self._listener.getsockname()[:2]

    def start(self, *, host="127.0.0.1", port=0):
        address = _loopback(host)
        if self._listener is not None:
            raise RuntimeError("connector already started")
        listener = socket.socket(socket.AF_INET6 if address.version == 6 else socket.AF_INET)
        try:
            listener.bind((host, port))
            listener.listen(16)
            listener.settimeout(.1)
        except BaseException:
            listener.close()
            raise
        self._stopping.clear()
        self._listener = listener
        self._thread = threading.Thread(target=self._accept, name="room-data-accept", daemon=True)
        self._thread.start()

    def _accept(self):
        while not self._stopping.is_set():
            try:
                sock, _ = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            if not self._slots.acquire(blocking=False):
                sock.close()
                continue
            worker = threading.Thread(target=self._serve_owned, args=(sock,), daemon=True)
            with self._lock:
                self.accepted += 1
                self._workers.add(worker)
                self._sockets.add(sock)
                self.peak_active = max(self.peak_active, len(self._workers))
            try:
                worker.start()
            except BaseException:
                with self._lock:
                    self._workers.discard(worker)
                    self._sockets.discard(sock)
                sock.close()
                self._slots.release()
                raise

    def _serve_owned(self, sock):
        try:
            self.serve(sock)
        finally:
            with self._lock:
                self._sockets.discard(sock)
                self._workers.discard(threading.current_thread())
            self._slots.release()

    def close(self):
        self._stopping.set()
        if self._listener is not None:
            self._listener.close()
        if self._thread is not None:
            self._thread.join(2)
        deadline = time.monotonic() + self.timeout + 1
        with self._lock:
            workers = tuple(self._workers)
        for worker in workers:
            worker.join(max(0, deadline - time.monotonic()))
        with self._lock:
            if self._workers:
                raise RuntimeError("connector teardown incomplete")
        self._listener = None
        self._thread = None
