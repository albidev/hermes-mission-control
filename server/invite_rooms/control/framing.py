"""Bounded, sequenced frames for one request/response TLS connection.

A ``SequencedTLS`` instance owns exactly one reader and one writer.  It is not
safe to share an instance between concurrent RPCs; the dispatcher creates a
fresh transport for every request.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import socket
import ssl
import struct
import time
from typing import Any

MAX_FRAME_BYTES = 64 * 1024
_HEADER = struct.Struct("!I")


class TLSIdentityError(ssl.SSLError):
    """Generic transport/authentication failure."""


class FrameError(TLSIdentityError):
    """Malformed, oversized, or replayed frame (the public code is generic)."""

    def __init__(self, code: str = "control_ambiguous") -> None:
        super().__init__(code)
        self.code = code


class TLSStreamClosed(FrameError):
    pass


def certificate_fingerprint(cert_der: bytes) -> str:
    if not isinstance(cert_der, bytes) or not cert_der:
        raise FrameError()
    return hashlib.sha256(cert_der).hexdigest()


def hmac_compare(left: str, right: str) -> bool:
    return hmac.compare_digest(left, right)


class SequencedTLS:
    def __init__(self, stream: socket.socket, *, max_frame_bytes: int = MAX_FRAME_BYTES,
                 deadline: float | None = None) -> None:
        if type(max_frame_bytes) is not int or not 1 <= max_frame_bytes <= MAX_FRAME_BYTES:
            raise ValueError("invalid frame limit")
        self.stream = stream
        self.deadline = deadline
        self.max_frame_bytes = max_frame_bytes
        self._next_send = 0
        self._next_receive = 0

    def send(self, payload: bytes) -> int:
        if not isinstance(payload, bytes) or not payload or len(payload) > self.max_frame_bytes:
            raise FrameError("control_busy")
        envelope = json.dumps({"sequence": self._next_send, "payload": payload.hex()},
                              separators=(",", ":"), ensure_ascii=True).encode("ascii")
        if len(envelope) > self.max_frame_bytes:
            raise FrameError("control_busy")
        _set_deadline(self.stream, self.deadline)
        self.stream.sendall(_HEADER.pack(len(envelope)) + envelope)
        sequence = self._next_send
        self._next_send += 1
        return sequence

    def receive(self) -> tuple[int, bytes]:
        header = _read_exact(self.stream, _HEADER.size, self.deadline)
        (length,) = _HEADER.unpack(header)
        if length == 0 or length > self.max_frame_bytes:
            raise FrameError()
        raw = _read_exact(self.stream, length, self.deadline)
        try:
            value: Any = json.loads(raw, object_pairs_hook=_unique,
                                    parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            if not isinstance(value, dict) or set(value) != {"sequence", "payload"}:
                raise ValueError()
            sequence = value["sequence"]
            payload = bytes.fromhex(value["payload"])
        except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
            raise FrameError() from None
        if type(sequence) is not int or sequence != self._next_receive:
            raise FrameError()
        if not payload or len(payload) > self.max_frame_bytes:
            raise FrameError()
        self._next_receive += 1
        return sequence, payload


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


def _set_deadline(stream, deadline):
    if deadline is not None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("control deadline")
        stream.settimeout(remaining)


def _read_exact(stream, size: int, deadline: float | None) -> bytes:
    result = bytearray()
    while len(result) < size:
        _set_deadline(stream, deadline)
        chunk = stream.recv(size - len(result))
        if not chunk:
            raise TLSStreamClosed()
        result.extend(chunk)
    return bytes(result)
