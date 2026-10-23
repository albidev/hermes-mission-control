"""Standard-library TLS 1.3 mutual-authenticated stream for the Rooms spike.

The relay transports the bytes produced here but never receives either peer's
private key. Certificate verification is mandatory; the caller must additionally
pin the peer certificate and bind it to an independently verified native identity.
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
    """Peer certificate or native installation binding is not approved."""


class TLSStreamClosed(TLSIdentityError):
    """Transport EOF, distinct from a failed identity/sequence check."""


def certificate_fingerprint(cert_der: bytes) -> str:
    if not isinstance(cert_der, bytes) or not cert_der:
        raise TLSIdentityError("peer did not present a certificate")
    return hashlib.sha256(cert_der).hexdigest()


def _common_context(*, purpose: ssl.Purpose, ca_file: str, cert_file: str, key_file: str) -> ssl.SSLContext:
    context = ssl.create_default_context(purpose=purpose, cafile=ca_file)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.maximum_version = ssl.TLSVersion.TLSv1_3
    context.check_hostname = False
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_cert_chain(certfile=cert_file, keyfile=key_file)
    return context


def client_context(*, ca_file: str, cert_file: str, key_file: str) -> ssl.SSLContext:
    return _common_context(purpose=ssl.Purpose.SERVER_AUTH, ca_file=ca_file, cert_file=cert_file, key_file=key_file)


def server_context(*, ca_file: str, cert_file: str, key_file: str) -> ssl.SSLContext:
    return _common_context(purpose=ssl.Purpose.CLIENT_AUTH, ca_file=ca_file, cert_file=cert_file, key_file=key_file)


def wrap_client(sock: socket.socket, context: ssl.SSLContext, *, expected_fingerprint: str) -> ssl.SSLSocket:
    stream = context.wrap_socket(sock, server_hostname=None)
    try:
        _check_fingerprint(stream, expected_fingerprint)
    except BaseException:
        stream.close()
        raise
    return stream


def wrap_server(sock: socket.socket, context: ssl.SSLContext, *, expected_fingerprint: str) -> ssl.SSLSocket:
    stream = context.wrap_socket(sock, server_side=True)
    try:
        _check_fingerprint(stream, expected_fingerprint)
    except BaseException:
        stream.close()
        raise
    return stream


def _check_fingerprint(stream: ssl.SSLSocket, expected: str) -> None:
    if not isinstance(expected, str) or len(expected) != 64:
        raise TLSIdentityError("invalid pinned fingerprint")
    peer_certificate = stream.getpeercert(binary_form=True)
    if not peer_certificate:
        raise TLSIdentityError("peer did not present a certificate")
    actual = certificate_fingerprint(peer_certificate)
    if not hmac_compare(actual, expected.lower()):
        raise TLSIdentityError("peer certificate fingerprint is not pinned")


def hmac_compare(left: str, right: str) -> bool:
    """Constant-time comparison without introducing a second crypto protocol."""
    return hmac.compare_digest(left, right)


class SequencedTLS:
    """Length-delimited application frames with monotonic anti-replay sequence."""

    def __init__(self, stream: ssl.SSLSocket, *, max_frame_bytes: int = MAX_FRAME_BYTES,
                 deadline: float | None = None) -> None:
        self.stream = stream
        self.deadline = deadline
        self.max_frame_bytes = max_frame_bytes
        self._next_send = 0
        self._next_receive = 0

    def send(self, payload: bytes) -> int:
        if not isinstance(payload, bytes) or not payload or len(payload) > self.max_frame_bytes:
            raise TLSIdentityError("payload exceeds frame limit")
        sequence = self._next_send
        envelope = json.dumps({"sequence": sequence, "payload": payload.hex()}, separators=(",", ":")).encode()
        if len(envelope) > self.max_frame_bytes:
            raise TLSIdentityError("encoded frame exceeds limit")
        _set_deadline(self.stream, self.deadline)
        self.stream.sendall(_HEADER.pack(len(envelope)) + envelope)
        self._next_send += 1
        return sequence

    def receive(self) -> tuple[int, bytes]:
        header = _read_exact(self.stream, _HEADER.size, self.deadline)
        (length,) = _HEADER.unpack(header)
        if length == 0 or length > self.max_frame_bytes:
            raise TLSIdentityError("invalid frame length")
        raw = _read_exact(self.stream, length, self.deadline)
        try:
            value: Any = json.loads(raw)
            sequence = value["sequence"]
            payload = bytes.fromhex(value["payload"])
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            raise TLSIdentityError("malformed application frame") from exc
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence != self._next_receive:
            raise TLSIdentityError("replayed or out-of-order frame")
        if not payload or len(payload) > self.max_frame_bytes:
            raise TLSIdentityError("invalid application payload")
        self._next_receive += 1
        return sequence, payload


def _set_deadline(stream, deadline):
    if deadline is not None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('TLS request deadline')
        stream.settimeout(remaining)


def _read_exact(stream: ssl.SSLSocket, size: int, deadline: float | None = None) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        _set_deadline(stream, deadline)
        chunk = stream.recv(size - len(chunks))
        if not chunk:
            raise TLSStreamClosed("TLS peer closed during frame")
        chunks.extend(chunk)
    return bytes(chunks)
