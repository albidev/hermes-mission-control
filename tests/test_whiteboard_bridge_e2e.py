"""End-to-end protocol tests: run the real telemetry server on an ephemeral
port and exercise the whiteboard bridge the way the agent and canvas do.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
import urllib.request
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parents[1] / "server"
sys.path.insert(0, str(SERVER_DIR))

import local_telemetry_server as server  # noqa: E402


class WhiteboardBridgeE2E(unittest.TestCase):
    httpd = None
    port = 0

    @classmethod
    def setUpClass(cls) -> None:
        import os
        os.environ["MISSION_CONTROL_TOKEN"] = "e2e-test-token"
        cls.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.httpd.server_port
        # Point the store at a temp file so tests never touch runtime state.
        import whiteboard_store
        cls._orig_state_file = whiteboard_store._STATE_FILE
        whiteboard_store._STATE_FILE = SERVER_DIR / "whiteboard_state.e2e.json"
        if whiteboard_store._STATE_FILE.exists():
            whiteboard_store._STATE_FILE.unlink()
        import threading
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()
        import whiteboard_store
        if whiteboard_store._STATE_FILE.exists():
            whiteboard_store._STATE_FILE.unlink()
        whiteboard_store._STATE_FILE = cls._orig_state_file

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def post(self, payload: dict) -> dict:
        request = urllib.request.Request(
            self.url("/api/local/chat/whiteboard"),
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "Authorization": "Bearer e2e-test-token"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read())

    def get(self, query: str) -> dict:
        request = urllib.request.Request(
            self.url(f"/api/local/chat/whiteboard?{query}"),
            headers={"Authorization": "Bearer e2e-test-token"},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read())

    def test_full_command_lifecycle(self) -> None:
        # 1. Enqueue a command like the agent would.
        enqueued = self.post({"sessionId": "e2e-session", "action": "enqueue", "command": {"type": "create_box", "text": "Node", "x": 10, "y": 10}})
        command = enqueued["command"]
        self.assertEqual(command["protocolVersion"], 2)
        self.assertTrue(command["id"])

        # 2. Canvas polls and sees it queued with feature negotiation.
        state = self.get("sessionId=e2e-session")
        self.assertEqual(state["protocolVersion"], 2)
        self.assertIn("import_mermaid", state["features"])
        ids = [c["id"] for c in state["commands"]]
        self.assertIn(command["id"], ids)

        # 3. Canvas acknowledges after applying.
        self.post({"sessionId": "e2e-session", "action": "ack", "commandIds": [command["id"]]})
        state_after = self.get("sessionId=e2e-session")
        self.assertEqual([c["id"] for c in state_after["commands"]], [])

    def test_mode_persistence_roundtrip(self) -> None:
        result = self.post({"sessionId": "e2e-mode", "action": "mode", "mode": "review"})
        self.assertEqual(result["mode"], "review")
        state = self.get("sessionId=e2e-mode")
        self.assertEqual(state["agentMode"], "review")
        # Invalid mode rejected.
        with self.assertRaises(Exception):
            self.post({"sessionId": "e2e-mode", "action": "mode", "mode": "party"})

    def test_snapshot_save_and_resume_via_stable_session_id(self) -> None:
        snapshot = {"document": {"store": {"shape:a1": {"type": "geo", "x": 0, "props": {"w": 100, "h": 50}}}}}
        self.post({"sessionKey": "runtime-key-1", "sessionId": "runtime-a", "snapshot": snapshot})
        by_new_key = self.get("sessionKey=runtime-key-2&sessionId=runtime-a")
        self.assertIsNotNone(by_new_key["snapshot"])
        self.assertEqual(by_new_key["snapshot"]["document"]["store"]["shape:a1"]["props"]["w"], 100)


if __name__ == "__main__":
    unittest.main()
