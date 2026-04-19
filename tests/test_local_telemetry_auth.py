import json
import os
import socket
import threading
import time
import types
import unittest
import urllib.error
import urllib.request
from contextlib import closing
from pathlib import Path
import importlib.util
from typing import Optional
import sys


fake_psutil = types.SimpleNamespace(
    cpu_percent=lambda interval=None: 7.5,
    virtual_memory=lambda: types.SimpleNamespace(percent=42.0, used=8 * 1024**3, available=8 * 1024**3, total=16 * 1024**3),
    disk_usage=lambda path: types.SimpleNamespace(percent=55.0, free=100 * 1024**3, total=200 * 1024**3),
    Process=lambda: types.SimpleNamespace(memory_info=lambda: types.SimpleNamespace(rss=256 * 1024**2)),
)
sys.modules.setdefault("psutil", fake_psutil)

MODULE_PATH = Path(__file__).resolve().parents[1] / "server" / "local_telemetry_server.py"
SPEC = importlib.util.spec_from_file_location("mission_control_local_telemetry_server", MODULE_PATH)
local_telemetry_server = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(local_telemetry_server)


class LocalTelemetryAuthTests(unittest.TestCase):
    def setUp(self):
        self._env_backup = {
            "MISSION_CONTROL_TOKEN": os.environ.get("MISSION_CONTROL_TOKEN"),
            "API_SERVER_KEY": os.environ.get("API_SERVER_KEY"),
        }
        os.environ["MISSION_CONTROL_TOKEN"] = "phase1-secret"
        os.environ.pop("API_SERVER_KEY", None)

        with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]

        self.server = local_telemetry_server.ThreadingHTTPServer(("127.0.0.1", self.port), local_telemetry_server.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        time.sleep(0.05)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        for key, value in self._env_backup.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _request(self, path: str, token: Optional[str] = None):
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}")
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        return urllib.request.urlopen(request, timeout=5)

    def test_health_endpoint_stays_open_without_auth(self):
        with self._request("/health") as response:
            self.assertEqual(response.status, 200)
            payload = json.loads(response.read().decode("utf-8"))
        self.assertTrue(payload["ok"])

    def test_system_endpoint_rejects_missing_token(self):
        with self.assertRaises(urllib.error.HTTPError) as exc:
            self._request("/api/local/system")
        self.assertEqual(exc.exception.code, 401)
        self.assertEqual(exc.exception.headers.get("WWW-Authenticate"), 'Bearer realm="Mission Control"')
        payload = json.loads(exc.exception.read().decode("utf-8"))
        self.assertEqual(payload["error"], "invalid_api_key")

    def test_system_endpoint_accepts_mission_control_token(self):
        with self._request("/api/local/system", token="phase1-secret") as response:
            self.assertEqual(response.status, 200)
            payload = json.loads(response.read().decode("utf-8"))
        self.assertEqual(payload["source"], "local-psutil")
        self.assertIn("cpuUsagePercent", payload)

    def test_api_server_key_is_used_as_fallback_token(self):
        os.environ.pop("MISSION_CONTROL_TOKEN", None)
        os.environ["API_SERVER_KEY"] = "api-key-secret"

        with self._request("/api/local/system", token="api-key-secret") as response:
            self.assertEqual(response.status, 200)
            payload = json.loads(response.read().decode("utf-8"))
        self.assertEqual(payload["source"], "local-psutil")


if __name__ == "__main__":
    unittest.main()
