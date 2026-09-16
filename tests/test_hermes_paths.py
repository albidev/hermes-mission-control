"""Tests for server/hermes_paths.py — service-identity Hermes home resolution.

Covers the precedence order documented in the module:
HERMES_HOME (explicit, profile-shaped or not) → platform default.

The invariant under test: Mission Control is a SERVICE whose home is fixed at
the default root. Profiles enter only as an EXPLICIT scope (a profile-shaped
HERMES_HOME); the sticky ``active_profile`` file — the interactive CLI's "where
I am working now" marker — must never move a serving process's state DB, cron
store, vault-brain candidates or credentials.

The core makes the same distinction for the same reason
(``hermes_cli/main.py::_under_gateway_supervisor``).

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

    def test_sticky_active_profile_does_not_move_the_service_home(self):
        """REGRESSION: the sticky profile must not redirect a serving process.

        Before this invariant, a non-default ``active_profile`` made MC resolve
        ``<root>/profiles/<name>`` for EVERYTHING: state DB, sessions, cron,
        vault-brain candidates, auth.json. A single ``hermes profile use`` in a
        terminal would therefore have silently moved the whole service — and the
        rooms subsystem (which anchors to the root) would have been reading a
        different DB from the rest of MC.
        """
        root = self._tmp / "root"
        root.mkdir(parents=True, exist_ok=True)
        os.environ["HERMES_HOME"] = str(root)
        self._write_active_profile("coder")

        self.assertEqual(
            hermes_paths.get_hermes_home(),
            root,
            "the service home must stay the root, not follow the sticky profile",
        )
        self.assertEqual(hermes_paths.get_active_profile(), "default")
        self.assertEqual(hermes_paths.hermes_state_db(), root / "state.db")
        self.assertEqual(
            hermes_paths.hermes_vault_brain_dir(), root / "vault-brain"
        )

        # The marker is still readable for diagnostics — it just has no
        # authority over path resolution.
        self.assertEqual(hermes_paths.read_sticky_active_profile(), "coder")

    def test_sticky_profile_does_not_override_an_env_home(self):
        """The sticky file must not beat an explicit HERMES_HOME either."""
        custom = self._tmp / "custom-home"
        custom.mkdir(parents=True, exist_ok=True)
        os.environ["HERMES_HOME"] = str(custom)
        self._write_active_profile("coder")
        self.assertEqual(hermes_paths.get_hermes_home(), custom)

    def test_sticky_profile_absent_is_default(self):
        root = self._tmp / "root"
        root.mkdir(parents=True, exist_ok=True)
        os.environ["HERMES_HOME"] = str(root)
        self.assertEqual(hermes_paths.read_sticky_active_profile(), "default")
        self.assertEqual(hermes_paths.get_hermes_home(), root)

    def test_blank_and_corrupt_sticky_files_are_default(self):
        root = self._tmp / "root"
        root.mkdir(parents=True, exist_ok=True)
        os.environ["HERMES_HOME"] = str(root)

        (root / "active_profile").write_text("   \n", encoding="utf-8")
        self.assertEqual(hermes_paths.read_sticky_active_profile(), "default")
        self.assertEqual(hermes_paths.get_hermes_home(), root)

        # Unreadable as text: must degrade to default, never raise, and never
        # change the resolved home.
        (root / "active_profile").write_bytes(b"\xff\xfe\x00bad")
        self.assertEqual(hermes_paths.read_sticky_active_profile(), "default")
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

    def test_state_paths_follow_an_explicit_profile_scope(self):
        """Explicit scoping still works: it is requested, not inherited."""
        root = self._tmp / "root"
        profile_home = root / "profiles" / "coder"
        profile_home.mkdir(parents=True, exist_ok=True)
        os.environ["HERMES_HOME"] = str(profile_home)
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
