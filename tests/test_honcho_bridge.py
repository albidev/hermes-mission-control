from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parents[1] / "server"
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import honcho_bridge


class HonchoBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._home = tempfile.TemporaryDirectory(prefix="mc-honcho-")
        self.root = Path(self._home.name) / ".hermes"
        self.root.mkdir(parents=True)
        self._previous_home = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = str(self.root)

    def tearDown(self) -> None:
        if self._previous_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = self._previous_home
        self._home.cleanup()

    def _write_honcho(self, payload: dict) -> Path:
        path = self.root / "honcho.json"
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return path

    def _profile(self, name: str) -> Path:
        profile = self.root / "profiles" / name
        profile.mkdir(parents=True)
        (profile / "profile.yaml").write_text(f"name: {name}\n", encoding="utf-8")
        return profile

    def test_status_reports_unresolved_local_identity_without_leaking_credentials(self) -> None:
        self._profile("researcher")
        (self.root / "config.yaml").write_text("memory:\n  provider: honcho\n", encoding="utf-8")
        self._write_honcho({
            "baseUrl": "http://127.0.0.1:8000",
            "apiKey": "super-secret",
            "enabled": True,
            "workspace": "team-memory",
            "aiPeer": "hermes",
        })

        status = honcho_bridge.load_honcho_status()

        self.assertTrue(status["configured"])
        self.assertTrue(status["enabled"])
        self.assertTrue(status["providerActive"])
        self.assertEqual(status["identityMode"], "unresolved")
        self.assertFalse(status["identityReady"])
        self.assertTrue(status["authenticatedRuntimeSupported"])
        self.assertEqual(status["workspace"], "team-memory")
        self.assertEqual([item["profile"] for item in status["profiles"]], ["default", "researcher"])
        self.assertNotIn("apiKey", json.dumps(status))
        self.assertNotIn("super-secret", json.dumps(status))

    def test_configure_local_identity_updates_default_and_named_profiles_with_backup(self) -> None:
        self._profile("researcher")
        self._profile("review-bot")
        config_path = self._write_honcho({
            "baseUrl": "http://127.0.0.1:8000",
            "enabled": True,
            "workspace": "team-memory",
            "aiPeer": "hermes",
            "customField": {"preserved": True},
            "hosts": {
                "hermes_researcher": {"recallMode": "tools", "aiPeer": "existing-researcher"},
            },
        })
        original = config_path.read_text(encoding="utf-8")

        result = honcho_bridge.configure_local_identity("team-owner")
        written = json.loads(config_path.read_text(encoding="utf-8"))

        self.assertTrue(result["success"])
        self.assertEqual(result["identityMode"], "local-single-user")
        self.assertEqual(written["peerName"], "team-owner")
        self.assertTrue(written["pinUserPeer"])
        self.assertTrue(written["sessionAiPeerPrefix"])
        self.assertEqual(written["customField"], {"preserved": True})

        default = written["hosts"]["hermes"]
        researcher = written["hosts"]["hermes_researcher"]
        reviewer = written["hosts"]["hermes_review-bot"]
        for block in (default, researcher, reviewer):
            self.assertEqual(block["peerName"], "team-owner")
            self.assertTrue(block["pinUserPeer"])
            self.assertTrue(block["sessionAiPeerPrefix"])
            self.assertEqual(block["workspace"], "team-memory")
        self.assertEqual(default["aiPeer"], "hermes")
        self.assertEqual(researcher["aiPeer"], "existing-researcher")
        self.assertEqual(researcher["recallMode"], "tools")
        self.assertEqual(reviewer["aiPeer"], "hermes_review-bot")

        backup = Path(result["backupPath"])
        self.assertTrue(backup.is_file())
        self.assertEqual(backup.read_text(encoding="utf-8"), original)
        self.assertEqual(config_path.stat().st_mode & 0o777, 0o600)

        status = honcho_bridge.load_honcho_status()
        self.assertTrue(status["identityReady"])
        self.assertEqual(status["identityMode"], "local-single-user")
        self.assertEqual(status["peerName"], "team-owner")
        self.assertTrue(status["sessionAiPeerPrefix"])
        self.assertTrue(all(item["identityReady"] for item in status["profiles"]))
        self.assertEqual(
            {item["profile"]: item["aiPeer"] for item in status["profiles"]},
            {"default": "hermes", "researcher": "existing-researcher", "review-bot": "hermes_review-bot"},
        )

    def test_named_profile_with_local_config_is_not_reassigned_to_default_owner(self) -> None:
        profile = self._profile("researcher")
        (profile / ".env").write_text("HERMES_HONCHO_HOST=research-memory\n", encoding="utf-8")
        profile_config = profile / "honcho.json"
        profile_config.write_text(json.dumps({
            "baseUrl": "http://127.0.0.1:9000",
            "workspace": "private-research",
            "hosts": {
                "research-memory": {
                    "aiPeer": "research-agent",
                    "recallMode": "tools",
                },
            },
        }, indent=2) + "\n", encoding="utf-8")
        root_config = self._write_honcho({
            "baseUrl": "http://127.0.0.1:8000",
            "workspace": "main-memory",
        })

        original_profile = profile_config.read_text(encoding="utf-8")
        result = honcho_bridge.configure_local_identity("owner")
        root_written = json.loads(root_config.read_text(encoding="utf-8"))

        self.assertEqual(root_written["hosts"]["hermes"]["peerName"], "owner")
        self.assertNotIn("hermes_researcher", root_written["hosts"])
        self.assertEqual(profile_config.read_text(encoding="utf-8"), original_profile)
        self.assertEqual(len(result["backupPaths"]), 1)
        self.assertEqual(result["skippedProfiles"], ["researcher"])
        self.assertTrue(Path(result["backupPaths"][0]).is_file())

        status = honcho_bridge.load_honcho_status()
        profiles = {item["profile"]: item for item in status["profiles"]}
        self.assertEqual(profiles["researcher"]["host"], "research-memory")
        self.assertEqual(profiles["researcher"]["workspace"], "private-research")
        self.assertEqual(profiles["researcher"]["aiPeer"], "research-agent")
        self.assertEqual(profiles["researcher"]["configPath"], str(profile_config))
        self.assertEqual(profiles["researcher"]["readiness"], "identity-required")
        self.assertEqual(profiles["researcher"]["managedBy"], "profile-local")

    def test_configure_local_identity_rejects_unsafe_peer_without_touching_file(self) -> None:
        config_path = self._write_honcho({"baseUrl": "http://127.0.0.1:8000", "enabled": True})
        original = config_path.read_bytes()

        with self.assertRaises(honcho_bridge.HonchoBridgeError) as ctx:
            honcho_bridge.configure_local_identity("../../someone else")

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(config_path.read_bytes(), original)
        self.assertEqual(list(self.root.glob("honcho.json.bak.*")), [])

    def test_ai_peer_defaults_to_host_key_like_core_lookup(self) -> None:
        """Core resolves the AI peer as `look.pick("aiPeer") or host`; a profile
        without an explicit aiPeer must not report its bare profile name."""
        self._profile("researcher")
        self._write_honcho({"baseUrl": "http://127.0.0.1:8000", "enabled": True})

        status = honcho_bridge.load_honcho_status()
        profiles = {item["profile"]: item for item in status["profiles"]}

        self.assertEqual(profiles["default"]["host"], "hermes")
        self.assertEqual(profiles["default"]["aiPeer"], "hermes")
        self.assertEqual(profiles["researcher"]["host"], "hermes_researcher")
        self.assertEqual(profiles["researcher"]["aiPeer"], "hermes_researcher")

    def test_status_reports_whether_the_memory_provider_is_installed(self) -> None:
        """`providerInstalled` must reflect a real on-disk memory provider, not a
        heuristic: MC hides the Honcho panel entirely when the provider is absent."""
        self._profile("researcher")
        (self.root / "config.yaml").write_text("memory:\n  provider: honcho\n", encoding="utf-8")
        self._write_honcho({"baseUrl": "http://127.0.0.1:8000", "enabled": True})

        status = honcho_bridge.load_honcho_status()

        self.assertIn("providerInstalled", status)
        self.assertIsInstance(status["providerInstalled"], bool)

    def test_provider_installed_can_be_overridden_for_tests(self) -> None:
        os.environ["MISSION_CONTROL_MEMORY_PROVIDERS_DIR"] = str(self.root / "providers")
        try:
            providers = self.root / "providers" / "honcho"
            providers.mkdir(parents=True)
            (providers / "__init__.py").write_text("", encoding="utf-8")
            self.assertTrue(honcho_bridge.load_honcho_status()["providerInstalled"])

            shutil.rmtree(providers)
            self.assertFalse(honcho_bridge.load_honcho_status()["providerInstalled"])
        finally:
            os.environ.pop("MISSION_CONTROL_MEMORY_PROVIDERS_DIR", None)

    def test_named_cloud_profiles_report_missing_credentials_without_copying_default_secret(self) -> None:
        self._profile("researcher")
        self._write_honcho({
            "enabled": True,
            "hosts": {
                "hermes": {
                    "apiKey": "default-profile-secret",
                    "workspace": "cloud-workspace",
                    "aiPeer": "hermes",
                    "peerName": "owner",
                },
                "hermes_researcher": {
                    "workspace": "cloud-workspace",
                    "aiPeer": "researcher",
                    "peerName": "owner",
                },
            },
        })

        status = honcho_bridge.load_honcho_status()
        profiles = {item["profile"]: item for item in status["profiles"]}

        self.assertTrue(profiles["default"]["connectionConfigured"])
        self.assertFalse(profiles["researcher"]["connectionConfigured"])
        self.assertEqual(profiles["researcher"]["readiness"], "credentials-required")
        self.assertNotIn("default-profile-secret", json.dumps(status))


if __name__ == "__main__":
    unittest.main()
