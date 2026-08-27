"""Static deployment contracts for the Linux systemd stack."""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SYSTEMD = ROOT / "systemd"
SCRIPTS = ROOT / "scripts"


class LinuxDeploymentContractTests(unittest.TestCase):
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
