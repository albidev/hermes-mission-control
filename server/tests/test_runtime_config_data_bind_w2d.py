"""W2d Card2-A2b3-1a: strict owner-private data bind/address config (pure config, no sockets)."""
from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import MappingProxyType

from invite_rooms import runtime_config

PEER_A = "peer-" + "a" * 32
PEER_B = "peer-" + "b" * 32
LINK_1 = "link-" + "1" * 32
LINK_2 = "link-" + "2" * 32


def _ep(host="127.0.0.1", port=7001):
    return {"host": host, "port": port}


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "owner"
        self.root.mkdir(mode=0o700)
        os.chmod(self.root, 0o700)
        self.peers = self.root / "peers.json"
        self.peers.write_text("[]", encoding="utf-8")
        os.chmod(self.peers, 0o600)
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
            },
        }

    def write(self, data, raw_text=None):
        self.config_path.write_text(raw_text if raw_text is not None else json.dumps(data), encoding="utf-8")
        os.chmod(self.config_path, 0o600)

    def load(self, data=None, raw_text=None):
        self.write(data, raw_text)
        return runtime_config._load_config_file(str(self.config_path))

    def with_pairing(self, **extra):
        data = self.base()
        data["pairing"].update(extra)
        return data


class DataListenTests(_Base):
    def test_data_listen_accepts_only_exact_loopback_and_explicit_integer_port(self):
        for host, port in (("127.0.0.1", 7100), ("::1", 65535), ("127.0.0.1", 1)):
            data = self.base()
            data["data_listen"] = _ep(host, port)
            config = self.load(data)
            self.assertIsNotNone(config, (host, port))
            self.assertEqual(config.data_listen, (host, port))
        self.assertIsNone(self.load(self.base()).data_listen)

    def test_data_listen_rejects_non_loopback_and_non_integer_ports(self):
        bad = [
            _ep("0.0.0.0"), _ep("::"), _ep("localhost"), _ep("127.0.0.2"), _ep("::ffff:127.0.0.1"),
            _ep("0:0:0:0:0:0:0:1"), _ep(" 127.0.0.1"), _ep("127.0.0.1 "), _ep("[::1]"), _ep("::1%lo0"),
            _ep("127.000.000.001"), _ep("LOCALHOST"), _ep(None), _ep(127),
            _ep(port=0), _ep(port=65536), _ep(port=-1), _ep(port=True), _ep(port=False), _ep(port=7001.0),
            _ep(port="7001"), _ep(port=None),
            {"host": "127.0.0.1"}, {"port": 7001}, {}, [], "127.0.0.1:7001", None, 7001,
        ]
        for value in bad:
            data = self.base()
            data["data_listen"] = value
            runtime_config._warned_once.clear()
            self.assertIsNone(self.load(data), repr(value))


class TargetListenersTests(_Base):
    def test_target_listeners_validate_adapter_ids_and_each_scope_bind(self):
        data = self.with_pairing(target_listeners={LINK_1: _ep(port=7201), LINK_2: _ep("::1", 7202)})
        config = self.load(data)
        self.assertIsNotNone(config)
        listeners = config.pairing.target_listeners
        self.assertIsInstance(listeners, MappingProxyType)
        self.assertEqual(dict(listeners), {LINK_1: ("127.0.0.1", 7201), LINK_2: ("::1", 7202)})
        with self.assertRaises(TypeError):
            listeners[LINK_1] = ("127.0.0.1", 1)
        bad_keys = ["link-" + "1" * 31, "link-" + "1" * 33, "link-" + "A" * 32, "LINK-" + "1" * 32,
                    "link-" + "g" * 32, "adapter-" + "1" * 32, PEER_A, "", " link-" + "1" * 32, LINK_1 + "\n"]
        for key in bad_keys:
            runtime_config._warned_once.clear()
            self.assertIsNone(self.load(self.with_pairing(target_listeners={key: _ep()})), repr(key))
        bad_values = [_ep("0.0.0.0"), _ep("localhost"), _ep(port=0), _ep(port=True), _ep(port="1"),
                      {"host": "127.0.0.1"}, "127.0.0.1:1", None]
        for value in bad_values:
            runtime_config._warned_once.clear()
            self.assertIsNone(self.load(self.with_pairing(target_listeners={LINK_1: value})), repr(value))
        for empty in ({}, [], None, "x"):
            runtime_config._warned_once.clear()
            self.assertIsNone(self.load(self.with_pairing(target_listeners=empty)), repr(empty))
        # Two scopes may not share one bind.
        dup = {LINK_1: _ep(port=7201), LINK_2: _ep(port=7201)}
        self.assertIsNone(self.load(self.with_pairing(target_listeners=dup)))


class DataEndpointsTests(_Base):
    def test_data_endpoints_validate_peer_scope_map_and_exact_destination_shape(self):
        data = self.with_pairing(data_endpoints={
            PEER_A: {LINK_1: _ep(port=7301), LINK_2: _ep("::1", 7302)},
            PEER_B: {LINK_1: _ep(port=7303)},
        })
        config = self.load(data)
        self.assertIsNotNone(config)
        endpoints = config.pairing.data_endpoints
        self.assertIsInstance(endpoints, MappingProxyType)
        self.assertIsInstance(endpoints[PEER_A], MappingProxyType)
        self.assertEqual(endpoints[PEER_A][LINK_2], ("::1", 7302))
        self.assertEqual(endpoints[PEER_B][LINK_1], ("127.0.0.1", 7303))
        with self.assertRaises(TypeError):
            endpoints[PEER_A] = {}
        with self.assertRaises(TypeError):
            endpoints[PEER_A][LINK_1] = ("127.0.0.1", 1)
        bad = [
            {"peer-" + "a" * 31: {LINK_1: _ep()}},
            {"peer-" + "A" * 32: {LINK_1: _ep()}},
            {LINK_1: {LINK_1: _ep()}},
            {"": {LINK_1: _ep()}},
            {PEER_A: {}},
            {PEER_A: []},
            {PEER_A: None},
            {PEER_A: _ep()},  # flat control-style shape is not a scope map
            {PEER_A: {PEER_B: _ep()}},
            {PEER_A: {"link-" + "1" * 31: _ep()}},
            {PEER_A: {LINK_1: _ep("0.0.0.0")}},
            {PEER_A: {LINK_1: _ep("::ffff:127.0.0.1")}},
            {PEER_A: {LINK_1: _ep("localhost")}},
            {PEER_A: {LINK_1: _ep(port=0)}},
            {PEER_A: {LINK_1: _ep(port=True)}},
            {PEER_A: {LINK_1: _ep(port=7001.0)}},
            {PEER_A: {LINK_1: _ep(port="7001")}},
            {PEER_A: {LINK_1: {"host": "127.0.0.1"}}},
            {PEER_A: {LINK_1: "127.0.0.1:7001"}},
            {},
            [],
            None,
            "x",
        ]
        for value in bad:
            runtime_config._warned_once.clear()
            self.assertIsNone(self.load(self.with_pairing(data_endpoints=value)), repr(value))

    def test_data_endpoints_reject_duplicate_target_address_across_scopes_and_peers(self):
        # Valid sibling: every (peer, scope) destination is distinct.
        good = self.with_pairing(data_endpoints={
            PEER_A: {LINK_1: _ep(port=7311), LINK_2: _ep(port=7312)},
            PEER_B: {LINK_1: _ep(port=7313)},
        })
        config = self.load(good)
        self.assertIsNotNone(config)
        self.assertEqual(config.pairing.data_endpoints[PEER_B][LINK_1], ("127.0.0.1", 7313))
        # Positive control: same port on different literal loopback hosts is a distinct address.
        runtime_config._warned_once.clear()
        distinct_hosts = self.with_pairing(data_endpoints={
            PEER_A: {LINK_1: _ep("127.0.0.1", 7310), LINK_2: _ep("::1", 7310)},
        })
        self.assertIsNotNone(self.load(distinct_hosts))
        duplicates = {
            "same_peer_two_scopes": {PEER_A: {LINK_1: _ep(port=7310), LINK_2: _ep(port=7310)}},
            "two_peers_same_scope": {PEER_A: {LINK_1: _ep(port=7310)}, PEER_B: {LINK_1: _ep(port=7310)}},
            "two_peers_different_scopes": {PEER_A: {LINK_1: _ep(port=7310)}, PEER_B: {LINK_2: _ep(port=7310)}},
            "same_peer_two_scopes_ipv6": {PEER_A: {LINK_1: _ep("::1", 7310), LINK_2: _ep("::1", 7310)}},
        }
        for name, value in duplicates.items():
            runtime_config._warned_once.clear()
            self.assertIsNone(self.load(self.with_pairing(data_endpoints=value)), name)


class AliasAndCollisionTests(_Base):
    def test_data_fields_reject_control_endpoint_aliases_and_local_bind_collisions(self):
        # Local binds must all differ.
        cases = {
            "control_vs_data": ({"pairing": {"control_listen": _ep(port=7400)}, "data_listen": _ep(port=7400)}),
            "control_vs_target": ({"pairing": {"control_listen": _ep(port=7400),
                                               "target_listeners": {LINK_1: _ep(port=7400)}}}),
            "data_vs_target": ({"data_listen": _ep(port=7401), "pairing": {"target_listeners": {LINK_1: _ep(port=7401)}}}),
            # Data destination may not equal the SAME peer's control destination (pairing map).
            "dest_vs_pairing_control": ({"pairing": {"control_endpoints": {PEER_A: _ep(port=7402)},
                                                     "data_endpoints": {PEER_A: {LINK_1: _ep(port=7402)}}}}),
            # ... nor the same peer's joining control destination.
            "dest_vs_joining_control": ({"joining": {"store_dir": "/x", "control_endpoints": {PEER_A: _ep(port=7403)}},
                                         "pairing": {"data_endpoints": {PEER_A: {LINK_2: _ep(port=7403)}}}}),
        }
        for name, patch in cases.items():
            data = self.base()
            for key, value in copy.deepcopy(patch).items():
                if key == "pairing":
                    data["pairing"].update(value)
                else:
                    data[key] = value
            runtime_config._warned_once.clear()
            self.assertIsNone(self.load(data), name)
        # Positive control: distinct addresses (including same port on different loopback literal) are accepted.
        data = self.base()
        data["data_listen"] = _ep(port=7500)
        data["pairing"].update({
            "control_listen": _ep(port=7501),
            "target_listeners": {LINK_1: _ep(port=7502)},
            "control_endpoints": {PEER_A: _ep(port=7503)},
            "data_endpoints": {PEER_A: {LINK_1: _ep(port=7504)}, PEER_B: {LINK_1: _ep(port=7503)}},
        })
        self.assertIsNotNone(self.load(data))


class StrictJsonTests(_Base):
    def test_new_data_fields_reject_duplicate_json_keys_and_extra_or_missing_keys(self):
        good = self.base()
        good["data_listen"] = _ep(port=7600)
        good["pairing"].update({"target_listeners": {LINK_1: _ep(port=7601)},
                                "data_endpoints": {PEER_A: {LINK_1: _ep(port=7602)}}})
        self.assertIsNotNone(self.load(good))
        text = json.dumps(good)
        dup_texts = [
            text.replace('"data_listen":', '"data_listen": {"host": "127.0.0.1", "port": 7600}, "data_listen":', 1),
            text.replace('"port": 7600', '"port": 7600, "port": 7600', 1),
            text.replace('"host": "127.0.0.1", "port": 7601', '"host": "127.0.0.1", "host": "127.0.0.1", "port": 7601', 1),
            text.replace('"target_listeners": {', '"target_listeners": {"%s": {"host": "::1", "port": 7609}, ' % LINK_1, 1),
            text.replace('"data_endpoints": {"%s": {' % PEER_A,
                         '"data_endpoints": {"%s": {}, "%s": {' % (PEER_A, PEER_A), 1),
            text.replace('"%s": {"host": "127.0.0.1", "port": 7602}' % LINK_1,
                         '"%s": {"host": "127.0.0.1", "port": 7602}, "%s": {"host": "::1", "port": 7603}' % (LINK_1, LINK_1), 1),
        ]
        for dup in dup_texts:
            self.assertNotEqual(dup, text)
            runtime_config._warned_once.clear()
            self.assertIsNone(self.load(raw_text=dup), dup)
        extras = []
        for path in ("data_listen", "target_listener", "data_endpoint"):
            data = copy.deepcopy(good)
            if path == "data_listen":
                data["data_listen"]["extra"] = 1
            elif path == "target_listener":
                data["pairing"]["target_listeners"][LINK_1]["extra"] = 1
            else:
                data["pairing"]["data_endpoints"][PEER_A][LINK_1]["extra"] = 1
            extras.append(data)
        for part in ("host", "port"):
            for path in ("data_listen", "target_listener", "data_endpoint"):
                data = copy.deepcopy(good)
                target = (data["data_listen"] if path == "data_listen" else
                          data["pairing"]["target_listeners"][LINK_1] if path == "target_listener" else
                          data["pairing"]["data_endpoints"][PEER_A][LINK_1])
                del target[part]
                extras.append(data)
        extras.append({**copy.deepcopy(good), "data_bind": _ep()})
        for data in extras:
            runtime_config._warned_once.clear()
            self.assertIsNone(self.load(data), json.dumps(data))
        # data_listen is top-level only: inside pairing it is an unknown pairing key (legacy: pairing disabled,
        # so no data listener/target config can reach runtime assembly).
        data = copy.deepcopy(good)
        data["pairing"]["data_listen"] = _ep(port=7700)
        runtime_config._warned_once.clear()
        config = self.load(data)
        self.assertTrue(config is None or config.pairing is None)


class FailureModeTests(_Base):
    def test_invalid_data_listener_disables_whole_invite_config_without_raw_warning_values(self):
        marker_host = "SECRET-HOST-MARKER.example"
        for data in (
            {**self.base(), "data_listen": _ep(marker_host, 7800)},
            self.with_pairing(target_listeners={LINK_1: _ep(marker_host, 7801)}),
            self.with_pairing(data_endpoints={PEER_A: {LINK_1: _ep(marker_host, 7802)}}),
            self.with_pairing(target_listeners={"link-SECRETKEYMARKER": _ep()}),
        ):
            runtime_config._warned_once.clear()
            with self.assertLogs("invite_rooms.runtime_config", level="WARNING") as logs:
                result = self.load(data)
            self.assertIsNone(result)
            rendered = "\n".join(logs.output)
            self.assertNotIn("SECRET", rendered)
            self.assertNotIn(marker_host, rendered)
            self.assertNotIn(str(self.root), rendered)
        # A valid sibling field never leaves a partial listener config behind.
        data = self.base()
        data["data_listen"] = _ep(port=7803)
        data["pairing"]["target_listeners"] = {LINK_1: _ep("0.0.0.0", 7804)}
        self.assertIsNone(self.load(data))


class LegacyTests(_Base):
    def test_omitted_data_fields_preserve_legacy_runtime_config_behavior(self):
        config = self.load(self.base())
        self.assertIsNotNone(config)
        self.assertIsNone(config.data_listen)
        self.assertIsNone(config.pairing.target_listeners)
        self.assertIsNone(config.pairing.data_endpoints)
        self.assertIsNone(config.pairing.control_listen)
        self.assertIsNone(config.pairing.control_endpoints)
        data = self.with_pairing(control_listen=_ep(port=7900), control_endpoints={PEER_A: _ep(port=7901)})
        config = self.load(data)
        self.assertEqual(config.pairing.control_listen, ("127.0.0.1", 7900))
        self.assertEqual(dict(config.pairing.control_endpoints), {PEER_A: ("127.0.0.1", 7901)})
        self.assertIsNone(config.data_listen)
        # Positional / keyword construction compatibility.
        pairing = runtime_config.PairingConfig(Path("/i"), Path("/j"), Path("/a"), Path("/p"))
        self.assertIsNone(pairing.target_listeners)
        self.assertIsNone(pairing.data_endpoints)
        pairing = runtime_config.PairingConfig(Path("/i"), Path("/j"), Path("/a"), Path("/p"), ("127.0.0.1", 1), None)
        self.assertEqual(pairing.control_listen, ("127.0.0.1", 1))
        rc = runtime_config.RuntimeConfig(Path("/s"), "install:" + "c" * 32, "owner", Path("/r"), None)
        self.assertIsNone(rc.data_listen)
        # Pairing-less config with data_listen only is valid and independent of pairing.
        data = self.base()
        del data["pairing"]
        data["data_listen"] = _ep(port=7902)
        config = self.load(data)
        self.assertIsNotNone(config)
        self.assertIsNone(config.pairing)
        self.assertEqual(config.data_listen, ("127.0.0.1", 7902))


if __name__ == "__main__":
    unittest.main()
