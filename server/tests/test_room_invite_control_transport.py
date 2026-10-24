from __future__ import annotations

import asyncio
import datetime as dt
import json
import socket
import ssl
import struct
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from invite_rooms.control.dispatcher import (
    BusyError,
    ControlDispatcher,
    ControlError,
    LinkState,
    TransportRegistry,
)
from invite_rooms.control.framing import (
    FrameError,
    SequencedTLS,
    TLSIdentityError,
)
from invite_rooms.control.tls import client_context, server_context, wrap_client, wrap_server

SPIKE = Path(__file__).resolve().parents[2] / "spikes" / "invite-rooms"
if str(SPIKE) not in sys.path:
    sys.path.insert(0, str(SPIKE))
import pki  # noqa: E402


class Authority:
    def __init__(self, state):
        self.state = state
        self.calls = 0

    def current(self, endpoint_id):
        self.calls += 1
        return self.state


class ReentrantAuthority(Authority):
    def __init__(self, state):
        super().__init__(state)
        self.in_io = False
        self.observed_reentrancy = False

    def current(self, endpoint_id):
        if self.in_io:
            self.observed_reentrancy = True
        return super().current(endpoint_id)


class ControlTransportTests(unittest.IsolatedAsyncioTestCase):
    def _state(self, protocol_state="acknowledged", generation=3, endpoint="ep-1"):
        return LinkState(
            binding={"endpoint_id": endpoint, "fingerprint": "a" * 64},
            generation=generation,
            protocol_state=protocol_state,
        )

    def _request(self, op="preview", **extra):
        value = {"op": op, "lobby_id": "l", "invite_id": "i"}
        value.update(extra)
        return value

    async def asyncTearDown(self):
        if hasattr(self, "dispatchers"):
            for dispatcher in self.dispatchers:
                await dispatcher.close()

    def make_dispatcher(self, *args, **kwargs):
        dispatcher = ControlDispatcher(*args, **kwargs)
        self.dispatchers = getattr(self, "dispatchers", []) + [dispatcher]
        return dispatcher

    async def test_dispatch_maps_consent_to_joining_consent_after_queue(self):
        state = self._state()
        calls = []

        async def handler(request):
            calls.append(request)
            return {"ok": True}

        dispatcher = self.make_dispatcher(
            Authority(state), handlers={"joining_consent": handler},
            owner_verify=lambda endpoint, request: True,
            admit=lambda endpoint, generation, op: calls.append((endpoint, generation, op)) or True,
        )
        result = await dispatcher.dispatch("ep-1", 3, {
            "op": "consent", "lobby_id": "l", "invite_id": "i", "receipt": {}, "signature": "s",
        })
        self.assertEqual(result, {"ok": True})
        self.assertEqual(calls[0], ("ep-1", 3, "joining_consent"))
        self.assertEqual(calls[1]["op"], "joining_consent")

    async def test_confirmed_only_accepts_pair_ack_and_rejects_everything_else(self):
        state = self._state("confirmed")
        calls = []
        dispatcher = self.make_dispatcher(
            Authority(state),
            handlers={"pair_ack": lambda request: calls.append(request) or {"ok": True}},
        )
        self.assertEqual(await dispatcher.dispatch("ep-1", 3, {"op": "pair_ack", "ack": "same"}), {"ok": True})
        self.assertEqual(len(calls), 1)
        with self.assertRaisesRegex(Exception, "control_unavailable"):
            await dispatcher.dispatch("ep-1", 3, self._request())
        self.assertEqual(len(calls), 1)

    async def test_wrong_peer_lobby_or_invite_is_refused_before_handler(self):
        called = []
        dispatcher = self.make_dispatcher(
            Authority(self._state()), handlers={"preview": lambda request: called.append(request)},
            owner_verify=lambda endpoint, request: request["lobby_id"] == "owned" and request["invite_id"] == "owned-invite",
        )
        with self.assertRaises(ControlError):
            await dispatcher.dispatch("ep-1", 3, self._request(lobby_id="other"))
        self.assertEqual(called, [])

    async def test_material_request_has_only_owner_ids_and_refuses_native_parameters(self):
        called = []
        dispatcher = self.make_dispatcher(
            Authority(self._state()), handlers={"material_request": lambda request: called.append(request)},
            owner_verify=lambda endpoint, request: True,
        )
        with self.assertRaises(ControlError):
            await dispatcher.dispatch("ep-1", 3, self._request("material_request", native_params={"method": "groups.peer"}))
        self.assertEqual(called, [])
        self.assertEqual(
            await dispatcher.dispatch("ep-1", 3, self._request("material_request")),
            None,
        )
        self.assertEqual(called[0]["op"], "material_request")

    async def test_disallowed_operation_is_generic_forbidden_and_handler_never_called(self):
        called = []
        dispatcher = self.make_dispatcher(
            Authority(self._state()), handlers={"preview": lambda request: called.append(request)},
            owner_verify=lambda endpoint, request: True,
        )
        # A B->A dispatcher cannot accept A->B material requests.
        dispatcher.direction = "B_to_A"
        with self.assertRaises(ControlError) as caught:
            await dispatcher.dispatch("ep-1", 3, self._request("material_request"))
        self.assertEqual(caught.exception.code, "forbidden")
        self.assertEqual(called, [])

    async def test_generation_change_while_queued_is_rejected_after_queue(self):
        authority = Authority(self._state(generation=3))
        gate = asyncio.Event()
        admitted = []

        async def slow(request):
            await gate.wait()
            return {"ok": True}

        def admit(endpoint, generation, op):
            admitted.append((endpoint, generation, op))
            return authority.state.generation == generation

        dispatcher = self.make_dispatcher(
            authority, handlers={"preview": slow}, owner_verify=lambda endpoint, request: True,
            admit=admit, queue_size=1,
        )
        first = asyncio.create_task(dispatcher.dispatch("ep-1", 3, self._request()))
        await asyncio.sleep(0.01)
        queued = asyncio.create_task(dispatcher.dispatch("ep-1", 3, self._request(invite_id="queued")))
        await asyncio.sleep(0.01)
        authority.state = self._state(generation=4)
        gate.set()
        self.assertEqual(await first, {"ok": True})
        with self.assertRaises(ControlError):
            await queued
        self.assertEqual(len(admitted), 2)

    async def test_generation_change_closes_open_connections(self):
        registry = TransportRegistry()
        closed = []
        registry.register("ep-1", 3, lambda: closed.append("old-1"))
        registry.register("ep-1", 3, lambda: closed.append("old-2"))
        registry.observe_generation("ep-1", 4)
        self.assertCountEqual(closed, ["old-1", "old-2"])

    async def test_saturation_is_bounded_and_returns_busy_without_deadlock(self):
        started = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def activation(request):
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return {"ok": True}

        dispatcher = self.make_dispatcher(
            Authority(self._state()), handlers={"preview": activation, "status": activation},
            owner_verify=lambda endpoint, request: True, queue_size=1, timeout=0.2,
            op_timeouts={"status": 0.03},
        )
        first = asyncio.create_task(dispatcher.dispatch("ep-1", 3, self._request()))
        await started.wait()
        with self.assertRaises(BusyError):
            await dispatcher.dispatch("ep-1", 3, self._request("status"))
        release.set()
        self.assertEqual(await first, {"ok": True})
        self.assertEqual(calls, 1)

    async def test_completion_callback_capacity_is_reserved(self):
        release = asyncio.Event()
        started = asyncio.Event()

        async def activation(request):
            started.set()
            await release.wait()
            return {"ok": True}

        callback = lambda request: {"receipt": True}
        dispatcher = self.make_dispatcher(
            Authority(self._state()), handlers={"preview": activation, "material_receipt": callback},
            owner_verify=lambda endpoint, request: True, queue_size=1, completion_workers=1, timeout=0.2,
            direction="B_to_A",
        )
        running = asyncio.create_task(dispatcher.dispatch("ep-1", 3, self._request()))
        await started.wait()
        # Completion is independent of the saturated activation executor/slot.
        receipt = await dispatcher.dispatch("ep-1", 3, self._request("material_receipt", receipt="r"))
        self.assertEqual(receipt, {"receipt": True})
        release.set()
        self.assertEqual(await running, {"ok": True})

    async def test_per_operation_timeout_is_generic_and_handler_is_not_reported(self):
        async def slow(request):
            await asyncio.sleep(0.1)
            return {"ok": True}

        dispatcher = self.make_dispatcher(
            Authority(self._state()), handlers={"preview": slow}, owner_verify=lambda endpoint, request: True,
            op_timeouts={"preview": 0.01},
        )
        with self.assertRaises(ControlError) as caught:
            await dispatcher.dispatch("ep-1", 3, self._request())
        self.assertEqual(caught.exception.code, "control_unavailable")

    async def test_authority_is_not_reentered_while_handler_io_runs(self):
        authority = ReentrantAuthority(self._state())

        async def handler(request):
            authority.in_io = True
            await asyncio.sleep(0.01)
            authority.in_io = False
            return {"ok": True}

        dispatcher = self.make_dispatcher(
            authority, handlers={"preview": handler}, owner_verify=lambda endpoint, request: True,
        )
        self.assertEqual(await dispatcher.dispatch("ep-1", 3, self._request()), {"ok": True})
        self.assertFalse(authority.observed_reentrancy)


class FramingTests(unittest.TestCase):
    def test_oversize_frame_is_closed_with_generic_error(self):
        left, right = socket.socketpair()
        try:
            stream = SequencedTLS(left, max_frame_bytes=64)
            envelope = {"sequence": 0, "payload": "00" * 65}
            raw = json.dumps(envelope, separators=(",", ":")).encode()
            right.sendall(struct.pack("!I", len(raw)) + raw)
            with self.assertRaises(FrameError) as error:
                stream.receive()
            self.assertEqual(error.exception.code, "control_ambiguous")
        finally:
            left.close()
            right.close()

    def test_sequence_replay_and_out_of_order_are_rejected(self):
        # A valid frame advances the receive counter; replaying sequence zero is rejected.
        left, right = socket.socketpair()
        try:
            stream = SequencedTLS(left)
            raw = json.dumps({"sequence": 0, "payload": "61"}).encode()
            right.sendall(struct.pack("!I", len(raw)) + raw)
            self.assertEqual(stream.receive(), (0, b"a"))
            right.sendall(struct.pack("!I", len(raw)) + raw)
            with self.assertRaises(FrameError):
                stream.receive()
        finally:
            left.close()
            right.close()
        # A first frame with sequence one is out of order.
        left, right = socket.socketpair()
        try:
            stream = SequencedTLS(left)
            raw = json.dumps({"sequence": 1, "payload": "61"}).encode()
            right.sendall(struct.pack("!I", len(raw)) + raw)
            with self.assertRaises(FrameError):
                stream.receive()
        finally:
            left.close()
            right.close()

    def test_slowloris_partial_header_hits_deadline(self):
        left, right = socket.socketpair()
        try:
            deadline = time.monotonic() + 0.03
            stream = SequencedTLS(left, deadline=deadline)
            right.sendall(b"\x00")
            with self.assertRaises((TimeoutError, socket.timeout)):
                stream.receive()
            self.assertLess(time.monotonic(), deadline + 0.2)
        finally:
            left.close()
            right.close()


class RealTLSTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.owner = pki.init_owner(root / "owner", "owner")
        self.peer = pki.init_owner(root / "peer", "peer")

    def tearDown(self):
        self.tmp.cleanup()

    def _pair(self, client_material, server_material, expected_client, expected_server, *, no_client_cert=False, server_trust=None):
        if no_client_cert:
            client_ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=server_material["ca_file"])
            client_ctx.minimum_version = client_ctx.maximum_version = ssl.TLSVersion.TLSv1_3
            client_ctx.check_hostname = False
        else:
            client_ctx = client_context(ca_file=server_material["ca_file"], cert_file=client_material["cert_file"], key_file=client_material["key_file"])
        trust = server_trust or client_material
        server_ctx = server_context(ca_file=trust["ca_file"], cert_file=server_material["cert_file"], key_file=server_material["key_file"])
        left, right = socket.socketpair()
        outcome = []

        def serve():
            try:
                with wrap_server(right, server_ctx, expected_fingerprint=expected_client) as tls:
                    frames = SequencedTLS(tls, deadline=time.monotonic() + 1)
                    frames.receive()
                    frames.send(b"ok")
                    outcome.append(tls.version())
            except Exception as exc:  # handshake failures are intentionally generic to caller
                outcome.append(exc)
            finally:
                right.close()

        thread = threading.Thread(target=serve)
        thread.start()
        client = None
        try:
            client = wrap_client(left, client_ctx, expected_fingerprint=expected_server)
            frames = SequencedTLS(client, deadline=time.monotonic() + 1)
            frames.send(b"request")
            response = frames.receive()[1]
            thread.join(1)
            return response, outcome
        finally:
            if client is not None:
                client.close()
            left.close()
            thread.join(1)

    def test_correct_leaf_and_ca_allows_real_tls_operation(self):
        response, outcome = self._pair(self.owner, self.peer, self.owner["fingerprint"], self.peer["fingerprint"])
        self.assertEqual(response, b"ok")
        self.assertEqual(outcome, ["TLSv1.3"])

    def test_right_ca_wrong_leaf_is_refused_before_handler(self):
        # Server trusts peer CA but pins a different leaf fingerprint.
        with self.assertRaises(Exception):
            self._pair(self.peer, self.peer, self.owner["fingerprint"], self.peer["fingerprint"])

    def test_wrong_ca_is_refused(self):
        with self.assertRaises(Exception):
            self._pair(self.peer, self.owner, self.peer["fingerprint"], self.owner["fingerprint"], server_trust=self.owner)

    def test_expired_leaf_is_refused(self):
        expired = self._expired_leaf(Path(self.tmp.name) / "expired", self.owner)
        with self.assertRaises(Exception):
            self._pair(expired, self.peer, expired["fingerprint"], self.peer["fingerprint"])

    def test_no_client_certificate_is_refused(self):
        with self.assertRaises(Exception):
            self._pair(self.owner, self.peer, self.owner["fingerprint"], self.peer["fingerprint"], no_client_cert=True)

    def _expired_leaf(self, out: Path, ca_material: dict[str, str]) -> dict[str, str]:
        out.mkdir()
        ca = x509.load_pem_x509_certificate(Path(ca_material["ca_file"]).read_bytes())
        ca_key = serialization.load_pem_private_key(Path(Path(ca_material["ca_file"]).with_name("ca.key")).read_bytes(), None)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        now = dt.datetime.now(dt.timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "expired")]))
                .issuer_name(ca.subject).public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - dt.timedelta(days=2)).not_valid_after(now - dt.timedelta(days=1))
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False, key_encipherment=True,
                                             data_encipherment=False, key_agreement=False, key_cert_sign=False,
                                             crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
                .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH, ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
                .sign(ca_key, hashes.SHA256()))
        (out / "leaf.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        (out / "leaf.key").write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        return {"ca_file": ca_material["ca_file"], "cert_file": str(out / "leaf.pem"), "key_file": str(out / "leaf.key"), "fingerprint": pki.fingerprint(out / "leaf.pem")}


if __name__ == "__main__":
    unittest.main()
