"""W2a: loopback control listener substrate (TLS identity multiplexer + ``pair_ack``).

One listener per sidecar, bound ONLY to an owner-private literal loopback ``(host, port)``
(``pairing.control_listen``).  The pairing journal is the sole authority:

1. before the handshake the CA PEMs of every current confirmed/acknowledged journal link are unioned IN MEMORY
   (``ssl`` ``cadata``; no CA file is ever written) and used with TLS 1.3 / ``CERT_REQUIRED``;
2. after the handshake the actual leaf fingerprint is resolved against a FRESH journal read to exactly one current
   link (fingerprint, generation, endpoint) whose own CA issued that leaf - never against a registry projection,
   never TOFU, ambiguity refuses;
3. the link is re-read again immediately before the first application frame is read and the dispatcher is selected
   from the journal role (local ``joining`` => peer is host A => ``A_to_B`` and the one real handler,
   ``PairingService.handle_pair_ack``; local ``host`` => ``B_to_A``: no handler unless the caller supplied H1
   ``host_handlers`` (contextual ``handler(context, request)`` invoked only with a fresh journal-authenticated
   ``AuthenticatedPeerContext``), otherwise generic refusal);
4. the existing ``ControlDispatcher`` still enforces state (``confirmed`` => ``pair_ack`` only) and durable admission.

No journal lock is held across socket/TLS/handler I/O.  This module is a TRANSPORT PRIMITIVE: nothing starts it
automatically (``local_telemetry_server.main()`` does not), and it registers no preview/redeem/status/consent/material
handlers (W2b/W2c/W2d).
"""
from __future__ import annotations

import asyncio
import base64
import hmac
import ipaddress
import json
import logging
import math
import socket
import threading
import time
from typing import Callable, Mapping, Optional

from cryptography import x509

from .connector import (MAX_MESSAGE, ConnectorError, _json, decode_request, encode_response, recv_message,
                        send_message)
from .dispatcher import CONTEXT_OPERATIONS, ControlDispatcher, ControlError, utc_now_ms
from .firewall import UpstreamResponse
from .framing import SequencedTLS, certificate_fingerprint
from .tls import server_context

log = logging.getLogger("rooms-control-listener")

CONTROL_PATH = "/control/joining"  # same fixed path the W1 client uses (runtime_joining.CONTROL_PATH)
_ACTIVE = ("confirmed", "acknowledged")
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1"})
_DENIED = UpstreamResponse(403, (("content-type", "application/json"),), b'{"error":"control denied"}')
DRAIN_TIMEOUT = 5.0   # default bound (seconds) for close() to wait for running handler/completion executor threads
_INCOMPLETE = "listener teardown incomplete"


def _join(thread: Optional[threading.Thread], timeout: Optional[float]) -> None:
    """Join only a thread that was actually started (``Thread.join`` raises on an unstarted one)."""
    if thread is not None and thread.ident is not None:
        thread.join(timeout)


class ListenerError(RuntimeError):
    pass


def _ca_text(ca_pem) -> str:
    return ca_pem.decode("ascii") if isinstance(ca_pem, bytes) else ca_pem


def _issued_by(leaf_der: bytes, ca_pem) -> bool:
    """True iff the presented leaf is directly issued (name + signature) by THIS link's own CA certificate."""
    try:
        leaf = x509.load_der_x509_certificate(leaf_der)
        pem = _ca_text(ca_pem).encode("ascii")
        for ca in x509.load_pem_x509_certificates(pem):
            try:
                leaf.verify_directly_issued_by(ca)
                return True
            except Exception:
                continue
    except Exception:
        return False
    return False


class ControlListener:
    """Bounded accept loop + worker threads + one private event loop for the dispatchers.

    ``authority``: a ``JournalLinkAuthority``.  ``identity``: ``() -> (cert_file, key_file)`` for the LOCAL leaf
    (re-read per connection).  ``pair_ack_handler``: ``(request: dict) -> dict`` - in product
    ``PairingService.handle_pair_ack``; installed ONLY on the joining-role direction.
    """

    def __init__(self, *, host: str, port: int, authority, identity: Callable[[], tuple],
                 pair_ack_handler: Callable[[dict], dict], timeout: float = 10.0, max_connections: int = 8,
                 max_frame_bytes: int = 64 * 1024, drain_timeout: Optional[float] = None,
                 host_handlers: Optional[Mapping[str, Callable]] = None,
                 material_handlers: Optional[Mapping[str, Callable]] = None) -> None:
        if drain_timeout is None:
            drain_timeout = DRAIN_TIMEOUT
        if type(drain_timeout) not in (int, float) or not math.isfinite(drain_timeout) or drain_timeout < 0:
            raise ListenerError("invalid bounds")
        self._drain_timeout = float(drain_timeout)
        if host not in _LOOPBACK_HOSTS or str(ipaddress.ip_address(host)) != host:
            raise ListenerError("literal loopback host required")
        if type(port) is not int or not 0 < port <= 65535:
            raise ListenerError("explicit port required")
        if (type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0
                or type(max_connections) is not int or not 1 <= max_connections <= 64):
            raise ListenerError("invalid bounds")
        for name in ("current", "admit", "active_trusts", "peer_trust"):
            if not callable(getattr(authority, name, None)):
                raise ListenerError("durable journal authority required")
        self._host, self._port = host, port
        self.authority, self._identity, self._pair_ack = authority, identity, pair_ack_handler
        # H1: contextual ``handler(context, request)`` map, installed ONLY on the host-role (B_to_A) dispatcher.  The
        # default is empty (every host operation is refused); the dispatcher validates keys/direction/authority.
        self._host_handlers = dict(host_handlers or {})
        if (not set(self._host_handlers) <= CONTEXT_OPERATIONS
                or not all(callable(h) for h in self._host_handlers.values())):
            raise ListenerError("invalid host handlers")
        self._material_handlers = dict(material_handlers or {})
        if (not set(self._material_handlers) <= {"material_request", "material_receipt"}
                or not all(callable(h) for h in self._material_handlers.values())):
            raise ListenerError("invalid material handlers")
        self.timeout, self._frame_limit = float(timeout), max_frame_bytes
        self._slots = threading.BoundedSemaphore(max_connections)
        self._lock = threading.Lock()
        self._workers: set = set()
        self._sockets: set = set()
        self._listener: Optional[socket.socket] = None
        self._accept_thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_thread: Optional[threading.Thread] = None
        self._dispatchers: dict = {}
        self._stopping = threading.Event()
        self._started = False
        self._closed = False
        self._probe_nonces: set[tuple[str, int, str, str]] = set()
        self.handled = 0        # accepted application frames dispatched (test/observability counter)
        self.dispatched = 0     # application frames that reached the dispatcher
        self.refused = 0        # connections refused before any application frame was read

    # -- lifecycle ----------------------------------------------------------------------------------------
    @property
    def address(self) -> tuple:
        return (self._host, self._port)

    def start(self) -> None:
        with self._lock:
            if self._started or self._closed:
                raise ListenerError("listener already used")
            self._started = True
        address = ipaddress.ip_address(self._host)
        listener = socket.socket(socket.AF_INET6 if address.version == 6 else socket.AF_INET)
        try:
            listener.bind((self._host, self._port))     # bind failure: nothing else was created
            listener.listen(16)
            listener.settimeout(.1)
        except BaseException:
            listener.close()
            with self._lock:
                self._closed = True
            raise
        try:
            self._loop = asyncio.new_event_loop()
            self._dispatchers = {
                "A_to_B": self._dispatcher("A_to_B", {"pair_ack": self._pair_ack},
                                            material={"material_request": self._material_handlers["material_request"]}
                                            if "material_request" in self._material_handlers else {}),
                "B_to_A": self._dispatcher("B_to_A", {}, self._host_handlers,
                                            material={"material_receipt": self._material_handlers["material_receipt"]}
                                            if "material_receipt" in self._material_handlers else {}),
            }
            loop_thread = threading.Thread(target=self._run_loop, name="room-control-loop", daemon=True)
            loop_thread.start()
            self._loop_thread = loop_thread      # recorded ONLY once actually started (never join an unstarted one)
            self._listener = listener
            accept_thread = threading.Thread(target=self._accept, name="room-control-accept", daemon=True)
            accept_thread.start()
            self._accept_thread = accept_thread
        except BaseException:
            listener.close()
            self._listener = None
            try:
                self.close()
            except BaseException:
                pass                             # the ORIGINAL startup error is what the caller must see
            raise

    def _dispatcher(self, direction: str, handlers: Mapping, contextual: Optional[Mapping] = None,
                    material: Optional[Mapping] = None) -> ControlDispatcher:
        return ControlDispatcher(self.authority, handlers=handlers, admit=self.authority.admit, direction=direction,
                                 timeout=self.timeout, max_workers=4, completion_workers=1, queue_size=8,
                                 contextual_handlers=contextual, material_handlers=material,
                                 allow_legacy_material=False)

    def _run_loop(self) -> None:
        loop = self._loop
        assert loop is not None
        asyncio.set_event_loop(loop)
        loop.run_forever()

    def close(self) -> None:
        """Stop accepting, finish/abort connection workers, drain handler executors, then stop and close the loop.

        Success means every thread this listener owns is gone.  Otherwise ``ListenerError("listener teardown
        incomplete")`` is raised and every still-live resource (loop, dispatchers, threads) stays referenced, so
        ``close()``/``join()`` may be retried; nothing is closed underneath a live task or abandoned.
        """
        self._stopping.set()
        with self._lock:
            self._closed = True
            listener, self._listener = self._listener, None
            accept = self._accept_thread
            sockets = tuple(self._sockets)
        if listener is not None:
            listener.close()
        for sock in sockets:           # unblock workers parked in handshake/recv
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        if accept is not None:
            accept.join(2)
        deadline = time.monotonic() + self.timeout + 1
        with self._lock:
            workers = tuple(self._workers)
        for worker in workers:
            _join(worker, max(0, deadline - time.monotonic()))
        with self._lock:
            leftover = bool(self._workers) or (accept is not None and accept.is_alive())
        if leftover:
            raise ListenerError(_INCOMPLETE)
        # Handler/completion executors: cancel queued work, bounded wait for the running (admitted) ones.
        drain_end = time.monotonic() + self._drain_timeout
        drained = True
        for dispatcher in tuple(self._dispatchers.values()):
            drained = dispatcher.drain(max(0.0, drain_end - time.monotonic())) and drained
        if not drained:
            raise ListenerError(_INCOMPLETE)
        loop, thread = self._loop, self._loop_thread
        if loop is not None:
            if thread is not None and thread.is_alive():
                try:
                    asyncio.run_coroutine_threadsafe(self._stop_loop(), loop).result(2)
                except BaseException:
                    raise ListenerError(_INCOMPLETE) from None
                thread.join(2)
                if thread.is_alive():
                    raise ListenerError(_INCOMPLETE)
            elif thread is not None and thread.ident is not None and loop.is_running():
                raise ListenerError(_INCOMPLETE)
            loop.close()                 # never-started loop (thread start failed) or fully stopped loop
        self._loop = self._loop_thread = self._accept_thread = None
        self._dispatchers = {}

    @staticmethod
    async def _stop_loop() -> None:
        """Cancel and await every other task, then stop the loop (never stop it under a live task)."""
        current = asyncio.current_task()
        pending = [t for t in asyncio.all_tasks() if t is not current]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        asyncio.get_running_loop().call_soon(asyncio.get_running_loop().stop)

    def join(self, timeout: Optional[float] = None) -> None:
        """Block until the accept loop, every worker and every handler executor thread are gone (after ``close``)."""
        end = None if timeout is None else time.monotonic() + timeout
        threads = [self._accept_thread, self._loop_thread, *tuple(self._workers)]
        for dispatcher in tuple(self._dispatchers.values()):
            threads.extend(dispatcher.threads())
        for thread in threads:
            if thread is not None:
                _join(thread, None if end is None else max(0, end - time.monotonic()))

    # -- accept / serve -----------------------------------------------------------------------------------
    def _accept(self) -> None:
        while not self._stopping.is_set():
            listener = self._listener
            if listener is None:
                break
            try:
                sock, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            if not self._slots.acquire(blocking=False):
                sock.close()
                continue
            worker = threading.Thread(target=self._serve_owned, args=(sock,), name="room-control-conn", daemon=True)
            with self._lock:
                self._workers.add(worker)
                self._sockets.add(sock)
            try:
                worker.start()
            except BaseException:
                with self._lock:
                    self._workers.discard(worker)
                    self._sockets.discard(sock)
                sock.close()
                self._slots.release()
                raise

    def _serve_owned(self, sock) -> None:
        try:
            self._serve(sock)
        except Exception:
            log.info("room_control_unavailable")      # never peer text, certificate or exception detail
        finally:
            with self._lock:
                self._sockets.discard(sock)
                self._workers.discard(threading.current_thread())
            self._slots.release()

    def _context(self, trusts):
        union = "\n".join(_ca_text(t.ca_pem).strip() for t in trusts)
        cert_file, key_file = self._identity()
        return server_context(ca_data=union, cert_file=cert_file, key_file=key_file)

    def _resolve(self, fingerprint: str, leaf_der: bytes):
        """Exactly one current link: fingerprint + own-CA issuance, from a FRESH journal read; else ``None``."""
        matches = [t for t in self.authority.active_trusts()
                   if t.protocol_state in _ACTIVE and hmac.compare_digest(t.fingerprint.lower(), fingerprint)
                   and _issued_by(leaf_der, t.ca_pem)]
        return matches[0] if len(matches) == 1 else None

    def _still_current(self, trust) -> bool:
        again = self.authority.peer_trust(trust.endpoint_id, trust.generation, role=trust.role, states=_ACTIVE)
        return again is not None and again == trust

    def _serve(self, sock) -> None:
        deadline = time.monotonic() + self.timeout
        sock.settimeout(self.timeout)
        tls = None
        try:
            trusts = self.authority.active_trusts()
            if not trusts:
                self._refuse()
                return
            context = self._context(trusts)
            try:
                # Registered BEFORE the handshake so close() can interrupt a stalled one (wrap_socket detaches ``sock``).
                tls = context.wrap_socket(sock, server_side=True, do_handshake_on_connect=False)
                with self._lock:
                    self._sockets.add(tls)
                tls.do_handshake()
            except Exception:
                self._refuse()
                return
            leaf = tls.getpeercert(binary_form=True)
            trust = self._resolve(certificate_fingerprint(leaf), leaf) if leaf else None
            # Fail closed on unpair / re-pair / generation change that happened during the handshake.
            if trust is None or not self._still_current(trust):
                self._refuse()
                return
            frames = SequencedTLS(tls, max_frame_bytes=self._frame_limit, deadline=deadline)
            raw = recv_message(frames)
            envelope = _json(raw)
            if (not isinstance(envelope, dict) or set(envelope) != {"endpoint_id", "generation", "request"}
                    or envelope["endpoint_id"] != trust.endpoint_id or type(envelope["generation"]) is not int
                    or envelope["generation"] != trust.generation):
                raise ConnectorError()
            request = decode_request(json.dumps(envelope["request"], separators=(",", ":")).encode())
            if request.method != "POST" or request.target != CONTROL_PATH:
                raise ConnectorError()
            body = _json(request.body)
            # R1: the nested signed acknowledgement must name THIS TLS-resolved link.  A genuine signature over a
            # different link's transcript is not authorization for this connection; refuse before any handler.
            if not self._ack_bound(trust, body):
                raise ConnectorError()
            if not self._still_current(trust):
                self._refuse()
                return
            if isinstance(body, dict) and body.get("op") == "material_probe":
                response = self._material_probe(trust, body)
                send_message(frames, encode_response(response))
                return
            response = self._dispatch(trust, body, deadline)
            send_message(frames, encode_response(response))
        finally:
            if tls is not None:
                with self._lock:
                    self._sockets.discard(tls)
            (tls if tls is not None else sock).close()

    @staticmethod
    def _ack_bound(trust, body) -> bool:
        if not isinstance(body, dict) or body.get("op") != "pair_ack":
            return True            # other shapes are refused by the dispatcher (W2a installs no other handler)
        ack = body.get("ack")
        endpoint = ack.get("endpoint_id") if isinstance(ack, dict) else None
        return isinstance(endpoint, str) and hmac.compare_digest(endpoint.encode(), trust.endpoint_id.encode())

    def _refuse(self) -> None:
        with self._lock:
            self.refused += 1

    def _material_probe(self, trust, body) -> UpstreamResponse:
        """Complete the authenticated four-timestamp probe on this pinned path."""
        required = {"op", "v", "operation", "request_id", "nonce", "t1_utc_ms"}
        if (set(body) != required or body.get("op") != "material_probe"
                or type(body.get("v")) is not int or body.get("v") != 1
                or body.get("operation") != "material_request"
                or not isinstance(body.get("request_id"), str) or len(body["request_id"]) != 32
                or not isinstance(body.get("nonce"), str) or len(body["nonce"]) != 32
                or type(body.get("t1_utc_ms")) is not int or body["t1_utc_ms"] < 0):
            raise ConnectorError()
        probe_key = (trust.endpoint_id, trust.generation, body["request_id"], body["nonce"])
        with self._lock:
            if probe_key in self._probe_nonces:
                raise ConnectorError()
            self._probe_nonces.add(probe_key)
        t2 = utc_now_ms()
        # The timestamp is taken immediately before the authenticated response is encoded.
        t3 = utc_now_ms()
        payload = {"op": "material_probe", "v": 1, "operation": "material_request",
                   "endpoint_id": trust.endpoint_id, "generation": trust.generation,
                   "request_id": body["request_id"], "nonce": body["nonce"],
                   "t1_utc_ms": body["t1_utc_ms"], "t2_utc_ms": t2, "t3_utc_ms": t3}
        if not self.authority.issue_material_probe(trust, payload):
            raise ConnectorError()
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return UpstreamResponse(200, (("content-type", "application/json"),), raw)

    def _dispatch(self, trust, body, deadline: float) -> UpstreamResponse:
        direction = "A_to_B" if trust.role == "joining" else "B_to_A"
        dispatcher = self._dispatchers.get(direction)
        loop = self._loop
        if dispatcher is None or loop is None or self._stopping.is_set():
            return _DENIED
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return _DENIED
        with self._lock:
            self.dispatched += 1
        future = None
        try:
            future = asyncio.run_coroutine_threadsafe(
                dispatcher.dispatch(trust.endpoint_id, trust.generation, body, expected_trust=trust, deadline=deadline),
                loop)
            result = future.result(remaining)
            with self._lock:
                self.handled += 1
            payload = json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
            if len(payload) > MAX_MESSAGE // 2:
                return _DENIED
            return UpstreamResponse(200, (("content-type", "application/json"),), payload)
        except Exception:
            if future is not None and not future.done() and isinstance(body, dict) and body.get("op") != "pair_ack":
                future.cancel()      # contextual lane only: the request expired, drop the owning task/queued work
            return _DENIED


def build_pair_ack_listener(config, *, installation_id: str, profile: str, **kw) -> ControlListener:
    """Construct (NOT start) the listener from a validated ``RuntimeConfig.pairing`` (``control_listen`` required).

    Journal authority and the local identity both come from the owner-private pairing config; the one handler is the
    real ``PairingService.handle_pair_ack`` (a service instance per request: all state lives in the journal).
    """
    from invite_rooms import identity as identity_module
    from invite_rooms.pairing_journal import Journal, JournalLinkAuthority
    from invite_rooms.pairing_service import PairingService
    pairing = getattr(config, "pairing", None)
    if pairing is None or pairing.control_listen is None:
        raise ListenerError("pairing.control_listen is not configured")
    host, port = pairing.control_listen
    journal = Journal(pairing.journal_dir, anchor=pairing.journal_anchor)

    def identity() -> tuple:
        bundle = identity_module.load_identity(pairing.identity_dir, installation_id=installation_id, profile=profile)
        return str(bundle.leaf_cert_file), str(bundle.leaf_key_file)

    def no_catalog():
        raise RuntimeError("catalog is not needed for pair_ack")

    def handle(request):
        service = PairingService(pairing, installation_id=installation_id, profile=profile,
                                 catalog_source=no_catalog, anchor=pairing.journal_anchor, journal=journal)
        return service.handle_pair_ack(request)

    return ControlListener(host=host, port=port, authority=JournalLinkAuthority(journal), identity=identity,
                           pair_ack_handler=handle, **kw)
