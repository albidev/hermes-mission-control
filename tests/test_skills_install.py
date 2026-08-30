import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


fake_psutil = types.SimpleNamespace(
    cpu_percent=lambda interval=None: 7.5,
    virtual_memory=lambda: types.SimpleNamespace(percent=42.0, used=8 * 1024**3, available=8 * 1024**3, total=16 * 1024**3),
    disk_usage=lambda path: types.SimpleNamespace(percent=55.0, free=100 * 1024**3, total=200 * 1024**3),
    Process=lambda: types.SimpleNamespace(memory_info=lambda: types.SimpleNamespace(rss=256 * 1024**2)),
)
sys.modules.setdefault("psutil", fake_psutil)  # type: ignore[arg-type]

MODULE_PATH = Path(__file__).resolve().parents[1] / "server" / "local_telemetry_server.py"
SPEC = importlib.util.spec_from_file_location("mission_control_skills_install_server", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("Unable to load local telemetry server module")
local_telemetry_server = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(local_telemetry_server)


class SkillsInstallTests(unittest.TestCase):
    def test_identifier_must_be_an_exact_catalog_entry(self):
        with patch.object(local_telemetry_server, "_collect_skills_catalog", return_value={"available": True, "skills": []}):
            status, payload = local_telemetry_server._install_catalog_skill("official/devops/not-in-catalog")

        self.assertEqual(status, 404)
        self.assertEqual(payload["error"], "skill_not_in_catalog")

    def test_already_installed_entry_is_not_reinstalled(self):
        catalog = {
            "available": True,
            "skills": [{"identifier": "official/devops/already-there", "name": "already-there", "installed": True}],
        }
        with patch.object(local_telemetry_server, "_collect_skills_catalog", return_value=catalog), patch.object(
            local_telemetry_server.subprocess, "run"
        ) as run:
            status, payload = local_telemetry_server._install_catalog_skill("official/devops/already-there")

        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "already_installed")
        run.assert_not_called()

    def test_install_uses_argv_and_verifies_installed_skill(self):
        catalog = {
            "available": True,
            "skills": [{"identifier": "official/devops/new-skill", "name": "new-skill", "installed": False}],
        }
        completed = types.SimpleNamespace(returncode=0, stdout="installed", stderr="")
        with patch.object(local_telemetry_server, "_collect_skills_catalog", return_value=catalog), patch.object(
            local_telemetry_server, "_resolve_hermes_cli", return_value=Path("/usr/local/bin/hermes")
        ), patch.object(local_telemetry_server, "_get_hermes_home", return_value=Path("/tmp/hermes-test")), patch.object(
            local_telemetry_server.subprocess, "run", return_value=completed
        ) as run, patch.object(
            local_telemetry_server, "_collect_skills_uncached", return_value={"skills": [{"name": "new-skill"}]}
        ):
            status, payload = local_telemetry_server._install_catalog_skill("official/devops/new-skill")

        self.assertEqual(status, 200)
        self.assertTrue(payload["success"])
        self.assertTrue(payload["verified"])
        args, kwargs = run.call_args
        self.assertEqual(args[0], ["/usr/local/bin/hermes", "skills", "install", "official/devops/new-skill", "--yes"])
        self.assertNotIn("shell", kwargs)
        self.assertEqual(kwargs["cwd"], "/tmp/hermes-test")

    def test_knowledge_walker_skips_generated_trees_and_vanishing_directories(self):
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            (root / "stable.md").write_text("# Stable", encoding="utf-8")
            (root / "target").mkdir()
            (root / "target" / "generated.md").write_text("# Generated", encoding="utf-8")
            (root / "volatile").mkdir()

            real_scandir = os.scandir

            def flaky_scandir(path):
                if Path(path).name == "volatile":
                    raise FileNotFoundError(path)
                return real_scandir(path)

            with patch.object(local_telemetry_server.os, "scandir", side_effect=flaky_scandir):
                found = local_telemetry_server._find_knowledge_markdown_files(root)

            self.assertEqual([path.name for path in found], ["stable.md"])

    def test_hermes_cli_resolution_falls_back_outside_launchagent_path(self):
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as raw_root:
            home = Path(raw_root)
            cli = home / "hermes-agent" / "venv" / "bin" / "hermes"
            cli.parent.mkdir(parents=True)
            cli.write_text("#!/bin/sh\n", encoding="utf-8")
            cli.chmod(0o755)
            with patch.object(local_telemetry_server.shutil, "which", return_value=None), patch.object(
                local_telemetry_server, "_get_hermes_home", return_value=home
            ):
                self.assertEqual(local_telemetry_server._resolve_hermes_cli(), cli.resolve())


if __name__ == "__main__":
    unittest.main()
