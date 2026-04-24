import json
import os
import socket
import threading
import time
import types
import unittest
import urllib.error
import urllib.parse
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

    def test_query_access_token_is_rejected_for_non_stream_endpoints(self):
        encoded = urllib.parse.quote("phase1-secret", safe="")
        with self.assertRaises(urllib.error.HTTPError) as exc:
            self._request(f"/api/local/system?access_token={encoded}")
        self.assertEqual(exc.exception.code, 401)

    def test_mission_control_agent_endpoints_are_served_from_sidecar(self):
        sessions_dir = Path.home() / ".hermes" / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        session_id = "mc-sidecar-test-session"
        sidecar_path = sessions_dir / f"session_{session_id}.json"
        index_path = sessions_dir / "sessions.json"
        original_sidecar = sidecar_path.read_text(encoding="utf-8", errors="replace") if sidecar_path.exists() else None
        original_index = index_path.read_text(encoding="utf-8", errors="replace") if index_path.exists() else None

        payload = {
            "platform": "discord",
            "model": "gpt-5.4",
            "session_start": "2026-04-24T12:00:00+00:00",
            "last_updated": "2999-04-24T12:00:05+00:00",
            "message_count": 4,
            "messages": [
                {"role": "user", "content": "Ship the sidecar trace feed.", "timestamp": "2026-04-24T12:00:00+00:00"},
                {
                    "role": "assistant",
                    "timestamp": "2026-04-24T12:00:01+00:00",
                    "tool_calls": [
                        {
                            "id": "call_sidecar_trace",
                            "function": {
                                "name": "search_files",
                                "arguments": '{"pattern":"mission-control"}',
                            },
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_sidecar_trace",
                    "tool_name": "search_files",
                    "content": '{"success": true, "total_count": 1}',
                    "timestamp": "2026-04-24T12:00:02+00:00",
                },
                {"role": "assistant", "content": "Done.", "timestamp": "2026-04-24T12:00:03+00:00"},
            ],
        }
        index_payload = {
            "discord-home": {
                "session_id": session_id,
                "platform": "discord",
                "display_name": "Mission Control sidecar test",
                "chat_type": "channel",
                "created_at": "2026-04-24T12:00:00+00:00",
                "updated_at": "2999-04-24T12:00:05+00:00",
            }
        }

        try:
            sidecar_path.write_text(json.dumps(payload), encoding="utf-8")
            index_path.write_text(json.dumps(index_payload), encoding="utf-8")

            with self._request("/api/local/mission-control/agents", token="phase1-secret") as response:
                self.assertEqual(response.status, 200)
                agents_payload = json.loads(response.read().decode("utf-8"))

            self.assertTrue(agents_payload["available"])
            self.assertTrue(agents_payload["capabilities"]["trace"]["stream"])
            self.assertTrue(any(item["model"] == "gpt-5.4" for item in agents_payload["items"]))

            with self._request("/api/local/mission-control/sessions?limit=1", token="phase1-secret") as response:
                self.assertEqual(response.status, 200)
                sessions_payload = json.loads(response.read().decode("utf-8"))

            self.assertEqual(len(sessions_payload["items"]), 1)
            self.assertEqual(sessions_payload["items"][0]["sessionId"], session_id)
            self.assertEqual(sessions_payload["items"][0]["traceMode"], "transcript")

            with self._request(
                f"/api/local/mission-control/agents/trace?session_id={session_id}&compact=1&limit=20",
                token="phase1-secret",
            ) as response:
                self.assertEqual(response.status, 200)
                trace_payload = json.loads(response.read().decode("utf-8"))

            self.assertTrue(trace_payload["available"])
            self.assertEqual(trace_payload["traceMode"], "transcript")
            self.assertTrue(any(event["type"] == "tool_call_started" for event in trace_payload["events"]))
            self.assertTrue(any(event["type"] == "tool_call_completed" for event in trace_payload["events"]))
        finally:
            if original_sidecar is None:
                try:
                    sidecar_path.unlink()
                except FileNotFoundError:
                    pass
            else:
                sidecar_path.write_text(original_sidecar, encoding="utf-8")

            if original_index is None:
                try:
                    index_path.unlink()
                except FileNotFoundError:
                    pass
            else:
                index_path.write_text(original_index, encoding="utf-8")

    def test_knowledge_endpoints_index_vault_and_return_file_content(self):
        hermes_home = Path.home() / ".hermes"
        hermes_home.mkdir(parents=True, exist_ok=True)
        soul_path = hermes_home / "SOUL.md"
        user_path = hermes_home / "USER.md"
        agents_path = hermes_home / "AGENTS.md"
        original_core = {
            soul_path: soul_path.read_text(encoding="utf-8", errors="replace") if soul_path.exists() else None,
            user_path: user_path.read_text(encoding="utf-8", errors="replace") if user_path.exists() else None,
            agents_path: agents_path.read_text(encoding="utf-8", errors="replace") if agents_path.exists() else None,
            hermes_home / "config.md": (hermes_home / "config.md").read_text(encoding="utf-8", errors="replace") if (hermes_home / "config.md").exists() else None,
        }

        vault = Path.home() / "Documents" / "Hermes"
        vault.mkdir(parents=True, exist_ok=True)
        knowledge_path = vault / "Knowledge Sharing.md"
        project_dir = vault / "projects"
        project_dir.mkdir(parents=True, exist_ok=True)
        project_path = project_dir / "mission-control.md"
        original_vault = {
            knowledge_path: knowledge_path.read_text(encoding="utf-8", errors="replace") if knowledge_path.exists() else None,
            project_path: project_path.read_text(encoding="utf-8", errors="replace") if project_path.exists() else None,
            vault / ".private" / "secret.md": (vault / ".private" / "secret.md").read_text(encoding="utf-8", errors="replace") if (vault / ".private" / "secret.md").exists() else None,
        }

        try:
            soul_path.write_text("# Soul\n\n- Identity\n", encoding="utf-8")
            user_path.write_text("# User\n\nPreferences\n", encoding="utf-8")
            agents_path.write_text("# Agents\n\n- Rules\n", encoding="utf-8")
            knowledge_path.write_text(
                "# Knowledge Sharing\n\nShared dashboard note.\n\n- Highlight one\n- Highlight two\n",
                encoding="utf-8",
            )
            project_path.write_text(
                "# Mission Control\n\nProject details here.\n\n## Next\n- Ship fixes\n",
                encoding="utf-8",
            )

            with self._request("/api/local/knowledge", token="phase1-secret") as response:
                self.assertEqual(response.status, 200)
                payload = json.loads(response.read().decode("utf-8"))

            self.assertTrue(payload["available"])
            self.assertEqual(payload["vaultPath"], "~/Documents/Hermes")
            self.assertEqual(payload["primary"]["title"], "Knowledge Sharing")
            self.assertEqual(payload["primary"]["sourcePath"], "~/Documents/Hermes/Knowledge Sharing.md")
            self.assertTrue(any(section["id"] == "vault-notes" for section in payload["sections"]))

            encoded = urllib.parse.quote("~/Documents/Hermes/Knowledge Sharing.md", safe="")
            with self._request(f"/api/local/knowledge/file?path={encoded}", token="phase1-secret") as response:
                self.assertEqual(response.status, 200)
                file_payload = json.loads(response.read().decode("utf-8"))

            self.assertTrue(file_payload["success"])
            self.assertEqual(file_payload["title"], "Knowledge Sharing")
            self.assertIn("Shared dashboard note.", file_payload["content"])

            encoded_core = urllib.parse.quote("~/.hermes/SOUL.md", safe="")
            with self._request(f"/api/local/knowledge/file?path={encoded_core}", token="phase1-secret") as response:
                self.assertEqual(response.status, 200)
                core_payload = json.loads(response.read().decode("utf-8"))

            self.assertTrue(core_payload["success"])
            self.assertEqual(core_payload["path"], "SOUL.md")
            self.assertEqual(core_payload["sourcePath"], "~/.hermes/SOUL.md")
            self.assertIn("Identity", core_payload["content"])

            hidden_dir = vault / ".private"
            hidden_dir.mkdir(parents=True, exist_ok=True)
            hidden_path = hidden_dir / "secret.md"
            hidden_path.write_text("# Hidden\n\nNope\n", encoding="utf-8")
            encoded_hidden = urllib.parse.quote("~/Documents/Hermes/.private/secret.md", safe="")
            with self.assertRaises(urllib.error.HTTPError) as hidden_exc:
                self._request(f"/api/local/knowledge/file?path={encoded_hidden}", token="phase1-secret")
            self.assertEqual(hidden_exc.exception.code, 403)

            extra_core = hermes_home / "config.md"
            extra_core.write_text("# Config\n\nShould not leak\n", encoding="utf-8")
            encoded_extra_core = urllib.parse.quote("~/.hermes/config.md", safe="")
            with self.assertRaises(urllib.error.HTTPError) as core_exc:
                self._request(f"/api/local/knowledge/file?path={encoded_extra_core}", token="phase1-secret")
            self.assertEqual(core_exc.exception.code, 403)
        finally:
            for path, original in {**original_core, **original_vault}.items():
                if original is None:
                    try:
                        path.unlink()
                    except FileNotFoundError:
                        pass
                else:
                    path.write_text(original, encoding="utf-8")

    def test_knowledge_file_endpoint_rejects_paths_outside_vault(self):
        outside = Path.home() / "Documents" / "outside.md"
        outside.parent.mkdir(parents=True, exist_ok=True)
        outside.write_text("# Outside\n", encoding="utf-8")
        encoded = urllib.parse.quote("~/Documents/outside.md", safe="")
        with self.assertRaises(urllib.error.HTTPError) as exc:
            self._request(f"/api/local/knowledge/file?path={encoded}", token="phase1-secret")
        self.assertEqual(exc.exception.code, 403)


if __name__ == "__main__":
    unittest.main()
