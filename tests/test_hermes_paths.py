"""Tests for server/hermes_paths.py — profile-aware Hermes home resolution.

Covers the precedence order documented in the module (issue #12):
HERMES_HOME profile-shaped → sticky active_profile → HERMES_HOME → default.
All tests run against an isolated temp root so they never touch the real
~/.hermes (no HERMES_HOME mutation leaks either — it is restored after each
test).
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

import sys

SERVER_DIR = Path(__file__).resolve().parent.parent / "server"
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import hermes_paths  # noqa: E402


class HermesPathsResolutionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="mc-hermes-paths-test-"))
        self._home_backup = os.environ.get("HOME")
        os.environ["HOME"] = str(self._tmp / "home")
        self._env_backup = os.environ.get("HERMES_HOME")
        os.environ.pop("HERMES_HOME", None)

    def tearDown(self):
        if self._home_backup is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self._home_backup
        if self._env_backup is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = self._env_backup
        import shutil

        shutil.rmtree(self._tmp, ignore_errors=True)

    def _write_active_profile(self, name: str) -> None:
        root = hermes_paths.hermes_root()
        root.mkdir(parents=True, exist_ok=True)
        (root / "active_profile").write_text(name + "\n", encoding="utf-8")

    def test_default_home_when_unset(self):
        self.assertEqual(
            hermes_paths.get_hermes_home(), Path.home() / ".hermes"
        )
        self.assertEqual(hermes_paths.get_active_profile(), "default")

    def test_hermes_home_env_is_honored(self):
        custom = self._tmp / "custom-home"
        custom.mkdir(parents=True, exist_ok=True)
        os.environ["HERMES_HOME"] = str(custom)
        self.assertEqual(hermes_paths.get_hermes_home(), custom)
        # A custom home outside the native layout is its own root, so the
        # profile is "default" — mirrors core get_active_profile_name().
        self.assertEqual(hermes_paths.get_active_profile(), "default")

    def test_profile_shaped_hermes_home_wins(self):
        profile_home = self._tmp / "root" / "profiles" / "coder"
        profile_home.mkdir(parents=True, exist_ok=True)
        os.environ["HERMES_HOME"] = str(profile_home)
        # A different sticky profile must NOT override an explicit profile home.
        self._write_active_profile("other")
        self.assertEqual(hermes_paths.get_hermes_home(), profile_home)
        self.assertEqual(hermes_paths.get_active_profile(), "coder")

    def test_active_profile_redirects_home(self):
        root = self._tmp / "root"
        root.mkdir(parents=True, exist_ok=True)
        os.environ["HERMES_HOME"] = str(root)
        self._write_active_profile("coder")
        expected = root / "profiles" / "coder"
        self.assertEqual(hermes_paths.get_hermes_home(), expected)
        self.assertEqual(hermes_paths.get_active_profile(), "coder")

    def test_active_profile_default_keeps_home(self):
        root = self._tmp / "root"
        root.mkdir(parents=True, exist_ok=True)
        os.environ["HERMES_HOME"] = str(root)
        self._write_active_profile("default")
        self.assertEqual(hermes_paths.get_hermes_home(), root)

    def test_hermes_root_in_profile_mode(self):
        root = self._tmp / "root"
        (root / "profiles" / "coder").mkdir(parents=True, exist_ok=True)
        os.environ["HERMES_HOME"] = str(root / "profiles" / "coder")
        self.assertEqual(hermes_paths.hermes_root(), root)

    def test_hermes_root_outside_native_home_is_custom(self):
        custom = self._tmp / "opt-data"
        custom.mkdir(parents=True, exist_ok=True)
        os.environ["HERMES_HOME"] = str(custom)
        self.assertEqual(hermes_paths.hermes_root(), custom)

    def test_state_convenience_paths_follow_home(self):
        root = self._tmp / "root"
        root.mkdir(parents=True, exist_ok=True)
        os.environ["HERMES_HOME"] = str(root)
        self.assertEqual(hermes_paths.hermes_state_db(), root / "state.db")
        self.assertEqual(hermes_paths.hermes_sessions_dir(), root / "sessions")
        self.assertEqual(hermes_paths.hermes_logs_dir(), root / "logs")
        self.assertEqual(hermes_paths.hermes_skills_dir(), root / "skills")
        self.assertEqual(hermes_paths.hermes_cache_dir(), root / "cache")
        self.assertEqual(hermes_paths.hermes_config_path(), root / "config.yaml")
        self.assertEqual(
            hermes_paths.hermes_core_dir(), root / "hermes-agent"
        )
        self.assertEqual(
            hermes_paths.hermes_vault_brain_dir(), root / "vault-brain"
        )

    def test_state_convenience_paths_follow_profile(self):
        root = self._tmp / "root"
        root.mkdir(parents=True, exist_ok=True)
        os.environ["HERMES_HOME"] = str(root)
        self._write_active_profile("coder")
        profile_home = root / "profiles" / "coder"
        self.assertEqual(hermes_paths.hermes_state_db(), profile_home / "state.db")
        self.assertEqual(
            hermes_paths.hermes_sessions_dir(), profile_home / "sessions"
        )

    def test_display_home_path(self):
        home = Path.home()
        self.assertEqual(hermes_paths.display_home_path(home / "SOUL.md"), "~/SOUL.md")
        self.assertEqual(hermes_paths.display_home_path(home), "~")
        outside = self._tmp / "outside" / "file.md"
        self.assertEqual(
            hermes_paths.display_home_path(outside), str(outside.resolve())
        )


if __name__ == "__main__":
    unittest.main()
