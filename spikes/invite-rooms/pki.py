"""Per-owner PKI for the inner mTLS channel (F1 cross-host lab).

Each owner generates a private mini-CA and one leaf certificate on their own
machine. Only the CA certificate and the leaf fingerprint are handed to the
peer owner (out of band); private keys never leave the host and never reach the
relay. The peer trusts that CA file and additionally pins the exact leaf
fingerprint, which is bound to the native installation identity by the owner.

Usage:
  python pki.py init --out DIR --name NAME [--days 30]
  python pki.py server --ca-dir DIR --out DIR --san IP:127.0.0.1[,DNS:relay.example]

Certificates are built in-process with the `cryptography` library (no external
openssl/LibreSSL binary), with an explicit X.509 v3 profile that passes
VERIFY_X509_STRICT (Python >=3.13 default, OpenSSL `-x509_strict`):
  CA:     basicConstraints critical CA:TRUE pathlen:0; keyUsage critical
          keyCertSign+cRLSign; SKI; AKI (self).
  leaves: basicConstraints critical CA:FALSE; keyUsage critical
          digitalSignature+keyEncipherment; EKU (owner leaf: serverAuth+clientAuth,
          relay server: serverAuth); SKI; AKI = issuing CA SKI; SAN only on the
          relay server certificate (as before).
File layout and the fingerprint contract are unchanged.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import ipaddress
import json
import os
import shutil
import ssl
import subprocess
import sys
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tls_stream import certificate_fingerprint

_QUIET = {"check": True, "stdout": subprocess.DEVNULL, "stderr": subprocess.PIPE}
_KEY_BITS = 2048
_BACKDATE = _dt.timedelta(minutes=5)


def _openssl(*args: str) -> None:
    """Legacy helper kept only for existing test counterexamples; pki itself never calls it."""
    if shutil.which("openssl") is None:
        raise RuntimeError("openssl is required")
    subprocess.run(["openssl", *args], **_QUIET)


def _private(path: Path) -> None:
    os.chmod(path, 0o600)


def _write_key(path: Path, key: rsa.RSAPrivateKey) -> None:
    data = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    _private(path)


def _write_cert(path: Path, cert: x509.Certificate) -> None:
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def _name(cn: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def _parse_san(san: str) -> list[x509.GeneralName]:
    names: list[x509.GeneralName] = []
    for item in (san or "").split(","):
        kind, sep, value = item.strip().partition(":")
        if not sep or not value:
            raise ValueError(f"invalid SAN entry {item!r}")
        if kind == "IP":
            names.append(x509.IPAddress(ipaddress.ip_address(value)))  # ValueError on bad IP
        elif kind == "DNS":
            names.append(x509.DNSName(value))
        else:
            raise ValueError(f"unsupported SAN type {kind!r} (IP or DNS only)")
    if not names:
        raise ValueError("SAN is required")
    return names


def _build(*, subject: str, public_key, issuer: x509.Name, issuer_key, issuer_ski: x509.SubjectKeyIdentifier,
           days: int, ca: bool, eku: list | None = None, san: list[x509.GeneralName] | None = None) -> x509.Certificate:
    if days < 1:
        raise ValueError("days must be >= 1")
    now = _dt.datetime.now(_dt.timezone.utc)
    ski = x509.SubjectKeyIdentifier.from_public_key(public_key)
    builder = (x509.CertificateBuilder()
               .subject_name(_name(subject)).issuer_name(issuer).public_key(public_key)
               .serial_number(x509.random_serial_number())
               .not_valid_before(now - _BACKDATE).not_valid_after(now + _dt.timedelta(days=days))
               .add_extension(x509.BasicConstraints(ca=ca, path_length=0 if ca else None), critical=True)
               .add_extension(x509.KeyUsage(digital_signature=not ca, content_commitment=False,
                                            key_encipherment=not ca, data_encipherment=False,
                                            key_agreement=False, key_cert_sign=ca, crl_sign=ca,
                                            encipher_only=False, decipher_only=False), critical=True)
               .add_extension(ski, critical=False)
               .add_extension(x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(issuer_ski),
                              critical=False))
    if eku:
        builder = builder.add_extension(x509.ExtendedKeyUsage(eku), critical=False)
    if san:
        builder = builder.add_extension(x509.SubjectAlternativeName(san), critical=False)
    return builder.sign(issuer_key, hashes.SHA256())


def _new_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=_KEY_BITS)


def _load_ca(ca_dir: Path):
    ca = x509.load_pem_x509_certificate((ca_dir / "ca.pem").read_bytes())
    key = serialization.load_pem_private_key((ca_dir / "ca.key").read_bytes(), password=None)
    try:
        ski = ca.extensions.get_extension_for_class(x509.SubjectKeyIdentifier).value
    except x509.ExtensionNotFound:
        ski = x509.SubjectKeyIdentifier.from_public_key(ca.public_key())
    return ca, key, ski


def fingerprint(cert_file: Path) -> str:
    return certificate_fingerprint(ssl.PEM_cert_to_DER_cert(Path(cert_file).read_text()))


def init_owner(out: Path, name: str, *, days: int = 30) -> dict[str, str]:
    """Create ca.pem/ca.key and leaf.pem/leaf.key for one owner."""
    out.mkdir(parents=True, exist_ok=True, mode=0o700)
    if any((out / f).exists() for f in ("ca.key", "leaf.key")):
        raise FileExistsError(f"refusing to overwrite existing owner keys in {out}")
    ca_key, ca, key, leaf = (out / n for n in ("ca.key", "ca.pem", "leaf.key", "leaf.pem"))
    ca_private = _new_key()
    ca_name = _name(f"{name}-rooms-ca")
    ca_ski = x509.SubjectKeyIdentifier.from_public_key(ca_private.public_key())
    ca_cert = _build(subject=f"{name}-rooms-ca", public_key=ca_private.public_key(), issuer=ca_name,
                     issuer_key=ca_private, issuer_ski=ca_ski, days=days, ca=True)
    leaf_private = _new_key()
    leaf_cert = _build(subject=f"{name}-rooms-peer", public_key=leaf_private.public_key(), issuer=ca_name,
                       issuer_key=ca_private, issuer_ski=ca_ski, days=days, ca=False,
                       eku=[ExtendedKeyUsageOID.SERVER_AUTH, ExtendedKeyUsageOID.CLIENT_AUTH])
    _write_key(ca_key, ca_private)
    _write_cert(ca, ca_cert)
    _write_key(key, leaf_private)
    _write_cert(leaf, leaf_cert)
    return {"name": name, "ca_file": str(ca), "cert_file": str(leaf), "key_file": str(key),
            "fingerprint": fingerprint(leaf)}


def issue_server(ca_dir: Path, out: Path, san: str, *, days: int = 30) -> dict[str, str]:
    """Issue an outer-TLS server certificate (relay WSS) signed by ca_dir's CA."""
    names = _parse_san(san)
    out.mkdir(parents=True, exist_ok=True, mode=0o700)
    key, cert = out / "server.key", out / "server.pem"
    ca_cert, ca_private, ca_ski = _load_ca(ca_dir)
    private = _new_key()
    server_cert = _build(subject="rooms-relay", public_key=private.public_key(), issuer=ca_cert.subject,
                         issuer_key=ca_private, issuer_ski=ca_ski, days=days, ca=False,
                         eku=[ExtendedKeyUsageOID.SERVER_AUTH], san=names)
    if key.exists():
        key.unlink()  # re-issue replaces the previous relay key (old behaviour overwrote it too)
    _write_key(key, private)
    _write_cert(cert, server_cert)
    return {"cert_file": str(cert), "key_file": str(key), "ca_file": str(ca_dir / "ca.pem")}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument("--out", required=True, type=Path)
    init.add_argument("--name", required=True)
    init.add_argument("--days", type=int, default=30)
    server = sub.add_parser("server")
    server.add_argument("--ca-dir", required=True, type=Path)
    server.add_argument("--out", required=True, type=Path)
    server.add_argument("--san", required=True)
    args = parser.parse_args(argv)
    if args.command == "init":
        result = init_owner(args.out, args.name, days=args.days)
        # Only public material is printed: the peer needs ca_file content + fingerprint.
        print(json.dumps({k: result[k] for k in ("name", "ca_file", "cert_file", "fingerprint")}, indent=2))
    else:
        print(json.dumps(issue_server(args.ca_dir, args.out, args.san), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
