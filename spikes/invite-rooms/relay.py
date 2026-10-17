"""Authenticated opaque relay for the invite-only Rooms spike.

The relay handles only provisioned routing metadata and opaque DATA payloads. It
has no Room/event model, grant verifier, prompt store, or offline queue. TLS is
owned by the websocket deployment; this module deliberately does not claim
end-to-end confidentiality (that is Gate E's inner-TLS contract).

A link has exactly two endpoints. Either endpoint may open a stream and the
other answers on the same ``stream_id``; ``CLOSE`` (from either side, or
implicitly when an endpoint disconnects) releases the stream quota. Stream-level
refusals (quota, unknown stream, offline peer, backpressure) are reported per
stream and do not tear down the connection; protocol violations (spoofing,
malformed frames, stale incarnation, route mismatch) remain fatal.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import hmac
import json
import secrets
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

try:  # the websocket library is optional for the in-memory routing core
    from websockets.exceptions import ConnectionClosed
except ImportError:  # pragma: no cover - only the daemon needs websockets
    class ConnectionClosed(Exception):  # type: ignore[no-redef]
        pass

MAX_FRAME_BYTES = 64 * 1024
MAX_QUEUED_BYTES = 1024 * 1024
MAX_STREAMS = 4
HANDSHAKE_TIMEOUT = 5.0
REQUEST_TIMEOUT = 30.0
BACKPRESSURE_TIMEOUT = 5.0
# base64 inflates by 4/3 and the JSON envelope adds coordinates: a connector must
# keep raw opaque chunks at or below this size to stay inside MAX_FRAME_BYTES.
MAX_RAW_CHUNK = 32 * 1024

_COORDINATES = frozenset({"type", "source", "target", "room_id", "link_id", "incarnation", "stream_id"})
_FRAME_FIELDS = {"DATA": _COORDINATES | {"payload"}, "CLOSE": _COORDINATES}


class RelayError(ValueError):
    """Fatal protocol violation: the websocket connection is closed."""


class StreamError(RelayError):
    """Refusal scoped to one stream: reported to the sender, connection survives."""


def _require_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 128:
        raise RelayError(f"invalid {field_name}")
    return value


@dataclass(frozen=True)
class Route:
    source: str
    target: str
    room_id: str
    link_id: str
    credentials: Mapping[str, str] = field(repr=False, default_factory=dict)

    def peer_of(self, identity: str) -> str | None:
        if identity == self.source:
            return self.target
        if identity == self.target:
            return self.source
        return None


class RelayConnection:
    def __init__(self, identity: str, incarnation: int, *, link_id: str, room_id: str) -> None:
        self.identity = identity
        self.incarnation = incarnation
        self.link_id = link_id
        self.room_id = room_id
        self.queue: asyncio.Queue[tuple[dict[str, Any], int] | None] = asyncio.Queue(maxsize=MAX_STREAMS * 2)
        self.queued_bytes = 0
        self.streams: set[str] = set()
        self.closed = False

    async def receive(self) -> dict[str, Any]:
        item = await self.queue.get()
        if item is None:
            raise RelayError("connection closed")
        frame, size = item
        self.queued_bytes -= size
        return frame

    def close(self) -> None:
        self.closed = True
        try:
            self.queue.put_nowait(None)
        except asyncio.QueueFull:
            pass


class Relay:
    """In-memory bounded routing core, suitable for a websocket adapter."""

    def __init__(self, *, credential_digest: Callable[[str], str] | None = None) -> None:
        # With a digest the route table stores only credential hashes (daemon mode):
        # a leaked route file does not let anyone connect as an endpoint.
        self._digest = credential_digest or (lambda value: value)
        self._routes: dict[str, Route] = {}
        self._connections: dict[str, RelayConnection] = {}
        self._incarnations: dict[str, int] = {}
        self._link_streams: dict[str, set[str]] = {}
        self._lock = asyncio.Lock()
        self.data_listener_ready = False

    def _validate_route(self, source: str, target: str, room_id: str, link_id: str) -> None:
        values = (source, target, room_id, link_id)
        if any(not isinstance(value, str) or not value or len(value) > 128 for value in values):
            raise RelayError("route identifiers are invalid")
        if source == target:
            raise RelayError("source and target must differ")

    def provision(self, *, source: str, target: str, room_id: str, link_id: str) -> str:
        """Legacy Gate D shape: one shared credential for both endpoints."""
        self._validate_route(source, target, room_id, link_id)
        credential = secrets.token_urlsafe(32)
        self._routes[link_id] = Route(source, target, room_id, link_id,
                                      {source: self._digest(credential), target: self._digest(credential)})
        return credential

    def provision_link(self, *, source: str, target: str, room_id: str, link_id: str) -> dict[str, str]:
        """Distinct per-endpoint credentials: neither owner can connect as the other."""
        self._validate_route(source, target, room_id, link_id)
        credentials = {source: secrets.token_urlsafe(32), target: secrets.token_urlsafe(32)}
        stored = {identity: self._digest(value) for identity, value in credentials.items()}
        self._routes[link_id] = Route(source, target, room_id, link_id, stored)
        return dict(credentials)

    def load_route(self, route: Route) -> None:
        """Install an operator-provisioned route (e.g. from the daemon's route file)."""
        self._validate_route(route.source, route.target, route.room_id, route.link_id)
        if set(route.credentials) != {route.source, route.target}:
            raise RelayError("route credentials must cover exactly both endpoints")
        self._routes[route.link_id] = route

    async def connect(self, *, identity: str, link_id: str, credential: str) -> RelayConnection:
        identity = _require_text(identity, "identity")
        link_id = _require_text(link_id, "link_id")
        credential = _require_text(credential, "credential")
        route = self._routes.get(link_id)
        expected = route.credentials.get(identity) if route is not None else None
        if route is None or expected is None or not hmac.compare_digest(self._digest(credential).encode(), expected.encode()):
            raise RelayError("unprovisioned identity")
        async with self._lock:
            if identity in self._connections and not self._connections[identity].closed:
                raise RelayError("duplicate active identity")
            incarnation = self._incarnations.get(identity, 0) + 1
            self._incarnations[identity] = incarnation
            connection = RelayConnection(identity, incarnation, link_id=route.link_id, room_id=route.room_id)
            self._connections[identity] = connection
            return connection

    async def disconnect(self, connection: RelayConnection) -> None:
        async with self._lock:
            active = self._connections.get(connection.identity) is connection
            if active:
                del self._connections[connection.identity]
            connection.close()
            if not active:
                return
            open_streams = self._link_streams.pop(connection.link_id, set())
            route = self._routes.get(connection.link_id)
            peer_identity = route.peer_of(connection.identity) if route else None
            peer = self._connections.get(peer_identity) if peer_identity else None
            for stream_id in sorted(open_streams):
                connection.streams.discard(stream_id)
                if peer is None or peer.closed or peer.link_id != connection.link_id:
                    continue
                peer.streams.discard(stream_id)
                notice = {"type": "CLOSE", "source": connection.identity, "target": peer.identity,
                          "room_id": connection.room_id, "link_id": connection.link_id,
                          "incarnation": connection.incarnation, "stream_id": stream_id}
                try:
                    peer.queue.put_nowait((notice, 0))
                except asyncio.QueueFull:
                    pass  # the peer's own stream deadline covers a lost notice

    async def route(self, connection: RelayConnection, frame: Mapping[str, Any]) -> None:
        """Validate sender/route/incarnation and enqueue an opaque frame boundedly."""
        if connection.closed or self._connections.get(connection.identity) is not connection:
            raise RelayError("stale connection incarnation")
        if not isinstance(frame, Mapping):
            raise RelayError("malformed frame")
        frame_type = frame.get("type")
        expected_fields = _FRAME_FIELDS.get(frame_type) if isinstance(frame_type, str) else None
        if expected_fields is None or set(frame) != expected_fields:
            raise RelayError("malformed frame")
        if frame["source"] != connection.identity:
            raise RelayError("claimed sender spoof")
        source = _require_text(frame["source"], "source")
        target_identity = _require_text(frame["target"], "target")
        room_id = _require_text(frame["room_id"], "room_id")
        link_id = _require_text(frame["link_id"], "link_id")
        stream_id = _require_text(frame["stream_id"], "stream_id")
        incarnation = frame["incarnation"]
        if not isinstance(incarnation, int) or isinstance(incarnation, bool) or incarnation < 1:
            raise RelayError("invalid incarnation")
        if source != connection.identity:
            raise RelayError("claimed sender spoof")
        route = self._routes.get(link_id)
        if route is None or route.peer_of(connection.identity) != target_identity or route.room_id != room_id:
            raise RelayError("wrong link or room")
        if connection.link_id != link_id or connection.room_id != room_id:
            raise RelayError("connection route binding mismatch")
        if incarnation != connection.incarnation:
            raise RelayError("stale connection incarnation")
        if frame_type == "DATA":
            payload = _require_text_payload(frame["payload"])
            try:
                raw = base64.b64decode(payload.encode("ascii"), validate=True)
            except (UnicodeEncodeError, binascii.Error) as exc:
                raise RelayError("payload is not base64") from exc
            if not raw or len(raw) > MAX_FRAME_BYTES:
                raise RelayError("frame payload exceeds limit")
        encoded = json.dumps(dict(frame), separators=(",", ":")).encode("utf-8")
        target = self._connections.get(route.target if connection.identity == route.source else route.source)
        if target is None or target.closed:
            raise StreamError("target offline")
        if target.link_id != link_id or target.room_id != room_id:
            raise RelayError("target route binding mismatch")
        streams = self._link_streams.setdefault(link_id, set())
        if frame_type == "CLOSE":
            if stream_id not in streams:
                raise StreamError("unknown stream")
        elif stream_id not in streams and len(streams) >= MAX_STREAMS:
            raise StreamError("stream quota exceeded")
        if len(encoded) > MAX_FRAME_BYTES or target.queued_bytes + len(encoded) > MAX_QUEUED_BYTES:
            raise StreamError("aggregate buffer limit exceeded")
        item = (dict(frame), len(encoded))
        try:
            target.queue.put_nowait(item)
        except asyncio.QueueFull:
            try:
                await asyncio.wait_for(target.queue.put(item), BACKPRESSURE_TIMEOUT)
            except asyncio.TimeoutError as exc:
                raise StreamError("target backpressure") from exc
            if target.closed or self._connections.get(target.identity) is not target:
                raise StreamError("target offline")
        target.queued_bytes += len(encoded)
        if frame_type == "CLOSE":
            streams.discard(stream_id)
            connection.streams.discard(stream_id)
            target.streams.discard(stream_id)
        else:
            streams.add(stream_id)
            connection.streams.add(stream_id)
            target.streams.add(stream_id)

    def health(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "listener": self.data_listener_ready,
            "data_path_ready": self.data_listener_ready and bool(self._connections),
            "active_identities": len(self._connections),
        }


def _require_text_payload(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise RelayError("invalid payload")
    return value


class RelayWebSocketServer:
    """Thin websocket transport; the Relay remains independently testable."""

    def __init__(self, relay: Relay) -> None:
        self.relay = relay

    async def handler(self, websocket, path: str | None = None) -> None:
        request = getattr(websocket, "request", None)
        path = path or getattr(request, "path", None) or getattr(websocket, "path", "")
        if path == "/health":
            await websocket.send(json.dumps(self.relay.health(), separators=(",", ":")))
            return
        if path != "/data":
            await websocket.close(code=1008, reason="unknown route")
            return
        connection = None
        sender = None
        try:
            raw = await asyncio.wait_for(websocket.recv(), HANDSHAKE_TIMEOUT)
            hello = json.loads(raw)
            if not isinstance(hello, dict) or set(hello) != {"type", "identity", "link_id", "credential"} or hello["type"] != "AUTH":
                raise RelayError("invalid handshake")
            connection = await self.relay.connect(identity=hello["identity"], link_id=hello["link_id"], credential=hello["credential"])
            await websocket.send(json.dumps({"type": "READY", "incarnation": connection.incarnation}))
            self.relay.data_listener_ready = True

            async def send_loop() -> None:
                while True:
                    outgoing = await connection.receive()
                    await websocket.send(json.dumps(outgoing, separators=(",", ":")))

            sender = asyncio.create_task(send_loop())
            async for raw in websocket:
                if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_FRAME_BYTES:
                    raise RelayError("frame exceeds limit")
                frame = json.loads(raw)
                try:
                    await asyncio.wait_for(self.relay.route(connection, frame), REQUEST_TIMEOUT)
                except StreamError as exc:
                    stream_id = frame.get("stream_id") if isinstance(frame, dict) else None
                    await websocket.send(json.dumps({"type": "STREAM_ERROR", "stream_id": stream_id, "error": str(exc)}))

        except ConnectionClosed:
            pass  # peer went away; its streams are closed towards the other endpoint below
        except (RelayError, json.JSONDecodeError, asyncio.TimeoutError) as exc:
            await websocket.send(json.dumps({"type": "ERROR", "error": str(exc)}))
            await websocket.close(code=1008, reason="relay rejected request")
        finally:
            if sender is not None:
                sender.cancel()
            if connection is not None:
                await self.relay.disconnect(connection)
            self.relay.data_listener_ready = bool(self.relay._connections)
