"""Backend tests for the session_synthesis Curate endpoints.

Covers the MC-side pre-write gate: auth, feature gate, vault isolation, safe
field projection (no transcript leak), and apply/reject routing.
"""

from __future__ import annotations

import importlib.util
import json
import os
import socket
import sys
import threading
import time
import types
import unittest
import urllib.error
import urllib.request
from contextlib import closing
from pathlib import Path
from unittest import mock

fake_psutil = types.SimpleNamespace(
    cpu_percent=lambda interval=None: 7.5,
    virtual_memory=lambda: types.SimpleNamespace(percent=42.0, used=8 * 1024**3, available=8 * 1024**3, total=16 * 1024**3),
    disk_usage=lambda path: types.SimpleNamespace(percent=55.0, free=100 * 1024**3, total=200 * 1024**3),
    Process=lambda: types.SimpleNamespace(memory_info=lambda: types.SimpleNamespace(rss=256 * 1024**2)),
)
sys.modules.setdefault("psutil", fake_psutil)

SERVER_DIR = Path(__file__).resolve().parents[1] / "server"
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

MODULE_PATH = SERVER_DIR / "local_telemetry_server.py"
SPEC = importlib.util.spec_from_file_location("mission_control_session_synthesis_curate", MODULE_PATH)
local_telemetry_server = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(local_telemetry_server)


def _candidate(**overrides):
    base = {
        "candidate_id": "cand-1",
        "synthesis_id": "syn-1",
        "session_id": "sess-1",
        "vault_id": "core",
        "source": "session_synthesis",
        "title": "Durable concept",
        "definition": "A full concept definition for review.",
        "confidence": "low",
        "created_at": "2026-09-06T00:00:00+00:00",
        "provenance": {"extractor_model": "model", "source_notes": ["note.md"]},
        "extra": {"slug": "durable-concept"},
        "status": "pending_review",
        "accepted_count": 2,
        "context_only_count": 1,
        "safe_provenance": {
            "session_title": "A session",
            "created_at": "2026-09-06T00:00:00+00:00",
            "source_ref": "wiki/entities/foo.md",
            "concept_summary": "A concept summary.",
        },
    }
    base.update(overrides)
    return base


class SessionSynthesisCurateTests(unittest.TestCase):
    def setUp(self):
        self._env_backup = {
            "MISSION_CONTROL_TOKEN": os.environ.get("MISSION_CONTROL_TOKEN"),
            "MC_ENABLE_BDH_CURATOR": os.environ.get("MC_ENABLE_BDH_CURATOR"),
            "HERMES_HOME": os.environ.get("HERMES_HOME"),
        }
        os.environ["MISSION_CONTROL_TOKEN"] = "phase1-secret"
        os.environ["MC_ENABLE_BDH_CURATOR"] = "1"
        os.environ.pop("HERMES_HOME", None)

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

    def _request(self, path, token=None, method="GET", body=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method
        )
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        return urllib.request.urlopen(request, timeout=5)

    def test_candidates_requires_auth(self):
        with self.assertRaises(urllib.error.HTTPError) as exc:
            self._request("/api/local/synthesis/candidates")
        self.assertEqual(exc.exception.code, 401)

    def test_candidates_disabled_without_feature_gate(self):
        os.environ.pop("MC_ENABLE_BDH_CURATOR", None)
        with self.assertRaises(urllib.error.HTTPError) as exc:
            self._request("/api/local/synthesis/candidates", token="phase1-secret")
        self.assertEqual(exc.exception.code, 404)
        payload = json.loads(exc.exception.read().decode("utf-8"))
        self.assertEqual(payload["error"], "feature_disabled")

    def test_candidates_expose_review_safe_detail_fields_without_transcript(self):
        with mock.patch.object(
            local_telemetry_server, "load_synthesis_candidates", return_value={
                "vault_id": "core", "count": 1, "candidates": [_candidate()],
            }
        ):
            with self._request("/api/local/synthesis/candidates?vault=core", token="phase1-secret") as response:
                self.assertEqual(response.status, 200)
                payload = json.loads(response.read().decode("utf-8"))
        self.assertEqual(payload["count"], 1)
        candidate = payload["candidates"][0]
        self.assertEqual(candidate["candidate_id"], "cand-1")
        self.assertEqual(candidate["definition"], "A full concept definition for review.")
        self.assertEqual(candidate["confidence"], "low")
        self.assertEqual(candidate["provenance"]["extractor_model"], "model")
        self.assertEqual(candidate["extra"]["slug"], "durable-concept")
        self.assertNotIn("transcript_sha256", candidate)

    def test_apply_rejects_missing_candidate_id(self):
        with self.assertRaises(urllib.error.HTTPError) as exc:
            self._request("/api/local/synthesis/apply", token="phase1-secret", method="POST", body={})
        self.assertEqual(exc.exception.code, 400)

    def test_apply_unknown_candidate_is_404(self):
        with mock.patch.object(local_telemetry_server, "get_synthesis_candidate", return_value=None):
            with self.assertRaises(urllib.error.HTTPError) as exc:
                self._request(
                    "/api/local/synthesis/apply",
                    token="phase1-secret",
                    method="POST",
                    body={"candidate_id": "cand-1", "vault": "core"},
                )
        self.assertEqual(exc.exception.code, 404)

    def test_apply_forwards_bdh_correlation_tuple(self):
        candidate = _candidate()
        with mock.patch.object(local_telemetry_server, "get_synthesis_candidate", return_value=candidate), \
             mock.patch.object(
                 local_telemetry_server, "apply_synthesis_candidate",
                 return_value={"status": "created", "note_path": "wiki/concepts/foo.md", "operation_id": "op-9"},
             ) as apply_mock:
            with self._request(
                "/api/local/synthesis/apply",
                token="phase1-secret",
                method="POST",
                body={"candidate_id": "cand-1", "vault": "core"},
            ) as response:
                self.assertEqual(response.status, 200)
                payload = json.loads(response.read().decode("utf-8"))
        self.assertEqual(payload["status"], "created")
        apply_mock.assert_called_once_with(
            candidate_id="cand-1",
            synthesis_id="syn-1",
            session_id="sess-1",
            vault_id="core",
            source="session_synthesis",
        )

    def test_apply_conflict_is_not_marked_applied(self):
        candidate = _candidate()
        with mock.patch.object(local_telemetry_server, "get_synthesis_candidate", return_value=candidate), \
             mock.patch.object(
                 local_telemetry_server, "apply_synthesis_candidate",
                 return_value={"status": "conflict", "reason": "conflicts with existing note"},
             ):
            with self._request(
                "/api/local/synthesis/apply",
                token="phase1-secret",
                method="POST",
                body={"candidate_id": "cand-1", "vault": "core"},
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))
        self.assertEqual(payload["status"], "conflict")
        self.assertNotIn("applied", payload)

    def test_reject_records_locally_and_never_applies(self):
        candidate = _candidate()
        with mock.patch.object(local_telemetry_server, "get_synthesis_candidate", return_value=candidate), \
             mock.patch.object(
                 local_telemetry_server.session_synthesis_rejections, "record_rejection",
                 return_value={"candidate_id": "cand-1", "reason": "nope"},
             ) as record_mock, \
             mock.patch.object(local_telemetry_server, "apply_synthesis_candidate") as apply_mock:
            with self._request(
                "/api/local/synthesis/reject",
                token="phase1-secret",
                method="POST",
                body={"candidate_id": "cand-1", "reason": "nope", "vault": "core"},
            ) as response:
                self.assertEqual(response.status, 200)
                payload = json.loads(response.read().decode("utf-8"))
        self.assertTrue(payload["success"])
        self.assertEqual(payload["status"], "rejected")
        record_mock.assert_called_once()
        apply_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
