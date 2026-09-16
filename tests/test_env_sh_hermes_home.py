"""The shell resolver must agree with server/hermes_paths.get_hermes_home.

Mission Control has TWO resolvers for the same decision: the Python module used
by the sidecar/telemetry server, and ``scripts/lib/env.sh`` used by the launchd
wrapper that starts it. They drifted once already — the shell copy kept an older
precedence order, so fixing only the Python side would have left the wrapper free
to launch the same process against a different home.

The invariant, identical on both sides: MC is a SERVICE with one identity. A
profile is an explicit scope (a profile-shaped ``HERMES_HOME``), never ambient
state inherited from the interactive CLI's sticky ``active_profile`` marker.

These tests drive the real shell function (``bash -c``), not a paraphrase of it,
so a future edit that re-introduces the sticky branch fails here.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_SH = REPO_ROOT / "scripts" / "lib" / "env.sh"


def _resolve(home: Path, hermes_home: str | None) -> str:
    """Call the real ``resolve_hermes_home`` from scripts/lib/env.sh."""
    script = f'source "{ENV_SH}"\nresolve_hermes_home'
    env = {**os.environ, "HOME": str(home)}
    env.pop("HERMES_HOME", None)
    if hermes_home is not None:
        env["HERMES_HOME"] = hermes_home
    # env.sh is `set -euo pipefail` aware; sourcing it must not need anything
    # beyond HOME, so an unset HERMES_HOME is a state we exercise on purpose.
    result = subprocess.run(
        ["/bin/bash", "-c", script],
        capture_output=True,
        text=True,
        env=env,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"resolve_hermes_home failed ({result.returncode}): {result.stderr.strip()}"
        )
    return result.stdout.strip()


class ShellHomeResolutionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="mc-envsh-test-"))
        self._home = self._tmp / "home"
        (self._home / ".hermes" / "profiles" / "coder").mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _write_active_profile(self, name: str) -> None:
        (self._home / ".hermes" / "active_profile").write_text(
            name + "\n", encoding="utf-8"
        )

    def test_default_home_when_unset(self):
        self.assertEqual(_resolve(self._home, None), str(self._home / ".hermes"))

    def test_sticky_active_profile_does_not_move_the_service_home(self):
        """REGRESSION: the shell resolver must not follow active_profile either.

        Before this, the launchd wrapper could start the telemetry server against
        ``<root>/profiles/<sticky>`` while the Python module resolved the root —
        two resolvers, one decision, disagreeing.
        """
        self._write_active_profile("coder")
        self.assertEqual(
            _resolve(self._home, None),
            str(self._home / ".hermes"),
            "the shell resolver must stay on the root, not follow the sticky profile",
        )

    def test_sticky_profile_does_not_beat_an_explicit_env_home(self):
        self._write_active_profile("coder")
        custom = self._tmp / "custom-home"
        self.assertEqual(_resolve(self._home, str(custom)), str(custom))

    def test_profile_shaped_hermes_home_still_scopes(self):
        """An explicit profile request is honoured — that is the supported path."""
        profile_home = self._home / ".hermes" / "profiles" / "coder"
        self.assertEqual(_resolve(self._home, str(profile_home)), str(profile_home))

    def test_blank_active_profile_is_harmless(self):
        (self._home / ".hermes" / "active_profile").write_text("  \n", encoding="utf-8")
        self.assertEqual(_resolve(self._home, None), str(self._home / ".hermes"))

    def test_resolution_matches_the_python_module(self):
        """Both resolvers must agree, with and without a sticky profile.

        This is the contract that actually matters: one decision, two
        implementations, never two answers.
        """
        import sys

        server_dir = REPO_ROOT / "server"
        if str(server_dir) not in sys.path:
            sys.path.insert(0, str(server_dir))
        import hermes_paths  # noqa: E402

        for sticky in (None, "coder"):
            if sticky:
                self._write_active_profile(sticky)
            for explicit in (None, str(self._home / ".hermes"), str(self._home / ".hermes" / "profiles" / "coder")):
                shell_answer = _resolve(self._home, explicit)
                previous_home = os.environ.get("HOME")
                previous_env = os.environ.get("HERMES_HOME")
                try:
                    os.environ["HOME"] = str(self._home)
                    if explicit is None:
                        os.environ.pop("HERMES_HOME", None)
                    else:
                        os.environ["HERMES_HOME"] = explicit
                    python_answer = str(hermes_paths.get_hermes_home())
                finally:
                    if previous_home is None:
                        os.environ.pop("HOME", None)
                    else:
                        os.environ["HOME"] = previous_home
                    if previous_env is None:
                        os.environ.pop("HERMES_HOME", None)
                    else:
                        os.environ["HERMES_HOME"] = previous_env

                self.assertEqual(
                    shell_answer,
                    python_answer,
                    f"resolvers disagree (sticky={sticky!r}, HERMES_HOME={explicit!r})",
                )


if __name__ == "__main__":
    unittest.main()
