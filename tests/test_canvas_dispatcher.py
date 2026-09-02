import json
import os
import socket
import threading
import time
import urllib.error
import urllib.request
from contextlib import closing
from pathlib import Path
import importlib.util
import unittest
from typing import Optional

MODULE_PATH = Path(__file__).resolve().parents[1] / "server" / "local_telemetry_server.py"
SPEC = importlib.util.spec_from_file_location("mission_control_local_telemetry_server", MODULE_PATH)
assert SPEC is not None
local_telemetry_server = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(local_telemetry_server)

TOKEN = "phase1-secret"


class CanvasAddonDispatcherTests(unittest.TestCase):
    def setUp(self):
        self._env_backup = {
            "MISSION_CONTROL_TOKEN": os.environ.get("MISSION_CONTROL_TOKEN"),
            "MISSION_CONTROL_READ_ONLY": os.environ.get("MISSION_CONTROL_READ_ONLY"),
        }
        os.environ["MISSION_CONTROL_TOKEN"] = TOKEN
        os.environ.pop("MISSION_CONTROL_READ_ONLY", None)

        with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]

        self.server = local_telemetry_server.ThreadingHTTPServer(
            ("127.0.0.1", self.port), local_telemetry_server.Handler
        )
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

    def _build(self, path, token: Optional[str] = TOKEN, method="GET", body=None):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", method=method
        )
        if token is not None and token != "":
            req.add_header("Authorization", f"Bearer {token}")
        if body is not None:
            data = json.dumps(body).encode()
            req.add_header("Content-Type", "application/json")
            req.data = data
        return req

    def _send(self, req):
        return urllib.request.urlopen(req, timeout=5)

    def test_tldraw_get_exists(self):
        """GET /api/local/chat/canvas/tldraw?sessionId=s1 returns 200."""
        req = self._build("/api/local/chat/canvas/tldraw?sessionId=s1")
        resp = self._send(req)
        self.assertEqual(resp.status, 200)

    def test_whiteboard_get_exists(self):
        """GET /api/local/chat/whiteboard?sessionId=s1 returns 200."""
        req = self._build("/api/local/chat/whiteboard?sessionId=s1")
        resp = self._send(req)
        self.assertEqual(resp.status, 200)

    def test_whiteboard_post_exists(self):
        """POST /api/local/chat/whiteboard returns 200."""
        req = self._build(
            "/api/local/chat/whiteboard",
            method="POST",
            body={"action": "enqueue", "sessionId": "s1", "command": {"type": "test"}},
        )
        resp = self._send(req)
        self.assertEqual(resp.status, 200)

    def test_canvas_get_with_unknown_addon_returns_400(self):
        """GET /api/local/chat/canvas/unknown_addon?sessionId=s1 → 400."""
        req = self._build("/api/local/chat/canvas/unknown_addon?sessionId=s1")
        with self.assertRaises(urllib.error.HTTPError) as exc:
            self._send(req)
        self.assertEqual(exc.exception.code, 400)
        payload = json.loads(exc.exception.read().decode("utf-8"))
        self.assertEqual(payload["error"], "bad_request")

    def test_canvas_post_with_unknown_addon_returns_400(self):
        """POST /api/local/chat/canvas/unknown_addon → 400."""
        req = self._build(
            "/api/local/chat/canvas/unknown_addon",
            method="POST",
            body={"action": "save"},
        )
        with self.assertRaises(urllib.error.HTTPError) as exc:
            self._send(req)
        self.assertEqual(exc.exception.code, 400)
        payload = json.loads(exc.exception.read().decode("utf-8"))
        self.assertEqual(payload["error"], "bad_request")

    def test_canvas_post_payload_too_large_raises(self):
        """POST /api/local/chat/canvas/tldraw with body > 8MB raises HTTPError or URLError."""
        huge = "x" * (9 * 1024 * 1024)  # Just over 8MB limit
        req = self._build(
            "/api/local/chat/canvas/tldraw",
            method="POST",
            body={"snapshot": huge},
        )
        with self.assertRaises((urllib.error.HTTPError, urllib.error.URLError)):
            self._send(req)

    def test_canvas_addon_without_token_returns_401(self):
        """GET /api/local/chat/canvas/tldraw?sessionId=s1 without token → 401."""
        req = self._build("/api/local/chat/canvas/tldraw?sessionId=s1", token=None)
        with self.assertRaises(urllib.error.HTTPError) as exc:
            self._send(req)
        self.assertEqual(exc.exception.code, 401)

    def test_canvas_addon_with_wrong_token_returns_401(self):
        """GET /api/local/chat/canvas/tldraw?sessionId=s1 with wrong token → 401."""
        req = self._build("/api/local/chat/canvas/tldraw?sessionId=s1", token="wrong-token")
        with self.assertRaises(urllib.error.HTTPError) as exc:
            self._send(req)
        self.assertEqual(exc.exception.code, 401)

    def test_canvas_addon_with_correct_token_returns_200(self):
        """GET /api/local/chat/canvas/tldraw?sessionId=s1 with correct token → 200."""
        req = self._build("/api/local/chat/canvas/tldraw?sessionId=s1")
        resp = self._send(req)
        self.assertEqual(resp.status, 200)
