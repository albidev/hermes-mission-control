"""Product-owned outbound-only WSS stream transport for Invite Rooms.

The WSS relay authenticates opaque endpoint credentials; all application bytes
remain inside the existing peer-pinned mTLS stream. Relay URLs are owner-configured,
never request-selected. A stream label is routing metadata only and can address
only a fixed loopback service supplied by the local sidecar.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import json
import logging
import math
import random
import re
import select
import socket
import ssl
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

log = logging.getLogger("invite-rooms-relay")
MAX_FRAME_BYTES = 64 * 1024
MAX_RAW_CHUNK = 32 * 1024
MAX_STREAMS = 4
HANDSHAKE_TIMEOUT = 5.0
STREAM_OPEN_TIMEOUT = 10.0
MAX_ENDED_STREAM_IDS = 1024
BRIDGE_IDLE_TIMEOUT = 66.0
_CHANNEL_RE = re.compile(r"(?:control|data:link-[0-9a-f]{32})\Z")

class RelayTransportError(RuntimeError):
    """Sanitized relay transport failure; never embeds secrets or peer payloads."""

ConnectorError = RelayTransportError


def _literal_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


def _resolve_literal(host: str) -> str:
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        return "127.0.0.1" if host == "localhost" else "0.0.0.0"


def validate_channel(channel: str) -> str:
    if not isinstance(channel, str) or not _CHANNEL_RE.fullmatch(channel):
        raise RelayTransportError("relay_channel_denied")
    return channel


def channel_from_stream_id(stream_id: str) -> str | None:
    if not isinstance(stream_id, str) or len(stream_id) > 128:
        return None
    channel, separator, nonce = stream_id.partition("|")
    if not separator or not re.fullmatch(r"[0-9a-f]{32}", nonce):
        return None
    try:
        return validate_channel(channel)
    except RelayTransportError:
        return None


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
        if (parts.scheme not in {"ws", "wss"} or not parts.hostname or parts.path != "/data"
                or parts.username is not None or parts.password is not None or parts.query or parts.fragment
                or parts.port is None or not 0 < parts.port <= 65535):
            raise RelayTransportError("invalid_relay_endpoint")
        if parts.scheme == "ws" and not _literal_loopback(parts.hostname):
            raise RelayTransportError("plaintext_relay_not_loopback")
        if (not all(isinstance(v, str) and v and len(v) <= 128 for v in
                    (identity, peer_identity, link_id, room_id))
                or not isinstance(credential, str) or not credential or len(credential) > 2048
                or any(c in credential for c in "\r\n\x00")):
            raise RelayTransportError("invalid_relay_link")
        if ssl_context is not None and (ssl_context.verify_mode != ssl.CERT_REQUIRED or not ssl_context.check_hostname):
            raise RelayTransportError("relay_tls_verification_required")
        if parts.scheme == "wss" and ssl_context is None:
            ssl_context = ssl.create_default_context()
            ssl_context.minimum_version = ssl.TLSVersion.TLSv1_2
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
            raise RelayTransportError("relay session not established")

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
            raise RelayTransportError("connector_teardown_incomplete")
        if self._teardown_errors:
            raise RelayTransportError("connector_teardown_failed:" + ",".join(sorted(set(self._teardown_errors))))

    @property
    def application_threads(self) -> tuple[threading.Thread, ...]:
        """All serving callbacks still owned, including ones blocked in native I/O."""
        with self._application_lock:
            return tuple(self._application_threads)

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    def open_stream(self, *, channel: str, timeout: float = STREAM_OPEN_TIMEOUT) -> socket.socket:
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise RelayTransportError("relay link unavailable")
        validate_channel(channel)
        deadline = time.monotonic() + timeout
        loop = self._loop
        if loop is None or not self._connected.wait(timeout):
            raise RelayTransportError("relay link unavailable")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RelayTransportError("relay link unavailable")
        coroutine = self._open_stream(channel)
        try:
            future = asyncio.run_coroutine_threadsafe(coroutine, loop)
        except Exception:
            coroutine.close()
            raise RelayTransportError("relay link unavailable") from None
        try:
            return future.result(remaining)
        except TimeoutError:
            if not future.cancel():
                try:
                    late_stream = future.result(timeout=0)
                except Exception:
                    late_stream = None
                if isinstance(late_stream, socket.socket):
                    late_stream.close()
            raise RelayTransportError("relay link unavailable") from None

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
                                     "error": type(exc).__name__}))
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
            if (not isinstance(ready, dict) or set(ready) != {"type", "incarnation"}
                    or ready.get("type") != "READY" or type(ready.get("incarnation")) is not int or ready["incarnation"] < 1):
                raise RelayTransportError(f"relay refused session: {ready.get('error', ready.get('type'))}")
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
            raise RelayTransportError(f"relay closed session: {frame.get('error')}")
        if not isinstance(stream_id, str) or not stream_id:
            raise RelayTransportError("relay frame without stream id")
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
            raise RelayTransportError("relay delivered a frame for another route")
        stream = self._streams.get(stream_id)
        if kind == "CLOSE":
            if stream is not None:
                stream.remote_close()
            return
        if kind != "DATA":
            raise RelayTransportError("unexpected relay frame")
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

    async def _open_stream(self, channel: str) -> socket.socket:
        stream = _Stream(self, f"{channel}|{uuid.uuid4().hex}")
        self._streams[stream.stream_id] = stream
        stream.start()
        return stream.app_end

    async def _send(self, kind: str, stream_id: str, raw: bytes | None = None) -> None:
        ws = self._ws
        if ws is None or self.incarnation is None:
            raise RelayTransportError("relay link unavailable")
        frame = {"type": kind, "source": self.identity, "target": self.peer_identity, "room_id": self.room_id,
                 "link_id": self.link_id, "incarnation": self.incarnation, "stream_id": stream_id}
        if kind == "DATA":
            frame["payload"] = base64.b64encode(raw or b"").decode("ascii")
        await ws.send(json.dumps(frame, separators=(",", ":")))

class RelayBridge:
    """Bridge labelled relay streams to owner-fixed loopback services only."""
    def __init__(self, service_targets: Mapping[str, tuple[str, int]], *, timeout: float = BRIDGE_IDLE_TIMEOUT,
                 authorize: Callable[[str], bool] | None = None):
        if not isinstance(service_targets, Mapping) or not service_targets:
            raise ValueError("relay bridge needs fixed services")
        if type(timeout) not in (int, float) or timeout <= 0 or timeout > 120:
            raise ValueError("invalid relay bridge timeout")
        targets = {}
        for channel, address in service_targets.items():
            validate_channel(channel)
            if (not isinstance(address, tuple) or len(address) != 2 or type(address[0]) is not str
                    or address[0] not in {"127.0.0.1", "::1"} or type(address[1]) is not int
                    or not 0 < address[1] <= 65535):
                raise ValueError("relay bridge target must be an explicit loopback socket")
            targets[channel] = address
        self._targets = targets
        if authorize is not None and not callable(authorize):
            raise ValueError("relay bridge authorizer must be callable")
        self._authorize = authorize
        self._timeout = float(timeout)
        self._lock = threading.Lock()
        self._sockets: set[socket.socket] = set()
        self._closing = False
        self.accepted = 0
        self.refused = 0

    def serve_stream(self, relay_socket: socket.socket, stream_id: str) -> None:
        channel = channel_from_stream_id(stream_id)
        if channel is None:
            with self._lock:
                self.refused += 1
            relay_socket.close()
            return
        with self._lock:
            target = None if self._closing else self._targets.get(channel)
        if target is None:
            with self._lock:
                self.refused += 1
            relay_socket.close()
            return
        allowed = True
        if self._authorize is not None:
            try:
                allowed = self._authorize(channel) is True
            except Exception:
                allowed = False
        if not allowed:
            with self._lock:
                self.refused += 1
            relay_socket.close()
            return
        with self._lock:
            if self._closing:
                self.refused += 1
                relay_socket.close()
                return
            self._sockets.add(relay_socket)
        upstream = None
        try:
            upstream = socket.create_connection(target, timeout=min(self._timeout, 5.0))
            upstream.settimeout(None)
            with self._lock:
                if self._closing:
                    return
                self._sockets.add(upstream)
                self.accepted += 1
            self._copy_duplex(relay_socket, upstream)
        except OSError:
            return
        finally:
            with self._lock:
                self._sockets.discard(relay_socket)
                if upstream is not None:
                    self._sockets.discard(upstream)
            for sock in (relay_socket, upstream):
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass

    def _copy_duplex(self, left: socket.socket, right: socket.socket) -> None:
        peers = {left: right, right: left}
        readers = {left, right}
        last = time.monotonic()
        while readers:
            with self._lock:
                if self._closing:
                    return
            remaining = self._timeout - (time.monotonic() - last)
            if remaining <= 0:
                return
            try:
                ready, _, _ = select.select(list(readers), [], [], min(remaining, .25))
            except (OSError, ValueError):
                return
            for source in ready:
                try:
                    payload = source.recv(MAX_RAW_CHUNK)
                except OSError:
                    payload = b""
                destination = peers[source]
                if not payload:
                    readers.discard(source)
                    try:
                        destination.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass
                    continue
                try:
                    destination.sendall(payload)
                    last = time.monotonic()
                except OSError:
                    return

    def close(self) -> None:
        with self._lock:
            self._closing = True
            sockets = tuple(self._sockets)
        for sock in sockets:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass


def relay_identity_for(installation_id: str, link_id: str) -> str:
    """Stable pair-scoped pseudonym for the relay; native identity remains inner-mTLS authority."""
    if (not isinstance(installation_id, str) or not installation_id.startswith("install:")
            or len(installation_id) > 64 or not isinstance(link_id, str) or not link_id or len(link_id) > 128):
        raise RelayTransportError("invalid_relay_identity")
    digest = hashlib.sha256(b"invite-rooms-relay-v1\0" + link_id.encode("utf-8") + b"\0"
                            + installation_id.encode("ascii", errors="strict")).hexdigest()[:32]
    return "relay-" + digest


def read_relay_credential(path: str | Path) -> str:
    """Read one owner-private relay credential without ever echoing it."""
    from invite_rooms import private_fs
    directory_fd = file_fd = None
    try:
        file = Path(path)
        directory_fd = private_fs.open_private_dir(file.parent, file.parent, create=False)
        file_fd = private_fs.open_regular(directory_fd, file.name)
        raw = private_fs.read_all(file_fd, 2048)
        value = raw.decode("ascii")
        if not value or any(c.isspace() for c in value):
            raise ValueError
        return value
    except (OSError, ValueError, UnicodeError, private_fs.PrivateStorageError):
        raise RelayTransportError("relay_credential_unavailable") from None
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if directory_fd is not None:
            os.close(directory_fd)


@dataclass
class _RelaySession:
    client: RelayClient
    bridge: RelayBridge
    trust: Any
    start_lock: threading.Lock = field(default_factory=threading.Lock)
    started: bool = False


class RelaySessionManager:
    """Per-sidecar relay sessions. Journal trust chooses the peer; config only chooses transport."""
    def __init__(self, *, relay_url: str, links: Mapping, installation_id: str, authority,
                 control_target: tuple[str, int] | None, data_targets: Mapping[str, tuple[str, int]],
                 relay_ssl_context: ssl.SSLContext | None = None):
        if not isinstance(links, Mapping) or not isinstance(data_targets, Mapping):
            raise ValueError("invalid relay session configuration")
        self.relay_url = relay_url
        self.links = dict(links)
        self.installation_id = installation_id
        self.authority = authority
        self.control_target = control_target
        self.data_targets = dict(data_targets)
        if relay_ssl_context is not None and (relay_ssl_context.verify_mode != ssl.CERT_REQUIRED
                or not relay_ssl_context.check_hostname):
            raise ValueError("relay TLS verification required")
        # Dependency injection permits a test CA; product construction passes None (system trust only).
        self._relay_ssl_context = relay_ssl_context
        self._lock = threading.RLock()
        self._sessions: dict[tuple[str, int], _RelaySession] = {}
        self._closed = False

    @staticmethod
    def _same_trust(left, right) -> bool:
        return (left is not None and left.endpoint_id == right.endpoint_id
                and left.generation == right.generation and left.role == right.role
                and left.peer_id == right.peer_id and left.ca_pem == right.ca_pem
                and left.fingerprint == right.fingerprint and left.binding == right.binding)

    def _check_link(self, trust):
        from invite_rooms.pairing_journal import PeerTrust
        if type(trust) is not PeerTrust or trust.protocol_state not in ("confirmed", "acknowledged"):
            raise RelayTransportError("relay_link_unavailable")
        link = self.links.get(trust.endpoint_id)
        if (link is None or link.endpoint_id != trust.endpoint_id or link.generation != trust.generation
                or link.peer_id != trust.peer_id):
            raise RelayTransportError("relay_link_unavailable")
        peer_installation_id = trust.binding_dict().get("target_install_id")
        if (not isinstance(peer_installation_id, str) or not peer_installation_id.startswith("install:")
                or peer_installation_id == self.installation_id):
            raise RelayTransportError("relay_link_unavailable")
        return link, peer_installation_id

    def _new_session(self, trust, link, peer_installation_id) -> _RelaySession:
        channels = {}
        if self.control_target is not None:
            channels["control"] = self.control_target
        if trust.role == "joining":
            channels.update({"data:" + adapter_id: address for adapter_id, address in self.data_targets.items()})
        def authorize(channel):
            states = ("confirmed", "acknowledged") if channel == "control" else ("acknowledged",)
            if channel != "control" and trust.role != "joining":
                return False
            try:
                fresh = self.authority.peer_trust(trust.endpoint_id, trust.generation,
                                                  role=trust.role, states=states)
            except Exception:
                return False
            return self._same_trust(fresh, trust)
        bridge = RelayBridge(channels, authorize=authorize) if channels else None
        identity = relay_identity_for(self.installation_id, link.link_id)
        peer_identity = relay_identity_for(peer_installation_id, link.link_id)
        credential = read_relay_credential(link.credential_file)
        client = RelayClient(relay_url=self.relay_url, identity=identity, peer_identity=peer_identity,
            link_id=link.link_id, room_id=link.relay_room_id, credential=credential,
            ssl_context=self._relay_ssl_context,
            on_new_stream=bridge.serve_stream if bridge is not None else None)
        if bridge is None:
            raise RelayTransportError("relay_link_has_no_local_services")
        return _RelaySession(client=client, bridge=bridge, trust=trust)

    def client_for(self, trust) -> RelayClient:
        link, peer_installation_id = self._check_link(trust)
        key = (trust.endpoint_id, trust.generation)
        with self._lock:
            if self._closed:
                raise RelayTransportError("relay_manager_closed")
            session = self._sessions.get(key)
            if session is None:
                session = self._new_session(trust, link, peer_installation_id)
                self._sessions[key] = session
        # Re-read the journal outside the manager lock and before exposing the
        # shared stream. HostConnector still performs admission at the effect boundary.
        try:
            fresh = self.authority.peer_trust(trust.endpoint_id, trust.generation,
                                              role=trust.role, states=("confirmed", "acknowledged"))
        except Exception:
            fresh = None
        if not self._same_trust(fresh, trust):
            self.stop_link(*key)
            raise RelayTransportError("relay_link_unavailable")
        with session.start_lock:
            if not session.started:
                session.started = True
                try:
                    session.client.start(wait=.25)
                except RelayTransportError:
                    # Client retains its bounded reconnect loop; calls fail closed
                    # until the public WSS session is ready.
                    pass
        return session.client

    def refresh(self) -> int:
        """Start/reuse sessions for currently journal-active links; no journal state is inferred from config."""
        try:
            trusts = self.authority.active_trusts()
        except Exception:
            return 0
        started = 0
        for trust in trusts:
            if trust.endpoint_id not in self.links:
                continue
            try:
                self.client_for(trust)
                started += 1
            except Exception:
                continue
        return started

    def start(self) -> None:
        self.refresh()

    def wait_connected(self, endpoint_id: str, generation: int, timeout: float = 10.0) -> bool:
        with self._lock:
            session = self._sessions.get((endpoint_id, generation))
        return bool(session and session.client.connected) if timeout <= 0 else bool(
            session and session.client._connected.wait(timeout))

    def _close_sessions(self, selected, *, error_code: str) -> None:
        failures = []
        closed = []
        for key, session in selected:
            session_failed = False
            try:
                session.bridge.close()
            except Exception:
                session_failed = True
            try:
                session.client.stop()
            except Exception:
                session_failed = True
            if session_failed:
                failures.append(key)
            else:
                closed.append((key, session))
        with self._lock:
            for key, session in closed:
                if self._sessions.get(key) is session:
                    self._sessions.pop(key)
        if failures:
            raise RelayTransportError(error_code)

    def stop_link(self, endpoint_id: str, generation: int | None = None) -> None:
        with self._lock:
            selected = [(key, session) for key, session in self._sessions.items()
                        if key[0] == endpoint_id and (generation is None or key[1] == generation)]
        self._close_sessions(selected, error_code="relay_session_cleanup_incomplete")

    def stop_peer(self, peer_id: str) -> None:
        """Stop sessions whose owner-private route belongs to the just-unpaired peer."""
        endpoints = [endpoint_id for endpoint_id, link in self.links.items()
                    if getattr(link, "peer_id", None) == peer_id]
        failures = False
        for endpoint_id in endpoints:
            try:
                self.stop_link(endpoint_id)
            except RelayTransportError:
                failures = True
        if failures:
            raise RelayTransportError("relay_session_cleanup_incomplete")

    def close(self) -> None:
        with self._lock:
            self._closed = True
            selected = list(self._sessions.items())
        self._close_sessions(selected, error_code="relay_manager_cleanup_incomplete")

    @property
    def session_count(self) -> int:
        with self._lock:
            return len(self._sessions)
