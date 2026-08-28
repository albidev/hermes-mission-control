"""Static deployment contracts for the Linux systemd stack."""

from pathlib import Path
import os
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SYSTEMD = ROOT / "systemd"
SCRIPTS = ROOT / "scripts"


class LinuxDeploymentContractTests(unittest.TestCase):
    def test_dashboard_api_script_defaults_and_forwards_configuration(self):
        script = ROOT / "scripts" / "run-dashboard-api.sh"
        with tempfile.TemporaryDirectory(prefix="mc-dashboard-script-") as tmp:
            core = Path(tmp) / "core"
            python_bin = core / "venv" / "bin" / "python"
            python_bin.parent.mkdir(parents=True)
            capture = Path(tmp) / "args"
            python_bin.write_text(
                "#!/usr/bin/env bash\nprintf '%s\\n' \"$@\" > \"$CAPTURE_FILE\"\n",
                encoding="utf-8",
            )
            python_bin.chmod(0o755)

            base_env = {
                **os.environ,
                "HOME": tmp,
                "HERMES_AGENT_DIR": str(core),
                "CAPTURE_FILE": str(capture),
                "MISSION_CONTROL_ENV_FILE": str(Path(tmp) / "missing.env"),
            }
            for key in (
                "MISSION_CONTROL_DASHBOARD_HOST",
                "MISSION_CONTROL_DASHBOARD_PORT",
            ):
                base_env.pop(key, None)

            subprocess.run([str(script)], env=base_env, check=True, capture_output=True, text=True)
            self.assertEqual(
                capture.read_text(encoding="utf-8").splitlines(),
                ["-m", "hermes_cli.main", "dashboard", "--host", "127.0.0.1", "--port", "9119", "--no-open"],
            )

            override_env = {
                **base_env,
                "MISSION_CONTROL_DASHBOARD_HOST": "127.0.0.2",
                "MISSION_CONTROL_DASHBOARD_PORT": "6002",
            }
            subprocess.run([str(script)], env=override_env, check=True, capture_output=True, text=True)
            self.assertEqual(
                capture.read_text(encoding="utf-8").splitlines(),
                ["-m", "hermes_cli.main", "dashboard", "--host", "127.0.0.2", "--port", "6002", "--no-open"],
            )

    def test_dashboard_api_script_uses_defaults_for_empty_values(self):
        script = ROOT / "scripts" / "run-dashboard-api.sh"
        with tempfile.TemporaryDirectory(prefix="mc-dashboard-empty-") as tmp:
            core = Path(tmp) / "core"
            python_bin = core / "venv" / "bin" / "python"
            python_bin.parent.mkdir(parents=True)
            capture = Path(tmp) / "args"
            python_bin.write_text(
                "#!/usr/bin/env bash\nprintf '%s\\n' \"$@\" > \"$CAPTURE_FILE\"\n",
                encoding="utf-8",
            )
            python_bin.chmod(0o755)
            env = {
                **os.environ,
                "HOME": tmp,
                "HERMES_AGENT_DIR": str(core),
                "CAPTURE_FILE": str(capture),
                "MISSION_CONTROL_ENV_FILE": str(Path(tmp) / "missing.env"),
                "MISSION_CONTROL_DASHBOARD_HOST": "",
                "MISSION_CONTROL_DASHBOARD_PORT": "",
            }
            subprocess.run([str(script)], env=env, check=True, capture_output=True, text=True)
            self.assertEqual(
                capture.read_text(encoding="utf-8").splitlines()[4:8],
                ["127.0.0.1", "--port", "9119", "--no-open"],
            )

    def test_dashboard_api_script_rejects_invalid_port(self):
        script = ROOT / "scripts" / "run-dashboard-api.sh"
        with tempfile.TemporaryDirectory(prefix="mc-dashboard-invalid-") as tmp:
            core = Path(tmp) / "core"
            (core / "venv" / "bin").mkdir(parents=True)
            env = {
                **os.environ,
                "HOME": tmp,
                "HERMES_AGENT_DIR": str(core),
                "MISSION_CONTROL_ENV_FILE": str(Path(tmp) / "missing.env"),
                "MISSION_CONTROL_DASHBOARD_PORT": "not-a-port",
            }
            result = subprocess.run([str(script)], env=env, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("invalid dashboard port", result.stderr)

    def test_units_use_external_env_file_and_portable_commands(self):
        for name in (
            "hermes-dashboard-api.service",
            "hermes-mission-control-telemetry.service",
            "hermes-mission-control.service",
        ):
            text = (SYSTEMD / name).read_text(encoding="utf-8")
            self.assertIn("EnvironmentFile=-%h/.hermes/mission-control.env", text)
            self.assertNotIn("EnvironmentFile=-%h/Projects/hermes-mission-control/.env", text)

        dashboard = (SYSTEMD / "hermes-dashboard-api.service").read_text(encoding="utf-8")
        telemetry = (SYSTEMD / "hermes-mission-control-telemetry.service").read_text(encoding="utf-8")
        frontend = (SYSTEMD / "hermes-mission-control.service").read_text(encoding="utf-8")
        self.assertIn("scripts/run-dashboard-api.sh", dashboard)
        self.assertIn("scripts/run-local-telemetry.sh", telemetry)
        self.assertIn("pnpm exec vite", frontend)

    def test_dashboard_port_contract_is_shared_with_vite_and_deployment_docs(self):
        vite = (ROOT / "vite.config.ts").read_text(encoding="utf-8")
        env_template = (ROOT / "deploy" / "systemd" / "env.template").read_text(encoding="utf-8")
        systemd_readme = (SYSTEMD / "README.md").read_text(encoding="utf-8")
        self.assertIn("MISSION_CONTROL_DASHBOARD_HOST", vite)
        self.assertIn("MISSION_CONTROL_DASHBOARD_PORT", vite)
        self.assertIn("HERMES_DASHBOARD_URL", vite)
        self.assertIn("const DASHBOARD_TARGET", vite)
        self.assertEqual(vite.count("target: DASHBOARD_TARGET"), 2)
        self.assertIn("MISSION_CONTROL_DASHBOARD_HOST", env_template)
        self.assertIn("MISSION_CONTROL_DASHBOARD_PORT", env_template)
        self.assertIn("HERMES_DASHBOARD_URL", env_template)
        self.assertIn("MISSION_CONTROL_DASHBOARD_PORT", systemd_readme)
        self.assertIn("HERMES_DASHBOARD_URL", systemd_readme)

    def test_authenticated_health_checks_do_not_put_token_in_argv(self):
        for name in (
            "check-mission-control-health.sh",
            "smoke-test-telemetry.sh",
            "smoke-upgrade.sh",
        ):
            text = (SCRIPTS / name).read_text(encoding="utf-8")
            self.assertNotIn('Bearer $TOKEN"', text)
            self.assertNotIn('Bearer ${TOKEN}"', text)
            self.assertNotIn("Bearer ***", text)
            self.assertIn("--config -", text)

    def test_env_loader_defaults_outside_repository(self):
        text = (SCRIPTS / "lib" / "env.sh").read_text(encoding="utf-8")
        self.assertIn('default_env="$HOME/.hermes/mission-control.env"', text)
        self.assertNotIn('default_env="$(mc_repo_root)/.env"', text)


if __name__ == "__main__":
    unittest.main()
