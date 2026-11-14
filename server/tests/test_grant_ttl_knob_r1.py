"""R1: joiner-local bearer TTL knob (behaviour-preserving at the default 600 s).

The knob is an installation-local decision of the minting (joining) side: it lives only in the
owner's ``joining`` runtime-config section, never in a request, message, URL, peer or signed scope.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import test_runtime_config_data_bind_w2d as rc_tests
import test_sidecar_data_plane_assembly_w2d as w2d
from invite_rooms import runtime_config
from invite_rooms.joining_material import MaterialError, TargetMaterialMint
from invite_rooms.joining_store import JoiningStore
from invite_rooms.material_control import MaterialControlChannel
from invite_rooms.runtime_config import RuntimeConfigError
from test_room_invite_joining import CATALOG

DEFAULT_PARAMS = {
    "profile": "default", "room_id": "room", "home_install_id": "host",
    "authority_gateway_id": "host", "authority_epoch": 1, "member_id": "member",
    "ttl_seconds": 600,
}


def setUpModule():
    w2d.setUpModule()


def tearDownModule():
    w2d.tearDownModule()


def _scope():
    return {
        "host_id": "host", "room_id": "room", "member_id": "member",
        "lobby_id": "lobby", "invite_id": "invite",
        "binding": {"endpoint_id": "endpoint", "fingerprint": "f" * 64,
                    "target_install_id": CATALOG["installation_id"], "target_profile": "default",
                    "catalog_digest": CATALOG["catalog_digest"],
                    "policy_digest": CATALOG["execution_policy"]["policy_digest"]},
    }


class _Store:
    def __init__(self, scope):
        self.scope, self.state, self.saved = scope, {}, {}

    def find_import(self, lobby, invite):
        return {"import_id": "import", "host_peer_id": "host-peer"}

    def confirmed_scope(self, _import):
        return self.scope

    def get_consent(self, _import):
        return {"receipt": dict(self.scope, purpose="joining-owner-consent-v1")}

    def mint_state(self, key):
        return self.state.get(key)

    def mint_begin(self, key, **_kwargs):
        self.state[key] = "intent"
        return "grant-id"

    def mint_complete(self, key, raw):
        self.state[key], self.saved[key] = "ready", raw

    def mint_load(self, key):
        return self.saved[key]


class _Signer:
    def sign(self, _payload):
        return "signature"


def _build(store, scope, native_calls, **knob):
    def connector(request, *, deadline=None, admit=None):
        if request["op"] == "status":
            return {"status": "ready", "scope": scope}
        return {"status": "material_ready"}

    async def rpc(method, params, **_kwargs):
        native_calls.append((method, params))
        now = time.time()
        claims = {"room_id": scope["room_id"], "home_install_id": scope["host_id"],
                  "authority_gateway_id": scope["host_id"], "authority_epoch": 1,
                  "member_id": scope["member_id"], "target_install_id": scope["binding"]["target_install_id"],
                  "target_profile": "default", "issued_at": now, "expires_at": now + 600,
                  "status_expires_at": now + 600, "execution_policy_digest": scope["binding"]["policy_digest"]}
        enc = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
        grant = enc + "." + base64.urlsafe_b64encode(b"x" * 32).rstrip(b"=").decode()
        return {"grant": grant, "target_profile": "default", "catalog": CATALOG, "endpoint": CATALOG["endpoint"]}

    channel = MaterialControlChannel(connector, lobby_id="lobby", invite_id="invite", lab=True)
    return TargetMaterialMint(store, consent_key=_Signer(), channel=channel, rpc=rpc,
                              host_peer_id="host-peer", **knob)


def _mint_once(**knob):
    scope = _scope()
    calls = []
    mint = _build(_Store(scope), scope, calls, **knob)
    asyncio.run(mint.mint({"lobby_id": "lobby", "invite_id": "invite"}))
    return calls


class MintParamsTests(unittest.TestCase):
    def test_default_params_are_byte_identical_to_pre_change(self):
        calls = _mint_once()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "groups.peer.invite")
        self.assertEqual(calls[0][1], DEFAULT_PARAMS)
        self.assertEqual(json.dumps(calls[0][1]), json.dumps(DEFAULT_PARAMS))
        self.assertIs(type(calls[0][1]["ttl_seconds"]), int)
        self.assertNotIn("status_ttl_seconds", calls[0][1])

    def test_configured_values_reach_native_request_with_exact_int_types(self):
        calls = _mint_once(grant_ttl_seconds=3600, grant_status_ttl_seconds=7200)
        params = calls[0][1]
        self.assertEqual(params, dict(DEFAULT_PARAMS, ttl_seconds=3600, status_ttl_seconds=7200))
        self.assertIs(type(params["ttl_seconds"]), int)
        self.assertIs(type(params["status_ttl_seconds"]), int)

    def test_ttl_only_carries_no_status_key(self):
        calls = _mint_once(grant_ttl_seconds=60)
        self.assertEqual(calls[0][1], dict(DEFAULT_PARAMS, ttl_seconds=60))

    def test_boundaries_accepted(self):
        for ttl, status in ((60, None), (86400, None), (60, 60), (86400, 2592000), (3600, 2592000)):
            with self.subTest(ttl=ttl, status=status):
                calls = _mint_once(grant_ttl_seconds=ttl, grant_status_ttl_seconds=status)
                self.assertEqual(calls[0][1]["ttl_seconds"], ttl)
                self.assertEqual("status_ttl_seconds" in calls[0][1], status is not None)

    def test_constructor_rejects_each_single_bad_property(self):
        bad = {
            "bool_ttl": dict(grant_ttl_seconds=True),
            "float_ttl": dict(grant_ttl_seconds=3600.0),
            "str_ttl": dict(grant_ttl_seconds="3600"),
            "negative_ttl": dict(grant_ttl_seconds=-1),
            "ttl_59": dict(grant_ttl_seconds=59),
            "ttl_86401": dict(grant_ttl_seconds=86401),
            "bool_status": dict(grant_ttl_seconds=3600, grant_status_ttl_seconds=True),
            "float_status": dict(grant_ttl_seconds=3600, grant_status_ttl_seconds=7200.0),
            "str_status": dict(grant_ttl_seconds=3600, grant_status_ttl_seconds="7200"),
            "status_lt_ttl": dict(grant_ttl_seconds=3600, grant_status_ttl_seconds=3599),
            "status_gt_max": dict(grant_ttl_seconds=3600, grant_status_ttl_seconds=2592001),
            "status_without_ttl": dict(grant_status_ttl_seconds=7200),
        }
        for name, knob in bad.items():
            with self.subTest(name):
                calls = []
                with self.assertRaisesRegex(MaterialError, "invalid_grant_ttl"):
                    _build(_Store(_scope()), _scope(), calls, **knob)
                self.assertEqual(calls, [])

    def test_no_ttl_is_ever_read_from_a_request_or_peer(self):
        scope = _scope()
        calls = []
        mint = _build(_Store(scope), scope, calls)
        for extra in ({"ttl_seconds": 86400}, {"status_ttl_seconds": 86400}, {"grant_ttl_seconds": 86400}):
            with self.subTest(extra=extra):
                with self.assertRaisesRegex(MaterialError, "invalid_owner_scope"):
                    asyncio.run(mint.mint(dict({"lobby_id": "lobby", "invite_id": "invite"}, **extra)))
        self.assertEqual(calls, [])
        # and a normal mint is still the default 600 despite hostile request attempts above
        asyncio.run(mint.mint({"lobby_id": "lobby", "invite_id": "invite"}))
        self.assertEqual(calls[0][1], DEFAULT_PARAMS)

    def test_idempotent_replay_survives_knob_change(self):
        scope = _scope()
        store, calls = _Store(scope), []
        first = _build(store, scope, calls, grant_ttl_seconds=3600, grant_status_ttl_seconds=7200)
        asyncio.run(first.mint({"lobby_id": "lobby", "invite_id": "invite"}))
        key = next(iter(store.saved))
        persisted = store.saved[key]
        self.assertEqual(len(calls), 1)
        second = _build(store, scope, calls, grant_ttl_seconds=86400, grant_status_ttl_seconds=2592000)
        self.assertEqual(asyncio.run(second.mint({"lobby_id": "lobby", "invite_id": "invite"})),
                         {"status": "material_ready"})
        default = _build(store, scope, calls)
        asyncio.run(default.mint({"lobby_id": "lobby", "invite_id": "invite"}))
        self.assertEqual(len(calls), 1)               # no re-mint
        self.assertEqual(store.saved[key], persisted)  # envelope byte-exact


class _RealJournalStore(JoiningStore):
    """REAL durable ``JoiningStore`` (encrypted SQLite mint journal: mint_state/begin/complete/load).

    Only the three owner-consent lookups are stubbed (they need a full pairing import, which is
    out of scope here); the mint journal under test is the production implementation."""
    scope = None

    def find_import(self, lobby, invite, **_kw):
        return {"import_id": "import", "host_peer_id": "host-peer"}

    def confirmed_scope(self, _import, **_kw):
        return self.scope

    def get_consent(self, _import, **_kw):
        return {"receipt": dict(self.scope, purpose="joining-owner-consent-v1")}


def _open_real(root, scope):
    store = _RealJournalStore(root)
    store.scope = scope
    return store


def _build_delivering(store, scope, native_calls, delivered, **knob):
    """Like ``_build`` but the control connector records the DELIVERED material envelope."""
    mint = _build(store, scope, native_calls, **knob)

    def connector(wire, *, deadline=None, admit=None):
        if wire["op"] == "status":
            return {"status": "ready", "scope": scope}
        assert wire["op"] == "material_receipt"
        delivered.append(json.dumps(wire["receipt"], sort_keys=True, separators=(",", ":")).encode())
        return {"status": "material_ready"}
    mint._channel = MaterialControlChannel(connector, lobby_id="lobby", invite_id="invite", lab=True)
    return mint


def _journal_keys(store):
    with store._read() as conn:
        return [r["scope_key"] for r in conn.execute("SELECT scope_key FROM material")]


def _replay_scenario(root):
    """mint -> close -> reopen -> remint (changed knob) -> close -> reopen -> remint (knob unset)."""
    scope, calls, delivered, persisted, results, counts = _scope(), [], [], [], [], []
    payload = {"lobby_id": "lobby", "invite_id": "invite"}
    steps = (dict(grant_ttl_seconds=3600, grant_status_ttl_seconds=7200),
             dict(grant_ttl_seconds=86400, grant_status_ttl_seconds=2592000), {})
    for knob in steps:
        store = _open_real(root, scope)          # fresh handle on the SAME directory each time
        try:
            mint = _build_delivering(store, scope, calls, delivered, **knob)
            results.append(asyncio.run(mint.mint(payload)))
            counts.append(len(calls))
            persisted.append(store.mint_load(_journal_keys(store)[0]))
        finally:
            store.close()
    return {"calls": calls, "delivered": delivered, "persisted": persisted,
            "results": results, "native_counts": counts}


def _assert_replay(tc, obs):
    tc.assertEqual(obs["native_counts"], [1, 1, 1])            # no re-mint after close/reopen
    tc.assertEqual(len(obs["calls"]), 1)
    params = obs["calls"][0][1]                                 # what the first mint really sent
    tc.assertEqual(params, dict(DEFAULT_PARAMS, ttl_seconds=3600, status_ttl_seconds=7200))
    tc.assertIs(type(params["ttl_seconds"]), int)
    tc.assertIs(type(params["status_ttl_seconds"]), int)
    tc.assertEqual(obs["results"], [{"status": "material_ready"}] * 3)
    tc.assertEqual(len(obs["delivered"]), 3)
    tc.assertEqual(obs["delivered"][1], obs["delivered"][0])    # DELIVERED envelope byte-identical
    tc.assertEqual(obs["delivered"][2], obs["delivered"][0])
    tc.assertEqual(obs["persisted"][1], obs["persisted"][0])    # persisted record unchanged
    tc.assertEqual(obs["persisted"][2], obs["persisted"][0])
    tc.assertEqual(json.loads(obs["persisted"][0]), json.loads(obs["delivered"][0]))


class RealJournalReplayTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "joining"

    def test_replay_after_close_reopen_with_changed_and_unset_knob_real_store(self):
        _assert_replay(self, _replay_scenario(self.root))

    def test_selfcheck_mutant_that_reminted_on_replay_is_detected(self):
        """Discrimination self-check: a mutant whose replay ignores the journal (re-calls native)
        MUST be caught by the real-store assertions; proves the test above is not vacuous."""
        real_begin = JoiningStore.mint_begin

        def forgetful_begin(self, key, **kw):      # mutant: journal row is discarded, mint runs again
            with self._write() as conn:
                conn.execute("DELETE FROM material WHERE scope_key=?", (key,))
            return real_begin(self, key, **kw)
        with mock.patch.object(_RealJournalStore, "mint_state", lambda self, key, **kw: None), \
                mock.patch.object(_RealJournalStore, "mint_begin", forgetful_begin):
            obs = _replay_scenario(self.root)     # must run to completion: detection is by assertion
            self.assertGreater(len(obs["calls"]), 1)
            with self.assertRaises(AssertionError):
                _assert_replay(self, obs)


class ConfigTests(rc_tests._Base):
    def cfg(self, **joining):
        data = self.base()
        data["joining"] = dict({"store_dir": str(self.root / "joining")}, **joining)
        return data

    def test_default_has_no_knob(self):
        config = self.load(self.cfg())
        self.assertIsNone(config.joining_grant_ttl_seconds)
        self.assertIsNone(config.joining_grant_status_ttl_seconds)

    def test_round_trip_through_real_loader(self):
        config = self.load(self.cfg(grant_ttl_seconds=3600, grant_status_ttl_seconds=7200))
        self.assertEqual((config.joining_grant_ttl_seconds, config.joining_grant_status_ttl_seconds), (3600, 7200))
        self.assertIs(type(config.joining_grant_ttl_seconds), int)
        config = self.load(self.cfg(grant_ttl_seconds=3600))
        self.assertEqual((config.joining_grant_ttl_seconds, config.joining_grant_status_ttl_seconds), (3600, None))
        for ttl, status in ((60, 60), (86400, 2592000)):
            config = self.load(self.cfg(grant_ttl_seconds=ttl, grant_status_ttl_seconds=status))
            self.assertEqual((config.joining_grant_ttl_seconds, config.joining_grant_status_ttl_seconds), (ttl, status))

    def test_each_single_bad_property_fails_closed(self):
        bad = {
            "bool_ttl": dict(grant_ttl_seconds=True),
            "float_ttl": dict(grant_ttl_seconds=3600.0),
            "str_ttl": dict(grant_ttl_seconds="3600"),
            "negative_ttl": dict(grant_ttl_seconds=-1),
            "ttl_59": dict(grant_ttl_seconds=59),
            "ttl_86401": dict(grant_ttl_seconds=86401),
            "bool_status": dict(grant_ttl_seconds=3600, grant_status_ttl_seconds=True),
            "float_status": dict(grant_ttl_seconds=3600, grant_status_ttl_seconds=7200.0),
            "str_status": dict(grant_ttl_seconds=3600, grant_status_ttl_seconds="7200"),
            "status_lt_ttl": dict(grant_ttl_seconds=3600, grant_status_ttl_seconds=3599),
            "status_gt_max": dict(grant_ttl_seconds=3600, grant_status_ttl_seconds=2592001),
            "status_without_ttl": dict(grant_status_ttl_seconds=7200),
            "unknown_key": dict(grant_ttl_seconds=3600, grant_bogus_seconds=1),
        }
        # The loader maps RuntimeConfigError to a fail-closed ``None`` (+ one warning); spy on the
        # dedicated validator so each negative is proven to reach it and raise ``invalid_joining``.
        real = runtime_config._parse_joining_grant_ttls
        for name, joining in bad.items():
            with self.subTest(name):
                seen = []

                def spy(raw, _real=real, _seen=seen):
                    try:
                        return _real(raw)
                    except RuntimeConfigError as exc:
                        _seen.append(str(exc))
                        raise
                with mock.patch.object(runtime_config, "_parse_joining_grant_ttls", spy):
                    self.assertIsNone(self.load(self.cfg(**joining)))
                if name == "unknown_key":      # rejected earlier by the closed field set
                    self.assertEqual(seen, [])
                else:
                    self.assertEqual(seen, ["invalid_joining"])


class SidecarWiringTests(w2d.Base):
    joining_extra: dict = {}

    def config_dict(self, side, remote_peer, **kw):
        data = super().config_dict(side, remote_peer, **kw)
        data["joining"].update(self.joining_extra)
        return data

    def _resolve(self):
        plane_b = self.plane(self.B)
        context = self.context(plane_b, side=self.B, role="joining", operation="material_request")
        request = {"op": "material_request", "lobby_id": "lobby-1", "invite_id": "invite-1"}
        loop = asyncio.new_event_loop()
        self.addCleanup(loop.close)
        return loop.run_until_complete(plane_b.material_control._target_resolver(
            context=context, request=request, deadline=time.monotonic() + 5))

    def test_default_config_builds_mint_without_knob(self):
        mint = self._resolve()
        self.assertIs(type(mint), TargetMaterialMint)
        self.assertEqual((mint._grant_ttl_seconds, mint._grant_status_ttl_seconds), (None, None))

    def test_configured_values_flow_config_to_resolver_to_mint(self):
        self.joining_extra = {"grant_ttl_seconds": 3600, "grant_status_ttl_seconds": 7200}
        mint = self._resolve()
        self.assertIs(type(mint), TargetMaterialMint)
        self.assertEqual((mint._grant_ttl_seconds, mint._grant_status_ttl_seconds), (3600, 7200))
        self.assertIs(type(mint._grant_ttl_seconds), int)


if __name__ == "__main__":
    unittest.main()
