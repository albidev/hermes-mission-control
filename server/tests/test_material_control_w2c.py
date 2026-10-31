import asyncio
import time
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


import pytest
from unittest.mock import patch

from invite_rooms.material_control import MaterialControl, MaterialControlChannel, HostConnectorMaterialTransport
from invite_rooms.joining_material import MaterialError
from invite_rooms.control.connector import HostConnector
from invite_rooms.control.firewall import FirewallDenied
from invite_rooms.control.dispatcher import ControlDispatcher, LinkState, ControlError
from invite_rooms.pairing_journal import MaterialPeerContext
from test_room_invite_joining import CATALOG


def test_internal_material_command_becomes_receipt_wire_and_ack():
    seen = []
    channel = MaterialControlChannel(lambda value: seen.append(value) or {"status": "material_ready"},
                                     lobby_id="lobby-1", invite_id="invite-1", lab=True)
    envelope = {"scope": {}, "grant": "g", "catalog": {}, "signature": "s"}
    assert channel.call({"op": "material", "invite_id": "invite-1", "envelope": envelope}) == {"status": "material_ready"}
    assert seen == [{"op": "material_receipt", "lobby_id": "lobby-1", "invite_id": "invite-1", "receipt": envelope}]


def test_material_channel_rejects_non_ready_ack():
    channel = MaterialControlChannel(lambda value: {"status": "queued"}, lobby_id="l", invite_id="i", lab=True)
    with pytest.raises(ValueError, match="control_unavailable"):
        channel.call({"op": "material", "invite_id": "i", "envelope": {}})


def test_request_origin_survives_host_restart_and_response_loss():
    """A durable origin, rather than process memory, owns retry identity."""
    sent = []
    transport = object.__new__(HostConnectorMaterialTransport)
    transport._connector = SimpleNamespace(binding=SimpleNamespace(endpoint_id="endpoint-1", generation=7))
    def send(wire, *, deadline, admit):
        sent.append(dict(wire))
        return {"status": "material_ready"}
    transport.send = send
    with tempfile.TemporaryDirectory() as root:
        from invite_rooms.pairing_journal import Journal
        journal = Journal(Path(root) / "journal", anchor=root)
        first = MaterialControl(lobby_id="lobby", invite_id="invite", connector=transport, journal=journal)
        assert asyncio.run(first.request()) == {"status": "material_ready"}
        assert journal.material_origin("endpoint-1", "lobby", "invite")["generation"] == 7
        assert not journal.consume_material_origin(
            endpoint_id="endpoint-1", generation=8, lobby_id="lobby", invite_id="invite",
            request_id=sent[0]["request_id"], expires_at_utc_ms=sent[0]["expires_at_utc_ms"])
        # Simulate process loss: a new MaterialControl has no in-memory origin.
        second = MaterialControl(lobby_id="lobby", invite_id="invite", connector=transport, journal=Journal(Path(root) / "journal", anchor=root))
        assert asyncio.run(second.request()) == {"status": "material_ready"}
    assert [(item["request_id"], item["expires_at_utc_ms"]) for item in sent] == [
        (sent[0]["request_id"], sent[0]["expires_at_utc_ms"]),
        (sent[0]["request_id"], sent[0]["expires_at_utc_ms"]),
    ]


def test_material_channel_rejects_callable_v2_sender_before_invocation():
    calls = []
    channel = MaterialControlChannel(lambda value: calls.append(value) or {"status": "material_ready"},
                                     lobby_id="l", invite_id="i")
    with pytest.raises(MaterialError, match="control_unavailable"):
        channel.call({"op": "material", "invite_id": "i", "request_id": "a" * 32,
                      "envelope": {}, "expires_at_utc_ms": 1, "probe": {},
                      "operation": {}, "operation_signature": "sig"})
    assert calls == []


def test_authenticated_peer_context_cannot_downgrade_when_request_meta_is_dropped():
    """The Sol R09 drop_meta shape fails before sender/store effects."""
    from invite_rooms.joining_material import TargetMaterialMint

    calls = []

    def arbitrary_sender(_wire, **_kwargs):
        calls.append("sender")
        return {"status": "material_ready"}

    class Store:
        def __getattr__(self, name):
            def unexpected(*_args, **_kwargs):
                calls.append(name)
                raise AssertionError("authenticated downgrade reached store")
            return unexpected

    class Signer:
        def sign(self, _payload):
            return "signature"

    class AuthenticatedContext:
        operation = "material_request"
        endpoint_id = "peer"
        generation = 4
        deadline = time.monotonic() + 1

        def readmit(self):
            return True

    channel = MaterialControlChannel(arbitrary_sender, lobby_id="lobby", invite_id="invite")
    mint = TargetMaterialMint(Store(), consent_key=Signer(), channel=channel,
                              rpc=arbitrary_sender, host_peer_id="peer:4")
    with pytest.raises(MaterialError, match="control_unavailable"):
        asyncio.run(mint.mint({"lobby_id": "lobby", "invite_id": "invite"},
                              deadline=AuthenticatedContext.deadline,
                              peer_context=AuthenticatedContext(), request_meta=None))
    assert calls == []


def test_host_connector_enforces_literal_false_material_admission():
    connector = object.__new__(HostConnector)
    with pytest.raises(FirewallDenied, match="room request denied"):
        connector._check(lambda: False)


def test_async_channel_uses_inherited_deadline_when_unconfigured():
    """A delayed connector must not get a fresh ten-second budget."""
    async def run():
        def delayed(_wire):
            time.sleep(0.2)
            return {"status": "ready"}

        channel = MaterialControlChannel(delayed, lobby_id="lobby", invite_id="invite")
        with pytest.raises(MaterialError, match="control_unavailable"):
            await channel.call_async(
                {"op": "status", "invite_id": "invite"},
                deadline=time.monotonic() + 0.04,
            )

    asyncio.run(run())


def test_async_channel_rejects_opaque_sync_facade_before_late_effect():
    """A timed-out request must not leave an unbounded sender worker alive."""
    effects = []

    async def run():
        def opaque(_wire):
            effects.append("sent")
            return {"status": "ready"}

        channel = MaterialControlChannel(opaque, lobby_id="lobby", invite_id="invite")
        with pytest.raises(MaterialError, match="control_unavailable"):
            await channel.call_async(
                {"op": "material", "invite_id": "invite", "envelope": {}},
                deadline=time.monotonic() + 0.04,
            )

    asyncio.run(run())
    assert effects == []


def test_deadline_capable_sync_facade_rechecks_before_effect():
    """The explicit capability receives the inherited deadline and admission."""
    effects = []

    async def run():
        def capable(_wire, *, deadline, admit):
            time.sleep(0.06)
            if time.monotonic() >= deadline or admit() is not True:
                raise MaterialError("control_unavailable")
            effects.append("sent")
            return {"status": "ready"}

        channel = MaterialControlChannel(capable, lobby_id="lobby", invite_id="invite")
        with pytest.raises(MaterialError, match="control_unavailable"):
            await channel.call_async(
                {"op": "material", "invite_id": "invite", "envelope": {}},
                deadline=time.monotonic() + 0.04,
            )

    asyncio.run(run())
    assert effects == []


def test_approved_scope_propagates_inherited_deadline_to_every_store_lookup():
    """Pre-mint SQLite lookups must receive the request's absolute deadline."""
    from invite_rooms.joining_material import TargetMaterialMint

    deadline = time.monotonic() + 5
    scope = {"host_id": "host", "room_id": "room", "member_id": "member",
             "lobby_id": "lobby", "invite_id": "invite", "binding": {}}
    seen = []

    class Store:
        def find_import(self, lobby_id, invite_id, *, deadline):
            seen.append(("find_import", deadline))
            return {"import_id": "import-1", "host_peer_id": "host-peer"}

        def confirmed_scope(self, import_id, *, deadline):
            seen.append(("confirmed_scope", deadline))
            return scope

        def get_consent(self, import_id, *, deadline):
            seen.append(("get_consent", deadline))
            return {"receipt": dict(scope, purpose="joining-owner-consent-v1")}

    class Channel:
        def call(self, request):
            return {"status": "ready", "scope": scope}

    mint = TargetMaterialMint(Store(), consent_key=None, channel=Channel(), rpc=None,
                              host_peer_id="host-peer")
    result = asyncio.run(mint.approved_scope_async(
        {"lobby_id": "lobby", "invite_id": "invite"}))
    assert result == scope
    assert seen == [("find_import", None), ("confirmed_scope", None),
                    ("get_consent", None)]


def test_request_bound_mint_rejects_kwargs_ignoring_native_rpc():
    """The effect boundary must require an actual admission-capable RPC."""
    from invite_rooms.joining_material import TargetMaterialMint, MintAmbiguous

    scope = {"host_id": "host", "room_id": "room", "member_id": "member",
             "lobby_id": "lobby", "invite_id": "invite", "binding": {"target_profile": "default"}}

    class Store:
        def mint_state(self, _key, **_kwargs):
            return None
        def mint_begin(self, _key, **_kwargs):
            return "grant-id"
        def mint_complete(self, *_args, **_kwargs):
            raise AssertionError("must not complete an unbound native effect")

    class Signer:
        def sign(self, _payload):
            return "signature"

    native_calls = []
    async def kwargs_ignoring_rpc(method, params, **_kwargs):
        native_calls.append((method, params))
        return {"grant": "g", "target_profile": "default",
                "catalog": {"endpoint": {}}, "endpoint": {}}

    mint = TargetMaterialMint(Store(), consent_key=Signer(), channel=None,
                              rpc=kwargs_ignoring_rpc, host_peer_id="host-peer")
    async def approved_scope(_payload, **_kwargs):
        return scope
    mint.approved_scope_async = approved_scope

    class Context:
        def readmit(self):
            return True

    with patch("invite_rooms.joining_material.validate_material"):
        with pytest.raises(MintAmbiguous):
            asyncio.run(mint.mint({"lobby_id": "lobby", "invite_id": "invite"},
                                  deadline=time.monotonic() + 1,
                                  final_admission=Context().readmit))
    assert native_calls == []


class Authority:
    def current(self, endpoint):
        return LinkState({"endpoint_id": endpoint}, 3, "acknowledged")

    def admit(self, endpoint, generation, operation):
        return True

    def authenticate_material_peer(self, expected, operation, deadline=None):
        return MaterialPeerContext._issue(lambda: True, endpoint_id=expected.endpoint_id, generation=3,
                                          role="joining", protocol_state="acknowledged", peer_id="p",
                                          binding=(), ca_pem="ca", fingerprint="fp", consent_public_key=b"k" * 32,
                                          operation=operation, deadline=deadline or time.monotonic() + 1)


def test_material_lane_requires_exact_wire_shape_and_uses_context():
    calls = []
    async def run():
        dispatcher = ControlDispatcher(Authority(), handlers={}, direction="A_to_B",
                                        material_handlers={"material_request": lambda c, r: calls.append(r) or {"status": "material_ready"}},
                                        allow_legacy_material=True)
        assert await dispatcher.dispatch("peer", 3, {"op": "material_request", "lobby_id": "l", "invite_id": "i"},
                                         expected_trust=type("Trust", (), {"endpoint_id": "peer"})()) == {"status": "material_ready"}
        with pytest.raises(ControlError):
            await dispatcher.dispatch("peer", 3, {"op": "material_request", "lobby_id": "l", "invite_id": "i", "extra": 1})
        await dispatcher.close()
    asyncio.run(run())
    assert calls == [{"op": "material_request", "lobby_id": "l", "invite_id": "i"}]


class JournalBackedReceiptStatusTest(unittest.TestCase):
    def test_receipt_requires_owner_scope_ready_before_registry_admission(self):
        from invite_rooms.material_control import MaterialControl

        class Scope:
            owner_installation_id = "owner-installation-12345678901234567890123456789012"
            planned_room_id = "room-12345678901234567890123456789012"
            member_id = "member-12345678901234567890123456789012"
            lobby_id = "lobby-12345678901234567890123456789012"
            invite_id = "invite-12345678901234567890123456789012"
            binding = {"endpoint_id": "endpoint-12345678901234567890123456789012",
                       "fingerprint": "a" * 64,
                       "target_install_id": "target-install-12345678901234567890123456789012",
                       "target_profile": "default", "catalog_digest": "b" * 64,
                       "policy_digest": "c" * 64}

        class Context:
            deadline = time.monotonic() + 2
            role = "host"
            operation = "material_receipt"
            endpoint_id = Scope.binding["endpoint_id"]
            generation = 1
            def readmit(self):
                return True

        envelope = {"scope": {"host_id": Scope.owner_installation_id, "room_id": Scope.planned_room_id,
                               "member_id": Scope.member_id, "lobby_id": Scope.lobby_id,
                               "invite_id": Scope.invite_id, "binding": Scope.binding},
                    "grant": "grant", "catalog": {}, "signature": "signature"}
        calls = []
        class Registry:
            async def admit_signed_material_async(self, scope, received):
                calls.append((scope, received))

        pending = MaterialControl(
            receipt_resolver=lambda **_: (Registry(), Scope()),
            scope_status_resolver=lambda **_: {"status": "consent_required", "scope": envelope["scope"]})
        with self.assertRaises(ControlError):
            asyncio.run(pending.receipt_handler(Context(), {"op": "material_receipt", "lobby_id": Scope.lobby_id,
                                                            "invite_id": Scope.invite_id, "receipt": envelope}))
        self.assertEqual(calls, [])

        ready = MaterialControl(
            receipt_resolver=lambda **_: (Registry(), Scope()),
            scope_status_resolver=lambda **_: {"status": "ready", "scope": envelope["scope"]})
        asyncio.run(ready.receipt_handler(Context(), {"op": "material_receipt", "lobby_id": Scope.lobby_id,
                                                       "invite_id": Scope.invite_id, "receipt": envelope}))
        self.assertEqual(len(calls), 1)


class SharedHandlerScopeResolutionTest(unittest.TestCase):
    def test_one_target_handler_resolves_two_scopes_from_authenticated_request(self):
        """The handler map is shared; only the trusted resolver selects the scope."""
        seen = []

        class Context:
            deadline = time.monotonic() + 2
            def readmit(self):
                return True

        class Mint:
            def __init__(self, scope):
                self.scope = scope
            async def mint(self, request, **_kwargs):
                seen.append((self.scope, dict(request)))
                return {"status": "material_ready"}

        mints = {
            ("lobby-a", "invite-a"): Mint("a"),
            ("lobby-b", "invite-b"): Mint("b"),
        }

        async def resolve(*, request, **_kwargs):
            return mints[request["lobby_id"], request["invite_id"]]

        control = MaterialControl(target_resolver=resolve)
        handlers = control.handlers()
        self.assertEqual(awaitable_run(handlers["material_request"](Context(), {
            "op": "material_request", "lobby_id": "lobby-a", "invite_id": "invite-a"})),
            {"status": "material_ready"})
        self.assertEqual(awaitable_run(handlers["material_request"](Context(), {
            "op": "material_request", "lobby_id": "lobby-b", "invite_id": "invite-b"})),
            {"status": "material_ready"})
        self.assertEqual([(scope, request["lobby_id"], request["invite_id"])
                          for scope, request in seen],
                         [("a", "lobby-a", "invite-a"), ("b", "lobby-b", "invite-b")])


def awaitable_run(awaitable):
    return asyncio.run(awaitable)


class MaterialControlAdversarialMatrixTest(unittest.TestCase):
    """Table-driven boundary proof for the product material lane.

    These cases intentionally stay at the dispatcher/control boundary: malformed
    wire data must be rejected before a handler, registry, or native adapter can
    observe it.  The native two-listener happy path is covered by the mTLS module.
    """

    def test_wire_shape_matrix_has_one_fixed_denial_and_no_handler_effect(self):
        good = {"op": "material_request", "lobby_id": "l", "invite_id": "i"}
        cases = {
            "extra_native_method": {**good, "method": "groups.peer.invite"},
            "extra_url": {**good, "url": "http://evil"},
            "extra_grant": {**good, "grant_id": "g"},
            "missing_lobby": {k: v for k, v in good.items() if k != "lobby_id"},
            "missing_invite": {k: v for k, v in good.items() if k != "invite_id"},
            "wrong_op": {**good, "op": "material_receipt"},
            "non_string_lobby": {**good, "lobby_id": 1},
            "empty_invite": {**good, "invite_id": ""},
        }
        effects = []
        for label, request in cases.items():
            with self.subTest(label=label):
                with self.assertRaises(ControlError) as error:
                    ControlDispatcher._request(request)
                self.assertEqual(str(error.exception), "control_unavailable")
                self.assertEqual(effects, [])

    def test_material_receipt_type_and_envelope_matrix_is_side_effect_free(self):
        class Scope:
            owner_installation_id = "owner"
            planned_room_id = "room"
            member_id = "member"
            lobby_id = "lobby"
            invite_id = "invite"
            binding = {"endpoint_id": "endpoint", "fingerprint": "f" * 64,
                       "target_install_id": "target", "target_profile": "default",
                       "catalog_digest": "c" * 64, "policy_digest": "p" * 64}

        class Context:
            deadline = time.monotonic() + 2
            role = "host"
            operation = "material_receipt"
            endpoint_id = "endpoint"
            generation = 1
            def readmit(self):
                return True

        calls = []
        class Registry:
            async def admit_signed_material_async(self, scope, receipt):
                calls.append((scope, receipt))

        valid_scope = {"host_id": Scope.owner_installation_id, "room_id": Scope.planned_room_id,
                       "member_id": Scope.member_id, "lobby_id": Scope.lobby_id,
                       "invite_id": Scope.invite_id, "binding": Scope.binding}
        valid = {"scope": valid_scope, "grant": "g", "catalog": {}, "signature": "s"}
        control = MaterialControl(lobby_id="lobby", invite_id="invite",
                                  receipt_resolver=lambda **_: (Registry(), Scope()),
                                  scope_status_resolver=lambda **_: {"status": "ready", "scope": valid_scope})
        cases = [
            {"op": "material_receipt", "lobby_id": "lobby", "invite_id": "invite", "receipt": "not-a-dict"},
            {"op": "material_receipt", "lobby_id": "lobby", "invite_id": "invite", "receipt": None},
            {"op": "material_receipt", "lobby_id": "other", "invite_id": "invite", "receipt": valid},
            {"op": "material_receipt", "lobby_id": "lobby", "invite_id": "other", "receipt": valid},
            {"op": "material_receipt", "lobby_id": "lobby", "invite_id": "invite", "receipt": {**valid, "scope": {}}},
        ]
        for request in cases:
            with self.subTest(request=request):
                with self.assertRaises(ControlError) as error:
                    asyncio.run(control.receipt_handler(Context(), request))
                self.assertEqual(str(error.exception), "control_unavailable")
                self.assertEqual(calls, [])

    def test_channel_ack_and_internal_command_matrix(self):
        envelope = {"scope": {}, "grant": "g", "catalog": {}, "signature": "s"}
        for reply in ({"status": "queued"}, {"status": "material_ready", "extra": 1}, None, "ok"):
            with self.subTest(reply=reply):
                channel = MaterialControlChannel(lambda _request, reply=reply: reply,
                                                 lobby_id="lobby", invite_id="invite", lab=True)
                with self.assertRaisesRegex(MaterialError, "control_unavailable"):
                    channel.call({"op": "material", "invite_id": "invite", "envelope": envelope})

    def test_direction_and_protocol_state_matrix_refuses_before_effect(self):
        class Authority:
            def __init__(self, state):
                self.state = state
            def current(self, _endpoint):
                return self.state
            def authenticate_material_peer(self, expected, operation, deadline=None):
                return MaterialPeerContext._issue(lambda: True, endpoint_id="peer", generation=3,
                    role="joining", protocol_state="acknowledged", peer_id="p", binding=(), ca_pem="ca",
                    fingerprint="fp", consent_public_key=b"k" * 32, operation=operation,
                    deadline=deadline or time.monotonic() + 1)

        effects = []
        async def handler(_context, _request):
            effects.append(True)
            return {"status": "material_ready"}
        async def run():
            for state, direction in (("pending", "A_to_B"), ("confirmed", "A_to_B"),
                                     ("acknowledged", "B_to_A")):
                with self.subTest(state=state, direction=direction):
                    dispatcher = ControlDispatcher(
                        Authority(LinkState({"endpoint_id": "peer"}, 3, state)), handlers={},
                        direction=direction, material_handlers={"material_request": handler})
                    try:
                        with self.assertRaises(ControlError) as error:
                            await dispatcher.dispatch("peer", 3,
                                {"op": "material_request", "lobby_id": "l", "invite_id": "i"},
                                expected_trust=type("Trust", (), {"endpoint_id": "peer"})())
                        self.assertIn(str(error.exception), {"control_unavailable", "forbidden"})
                    finally:
                        await dispatcher.close()
            self.assertEqual(effects, [])
        asyncio.run(run())


class PostCommitTransportLossRetryTests(unittest.TestCase):
    """R07 retry proofs: transport loss is not a second mint or admission.

    The mTLS listener tests exercise the real socket boundary.  These tests keep
    the durable retry invariant small and deterministic: the first connector
    call commits, then loses only its reply; the second call must replay the
    exact persisted envelope (or durable ready response).
    """

    def test_target_mint_replays_immutable_envelope_after_receipt_reply_loss(self):
        from invite_rooms.joining_material import TargetMaterialMint, MaterialError

        scope = {
            "host_id": "host", "room_id": "room", "member_id": "member",
            "lobby_id": "lobby", "invite_id": "invite",
            "binding": {"endpoint_id": "endpoint", "fingerprint": "f" * 64,
                        "target_install_id": CATALOG["installation_id"], "target_profile": "default",
                        "catalog_digest": CATALOG["catalog_digest"],
                        "policy_digest": CATALOG["execution_policy"]["policy_digest"]},
        }
        envelope = {"scope": scope, "grant": "grant", "catalog": {"endpoint": {"url": "u"}},
                    "signature": "signature"}

        class Store:
            def __init__(self):
                self.state, self.saved, self.completes = {}, {}, 0
            def find_import(self, lobby, invite):
                return {"import_id": "import", "host_peer_id": "host-peer"}
            def confirmed_scope(self, _import):
                return scope
            def get_consent(self, _import):
                return {"receipt": dict(scope, purpose="joining-owner-consent-v1")}
            def mint_state(self, key):
                return self.state.get(key)
            def mint_begin(self, key, **_kwargs):
                self.state[key] = "intent"
                return "grant-id"
            def mint_complete(self, key, raw):
                self.state[key], self.saved[key] = "ready", raw
                self.completes += 1
            def mint_load(self, key):
                return self.saved[key]

        class Signer:
            def sign(self, _payload):
                return "signature"

        store, native_calls, wire = Store(), [], []
        drop = {"value": True}
        def connector(request, *, deadline=None, admit=None):
            wire.append(request)
            if request["op"] == "status":
                return {"status": "ready", "scope": scope}
            if drop["value"]:
                drop["value"] = False
                raise OSError("reply dropped after durable receipt commit")
            return {"status": "material_ready"}

        channel = MaterialControlChannel(connector, lobby_id="lobby", invite_id="invite", lab=True)
        async def rpc(method, params, **_kwargs):
            native_calls.append((method, params))
            import base64, json, time
            now = time.time()
            claims = {"room_id": scope["room_id"], "home_install_id": scope["host_id"],
                      "authority_gateway_id": scope["host_id"], "authority_epoch": 1,
                      "member_id": scope["member_id"],
                      "target_install_id": scope["binding"]["target_install_id"],
                      "target_profile": "default", "issued_at": now, "expires_at": now + 600,
                      "status_expires_at": now + 600,
                      "execution_policy_digest": scope["binding"]["policy_digest"]}
            encoded = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
            grant = encoded + "." + base64.urlsafe_b64encode(b"x" * 32).rstrip(b"=").decode()
            return {"grant": grant, "target_profile": "default", "catalog": CATALOG,
                    "endpoint": CATALOG["endpoint"]}

        mint = TargetMaterialMint(store, consent_key=Signer(), channel=channel,
                                  rpc=rpc, host_peer_id="host-peer")
        with self.assertRaisesRegex(MaterialError, "control_unavailable"):
            asyncio.run(mint.mint({"lobby_id": "lobby", "invite_id": "invite"}))
        key = __import__("invite_rooms.consent", fromlist=["canonical"]).canonical(scope).decode()
        persisted = store.saved[key]
        self.assertEqual(store.completes, 1)
        self.assertEqual(len(native_calls), 1)
        self.assertEqual(asyncio.run(mint.mint({"lobby_id": "lobby", "invite_id": "invite"})),
                         {"status": "material_ready"})
        self.assertEqual(store.saved[key], persisted)
        self.assertEqual(store.completes, 1)
        self.assertEqual(len(native_calls), 1)
        self.assertEqual(wire[-1]["receipt"], __import__("json").loads(persisted))

    def test_outer_material_request_retry_reuses_durable_ready_without_remint(self):
        calls, committed = [], {"value": False}
        def connector(request, *, deadline=None, admit=None):
            calls.append(request)
            if not committed["value"]:
                committed["value"] = True
                from invite_rooms.joining_material import MaterialError
                raise MaterialError("control_unavailable")
            return {"status": "material_ready"}

        control = MaterialControl(lobby_id="lobby", invite_id="invite", connector=connector,
                                  request_bound=False)
        with self.assertRaisesRegex(Exception, "control_unavailable"):
            asyncio.run(control.request())
        self.assertEqual(asyncio.run(control.request()), {"status": "material_ready"})
        self.assertEqual(calls, [
            {"op": "material_request", "lobby_id": "lobby", "invite_id": "invite"},
            {"op": "material_request", "lobby_id": "lobby", "invite_id": "invite"},
        ])
