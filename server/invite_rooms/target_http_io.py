"""Small deadline-aware numeric-loopback HTTP POST engine."""
from __future__ import annotations

import selectors
import socket
import time
from dataclasses import dataclass
from http.client import HTTPResponse
from urllib.parse import urlsplit

MAX_RESPONSE_BYTES = 64 * 1024

@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes

class _SocketFacade:
    def __init__(self, sock, selector, deadline):
        self._sock, self._selector, self._deadline = sock, selector, deadline
        self._buffer = bytearray()
    def _wait(self, event):
        if self._deadline is None:
            timeout = 10.0
        else:
            timeout = min(10.0, self._deadline - time.monotonic())
            if timeout <= 0:
                raise TimeoutError
        ready = self._selector.select(timeout)
        if not ready or (self._deadline is not None and time.monotonic() >= self._deadline):
            raise TimeoutError
    def recv(self, size, *args):
        while True:
            try:
                return self._sock.recv(size, *args)
            except BlockingIOError:
                self._wait(selectors.EVENT_READ)
    def sendall(self, data):
        view = memoryview(data)
        while view:
            if self._deadline is not None and time.monotonic() >= self._deadline:
                raise TimeoutError
            try:
                sent = self._sock.send(view)
            except BlockingIOError:
                self._wait(selectors.EVENT_WRITE)
                continue
            if sent <= 0:
                raise OSError("short write")
            view = view[sent:]
    def makefile(self, mode='rb', buffering=None):
        # HTTPResponse only uses read/readinto on this facade.  It is deliberately
        # not a normal socket.makefile: every read remains deadline-gated.
        return self
    def read(self, size=-1):
        if size < 0:
            chunks = []
            if self._buffer:
                chunks.append(bytes(self._buffer)); self._buffer.clear()
            while True:
                part = self.recv(65536)
                if not part: break
                chunks.append(part)
            return b''.join(chunks)
        if self._buffer:
            data = bytes(self._buffer[:size]); del self._buffer[:len(data)]; return data
        return self.recv(size)
    def readinto(self, buffer):
        data = self.read(len(buffer))
        buffer[:len(data)] = data
        return len(data)
    def readline(self, limit=-1):
        while True:
            marker = self._buffer.find(b'\n')
            if marker >= 0:
                end = marker + 1
                if limit >= 0: end = min(end, limit)
                data = bytes(self._buffer[:end]); del self._buffer[:end]
                return data
            if limit >= 0 and len(self._buffer) >= limit:
                data = bytes(self._buffer[:limit]); del self._buffer[:limit]
                return data
            chunk = self.recv(65536)
            if not chunk:
                data = bytes(self._buffer); self._buffer.clear(); return data
            self._buffer.extend(chunk)
    def isclosed(self):
        return False
    def close(self):
        return None
    def fileno(self):
        return self._sock.fileno()

def _header_ok(name, value):
    if not isinstance(name, str) or not name or any(ord(c) <= 31 or ord(c) == 127 or c in ':()<>@,;\\"/[]?={} \t' for c in name):
        raise ValueError("invalid header")
    if not isinstance(value, str) or any(c in value for c in '\r\n') or any(ord(c) < 32 and c != '\t' for c in value) or any(ord(c) == 127 for c in value):
        raise ValueError("invalid header")

def post_loopback(url, headers, payload, *, deadline=None):
    parsed = urlsplit(url)
    if parsed.scheme != 'http' or parsed.hostname not in {'127.0.0.1', '::1'} or parsed.port is None or parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise ValueError("invalid target")
    for key, value in headers.items(): _header_ok(key, value)
    if not isinstance(payload, bytes): raise TypeError("payload")
    sel = selectors.DefaultSelector(); sock = socket.socket(socket.AF_INET6 if parsed.hostname == '::1' else socket.AF_INET, socket.SOCK_STREAM)
    facade = None
    try:
        sock.setblocking(False); sel.register(sock, selectors.EVENT_WRITE)
        result = sock.connect_ex((parsed.hostname, parsed.port))
        if result not in (0, 36, 10035, 115):
            raise OSError(result, "connect")
        if result:
            if deadline is not None and time.monotonic() >= deadline: raise TimeoutError
            if not sel.select(10.0 if deadline is None else min(10.0, max(0, deadline-time.monotonic()))): raise TimeoutError
            if deadline is not None and time.monotonic() >= deadline: raise TimeoutError
            if sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR): raise OSError("connect")
        facade = _SocketFacade(sock, sel, deadline)
        request = f"POST {parsed.path or '/'} HTTP/1.1\r\n".encode('ascii')
        request += f"Host: {parsed.hostname}:{parsed.port}\r\n".encode('ascii')
        for key, value in headers.items(): request += f"{key}: {value}\r\n".encode('latin-1')
        request += f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n".encode('ascii') + payload
        facade.sendall(request)
        response = HTTPResponse(facade); response.begin()
        body = response.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES: raise ValueError("response too large")
        return HttpResponse(response.status, body)
    finally:
        try:
            if facade is not None: facade.close()
        finally:
            try: sel.unregister(sock)
            except Exception: pass
            sel.close(); sock.close()
