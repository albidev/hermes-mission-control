import importlib.util
import tempfile
import types
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "server" / "cron_bridge.py"
SPEC = importlib.util.spec_from_file_location("mission_control_cron_bridge", MODULE_PATH)
cron_bridge = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(cron_bridge)


class CronBridgeTests(unittest.TestCase):
    def setUp(self):
        self.jobs = [
            {
                "id": "job-123",
                "name": "nightly",
                "prompt": "Do the nightly work.",
                "schedule": {"kind": "cron", "expr": "0 2 * * *", "display": "0 2 * * *"},
                "schedule_display": "0 2 * * *",
                "enabled": True,
                "state": "scheduled",
                "next_run_at": "2099-01-01T02:00:00+00:00",
                "last_run_at": "2098-12-31T02:00:00+00:00",
                "last_status": "ok",
                "skills": ["nightly-skill"],
                "no_agent": False,
                "script": None,
            }
        ]
        self.output_dir = tempfile.TemporaryDirectory()
        self.core = types.SimpleNamespace(
            list_jobs=lambda include_disabled=False: list(self.jobs),
            create_job=self._create_job,
            update_job=self._update_job,
            pause_job=self._pause_job,
            resume_job=self._resume_job,
            trigger_job=self._trigger_job,
            remove_job=self._remove_job,
            get_cron_output_dir=lambda: Path(self.output_dir.name),
        )
        cron_bridge._core_module = self.core

    def tearDown(self):
        cron_bridge._core_module = None
        self.output_dir.cleanup()

    def _create_job(self, **kwargs):
        return {"id": "created-1", **kwargs}

    def _update_job(self, job_id, updates):
        return {"id": job_id, **updates}

    def _pause_job(self, job_id, reason=None):
        return {"id": job_id, "state": "paused", "paused_reason": reason}

    def _resume_job(self, job_id):
        return {"id": job_id, "state": "scheduled", "enabled": True}

    def _trigger_job(self, job_id):
        return {"id": job_id, "next_run_at": "2099-01-01T00:00:00+00:00"}

    def _remove_job(self, job_id):
        return True

    def test_list_jobs_enriches_job_with_latest_output(self):
        output = Path(self.output_dir.name) / "job-123_20981231_020000.txt"
        output.write_text("latest cron output\n", encoding="utf-8")

        result = cron_bridge.list_jobs()

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["id"], "job-123")
        self.assertEqual(result[0]["last_output"], "latest cron output\n")
        self.assertEqual(result[0]["latest_execution"]["status"], "ok")

    def test_create_job_only_forwards_supported_fields(self):
        result = cron_bridge.create_job(
            {
                "name": "new job",
                "prompt": "Run it",
                "schedule": "every 2h",
                "repeat": 3,
                "no_agent": True,
                "unknown": "must not leak",
            }
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["job"]["id"], "created-1")
        self.assertEqual(result["job"]["name"], "new job")
        self.assertEqual(result["job"]["repeat"], 3)
        self.assertTrue(result["job"]["no_agent"])
        self.assertNotIn("unknown", result["job"])

    def test_missing_job_raises_not_found_error(self):
        with self.assertRaises(cron_bridge.CronBridgeError) as context:
            cron_bridge.get_job("missing")
        self.assertEqual(context.exception.status_code, 404)

    def test_run_job_returns_triggered_job(self):
        result = cron_bridge.run_job("job-123")
        self.assertTrue(result["success"])
        self.assertEqual(result["job"]["id"], "job-123")


if __name__ == "__main__":
    unittest.main()
