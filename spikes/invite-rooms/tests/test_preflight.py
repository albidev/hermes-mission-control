import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import preflight


class PreflightBehaviorTests(unittest.TestCase):
    def setUp(self):
        preflight.APPROVED_RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
        self.home = Path(tempfile.mkdtemp(prefix="case-", dir=preflight.APPROVED_RUNTIME_ROOT))
        # The implementation owns these children; remove setup contents so writes are observable.
        shutil.rmtree(self.home)

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)
        shutil.rmtree(preflight.SPIKE_ROOT / "outside-sentinel", ignore_errors=True)
        escape = preflight.APPROVED_RUNTIME_ROOT / "escape"
        if escape.is_symlink():
            escape.unlink()

    def test_exact_containment_rejects_production_traversal_sibling_and_symlink_without_writes(self):
        outside = preflight.SPIKE_ROOT / "outside-sentinel"
        outside.mkdir(parents=True, exist_ok=True)
        sentinel = outside / "sentinel"
        sentinel.write_text("unchanged", encoding="utf-8")
        sibling = preflight.SPIKE_ROOT / "preflight-homes-recovery-sibling" / "new"
        escape_link = preflight.APPROVED_RUNTIME_ROOT / "escape"
        escape_link.symlink_to(outside, target_is_directory=True)
        cases = [
            Path("/Users/albi"),
            sibling,
            preflight.APPROVED_RUNTIME_ROOT / ".." / "escape-by-traversal",
            escape_link / "new-home",
        ]
        for candidate in cases:
            with self.subTest(candidate=candidate), self.assertRaises(preflight.PreflightBlocked):
                preflight.validate_scoped_home(candidate)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "unchanged")
        self.assertFalse((outside / "new-home").exists())

    def test_child_environment_is_exact_and_restored(self):
        before = dict(os.environ)
        self.home.mkdir()
        (self.home / "runtime").mkdir()
        (self.home / "tmp").mkdir()
        with patch.dict(os.environ, {
            "HOME": "/Users/albi", "PYTHONPATH": "/poison", "ROOM_CONTEXT": "room-secret",
            "PREFLIGHT_CONTEXT": "preflight-secret", "API_SERVER_KEY": "secret",
            "PATH": "/usr/bin", "LANG": "C.UTF-8",
        }, clear=False):
            env = preflight.isolated_child_env(self.home)
            self.assertEqual(env["HOME"], str(self.home))
            self.assertEqual(env["HERMES_HOME"], str(self.home))
            self.assertEqual(env["HERMES_RUNTIME_DIR"], str(self.home / "runtime"))
            self.assertEqual(env["TMPDIR"], str(self.home / "tmp"))
            self.assertNotIn("PYTHONPATH", env)
            self.assertNotIn("ROOM_CONTEXT", env)
            self.assertNotIn("PREFLIGHT_CONTEXT", env)
            self.assertNotIn("API_SERVER_KEY", env)
        self.assertEqual(dict(os.environ), before)

    def test_native_identity_is_real_distinct_and_unknown_blocks(self):
        self.home.mkdir()
        (self.home / "runtime").mkdir()
        (self.home / "tmp").mkdir()
        first = preflight.native_identity(self.home)
        second = self.home / "second"
        second.mkdir()
        (second / "runtime").mkdir()
        (second / "tmp").mkdir()
        identity2 = preflight.native_identity(second)
        self.assertTrue(first["gateway_id"].startswith("install:"))
        self.assertNotEqual(first["gateway_id"], identity2["gateway_id"])
        with patch.object(preflight, "_native_probe", return_value={}):
            with self.assertRaises(preflight.PreflightBlocked):
                preflight.native_identity(self.home)

    def test_effective_policy_is_read_from_config_and_empty_config_blocks(self):
        self.home.mkdir()
        (self.home / "runtime").mkdir()
        (self.home / "tmp").mkdir()
        (self.home / "config.yaml").write_text("\n", encoding="utf-8")
        with self.assertRaises(preflight.PreflightBlocked):
            preflight.effective_native_policy(self.home)
        preflight._write_safe_config(self.home)
        policy = preflight.effective_native_policy(self.home)
        self.assertEqual(policy["approval_mode"], "manual")
        self.assertEqual(policy["enabled_toolsets"], ["bot_room"])
        self.assertEqual(policy["max_iterations"], 1)

    def test_port_availability_is_not_readiness_or_reservation(self):
        result = preflight.check_port_available(0)
        self.assertTrue(result["available"])
        self.assertFalse(result["reserved"])
        self.assertFalse(result["capability_ready"])

    def test_cli_is_sanitized_and_does_not_launch(self):
        preflight.APPROVED_RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            [str(preflight.MANAGED_PYTHON), str(HERE / "preflight.py"), "--home", str(self.home)],
            capture_output=True, text=True, check=False, timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["preflight_ok"])
        self.assertFalse(any(payload["execution"].values()))
        self.assertNotIn("PYTHONPATH", payload["env_keys"])
        self.assertEqual(payload["effective_policy"]["enabled_toolsets"], ["bot_room"])

    def test_native_unknown_and_malformed_identity_fail_in_real_child(self):
        self.home.mkdir()
        (self.home / "runtime").mkdir()
        (self.home / "tmp").mkdir()
        (self.home / "install_id").mkdir()
        with self.assertRaises(preflight.PreflightBlocked):
            preflight.native_identity(self.home)

    def test_missing_managed_interpreter_blocks(self):
        self.home.mkdir()
        (self.home / "runtime").mkdir()
        (self.home / "tmp").mkdir()
        with patch.object(preflight, "MANAGED_PYTHON", self.home / "missing-python"):
            with self.assertRaises(preflight.PreflightBlocked):
                preflight.native_identity(self.home)

    def test_malformed_native_policy_payload_blocks_at_boundary(self):
        self.home.mkdir()
        (self.home / "runtime").mkdir()
        (self.home / "tmp").mkdir()
        preflight._write_safe_config(self.home)
        malformed = {
            "approval_mode": "manual", "enabled_toolsets": "bot_room",
            "max_iterations": "1", "policy_digest": "not-a-digest",
            "target_profile": "default", "version": 1,
        }
        with patch.object(preflight, "_native_probe", return_value=malformed):
            with self.assertRaises(preflight.PreflightBlocked):
                preflight.effective_native_policy(self.home)

    def test_subprocess_probe_cannot_inherit_secret_context_or_fallback_keys(self):
        self.home.mkdir()
        (self.home / "runtime").mkdir()
        (self.home / "tmp").mkdir()
        before = dict(os.environ)
        poison = {
            "PYTHONPATH": "/poison", "ROOM_CONTEXT": "secret", "PREFLIGHT_CONTEXT": "secret",
            "OPENROUTER_API_KEY": "secret", "HONCHO_HOME": "/secret", "HERMES_CONTEXT": "secret",
            "FALLBACK_ROUTE": "secret",
        }
        with patch.dict(os.environ, poison, clear=False):
            env = preflight.isolated_child_env(self.home)
            probe = subprocess.run(
                [str(preflight.MANAGED_PYTHON), "-c", "import json, os; print(json.dumps(dict(os.environ)))"],
                cwd=str(preflight.HERMES_AGENT_ROOT), env=env, capture_output=True, text=True, check=True,
            )
            child = json.loads(probe.stdout)
            for key in poison:
                self.assertNotIn(key, child)
        self.assertEqual(dict(os.environ), before)

    def test_unavailable_requested_port_blocks_preflight(self):
        sock = __import__("socket").socket()
        sock.bind(("127.0.0.1", 0))
        occupied = sock.getsockname()[1]
        try:
            with self.assertRaises(preflight.PreflightBlocked):
                preflight.run_preflight(self.home, port=occupied)
            self.assertFalse(self.home.exists())
        finally:
            sock.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
