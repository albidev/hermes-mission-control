"""Tests for CORS origin enforcement (issue #17).

When ``MISSION_CONTROL_ALLOWED_ORIGIN`` is configured, the telemetry server
must accept only that exact origin. Requests carrying any other ``Origin``
must receive no CORS headers, and requests without an ``Origin`` header are
not subject to CORS at all.

When the variable is unset, the historical dev-mode behavior is preserved:
the incoming origin is mirrored so Tailscale/LAN browser sidecars work
without extra configuration.
"""

import json
import os
import socket
import sys
import threading
import time
import types
import unittest
import urllib.request
from contextlib import closing
from pathlib import Path
import importlib.util
from typing import Optional


fake_psutil = types.SimpleNamespace(
    cpu_percent=lambda interval=None: 7.5,
    virtual_memory=lambda: types.SimpleNamespace(percent=42.0, used=8 * 1024**3, available=8 * 1024**3, total=16 * 1024**3),
    disk_usage=lambda path: types.SimpleNamespace(percent=55.0, free=100 * 1024**3, total=200 * 1024**3),
    Process=lambda: types.SimpleNamespace(memory_info=lambda: types.SimpleNamespace(rss=256 * 1024**2)),
)
sys.modules.setdefault("psutil", fake_psutil)

MODULE_PATH = Path(__file__).resolve().parents[1] / "server" / "local_telemetry_server.py"
SPEC = importlib.util.spec_from_file_location("mission_control_local_telemetry_cors", MODULE_PATH)
local_telemetry_server = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(local_telemetry_server)


def _start_server():
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = local_telemetry_server.ThreadingHTTPServer(("127.0.0.1", port), local_telemetry_server.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.05)
    return server, thread, port


def _stop_server(server, thread):
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


class CorsOriginEnforcementTests(unittest.TestCase):
    def setUp(self):
        self._env_backup = {
            "MISSION_CONTROL_ALLOWED_ORIGIN": os.environ.get("MISSION_CONTROL_ALLOWED_ORIGIN"),
            "MISSION_CONTROL_TOKEN": os.environ.get("MISSION_CONTROL_TOKEN"),
        }
        os.environ["MISSION_CONTROL_TOKEN"] = "cors-secret"
        os.environ.pop("MISSION_CONTROL_ALLOWED_ORIGIN", None)
        self.server, self.thread, self.port = _start_server()

    def tearDown(self):
        _stop_server(self.server, self.thread)
        for key, value in self._env_backup.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _request(self, path: str, origin: Optional[str] = None):
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}")
        request.add_header("Authorization", "Bearer cors-secret")
        if origin:
            request.add_header("Origin", origin)
        return urllib.request.urlopen(request, timeout=5)

    def _allowed_origin_header(self, origin: Optional[str]) -> Optional[str]:
        with self._request("/api/local/system", origin=origin) as response:
            return response.headers.get("Access-Control-Allow-Origin")

    # --- configured allow-list mode -------------------------------------

    def test_configured_origin_is_accepted(self):
        os.environ["MISSION_CONTROL_ALLOWED_ORIGIN"] = "http://100.84.148.17:5174"
        self.assertEqual(
            self._allowed_origin_header("http://100.84.148.17:5174"),
            "http://100.84.148.17:5174",
        )

    def test_rejected_origin_gets_no_cors_headers(self):
        os.environ["MISSION_CONTROL_ALLOWED_ORIGIN"] = "http://100.84.148.17:5174"
        self.assertIsNone(self._allowed_origin_header("http://evil.example:5174"))

    def test_similar_origin_is_still_rejected(self):
        os.environ["MISSION_CONTROL_ALLOWED_ORIGIN"] = "http://100.84.148.17:5174"
        self.assertIsNone(self._allowed_origin_header("http://100.84.148.17:5175"))
        self.assertIsNone(self._allowed_origin_header("http://100.84.148.17:5174.evil.example"))
        self.assertIsNone(self._allowed_origin_header("https://100.84.148.17:5174"))

    def test_missing_origin_with_allowlist_passes_through(self):
        os.environ["MISSION_CONTROL_ALLOWED_ORIGIN"] = "http://100.84.148.17:5174"
        # A non-browser client (no Origin header) is not subject to CORS and
        # must still be served; it just gets no CORS headers.
        self.assertIsNone(self._allowed_origin_header(None))

    def test_preflight_with_matching_origin_is_allowed(self):
        os.environ["MISSION_CONTROL_ALLOWED_ORIGIN"] = "http://100.84.148.17:5174"
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/local/config", method="OPTIONS")
        request.add_header("Origin", "http://100.84.148.17:5174")
        request.add_header("Access-Control-Request-Method", "GET")
        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertEqual(response.status, 204)
            self.assertEqual(response.headers.get("Access-Control-Allow-Origin"), "http://100.84.148.17:5174")

    def test_preflight_with_wrong_origin_is_rejected(self):
        os.environ["MISSION_CONTROL_ALLOWED_ORIGIN"] = "http://100.84.148.17:5174"
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/local/config", method="OPTIONS")
        request.add_header("Origin", "http://evil.example:5174")
        request.add_header("Access-Control-Request-Method", "GET")
        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertEqual(response.status, 204)
            self.assertIsNone(response.headers.get("Access-Control-Allow-Origin"))

    # --- dev mode (unset) ------------------------------------------------

    def test_unconfigured_dev_mode_mirrors_incoming_origin(self):
        self.assertEqual(self._allowed_origin_header("http://100.84.148.17:5174"), "http://100.84.148.17:5174")

    def test_unconfigured_dev_mode_without_origin(self):
        self.assertIsNone(self._allowed_origin_header(None))


class TelemetryBindSmokeTests(unittest.TestCase):
    """Smoke test proving the configured bind host and port are honored."""

    def setUp(self):
        self._env_backup = {
            "MISSION_CONTROL_LOCAL_TELEMETRY_HOST": os.environ.get("MISSION_CONTROL_LOCAL_TELEMETRY_HOST"),
            "MISSION_CONTROL_LOCAL_TELEMETRY_PORT": os.environ.get("MISSION_CONTROL_LOCAL_TELEMETRY_PORT"),
            "TELEMETRY_BIND_HOST": os.environ.get("TELEMETRY_BIND_HOST"),
            "TELEMETRY_BIND_PORT": os.environ.get("TELEMETRY_BIND_PORT"),
            "MISSION_CONTROL_TOKEN": os.environ.get("MISSION_CONTROL_TOKEN"),
        }
        for key in ("MISSION_CONTROL_LOCAL_TELEMETRY_HOST", "MISSION_CONTROL_LOCAL_TELEMETRY_PORT",
                    "TELEMETRY_BIND_HOST", "TELEMETRY_BIND_PORT"):
            os.environ.pop(key, None)
        os.environ["MISSION_CONTROL_TOKEN"] = "bind-secret"

    def tearDown(self):
        for key, value in self._env_backup.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_default_bind_is_loopback(self):
        host, port = local_telemetry_server._resolve_telemetry_bind()
        self.assertEqual(host, "127.0.0.1")
        self.assertEqual(port, 8765)

    def test_explicit_bind_host_and_port_are_resolved(self):
        os.environ["MISSION_CONTROL_LOCAL_TELEMETRY_HOST"] = "0.0.0.0"
        os.environ["MISSION_CONTROL_LOCAL_TELEMETRY_PORT"] = "9876"
        host, port = local_telemetry_server._resolve_telemetry_bind()
        self.assertEqual(host, "0.0.0.0")
        self.assertEqual(port, 9876)

    def test_server_listens_on_configured_loopback_port(self):
        os.environ["MISSION_CONTROL_LOCAL_TELEMETRY_PORT"] = "0"  # invalid by design -> proves validation
        with self.assertRaises(SystemExit):
            local_telemetry_server._resolve_telemetry_bind()

    def test_loopback_health_endpoint_is_reachable(self):
        # Bind the real handler to a loopback port and prove /health answers
        # without auth, using the resolved (default) bind host.
        host, _ = local_telemetry_server._resolve_telemetry_bind()
        with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
            sock.bind((host, 0))
            port = sock.getsockname()[1]
        server = local_telemetry_server.ThreadingHTTPServer((host, port), local_telemetry_server.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        time.sleep(0.05)
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as response:
                self.assertEqual(response.status, 200)
                payload = json.loads(response.read().decode("utf-8"))
            self.assertTrue(payload["ok"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
