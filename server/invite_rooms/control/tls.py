"""Strict TLS 1.3 mutual authentication with a second, exact leaf pin."""
from __future__ import annotations

import hmac
import re
import socket
import ssl

from .framing import TLSIdentityError, certificate_fingerprint

_PIN = re.compile(r"[0-9a-f]{64}\Z")


def _context(*, purpose: ssl.Purpose, ca_file: str | None, ca_data: str | None, cert_file: str,
             key_file: str) -> ssl.SSLContext:
    # Exactly one trust source: a path (spike/tests) or in-memory PEM text (product; never written to disk).
    if (ca_file is None) == (ca_data is None):
        raise ValueError("exactly one of ca_file or ca_data is required")
    if ca_data is not None and (not isinstance(ca_data, str) or not ca_data.strip()):
        raise ValueError("invalid in-memory CA")
    context = ssl.create_default_context(purpose=purpose, cafile=ca_file, cadata=ca_data)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.maximum_version = ssl.TLSVersion.TLSv1_3
    context.check_hostname = False
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_cert_chain(certfile=cert_file, keyfile=key_file)
    return context


def client_context(*, ca_file: str | None = None, ca_data: str | None = None, cert_file: str,
                   key_file: str) -> ssl.SSLContext:
    return _context(purpose=ssl.Purpose.SERVER_AUTH, ca_file=ca_file, ca_data=ca_data,
                    cert_file=cert_file, key_file=key_file)


def server_context(*, ca_file: str | None = None, ca_data: str | None = None, cert_file: str,
                   key_file: str) -> ssl.SSLContext:
    return _context(purpose=ssl.Purpose.CLIENT_AUTH, ca_file=ca_file, ca_data=ca_data,
                    cert_file=cert_file, key_file=key_file)


def _validate_pin(expected_fingerprint: str) -> str:
    if not isinstance(expected_fingerprint, str) or not _PIN.fullmatch(expected_fingerprint.lower()):
        raise TLSIdentityError("control_ambiguous")
    return expected_fingerprint.lower()


def check_peer_fingerprint(stream: ssl.SSLSocket, expected_fingerprint: str) -> None:
    expected = _validate_pin(expected_fingerprint)
    actual_der = stream.getpeercert(binary_form=True)
    if not actual_der or not hmac.compare_digest(certificate_fingerprint(actual_der), expected):
        raise TLSIdentityError("control_ambiguous")


def wrap_client(sock: socket.socket, context: ssl.SSLContext, *, expected_fingerprint: str,
                server_hostname: str | None = None) -> ssl.SSLSocket:
    # Pinning is mandatory even when the owner leaf intentionally has no SAN.
    stream = context.wrap_socket(sock, server_hostname=server_hostname)
    try:
        check_peer_fingerprint(stream, expected_fingerprint)
    except BaseException:
        stream.close()
        raise
    return stream


def wrap_server(sock: socket.socket, context: ssl.SSLContext, *, expected_fingerprint: str) -> ssl.SSLSocket:
    stream = context.wrap_socket(sock, server_side=True)
    try:
        check_peer_fingerprint(stream, expected_fingerprint)
    except BaseException:
        stream.close()
        raise
    return stream
