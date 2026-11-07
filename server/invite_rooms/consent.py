"""Owner-private Ed25519 consent attestations for the joining-owner side.

Canonical encoding and signature scheme: JSON with sorted keys, no NaN/Infinity,
raw Ed25519 signatures base64-encoded. This module holds ONLY the pure
cryptographic primitives (no I/O, no control-channel knowledge) so the
companion real-lab parity test can verify interoperability without this
product package ever importing spike code.

The consent signing key is a distinct credential from the invitation secret
and from any transport/TLS credential: possession of one must never imply
the others. The key loader enforces 0700 parent / 0600 file permissions and
refuses otherwise rather than silently relaxing the requirement.
"""
from __future__ import annotations

import base64
import json
import os
import stat
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, PublicFormat, NoEncryption
from cryptography.exceptions import InvalidSignature


class ConsentError(ValueError):
    """Machine code only: never interpolates remote values or secrets."""


def canonical(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def verify(public_key: bytes, receipt: dict, signature: str, expected: dict) -> None:
    """Verify a joining-owner consent receipt. Raises ConsentError on any mismatch."""
    try:
        if not isinstance(receipt, dict) or receipt != expected:
            raise ValueError()
        if not isinstance(signature, str) or len(signature) != 88:
            raise ValueError()
        if not isinstance(public_key, (bytes, bytearray)) or len(public_key) != 32:
            raise ValueError()
        Ed25519PublicKey.from_public_bytes(bytes(public_key)).verify(
            base64.b64decode(signature, validate=True), canonical(receipt)
        )
    except (ValueError, TypeError, InvalidSignature):
        raise ConsentError("invalid_consent") from None


class ConsentKey:
    """Owner-private Ed25519 signing key, loaded from a 0700/0600-protected file.

    Distinct credential from the invitation secret and from transport/TLS
    material: this class never sees either of those, and its public key is
    exposed only via .public_bytes for registration with the host out-of-band.
    """

    def __init__(self, root: Path):
        root = Path(root)
        if root.exists() and not root.is_dir():
            raise ConsentError("private_storage_required")
        existed = root.exists()
        root.mkdir(parents=True, exist_ok=True)
        if not existed:
            # 0700 only for a directory THIS code created; a pre-existing
            # parent with a different mode is rejected, never silently
            # relaxed (R6).
            os.chmod(root, 0o700)
        if stat.S_IMODE(os.stat(root).st_mode) != 0o700:
            raise ConsentError("private_storage_required")
        keyfile = root / "consent.key"
        try:
            fd = os.open(keyfile, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            fd = os.open(keyfile, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                if stat.S_IMODE(os.fstat(fd).st_mode) != 0o600:
                    raise ConsentError("private_storage_required")
                with os.fdopen(fd, "rb") as f:
                    raw = f.read(33)
            except BaseException:
                raise
            if len(raw) != 32:
                raise ConsentError("private_storage_required")
            self._key = Ed25519PrivateKey.from_private_bytes(raw)
        else:
            self._key = Ed25519PrivateKey.generate()
            with os.fdopen(fd, "wb") as f:
                f.write(self._key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption()))
                f.flush()
                os.fsync(f.fileno())
        self.public_bytes = self._key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)

    def sign(self, receipt: dict) -> str:
        return base64.b64encode(self._key.sign(canonical(receipt))).decode()
