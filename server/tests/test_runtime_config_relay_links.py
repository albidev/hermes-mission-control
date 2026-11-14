"""Strict owner-private config for outbound Invite Rooms relay links."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import MappingProxyType

from invite_rooms import runtime_config
from invite_rooms.sidecar_data_plane import _complete, _validate_topology

PEER = "peer-" + "a" * 32
ENDPOINT = "link-" + "b" * 32


class RelayRuntimeConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="relay-config-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "owner"
        self.root.mkdir(mode=0o700); os.chmod(self.root, 0o700)
        self.peers = self.root / "peers.json"
        self.peers.write_text("[]", encoding="utf-8"); os.chmod(self.peers, 0o600)
        self.secret_dir = self.root / "relay-credentials"
        self.secret_dir.mkdir(mode=0o700); os.chmod(self.secret_dir, 0o700)
        self.credential = self.secret_dir / "peer.credential"
        self.credential.write_text("test-relay-credential", encoding="utf-8"); os.chmod(self.credential, 0o600)
        self.config_path = self.root / "config.json"
        runtime_config._warned_once.clear()
        self.addCleanup(runtime_config._warned_once.clear)

    def base(self):
        return {
            "store_path": str(self.root / "invites.db"),
            "installation_id": "install:" + "c" * 32,
            "profile": "owner",
            "peer_registry_path": str(self.peers),
            "pairing": {
                "identity_dir": str(self.root / "identity"),
                "journal_dir": str(self.root / "journal"),
                "peers_path": str(self.peers),
                "relay_url": "wss://relay.example.test:443/data",
                "relay_links": {ENDPOINT: {
                    "generation": 1,
                    "peer_id": PEER,
                    "link_id": "rly-link-1",
                    "relay_room_id": "rly-room-1",
                    "credential_file": str(self.credential),
                }},
            },
        }

    def load(self, config):
        self.config_path.write_text(json.dumps(config), encoding="utf-8")
        os.chmod(self.config_path, 0o600)
        return runtime_config._load_config_file(str(self.config_path))

    def assert_pairing_disabled(self, data, message=""):
        config = self.load(data)
        if config is not None:
            self.assertIsNone(config.pairing, message)

    def test_valid_wss_link_is_owner_private_and_immutable(self):
        config = self.load(self.base())
        if config is None or config.pairing is None:
            raise AssertionError("valid relay config disabled the pairing feature")
        pairing = config.pairing
        self.assertEqual(pairing.relay_url, "wss://relay.example.test:443/data")
        self.assertIsInstance(pairing.relay_links, MappingProxyType)
        link = pairing.relay_links[ENDPOINT]
        self.assertEqual((link.endpoint_id, link.generation, link.peer_id), (ENDPOINT, 1, PEER))
        self.assertEqual((link.link_id, link.relay_room_id), ("rly-link-1", "rly-room-1"))
        self.assertEqual(link.credential_file, self.credential)
        with self.assertRaises(TypeError):
            pairing.relay_links[ENDPOINT] = link

    def test_invalid_or_ambiguous_relay_topologies_disable_whole_config(self):
        cases = []
        for url in (
            "ws://relay.example.test:443/data", "https://relay.example.test:443/data",
            "wss://relay.example.test/data", "wss://user:pass@relay.example.test:443/data",
            "wss://relay.example.test:443/other", "wss://relay.example.test:443/data?token=x",
            "wss://relay.example.test:443/data#fragment", "wss:///data",
        ):
            data = self.base(); data["pairing"]["relay_url"] = url; cases.append((url, data))
        common = {"link_id": "rly-link-1", "relay_room_id": "rly-room-1",
                  "credential_file": str(self.credential)}
        malformed = [
            dict(common, generation=True, peer_id=PEER),
            dict(common, generation=0, peer_id=PEER),
            dict(common, generation=1, peer_id="peer-bad"),
            dict(common, generation=1, peer_id=PEER, extra=1),
        ]
        for entry in malformed:
            data = self.base(); data["pairing"]["relay_links"] = {ENDPOINT: entry}; cases.append(("entry", data))
        for label, data in cases:
            runtime_config._warned_once.clear()
            self.assert_pairing_disabled(data, label)

    def test_relay_credential_must_be_a_private_file(self):
        public = self.secret_dir / "public.credential"
        public.write_text("not-private", encoding="utf-8"); os.chmod(public, 0o640)
        data = self.base(); data["pairing"]["relay_links"][ENDPOINT]["credential_file"] = str(public)
        self.assert_pairing_disabled(data)
        os.chmod(public, 0o600); os.chmod(self.secret_dir, 0o755)
        self.assert_pairing_disabled(data)

    def test_relay_only_topology_is_complete_and_has_no_direct_data_destination(self):
        from dataclasses import replace
        loaded = self.load(self.base())
        if loaded is None or loaded.pairing is None:
            raise AssertionError("valid relay config disabled")
        pairing = replace(loaded.pairing, control_listen=("127.0.0.1", 9001),
            target_listeners={"link-" + "d" * 32: ("127.0.0.1", 9002)}, data_endpoints=None)
        config = replace(loaded, pairing=pairing, data_listen=("127.0.0.1", 9003),
            joining_store_dir=self.root / "joining", target_http=object(), native_rpc=object(),
            joining_owner={"store_dir": str(self.root / "joining")}, original_frontdoor_url=None)
        self.assertIsNone(pairing.data_endpoints)
        self.assertTrue(pairing.relay_links)
        self.assertTrue(_complete(config))
        _validate_topology(config)

    def test_relay_url_without_links_and_direct_plus_relay_peer_are_rejected(self):
        data = self.base(); data["pairing"].pop("relay_links")
        self.assert_pairing_disabled(data)
        data = self.base(); data["pairing"]["control_endpoints"] = {PEER: {"host": "127.0.0.1", "port": 9010}}
        self.assert_pairing_disabled(data)
        data = self.base(); data["joining"] = {"store_dir": str(self.root / "joining"),
            "control_endpoints": {PEER: {"host": "127.0.0.1", "port": 9011}}}
        self.assert_pairing_disabled(data)


if __name__ == "__main__":
    unittest.main()
