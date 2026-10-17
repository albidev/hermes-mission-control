"""Per-owner PKI for the inner mTLS channel (F1 cross-host lab).

Each owner generates a private mini-CA and one leaf certificate on their own
machine. Only the CA certificate and the leaf fingerprint are handed to the
peer owner (out of band); private keys never leave the host and never reach the
relay. The peer trusts that CA file and additionally pins the exact leaf
fingerprint, which is bound to the native installation identity by the owner.

Usage:
  python pki.py init --out DIR --name NAME [--days 30]
  python pki.py server --ca-dir DIR --out DIR --san IP:127.0.0.1|DNS:relay.example
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import ssl
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tls_stream import certificate_fingerprint

_QUIET = {"check": True, "stdout": subprocess.DEVNULL, "stderr": subprocess.PIPE}


def _openssl(*args: str) -> None:
    if shutil.which("openssl") is None:
        raise RuntimeError("openssl is required")
    subprocess.run(["openssl", *args], **_QUIET)


def _private(path: Path) -> None:
    os.chmod(path, 0o600)


def fingerprint(cert_file: Path) -> str:
    return certificate_fingerprint(ssl.PEM_cert_to_DER_cert(Path(cert_file).read_text()))


def init_owner(out: Path, name: str, *, days: int = 30) -> dict[str, str]:
    """Create ca.pem/ca.key and leaf.pem/leaf.key for one owner."""
    out.mkdir(parents=True, exist_ok=True, mode=0o700)
    if any((out / f).exists() for f in ("ca.key", "leaf.key")):
        raise FileExistsError(f"refusing to overwrite existing owner keys in {out}")
    ca_key, ca, key, csr, leaf = (out / n for n in ("ca.key", "ca.pem", "leaf.key", "leaf.csr", "leaf.pem"))
    _openssl("req", "-x509", "-new", "-nodes", "-newkey", "rsa:2048", "-keyout", str(ca_key), "-out", str(ca),
             "-subj", f"/CN={name}-rooms-ca", "-days", str(days))
    _private(ca_key)
    _openssl("req", "-new", "-nodes", "-newkey", "rsa:2048", "-keyout", str(key), "-out", str(csr),
             "-subj", f"/CN={name}-rooms-peer")
    _private(key)
    _openssl("x509", "-req", "-in", str(csr), "-CA", str(ca), "-CAkey", str(ca_key), "-CAcreateserial",
             "-out", str(leaf), "-days", str(days))
    csr.unlink()
    return {"name": name, "ca_file": str(ca), "cert_file": str(leaf), "key_file": str(key),
            "fingerprint": fingerprint(leaf)}


def issue_server(ca_dir: Path, out: Path, san: str, *, days: int = 30) -> dict[str, str]:
    """Issue an outer-TLS server certificate (relay WSS) signed by ca_dir's CA."""
    out.mkdir(parents=True, exist_ok=True, mode=0o700)
    key, csr, cert, ext = (out / n for n in ("server.key", "server.csr", "server.pem", "server.ext"))
    ext.write_text(f"subjectAltName={san}\nextendedKeyUsage=serverAuth\n")
    _openssl("req", "-new", "-nodes", "-newkey", "rsa:2048", "-keyout", str(key), "-out", str(csr),
             "-subj", "/CN=rooms-relay")
    _private(key)
    _openssl("x509", "-req", "-in", str(csr), "-CA", str(ca_dir / "ca.pem"), "-CAkey", str(ca_dir / "ca.key"),
             "-CAcreateserial", "-out", str(cert), "-days", str(days), "-extfile", str(ext))
    csr.unlink(); ext.unlink()
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
