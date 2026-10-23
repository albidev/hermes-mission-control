"""Outbound-only Room connector (host or target side) for the F1 cross-host lab.

Both participants dial the relay outbound over WSS; neither opens an inbound
port. Every Room request travels in its own relay stream carrying an inner
TLS 1.3 mutual-auth session with pinned peer fingerprints (``tls_stream``), so
the relay forwards ciphertext only. Both sides apply the RoomFirewall
allowlist: the host before anything leaves the machine, the target before
anything reaches its native loopback API.

  python connector.py host   --config host.json     # prints the loopback adapter URL
  python connector.py target --config target.json

Relay loss holds a caller for at most ten seconds; only GET and identical
idempotency-keyed dispatches may be resent after a broken stream. There is no
offline queue. Native Idempotency-Key, not the transport, prevents admission twice.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import ipaddress
import json
import logging
import random
import signal
import socket
import ssl
import stat
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
from relay import MAX_FRAME_BYTES, MAX_RAW_CHUNK
from room_firewall import FirewallDenied, ForwardedRequest, Request, RoomFirewall, UpstreamAmbiguous, UpstreamResponse
from tls_stream import SequencedTLS, TLSIdentityError, TLSStreamClosed, client_context, server_context, wrap_client, wrap_server

log = logging.getLogger("rooms-connector")

MESSAGE_CHUNK = 24 * 1024          # SequencedTLS hex-encodes; keeps frames under its 64 KiB cap
MAX_MESSAGE = 1024 * 1024
STREAM_OPEN_TIMEOUT = 10.0
REQUEST_TIMEOUT = 60.0
HOLD_BOUND = 10.0  # total host request budget, strictly below lease_ttl / 2 = 15s
MAX_ENDED_STREAM_IDS = 1024  # per-session memory of relay-aborted ids (never resurrected)
_DENIED = UpstreamResponse(403, (("content-type", "application/json"),), b'{"error":"room request denied"}')
_AMBIGUOUS = UpstreamResponse(502, (("content-type", "application/json"),),
                              b'{"error":{"message":"room link outcome unknown","type":"room_error","code":null}}')
_UNAVAILABLE = UpstreamResponse(503, (("content-type", "application/json"),), b'{"error":"room link unavailable"}')


class ConnectorError(RuntimeError):
    pass


# ---------------------------------------------------------------- inner messages

def send_message(frames: SequencedTLS, data: bytes) -> None:
    if not data or len(data) > MAX_MESSAGE:
        raise ConnectorError("message size out of bounds")
    chunks = [data[i:i + MESSAGE_CHUNK] for i in range(0, len(data), MESSAGE_CHUNK)]
    frames.send(json.dumps({"chunks": len(chunks), "length": len(data)}).encode())
    for chunk in chunks:
        frames.send(chunk)


def recv_message(frames: SequencedTLS) -> bytes:
    _, raw = frames.receive()
    try:
        header = json.loads(raw)
        count, length = header["chunks"], header["length"]
    except (ValueError, TypeError, KeyError) as exc:
        raise ConnectorError("malformed message header") from exc
    if not (isinstance(count, int) and isinstance(length, int) and 0 < length <= MAX_MESSAGE
            and count == -(-length // MESSAGE_CHUNK)):
        raise ConnectorError("message header out of bounds")
    data = bytearray()
    for _ in range(count):
        data.extend(frames.receive()[1])
    if len(data) != length:
        raise ConnectorError("message length mismatch")
    return bytes(data)


# ---------------------------------------------------------------- relay client

class _Stream:
    """A socketpair bridging a blocking TLS thread to relay DATA frames."""

    def __init__(self, client: "RelayClient", stream_id: str) -> None:
        self.client, self.stream_id = client, stream_id
        self.relay_end, self.app_end = socket.socketpair()
        self.relay_end.setblocking(False)
        self.remote_closed = False
        self.pump: asyncio.Task | None = None

    def start(self) -> None:
        self.pump = asyncio.get_running_loop().create_task(self._pump())

    async def _pump(self) -> None:
        loop = asyncio.get_running_loop()
        try:
            while True:
                data = await loop.sock_recv(self.relay_end, MAX_RAW_CHUNK)
                if not data:
                    break
                await self.client._send("DATA", self.stream_id, data)
        except (OSError, ConnectorError):
            pass
        finally:
            try:
                if not self.remote_closed:
                    try:
                        await self.client._send("CLOSE", self.stream_id)
                    except (OSError, ConnectorError):
                        pass
            finally:
                if self.client._streams.get(self.stream_id) is self:
                    self.client._streams.pop(self.stream_id)
                self.relay_end.close()

    async def deliver(self, raw: bytes) -> None:
        await asyncio.get_running_loop().sock_sendall(self.relay_end, raw)

    def remote_close(self) -> None:
        self.remote_closed = True
        try:
            self.relay_end.shutdown(socket.SHUT_WR)
        except OSError:
            pass

    def abort(self) -> None:
        self.remote_closed = True
        if self.pump is not None:
            self.pump.cancel()
        self.relay_end.close()


class RelayClient:
    """Owns one outbound WSS session (reconnecting) for one link."""

    def __init__(self, *, relay_url: str, identity: str, peer_identity: str, link_id: str, room_id: str,
                 credential: str, ssl_context: ssl.SSLContext | None = None,
                 on_new_stream: Callable[[socket.socket, str], None] | None = None) -> None:
        parts = urlsplit(relay_url)
        if parts.scheme not in {"ws", "wss"} or not parts.hostname:
            raise ConnectorError("relay_url must be ws(s)://host[:port]/data")
        if parts.scheme == "ws" and not ipaddress.ip_address(_resolve_literal(parts.hostname)).is_loopback:
            raise ConnectorError("plaintext ws:// is allowed only to a loopback relay")
        self.relay_url, self.identity, self.peer_identity = relay_url, identity, peer_identity
        self.link_id, self.room_id, self._credential = link_id, room_id, credential
        self.ssl_context, self.on_new_stream = ssl_context, on_new_stream
        self.incarnation: int | None = None
        self.sessions = 0
        self._ws = None
        self._streams: dict[str, _Stream] = {}
        # Ids ended by a relay STREAM_ERROR in the current session. Late DATA for
        # them is answered stream-locally (CLOSE) and never opens a new stream.
        self._ended: dict[str, None] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._connected = threading.Event()
        self._stopping = False
        self._teardown_errors: list[str] = []
        self._run_task: asyncio.Task | None = None
        self._application_lock = threading.Lock()
        self._application_threads: set[threading.Thread] = set()

    # thread-safe API --------------------------------------------------------
    def start(self, *, wait: float = 15.0) -> None:
        self._thread = threading.Thread(target=self._thread_main, name=f"relay-{self.identity}", daemon=True)
        self._thread.start()
        if not self._connected.wait(wait):
            raise ConnectorError("relay session not established")

    def stop(self) -> None:
        self._stopping = True
        if self._loop is not None and self._loop.is_running():
            async def shutdown() -> None:
                try:
                    if self._ws is not None:
                        await asyncio.wait_for(self._ws.close(code=1000, reason="connector stopping"), 1)
                finally:
                    # Cancel the session owner, never the finalizer/drain task
                    # which may already be running after websocket close.
                    if self._run_task is not None and not self._run_task.done():
                        self._run_task.cancel()
            future = asyncio.run_coroutine_threadsafe(shutdown(), self._loop)
            try:
                future.result(2)
            except Exception as exc:
                self._teardown_errors.append("shutdown_" + type(exc).__name__)
                future.cancel()
        if self._thread is not None:
            self._thread.join(3)
        deadline = time.monotonic() + 3
        for thread in self.application_threads:
            thread.join(max(0, deadline - time.monotonic()))
        if self.application_threads or (self._thread is not None and self._thread.is_alive()) or (
                self._loop is not None and not self._loop.is_closed()):
            raise ConnectorError("connector_teardown_incomplete")
        if self._teardown_errors:
            raise ConnectorError("connector_teardown_failed:" + ",".join(sorted(set(self._teardown_errors))))

    @property
    def application_threads(self) -> tuple[threading.Thread, ...]:
        """All serving callbacks still owned, including ones blocked in native I/O."""
        with self._application_lock:
            return tuple(self._application_threads)

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    def open_stream(self, timeout: float = STREAM_OPEN_TIMEOUT) -> socket.socket:
        if self._loop is None or not self._connected.is_set():
            raise ConnectorError("relay link unavailable")
        future = asyncio.run_coroutine_threadsafe(self._open_stream(), self._loop)
        return future.result(timeout)

    # loop internals -----------------------------------------------------------
    def _thread_main(self) -> None:
        self._loop = asyncio.new_event_loop()
        try:
            self._run_task = self._loop.create_task(self._run())
            self._loop.run_until_complete(self._run_task)
        except asyncio.CancelledError:
            pass
        finally:
            pending = asyncio.all_tasks(self._loop)
            for task in pending:
                if not task.cancelling():
                    task.cancel()
            async def drain(tasks):
                if not tasks:
                    return set()
                done, remaining = await asyncio.wait(tasks, timeout=1)
                for task in done:
                    if not task.cancelled():
                        task.exception()  # retrieve, never render transport diagnostics
                if remaining:
                    self._teardown_errors.append("drain_timeout")
                    for task in remaining:
                        task.cancel()  # second cancellation interrupts a suspended CLOSE
                    done, remaining = await asyncio.wait(remaining, timeout=1)
                    for task in done:
                        if not task.cancelled():
                            task.exception()
                return remaining

            remaining = self._loop.run_until_complete(drain(pending))
            if not remaining:
                closing = self._loop.create_task(self._loop.shutdown_asyncgens())
                remaining = self._loop.run_until_complete(drain({closing}))
            # Never destroy pending tasks or pretend an open loop was released.
            # Non-cooperative coroutines require caller escalation, not an unbounded join.
            if not remaining:
                self._loop.close()
            else:
                self._teardown_errors.append("pending_tasks")

    async def _run(self) -> None:
        delay = 1.0
        while not self._stopping:
            try:
                await self._session()
                delay = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # any transport failure → reconnect with backoff
                log.info(json.dumps({"event": "relay_session_failed", "identity": self.identity,
                                     "error": type(exc).__name__ + ": " + str(exc)[:200]}))
            self._connected.clear()
            if self._stopping:
                break
            await asyncio.sleep(delay + random.uniform(0, delay / 2))
            delay = min(delay * 2, 30.0)

    async def _session(self) -> None:
        from websockets.asyncio.client import connect

        async with connect(self.relay_url, ssl=self.ssl_context, max_size=MAX_FRAME_BYTES + 1024,
                           open_timeout=10, ping_interval=20, ping_timeout=20, close_timeout=5,
                           proxy=None) as ws:
            await ws.send(json.dumps({"type": "AUTH", "identity": self.identity, "link_id": self.link_id,
                                      "credential": self._credential}))
            ready = json.loads(await asyncio.wait_for(ws.recv(), 10))
            if ready.get("type") != "READY" or not isinstance(ready.get("incarnation"), int):
                raise ConnectorError(f"relay refused session: {ready.get('error', ready.get('type'))}")
            self._ws, self.incarnation = ws, ready["incarnation"]
            self.sessions += 1
            self._connected.set()
            log.info(json.dumps({"event": "relay_ready", "identity": self.identity, "incarnation": self.incarnation}))
            try:
                async for raw in ws:
                    await self._dispatch(json.loads(raw))
            finally:
                self._connected.clear()
                self._ws = None
                for stream in list(self._streams.values()):
                    stream.abort()
                self._streams.clear()
                self._ended.clear()

    async def _dispatch(self, frame: dict[str, Any]) -> None:
        kind, stream_id = frame.get("type"), frame.get("stream_id")
        if kind == "ERROR":
            raise ConnectorError(f"relay closed session: {frame.get('error')}")
        if not isinstance(stream_id, str) or not stream_id:
            raise ConnectorError("relay frame without stream id")
        if kind == "STREAM_ERROR":
            stream = self._streams.get(stream_id)
            if stream is not None:
                stream.abort()
                # The pump finalizer may never run (cancel before its first step),
                # so unmap here; identity-conditional like every other removal (R1).
                if self._streams.get(stream_id) is stream:
                    self._streams.pop(stream_id)
                self._ended[stream_id] = None
                while len(self._ended) > MAX_ENDED_STREAM_IDS:
                    self._ended.pop(next(iter(self._ended)))
            return
        if frame.get("source") != self.peer_identity or frame.get("link_id") != self.link_id \
                or frame.get("room_id") != self.room_id or frame.get("target") != self.identity:
            raise ConnectorError("relay delivered a frame for another route")
        stream = self._streams.get(stream_id)
        if kind == "CLOSE":
            if stream is not None:
                stream.remote_close()
            return
        if kind != "DATA":
            raise ConnectorError("unexpected relay frame")
        if stream is None and stream_id in self._ended:
            # Late DATA for a relay-aborted stream: answer for this id only;
            # never resurrect it and never fail the shared session.
            await self._send("CLOSE", stream_id)
            return
        if stream is None:
            if self.on_new_stream is None:
                await self._send("CLOSE", stream_id)
                return
            stream = _Stream(self, stream_id)
            self._streams[stream_id] = stream
            stream.start()
            thread = threading.Thread(target=self._serve_stream, args=(stream, self.on_new_stream),
                                      name=f"room-stream-{self.identity}", daemon=True)
            with self._application_lock:
                self._application_threads.add(thread)
            try:
                thread.start()
            except BaseException:
                with self._application_lock:
                    self._application_threads.discard(thread)
                stream.app_end.close()
                stream.abort()
                if self._streams.get(stream_id) is stream:
                    self._streams.pop(stream_id)
                raise
        raw = base64.b64decode(frame["payload"], validate=True)
        try:
            await stream.deliver(raw)
        except OSError:
            # A rejected TLS request closes its app socket while peer bytes may
            # still be queued (BrokenPipe/Reset), or the local end is already
            # closed (EBADF). Either way end that stream, not the shared WSS session.
            stream.abort()
            if self._streams.get(stream_id) is stream:
                self._streams.pop(stream_id)
            await self._send("CLOSE", stream_id)

    def _serve_stream(self, stream: _Stream, callback: Callable[[socket.socket, str], None]) -> None:
        try:
            callback(stream.app_end, stream.stream_id)
        except Exception as exc:
            # Arbitrary callback diagnostics may contain credential material.
            log.info(json.dumps({"event": "serving_failed", "error": type(exc).__name__}))
        finally:
            stream.app_end.close()
            with self._application_lock:
                self._application_threads.discard(threading.current_thread())

    async def _open_stream(self) -> socket.socket:
        stream = _Stream(self, uuid.uuid4().hex)
        self._streams[stream.stream_id] = stream
        stream.start()
        return stream.app_end

    async def _send(self, kind: str, stream_id: str, raw: bytes | None = None) -> None:
        ws = self._ws
        if ws is None or self.incarnation is None:
            raise ConnectorError("relay link unavailable")
        frame = {"type": kind, "source": self.identity, "target": self.peer_identity, "room_id": self.room_id,
                 "link_id": self.link_id, "incarnation": self.incarnation, "stream_id": stream_id}
        if kind == "DATA":
            frame["payload"] = base64.b64encode(raw or b"").decode("ascii")
        await ws.send(json.dumps(frame, separators=(",", ":")))


def _resolve_literal(host: str) -> str:
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        return "127.0.0.1" if host == "localhost" else "0.0.0.0"


# ---------------------------------------------------------------- roles

class HostConnector:
    """Host side: native host gateway → loopback adapter → firewall → this → relay."""

    def __init__(self, client: RelayClient, *, inner: dict[str, str]) -> None:
        self.client = client
        self.context = client_context(ca_file=inner["peer_ca_file"], cert_file=inner["cert_file"], key_file=inner["key_file"])
        self.peer_fingerprint = inner["peer_fingerprint"]

    def forward(self, request: ForwardedRequest) -> UpstreamResponse:
        # One absolute budget, NOT HOLD_BOUND + REQUEST_TIMEOUT per attempt.
        # Serialize once: a dispatch retry cannot change its key, body or grant.
        deadline = time.monotonic() + min(HOLD_BOUND, REQUEST_TIMEOUT)
        payload = json.dumps({"method": request.method, "path": request.path,
                              "headers": [list(h) for h in request.headers],
                              "body": base64.b64encode(request.body).decode()},
                             separators=(",", ":")).encode()
        replay_safe = request.method == 'GET' or (
            request.method == 'POST' and request.path == '/v1/runs'
            and any(k.lower() == 'idempotency-key' and v for k, v in request.headers))
        while time.monotonic() < deadline:
            sock = tls = None
            try:
                try:
                    sock = self.client.open_stream(timeout=min(STREAM_OPEN_TIMEOUT,
                                                              deadline - time.monotonic()))
                except Exception:
                    # No application bytes sent: any method may wait for its first stream.
                    pass
                else:
                    sock.settimeout(max(.001, deadline - time.monotonic()))
                    tls = wrap_client(sock, self.context, expected_fingerprint=self.peer_fingerprint)
                    frames = SequencedTLS(tls, deadline=deadline)
                    send_message(frames, payload)
                    result = json.loads(recv_message(frames))
                    return UpstreamResponse(int(result["status"]), tuple(tuple(h) for h in result["headers"]),
                                            base64.b64decode(result["body"]))
            except OSError as exc:
                # Only explicit transport failures are recovery signals. In particular,
                # plain SSLError includes fatal TLS alerts, not just transport EOF.
                retryable = isinstance(exc, (ConnectionError, TimeoutError, TLSStreamClosed,
                                            ssl.SSLEOFError, ssl.SSLZeroReturnError))
                if not retryable or not replay_safe:
                    raise
            finally:
                if tls is not None or sock is not None:
                    (tls or sock).close()
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(.1, remaining))
        return _UNAVAILABLE


class _NoCredentialRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Reject BEFORE a credential can leave the fixed backend origin.


def native_http_backend(target_base: str) -> Callable[[ForwardedRequest], UpstreamResponse]:
    host = urlsplit(target_base).hostname or ""
    if urlsplit(target_base).scheme != "http" or not ipaddress.ip_address(_resolve_literal(host)).is_loopback:
        raise ConnectorError("native target must be a fixed loopback http:// base")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoCredentialRedirect())

    def forward(request: ForwardedRequest) -> UpstreamResponse:
        native = urllib.request.Request(target_base + request.path, data=request.body or None,
                                        method=request.method, headers=dict(request.headers))
        try:
            with opener.open(native, timeout=25) as response:
                headers = tuple((k, v) for k, v in response.headers.items() if k.lower() in {"content-type", "content-length"})
                return UpstreamResponse(response.status, headers, response.read())
        except urllib.error.HTTPError as exc:
            headers = tuple((k, v) for k, v in exc.headers.items() if k.lower() in {"content-type", "content-length"})
            return UpstreamResponse(exc.code, headers, exc.read())
    return forward


class TargetConnector:
    """Target side: relay → inner TLS server → firewall → fixed native loopback API."""

    def respond(self, request: Request) -> UpstreamResponse:
        try:
            return self.firewall.forward(request)
        except UpstreamAmbiguous as exc:
            # Forwarded to the native API, outcome withheld/unknown: 5xx so the host
            # client keeps it ambiguous; a 403 would read as a definitive refusal.
            log.info(json.dumps({"event": "upstream_ambiguous", "reason": str(exc)[:80]}))
            return _AMBIGUOUS
        except FirewallDenied:
            return _DENIED

    def __init__(self, *, inner: dict[str, str], firewall: RoomFirewall) -> None:
        self.context = server_context(ca_file=inner["peer_ca_file"], cert_file=inner["cert_file"], key_file=inner["key_file"])
        self.peer_fingerprint = inner["peer_fingerprint"]
        self.firewall = firewall
        self.served = 0

    def serve(self, sock: socket.socket, stream_id: str = "") -> None:
        sock.settimeout(REQUEST_TIMEOUT)
        tls = None
        try:
            tls = wrap_server(sock, self.context, expected_fingerprint=self.peer_fingerprint)
            frames = SequencedTLS(tls)
            raw = json.loads(recv_message(frames))
            request = Request(raw["method"], raw["path"], tuple(tuple(h) for h in raw["headers"]),
                              base64.b64decode(raw["body"]))
            response = self.respond(request)
            send_message(frames, json.dumps({"status": response.status, "headers": [list(h) for h in response.headers],
                                             "body": base64.b64encode(response.body).decode()},
                                            separators=(",", ":")).encode())
            self.served += 1
        except Exception as exc:  # a broken stream never takes the daemon down
            log.info(json.dumps({"event": "stream_failed", "error": type(exc).__name__}))
        finally:
            (tls or sock).close()


# ---------------------------------------------------------------- config + CLI

def _expand_paths(value):
    if isinstance(value, dict):
        return {key: _expand_paths(item) for key, item in value.items()}
    if isinstance(value, str) and value.startswith("~/"):
        return str(Path(value).expanduser())
    return value


def _read_secret(path: str) -> str:
    file = Path(path)
    mode = stat.S_IMODE(file.stat().st_mode)
    if mode & 0o077:
        raise ConnectorError(f"credential file {path} must be private (0600), is {oct(mode)}")
    return file.read_text(encoding="utf-8").strip()


def _relay_tls(config: dict) -> ssl.SSLContext | None:
    if urlsplit(config["relay_url"]).scheme != "wss":
        return None
    context = ssl.create_default_context(cafile=config.get("relay_ca_file"))
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


def build_client(config: dict, on_new_stream=None) -> RelayClient:
    return RelayClient(relay_url=config["relay_url"], identity=config["identity"],
                       peer_identity=config["peer_identity"], link_id=config["link_id"],
                       room_id=config["room_id"], credential=_read_secret(config["credential_file"]),
                       ssl_context=_relay_tls(config), on_new_stream=on_new_stream)


def run_target(config: dict) -> tuple[RelayClient, TargetConnector]:
    from run_bindings import NativeReceiptVerifier
    firewall = RoomFirewall(expected_scope=config["scope"], backend=native_http_backend(config["target_base"]),
                            verify_native=NativeReceiptVerifier(config["native_receipt_db"]))
    target = TargetConnector(inner=config["inner_tls"], firewall=firewall)
    client = build_client(config, on_new_stream=target.serve)
    client.start()
    return client, target


def run_host(config: dict):
    from adapter import LoopbackAdapter
    from run_bindings import HostRunBindings

    bindings = HostRunBindings(config["host_binding_db"])
    client = build_client(config)
    client.start()
    host = HostConnector(client, inner=config["inner_tls"])
    adapter = LoopbackAdapter()
    adapter_cfg = config.get("adapter", {})
    adapter.start(host=adapter_cfg.get("host", "127.0.0.1"), port=int(adapter_cfg.get("port", 0)))
    url = adapter.bind(config["link_id"], RoomFirewall(expected_scope=config["scope"], backend=host.forward,
                                                       verify_native=bindings, on_admitted=bindings.record_ack),
                       target_url="relay:" + config["link_id"], scope=config["scope"])
    return client, adapter, url


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("role", choices=("host", "target"))
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args(argv)
    config = _expand_paths(json.loads(args.config.read_text(encoding="utf-8")))
    if config.get("role") != args.role:
        raise ConnectorError("config role does not match the requested role")
    done = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: done.set())
    signal.signal(signal.SIGINT, lambda *_: done.set())
    if args.role == "target":
        client, _ = run_target(config)
        print(json.dumps({"event": "target_ready", "incarnation": client.incarnation}), flush=True)
    else:
        client, adapter, url = run_host(config)
        print(json.dumps({"event": "host_ready", "adapter_url": url, "incarnation": client.incarnation}), flush=True)
    done.wait()
    client.stop()
    print(json.dumps({"event": "stopped"}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
