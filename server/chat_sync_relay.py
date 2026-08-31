"""Cross-device chat relay owned by the Mission Control sidecar.

The Hermes gateway currently routes a session's live events to one transport.
This module deliberately stays outside Hermes core: Mission Control clients
mirror the events they receive into this relay, and the relay fans them out to
other Mission Control clients watching the same session.
"""
from __future__ import annotations

import math
import threading
from collections import deque
from queue import Empty, Full, Queue
from typing import Any, Deque, Dict, List, Optional, Tuple

_RELAY_BUFFER_MAX = 2048
_SUBSCRIBER_QUEUE_MAX = 4096


class ChatSyncRelay:
    """Thread-safe per-session event bus with bounded replay."""

    def __init__(self, buffer_max: int = _RELAY_BUFFER_MAX) -> None:
        self._buffer_max = max(32, int(buffer_max))
        self._lock = threading.RLock()
        self._next_seq: Dict[str, int] = {}
        self._buffers: Dict[str, Deque[Dict[str, Any]]] = {}
        self._dedupe: Dict[str, Dict[str, int]] = {}
        self._subscribers: Dict[str, Dict[str, Queue]] = {}

    @staticmethod
    def _valid_session_id(session_id: str) -> str:
        return str(session_id or "").strip()

    @staticmethod
    def _valid_client_id(client_id: str) -> str:
        return str(client_id or "").strip()[:160]

    def subscribe(
        self,
        session_id: str,
        client_id: str,
        since: Optional[int] = None,
    ) -> Tuple[Queue, List[Dict[str, Any]], int]:
        sid = self._valid_session_id(session_id)
        cid = self._valid_client_id(client_id)
        if not sid or not cid:
            raise ValueError("session_id and client_id are required")
        with self._lock:
            latest = self._next_seq.get(sid, 0)
            buffer = self._buffers.get(sid, ())
            replay = [
                dict(item)
                for item in buffer
                if since is not None and item["relay_seq"] > since
            ]
            queue: Queue = Queue(maxsize=_SUBSCRIBER_QUEUE_MAX)
            self._subscribers.setdefault(sid, {})[cid] = queue
            return queue, replay, latest

    def unsubscribe(self, session_id: str, client_id: str) -> None:
        sid = self._valid_session_id(session_id)
        cid = self._valid_client_id(client_id)
        if not sid or not cid:
            return
        with self._lock:
            subscribers = self._subscribers.get(sid)
            if not subscribers:
                return
            subscribers.pop(cid, None)
            if not subscribers:
                self._subscribers.pop(sid, None)

    def publish(
        self,
        session_id: str,
        client_id: str,
        kind: str,
        payload: Dict[str, Any],
        dedupe_key: str,
    ) -> Dict[str, Any]:
        sid = self._valid_session_id(session_id)
        cid = self._valid_client_id(client_id)
        if not sid or not cid:
            raise ValueError("session_id and client_id are required")
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")
        normalized_kind = str(kind or "").strip()[:64]
        if not normalized_kind:
            raise ValueError("kind is required")
        key = str(dedupe_key or "").strip()[:320]
        if not key:
            raise ValueError("dedupe_key is required")

        with self._lock:
            existing_seq = self._dedupe.setdefault(sid, {}).get(key)
            if existing_seq is not None:
                return {
                    "session_id": sid,
                    "relay_seq": existing_seq,
                    "dedupe_key": key,
                    "deduplicated": True,
                }

            relay_seq = self._next_seq.get(sid, 0) + 1
            envelope = {
                "session_id": sid,
                "relay_seq": relay_seq,
                "dedupe_key": key,
                "kind": normalized_kind,
                "payload": dict(payload),
            }
            buffer = self._buffers.setdefault(sid, deque(maxlen=self._buffer_max))
            if len(buffer) == self._buffer_max:
                evicted = buffer[0]
                self._dedupe[sid].pop(str(evicted.get("dedupe_key") or ""), None)
            buffer.append(envelope)
            self._next_seq[sid] = relay_seq
            self._dedupe[sid][key] = relay_seq

            subscribers = list(self._subscribers.get(sid, {}).items())
            for subscriber_id, queue in subscribers:
                if subscriber_id == cid:
                    continue
                try:
                    queue.put_nowait(dict(envelope))
                except Full:
                    # A slow client can reconnect from the last relay_seq. Do
                    # not block the publisher or let one device stall another.
                    self._subscribers.get(sid, {}).pop(subscriber_id, None)
            return envelope

    def wait(self, queue: Queue, timeout: float = 15.0) -> Optional[Dict[str, Any]]:
        try:
            return queue.get(timeout=timeout)
        except Empty:
            return None

    def replay_since(self, session_id: str, since: int) -> List[Dict[str, Any]]:
        sid = self._valid_session_id(session_id)
        try:
            watermark = max(0, int(since))
        except (TypeError, ValueError):
            watermark = 0
        with self._lock:
            return [
                dict(item)
                for item in self._buffers.get(sid, ())
                if item["relay_seq"] > watermark
            ]

    def stats(self) -> Dict[str, int]:
        with self._lock:
            return {
                "sessions": len(self._buffers),
                "events": sum(len(events) for events in self._buffers.values()),
                "subscribers": sum(len(items) for items in self._subscribers.values()),
                "buffer_max": self._buffer_max,
            }

    def reset(self) -> None:
        with self._lock:
            self._next_seq.clear()
            self._buffers.clear()
            self._dedupe.clear()
            self._subscribers.clear()


def core_event_dedupe_key(session_id: str, event: Dict[str, Any], event_id: str = "") -> str:
    """Build a stable key for a mirrored gateway event."""
    seq = event.get("seq") if isinstance(event, dict) else None
    if isinstance(seq, (int, float)) and math.isfinite(float(seq)):
        return f"core:{session_id}:{int(seq)}"
    fallback = str(event_id or "").strip()
    if not fallback:
        fallback = "unknown"
    return f"core:{session_id}:{fallback}"


def user_message_dedupe_key(session_id: str, message_id: str) -> str:
    return f"user:{session_id}:{str(message_id or '').strip()}"


chat_sync_relay = ChatSyncRelay()

__all__ = [
    "ChatSyncRelay",
    "chat_sync_relay",
    "core_event_dedupe_key",
    "user_message_dedupe_key",
]
