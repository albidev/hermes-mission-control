import json
import sys
import unittest
from pathlib import Path
from unittest import mock

SERVER_DIR = Path(__file__).resolve().parents[1] / "server"
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from synthesis_activity_proxy import (  # noqa: E402
    apply_synthesis_candidate,
    approve_synthesis_candidate,
    get_synthesis_candidate,
    load_synthesis_activity,
    load_synthesis_candidates,
    revert_synthesis,
)


class FakeResponse:
    status = 200

    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class SynthesisActivityProxyTests(unittest.TestCase):
    def test_load_activity_forwards_vault_scope(self):
        seen = []

        def fake_urlopen(request, timeout):
            seen.append((request.full_url, request.method, timeout))
            return FakeResponse({"vault_id": "core", "activities": [], "count": 0})

        with unittest.mock.patch("synthesis_activity_proxy.urllib.request.urlopen", fake_urlopen):
            result = load_synthesis_activity("crossnection")

        self.assertEqual(result["vault_id"], "core")
        self.assertEqual(
            seen,
            [("http://127.0.0.1:8643/api/synthesis-activity?vault_id=crossnection", "GET", 8)],
        )

    def test_revert_refreshes_graph_after_success(self):
        calls = []

        def fake_request(path, *, method="GET", payload=None):
            calls.append((path, method, payload))
            if path == "/api/synthesis/revert":
                return {"status": "reverted", "refresh_required": True}
            return {"status": "refreshed"}

        with unittest.mock.patch("synthesis_activity_proxy._request", fake_request):
            result = revert_synthesis("op-1", "core")

        self.assertEqual(result["status"], "reverted")
        self.assertEqual(result["graph_refresh"], {"status": "refreshed"})
        self.assertEqual(
            calls,
            [
                ("/api/synthesis/revert", "POST", {"operation_id": "op-1", "vault_id": "core"}),
                ("/api/refresh-graph", "POST", {"vault_id": "core"}),
            ],
        )

    def test_load_candidates_forwards_query_and_projects_safe_fields(self):
        seen = []

        def fake_request(path, *, method="GET", payload=None):
            seen.append((path, method, payload))
            return {
                "vault_id": "core",
                "count": 1,
                "candidates": [
                    {
                        "candidate_id": "cand-1",
                        "synthesis_id": "syn-1",
                        "session_id": "sess-1",
                        "vault_id": "core",
                        "transcript_sha256": "a" * 64,
                        "source": "session_synthesis",
                        "title": "Durable concept",
                        "definition": "A concept summary, not a transcript.",
                        "status": "pending_review",
                        "created_at": "2026-09-06T00:00:00+00:00",
                        "provenance": {
                            "session_title": "A session",
                            "source_ref": "wiki/entities/foo.md",
                            "raw_transcript": "SHOULD NEVER LEAK",
                        },
                    }
                ],
            }

        with unittest.mock.patch("synthesis_activity_proxy._request", fake_request):
            result = load_synthesis_candidates("core", "pending_review", "syn-1")

        self.assertEqual(
            seen,
            [("/api/synthesis/candidates?vault_id=core&status=pending_review&synthesis_id=syn-1", "GET", None)],
        )
        self.assertEqual(result["count"], 1)
        candidate = result["candidates"][0]
        self.assertEqual(candidate["candidate_id"], "cand-1")
        self.assertEqual(candidate["synthesis_id"], "syn-1")
        self.assertEqual(candidate["session_id"], "sess-1")
        self.assertEqual(candidate["vault_id"], "core")
        self.assertEqual(candidate["title"], "Durable concept")
        self.assertEqual(candidate["status"], "pending_review")
        self.assertEqual(candidate["safe_provenance"]["session_title"], "A session")
        self.assertEqual(candidate["safe_provenance"]["source_ref"], "wiki/entities/foo.md")
        self.assertEqual(candidate["safe_provenance"]["concept_summary"], "A concept summary, not a transcript.")
        self.assertEqual(candidate["definition"], "A concept summary, not a transcript.")
        self.assertEqual(candidate["provenance"]["source_ref"], "wiki/entities/foo.md")
        # Transcript/hash and raw provenance fields must never leak.
        self.assertNotIn("transcript_sha256", candidate)
        self.assertNotIn("raw_transcript", candidate["provenance"])
        self.assertNotIn("raw_transcript", candidate["safe_provenance"])

    def test_load_candidates_hides_locally_rejected_ids(self):
        payload = {
            "vault_id": "core",
            "candidates": [
                {"candidate_id": "cand-kept", "vault_id": "core", "title": "Kept", "definition": "D"},
                {"candidate_id": "cand-rejected", "vault_id": "core", "title": "Rejected", "definition": "D"},
            ],
        }
        with mock.patch("synthesis_activity_proxy._request", return_value=payload), \
             mock.patch(
                 "synthesis_activity_proxy.session_synthesis_rejections.list_rejections",
                 return_value=[{"candidate_id": "cand-rejected", "vault_id": "core"}],
             ):
            result = load_synthesis_candidates("core")
        self.assertEqual(result["count"], 1)
        self.assertEqual([c["candidate_id"] for c in result["candidates"]], ["cand-kept"])

    def test_approve_forwards_correlation_tuple(self):
        seen = []

        def fake_request(path, *, method="GET", payload=None):
            seen.append((path, method, payload))
            return {"status": "approved", "candidate_id": "cand-1"}

        with mock.patch("synthesis_activity_proxy._request", fake_request):
            result = approve_synthesis_candidate("cand-1", "syn-1", "sess-1", "core")

        self.assertEqual(result["status"], "approved")
        self.assertEqual(seen, [("/api/synthesis/approve", "POST", {
            "candidate_id": "cand-1", "synthesis_id": "syn-1", "session_id": "sess-1",
            "vault_id": "core", "source": "session_synthesis",
        })])

    def test_apply_forwards_correlation_tuple(self):
        seen = []

        def fake_request(path, *, method="GET", payload=None):
            seen.append((path, method, payload))
            return {"status": "created", "note_path": "wiki/concepts/foo.md", "operation_id": "op-9"}

        with unittest.mock.patch("synthesis_activity_proxy._request", fake_request):
            result = apply_synthesis_candidate("cand-1", "syn-1", "sess-1", "core")

        self.assertEqual(result["status"], "created")
        self.assertEqual(
            seen,
            [
                (
                    "/api/synthesis/apply",
                    "POST",
                    {
                        "candidate_id": "cand-1",
                        "synthesis_id": "syn-1",
                        "session_id": "sess-1",
                        "vault_id": "core",
                        "source": "session_synthesis",
                    },
                )
            ],
        )

    def test_get_candidate_resolves_within_vault(self):
        def fake_load(vault_id=None, status=None, synthesis_id=None):
            return {
                "vault_id": vault_id,
                "count": 1,
                "candidates": [
                    {
                        "candidate_id": "cand-1",
                        "synthesis_id": "syn-1",
                        "session_id": "sess-1",
                        "vault_id": vault_id,
                        "source": "session_synthesis",
                        "title": "T",
                        "status": "pending_review",
                    }
                ],
            }

        with unittest.mock.patch("synthesis_activity_proxy.load_synthesis_candidates", fake_load):
            self.assertEqual(get_synthesis_candidate("cand-1", "core")["candidate_id"], "cand-1")
            self.assertIsNone(get_synthesis_candidate("missing", "core"))


if __name__ == "__main__":
    unittest.main()
