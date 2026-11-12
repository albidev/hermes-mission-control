"""W2d Card2-A2b3-2: sidecar data-plane lifecycle (start / rollback / close / join) in the telemetry sidecar.

Real scratch owner config, identities, pairing journals and the actual owner loader/components.  Child processes run the
real ``local_telemetry_server`` under a scratch HOME/TMPDIR on literal-loopback ports chosen by this harness; nothing
here contacts Core, a gateway, a model or any fixed product port.  Target activation (``ScopedAdapterRegistry.register``
callers, per-scope ``TargetConnector``) is NOT exercised: it is a later packet.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import psutil
import queue
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pairing_support as ps  # noqa: E402
from invite_rooms import runtime_config  # noqa: E402
from invite_rooms.control.listener import ListenerError  # noqa: E402
from invite_rooms.peer_registry import MaterialScope, adapter_id_for  # noqa: E402
from invite_rooms import pairing_journal as pj  # noqa: E402

SERVER_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = SERVER_DIR.parent
CORE_CONTRACT_DIR = Path(sys.modules["gateway.hosted_room_peer"].__file__).resolve().parents[1]
LTS_PATH = SERVER_DIR / "local_telemetry_server.py"
CONFIG_ENV = "MISSION_CONTROL_ROOM_INVITES_CONFIG"
TOKEN = "scratch-lifecycle-token"
PREFIX = "/api/local/room-invites"
START_TIMEOUT = 40.0
STOP_BOUND = 20.0
OWNED_THREAD_PREFIXES = ("room-control", "room-loopback")

# Shared by ordinary and fault-injection children; never enter the gateway watcher.
CHILD_BOOTSTRAP = r'''
import os, sys, threading
import local_telemetry_server as lts
os.umask(0o022)
_isolated_watcher_calls = []


def isolated_gateway_watcher():
    _isolated_watcher_calls.append(1)
    print("LIFECYCLE-WATCHER-ISOLATED", flush=True)


lts.start_gateway_watcher = isolated_gateway_watcher


def assert_gateway_isolated():
    assert _isolated_watcher_calls == [1], "main must use the isolated watcher exactly once"
    assert "hermes_state" not in sys.modules, "watcher must not lazily import SessionDB"
    assert not [t.name for t in threading.enumerate() if t.name.startswith("mc-push-")], "observer thread started"
    print("LIFECYCLE-ISOLATION-VERIFIED", flush=True)
'''

ORDINARY_CHILD = CHILD_BOOTSTRAP + r'''
try:
    lts.main()
finally:
    assert_gateway_isolated()
'''

WRAPPER = CHILD_BOOTSTRAP + r'''
import contextlib, json, os, socketserver, sys, threading
import local_telemetry_server as lts
from invite_rooms import sidecar_data_plane as sdp
from invite_rooms import runtime_joining as rj

STATUS, MODE = os.environ["LIFECYCLE_STATUS"], os.environ["LIFECYCLE_MODE"]
INJECT = MODE.startswith("cleanup-")


class Injected(Exception):
    pass


FIRST_CLEANUP = Injected("clear_joining_wiring")
emergency = []


def rec(**kw):
    with open(STATUS, "a") as handle:
        handle.write(json.dumps(kw) + "\n")


def owned():
    return sorted(t.name for t in threading.enumerate() if t.name.startswith(("room-control", "room-loopback")))


def alive_extra():
    return sorted(t.name for t in threading.enumerate() if t is not threading.main_thread())


orig_close = sdp.SidecarDataPlane.close


def close(self):
    rec(event="close_begin", threads=owned())
    try:
        result = orig_close(self)
        if INJECT:
            raise Injected("plane close")
        return result
    finally:
        rec(event="close_end", threads=owned())


sdp.SidecarDataPlane.close = close

# Every ``server.shutdown()`` call is bracketed: a begin without an end is a shutdown waiter that never returned.
orig_shutdown = socketserver.BaseServer.shutdown


def shutdown(self):
    rec(event="shutdown_begin", thread=threading.current_thread().name)
    orig_shutdown(self)
    rec(event="shutdown_end", thread=threading.current_thread().name)


lts.TelemetryHTTPServer.shutdown = shutdown

PRIMARY = []
orig_start = getattr(sdp.SidecarDataPlane, "start", None)
if MODE in ("startup-sigint", "serve-seam", "startup-repeat"):
    handled = threading.Event()
    handled_again = threading.Event()
    signal_calls = []
    orig_sts = lts.shutdown_terminal_sessions

    def sts():
        signal_calls.append(1)
        if len(signal_calls) >= 2:
            handled_again.set()
        handled.set()
        return orig_sts()

    lts.shutdown_terminal_sessions = sts
if orig_start is not None:
    def start(self, *a, **k):
        if INJECT:
            emergency.append(lambda: orig_close(self))
        if MODE in ("startup-sigint", "startup-repeat"):
            print("AT-STARTUP-SEAM", flush=True)       # bundle is built; no listener bound yet; serve_forever not begun
            if not handled.wait(30):
                rec(event="barrier_timeout")
        try:
            return orig_start(self, *a, **k)
        except BaseException as exc:
            PRIMARY.append(exc)
            rec(event="primary_raised", type=type(exc).__name__)
            raise

    sdp.SidecarDataPlane.start = start

shutdown_helpers = []
if MODE == "startup-repeat":
    import signal
    real_signal = signal.signal
    delivered = []

    def install_signal(signum, handler):
        def observed(*args):
            try:
                return handler(*args)
            finally:
                delivered.append(args[0])
                if len(delivered) >= 2:
                    handled_again.set()
        return real_signal(signum, observed)

    signal.signal = install_signal
    real_clear = rj.clear_joining_wiring
    real_threading = threading

    class RepeatProxy:
        def Thread(self, *a, **k):
            thread = real_threading.Thread(*a, **k)
            if getattr(k.get("target"), "__name__", None) == "shutdown":
                shutdown_helpers.append(thread)
            return thread

        def __getattr__(self, name):
            return getattr(real_threading, name)

    lts.threading = RepeatProxy()

    def clear():
        # This release is after the original empty waiter-join loop: the second signal cannot be joined there.
        print("AT-TEARDOWN-SEAM", flush=True)
        if not handled_again.wait(10):
            rec(event="barrier_timeout")
        return real_clear()

    rj.clear_joining_wiring = clear

if MODE == "serve-seam":
    # Late seam: ``main`` creates stop_requested then serving.  Block INSIDE the second main-created Event's set(),
    # i.e. after the serving transition is committed but before the serve loop is entered.
    real_threading, created, fired = threading, [], []

    class SeamEvent(real_threading.Event):
        def set(self):
            super().set()
            if created[1:2] == [self] and not fired:
                fired.append(1)
                print("AT-SERVE-SEAM", flush=True)
                if not handled.wait(30):
                    rec(event="barrier_timeout")

    def make_event():
        event = SeamEvent()
        if sys._getframe(1).f_code.co_name == "main":
            created.append(event)
        return event

    class Proxy:
        Event = staticmethod(make_event)

        def __getattr__(self, name):
            return getattr(real_threading, name)

    lts.threading = Proxy()

if INJECT:
    real_clear = rj.clear_joining_wiring
    emergency.append(real_clear)

    def clear():
        rec(event="cleanup", name="clear_joining_wiring")
        real_clear()
        raise FIRST_CLEANUP

    rj.clear_joining_wiring = clear
    real_server_close = lts.TelemetryHTTPServer.server_close
    real_server_init = lts.TelemetryHTTPServer.__init__

    def server_init(self, *a, **k):
        real_server_init(self, *a, **k)
        emergency.append(lambda: real_server_close(self))

    lts.TelemetryHTTPServer.__init__ = server_init

    def server_close(self):
        rec(event="cleanup", name="server_close")
        real_server_close(self)
        raise Injected("server_close")

    lts.TelemetryHTTPServer.server_close = server_close
    real_sts = lts.shutdown_terminal_sessions
    emergency.append(real_sts)

    def sts():
        if sys._getframe(1).f_code.co_name != "shutdown":      # the outer cleanup, not the signal handler
            rec(event="cleanup", name="shutdown_terminal_sessions")
            real_sts()
            raise Injected("shutdown_terminal_sessions")
        return real_sts()

    lts.shutdown_terminal_sessions = sts
    real_terminal = lts.start_terminal_server

    def terminal():
        thread = real_terminal()
        real_stop, real_join = thread.stop, thread.join
        emergency.extend((real_stop, lambda: real_join(timeout=2)))

        def stop():
            rec(event="cleanup", name="terminal_stop")
            real_stop()
            raise Injected("terminal_stop")

        def join(*a, **k):
            rec(event="cleanup", name="terminal_join")
            return real_join(*a, **k)

        thread.stop, thread.join = stop, join
        return thread

    lts.start_terminal_server = terminal

real_stderr = sys.stderr
if MODE.endswith(("-write", "-flush", "-oserror")):
    class DiagnosticSink:
        armed = False

        def write(self, text):
            if "cleanup failed" in text:
                rec(event="diagnostic_fault", operation=MODE.rsplit("-", 1)[-1])
                if MODE.endswith("-write"):
                    raise BrokenPipeError("diagnostic write failed")
                self.armed = True
            return real_stderr.write(text)

        def flush(self):
            if self.armed:
                self.armed = False
                if MODE.endswith("-oserror"):
                    raise OSError("diagnostic flush failed")
                raise ValueError("diagnostic stream closed")
            return real_stderr.flush()

    sys.stderr = DiagnosticSink()

try:
    lts.main()
except BaseException as exc:
    sys.stderr = real_stderr  # never route probe assertions/tracebacks through the injected diagnostic sink
    rec(event="main_raised", type=type(exc).__name__, message=str(exc)[:80],
        is_primary=bool(PRIMARY) and exc is PRIMARY[0], is_first_cleanup=exc is FIRST_CLEANUP)
    raise
finally:
    sys.stderr = real_stderr
    # Independent emergency owners release real resources even on behavioral RED; these are NOT main's release spies.
    for release in emergency:
        with contextlib.suppress(Exception):
            release()
    assert_gateway_isolated()
sys.stderr = real_stderr
rec(event="main_returned", threads=alive_extra(),
    shutdown_helpers_alive=[t.name for t in shutdown_helpers if t.is_alive()])
'''

_DUMMY = None


def sdp():
    from invite_rooms import sidecar_data_plane
    return sidecar_data_plane


def setUpModule():
    global TEMPLATES
    TEMPLATES = {i: ps.identity_template(i) for i in (ps.INSTALL_A, ps.INSTALL_B)}


def tearDownModule():
    for path in TEMPLATES.values():
        shutil.rmtree(path.parent, ignore_errors=True)


def free_ports(count: int) -> list:
    socks = [socket.socket() for _ in range(count)]
    try:
        for s in socks:
            s.bind(("127.0.0.1", 0))
        return [s.getsockname()[1] for s in socks]
    finally:
        for s in socks:
            s.close()


def ep(port, host="127.0.0.1"):
    return {"host": host, "port": port}


def private_json(path: Path, value) -> None:
    ps.private_dir(path.parent)
    path.write_text(json.dumps(value))
    path.chmod(0o600)


def bindable(port: int, host: str = "127.0.0.1") -> bool:
    probe = socket.socket()
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        probe.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def listening_ports(pid: int) -> set:
    try:
        connections = psutil.Process(pid).net_connections(kind="tcp")
    except psutil.NoSuchProcess:
        return set()
    return {(conn.laddr.ip, conn.laddr.port) for conn in connections
            if conn.status == psutil.CONN_LISTEN and conn.laddr}


def http(port: int, path: str, *, token: str | None = None, timeout: float = 10.0):
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
    if token:
        request.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def failing_thread_start(name: str, marker: BaseException, on_fail=None):
    """A ``Thread.start`` replacement that raises ``marker`` only for the thread called ``name``."""
    real_start = threading.Thread.start

    def start(thread, *args, **kwargs):
        if thread.name == name:
            if on_fail is not None:
                on_fail()
            raise marker
        return real_start(thread, *args, **kwargs)

    return start


ALL_OUTER_CLEANUPS = ("clear_joining_wiring", "server_close", "shutdown_terminal_sessions", "terminal_stop",
                      "terminal_join")


def tls_unpinned_refused(port: int) -> bool:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5) as raw:
            with ctx.wrap_socket(raw) as tls:
                tls.sendall(b"x")
                return tls.recv(1024) == b""
    except (ssl.SSLError, OSError):
        return True


def record_probe(argv, env, cwd, proc, birth, started, output, *, timed_out=False, status=None):
    """Keep inner-probe provenance outside its disposable fixture directory (in this run's explicit TMPDIR)."""
    root = Path(tempfile.gettempdir()) / "probe-records"
    root.mkdir(exist_ok=True)
    name = f"{proc.pid}-{birth:.6f}"
    log = root / (name + ".log")
    log.write_text(output)
    try:
        gone = psutil.Process(proc.pid).create_time() != birth
    except psutil.NoSuchProcess:
        gone = True
    events = status.read_text() if status is not None and status.exists() else None
    record = {"argv": argv, "env": env, "cwd": str(cwd), "pid": proc.pid, "birth": birth,
              "started_at_epoch": started, "ended_at_epoch": time.time(), "exit_code": proc.returncode,
              "timed_out": timed_out, "pid_gone": gone, "log": str(log),
              "log_sha256": hashlib.sha256(log.read_bytes()).hexdigest(), "footer": output.splitlines()[-8:],
              "status_jsonl": events}
    (root / (name + ".json")).write_text(json.dumps(record, indent=2))


class Child:
    """A real sidecar child process whose merged output is pumped by a thread (deterministic line barriers)."""

    def __init__(self, argv, env, cwd):
        self.argv = ["/usr/bin/env", "-i", *[f"{k}={v}" for k, v in env.items()], *argv]
        self.cwd, self.env = str(cwd), env
        self.started, self.timed_out, self.recorded = time.time(), False, False
        self.proc = subprocess.Popen(self.argv, env=env, cwd=self.cwd, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        self.birth = psutil.Process(self.proc.pid).create_time()
        self.lines: list = []
        self._cv = threading.Condition()
        self._pump = threading.Thread(target=self._read, daemon=True)
        self._pump.start()

    @property
    def pid(self) -> int:
        return self.proc.pid

    def _read(self) -> None:
        for line in self.proc.stdout:
            with self._cv:
                self.lines.append(line.rstrip("\n"))
                self._cv.notify_all()

    def output(self) -> str:
        with self._cv:
            return "\n".join(self.lines)

    def wait_line(self, needle: str, timeout: float) -> bool:
        end = time.monotonic() + timeout
        with self._cv:
            while not any(needle in line for line in self.lines):
                if self.proc.poll() is not None and not self._pump.is_alive():
                    return False
                remaining = end - time.monotonic()
                if remaining <= 0:
                    return False
                self._cv.wait(min(remaining, 0.2))
            return True

    def wait_exit(self, timeout: float):
        try:
            code = self.proc.wait(timeout)
        except subprocess.TimeoutExpired:
            self.timed_out = True
            return None
        self._pump.join(5)
        self.proc.stdout.close()
        self._record()
        return code

    def _record(self):
        if not self.recorded:
            status = self.env.get("LIFECYCLE_STATUS")
            record_probe(self.argv, self.env, self.cwd, self.proc, self.birth, self.started, self.output(),
                         timed_out=self.timed_out, status=Path(status) if status else None)
            self.recorded = True

    def alive(self) -> bool:
        return self.proc.poll() is None

    def kill(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(10)
        self._pump.join(5)
        if not self.proc.stdout.closed:
            self.proc.stdout.close()
        self._record()


class Base(unittest.TestCase):
    """Host A paired with joining B (both ``acknowledged``) with a complete scratch owner config for A."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="w2d-a2b3-2-"))
        self.tmp.chmod(0o700)
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = ps.private_dir(self.tmp / "home")
        self.children: list = []
        self.addCleanup(self._reap)
        self.clock = ps.Clock(time.time())
        self.A = ps.Side(self.tmp, "a", ps.INSTALL_A, template=TEMPLATES[ps.INSTALL_A])
        self.B = ps.Side(self.tmp, "b", ps.INSTALL_B, template=TEMPLATES[ps.INSTALL_B])
        self.a, self.b = self.A.service(self.clock), self.B.service(self.clock)
        self.paired = ps.drive_pairing(self.a, self.b)
        req = self.a.ack_request(self.paired["a_id"])
        self.a.complete_ack(self.paired["a_id"], self.b.handle_pair_ack(req.request))
        self.endpoint, self.generation = req.endpoint_id, req.generation
        self.link_a = self.a.journal.snapshot().links[self.endpoint]
        self.peer_b = self.link_a["peer"]["peer_id"]
        self.binding = pj.binding_for(self.link_a)
        self.scope = MaterialScope(
            owner_installation_id=ps.INSTALL_A, owner_profile="default", lobby_id="lobby-1", planned_room_id="room-1",
            member_id="member-1", invite_id="invite-1", peer_id=self.peer_b, pairing_generation=self.generation,
            **self.binding)
        self.adapter_id = adapter_id_for(self.scope)
        runtime_config._warned_once.clear()
        self.addCleanup(runtime_config._warned_once.clear)
        self.planes: list = []
        self.addCleanup(self._close_planes)
        ports = free_ports(7)
        self.ports = dict(zip(("control", "data", "target", "dest", "joining", "telemetry", "terminal"), ports))

    # -- resources --------------------------------------------------------------------------------------
    def _reap(self):
        for child in self.children:
            child.kill()

    def _close_planes(self):
        for plane in self.planes:
            with contextlib.suppress(Exception):
                plane.close()

    # -- config -----------------------------------------------------------------------------------------
    def config_dict(self, *, omit=()):
        side, ports = self.A, self.ports
        token = side.root / "cfg" / "native.token"
        token.write_text("tok")
        token.chmod(0o600)
        ps.private_dir(side.root / "invites")
        data = {
            "store_path": str(side.root / "invites" / "invites.sqlite"),
            "installation_id": side.installation_id,
            "profile": "default",
            "peer_registry_path": str(side.peers_path),
            "native_rpc": {"endpoint": "ws://127.0.0.1:1", "token_path": str(token)},
            "target_http": {"base_url": "http://127.0.0.1:1", "profile": "default"},
            "data_listen": ep(ports["data"]),
            "joining": {"store_dir": str(side.root / "joining"), "identity_dir": str(side.identity_dir),
                        "hosts_path": str(side.root / "cfg" / "hosts.json"),
                        "origins": {self.peer_b: "https://remote.example"},
                        "control_endpoints": {self.peer_b: ep(ports["joining"])}},
            "pairing": {"identity_dir": str(side.identity_dir), "journal_dir": str(side.journal_dir),
                        "peers_path": str(side.peers_path), "control_listen": ep(ports["control"]),
                        "target_listeners": {self.adapter_id: ep(ports["target"])},
                        "data_endpoints": {self.peer_b: {self.adapter_id: ep(ports["dest"])}}},
        }
        for dotted in omit:
            node = data
            *parents, leaf = dotted.split(".")
            for key in parents:
                node = node[key]
            del node[leaf]
        return data

    def write_config(self, data=None, *, omit=()) -> Path:
        path = self.A.root / "cfg" / "runtime.json"
        private_json(path, data if data is not None else self.config_dict(omit=omit))
        return path

    def build_plane(self, **kw):
        path = self.write_config(**kw)
        runtime_config._warned_once.clear()
        loaded = runtime_config.load_service({CONFIG_ENV: str(path)})
        self.assertIsNotNone(loaded, "scratch owner config must be accepted by the canonical loader")
        plane = sdp().build_sidecar_data_plane(loaded)
        self.assertIsNotNone(plane, "complete owner config must assemble a data plane bundle")
        self.planes.append(plane)
        return plane

    # -- child processes --------------------------------------------------------------------------------
    def child_env(self, config_path=None, **extra) -> dict:
        env = {
            "PATH": f"{Path(sys.executable).parent}:/usr/bin:/bin",
            "LANG": "en_US.UTF-8",
            "TERM": "dumb",
            "HOME": str(self.home),
            "TMPDIR": str(self.tmp),
            "PYTHONPATH": f"{SERVER_DIR}:{SERVER_DIR / 'tests'}:{CORE_CONTRACT_DIR}",
            "PYTHONDONTWRITEBYTECODE": "1",
            "MISSION_CONTROL_LOCAL_TELEMETRY_HOST": "127.0.0.1",
            "MISSION_CONTROL_LOCAL_TELEMETRY_PORT": str(self.ports["telemetry"]),
            "MISSION_CONTROL_TERMINAL_PORT": str(self.ports["terminal"]),
            "MISSION_CONTROL_TOKEN": TOKEN,
        }
        if config_path is not None:
            env[CONFIG_ENV] = str(config_path)
        env.update(extra)
        return env

    def spawn(self, config_path=None, *, wrapper_mode=None) -> Child:
        extra = {}
        wrapper = self.tmp / "wrapper.py"
        wrapper.write_text(ORDINARY_CHILD if wrapper_mode is None else WRAPPER)
        argv = [sys.executable, "-B", "-W", "error::ResourceWarning", str(wrapper)]
        if wrapper_mode is not None:
            self.status = self.tmp / "status.jsonl"
            extra = {"LIFECYCLE_STATUS": str(self.status), "LIFECYCLE_MODE": wrapper_mode}
        child = Child(argv, self.child_env(config_path, **extra), REPO_DIR)
        self.children.append(child)
        return child

    def status_events(self) -> list:
        if not getattr(self, "status", None) or not self.status.exists():
            return []
        return [json.loads(line) for line in self.status.read_text().splitlines() if line.strip()]

    def wait_healthy(self, child: Child) -> None:
        self.assertTrue(child.wait_line("listening on http://", START_TIMEOUT),
                        "sidecar never reported listening; output:\n" + child.output())
        end = time.monotonic() + START_TIMEOUT
        while time.monotonic() < end:
            if not child.alive():
                self.fail("sidecar exited during startup:\n" + child.output())
            with contextlib.suppress(OSError):
                if http(self.ports["telemetry"], "/api/local/health")[0] == 200:
                    return
            time.sleep(0.1)
        self.fail("telemetry health never returned 200:\n" + child.output())

    def stop(self, child: Child, sig=signal.SIGTERM):
        os.kill(child.pid, sig)
        code = child.wait_exit(STOP_BOUND)
        self.assertIsNotNone(code, f"child did not exit within {STOP_BOUND}s of {sig.name}:\n" + child.output())
        with self.assertRaises(ProcessLookupError):
            os.kill(child.pid, 0)
        return code

    def assert_only_telemetry_sockets(self, child: Child) -> None:
        allowed = {self.ports["telemetry"], self.ports["terminal"]}
        got = {port for _host, port in listening_ports(child.pid)}
        self.assertEqual(got - allowed, set(), "no data/control/target listener may be bound; got " + repr(got))
        self.assertIn(self.ports["telemetry"], got)
        for name in ("control", "data", "target"):
            self.assertTrue(bindable(self.ports[name]), f"candidate {name} port must stay bindable")

    def assert_ports_rebindable(self, *names) -> None:
        for name in names or ("control", "data", "target"):
            self.assertTrue(bindable(self.ports[name]), f"{name} port must be re-bindable")

    def occupy(self, name: str) -> socket.socket:
        holder = socket.socket()
        holder.bind(("127.0.0.1", self.ports[name]))
        holder.listen(4)
        self.addCleanup(holder.close)
        return holder


# =====================================================================================================================
# P1 / P2 / P4: preservation (feature off, incomplete or invalid owner config): no data/control bind, healthy sidecar
# =====================================================================================================================
class PreservationTest(Base):
    def test_p1_feature_off_serves_telemetry_and_binds_no_data_or_control(self):
        child = self.spawn(None)
        self.wait_healthy(child)
        self.assert_only_telemetry_sockets(child)
        status, body = http(self.ports["telemetry"], PREFIX + "/pairing", token=TOKEN)
        self.assertEqual(status, 404, body)
        self.assertEqual(self.stop(child), 0, child.output())

    def test_p2_incomplete_owner_config_binds_nothing_and_keeps_routes(self):
        path = self.write_config(omit=("data_listen", "pairing.control_listen", "pairing.target_listeners",
                                       "pairing.data_endpoints"))
        child = self.spawn(path)
        self.wait_healthy(child)
        self.assert_only_telemetry_sockets(child)
        status, body = http(self.ports["telemetry"], PREFIX + "/pairing", token=TOKEN)
        self.assertEqual(status, 200, body)           # the valid pairing-only owner config keeps serving its routes
        self.assertEqual(self.stop(child), 0, child.output())

    def test_p4_invalid_config_matrix_fails_closed_with_healthy_telemetry(self):
        control = ep(self.ports["control"])

        def with_data(value):
            def mutate(cfg):
                cfg["data_listen"] = value
            return mutate

        def target_is_control(cfg):
            cfg["pairing"]["target_listeners"] = {self.adapter_id: dict(control)}

        def data_is_target(cfg):
            cfg["data_listen"] = ep(self.ports["target"])

        def dest_is_control(cfg):
            cfg["pairing"]["data_endpoints"] = {self.peer_b: {self.adapter_id: dict(control)}}

        def dest_is_data_listen(cfg):
            cfg["pairing"]["data_endpoints"] = {self.peer_b: {self.adapter_id: ep(self.ports["data"])}}

        variants = {
            "dns_host": with_data(ep(self.ports["data"], "localhost")),
            "wildcard": with_data(ep(self.ports["data"], "0.0.0.0")),
            "other_loopback": with_data(ep(self.ports["data"], "127.0.0.2")),
            "port_zero": with_data(ep(0)),
            "port_bool": with_data(ep(True)),
            "port_out_of_range": with_data(ep(70000)),
            "port_string": with_data(ep(str(self.ports["data"]))),
            "data_equals_control_listen": with_data(dict(control)),
            "data_equals_target_listener": data_is_target,
            "data_endpoint_equals_data_listen": dest_is_data_listen,
            "target_listener_equals_control_listen": target_is_control,
            "data_endpoint_equals_control_listen": dest_is_control,
        }
        for name, mutate in variants.items():
            with self.subTest(variant=name):
                self.tearDown_children()
                cfg = self.config_dict()
                mutate(cfg)
                path = self.write_config(cfg)
                # Whatever the canonical loader decides for the legacy per-request path is what must still happen.
                expected_status = 200 if runtime_config.load_service({CONFIG_ENV: str(path)}) is not None else 404
                runtime_config._warned_once.clear()
                child = self.spawn(path)
                self.wait_healthy(child)
                self.assert_only_telemetry_sockets(child)
                # The legacy per-request loader is what reports a disabled/invalid owner config (once per cause).
                self.assertEqual(http(self.ports["telemetry"], PREFIX + "/pairing", token=TOKEN)[0], expected_status)
                warnings = [line for line in child.lines if "room-invites feature disabled" in line]
                self.assertEqual(len(warnings), 1, child.output())
                for secret in (str(self.tmp), TOKEN, str(self.ports["data"]), str(self.ports["control"])):
                    self.assertNotIn(secret, warnings[0])
                self.assertEqual(self.stop(child), 0, child.output())

    def tearDown_children(self):
        for child in self.children:
            child.kill()
        self.children.clear()


# =====================================================================================================================
# P3 / P5 / P6 / P7 / P8: real child process lifecycle
# =====================================================================================================================
class ProcessLifecycleTest(Base):
    def _diagnostic_failure(self, primary):
        for operation in ("write", "flush", "oserror"):
            with self.subTest(primary=primary, operation=operation):
                if getattr(self, "status", None) is not None:
                    self.status.unlink(missing_ok=True)
                holder = self.occupy("data") if primary else None
                child = self.spawn(self.write_config(), wrapper_mode=
                                   f"cleanup-{'primary' if primary else 'normal'}-{operation}")
                try:
                    if not primary:
                        self.wait_healthy(child)
                        os.kill(child.pid, signal.SIGTERM)
                    code = child.wait_exit(STOP_BOUND)
                    events = self.status_events()
                    self.assertTrue([e for e in events if e["event"] == "diagnostic_fault"],
                                    "diagnostic-only stderr injection must fire: " + child.output())
                    self._assert_cleanup_record(events, child)
                    raised = [e for e in events if e["event"] == "main_raised"]
                    self.assertEqual(len(raised), 1, repr(events))
                    self.assertTrue(raised[0]["is_primary" if primary else "is_first_cleanup"],
                                    "diagnostic IO must not replace the exact primary/first cleanup object: " + repr(events))
                    self.assertIsNotNone(code, "cleanup diagnostic failure hung teardown: " + child.output())
                    self.assertNotEqual(code, 0, child.output())
                    self.assertFalse(psutil.pid_exists(child.pid), "owned child must be reaped")
                finally:
                    child.kill()
                    if holder is not None:
                        holder.close()
                self.assert_ports_rebindable(*self.ports)

    def test_f03_diagnostic_io_failure_preserves_primary_and_attempts_all_releases(self):
        self._diagnostic_failure(primary=True)

    def test_f03_diagnostic_io_failure_surfaces_exact_first_cleanup_after_all_releases(self):
        self._diagnostic_failure(primary=False)

    def test_f03_repeated_startup_signals_cannot_create_shutdown_helpers_during_teardown(self):
        for second in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(second=second.name):
                if getattr(self, "status", None) is not None:
                    self.status.unlink(missing_ok=True)  # each sequential child has independent event evidence
                child = self.spawn(self.write_config(), wrapper_mode="startup-repeat")
                try:
                    self.assertTrue(child.wait_line("AT-STARTUP-SEAM", START_TIMEOUT), child.output())
                    os.kill(child.pid, signal.SIGINT)
                    self.assertTrue(child.wait_line("AT-TEARDOWN-SEAM", STOP_BOUND), child.output())
                    os.kill(child.pid, second)
                    code = child.wait_exit(STOP_BOUND)
                    self.assertIsNotNone(code, "repeated signal prevented bounded teardown: " + child.output())
                    self.assertEqual(code, 0, child.output())
                    events = self.status_events()
                    self.assertNotIn("barrier_timeout", [e["event"] for e in events])
                    returned = next(e for e in events if e["event"] == "main_returned")
                    self.assertEqual(returned["shutdown_helpers_alive"], [],
                                     "shutdown helper waiting on an abandoned loop: " + repr(events))
                    self.assertEqual(sum(e["event"] == "shutdown_begin" for e in events),
                                     sum(e["event"] == "shutdown_end" for e in events), repr(events))
                    self.assertFalse([n for n in returned["threads"] if "shutdown" in n], repr(returned))
                    self.assert_ports_rebindable(*self.ports)
                    self.assertFalse(psutil.pid_exists(child.pid), "owned child must be reaped")
                finally:
                    child.kill()

    def test_p3_full_bundle_binds_exact_loopback_addresses_and_serves(self):
        path = self.write_config()
        child = self.spawn(path)
        self.wait_healthy(child)
        got = listening_ports(child.pid)
        self.assertIn(("127.0.0.1", self.ports["data"]), got, child.output())
        self.assertIn(("127.0.0.1", self.ports["control"]), got, child.output())
        self.assertNotIn(self.ports["target"], {p for _h, p in got}, "no TargetConnector may be started")
        self.assertEqual(http(self.ports["telemetry"], "/api/local/health")[0], 200)
        status, body = http(self.ports["data"], "/links/link-" + "0" * 32 + "/ping")
        self.assertEqual(status, 404, body)
        self.assertNotIn(b"link-", body)
        self.assertTrue(tls_unpinned_refused(self.ports["control"]), "an unpinned mTLS client must be refused")
        self.assertEqual(http(self.ports["telemetry"], PREFIX + "/pairing", token=TOKEN)[0], 200)
        self.assertEqual(self.stop(child), 0, child.output())

    def test_p5_data_bind_failure_propagates_oserror_and_releases_everything(self):
        path = self.write_config()
        self.occupy("data")
        child = self.spawn(path)
        code = child.wait_exit(START_TIMEOUT)
        self.assertIsNotNone(code, "sidecar must terminate when the data port is occupied:\n" + child.output())
        self.assertNotEqual(code, 0, child.output())
        self.assertIn("OSError", child.output())
        self.assertIn("Address already in use", child.output())
        self.assertNotIn("DataPlaneError", child.output())
        self.assert_ports_rebindable("control", "target")
        self.assertTrue(bindable(self.ports["telemetry"]), "telemetry socket must be released")
        self.assertEqual(listening_ports(child.pid) if child.alive() else set(), set())

    def test_p6_control_bind_failure_after_data_start_rolls_data_back(self):
        path = self.write_config()
        holder = self.occupy("control")
        child = self.spawn(path)
        code = child.wait_exit(START_TIMEOUT)
        self.assertIsNotNone(code, "sidecar must terminate when the control port is occupied:\n" + child.output())
        self.assertNotEqual(code, 0, child.output())
        self.assertIn("OSError", child.output())
        self.assertIn("Address already in use", child.output())
        self.assertTrue(bindable(self.ports["data"]), "data listener must be rolled back:\n" + child.output())
        self.assertTrue(bindable(self.ports["telemetry"]), "telemetry socket must be released")
        holder.close()
        self.assert_ports_rebindable("control", "data", "target")

    def test_p7_sigterm_while_serving_closes_and_joins_owned_listeners(self):
        path = self.write_config()
        child = self.spawn(path, wrapper_mode="serve")
        self.wait_healthy(child)
        got = {p for _h, p in listening_ports(child.pid)}
        self.assertTrue({self.ports["data"], self.ports["control"]} <= got, child.output())
        started = time.monotonic()
        code = self.stop(child, signal.SIGTERM)
        self.assertLess(time.monotonic() - started, STOP_BOUND)
        self.assertEqual(code, 0, child.output())
        events = {e["event"]: e for e in self.status_events()}
        self.assertTrue(events.get("close_begin", {}).get("threads"), "owned listener threads were alive before close")
        self.assertEqual(events["close_end"]["threads"], [], "owned threads must be joined before the process ends")
        self.assertIn("main_returned", events)
        self.assert_ports_rebindable("control", "data", "target")
        self.assertTrue(bindable(self.ports["telemetry"]))

    def test_p8_sigint_between_bundle_build_and_serve_forever_takes_the_bounded_cleanup_path(self):
        path = self.write_config()
        child = self.spawn(path, wrapper_mode="startup-sigint")
        self.assertTrue(child.wait_line("AT-STARTUP-SEAM", START_TIMEOUT),
                        "the sidecar never reached SidecarDataPlane.start() after building the bundle:\n"
                        + child.output())
        os.kill(child.pid, signal.SIGINT)
        started = time.monotonic()
        code = child.wait_exit(STOP_BOUND)
        self.assertIsNotNone(code, "SIGINT during startup deadlocked or hung:\n" + child.output())
        self.assertLess(time.monotonic() - started, STOP_BOUND)
        self.assertEqual(code, 0, child.output())
        with self.assertRaises(ProcessLookupError):
            os.kill(child.pid, 0)
        events = {e["event"]: e for e in self.status_events()}
        self.assertNotIn("barrier_timeout", events)
        self.assertTrue(events.get("close_begin", {}).get("threads"), "listeners had been started before cleanup")
        self.assertEqual(events["close_end"]["threads"], [])
        self.assert_ports_rebindable("control", "data", "target")
        self.assertTrue(bindable(self.ports["telemetry"]))

    def test_sigint_between_serve_event_and_actual_serve_forever_joins_shutdown_waiter(self):
        path = self.write_config()
        child = self.spawn(path, wrapper_mode="serve-seam")
        try:
            self.assertTrue(child.wait_line("AT-SERVE-SEAM", START_TIMEOUT),
                            "the sidecar never reached the post-serving/pre-serve-loop seam:\n" + child.output())
            os.kill(child.pid, signal.SIGINT)
            started = time.monotonic()
            code = child.wait_exit(STOP_BOUND)
            self.assertIsNotNone(code, "SIGINT at the late seam deadlocked or hung:\n" + child.output())
            self.assertLess(time.monotonic() - started, STOP_BOUND)
            self.assertEqual(code, 0, child.output())
            with self.assertRaises(ProcessLookupError):
                os.kill(child.pid, 0)
            events = self.status_events()
            names = [e["event"] for e in events]
            self.assertNotIn("barrier_timeout", names)
            self.assertIn("main_returned", names, child.output())
            self.assertEqual(names.count("shutdown_begin"), names.count("shutdown_end"),
                             "a server.shutdown() waiter never returned: " + repr(events))
            returned = next(e for e in events if e["event"] == "main_returned")
            self.assertEqual([n for n in returned["threads"] if "(shutdown)" in n], [],
                             "the shutdown helper must be joined before main returns")
            close_begin = next(e for e in events if e["event"] == "close_begin")
            close_end = next(e for e in events if e["event"] == "close_end")
            self.assertTrue(close_begin["threads"], "listeners had been started before cleanup")
            self.assertEqual(close_end["threads"], [])
            self.assert_ports_rebindable("control", "data", "target")
            self.assertTrue(bindable(self.ports["telemetry"]))
        finally:
            child.kill()
        self.assertFalse(child.alive())

    def _assert_cleanup_record(self, events, child):
        names = [e["event"] for e in events]
        attempted = [e["name"] for e in events if e["event"] == "cleanup"]
        self.assertEqual(sorted(set(attempted)), sorted(ALL_OUTER_CLEANUPS),
                         "every outer cleanup must be attempted despite earlier cleanup failures; attempted="
                         + repr(attempted) + "\n" + child.output())
        self.assertIn("close_begin", names, "the data-plane close must be attempted")
        close_end = next(e for e in events if e["event"] == "close_end")
        self.assertEqual(close_end["threads"], [], "no owned listener thread may remain")

    def test_main_attempts_all_outer_cleanup_and_preserves_primary_error(self):
        path = self.write_config()
        holder = self.occupy("data")                     # the intentional primary failure: the data bind is refused
        child = self.spawn(path, wrapper_mode="cleanup-primary")
        try:
            code = child.wait_exit(STOP_BOUND)
            events = self.status_events()
            names = [e["event"] for e in events]
            self.assertIn("primary_raised", names, "the intended primary bind failure was not reached:\n" + child.output())
            primary = next(e for e in events if e["event"] == "primary_raised")
            self.assertEqual(primary["type"], "OSError")
            self.assertIn("Address already in use", child.output())
            if "cleanup" in names:
                self.assertLess(names.index("primary_raised"), names.index("cleanup"))
            self._assert_cleanup_record(events, child)
            raised = [e for e in events if e["event"] == "main_raised"]
            self.assertEqual([(e["type"], e["is_primary"]) for e in raised], [("OSError", True)],
                             "the ORIGINAL OSError object must propagate out of main()")
            self.assertIsNotNone(code, "sidecar cleanup hung after the primary failure:\n" + child.output())
            self.assertNotEqual(code, 0)
            with self.assertRaises(ProcessLookupError):
                os.kill(child.pid, 0)
            self.assertTrue(bindable(self.ports["telemetry"]))
            self.assert_ports_rebindable("control", "target")
        finally:
            child.kill()
            holder.close()
        self.assertFalse(child.alive())

    def test_main_surfaces_cleanup_failure_only_after_all_attempts_on_normal_shutdown(self):
        path = self.write_config()
        child = self.spawn(path, wrapper_mode="cleanup-normal")
        try:
            self.wait_healthy(child)
            got = {p for _h, p in listening_ports(child.pid)}
            self.assertTrue({self.ports["data"], self.ports["control"]} <= got, "bundle must be serving: " + repr(got))
            os.kill(child.pid, signal.SIGTERM)
            code = child.wait_exit(STOP_BOUND)
            events = self.status_events()
            names = [e["event"] for e in events]
            self._assert_cleanup_record(events, child)
            self.assertIn("main_raised", names, "a cleanup failure must be surfaced, not reported as success")
            self.assertNotIn("main_returned", names)
            self.assertIsNotNone(code, "sidecar did not exit after SIGTERM with failing cleanups:\n" + child.output())
            self.assertNotEqual(code, 0, child.output())
            with self.assertRaises(ProcessLookupError):
                os.kill(child.pid, 0)
            self.assert_ports_rebindable("control", "data", "target")
            self.assertTrue(bindable(self.ports["telemetry"]))
        finally:
            child.kill()
        self.assertFalse(child.alive())


# =====================================================================================================================
# Component-level lifecycle (real bundle components; only close/start seams are spied or failed)
# =====================================================================================================================
class BundleLifecycleTest(Base):
    def test_card3_close_during_bundle_start_retains_stores_and_reaps_late_bind(self):
        plane = self.build_plane()
        entered, release = threading.Event(), threading.Event()
        errors = []
        real_start = plane.adapter.start

        def paused_start(**kwargs):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("barrier timeout")
            real_start(**kwargs)

        def start():
            try:
                plane.start()
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=start)
        try:
            with mock.patch.object(plane.adapter, "start", side_effect=paused_start):
                thread.start()
                self.assertTrue(entered.wait(2))
                with self.assertRaisesRegex(Exception, "incomplete"):
                    plane.close()
                self.assertIsNotNone(plane.admission_store._fd)
                release.set()
                thread.join(5)
                self.assertFalse(thread.is_alive())
            self.assertEqual(len(errors), 1)
            plane.close()
            self.assert_ports_rebindable("control", "data", "target")
        finally:
            release.set()
            thread.join(5)
            # Independent emergency release makes the failing baseline witness leak-free.
            plane.adapter.close()
            plane.control_listener.close()
            plane.control_listener.join(timeout=5)
            plane.close()

    def test_card3_manager_failure_still_attempts_other_releases_and_retains_store_owner(self):
        plane = self.build_plane()
        self.assertTrue(hasattr(plane, "scope_lifecycle"), "bundle must own target lifecycle")
        manager = plane.scope_lifecycle
        failure = RuntimeError("target-cleanup-incomplete")
        calls = []
        with mock.patch.object(manager, "close", side_effect=failure), \
                mock.patch.object(manager, "snapshot", return_value=(("owned", "retiring"),)), \
                mock.patch.object(plane.scoped_registry, "close", side_effect=lambda: calls.append("registry")), \
                mock.patch.object(plane.adapter, "close", side_effect=lambda: calls.append("adapter")), \
                mock.patch.object(plane.control_listener, "close", side_effect=lambda: calls.append("control")):
            with self.assertRaises(RuntimeError) as caught:
                plane.close()
        self.assertIs(caught.exception, failure)
        self.assertEqual(calls, ["registry", "adapter", "control"])
        self.assertIsNotNone(plane.admission_store._fd, "live target cleanup must retain stores for retry")
        plane.close()
        self.assertIsNone(plane.admission_store._fd)

    def test_start_binds_exact_configured_addresses_attaches_registry_then_starts_control(self):
        plane = self.build_plane()
        order = []
        real_control_start = plane.control_listener.start

        def spy_control_start():
            order.append("control_start")
            real_control_start()

        with mock.patch.object(plane.control_listener, "start", side_effect=spy_control_start):
            plane.start(attach=lambda registry: order.append(("attach", registry, not bindable(self.ports["data"]))))
        self.assertEqual(order[0][0], "attach")
        self.assertIs(order[0][1], plane.scoped_registry)
        self.assertTrue(order[0][2], "the data adapter must already be bound when the registry is attached")
        self.assertEqual(order[1:], ["control_start"])
        self.assertFalse(bindable(self.ports["data"]))
        self.assertFalse(bindable(self.ports["control"]))
        self.assertTrue(bindable(self.ports["target"]))
        self.assertEqual(plane.adapter._server.server_address[:2], ("127.0.0.1", self.ports["data"]))
        with self.assertRaises(Exception):
            plane.start()                              # a bundle is single-use
        plane.close()
        self.assert_ports_rebindable("control", "data")

    def test_control_bind_failure_unwinds_data_registry_and_bundle_stores_only(self):
        plane = self.build_plane()
        holder = self.occupy("control")
        calls = []
        for name, owner in (("registry", plane.scoped_registry), ("adapter", plane.adapter),
                            ("joining_store", plane.joining_store), ("admission_store", plane.admission_store)):
            real = owner.close
            patcher = mock.patch.object(owner, "close", side_effect=lambda r=real, n=name: (calls.append(n), r())[1])
            patcher.start()
            self.addCleanup(patcher.stop)
        with self.assertRaises(OSError) as raised:
            plane.start()
        self.assertEqual(type(raised.exception), OSError)
        self.assertIn("registry", calls)
        self.assertIn("adapter", calls)
        self.assertLess(calls.index("registry"), calls.index("adapter"))
        self.assertIn("joining_store", calls)
        self.assertIn("admission_store", calls)
        self.assertTrue(bindable(self.ports["data"]))
        self.assertFalse([t for t in threading.enumerate() if t.name.startswith(OWNED_THREAD_PREFIXES)])
        holder.close()
        self.assertTrue(bindable(self.ports["control"]))

    def test_rollback_failure_never_masks_the_original_startup_exception(self):
        plane = self.build_plane()
        self.occupy("control")
        marker = ListenerError("rollback close failed")
        with mock.patch.object(plane.adapter, "close", side_effect=marker) as adapter_close, \
                mock.patch.object(plane.scoped_registry, "close", side_effect=marker) as registry_close:
            with self.assertRaises(OSError) as raised:
                plane.start()
        self.assertEqual(type(raised.exception), OSError)
        adapter_close.assert_called()
        registry_close.assert_called()

    def test_attach_failure_after_data_start_is_unwound_and_reraised(self):
        plane = self.build_plane()

        def boom(_registry):
            raise RuntimeError("attach failed")

        with self.assertRaises(RuntimeError):
            plane.start(attach=boom)
        self.assertTrue(bindable(self.ports["data"]))
        self.assertTrue(bindable(self.ports["control"]))

    def test_p9_close_failure_is_surfaced_after_best_effort_cleanup(self):
        plane = self.build_plane()
        plane.start()
        order = []
        real_registry, real_adapter = plane.scoped_registry.close, plane.adapter.close
        real_control_close, real_join = plane.control_listener.close, plane.control_listener.join

        def failing_control_close():
            order.append("control_close")
            raise ListenerError("listener teardown incomplete")

        with mock.patch.object(plane.scoped_registry, "close", side_effect=lambda: (order.append("registry"), real_registry())), \
                mock.patch.object(plane.adapter, "close", side_effect=lambda: (order.append("adapter"), real_adapter())), \
                mock.patch.object(plane.control_listener, "close", side_effect=failing_control_close), \
                mock.patch.object(plane.joining_store, "close", wraps=plane.joining_store.close) as joining_close, \
                mock.patch.object(plane.admission_store, "close", wraps=plane.admission_store.close) as admission_close:
            with self.assertRaises(ListenerError) as raised:
                plane.close()
        self.assertEqual(str(raised.exception), "listener teardown incomplete")
        self.assertEqual(order, ["registry", "adapter", "control_close"])
        joining_close.assert_called()
        admission_close.assert_called()
        self.assertTrue(bindable(self.ports["data"]), "the data adapter must have been closed despite the failure")
        # The failure is not swallowed into a success state: a retry re-attempts the teardown, then it is idempotent.
        real_control_close()
        real_join()
        plane.close()
        plane.close()
        self.assert_ports_rebindable("control", "data")

    def test_close_runs_registry_then_adapter_then_control_close_and_join(self):
        plane = self.build_plane()
        plane.start()
        order = []
        for name, owner, method in (("registry", plane.scoped_registry, "close"), ("adapter", plane.adapter, "close"),
                                    ("control_close", plane.control_listener, "close"),
                                    ("control_join", plane.control_listener, "join")):
            real = getattr(owner, method)
            patcher = mock.patch.object(owner, method, side_effect=lambda *a, r=real, n=name, **k: (order.append(n), r(*a, **k))[1])
            patcher.start()
            self.addCleanup(patcher.stop)
        plane.close()
        self.assertEqual(order, ["registry", "adapter", "control_close", "control_join"])
        self.assertFalse([t for t in threading.enumerate() if t.name.startswith(OWNED_THREAD_PREFIXES)])
        plane.close()
        self.assertEqual(order, ["registry", "adapter", "control_close", "control_join"], "close is idempotent")

    def test_sidecar_bundle_rolls_back_after_adapter_thread_start_failure(self):
        plane = self.build_plane()
        marker = RuntimeError("adapter serve thread start failed")
        bound_when_failed, calls = [], []
        try:
            with contextlib.ExitStack() as stack:
                for name, owner in (("registry", plane.scoped_registry), ("adapter", plane.adapter),
                                    ("joining_store", plane.joining_store), ("admission_store", plane.admission_store)):
                    real = owner.close
                    stack.enter_context(mock.patch.object(
                        owner, "close", side_effect=lambda r=real, n=name: (calls.append(n), r())[1]))
                stack.enter_context(mock.patch.object(
                    threading.Thread, "start",
                    failing_thread_start("room-loopback-adapter", marker,
                                         lambda: bound_when_failed.append(not bindable(self.ports["data"])))))
                with self.assertRaises(RuntimeError) as raised:
                    plane.start()
            self.assertIs(raised.exception, marker)
            self.assertEqual(bound_when_failed, [True], "the real data listener must have been bound at the failure")
            for name in ("registry", "adapter", "joining_store", "admission_store"):
                self.assertIn(name, calls, f"{name} cleanup must be attempted after a partial adapter start")
            self.assertTrue(bindable(self.ports["data"]), "the partially started data listener must be released")
            self.assert_ports_rebindable("control", "target")
            self.assertFalse([t for t in threading.enumerate() if t.name.startswith(OWNED_THREAD_PREFIXES)])
            plane.close()                                 # a fully rolled-back bundle closes idempotently
        finally:
            leaked = plane.adapter._server
            if leaked is not None and not (plane.adapter._thread is not None and plane.adapter._thread.is_alive()):
                leaked.server_close()                     # never shutdown(): the serve loop never ran
                plane.adapter._server = None

    def test_sidecar_bundle_retries_control_cleanup_after_start_failure(self):
        plane = self.build_plane()
        marker = RuntimeError("control accept thread start failed")
        listener = plane.control_listener
        real_close, real_join = listener.close, listener.join
        closes, joins = [], []

        def flaky_close():
            closes.append(1)
            if len(closes) == 1:
                raise ListenerError("listener self-clean failed once")   # the listener's OWN start() self-clean
            return real_close()

        def spy_join(*args, **kwargs):
            joins.append(1)
            return real_join(*args, **kwargs)

        try:
            with mock.patch.object(listener, "close", side_effect=flaky_close), \
                    mock.patch.object(listener, "join", side_effect=spy_join), \
                    mock.patch.object(threading.Thread, "start",
                                      failing_thread_start("room-control-accept", marker)):
                with self.assertRaises(RuntimeError) as raised:
                    plane.start()
            self.assertIs(raised.exception, marker)
            self.assertGreaterEqual(len(closes), 2, "the sidecar must retry the incomplete control close")
            self.assertTrue(joins, "the sidecar must retry the control join")
            self.assertFalse([t for t in threading.enumerate() if t.name.startswith(OWNED_THREAD_PREFIXES)])
            self.assert_ports_rebindable("control", "data", "target")
        finally:
            with contextlib.suppress(Exception):          # never leave the started loop thread behind
                real_close()
                real_join(timeout=5)


# =====================================================================================================================
# P10: Handler propagation of the server-owned registry (unit level, no socket)
# =====================================================================================================================
class HandlerPropagationTest(unittest.TestCase):
    def handler(self, path, **server_attrs):
        import local_telemetry_server as lts
        handler = object.__new__(lts.Handler)
        handler.path = path
        handler.server = SimpleNamespace(**server_attrs)
        return lts, handler

    def dispatch(self, method, path, **server_attrs):
        lts, handler = self.handler(path, **server_attrs)
        with mock.patch.object(lts.room_invite_routes, "handle_request", return_value=True) as routes_call, \
                mock.patch.object(lts.Handler, "_reject_mutation_in_read_only_mode", return_value=False):
            getattr(handler, "do_" + method)()
        return lts, handler, routes_call

    def test_p10_get_post_delete_pass_the_exact_server_owned_registry(self):
        sentinel = object()
        for method in ("GET", "POST", "DELETE"):
            with self.subTest(method=method):
                path = PREFIX + "/pairing"
                lts, handler, routes_call = self.dispatch(method, path, scoped_adapter_registry=sentinel)
                routes_call.assert_called_once()
                args, kwargs = routes_call.call_args
                self.assertEqual(args[0], handler)
                self.assertEqual(args[1], method)
                self.assertEqual(args[2], path)
                self.assertIs(args[4], lts._is_authorized)
                self.assertEqual(set(kwargs), {"scoped_adapter_registry"})
                self.assertIs(kwargs["scoped_adapter_registry"], sentinel)

    def test_p10_without_a_bundle_the_legacy_call_signature_is_preserved(self):
        for method in ("GET", "POST", "DELETE"):
            for attrs in ({}, {"scoped_adapter_registry": None}):
                with self.subTest(method=method, attrs=attrs):
                    lts, handler, routes_call = self.dispatch(method, PREFIX + "/pairing", **attrs)
                    routes_call.assert_called_once_with(handler, method, PREFIX + "/pairing", mock.ANY,
                                                        lts._is_authorized)

    def test_p10_read_only_gating_still_precedes_the_invite_dispatch(self):
        for method in ("POST", "DELETE"):
            with self.subTest(method=method):
                lts, handler = self.handler(PREFIX + "/pairing", scoped_adapter_registry=object())
                with mock.patch.object(lts.room_invite_routes, "handle_request") as routes_call, \
                        mock.patch.object(lts.Handler, "_reject_mutation_in_read_only_mode", return_value=True):
                    getattr(handler, "do_" + method)()
                routes_call.assert_not_called()

    def test_p10_server_attribute_is_per_instance_not_a_module_global(self):
        import local_telemetry_server as lts
        self.assertFalse(hasattr(lts, "_SCOPED_ADAPTER_REGISTRY"))
        self.assertFalse(hasattr(lts.TelemetryHTTPServer, "scoped_adapter_registry"))


class AdapterStartTransactionTest(unittest.TestCase):
    """B1: ``LoopbackAdapter.start()`` is transactional when the serve thread cannot be started."""

    def test_f03_started_thread_failure_preserves_primary_and_terminates_thread(self):
        from invite_rooms.control.adapter import LoopbackAdapter
        adapter, threads = LoopbackAdapter(), []
        marker = RuntimeError("start raised after real bootstrap")
        real_start = threading.Thread.start

        def start(thread):
            real_start(thread)
            if thread.name == "room-loopback-adapter":
                threads.append(thread)
                raise marker

        port = free_ports(1)[0]
        try:
            with mock.patch.object(threading.Thread, "start", start):
                with self.assertRaises(RuntimeError) as raised:
                    adapter.start(port=port)
            self.assertIs(raised.exception, marker)
            self.assertTrue(threads)
            self.assertFalse(any(t.is_alive() for t in threads))
            self.assertTrue(bindable(port))
        finally:
            adapter.close()

    def test_f03_join_timeout_retains_live_thread_until_owner_retry(self):
        from invite_rooms.control import adapter as mod
        release, tail = threading.Event(), threading.Event()
        real_server = mod.ThreadingHTTPServer

        class Server(real_server):
            def serve_forever(self, poll_interval=0.5):
                super().serve_forever(poll_interval=0.01)
                tail.set()
                release.wait(10)  # a real, live thread even after the serve loop has stopped

        adapter, thread = mod.LoopbackAdapter(), None
        port = free_ports(1)[0]
        try:
            with mock.patch.object(mod, "ThreadingHTTPServer", Server):
                adapter.start(port=port)
            thread = adapter._thread
            assert thread is not None
            with self.assertRaisesRegex(RuntimeError, "did not terminate"):
                adapter.close()
            self.assertTrue(tail.is_set())
            self.assertIs(adapter._thread, thread)
            self.assertTrue(thread.is_alive(), "join timeout must not count as termination")
            self.assertIsNotNone(adapter._server, "incomplete cleanup must stay owner-reachable")
        finally:
            release.set()
            if thread is not None:
                thread.join(5)
            adapter.close()
        assert thread is not None
        self.assertFalse(thread.is_alive())
        self.assertIsNone(adapter._thread)
        self.assertIsNone(adapter._server)
        self.assertTrue(bindable(port))

    def _transaction_probe(self, mode):
        code = r'''
import contextlib, sys, threading
from unittest import mock
from invite_rooms.control import adapter as mod
from test_sidecar_data_plane_lifecycle_w2d import bindable, failing_thread_start

mode = sys.argv[1]
servers, forbidden, retained = [], [], []
real_server, real_thread = mod.ThreadingHTTPServer, threading.Thread
marker = RuntimeError("owned startup marker")
close_error = OSError("close failed once")

class Server(real_server):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        servers.append(self)  # emergency ownership, independent of adapter references/tracebacks
        self.close_attempts = 0
    def shutdown(self):
        forbidden.append("shutdown")
        raise AssertionError("never-served server must not be shut down")
    def server_close(self):
        self.close_attempts += 1
        if mode == "retry" and self.close_attempts == 1:
            raise close_error
        return super().server_close()

class Thread(real_thread):
    def __init__(self, *a, **k):
        if k.get("name") == "room-loopback-adapter":
            assert servers and not bindable(servers[0].server_address[1]), "failure must follow real bind"
            raise marker
        super().__init__(*a, **k)

adapter = mod.LoopbackAdapter()
try:
    with mock.patch.object(mod, "ThreadingHTTPServer", Server):
        patch = (mock.patch.object(threading, "Thread", Thread) if mode == "constructor" else
                 mock.patch.object(threading.Thread, "start", failing_thread_start("room-loopback-adapter", marker)))
        with patch, mock.patch.object(real_thread, "join", side_effect=AssertionError("unstarted thread joined")):
            try:
                adapter.start()
            except RuntimeError as exc:
                retained.extend((exc, exc.__traceback__))
            else:
                raise AssertionError("startup injection did not fire")
        assert retained[0] is marker and retained[1] is not None, "exact primary and traceback required"
        server = servers[0]
        port = server.server_address[1]
        if mode == "retry":
            assert adapter._server is server, "incomplete cleanup must retain socket ownership"
            assert not bindable(port), "one-shot close failure must leave a real bound socket"
            adapter.close()
            assert server.close_attempts >= 2, "owner cleanup must retry release"
        assert bindable(port), "bound socket leaked while primary exception and traceback are retained"
        assert not forbidden, "no shutdown on a never-served socket"
        adapter.close()
        assert not [t for t in threading.enumerate() if t.name == "room-loopback-adapter"]
    # Positive control uses the same object and port, with real thread construction/start/close.
    adapter.start(port=port)
    assert adapter.is_running() and not bindable(port)
    adapter.close()
    assert bindable(port) and not adapter.is_running()
    print("adapter transaction assertions passed", flush=True)
finally:
    # RED must also release the real server handle retained only by the traceback.
    for server in servers:
        real_server.server_close(server)  # never call shutdown on these never-served sockets
    if adapter.is_running():
        adapter.close()
'''
        with tempfile.TemporaryDirectory(prefix="f03-adapter-") as root:
            env = {"PATH": "/usr/bin:/bin", "HOME": root, "TMPDIR": root,
                   "PYTHONPATH": f"{SERVER_DIR}:{SERVER_DIR / 'tests'}:{CORE_CONTRACT_DIR}", "PYTHONDONTWRITEBYTECODE": "1"}
            argv = ["/usr/bin/env", "-i", *[f"{k}={v}" for k, v in env.items()],
                    sys.executable, "-B", "-W", "error::ResourceWarning", "-c", code, mode]
            started, timed_out = time.time(), False
            child = subprocess.Popen(argv, cwd=REPO_DIR, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            birth = psutil.Process(child.pid).create_time()
            try:
                try:
                    output, _ = child.communicate(timeout=15)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    self.fail("adapter transaction probe failed to finish bounded cleanup")
                self.assertEqual(child.returncode, 0, output)
            finally:
                if child.poll() is None:
                    child.kill()
                final_output, _ = child.communicate(timeout=5)
                record_probe(argv, env, REPO_DIR, child, birth, started, final_output, timed_out=timed_out)
            self.assertFalse(psutil.pid_exists(child.pid), "owned probe must be reaped")

    def test_f03_constructor_failure_releases_socket_with_live_primary_traceback(self):
        self._transaction_probe("constructor")

    def test_f03_failed_start_retains_socket_for_close_retry(self):
        self._transaction_probe("retry")

    def test_loopback_adapter_thread_start_failure_closes_bound_socket_and_reraises(self):
        from invite_rooms.control.adapter import LoopbackAdapter
        port = free_ports(1)[0]
        marker = RuntimeError("adapter serve thread start failed")
        bound_when_failed = []
        adapter = LoopbackAdapter()
        try:
            patched = failing_thread_start("room-loopback-adapter", marker,
                                           lambda: bound_when_failed.append(not bindable(port)))
            with mock.patch.object(threading.Thread, "start", patched):
                with self.assertRaises(RuntimeError) as raised:
                    adapter.start(host="127.0.0.1", port=port)
            self.assertIs(raised.exception, marker)
            self.assertEqual(bound_when_failed, [True], "the real listener must have been bound at the failure point")
            self.assertTrue(bindable(port), "the bound listening socket must be released")
            self.assertIsNone(adapter._server)
            self.assertIsNone(adapter._thread)
            self.assertFalse([t for t in threading.enumerate() if t.name.startswith(OWNED_THREAD_PREFIXES)])
            # Positive control: the very same adapter object starts and closes normally afterwards.
            adapter.start(host="127.0.0.1", port=port)
            self.assertFalse(bindable(port))
            self.assertTrue(adapter.is_running())
            adapter.close()
            self.assertTrue(bindable(port))
            self.assertFalse([t for t in threading.enumerate() if t.name.startswith(OWNED_THREAD_PREFIXES)])
        finally:
            leaked = adapter._server
            if leaked is not None and not (adapter._thread is not None and adapter._thread.is_alive()):
                leaked.server_close()                   # never call shutdown(): no serve loop ever ran
                adapter._server = None


class FixtureIsolationTest(unittest.TestCase):
    def _bootstrap_probe(self, *, contaminate=False, forbidden=False):
        # CHILD_BOOTSTRAP owns process-global state (watcher and umask). Never
        # execute it in unittest's shared interpreter: prior SessionDB imports
        # there are legitimate, but remain forbidden in each actual child.
        import local_telemetry_server as lts
        watcher = lts.start_gateway_watcher
        mask = os.umask(0o077)
        os.umask(mask)
        parent_state = sys.modules.get('hermes_state')
        bootstrap = CHILD_BOOTSTRAP
        if forbidden:
            bootstrap = bootstrap.replace('lts.start_gateway_watcher = isolated_gateway_watcher', '')
        code = r'''
import os, sys, socket
from unittest import mock
namespace = {}
mask = os.umask(0o077)
os.umask(mask)
with mock.patch.object(socket, 'socket', side_effect=AssertionError('network forbidden')), \
     mock.patch.object(socket, 'create_connection', side_effect=AssertionError('network forbidden')):
    import local_telemetry_server as lts
    watcher = lts.start_gateway_watcher
    try:
        with mock.patch.object(lts, 'start_gateway_watcher',
                               side_effect=AssertionError('forbidden watcher entered')) as forbidden:
            exec(compile(sys.argv[1], '<child-bootstrap>', 'exec'), namespace)
            lts.start_gateway_watcher()
            if sys.argv[2] == 'contaminate':
                import hermes_state
            namespace['assert_gateway_isolated']()
            forbidden.assert_not_called()
    finally:
        os.umask(mask)
        assert lts.start_gateway_watcher is watcher, 'watcher mock leaked'
        restored = os.umask(mask)
        assert restored == mask, 'umask leaked'
'''
        with tempfile.TemporaryDirectory(prefix='bootstrap-isolation-') as root:
            home = Path(root) / 'home'
            home.mkdir(mode=0o700)
            env = {'PATH': '/usr/bin:/bin', 'HOME': str(home), 'HERMES_HOME': str(home / '.hermes'),
                   'TMPDIR': root, 'PYTHONDONTWRITEBYTECODE': '1',
                   'PYTHONPATH': f'{SERVER_DIR}:{SERVER_DIR / "tests"}:{CORE_CONTRACT_DIR}'}
            argv = ['/usr/bin/env', '-i', *[f'{k}={v}' for k, v in env.items()],
                    sys.executable, '-B', '-c', code, bootstrap, 'contaminate' if contaminate else 'clean']
            started = time.time()
            child = subprocess.Popen(argv, cwd=REPO_DIR, env=env, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True)
            birth = psutil.Process(child.pid).create_time()
            timed_out = False
            try:
                output, _ = child.communicate(timeout=60)
            except subprocess.TimeoutExpired:
                timed_out = True
                child.kill()
                output, _ = child.communicate(timeout=10)
            finally:
                if child.poll() is None:
                    child.kill()
                    child.communicate(timeout=10)
            record_probe(argv, env, REPO_DIR, child, birth, started, output, timed_out=timed_out)
            self.assertFalse(timed_out, output)
            self.assertFalse(psutil.pid_exists(child.pid), 'owned bootstrap probe must be reaped')
        self.assertIs(lts.start_gateway_watcher, watcher)
        self.assertIs(sys.modules.get('hermes_state'), parent_state)
        current = os.umask(mask)
        self.assertEqual(current, mask, 'bootstrap changed parent umask')
        return child.returncode, output

    def test_bootstrap_never_enters_the_forbidden_watcher(self):
        code, output = self._bootstrap_probe()
        self.assertEqual(code, 0, output)
        self.assertIn('LIFECYCLE-ISOLATION-VERIFIED', output)

    def test_bootstrap_rejects_sessiondb_import_in_child(self):
        code, output = self._bootstrap_probe(contaminate=True)
        self.assertNotEqual(code, 0, output)
        self.assertIn('watcher must not lazily import SessionDB', output)
        self.assertNotIn('LIFECYCLE-ISOLATION-VERIFIED', output)

    def test_bootstrap_rejects_forbidden_watcher_in_child(self):
        code, output = self._bootstrap_probe(forbidden=True)
        self.assertNotEqual(code, 0, output)
        self.assertIn('forbidden watcher entered', output)
        self.assertNotIn('LIFECYCLE-ISOLATION-VERIFIED', output)

    def test_all_child_launch_modes_install_watcher_isolation_before_main(self):
        modes = (None, "serve", "startup-sigint", "startup-repeat", "serve-seam",
                 "cleanup-primary", "cleanup-normal",
                 *(f"cleanup-{kind}-{op}" for kind in ("primary", "normal")
                   for op in ("write", "flush", "oserror")))
        with tempfile.TemporaryDirectory(prefix="isolation-launch-") as root:
            fixture = SimpleNamespace(tmp=Path(root), children=[], child_env=lambda *a, **k: {})
            for mode in modes:
                with self.subTest(mode=mode), mock.patch(__name__ + ".Child") as child:
                    Base.spawn(fixture, wrapper_mode=mode)
                    argv = child.call_args.args[0]
                    source = Path(argv[-1]).read_text()
                    self.assertIn("lts.start_gateway_watcher = isolated_gateway_watcher", source)
                    self.assertLess(source.index("lts.start_gateway_watcher = isolated_gateway_watcher"),
                                    source.index("lts.main()"))


class ImportGuardTest(unittest.TestCase):
    def test_telemetry_module_keeps_listener_literals_out_and_lazy_imports_the_plane(self):
        source = LTS_PATH.read_text()
        for needle in ("control.listener", "ControlListener", "build_pair_ack_listener", "control_listen"):
            self.assertNotIn(needle, source)
        code = ("import sys; sys.path.insert(0, %r); import local_telemetry_server; "
                "print('invite_rooms.control.listener' in sys.modules, 'invite_rooms.sidecar_data_plane' in sys.modules)"
                % str(SERVER_DIR))
        env = {"PATH": "/usr/bin:/bin", "HOME": tempfile.gettempdir(), "TMPDIR": tempfile.gettempdir(),
               "PYTHONPATH": f"{SERVER_DIR}:{SERVER_DIR / 'tests'}", "PYTHONDONTWRITEBYTECODE": "1"}
        out = subprocess.run(["/usr/bin/env", "-i", *[f"{k}={v}" for k, v in env.items()],
                              sys.executable, "-B", "-W", "error::ResourceWarning", "-c", code],
                             capture_output=True, text=True, timeout=60, env=env)
        self.assertEqual(out.stdout.strip().splitlines()[-1:], ["False False"], out.stderr[-500:])


if __name__ == "__main__":
    unittest.main()
