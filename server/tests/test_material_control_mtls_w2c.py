"""Post-hoc R02 behavioural proof: material over two real loopback mTLS listeners.

This module intentionally uses the retained pairing-support owner fixtures only to
create real journals/identity bundles.  The acceptance path is ControlListener ->
TLS -> MaterialControl -> TargetMaterialMint -> HostConnector -> ControlListener;
no handler is called directly and no context is fabricated.
"""
from __future__ import annotations

import asyncio
import base64
import copy
import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import aiohttp
from aiohttp import web

import pairing_support as ps
import test_control_listener_w2a as w2a
from invite_rooms import identity as identity_module
from invite_rooms.control.connector import ForwardedRequest, HostConnector, LinkBinding
from invite_rooms.control.listener import ControlListener
import invite_rooms.control.listener as listener_module
from invite_rooms.control.framing import SequencedTLS
from invite_rooms.control.connector import decode_response, recv_message, send_message
from invite_rooms.control import tls as tls_module
from invite_rooms.consent import canonical
from invite_rooms.joining_material import MaterialError, TargetMaterialMint
from invite_rooms.joining_store import JoiningStore
from invite_rooms.material_control import (MaterialControl, MaterialControlChannel,
                                            HostConnectorMaterialTransport)
from invite_rooms.material_receipts import MaterialReceiptStore
from invite_rooms.native_rpc import BoundedNativeRpc
from invite_rooms.peer_registry import MaterialScope, PeerRegistry
from invite_rooms.pairing_journal import JournalLinkAuthority
from invite_rooms.host_control import build_host_handlers
from invite_rooms.service import OwnerContext

from test_room_invite_joining import CATALOG, HOST_INSTALL, SELF_BINDING, _invitation_text
from test_room_invite_material_receipts import authoritative_source, raw_rows
from test_room_invite_routes import CA_PEM

INSTALL_A, INSTALL_B = ps.INSTALL_A, ps.INSTALL_B
LOBBY = "lobby-" + "1" * 32
INVITE = "invite-" + "2" * 32


class _JournalConsentSigner:
    """ConsentKey-compatible signer backed by the paired B identity key."""
    def __init__(self, private_key, public_bytes):
        self._private_key = private_key
        self.public_bytes = public_bytes

    def sign(self, payload):
        return base64.b64encode(self._private_key.sign(canonical(payload))).decode()


def _wire(port, *, client, server, endpoint_id, generation, request):
    ctx = tls_module.client_context(ca_data=server.ca_pem,
                                    cert_file=str(client.leaf_cert_file), key_file=str(client.leaf_key_file))
    sock = __import__("socket").create_connection(("127.0.0.1", port), timeout=4)
    tls = tls_module.wrap_client(sock, ctx, expected_fingerprint=server.leaf_fingerprint)
    try:
        frames = SequencedTLS(tls, deadline=time.monotonic() + 4)
        envelope = {"endpoint_id": endpoint_id, "generation": generation,
                    "request": {"method": "POST", "path": "/control/joining", "headers": [],
                                "body": base64.b64encode(json.dumps(request, sort_keys=True,
                                                                     separators=(",", ":")).encode()).decode()}}
        send_message(frames, json.dumps(envelope, separators=(",", ":")).encode())
        response = decode_response(recv_message(frames))
        return response.status, json.loads(response.body)
    finally:
        tls.close()


class RealTwoListenerMaterialRoundtrip(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.tmp.chmod(0o700)
        self.addCleanup(shutil.rmtree, self.tmp)
        self.clock = ps.Clock(time.time())
        self.a_side = ps.Side(self.tmp, "a", INSTALL_A, template=ps.identity_template(INSTALL_A))
        self.b_side = ps.Side(self.tmp, "b", INSTALL_B, template=ps.identity_template(INSTALL_B))
        self.a = self.a_side.service(self.clock)
        self.b = self.b_side.service(self.clock)
        self.paired = ps.drive_pairing(self.a, self.b)
        # Promote the real journal link on both owners before installing data handlers.
        ack_request = self.a.ack_request(self.paired["a_id"])
        self.a.complete_ack(self.paired["a_id"], self.b.handle_pair_ack(ack_request.request))
        self.a_bundle = identity_module.load_identity(self.a_side.identity_dir, installation_id=INSTALL_A, profile="default")
        self.b_bundle = identity_module.load_identity(self.b_side.identity_dir, installation_id=INSTALL_B, profile="default")
        self.listeners = []
        self.addCleanup(self.close_listeners)

    def close_listeners(self):
        for listener in self.listeners:
            try:
                listener.close()
            except Exception:
                pass

    def _approved_joining_fixture(self, peer_id, binding, label="single"):
        # Real JoiningStore + signed owner consent; only the old host-side fixture
        # is used to seed the durable invitation state.
        from test_room_invite_joining import make_fixture
        fx = make_fixture(self.tmp / ("joining-material-" + label))
        # The target envelope must be signed by the journal-pinned B identity,
        # not by the legacy helper's unrelated scratch key.
        fx["consent_key"] = _JournalConsentSigner(self.b_bundle.consent_key(),
                                                   self.b_bundle.consent_public_key)
        fx["channel"].binding = dict(binding)
        fx["service"]._self_binding = dict(binding)
        # Bind the durable H3 store to the same owner represented by the
        # journal-backed listener, and make the roster peer the authenticated
        # journal peer (the old lab helper intentionally used a fixture id).
        fx["store"].bind_owner_scope(HOST_INSTALL, "default")
        with fx["store"]._write() as conn:
            roster = json.loads(conn.execute("SELECT roster_json FROM lobbies WHERE lobby_id=?",
                                             (fx["lobby"]["lobby_id"],)).fetchone()[0])
            roster[1]["target"]["peer_id"] = peer_id
            conn.execute("UPDATE lobbies SET roster_json=? WHERE lobby_id=?",
                         (json.dumps(roster), fx["lobby"]["lobby_id"]))
        text = _invitation_text(fx["lobby"]["lobby_id"], fx["invite"].invite_id, fx["invite"].secret)
        preview = fx["service"].preview(text)
        fx["service"].redeem(preview["import_id"])
        fx["store"].decide_host(fx["lobby"]["lobby_id"], fx["invite"].invite_id,
                                binding=binding, approved=True)
        fx["service"].confirm_scope(preview["import_id"], room_id=fx["lobby"]["planned_room_id"], member_id="peer-b")
        fx["service"].consent(preview["import_id"], approve=True)
        return fx, {"lobby_id": fx["lobby"]["lobby_id"], "invite_id": fx["invite"].invite_id}

    def test_material_request_and_receipt_cross_two_real_listeners(self):
        endpoint = next(iter(self.a.journal.snapshot().links))
        link = self.a.journal.snapshot().links[endpoint]
        fx, payload = self._approved_joining_fixture(link["peer"]["peer_id"], {
            "endpoint_id": endpoint, "fingerprint": link["peer"]["fingerprint"],
            "target_install_id": link["peer"]["installation_id"], "target_profile": link["peer"]["profile"],
            "catalog_digest": link["peer"]["catalog_digest"], "policy_digest": link["peer"]["policy_digest"]})
        authority_a = JournalLinkAuthority(self.a.journal)
        authority_b = JournalLinkAuthority(self.b.journal)
        link = self.a.journal.snapshot().links[endpoint]
        generation = self.a.journal.snapshot().link_generation(link)
        with fx["joining_store"]._write() as conn:
            conn.execute("UPDATE imports SET host_peer_id=?", (f"{endpoint}:{generation}",))
        trust_a = authority_a.peer_trust(endpoint, generation, role="host")
        self.assertIsNotNone(trust_a)
        scope_wire = fx["channel"]._scope(payload["invite_id"], fx["store"].get_redemption(payload["lobby_id"], payload["invite_id"]))
        scope = MaterialScope(owner_installation_id=scope_wire["host_id"], owner_profile="default",
                              lobby_id=scope_wire["lobby_id"], planned_room_id=scope_wire["room_id"],
                              member_id=scope_wire["member_id"], invite_id=scope_wire["invite_id"],
                              peer_id=trust_a.peer_id, pairing_generation=1, **scope_wire["binding"])

        # Host admission is the real immutable receipt path, not a callback/list
        # sink; the registry, journal authority, source and encrypted receipt
        # store are all real scratch-backed components.
        host_receipts = MaterialReceiptStore(self.tmp / "host-material" / "receipts")
        host_registry = PeerRegistry(receipt_store=host_receipts, route_provider=lambda _scope: 19001,
                                     authority=authority_a)
        host_registry.register(peer_id=trust_a.peer_id, installation_id=scope.target_install_id,
                               profile=scope.target_profile, fingerprint=scope.fingerprint,
                               endpoint_id=scope.endpoint_id, catalog=copy.deepcopy(CATALOG),
                               consent_public_key=self.b_bundle.consent_public_key, tls_ca_pem=CA_PEM,
                               pairing_generation=scope.pairing_generation)
        host_registry._source = authoritative_source(
            self.tmp / "host-material", peer_id=trust_a.peer_id, installation_id=scope.target_install_id,
            profile=scope.target_profile, fingerprint=scope.fingerprint, endpoint_id=scope.endpoint_id,
            catalog=CATALOG, consent_public_key=self.b_bundle.consent_public_key,
            tls_ca_pem=CA_PEM)
        registry_errors = []
        _admit = host_registry.admit_signed_material_async
        async def admitting(scope_arg, envelope_arg):
            try:
                return await _admit(scope_arg, envelope_arg)
            except Exception as exc:
                registry_errors.append(type(exc).__name__ + ":" + str(exc))
                raise
        host_registry.admit_signed_material_async = admitting

        def host_scope(context=None, request=None, deadline=None):
            # Ask the accepted H3 handler with the authenticated MaterialPeerContext
            # immediately before receipt admission.  The wire envelope never selects
            # owner, peer or generation.
            status = h3_handlers["status"](context, {"op": "status", **payload})
            self.assertEqual(status, {"status": "ready", "scope": scope_wire})
            bound = status["scope"]
            resolved = MaterialScope(owner_installation_id=bound["host_id"], owner_profile="default",
                                     lobby_id=bound["lobby_id"], planned_room_id=bound["room_id"],
                                     member_id=bound["member_id"], invite_id=bound["invite_id"],
                                     peer_id=context.peer_id, pairing_generation=context.generation,
                                     **bound["binding"])
            return host_registry, resolved

        # H3 is the source of truth for status.  The material resolver below
        # uses the authenticated peer context and the same durable store, not
        # a canned status callback.
        h3_handlers = build_host_handlers(fx["store"], OwnerContext(HOST_INSTALL, "default"))
        # The host-side receipt validator must use the same durable origin
        # journal as the requester. This makes the retry below survive a real
        # control-object restart instead of relying on process-local state.
        host_control = MaterialControl(receipt_resolver=host_scope, journal=self.a.journal)
        receipt_errors = []
        receipt_context = []
        async def receipt_handler(context, request):
            try:
                receipt_context.append((context.role, context.operation, context.endpoint_id,
                                        context.generation, context.readmit()))
                return await host_control.receipt_handler(context, request)
            except Exception as exc:
                receipt_errors.append(type(exc).__name__ + ":" + str(exc))
                raise

        a_listener = ControlListener(
            host="127.0.0.1", port=w2a.free_port(), authority=authority_a,
            identity=lambda: (str(self.a_bundle.leaf_cert_file), str(self.a_bundle.leaf_key_file)),
            pair_ack_handler=lambda request: self.a.handle_pair_ack(request),
            host_handlers={"status": h3_handlers["status"]},
            material_handlers={"material_receipt": receipt_handler}, timeout=4.0)
        self.listeners.append(a_listener)
        a_listener.start()

        trust_b = authority_b.peer_trust(endpoint, generation, role="joining")
        self.assertIsNotNone(trust_b)
        binding = dict(trust_b.binding_dict())
        connector = HostConnector(
            binding=LinkBinding(endpoint, generation, a_listener.address, None,
                                trust_b.fingerprint, binding, peer_ca_data=trust_b.ca_pem),
            identity=type("Identity", (), {"cert_file": str(self.b_bundle.leaf_cert_file),
                                            "key_file": str(self.b_bundle.leaf_key_file)})(),
            authority=authority_b)
        # B->A uses a real HostConnector on every material_receipt call.
        def b_to_a(request, *, deadline=None, admit=None):
            raw = json.dumps(request, separators=(",", ":")).encode()
            result = connector.forward(ForwardedRequest("POST", "/control/joining", (), raw),
                                       admit=admit, deadline=deadline)
            return json.loads(result.body)

        channel = MaterialControlChannel(HostConnectorMaterialTransport(connector),
                                         lobby_id=payload["lobby_id"], invite_id=payload["invite_id"])
        native_calls = []

        async def native(method, params, invite_admission=None, deadline=None):
            # This is a real websocket boundary.  The responder is deliberately
            # dumb: it records the canonical request and returns a deterministic
            # native result; it cannot inject method, URL, grant or target fields.
            app = web.Application()
            async def native_handler(request):
                ws = web.WebSocketResponse()
                await ws.prepare(request)
                async for frame in ws:
                    message = json.loads(frame.data)
                    native_calls.append(message)
                    now = time.time()
                    claims = {"room_id": scope.planned_room_id, "home_install_id": scope.owner_installation_id,
                              "authority_gateway_id": scope.owner_installation_id, "authority_epoch": 1,
                              "member_id": scope.member_id, "target_install_id": SELF_BINDING["target_install_id"],
                              "target_profile": "default", "issued_at": now, "expires_at": now + 600,
                              "status_expires_at": now + 600, "execution_policy_digest": SELF_BINDING["policy_digest"]}
                    encoded = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
                    grant = encoded + "." + base64.urlsafe_b64encode(b"x" * 32).rstrip(b"=").decode()
                    await ws.send_json({"jsonrpc": "2.0", "id": message["id"], "result": {
                        "target_profile": "default", "endpoint": CATALOG["endpoint"],
                        "catalog": CATALOG, "grant": grant}})
                return ws
            app.router.add_get("/api/ws", native_handler)
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, "127.0.0.1", 0)
            await site.start()
            port = site._server.sockets[0].getsockname()[1]
            try:
                async with aiohttp.ClientSession(trust_env=False) as session:
                    async with session.ws_connect(f"http://127.0.0.1:{port}/api/ws") as ws:
                        return await BoundedNativeRpc(
                            ws, timeout=2, invite_admission=invite_admission
                        )(method, params, deadline=deadline)
            finally:
                await runner.cleanup()

        def bind_native_request(*, invite_admission, deadline):
            async def bound(method, params):
                return await native(method, params, invite_admission=invite_admission, deadline=deadline)
            bound._request_bound_native_rpc = True
            return bound
        native.bind_request = bind_native_request

        mint = TargetMaterialMint(fx["joining_store"], consent_key=fx["consent_key"],
                                 channel=channel, rpc=native, host_peer_id=f"{endpoint}:{generation}")
        target = MaterialControl(target_resolver=lambda **_: mint)
        target_handler_errors = []
        target_requests = []
        async def target_handler(context, request):
            try:
                target_requests.append(copy.deepcopy(request))
                return await target.target_handler(context, request)
            except Exception as exc:
                target_handler_errors.append(type(exc).__name__ + ":" + str(exc))
                raise
        b_listener = ControlListener(
            host="127.0.0.1", port=w2a.free_port(), authority=authority_b,
            identity=lambda: (str(self.b_bundle.leaf_cert_file), str(self.b_bundle.leaf_key_file)),
            pair_ack_handler=lambda request: self.b.handle_pair_ack(request),
            material_handlers={"material_request": target_handler}, timeout=4.0)
        self.listeners.append(b_listener)
        b_listener.start()

        self.assertEqual(asyncio.run(mint.approved_scope_async(
            payload, deadline=time.monotonic() + 2)), scope_wire)
        # The first A->B request is a real TLS connection and the B-side handler
        # executes the real TargetMaterialMint. Drive it through the product
        # requester (not a direct dispatcher/helper call); its receipt is a
        # separate real TLS connection back to A.
        requester_connector = HostConnector(
            binding=LinkBinding(endpoint, generation, b_listener.address, None,
                                trust_a.fingerprint, dict(trust_a.binding_dict()), peer_ca_data=trust_a.ca_pem),
            identity=type("Identity", (), {"cert_file": str(self.a_bundle.leaf_cert_file),
                                            "key_file": str(self.a_bundle.leaf_key_file)})(),
            authority=authority_a)
        requester = MaterialControl(
            lobby_id=payload["lobby_id"], invite_id=payload["invite_id"],
            connector=HostConnectorMaterialTransport(requester_connector), journal=self.a.journal,
        )
        self.assertEqual(asyncio.run(requester.request()), {"status": "material_ready"},
                         (b_listener.dispatched, b_listener.handled, b_listener.refused,
                          a_listener.dispatched, a_listener.handled, a_listener.refused,
                          target_handler_errors, receipt_errors, registry_errors, receipt_context))

        # R08-1: drop only the reverse receipt reply at A's real send boundary,
        # after the durable host row is independently observable.  The outer
        # request must fail, while the persisted target envelope is replayed on
        # retry without another native mint or host association.
        host_rows = raw_rows(host_receipts)
        target_root = fx["joining_store"].root
        target_before_loss = {path.name: path.read_bytes() for path in target_root.iterdir() if path.is_file()}
        persisted_raw_before_restart = fx["joining_store"].mint_load(canonical(scope_wire).decode())
        original_send = listener_module.send_message
        receipt_drop = {"armed": True, "events": []}
        def drop_receipt_reply(frames, payload):
            response = decode_response(payload)
            is_material_ready = response.status == 200 and json.loads(response.body) == {"status": "material_ready"}
            if receipt_drop["armed"] and is_material_ready and frames.stream.getsockname()[1] == a_listener.address[1]:
                receipt_drop["armed"] = False
                receipt_drop["events"].append("host_commit_then_receipt_reply_drop")
                self.assertIsInstance(host_receipts.load(scope), dict)
                raise OSError("r08 receipt response dropped after durable commit")
            return original_send(frames, payload)
        with mock.patch.object(listener_module, "send_message", drop_receipt_reply):
            with self.assertRaisesRegex(MaterialError, "control_unavailable"):
                asyncio.run(requester.request())
        self.assertEqual(receipt_drop["events"], ["host_commit_then_receipt_reply_drop"])
        self.assertEqual(raw_rows(host_receipts), host_rows)
        self.assertEqual({path.name: path.read_bytes() for path in target_root.iterdir() if path.is_file()}, target_before_loss)
        self.assertEqual(len(native_calls), 1)
        # A fresh control object must recover the origin from the journal and
        # replay the target's already-durable envelope after the response loss.
        restarted_connector = HostConnector(
            binding=LinkBinding(endpoint, generation, b_listener.address, None,
                                trust_a.fingerprint, dict(trust_a.binding_dict()), peer_ca_data=trust_a.ca_pem),
            identity=type("Identity", (), {"cert_file": str(self.a_bundle.leaf_cert_file),
                                            "key_file": str(self.a_bundle.leaf_key_file)})(),
            authority=authority_a)
        restarted = MaterialControl(
            lobby_id=payload["lobby_id"], invite_id=payload["invite_id"],
            connector=HostConnectorMaterialTransport(restarted_connector), journal=self.a.journal,
        )
        self.assertEqual(asyncio.run(restarted.request()), {"status": "material_ready"})
        self.assertEqual(raw_rows(host_receipts), host_rows)
        self.assertEqual({path.name: path.read_bytes() for path in target_root.iterdir() if path.is_file()}, target_before_loss)
        self.assertEqual(len(native_calls), 1)
        self.assertGreaterEqual(len(target_requests), 2)
        first_request, retry_request = target_requests[0], target_requests[1]
        self.assertEqual(first_request["request_id"], retry_request["request_id"])
        self.assertEqual(first_request["expires_at_utc_ms"], retry_request["expires_at_utc_ms"])
        persisted_envelope = json.loads(fx["joining_store"].mint_load(canonical(scope_wire).decode()))
        self.assertEqual(fx["joining_store"].mint_load(canonical(scope_wire).decode()), persisted_raw_before_restart)
        self.assertEqual(persisted_envelope["operation"], {
            "endpoint_id": endpoint, "generation": generation,
            "lobby_id": payload["lobby_id"], "invite_id": payload["invite_id"],
            "request_id": first_request["request_id"],
            "expires_at_utc_ms": first_request["expires_at_utc_ms"],
        })
        self.assertEqual(persisted_envelope["signature"],
                         json.loads(persisted_raw_before_restart)["signature"])

        # successful outer material_request reply from B.  This distinguishes
        # the two response frames and proves retry after post-commit loss.
        outer_drop = {"receipt_seen": False, "armed": True, "events": []}
        def drop_outer_reply(frames, payload):
            response = decode_response(payload)
            is_material_ready = response.status == 200 and json.loads(response.body) == {"status": "material_ready"}
            if is_material_ready and frames.stream.getsockname()[1] == a_listener.address[1]:
                outer_drop["receipt_seen"] = True
                outer_drop["events"].append("receipt_ack_sent")
                return original_send(frames, payload)
            if is_material_ready and frames.stream.getsockname()[1] == b_listener.address[1] and outer_drop["armed"]:
                outer_drop["armed"] = False
                outer_drop["events"].append("host_commit_then_outer_reply_drop")
                self.assertIsInstance(host_receipts.load(scope), dict)
                raise OSError("r08 outer response dropped after receipt commit")
            return original_send(frames, payload)
        with mock.patch.object(listener_module, "send_message", drop_outer_reply):
            with self.assertRaises(Exception):
                asyncio.run(restarted.request())
        self.assertEqual(outer_drop["events"], ["receipt_ack_sent", "host_commit_then_outer_reply_drop"])
        self.assertEqual(raw_rows(host_receipts), host_rows)
        self.assertEqual(len(native_calls), 1)
        self.assertEqual(asyncio.run(restarted.request()), {"status": "material_ready"})
        self.assertEqual(raw_rows(host_receipts), host_rows)
        self.assertEqual(len(native_calls), 1)

        # Same acknowledged peer, but a foreign lobby/scope: generic denial
        # with no target-store, receipt-store, or native side effects.
        foreign_host_rows = raw_rows(host_receipts)
        foreign_target = {path.name: path.read_bytes() for path in target_root.iterdir() if path.is_file()}
        status, body = _wire(
            b_listener.address[1], client=self.a_bundle, server=self.b_bundle,
            endpoint_id=endpoint, generation=generation,
            request={"op": "material_request", "lobby_id": "foreign-lobby", "invite_id": payload["invite_id"]})
        self.assertEqual((status, body), (403, {"error": "control denied"}))
        self.assertEqual(raw_rows(host_receipts), foreign_host_rows)
        self.assertEqual({path.name: path.read_bytes() for path in target_root.iterdir() if path.is_file()}, foreign_target)
        self.assertEqual(len(native_calls), 1)

        # A swapped scope is denied by the real listener/handler before any
        # target mint or host admission.  Capture both durable stores before the
        # attempt so the refusal is also a no-write proof.
        host_rows_before = raw_rows(host_receipts)
        def target_snapshot():
            return {path.name: path.read_bytes() for path in fx["joining_store"].root.iterdir()
                    if path.is_file()}
        target_rows_before = target_snapshot()
        status, body = _wire(
            b_listener.address[1], client=self.a_bundle, server=self.b_bundle,
            endpoint_id=endpoint, generation=generation,
            request={"op": "material_request", "lobby_id": payload["lobby_id"],
                     "invite_id": payload["invite_id"] + "-swapped"})
        self.assertEqual((status, body), (403, {"error": "control denied"}))
        self.assertEqual(raw_rows(host_receipts), host_rows_before)
        self.assertEqual(target_snapshot(), target_rows_before)
        self.assertEqual(len(native_calls), 1)
        host_rows_before = raw_rows(host_receipts)
        self.assertEqual(len(host_rows_before), 1)
        self.assertEqual(native_calls[0]["method"], "groups.peer.invite")
        self.assertEqual(native_calls[0]["params"], {
            "room_id": scope.planned_room_id, "home_install_id": scope.owner_installation_id,
            "authority_gateway_id": scope.owner_installation_id, "authority_epoch": 1,
            "member_id": scope.member_id, "profile": "default",
            "grant_id": native_calls[0]["params"]["grant_id"],
            "ttl_seconds": native_calls[0]["params"]["ttl_seconds"],
        })
        self.assertEqual(len(native_calls), 1)
        self.assertFalse(any(call["method"] in {"groups.create", "groups.peer.register"} for call in native_calls))
        # Reopen both stores.  The target's exact encrypted envelope and the
        # host's decrypted association must carry the same grant.
        target_scope = scope_wire
        target_root = fx["joining_store"].root
        fx["joining_store"].close()
        reopened_target = JoiningStore(target_root)
        target_envelope = json.loads(reopened_target.mint_load(canonical(target_scope).decode()))
        host_material = host_receipts.load(scope)
        self.assertEqual(host_material["grant"], target_envelope["grant"])
        host_receipts.close()
        reopened = MaterialReceiptStore(self.tmp / "host-material" / "receipts")
        self.assertEqual(raw_rows(reopened), host_rows_before)
        self.assertEqual(reopened.load(scope)["grant"], target_envelope["grant"])
        # A duplicate receipt is accepted idempotently and does not invoke the
        # native adapter again or create a second immutable host association.
        host_registry._receipt_store = reopened
        asyncio.run(host_registry.admit_signed_material_async(scope, target_envelope))
        self.assertEqual(raw_rows(reopened), host_rows_before)
        self.assertEqual(len(native_calls), 1)

    def test_accepted_h3_default_empty_material_request_is_fixed_denial(self):
        """Post-hoc baseline: accepted H3, with no W2c material handler."""
        endpoint = next(iter(self.a.journal.snapshot().links))
        link = self.a.journal.snapshot().links[endpoint]
        generation = self.a.journal.snapshot().link_generation(link)
        authority_b = JournalLinkAuthority(self.b.journal)
        store = __import__("room_invite_store", fromlist=["RoomInviteStore"]).RoomInviteStore(
            self.tmp / "baseline.sqlite", clock=self.clock)
        store.bind_owner_scope(INSTALL_B, "default")
        handlers = build_host_handlers(store, OwnerContext(INSTALL_B, "default"))
        listener = ControlListener(
            host="127.0.0.1", port=w2a.free_port(), authority=authority_b,
            identity=lambda: (str(self.b_bundle.leaf_cert_file), str(self.b_bundle.leaf_key_file)),
            pair_ack_handler=lambda request: self.b.handle_pair_ack(request),
            host_handlers={"status": handlers["status"]}, timeout=4.0)
        self.listeners.append(listener)
        listener.start()
        status, body = _wire(
            listener.address[1], client=self.a_bundle, server=self.b_bundle,
            endpoint_id=endpoint, generation=generation,
            request={"op": "material_request", "lobby_id": "baseline-lobby",
                     "invite_id": "baseline-invite"})
        self.assertEqual((status, body), (403, {"error": "control denied"}))
        self.assertEqual(listener.handled, 0)

    def test_request_admission_withdrawal_after_journal_admission_sends_no_receipt(self):
        """The request-local gate is enforced at the real pinned-mTLS send boundary."""
        endpoint = next(iter(self.a.journal.snapshot().links))
        link = self.a.journal.snapshot().links[endpoint]
        generation = self.a.journal.snapshot().link_generation(link)
        authority_a = JournalLinkAuthority(self.a.journal)
        authority_b = JournalLinkAuthority(self.b.journal)
        trust_b = authority_b.peer_trust(endpoint, generation, role="joining")
        self.assertIsNotNone(trust_b)
        binding = dict(trust_b.binding_dict())
        scope = MaterialScope(owner_installation_id=INSTALL_A, owner_profile="default",
                              lobby_id=LOBBY, planned_room_id="room-" + "3" * 32,
                              member_id="member-" + "4" * 32, invite_id=INVITE,
                              peer_id=trust_b.peer_id, pairing_generation=1, **binding)
        receipt = {"binding": dict(scope.binding), "catalog": {}, "grant": "grant-withdrawn"}
        host_receipts = MaterialReceiptStore(self.tmp / "withdrawn-host" / "receipts")
        handled = []

        def receipt_handler(context, request):
            handled.append((context.operation, request))
            host_receipts.admit(scope, request["receipt"])
            return {"status": "material_ready"}

        listener = ControlListener(
            host="127.0.0.1", port=w2a.free_port(), authority=authority_a,
            identity=lambda: (str(self.a_bundle.leaf_cert_file), str(self.a_bundle.leaf_key_file)),
            pair_ack_handler=lambda request: self.a.handle_pair_ack(request),
            material_handlers={"material_receipt": receipt_handler}, timeout=4.0)
        self.listeners.append(listener)
        listener.start()

        gate = {"open": True, "calls": 0}
        original_admit = authority_b.admit

        def durable_admit(endpoint_id, link_generation, operation):
            result = original_admit(endpoint_id, link_generation, operation)
            if operation == "data_send":
                gate["open"] = False
            return result

        connector = HostConnector(
            binding=LinkBinding(endpoint, generation, listener.address, None,
                                trust_b.fingerprint, binding, peer_ca_data=trust_b.ca_pem),
            identity=type("Identity", (), {"cert_file": str(self.b_bundle.leaf_cert_file),
                                            "key_file": str(self.b_bundle.leaf_key_file)})(),
            authority=authority_b)
        with mock.patch.object(authority_b, "admit", side_effect=durable_admit):
            def request_admit():
                gate["calls"] += 1
                return gate["open"]

            request = {"op": "material_receipt", "lobby_id": LOBBY,
                       "invite_id": INVITE, "receipt": receipt}
            with self.assertRaisesRegex(Exception, "room link outcome unknown"):
                connector.forward(ForwardedRequest("POST", "/control/joining", (),
                                                    json.dumps(request).encode()),
                                  admit=request_admit, deadline=time.monotonic() + 2)
        time.sleep(0.1)
        self.assertGreaterEqual(gate["calls"], 2)
        self.assertEqual(handled, [])
        self.assertEqual(raw_rows(host_receipts), [])
        with self.assertRaises(Exception):
            host_receipts.load(scope)

    def test_two_independent_scopes_share_one_listener_pair(self):
        """Two durable imports multiplex through the same mTLS handler maps."""
        endpoint = next(iter(self.a.journal.snapshot().links))
        link = self.a.journal.snapshot().links[endpoint]
        generation = self.a.journal.snapshot().link_generation(link)
        authority_a = JournalLinkAuthority(self.a.journal)
        authority_b = JournalLinkAuthority(self.b.journal)
        trust_a = authority_a.peer_trust(endpoint, generation, role="host")
        trust_b = authority_b.peer_trust(endpoint, generation, role="joining")
        self.assertIsNotNone(trust_a)
        self.assertIsNotNone(trust_b)
        binding = dict(trust_b.binding_dict())
        fixture_binding = {
            "endpoint_id": endpoint, "fingerprint": link["peer"]["fingerprint"],
            "target_install_id": link["peer"]["installation_id"], "target_profile": link["peer"]["profile"],
            "catalog_digest": link["peer"]["catalog_digest"], "policy_digest": link["peer"]["policy_digest"],
        }

        fixtures = []
        scopes = {}
        for index in ("one", "two"):
            fx, payload = self._approved_joining_fixture(trust_a.peer_id, fixture_binding, index)
            with fx["joining_store"]._write() as conn:
                conn.execute("UPDATE imports SET host_peer_id=?", (f"{endpoint}:{generation}",))
            fx["channel"].binding = dict(fixture_binding)
            scope_wire = fx["channel"]._scope(
                payload["invite_id"],
                fx["store"].get_redemption(payload["lobby_id"], payload["invite_id"]),
            )
            scope = MaterialScope(
                owner_installation_id=scope_wire["host_id"], owner_profile="default",
                lobby_id=scope_wire["lobby_id"], planned_room_id=scope_wire["room_id"],
                member_id=scope_wire["member_id"], invite_id=scope_wire["invite_id"],
                peer_id=trust_a.peer_id, pairing_generation=1, **scope_wire["binding"])
            fixtures.append((fx, payload, scope_wire, scope))
            scopes[(payload["lobby_id"], payload["invite_id"])] = (fx, scope_wire, scope)
        self.assertEqual(len({item[1]["lobby_id"] for item in fixtures}), 2)
        self.assertEqual(len({item[1]["invite_id"] for item in fixtures}), 2)

        host_receipts = MaterialReceiptStore(self.tmp / "two-scope-host" / "receipts")
        host_registry = PeerRegistry(receipt_store=host_receipts, route_provider=lambda _scope: 19001,
                                     authority=authority_a)
        first_scope = fixtures[0][3]
        host_registry.register(
            peer_id=trust_a.peer_id, installation_id=first_scope.target_install_id,
            profile=first_scope.target_profile, fingerprint=first_scope.fingerprint,
            endpoint_id=first_scope.endpoint_id, catalog=copy.deepcopy(CATALOG),
            consent_public_key=self.b_bundle.consent_public_key, tls_ca_pem=CA_PEM,
            pairing_generation=first_scope.pairing_generation)
        host_registry._source = authoritative_source(
            self.tmp / "two-scope-host", peer_id=trust_a.peer_id,
            installation_id=first_scope.target_install_id, profile=first_scope.target_profile,
            fingerprint=first_scope.fingerprint, endpoint_id=first_scope.endpoint_id,
            catalog=CATALOG, consent_public_key=self.b_bundle.consent_public_key,
            tls_ca_pem=CA_PEM)
        h3_handlers = {
            (payload["lobby_id"], payload["invite_id"]): build_host_handlers(
                fx["store"], OwnerContext(HOST_INSTALL, "default"))
            for fx, payload, _wire_scope, _scope in fixtures
        }

        def shared_status(context, request):
            key = (request.get("lobby_id"), request.get("invite_id"))
            handler = h3_handlers.get(key)
            if handler is None:
                raise ValueError("unknown material scope")
            return handler["status"](context, request)

        def resolve_host(*, context, request, **_kwargs):
            key = (request["lobby_id"], request["invite_id"])
            item = scopes.get(key)
            if item is None or context.peer_id != trust_a.peer_id:
                raise ValueError("foreign material scope")
            fx, scope_wire, scope = item
            status = h3_handlers[key]["status"](
                context, {"op": "status", "lobby_id": key[0], "invite_id": key[1]})
            if status != {"status": "ready", "scope": scope_wire}:
                raise ValueError("scope is not ready")
            return host_registry, scope

        host_control = MaterialControl(receipt_resolver=resolve_host,
                                       scope_status_resolver=lambda **kwargs: {
                                           "status": "ready",
                                           "scope": scopes[(kwargs["request"]["lobby_id"],
                                                            kwargs["request"]["invite_id"])][1],
                                       })
        a_listener = ControlListener(
            host="127.0.0.1", port=w2a.free_port(), authority=authority_a,
            identity=lambda: (str(self.a_bundle.leaf_cert_file), str(self.a_bundle.leaf_key_file)),
            pair_ack_handler=lambda request: self.a.handle_pair_ack(request),
            host_handlers={"status": shared_status},
            material_handlers={"material_receipt": host_control.receipt_handler}, timeout=4.0)
        self.listeners.append(a_listener)
        a_listener.start()

        connector = HostConnector(
            binding=LinkBinding(endpoint, generation, a_listener.address, None,
                                trust_b.fingerprint, binding, peer_ca_data=trust_b.ca_pem),
            identity=type("Identity", (), {"cert_file": str(self.b_bundle.leaf_cert_file),
                                            "key_file": str(self.b_bundle.leaf_key_file)})(),
            authority=authority_b)
        def b_to_a(request, *, deadline=None, admit=None):
            result = connector.forward(ForwardedRequest("POST", "/control/joining", (),
                                                         json.dumps(request, separators=(",", ":")).encode()),
                                       admit=admit, deadline=deadline)
            return json.loads(result.body)

        # Each mint has its own durable store and channel, but all receipts use
        # the one reverse connector and the one A-side listener.
        native_calls = []
        async def native(method, params, invite_admission=None, deadline=None):
            native_calls.append((method, dict(params)))
            app = web.Application()
            async def native_handler(request):
                ws = web.WebSocketResponse()
                await ws.prepare(request)
                async for frame in ws:
                    message = json.loads(frame.data)
                    now = time.time()
                    claims = {"room_id": params["room_id"], "home_install_id": params["home_install_id"],
                              "authority_gateway_id": params["authority_gateway_id"], "authority_epoch": 1,
                              "member_id": params["member_id"], "target_install_id": SELF_BINDING["target_install_id"],
                              "target_profile": "default", "issued_at": now, "expires_at": now + 600,
                              "status_expires_at": now + 600, "execution_policy_digest": SELF_BINDING["policy_digest"]}
                    encoded = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
                    grant = encoded + "." + base64.urlsafe_b64encode(b"x" * 32).rstrip(b"=").decode()
                    await ws.send_json({"jsonrpc": "2.0", "id": message["id"], "result": {
                        "target_profile": "default", "endpoint": CATALOG["endpoint"],
                        "catalog": CATALOG, "grant": grant}})
                return ws
            app.router.add_get("/api/ws", native_handler)
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, "127.0.0.1", 0)
            await site.start()
            port = site._server.sockets[0].getsockname()[1]
            try:
                async with aiohttp.ClientSession(trust_env=False) as session:
                    async with session.ws_connect(f"http://127.0.0.1:{port}/api/ws") as ws:
                        return await BoundedNativeRpc(
                            ws, timeout=2, invite_admission=invite_admission
                        )(method, params, deadline=deadline)
            finally:
                await runner.cleanup()

        def bind_native_request(*, invite_admission, deadline):
            async def bound(method, params):
                return await native(method, params, invite_admission=invite_admission, deadline=deadline)
            bound._request_bound_native_rpc = True
            return bound
        native.bind_request = bind_native_request

        mints = {}
        target_roots = {}
        for fx, payload, _wire_scope, _scope in fixtures:
            channel = MaterialControlChannel(
                HostConnectorMaterialTransport(connector),
                lobby_id=payload["lobby_id"], invite_id=payload["invite_id"])
            mint = TargetMaterialMint(
                fx["joining_store"], consent_key=fx["consent_key"], channel=channel,
                rpc=native, host_peer_id=f"{endpoint}:{generation}")
            mints[(payload["lobby_id"], payload["invite_id"])] = mint
            target_roots[(payload["lobby_id"], payload["invite_id"])] = fx["joining_store"].root

        target_control = MaterialControl(target_resolver=lambda *, request, **_kwargs:
                                         mints.get((request["lobby_id"], request["invite_id"])))
        b_listener = ControlListener(
            host="127.0.0.1", port=w2a.free_port(), authority=authority_b,
            identity=lambda: (str(self.b_bundle.leaf_cert_file), str(self.b_bundle.leaf_key_file)),
            pair_ack_handler=lambda request: self.b.handle_pair_ack(request),
            material_handlers={"material_request": target_control.target_handler}, timeout=4.0)
        self.listeners.append(b_listener)
        b_listener.start()

        forward_transport = HostConnectorMaterialTransport(HostConnector(
            binding=LinkBinding(endpoint, generation, b_listener.address, None,
                                trust_a.fingerprint, dict(trust_a.binding_dict()), peer_ca_data=trust_a.ca_pem),
            identity=type("Identity", (), {"cert_file": str(self.a_bundle.leaf_cert_file),
                                            "key_file": str(self.a_bundle.leaf_key_file)})(),
            authority=authority_a))
        requesters = [MaterialControl(lobby_id=payload["lobby_id"], invite_id=payload["invite_id"],
                                      connector=forward_transport)
                      for _fx, payload, _wire_scope, _scope in fixtures]
        self.assertEqual([asyncio.run(requester.request()) for requester in requesters],
                         [{"status": "material_ready"}, {"status": "material_ready"}])
        self.assertEqual(len(native_calls), 2)
        host_rows = raw_rows(host_receipts)
        self.assertEqual(len(host_rows), 2)
        host_before = {path.name: path.read_bytes() for path in host_receipts.root.iterdir() if path.is_file()}
        target_before = {key: {path.name: path.read_bytes() for path in root.iterdir() if path.is_file()}
                         for key, root in target_roots.items()}

        # A retry on the first scope replays its durable envelope only.
        self.assertEqual(asyncio.run(requesters[0].request()), {"status": "material_ready"})
        self.assertEqual(len(native_calls), 2)
        self.assertEqual(raw_rows(host_receipts), host_rows)
        self.assertEqual({path.name: path.read_bytes() for path in host_receipts.root.iterdir() if path.is_file()}, host_before)

        # Cross-scope selectors are denied by the shared target resolver before
        # minting; neither durable store nor the native adapter changes.
        first = fixtures[0][1]
        second = fixtures[1][1]
        status, body = _wire(
            b_listener.address[1], client=self.a_bundle, server=self.b_bundle,
            endpoint_id=endpoint, generation=generation,
            request={"op": "material_request", "lobby_id": first["lobby_id"],
                     "invite_id": second["invite_id"]})
        self.assertEqual((status, body), (403, {"error": "control denied"}))
        self.assertEqual(len(native_calls), 2)
        self.assertEqual(raw_rows(host_receipts), host_rows)
        self.assertEqual({path.name: path.read_bytes() for path in host_receipts.root.iterdir() if path.is_file()}, host_before)
        self.assertEqual(target_before,
                         {key: {path.name: path.read_bytes() for path in root.iterdir() if path.is_file()}
                          for key, root in target_roots.items()})

        # Reopen both target stores and the host store; each association remains
        # bound to exactly its own signed envelope.
        for fx, payload, scope_wire, scope in fixtures:
            fx["joining_store"].close()
            reopened = JoiningStore(target_roots[(payload["lobby_id"], payload["invite_id"])])
            envelope = json.loads(reopened.mint_load(canonical(scope_wire).decode()))
            self.assertEqual(host_receipts.load(scope)["grant"], envelope["grant"])
        host_receipts.close()
        reopened_host = MaterialReceiptStore(self.tmp / "two-scope-host" / "receipts")
        self.assertEqual(len(raw_rows(reopened_host)), 2)
        for _fx, _payload, _wire_scope, scope in fixtures:
            self.assertIsInstance(reopened_host.load(scope), dict)


if __name__ == "__main__":
    unittest.main()
