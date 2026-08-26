import importlib.util
import json
import os
import socket
import sys
import threading
import types
import unittest
import urllib.error
import urllib.request
from contextlib import closing
from pathlib import Path


fake_psutil = types.SimpleNamespace(
    cpu_percent=lambda interval=None: 7.5,
    virtual_memory=lambda: types.SimpleNamespace(percent=42.0, used=8 * 1024**3, available=8 * 1024**3, total=16 * 1024**3),
    disk_usage=lambda path: types.SimpleNamespace(percent=55.0, free=100 * 1024**3, total=200 * 1024**3),
    Process=lambda: types.SimpleNamespace(memory_info=lambda: types.SimpleNamespace(rss=256 * 1024**2)),
)
sys.modules.setdefault("psutil", fake_psutil)

MODULE_PATH = Path(__file__).resolve().parents[1] / "server" / "local_telemetry_server.py"
SPEC = importlib.util.spec_from_file_location("mission_control_cron_api_server", MODULE_PATH)
local_telemetry_server = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(local_telemetry_server)


class CronApiTests(unittest.TestCase):
    def setUp(self):
        self.job = {"id": "job-123", "name": "nightly", "enabled": True, "state": "scheduled"}
        self.calls = []

        def record(name, result=None):
            def handler(*args, **kwargs):
                self.calls.append((name, args, kwargs))
                return result if result is not None else {"success": True, "job": self.job}
            return handler

        self.fake_bridge = types.SimpleNamespace(
            list_jobs=lambda include_disabled=True, include_output=True: [self.job],
            get_job=record("get", self.job),
            create_job=record("create"),
            update_job=record("update"),
            pause_job=record("pause"),
            resume_job=record("resume"),
            run_job=record("run"),
            delete_job=record("delete", {"success": True, "job_id": "job-123"}),
        )
        self.previous_bridge = local_telemetry_server.cron_bridge_mod
        local_telemetry_server.cron_bridge_mod = self.fake_bridge
        self.previous_token = os.environ.get("MISSION_CONTROL_TOKEN")
        os.environ["MISSION_CONTROL_TOKEN"] = "cron-test-token"
        with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        self.server = local_telemetry_server.ThreadingHTTPServer(("127.0.0.1", self.port), local_telemetry_server.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        local_telemetry_server.cron_bridge_mod = self.previous_bridge
        if self.previous_token is None:
            os.environ.pop("MISSION_CONTROL_TOKEN", None)
        else:
            os.environ["MISSION_CONTROL_TOKEN"] = self.previous_token

    def request(self, path, method="GET", payload=None, token="cron-test-token"):
        body = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=body, method=method)
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        if body:
            request.add_header("Content-Type", "application/json")
        return urllib.request.urlopen(request, timeout=5)

    def test_list_and_detail_are_authorized_and_return_cron_data(self):
        with self.request("/api/local/cron/jobs") as response:
            payload = json.loads(response.read())
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["jobs"][0]["id"], "job-123")

        with self.request("/api/local/cron/jobs/job-123") as response:
            detail = json.loads(response.read())
        self.assertEqual(detail["id"], "job-123")
        self.assertEqual(self.calls[0][0], "get")

    def test_mutating_actions_route_to_bridge(self):
        actions = [
            ("POST", "/api/local/cron/jobs", {"name": "new", "schedule": "every 1h"}, "create"),
            ("PATCH", "/api/local/cron/jobs/job-123", {"name": "changed"}, "update"),
            ("POST", "/api/local/cron/jobs/job-123/run", {}, "run"),
            ("POST", "/api/local/cron/jobs/job-123/pause", {"reason": "maintenance"}, "pause"),
            ("POST", "/api/local/cron/jobs/job-123/resume", {}, "resume"),
            ("DELETE", "/api/local/cron/jobs/job-123", None, "delete"),
        ]
        for method, path, payload, expected_call in actions:
            with self.subTest(method=method, path=path):
                with self.request(path, method=method, payload=payload) as response:
                    self.assertEqual(response.status, 200)
                self.assertEqual(self.calls[-1][0], expected_call)

    def test_cron_mutations_require_authentication(self):
        with self.assertRaises(urllib.error.HTTPError) as context:
            self.request("/api/local/cron/jobs/job-123/run", method="POST", payload={"x": 1}, token=None)
        self.assertEqual(context.exception.code, 401)


if __name__ == "__main__":
    unittest.main()
