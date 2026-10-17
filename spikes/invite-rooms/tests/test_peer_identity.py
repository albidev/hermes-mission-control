import os
import select
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
from pairing import approve_binding, verify_binding
from tls_stream import (
    TLSIdentityError,
    SequencedTLS,
    certificate_fingerprint,
    client_context,
    server_context,
    wrap_client,
    wrap_server,
)


class RelayCannotDecrypt(RuntimeError):
    """The opaque relay has no inner-TLS private keys or terminating context."""


class OpaqueTLSRelay:
    """Forward raw TLS bytes while recording only what the relay can observe."""

    def __init__(self, left, right):
        self.left = left
        self.right = right
        self.observed_data = []
        self.private_keys = {}
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._forward, daemon=True)

    def start(self):
        self.thread.start()

    def _forward(self):
        peers = {self.left: self.right, self.right: self.left}
        while not self.stop.is_set():
            readable, _, _ = select.select(tuple(peers), (), (), 0.1)
            for source in readable:
                try:
                    data = source.recv(65536)
                    if not data:
                        return
                    self.observed_data.append(data)
                    peers[source].sendall(data)
                except OSError:
                    return

    def decrypt_observed_data(self):
        raise RelayCannotDecrypt("opaque relay has no peer private key or TLS context")

    def close(self):
        self.stop.set()
        for sock in (self.left, self.right):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()
        self.thread.join(timeout=2)


class TLSPeerIdentityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if shutil.which("openssl") is None:
            raise unittest.SkipTest("openssl is required for real certificate handshakes")
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.ca_key, cls.ca_cert = root / "ca.key", root / "ca.pem"
        cls._openssl(["req", "-x509", "-new", "-nodes", "-newkey", "rsa:2048", "-keyout", str(cls.ca_key), "-out", str(cls.ca_cert), "-subj", "/CN=rooms-test-ca", "-days", "1"])
        cls.certificates = {}
        for name in ("a", "b", "wrong"):
            key, csr, cert = root / f"{name}.key", root / f"{name}.csr", root / f"{name}.pem"
            cls._openssl(["req", "-new", "-nodes", "-newkey", "rsa:2048", "-keyout", str(key), "-out", str(csr), "-subj", f"/CN={name}"])
            cls._openssl(["x509", "-req", "-in", str(csr), "-CA", str(cls.ca_cert), "-CAkey", str(cls.ca_key), "-CAcreateserial", "-out", str(cert), "-days", "1"])
            cls.certificates[name] = (cert, key)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    @classmethod
    def _openssl(cls, args):
        subprocess.run(["openssl", *args], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _contexts(self, name):
        cert, key = self.certificates[name]
        return (
            client_context(ca_file=str(self.ca_cert), cert_file=str(cert), key_file=str(key)),
            server_context(ca_file=str(self.ca_cert), cert_file=str(cert), key_file=str(key)),
        )

    def _handshake(self, client_name="a", server_name="b", expected_client=None, expected_server=None):
        client_sock, server_sock = socket.socketpair()
        client_ctx, _ = self._contexts(client_name)
        _, server_ctx = self._contexts(server_name)
        # Read the exact DER with stdlib instead of treating PEM bytes as the pin.
        expected_client = certificate_fingerprint(ssl.PEM_cert_to_DER_cert(Path(self.certificates[client_name][0]).read_text()))
        expected_server = expected_server or certificate_fingerprint(ssl.PEM_cert_to_DER_cert(Path(self.certificates[server_name][0]).read_text()))
        result = {}

        def accept():
            try:
                result["server"] = wrap_server(server_sock, server_ctx, expected_fingerprint=expected_client)
            except Exception as exc:
                result["server_error"] = exc

        thread = threading.Thread(target=accept)
        thread.start()
        try:
            try:
                result["client"] = wrap_client(client_sock, client_ctx, expected_fingerprint=expected_server)
            except Exception as exc:
                result["client_error"] = exc
            thread.join(timeout=5)
            if thread.is_alive():
                self.fail("TLS handshake thread did not finish")
            return result
        finally:
            for stream in (result.get("client"), result.get("server")):
                if stream is not None:
                    stream.close()
            client_sock.close()
            server_sock.close()

    def test_real_mutual_tls_and_sequence_replay_rejection(self):
        result = self._handshake()
        self.assertNotIn("client_error", result)
        self.assertNotIn("server_error", result)
        # Exercise a second real pair to retain both ends for frame behavior.
        client_sock, server_sock = socket.socketpair()
        client_ctx, _ = self._contexts("a")
        _, server_ctx = self._contexts("b")
        client = {}
        def accept():
            client["server"] = wrap_server(server_sock, server_ctx, expected_fingerprint=self._fp("a"))
        thread = threading.Thread(target=accept); thread.start()
        client["client"] = wrap_client(client_sock, client_ctx, expected_fingerprint=self._fp("b")); thread.join()
        try:
            sender, receiver = SequencedTLS(client["client"]), SequencedTLS(client["server"])
            self.assertEqual(sender.send(b"grant=sentinel"), 0)
            self.assertEqual(receiver.receive(), (0, b"grant=sentinel"))
            # Replaying the exact application frame is rejected by sequence fencing.
            client["client"].sendall(b"\x00\x00\x00\x2a{" + b'"sequence":0,"payload":"6772616e743d73656e74696e656c"}')
            with self.assertRaises(TLSIdentityError):
                receiver.receive()
        finally:
            client["client"].close(); client["server"].close()

    def _fp(self, name):
        return certificate_fingerprint(ssl.PEM_cert_to_DER_cert(Path(self.certificates[name][0]).read_text()))

    def test_wrong_or_untrusted_peer_and_unapproved_rotation_fail(self):
        result = self._handshake(server_name="wrong", expected_server=self._fp("b"))
        self.assertTrue("client_error" in result or "server_error" in result)
        binding = approve_binding(link_id="link", peer_certificate_der=ssl.PEM_cert_to_DER_cert(Path(self.certificates["b"][0]).read_text()), native_identity={"install_id": "install:b"}, profile="default")
        with self.assertRaises(TLSIdentityError):
            verify_binding(binding, peer_certificate_der=ssl.PEM_cert_to_DER_cert(Path(self.certificates["wrong"][0]).read_text()), native_identity_probe=lambda: {"install_id": "install:b"}, profile="default")

    def test_native_installation_and_profile_are_independent_bindings(self):
        der = ssl.PEM_cert_to_DER_cert(Path(self.certificates["b"][0]).read_text())
        binding = approve_binding(link_id="link", peer_certificate_der=der, native_identity={"install_id": "install:b"}, profile="default")
        with self.assertRaises(TLSIdentityError):
            verify_binding(binding, peer_certificate_der=der, native_identity_probe=lambda: {"install_id": "install:other"}, profile="default")
        with self.assertRaises(TLSIdentityError):
            verify_binding(binding, peer_certificate_der=der, native_identity_probe=lambda: {"install_id": "install:b"}, profile="other")

    def test_corrupted_application_frame_is_rejected_after_tls(self):
        client_sock, server_sock = socket.socketpair()
        client_ctx, _ = self._contexts("a")
        _, server_ctx = self._contexts("b")
        streams = {}
        def accept():
            streams["server"] = wrap_server(server_sock, server_ctx, expected_fingerprint=self._fp("a"))
        thread = threading.Thread(target=accept); thread.start()
        streams["client"] = wrap_client(client_sock, client_ctx, expected_fingerprint=self._fp("b")); thread.join()
        try:
            # TLS authenticates the transport, but the application framing still rejects corruption.
            streams["client"].sendall(b"\x00\x00\x00\x0fnot-json-corrupt")
            with self.assertRaises(TLSIdentityError):
                SequencedTLS(streams["server"]).receive()
        finally:
            streams["client"].close(); streams["server"].close()

    def test_relay_visible_data_is_ciphertext_not_sentinels(self):
        # Route actual TLS records through an untrusted byte-forwarding relay.
        client_sock, relay_left = socket.socketpair()
        relay_right, server_sock = socket.socketpair()
        relay = OpaqueTLSRelay(relay_left, relay_right)
        relay.start()
        streams = {}
        client_ctx, _ = self._contexts("a")
        _, server_ctx = self._contexts("b")

        def accept():
            streams["server"] = wrap_server(server_sock, server_ctx, expected_fingerprint=self._fp("a"))

        thread = threading.Thread(target=accept)
        thread.start()
        streams["client"] = wrap_client(client_sock, client_ctx, expected_fingerprint=self._fp("b"))
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive(), "TLS handshake through relay did not finish")
        sender = SequencedTLS(streams["client"])
        receiver = SequencedTLS(streams["server"])
        payload = b"grant=real-test-grant prompt=real-test-prompt output=real-test-output"
        try:
            self.assertEqual(sender.send(payload), 0)
            self.assertEqual(receiver.receive(), (0, payload))
            observed = b"".join(relay.observed_data)
            for sentinel in (b"grant=real-test-grant", b"prompt=real-test-prompt", b"output=real-test-output"):
                self.assertNotIn(sentinel, observed)
            self.assertTrue(relay.observed_data)
            self.assertEqual(relay.private_keys, {})
            with self.assertRaises(RelayCannotDecrypt):
                relay.decrypt_observed_data()
        finally:
            streams["client"].close()
            streams["server"].close()
            relay.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
