"""Regression tests for candidate promotion collision handling."""

from __future__ import annotations

import os
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import candidates as candidates_module  # noqa: E402


class CandidateMergeTests(unittest.TestCase):
    def test_merge_bodies_unions_metadata_and_preserves_incoming_description(self):
        existing = (
            "---\n"
            "tags: [a, b]\n"
            "sources: [src1]\n"
            "updated: 2026-09-19\n"
            "---\n\n"
            "Existing description.\n"
        )
        incoming = (
            "---\n"
            "tags: [b, c]\n"
            "sources: [src2]\n"
            "description: >\n"
            "  Incoming distinct description line.\n"
            "---\n"
        )

        merged = candidates_module._merge_bodies(existing, incoming, {"id": "candidate-1"})

        self.assertIn("tags: [a, b, c]", merged)
        self.assertIn("sources: [src1, src2]", merged)
        self.assertIn("Incoming distinct description line.", merged)
        self.assertIn("merged_from candidate: candidate-1", merged)

    def test_promote_ready_merges_complementary_collision(self):
        with tempfile.TemporaryDirectory(prefix="mc-candidate-merge-") as raw:
            root = Path(raw)
            candidates_dir = root / "candidates"
            vault_dir = root / "vault"
            candidates_dir.mkdir()
            concepts_dir = vault_dir / "wiki" / "concepts"
            concepts_dir.mkdir(parents=True)
            destination = concepts_dir / "test-merge.md"
            destination.write_text(
                "---\n"
                "tags: [existing]\n"
                "sources: [old]\n"
                "updated: 2026-09-19\n"
                "---\n\n"
                "Existing description.\n",
                encoding="utf-8",
            )
            quarantine_until = (
                datetime.now(timezone.utc) - timedelta(minutes=1)
            ).isoformat()
            (candidates_dir / "candidate.md").write_text(
                "---\n"
                "id: candidate-1\n"
                "status: approved\n"
                "quarantine_until: " + quarantine_until + "\n"
                "title: Test Merge\n"
                "jev_confidence: 0.95\n"
                "---\n\n"
                "---\n"
                "tags: [incoming]\n"
                "sources: [new]\n"
                "description: >\n"
                "  Incoming distinct description line.\n"
                "---\n",
                encoding="utf-8",
            )

            with patch.object(candidates_module, "_candidates_dir", return_value=candidates_dir), \
                 patch.object(candidates_module, "_load_vaults", return_value={}), \
                 patch.object(candidates_module, "hermes_vault_dir", return_value=vault_dir), \
                 patch.object(
                     candidates_module,
                     "_merge_check",
                     return_value={"action": "merge", "similarity": 0.95},
                 ):
                promoted = candidates_module.promote_ready()

            self.assertEqual(len(promoted), 1)
            self.assertEqual(promoted[0]["status"], "promoted")
            merged = destination.read_text(encoding="utf-8")
            self.assertIn("Incoming distinct description line.", merged)
            self.assertIn("merged_from candidate: candidate-1", merged)

    def test_report_without_valid_owner_requires_review_instead_of_guessing_a_project(self):
        for profile in (None, "../outside", ""):
            with self.subTest(profile=profile), tempfile.TemporaryDirectory(prefix="mc-report-routing-") as raw:
                root = Path(raw)
                candidates_dir = root / "candidates"
                candidates_dir.mkdir()
                vault_dir = root / "vault"
                quarantine_until = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
                candidate = candidates_dir / "cron-brief-run.md"
                profile_line = f"profile: {profile}\n" if profile is not None else ""
                candidate.write_text(
                    "---\n"
                    "id: cron-brief-run\n"
                    "type: cron-brief\n"
                    "status: approved\n"
                    f"quarantine_until: {quarantine_until}\n"
                    f"{profile_line}"
                    "---\n\nExample report.\n",
                    encoding="utf-8",
                )
                with patch.object(candidates_module, "_candidates_dir", return_value=candidates_dir), \
                     patch.object(candidates_module, "_load_vaults", return_value={}), \
                     patch.object(candidates_module, "hermes_vault_dir", return_value=vault_dir):
                    promoted = candidates_module.promote_ready()
                self.assertEqual(promoted, [])
                self.assertFalse((vault_dir / "projects").exists())
                reviewed = candidates_module._read_candidate(candidate)
                self.assertEqual(reviewed["status"], "needs_review")
                self.assertIn("profile", reviewed["routing_error"])

    def test_report_uses_explicit_profile_for_project_directory(self):
        with tempfile.TemporaryDirectory(prefix="mc-report-routing-") as raw:
            root = Path(raw)
            candidates_dir = root / "candidates"
            candidates_dir.mkdir()
            vault_dir = root / "vault"
            quarantine_until = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
            candidate = candidates_dir / "cron-brief-run.md"
            candidate.write_text(
                "---\n"
                "id: cron-brief-run\n"
                "type: cron-brief\n"
                "profile: example-delivery\n"
                "routing_error: missing or invalid profile for report routing\n"
                "status: approved\n"
                f"quarantine_until: {quarantine_until}\n"
                "---\n\nExample report.\n",
                encoding="utf-8",
            )
            with patch.object(candidates_module, "_candidates_dir", return_value=candidates_dir), \
                 patch.object(candidates_module, "_load_vaults", return_value={}), \
                 patch.object(candidates_module, "hermes_vault_dir", return_value=vault_dir):
                promoted = candidates_module.promote_ready()
            self.assertEqual(len(promoted), 1)
            self.assertEqual(promoted[0]["status"], "promoted")
            self.assertNotIn("routing_error", candidates_module._read_candidate(candidate))
            self.assertEqual((vault_dir / "projects" / "example" / "reports" / candidate.name).read_text(), "Example report.\n")

    def test_jev_provider_is_available_from_mc_server_checkout(self):
        server_dir = Path(candidates_module.__file__).resolve().parent
        with patch.dict(
            os.environ,
            {"JEV_BENCHMARK_DIR": "", "BDH_GRAPH_HARNESS_DIR": ""},
            clear=False,
        ):
            self.assertEqual(candidates_module._jev_benchmark_dir(), server_dir)

    def test_merge_check_uses_configured_harness_and_120_char_threshold(self):
        fake_provider_module = types.ModuleType("jev_benchmark")

        class FakeProvider:
            def evaluate(self, state, questions):
                return {
                    "answers": {
                        "mrg": {"choice": "complementary", "confidence": 0.95}
                    }
                }

        fake_provider_module.CloudTypeSafeProvider = FakeProvider
        with tempfile.TemporaryDirectory(prefix="mc-jev-harness-") as raw:
            harness_root = Path(raw)
            benchmark_dir = harness_root / "benchmarks"
            benchmark_dir.mkdir()
            (benchmark_dir / "jev_benchmark.py").write_text("# test provider\n", encoding="utf-8")
            destination = harness_root / "existing.md"
            destination.write_text("E" * 150, encoding="utf-8")

            with patch.dict(
                os.environ,
                {"BDH_GRAPH_HARNESS_DIR": str(harness_root)},
                clear=False,
            ), patch.dict(sys.modules, {"jev_benchmark": fake_provider_module}):
                result = candidates_module._merge_check(destination, "N" * 150)

        self.assertEqual(result["action"], "merge")
        self.assertEqual(candidates_module.MIN_MERGE_CHARS, 120)


if __name__ == "__main__":
    unittest.main()
