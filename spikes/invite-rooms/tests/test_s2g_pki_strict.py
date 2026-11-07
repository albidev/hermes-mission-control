"""F1X-O1: pki.py output must verify under strict X.509 (Python >=3.13 default).

The owner CA/leaves and the relay server certificate are checked with
VERIFY_X509_STRICT set explicitly, so this is RED on Python 3.11 too when pki.py
emits an X.509 v1 CA (LibreSSL) or a CA without keyUsage (OpenSSL 3 defaults).
No sockets are bound: handshakes run over socket.socketpair().
"""
from __future__ import annotations

import os
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pki  # noqa: E402
from tls_stream import certificate_fingerprint, client_context, server_context  # noqa: E402

from cryptography import x509  # noqa: E402
from cryptography.x509.oid import ExtendedKeyUsageOID  # noqa: E402

STRICT = getattr(ssl, "VERIFY_X509_STRICT")


def _strict(context: ssl.SSLContext) -> ssl.SSLContext:
    context.verify_flags |= STRICT
    return context


def _handshake(client: ssl.SSLContext, server: ssl.SSLContext, *, server_hostname=None) -> dict:
    a, b = socket.socketpair()
    a.settimeout(10)
    b.settimeout(10)
    result: dict = {}

    def serve():
        try:
            with server.wrap_socket(b, server_side=True) as tls:
                result["server_peer"] = tls.getpeercert(binary_form=True)
                tls.sendall(b"ok")
        except Exception as exc:  # recorded and asserted by the caller
            result["server_error"] = repr(exc)
        finally:
            b.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        with client.wrap_socket(a, server_hostname=server_hostname) as tls:
            result["client_peer"] = tls.getpeercert(binary_form=True)
            result["data"] = tls.recv(2)
    except Exception as exc:
        result["client_error"] = repr(exc)
    finally:
        a.close()
        thread.join(10)
    return result


def _load(path) -> x509.Certificate:
    return x509.load_pem_x509_certificate(Path(path).read_bytes())


def _openssl3() -> str | None:
    candidates = [shutil.which("openssl"), "/opt/homebrew/opt/openssl@3/bin/openssl",
                  "/opt/homebrew/opt/openssl/bin/openssl", "/usr/local/opt/openssl@3/bin/openssl"]
    for candidate in candidates:
        if not candidate or not os.path.exists(candidate):
            continue
        out = subprocess.run([candidate, "version"], capture_output=True, text=True).stdout
        if out.startswith("OpenSSL 3"):
            return candidate
    return None


class StrictPkiTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="s2g-pki-")
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_owner_mtls_chain_verifies_under_explicit_x509_strict(self):
        a = pki.init_owner(self.root / "a", "a")
        b = pki.init_owner(self.root / "b", "b")
        # A connects as client to B; each trusts the other's CA (as in the lab).
        client = _strict(client_context(ca_file=b["ca_file"], cert_file=a["cert_file"], key_file=a["key_file"]))
        server = _strict(server_context(ca_file=a["ca_file"], cert_file=b["cert_file"], key_file=b["key_file"]))
        result = _handshake(client, server)
        self.assertNotIn("client_error", result, result)
        self.assertNotIn("server_error", result, result)
        self.assertEqual(result["data"], b"ok")
        self.assertEqual(certificate_fingerprint(result["client_peer"]), b["fingerprint"])
        self.assertEqual(certificate_fingerprint(result["server_peer"]), a["fingerprint"])

    def test_relay_server_cert_verifies_strict_with_hostname_check(self):
        relay = pki.init_owner(self.root / "relay-ca", "relay")
        server_files = pki.issue_server(self.root / "relay-ca", self.root / "relay-tls", "IP:127.0.0.1,DNS:relay.example")
        server = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        server.load_cert_chain(server_files["cert_file"], server_files["key_file"])
        for hostname in ("127.0.0.1", "relay.example"):
            with self.subTest(hostname=hostname):
                client = _strict(ssl.create_default_context(cafile=relay["ca_file"]))
                self.assertTrue(client.check_hostname)
                result = _handshake(client, server, server_hostname=hostname)
                self.assertNotIn("client_error", result, result)
                self.assertEqual(result["data"], b"ok")
        client = _strict(ssl.create_default_context(cafile=relay["ca_file"]))
        result = _handshake(client, server, server_hostname="other.example")
        self.assertIn("client_error", result)  # SAN still enforced

    def test_extension_profile(self):
        owner = pki.init_owner(self.root / "o", "o")
        ca = _load(owner["ca_file"])
        leaf = _load(owner["cert_file"])
        self.assertEqual(ca.version, x509.Version.v3)
        bc = ca.extensions.get_extension_for_class(x509.BasicConstraints)
        self.assertTrue(bc.critical)
        self.assertTrue(bc.value.ca)
        self.assertEqual(bc.value.path_length, 0)
        ku = ca.extensions.get_extension_for_class(x509.KeyUsage)
        self.assertTrue(ku.critical)
        self.assertTrue(ku.value.key_cert_sign and ku.value.crl_sign)
        self.assertFalse(ku.value.digital_signature)
        ca_ski = ca.extensions.get_extension_for_class(x509.SubjectKeyIdentifier).value
        self.assertEqual(ca.extensions.get_extension_for_class(x509.AuthorityKeyIdentifier).value.key_identifier,
                         ca_ski.digest)
        self.assertEqual(ca.subject.rfc4514_string(), "CN=o-rooms-ca")

        self.assertEqual(leaf.version, x509.Version.v3)
        self.assertEqual(leaf.issuer, ca.subject)
        self.assertEqual(leaf.subject.rfc4514_string(), "CN=o-rooms-peer")
        lbc = leaf.extensions.get_extension_for_class(x509.BasicConstraints)
        self.assertTrue(lbc.critical)
        self.assertFalse(lbc.value.ca)
        lku = leaf.extensions.get_extension_for_class(x509.KeyUsage)
        self.assertTrue(lku.critical)
        self.assertTrue(lku.value.digital_signature and lku.value.key_encipherment)
        self.assertFalse(lku.value.key_cert_sign)
        eku = leaf.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
        self.assertEqual(set(eku), {ExtendedKeyUsageOID.SERVER_AUTH, ExtendedKeyUsageOID.CLIENT_AUTH})
        self.assertEqual(leaf.extensions.get_extension_for_class(x509.AuthorityKeyIdentifier).value.key_identifier,
                         ca_ski.digest)
        with self.assertRaises(x509.ExtensionNotFound):  # no SAN on owner leaf, as before
            leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName)

        srv = pki.issue_server(self.root / "o", self.root / "srv", "IP:127.0.0.1")
        cert = _load(srv["cert_file"])
        self.assertEqual(cert.subject.rfc4514_string(), "CN=rooms-relay")
        self.assertFalse(cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca)
        self.assertEqual(list(cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value),
                         [ExtendedKeyUsageOID.SERVER_AUTH])
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        self.assertEqual([str(ip) for ip in san.get_values_for_type(x509.IPAddress)], ["127.0.0.1"])
        self.assertEqual(srv["ca_file"], owner["ca_file"])

    def test_file_layout_permissions_and_fingerprint_contract(self):
        out = self.root / "layout"
        owner = pki.init_owner(out, "n", days=2)
        self.assertEqual(set(owner), {"name", "ca_file", "cert_file", "key_file", "fingerprint"})
        self.assertEqual(Path(owner["ca_file"]), out / "ca.pem")
        self.assertEqual(Path(owner["cert_file"]), out / "leaf.pem")
        self.assertEqual(Path(owner["key_file"]), out / "leaf.key")
        self.assertFalse((out / "leaf.csr").exists())
        for key in ("ca.key", "leaf.key"):
            self.assertEqual((out / key).stat().st_mode & 0o777, 0o600, key)
            self.assertIn(b"-----BEGIN PRIVATE KEY-----", (out / key).read_bytes())
        self.assertEqual(out.stat().st_mode & 0o777, 0o700)
        der = ssl.PEM_cert_to_DER_cert((out / "leaf.pem").read_text())
        self.assertEqual(owner["fingerprint"], certificate_fingerprint(der))
        self.assertEqual(owner["fingerprint"], pki.fingerprint(out / "leaf.pem"))
        leaf = _load(owner["cert_file"])
        lifetime = leaf.not_valid_after_utc - leaf.not_valid_before_utc
        self.assertLessEqual(lifetime.days, 2)
        with self.assertRaises(FileExistsError):
            pki.init_owner(out, "n")
        srv = pki.issue_server(out, self.root / "srv", "DNS:relay.example")
        self.assertEqual(set(srv), {"cert_file", "key_file", "ca_file"})
        self.assertFalse((self.root / "srv" / "server.csr").exists())
        self.assertFalse((self.root / "srv" / "server.ext").exists())
        self.assertEqual((self.root / "srv" / "server.key").stat().st_mode & 0o777, 0o600)

    def test_no_external_openssl_process(self):
        boom = mock.Mock(side_effect=AssertionError("external process spawned"))
        with mock.patch.object(subprocess, "run", boom), mock.patch.object(subprocess, "Popen", boom), \
                mock.patch.object(shutil, "which", return_value=None):
            owner = pki.init_owner(self.root / "np", "np")
            pki.issue_server(self.root / "np", self.root / "np-srv", "IP:127.0.0.1")
        boom.assert_not_called()
        self.assertTrue(Path(owner["cert_file"]).exists())

    def test_bad_san_rejected(self):
        pki.init_owner(self.root / "bad", "bad")
        for san in ("URI:http://x", "IP:not-an-ip", "", "DNS:"):
            with self.subTest(san=san), self.assertRaises(ValueError):
                pki.issue_server(self.root / "bad", self.root / f"srv-{abs(hash(san))}", san)

    def test_openssl3_x509_strict_verify_if_available(self):
        binary = _openssl3()
        if binary is None:
            self.skipTest("no OpenSSL 3 binary available")
        owner = pki.init_owner(self.root / "v", "v")
        srv = pki.issue_server(self.root / "v", self.root / "v-srv", "IP:127.0.0.1")
        for cert, purpose in ((owner["cert_file"], "sslclient"), (owner["cert_file"], "sslserver"),
                              (srv["cert_file"], "sslserver")):
            with self.subTest(cert=Path(cert).name, purpose=purpose):
                proc = subprocess.run([binary, "verify", "-x509_strict", "-purpose", purpose,
                                       "-CAfile", owner["ca_file"], cert], capture_output=True, text=True)
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertIn(": OK", proc.stdout)


if __name__ == "__main__":
    unittest.main()
