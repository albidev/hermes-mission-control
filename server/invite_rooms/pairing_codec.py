"""Pairing codes (P3b.1a-1): offer / accept / reveal. Pure functions and dataclasses, no IO.

Design: ``docs/plans/invite-rooms-p3b-pairing.md`` section 1 ("Why three codes", "Encoding", "Payloads") and the
normative amendment R1-08.

Wire
----
``mcpair1.<kind>.<b64url(payload)>.<b64url(sig)>`` with ``kind`` in ``offer|accept|reveal``; base64url is unpadded and
canonical (re-encoding the decoded bytes must reproduce the segment); the signature is exactly 64 bytes.
The cap (``MAX_WIRE_CHARS``) applies to the WHOLE wire string and is checked before anything is decoded;
``max_payload_bytes(kind)`` is the largest payload that still fits for that kind.

Payload
-------
Strict JSON: duplicate keys, floats, NaN/Infinity, bools used as ints, nesting deeper than ``MAX_DEPTH`` (root = 1),
unknown/missing keys, out-of-range values and any byte sequence that differs from ``consent.canonical`` of the
parsed value are rejected.  ``sig = Ed25519(consent_key, "mc-pair-sig-v1\\0" || kind || "\\0" || payload_bytes)``.

Commit-then-reveal
------------------
A puts ``C = SHA256("mc-pair-commit-v1" || r_A)`` in the offer.  B's accept binds ``H(offer)`` and carries a fresh
``r_B`` (its ``nonce``).  A's reveal binds ``H(offer)``, ``H(accept)`` and discloses ``r_A``; B checks ``C``.
``H(x)`` is SHA-256 over the raw PAYLOAD bytes of the respective code; KDF inputs are the raw concatenated hashes.

Errors
------
Everything fails with ``PairingCodecError`` whose message is one of two generic machine codes and never contains
input, keys, codes or the SAS: ``pairing_code_invalid`` (syntax, schema, signature, commitment) and
``pairing_unavailable`` (expired, or bound to a different session).
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from invite_rooms.consent import canonical

PREFIX = "mcpair1"
KINDS = ("offer", "accept", "reveal")
VERSION = 1

MAX_WIRE_CHARS = 12 * 1024
MAX_PAYLOAD_BYTES = 9 * 1024
MAX_DEPTH = 4
MAX_TTL_SECONDS = 15 * 60
NONCE_BYTES = 32
SIGNATURE_BYTES = 64
_SIG_B64_CHARS = 86  # ceil(64 * 4 / 3) unpadded

SIG_DOMAIN = b"mc-pair-sig-v1\x00"
COMMIT_DOMAIN = b"mc-pair-commit-v1"
SAS_SALT, SAS_INFO = b"mc-pair-sas-salt-v1", b"mc-pair-sas-v1"
LINK_SALT, LINK_INFO = b"mc-pair-link-salt-v1", b"mc-pair-link-v1"

INVALID = "pairing_code_invalid"
UNAVAILABLE = "pairing_unavailable"

# per-field ranges
MAX_CA_PEM_CHARS = 8192
MAX_STRING_CHARS = 2048
MAX_LIST_ITEMS = 64
MAX_MAPPING_KEYS = 64
MAX_KEY_CHARS = 128
MAX_INT = 2 ** 53
MAX_EXPIRES_AT = 2 ** 40

_IDENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9:_.\-]*")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_B64URL = re.compile(r"[A-Za-z0-9_\-]+")
_PEM_HEAD, _PEM_TAIL = "-----BEGIN CERTIFICATE-----\n", "-----END CERTIFICATE-----\n"
_PEM_BODY = re.compile(r"[A-Za-z0-9+/=\n]+")


class PairingCodecError(ValueError):
    """Sanitized codec failure: ``str(exc)`` is exactly one of the two generic codes."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _invalid():
    raise PairingCodecError(INVALID) from None


def _unavailable():
    raise PairingCodecError(UNAVAILABLE) from None


# ---------------------------------------------------------------------------------------------------------------
# base64url / limits
# ---------------------------------------------------------------------------------------------------------------
def b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64d(text: str, *, exact: int | None = None, max_bytes: int | None = None) -> bytes:
    """Strict base64url: URL-safe alphabet only, no padding, canonical trailing bits."""
    if type(text) is not str or not text or not _B64URL.fullmatch(text) or len(text) % 4 == 1:
        _invalid()
    try:
        raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError):
        _invalid()
    if b64e(raw) != text or (exact is not None and len(raw) != exact) or (max_bytes is not None and len(raw) > max_bytes):
        _invalid()
    return raw


def max_payload_bytes(kind: str) -> int:
    """Largest payload that keeps the WHOLE wire string within ``MAX_WIRE_CHARS`` for this kind."""
    if kind not in KINDS:
        raise ValueError("kind")
    overhead = len(PREFIX) + 1 + len(kind) + 1 + 1 + _SIG_B64_CHARS  # "mcpair1." kind "." <payload> "." <sig>
    return min(MAX_PAYLOAD_BYTES, (MAX_WIRE_CHARS - overhead) * 3 // 4)


def _sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def commitment_for(r_a: bytes) -> str:
    _require_nonce(r_a)
    return _sha256(COMMIT_DOMAIN + r_a).hex()


def _require_nonce(value) -> None:
    if type(value) is not bytes or len(value) != NONCE_BYTES:
        _invalid()


# ---------------------------------------------------------------------------------------------------------------
# strict JSON
# ---------------------------------------------------------------------------------------------------------------
def _depth_ok(data: bytes) -> bool:
    depth, in_string, escaped = 0, False, False
    for byte in data:
        if in_string:
            if escaped:
                escaped = False
            elif byte == 0x5C:
                escaped = True
            elif byte == 0x22:
                in_string = False
        elif byte == 0x22:
            in_string = True
        elif byte in (0x5B, 0x7B):
            depth += 1
            if depth > MAX_DEPTH:
                return False
        elif byte in (0x5D, 0x7D):
            depth -= 1
    return True


def _object_pairs(pairs):
    out: dict = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate")
        out[key] = value
    return out


def _reject_number(_text):
    raise ValueError("number")


def _strict_loads(data: bytes) -> dict:
    if not _depth_ok(data):
        _invalid()
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=_object_pairs,
                           parse_float=_reject_number, parse_constant=_reject_number)
    except (ValueError, RecursionError):
        _invalid()
    if type(value) is not dict:
        _invalid()
    try:
        if canonical(value) != data:
            _invalid()
    except (TypeError, ValueError):
        _invalid()
    return value


def _check_tree(value: Any) -> None:
    """Bounded scalars/containers for the self-asserted catalog; bool is never an int."""
    kind = type(value)
    if kind is bool:
        return
    if kind is int:
        if abs(value) > MAX_INT:
            _invalid()
    elif kind is str:
        if len(value) > MAX_STRING_CHARS:
            _invalid()
    elif kind is list:
        if len(value) > MAX_LIST_ITEMS:
            _invalid()
        for item in value:
            _check_tree(item)
    elif kind is dict:
        if len(value) > MAX_MAPPING_KEYS:
            _invalid()
        for key, item in value.items():
            if type(key) is not str or not key or len(key) > MAX_KEY_CHARS:
                _invalid()
            _check_tree(item)
    else:
        _invalid()


def _ident(value, maxlen: int) -> str:
    if type(value) is not str or not 1 <= len(value) <= maxlen or not _IDENT.fullmatch(value):
        _invalid()
    return value


def _hex64(value) -> str:
    if type(value) is not str or not _HEX64.fullmatch(value):
        _invalid()
    return value


def _int(value, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        _invalid()
    return value


def _pem(value) -> str:
    if (type(value) is not str or not value.isascii() or not 64 <= len(value) <= MAX_CA_PEM_CHARS
            or not value.startswith(_PEM_HEAD) or not value.endswith(_PEM_TAIL)
            or not _PEM_BODY.fullmatch(value[len(_PEM_HEAD):-len(_PEM_TAIL)] or "!") or "\n\n" in value):
        _invalid()
    return value


# ---------------------------------------------------------------------------------------------------------------
# dataclasses
# ---------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True, eq=True)
class PeerIdentity:
    """The public, self-asserted identity a code carries. The catalog is NOT trusted until core validation."""

    installation_id: str
    profile: str
    catalog: Mapping[str, Any] = field(compare=True)
    consent_public_key: bytes
    ca_pem: str
    leaf_fingerprint: str

    __hash__ = None  # type: ignore[assignment]  (mapping field)

    def as_mapping(self) -> dict:
        return {"installation_id": self.installation_id, "profile": self.profile, "catalog": self.catalog,
                "consent_public_key": b64e(self.consent_public_key),
                "tls": {"ca_pem": self.ca_pem, "leaf_fingerprint": self.leaf_fingerprint}}


@dataclass(frozen=True)
class Offer:
    wire: str
    payload: bytes
    digest: bytes  # H(offer): SHA-256 over the payload bytes
    identity: PeerIdentity
    expires_at: int
    nonce: bytes  # session label nonce (NOT r_A)
    commitment: str  # hex H("mc-pair-commit-v1" || r_A)

    @property
    def digest_hex(self) -> str:
        return self.digest.hex()


@dataclass(frozen=True)
class Accept:
    wire: str
    payload: bytes
    digest: bytes  # H(accept)
    offer_digest: bytes
    identity: PeerIdentity
    expires_at: int
    r_b: bytes

    @property
    def digest_hex(self) -> str:
        return self.digest.hex()


@dataclass(frozen=True)
class Reveal:
    wire: str
    payload: bytes
    digest: bytes
    offer_digest: bytes
    accept_digest: bytes
    expires_at: int
    r_a: bytes


# ---------------------------------------------------------------------------------------------------------------
# payload schemas
# ---------------------------------------------------------------------------------------------------------------
_COMMON = {"v", "kind", "expires_at", "nonce"}
_IDENTITY_KEYS = {"installation_id", "profile", "catalog", "consent_public_key", "tls"}
_KEYSETS = {
    "offer": _COMMON | _IDENTITY_KEYS | {"commit"},
    "accept": _COMMON | _IDENTITY_KEYS | {"offer_hash"},
    "reveal": _COMMON | {"offer_hash", "accept_hash"},
}


def _check_payload(kind: str, value: dict) -> None:
    if set(value) != _KEYSETS[kind]:
        _invalid()
    _int(value["v"], VERSION, VERSION)
    if value["kind"] != kind:
        _invalid()
    _int(value["expires_at"], 0, MAX_EXPIRES_AT)
    _b64d(value["nonce"], exact=NONCE_BYTES)
    if kind == "reveal":
        _hex64(value["offer_hash"]); _hex64(value["accept_hash"])
        return
    _ident(value["installation_id"], 128)
    _ident(value["profile"], 64)
    catalog = value["catalog"]
    if type(catalog) is not dict or not catalog:
        _invalid()
    _check_tree(catalog)
    if catalog.get("installation_id") != value["installation_id"]:
        _invalid()
    _b64d(value["consent_public_key"], exact=32)
    tls = value["tls"]
    if type(tls) is not dict or set(tls) != {"ca_pem", "leaf_fingerprint"}:
        _invalid()
    _pem(tls["ca_pem"]); _hex64(tls["leaf_fingerprint"])
    if kind == "offer":
        _hex64(value["commit"])
    else:
        _hex64(value["offer_hash"])


def _identity_from(value: dict) -> PeerIdentity:
    return PeerIdentity(value["installation_id"], value["profile"], value["catalog"],
                        _b64d(value["consent_public_key"], exact=32), value["tls"]["ca_pem"],
                        value["tls"]["leaf_fingerprint"])


def _sign_message(kind: str, payload: bytes) -> bytes:
    return SIG_DOMAIN + kind.encode("ascii") + b"\x00" + payload


def _seal(kind: str, key: Ed25519PrivateKey, payload_value: dict) -> tuple[str, bytes]:
    """Encode + sign. The result is re-validated through the SAME strict decoder the receiver uses."""
    payload = canonical(payload_value)
    if len(payload) > max_payload_bytes(kind):
        _invalid()
    _check_payload(kind, _strict_loads(payload))
    sig = key.sign(_sign_message(kind, payload))
    return f"{PREFIX}.{kind}.{b64e(payload)}.{b64e(sig)}", payload


def _open(kind: str, wire: str, verify_key: bytes | None) -> tuple[bytes, dict]:
    """Return (payload_bytes, value). ``verify_key=None`` means: use the key embedded in the payload."""
    if type(wire) is not str or len(wire) > MAX_WIRE_CHARS or not wire.isascii():
        _invalid()
    parts = wire.split(".")
    if len(parts) != 4 or parts[0] != PREFIX or parts[1] != kind:
        _invalid()
    b64_payload, b64_sig = parts[2], parts[3]
    if len(b64_payload) > (max_payload_bytes(kind) * 4 + 2) // 3 + 1 or len(b64_sig) != _SIG_B64_CHARS:
        _invalid()
    payload = _b64d(b64_payload, max_bytes=max_payload_bytes(kind))
    signature = _b64d(b64_sig, exact=SIGNATURE_BYTES)
    value = _strict_loads(payload)
    _check_payload(kind, value)
    public = verify_key if verify_key is not None else _b64d(value["consent_public_key"], exact=32)
    try:
        Ed25519PublicKey.from_public_bytes(public).verify(signature, _sign_message(kind, payload))
    except (InvalidSignature, ValueError):
        _invalid()
    return payload, value


def _check_time(expires_at: int, now: int | None, *, upper: bool = True) -> None:
    if now is None:
        return
    _int(now, 0, MAX_EXPIRES_AT)
    if expires_at < now:
        _unavailable()
    if upper and expires_at > now + MAX_TTL_SECONDS:
        _invalid()


def _require_key(key, identity_or_pub) -> None:
    pub = identity_or_pub.consent_public_key if isinstance(identity_or_pub, PeerIdentity) else identity_or_pub
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    if not isinstance(key, Ed25519PrivateKey) or key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw) != pub:
        _invalid()


def _require_identity(identity) -> None:
    if not isinstance(identity, PeerIdentity):
        _invalid()


# ---------------------------------------------------------------------------------------------------------------
# offer (A)
# ---------------------------------------------------------------------------------------------------------------
def build_offer(key: Ed25519PrivateKey, identity: PeerIdentity, *, nonce: bytes, r_a: bytes, expires_at: int,
                now: int) -> Offer:
    """A's offer. ``r_a`` is NOT in the offer (only its commitment); the caller journals it."""
    _require_identity(identity); _require_key(key, identity); _require_nonce(nonce); _require_nonce(r_a)
    _check_time(expires_at, now)
    value = {"v": VERSION, "kind": "offer", "expires_at": expires_at, "nonce": b64e(nonce),
             "commit": commitment_for(r_a), **identity.as_mapping()}
    wire, _ = _seal("offer", key, value)
    return parse_offer(wire, now=now)


def parse_offer(wire: str, *, now: int | None) -> Offer:
    """Verify a received offer (self-signed). ``now=None`` skips ONLY the time checks (journal reload)."""
    payload, value = _open("offer", wire, None)
    _check_time(value["expires_at"], now)
    return Offer(wire, payload, _sha256(payload), _identity_from(value), value["expires_at"],
                 _b64d(value["nonce"], exact=NONCE_BYTES), value["commit"])


# ---------------------------------------------------------------------------------------------------------------
# accept (B)
# ---------------------------------------------------------------------------------------------------------------
def build_accept(key: Ed25519PrivateKey, identity: PeerIdentity, offer: Offer, *, r_b: bytes, expires_at: int,
                 now: int) -> Accept:
    _require_identity(identity); _require_key(key, identity); _require_nonce(r_b)
    if not isinstance(offer, Offer) or expires_at > offer.expires_at:
        _invalid()
    _check_time(offer.expires_at, now, upper=False)
    _check_time(expires_at, now)
    value = {"v": VERSION, "kind": "accept", "expires_at": expires_at, "nonce": b64e(r_b),
             "offer_hash": offer.digest_hex, **identity.as_mapping()}
    wire, _ = _seal("accept", key, value)
    return parse_accept(wire, offer=offer, now=now)


def parse_accept(wire: str, *, offer: Offer, now: int | None) -> Accept:
    """A verifies B's accept against its own open offer."""
    payload, value = _open("accept", wire, None)
    if not isinstance(offer, Offer):
        _invalid()
    if not hmac.compare_digest(bytes.fromhex(value["offer_hash"]), offer.digest):
        _unavailable()  # bound to another session
    _check_time(value["expires_at"], now)
    _check_time(offer.expires_at, now, upper=False)
    identity = _identity_from(value)
    if value["expires_at"] > offer.expires_at or identity.installation_id == offer.identity.installation_id:
        _invalid()
    return Accept(wire, payload, _sha256(payload), offer.digest, identity, value["expires_at"],
                  _b64d(value["nonce"], exact=NONCE_BYTES))


# ---------------------------------------------------------------------------------------------------------------
# reveal (A)
# ---------------------------------------------------------------------------------------------------------------
def build_reveal(key: Ed25519PrivateKey, offer: Offer, accept: Accept, *, r_a: bytes, expires_at: int,
                 now: int) -> Reveal:
    """Refuses unless ``r_a`` opens the commitment published in this very offer."""
    if not isinstance(offer, Offer) or not isinstance(accept, Accept):
        _invalid()
    _require_key(key, offer.identity); _require_nonce(r_a)
    if (not hmac.compare_digest(bytes.fromhex(commitment_for(r_a)), bytes.fromhex(offer.commitment))
            or not hmac.compare_digest(accept.offer_digest, offer.digest) or expires_at > accept.expires_at):
        _invalid()
    _check_time(accept.expires_at, now, upper=False)
    _check_time(expires_at, now)
    value = {"v": VERSION, "kind": "reveal", "expires_at": expires_at, "nonce": b64e(r_a),
             "offer_hash": offer.digest_hex, "accept_hash": accept.digest_hex}
    wire, _ = _seal("reveal", key, value)
    return parse_reveal(wire, offer=offer, accept=accept, now=now)


def parse_reveal(wire: str, *, offer: Offer, accept: Accept, now: int | None) -> Reveal:
    """B verifies A's reveal against ITS OWN offer/accept: signature by A's key from the offer, both hashes,
    and the commitment. A reveal for any other session or accept fails ``pairing_unavailable``."""
    if not isinstance(offer, Offer) or not isinstance(accept, Accept):
        _invalid()
    payload, value = _open("reveal", wire, offer.identity.consent_public_key)
    if (not hmac.compare_digest(bytes.fromhex(value["offer_hash"]), offer.digest)
            or not hmac.compare_digest(bytes.fromhex(value["accept_hash"]), accept.digest)
            or not hmac.compare_digest(accept.offer_digest, offer.digest)):
        _unavailable()
    r_a = _b64d(value["nonce"], exact=NONCE_BYTES)
    if not hmac.compare_digest(bytes.fromhex(commitment_for(r_a)), bytes.fromhex(offer.commitment)):
        _invalid()
    _check_time(value["expires_at"], now)
    if value["expires_at"] > accept.expires_at:
        _invalid()
    return Reveal(wire, payload, _sha256(payload), offer.digest, accept.digest, value["expires_at"], r_a)


# ---------------------------------------------------------------------------------------------------------------
# derived values
# ---------------------------------------------------------------------------------------------------------------
def _hkdf(ikm: bytes, *, salt: bytes, info: bytes, length: int) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt, info=info).derive(ikm)


def sas_for(offer_digest: bytes, accept_digest: bytes, r_a: bytes) -> str:
    """6-digit SAS: HKDF(H(offer) || H(accept) || r_A, 8 bytes) mod 10^6, zero-padded."""
    if type(offer_digest) is not bytes or type(accept_digest) is not bytes or len(offer_digest) != 32 \
            or len(accept_digest) != 32:
        _invalid()
    _require_nonce(r_a)
    raw = _hkdf(offer_digest + accept_digest + r_a, salt=SAS_SALT, info=SAS_INFO, length=8)
    return f"{int.from_bytes(raw, 'big') % 1_000_000:06d}"


def endpoint_id_for(offer_digest: bytes, accept_digest: bytes) -> str:
    """``link-<hex16>``: public link name derived from the transcript, never accepted from input."""
    if type(offer_digest) is not bytes or type(accept_digest) is not bytes or len(offer_digest) != 32 \
            or len(accept_digest) != 32:
        _invalid()
    return "link-" + _hkdf(offer_digest + accept_digest, salt=LINK_SALT, info=LINK_INFO, length=16).hex()


def sas_for_session(offer: Offer, accept: Accept, r_a: bytes) -> str:
    if not hmac.compare_digest(accept.offer_digest, offer.digest):
        _unavailable()
    return sas_for(offer.digest, accept.digest, r_a)


def endpoint_id_for_session(offer: Offer, accept: Accept) -> str:
    if not hmac.compare_digest(accept.offer_digest, offer.digest):
        _unavailable()
    return endpoint_id_for(offer.digest, accept.digest)
