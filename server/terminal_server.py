"""Authenticated raw Bash/Zsh PTY bridge for Mission Control."""
from __future__ import annotations

import asyncio
import atexit
import hmac
import os
import secrets
import signal
import threading
import time
import urllib.parse
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from websockets.asyncio.server import ServerConnection

_TICKET_TTL = 30
_MAX_TICKETS = 32
_MAX_ACTIVE_SESSIONS = 8
_tickets: dict[str, float] = {}
_ticket_lock = threading.Lock()
_session_slots = threading.BoundedSemaphore(_MAX_ACTIVE_SESSIONS)
_active_pids: set[int] = set()
_active_pids_lock = threading.Lock()


def _authorized(value: str) -> bool:
    expected = os.getenv("MISSION_CONTROL_TOKEN", "").strip() or os.getenv("API_SERVER_KEY", "").strip()
    return bool(expected and hmac.compare_digest(value, expected))


def issue_ticket(token: str) -> str | None:
    if not _authorized(token):
        return None
    ticket = secrets.token_urlsafe(32)
    with _ticket_lock:
        now = time.monotonic()
        _tickets.update({k: v for k, v in _tickets.items() if v > now})
        if len(_tickets) >= _MAX_TICKETS:
            return None
        _tickets[ticket] = now + _TICKET_TTL
    return ticket


def _consume_ticket(ticket: str) -> bool:
    with _ticket_lock:
        expires = _tickets.pop(ticket, None)
    return expires is not None and expires > time.monotonic()


def _shell_argv() -> list[str]:
    shell = Path(os.getenv("SHELL", "/bin/bash")).name
    return ["/bin/zsh"] if shell == "zsh" and Path("/bin/zsh").exists() else ["/bin/bash"]


def _child_environment() -> dict[str, str]:
    shell = _shell_argv()[0]
    home = str(Path.home())
    return {
        "HOME": home,
        "LANG": os.getenv("LANG", "C.UTF-8"),
        "LOGNAME": os.getenv("LOGNAME", "cyclone"),
        "PATH": os.getenv("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "SHELL": shell,
        "TERM": "xterm-256color",
        "COLORTERM": "truecolor",
        "USER": os.getenv("USER", "cyclone"),
    }


def _resize(fd: int, cols: int, rows: int) -> None:
    import fcntl
    import struct
    import termios
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


async def _read_pty(ws: Any, fd: int) -> None:
    while True:
        try:
            data = await asyncio.to_thread(os.read, fd, 65536)
        except asyncio.CancelledError:
            return
        except OSError:
            try:
                await ws.close(code=1000, reason="shell exited")
            except Exception:
                pass
            return
        if not data:
            try:
                await ws.close(code=1000, reason="shell exited")
            except Exception:
                pass
            return
        try:
            await ws.send(data)
        except Exception:
            return


async def _write_all(fd: int, data: bytes) -> None:
    offset = 0
    while offset < len(data):
        written = await asyncio.to_thread(os.write, fd, data[offset:])
        if written <= 0:
            raise OSError("PTY write made no progress")
        offset += written


async def _reap_process(pid: int, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            waited, _ = await asyncio.to_thread(os.waitpid, pid, os.WNOHANG)
        except ChildProcessError:
            return
        if waited == pid:
            return
        await asyncio.sleep(0.05)
    return


async def _terminate_process_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGHUP)
    except ProcessLookupError:
        await _reap_process(pid, timeout=0.2)
    else:
        await _reap_process(pid, timeout=1.0)
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    else:
        await _reap_process(pid, timeout=1.0)
    # Always issue a final group kill. The leader may already be reaped while
    # descendants remain alive in the same process group.
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    await _reap_process(pid, timeout=0.5)


def shutdown_terminal_sessions() -> None:
    with _active_pids_lock:
        pids = list(_active_pids)
    for pid in pids:
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        with _active_pids_lock:
            if not _active_pids:
                return
        time.sleep(0.05)
    with _active_pids_lock:
        remaining = list(_active_pids)
    for pid in remaining:
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            pass


async def _handle(ws: Any) -> None:
    origin = ws.request.headers.get("Origin", "")
    if origin:
        origin_host = urllib.parse.urlparse(origin).hostname
        request_host = (ws.request.headers.get("Host", "").split(":", 1)[0]).strip().lower()
        if not origin_host or origin_host.lower() != request_host:
            await ws.close(code=4403, reason="terminal origin rejected")
            return
    params = urllib.parse.parse_qs(urllib.parse.urlsplit(ws.request.path).query)
    ticket = (params.get("ticket") or [""])[0]
    if not _consume_ticket(ticket):
        await ws.close(code=4401, reason="invalid terminal ticket")
        return

    if not await asyncio.to_thread(_session_slots.acquire, False):
        await ws.close(code=4429, reason="terminal session limit reached")
        return

    import pty
    try:
        pid, fd = pty.fork()
    except BaseException:
        _session_slots.release()
        raise
    if pid == 0:
        os.chdir(Path.home())
        argv = _shell_argv()
        os.execvpe(argv[0], argv, _child_environment())

    with _active_pids_lock:
        _active_pids.add(pid)
    reader = asyncio.create_task(_read_pty(ws, fd))
    try:
        async for message in ws:
            if isinstance(message, str) and message.startswith("\x1b[RESIZE:") and message.endswith("]"):
                try:
                    cols, rows = message[9:-1].split(";", 1)
                    parsed_cols, parsed_rows = int(cols), int(rows)
                    if not (1 <= parsed_cols <= 1000 and 1 <= parsed_rows <= 500):
                        continue
                    _resize(fd, parsed_cols, parsed_rows)
                except (ValueError, OSError, OverflowError):
                    pass
                continue
            data = message.encode() if isinstance(message, str) else message
            if data:
                await _write_all(fd, data)
    finally:
        reader.cancel()
        try:
            await reader
        except asyncio.CancelledError:
            pass
        await _terminate_process_group(pid)
        try:
            os.close(fd)
        except OSError:
            pass
        with _active_pids_lock:
            _active_pids.discard(pid)
        _session_slots.release()


def start_terminal_server(host: str | None = None, port: int | None = None) -> threading.Thread:
    from websockets.asyncio.server import serve

    bind_host = host or os.getenv("MISSION_CONTROL_TERMINAL_HOST", "127.0.0.1")
    bind_port = port or int(os.getenv("MISSION_CONTROL_TERMINAL_PORT", "8766"))
    started = threading.Event()
    stop_requested = threading.Event()
    failure: list[BaseException] = []

    def run() -> None:
        async def serve_forever() -> None:
            async with serve(_handle, bind_host, bind_port, max_size=65536, ping_interval=20):
                started.set()
                await asyncio.to_thread(stop_requested.wait)

        try:
            asyncio.run(serve_forever())
        except BaseException as exc:
            failure.append(exc)
            started.set()

    thread = threading.Thread(target=run, name="mc-terminal-ws", daemon=True)
    thread.start()
    if not started.wait(timeout=2):
        raise RuntimeError("Terminal WebSocket server did not become ready")
    if failure:
        raise RuntimeError(f"Terminal WebSocket server failed to start: {failure[0]}") from failure[0]
    thread.stop = stop_requested.set  # type: ignore[attr-defined]
    atexit.register(shutdown_terminal_sessions)
    return thread
