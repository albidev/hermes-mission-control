"""Joiner-side acceptance and local clamping of the unsigned host TTL hint."""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from invite_rooms import runtime_config
from invite_rooms.joining import JoiningError, JoiningOwnerService
from invite_rooms.joining_material import MaterialError, _GRANT_TTL_HINT, TargetMaterialMint
from invite_rooms.runtime_joining import JoiningOwner
from invite_rooms.material_control import MaterialControlChannel
from invite_rooms.joining_store import JoiningStore, JoiningStoreError
from test_grant_ttl_knob_r1 import (_Store, _Signer, _scope, _build,
                                    _RealJournalStore, _build_delivering, _assert_replay)


class HintMintTests(unittest.TestCase):
    def mint(self, hint, **knob):
        scope = _scope()
        calls = []
        mint = _build(_Store(scope), scope, calls, **knob)

        def connector(request, *, deadline=None, admit=None):
            if request["op"] == "status":
                result = {"status": "ready", "scope": scope}
                if hint is not _MISSING:
                    result["grant_ttl_hint_seconds"] = hint
                return result
            return {"status": "material_ready"}

        mint._channel = MaterialControlChannel(connector, lobby_id="lobby", invite_id="invite", lab=True)
        asyncio.run(mint.mint({"lobby_id": "lobby", "invite_id": "invite"}))
        return calls[0][1]

    def test_hint_is_honoured_and_capped(self):
        self.assertEqual(self.mint(3600, grant_ttl_max_seconds=86400)["ttl_seconds"], 3600)
        self.assertEqual(self.mint(86400, grant_ttl_max_seconds=7200)["ttl_seconds"], 7200)
        self.assertEqual(self.mint(90000, grant_ttl_seconds=3600, grant_ttl_max_seconds=7200)["ttl_seconds"], 3600)
        self.assertEqual(self.mint(60)["ttl_seconds"], 60)
        self.assertEqual(self.mint(601)["ttl_seconds"], 600)

    def test_explicit_cap_applies_to_fallback_and_unset_cap_is_base(self):
        self.assertEqual(self.mint(_MISSING, grant_ttl_max_seconds=60)["ttl_seconds"], 60)
        self.assertEqual(self.mint(True, grant_ttl_max_seconds=60)["ttl_seconds"], 60)
        self.assertEqual(self.mint(86400, grant_ttl_seconds=3600)["ttl_seconds"], 3600)
        self.assertEqual(self.mint(120, grant_ttl_seconds=3600)["ttl_seconds"], 120)

    def test_status_ttl_is_raised_to_effective_hint(self):
        params = self.mint(86400, grant_ttl_seconds=60, grant_status_ttl_seconds=60,
                            grant_ttl_max_seconds=86400)
        self.assertEqual(params["ttl_seconds"], 86400)
        self.assertEqual(params["status_ttl_seconds"], 86400)

    def test_invalid_hint_falls_back_without_failing(self):
        for hint in (True, 3600.0, "3600", -1, 0, 59, 86401, 10**100, 10**1000, None):
            with self.subTest(hint=hint):
                self.assertEqual(self.mint(hint)["ttl_seconds"], 600)

    def test_old_status_wire_key_is_rejected(self):
        scope = _scope()
        mint = _build(_Store(scope), scope, [])
        mint._channel = MaterialControlChannel(
            lambda request, **_: {"status": "ready", "scope": scope, "grant_ttl_seconds": 120},
            lobby_id="lobby", invite_id="invite", lab=True)
        with self.assertRaisesRegex(MaterialError, "both_consents_required"):
            asyncio.run(mint.mint({"lobby_id": "lobby", "invite_id": "invite"}))

    def test_unknown_status_key_remains_rejected(self):
        scope = _scope()
        mint = _build(_Store(scope), scope, [])
        mint._channel = MaterialControlChannel(
            lambda request, **_: {"status": "ready", "scope": scope, "unexpected": 1},
            lobby_id="lobby", invite_id="invite", lab=True)
        with self.assertRaisesRegex(MaterialError, "both_consents_required"):
            asyncio.run(mint.mint({"lobby_id": "lobby", "invite_id": "invite"}))

    def test_mint_honours_replaced_approved_scope_async_seam(self):
        scope = _scope()
        calls = []
        mint = _build(_Store(scope), scope, calls)

        async def approved_scope(payload, *, deadline=None, peer_context=None):
            calls.append((payload, deadline, peer_context))
            return scope

        mint.approved_scope_async = approved_scope
        asyncio.run(mint.mint({"lobby_id": "lobby", "invite_id": "invite"}))
        self.assertEqual(calls[0], ({"lobby_id": "lobby", "invite_id": "invite"}, None, None))
        self.assertEqual(calls[1][0], "groups.peer.invite")

    def test_contextvar_hint_is_reset_and_isolated_between_interleaved_mints(self):
        scopes = {}
        hints = {}
        for invite, hint in (("invite", 3600), ("invite-2", _MISSING),
                             ("invite-3", 1800), ("invite-4", 2400)):
            scope = dict(_scope(), invite_id=invite)
            scopes[invite], hints[invite] = scope, hint

        class Store:
            def __init__(self):
                self.state, self.saved = {}, {}

            def find_import(self, _lobby, invite):
                return {"import_id": invite, "host_peer_id": "host-peer"}

            def confirmed_scope(self, invite):
                return scopes[invite]

            def get_consent(self, invite):
                scope = scopes[invite]
                return {"receipt": dict(scope, purpose="joining-owner-consent-v1")}

            def mint_state(self, key):
                return self.state.get(key)

            def mint_begin(self, key, **_kwargs):
                self.state[key] = "intent"

            def mint_complete(self, key, raw):
                self.state[key], self.saved[key] = "ready", raw

            def mint_load(self, key):
                return self.saved[key]

        class Channel:
            def call(self, request):
                invite = request["invite_id"]
                if request["op"] == "status":
                    result = {"status": "ready", "scope": scopes[invite]}
                    if hints[invite] is not _MISSING:
                        result["grant_ttl_hint_seconds"] = hints[invite]
                    return result
                return {"status": "material_ready"}

        store, native_calls = Store(), []
        mint = _build(store, scopes["invite"], native_calls, grant_ttl_max_seconds=86400)
        mint._channel = Channel()

        async def run(payload):
            await asyncio.sleep(0)
            await mint.mint(payload)

        async def exercise():
            await run({"lobby_id": "lobby", "invite_id": "invite"})
            self.assertIsNot(_GRANT_TTL_HINT.get(), 3600)
            await run({"lobby_id": "lobby", "invite_id": "invite-2"})

        asyncio.run(exercise())
        self.assertEqual([params["ttl_seconds"] for _, params in native_calls[:2]], [3600, 600])

        async def gather_two():
            await asyncio.gather(
                run({"lobby_id": "lobby", "invite_id": "invite-3"}),
                run({"lobby_id": "lobby", "invite_id": "invite-4"}),
            )

        asyncio.run(gather_two())
        self.assertEqual(sorted(params["ttl_seconds"] for _, params in native_calls[2:]), [1800, 2400])


class HintConfigTests(unittest.TestCase):
    def test_max_validator_and_ttl_ordering(self):
        self.assertEqual(runtime_config._parse_joining_grant_ttls({"grant_ttl_max_seconds": 7200}), (None, None, 7200))
        self.assertEqual(runtime_config._parse_joining_grant_ttls({"grant_ttl_seconds": 3600, "grant_ttl_max_seconds": 3600}), (3600, None, 3600))
        for value in (True, 59, 86401, 7200.0):
            with self.subTest(value=value):
                with self.assertRaisesRegex(runtime_config.RuntimeConfigError, "invalid_joining"):
                    runtime_config._parse_joining_grant_ttls({"grant_ttl_max_seconds": value})
        with self.assertRaisesRegex(runtime_config.RuntimeConfigError, "invalid_joining"):
            runtime_config._parse_joining_grant_ttls({"grant_ttl_seconds": 7201, "grant_ttl_max_seconds": 7200})

    def test_real_target_mint_replay_after_close_reopen_ignores_changed_hint_and_cap(self):
        with tempfile.TemporaryDirectory() as td:
            root, scope, payload = Path(td) / "joining", _scope(), {"lobby_id": "lobby", "invite_id": "invite"}
            calls, delivered = [], []
            for hint, cap in ((3600, 7200), (86400, 60)):
                store = _RealJournalStore(root)
                store.scope = scope
                mint = _build(store, scope, calls, grant_ttl_seconds=cap,
                              grant_status_ttl_seconds=cap)
                def connector(wire, *, _hint=hint):
                    if wire["op"] == "status":
                        return {"status": "ready", "scope": scope, "grant_ttl_hint_seconds": _hint}
                    delivered.append(json.dumps(wire["receipt"], sort_keys=True, separators=(",", ":")).encode())
                    return {"status": "material_ready"}
                mint._channel = MaterialControlChannel(connector, lobby_id="lobby", invite_id="invite", lab=True)
                try:
                    result = asyncio.run(mint.mint(payload))
                    self.assertEqual(result, {"status": "material_ready"})
                    with store._read() as conn:
                        key = conn.execute("SELECT scope_key FROM material").fetchone()["scope_key"]
                    persisted = store.mint_load(key)
                    if len(delivered) == 1:
                        first_persisted = persisted
                    else:
                        self.assertEqual(persisted, first_persisted)
                finally:
                    store.close()
            self.assertEqual(len(calls), 1)
            self.assertEqual(len(delivered), 2)
            self.assertEqual(delivered[1], delivered[0])
            self.assertEqual(persisted, first_persisted)

    def test_entry_sentinel_context_is_restored_after_exception(self):
        sentinel = object()
        token = _GRANT_TTL_HINT.set(sentinel)
        scope, calls = _scope(), []
        mint = _build(_Store(scope), scope, calls)
        async def failing_approval(*_args, **_kwargs):
            _GRANT_TTL_HINT.set(3600)
            raise RuntimeError("native failure")
        mint.approved_scope_async = failing_approval
        async def same_task():
            self.assertIs(_GRANT_TTL_HINT.get(), sentinel)
            with self.assertRaises(RuntimeError):
                await mint.mint({"lobby_id": "lobby", "invite_id": "invite"})
            self.assertIs(_GRANT_TTL_HINT.get(), sentinel)
        try:
            asyncio.run(same_task())
        finally:
            self.assertIs(_GRANT_TTL_HINT.get(), sentinel)
            _GRANT_TTL_HINT.reset(token)


_MISSING = object()


class WireConsumerStrictnessTests(unittest.TestCase):
    def test_joining_consent_accepts_only_unsigned_hint_extra(self):
        scope = _scope()
        class Store:
            def confirmed_scope(self, _): return {k: v for k, v in scope.items() if k != "invite_id"}
            def get_consent(self, _): return None
            def begin_consent(self, *args, **kwargs): pass
            def mark_consent_delivered(self, _): pass
        service = JoiningOwnerService.__new__(JoiningOwnerService)
        service._store, service._channel, service._consent_key = Store(), object(), _Signer()
        service._call_count = 0
        def call(_request):
            service._call_count += 1
            return ({"status": "consent_required", "scope": scope, "grant_ttl_hint_seconds": 3600}
                    if service._call_count == 1 else {"status": "ready"})
        service._call = call
        service._owned_import = lambda _id: {"import_id": "import", "invite_id": "invite", "state": "redeemed"}
        try:
            self.assertEqual(service.consent("import", approve=True)["status"], "ready")
        except JoiningError as exc:
            self.fail(f"hint-bearing consent status was rejected: {exc}")
        for extra in ("unexpected", "grant_ttl_seconds"):
            service._call = lambda _request, key=extra: {"status": "consent_required", "scope": scope, key: 1}
            with self.assertRaisesRegex(JoiningError, "control_unavailable"):
                service.consent("import", approve=True)

    def test_runtime_remote_accepts_hint_but_rejects_missing_or_unknown_fields(self):
        owner = JoiningOwner.__new__(JoiningOwner)
        valid_scope = dict(_scope(), room_id="invite-room-test")
        owner.self_binding, owner.host_installation_id = valid_scope["binding"], "host"
        record = {"lobby_id": "lobby", "invite_id": "invite"}
        for response in ({"status": "ready", "scope": valid_scope, "grant_ttl_hint_seconds": 3600},
                         {"status": "ready"}, {"scope": valid_scope},
                         {"status": "ready", "scope": valid_scope, "x": 1}, None):
            owner.control = lambda *args, response=response, **kwargs: response
            with self.subTest(response=response):
                if response is not None and set(response) == {"status", "scope", "grant_ttl_hint_seconds"}:
                    try:
                        self.assertEqual(owner._remote(record)[0], "ready")
                    except JoiningError as exc:
                        self.fail(f"hint-bearing runtime status was rejected: {exc}")
                else:
                    with self.assertRaisesRegex(JoiningError, "control_unavailable"):
                        owner._remote(record)

    def test_product_mint_restores_entry_contextvar_when_approval_raises(self):
        scope, calls = _scope(), []
        mint = _build(_Store(scope), scope, calls)
        entry = object()
        token = _GRANT_TTL_HINT.set(entry)
        async def failing_approval(*_args, **_kwargs):
            _GRANT_TTL_HINT.set(3600)
            raise RuntimeError("native failure")
        mint.approved_scope_async = failing_approval
        try:
            with self.assertRaises(RuntimeError):
                asyncio.run(mint.mint({"lobby_id": "lobby", "invite_id": "invite"}))
            self.assertIs(_GRANT_TTL_HINT.get(), entry)
        finally:
            _GRANT_TTL_HINT.reset(token)


if __name__ == "__main__":
    unittest.main()
