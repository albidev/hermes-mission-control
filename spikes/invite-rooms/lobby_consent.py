"""Owner-private consent attestations; no enrollment/native authority.

Public signing key is manually owner-confirmed alongside the fixed TLS route.
It is distinct from connector TLS keys: possession of a relay session alone
cannot manufacture a local-owner receipt. Never sent a local Bearer token.
"""
import base64
import json
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.exceptions import InvalidSignature
from lobby_control import ControlError


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def verify(public_key, receipt, signature, expected):
    try:
        if not isinstance(receipt, dict) or receipt != expected:
            raise ValueError()
        if not isinstance(signature, str) or len(signature) != 88:
            raise ValueError()
        Ed25519PublicKey.from_public_bytes(public_key).verify(base64.b64decode(signature, validate=True), canonical(receipt))
    except (ValueError, TypeError, InvalidSignature):
        raise ControlError('invalid_consent') from None
