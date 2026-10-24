"""P3b.1a-1: pairing codec (offer / accept / reveal) -- vectors, size gate, strict decoding, SAS grinding."""
from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import json
import os
import re
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from invite_rooms import identity as identity_mod
from invite_rooms import pairing_codec as pc
from invite_rooms.consent import canonical
from invite_rooms.pairing_codec import PairingCodecError, PeerIdentity

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "pairing_vectors.json"
NOW = 1_800_000_000
INSTALL_A, INSTALL_B = "install:" + "a" * 32, "install:" + "b" * 32
KEY_A = Ed25519PrivateKey.from_private_bytes(b"\x01" * 32)
KEY_B = Ed25519PrivateKey.from_private_bytes(b"\x02" * 32)
KEY_X = Ed25519PrivateKey.from_private_bytes(b"\x03" * 32)  # the attacker
NONCE_A, R_A, R_B = b"\x11" * 32, b"\x22" * 32, b"\x33" * 32
GENERIC = {pc.INVALID, pc.UNAVAILABLE}
def _pub(key: Ed25519PrivateKey) -> bytes:
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    return key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)


def make_catalog(installation_id: str, profile: str = "default", toolsets=("bot_room", "web", "file")) -> dict:
    """A GatewayRoomCatalog-shaped mapping (digest-correct) with three enabled toolsets and an endpoint."""
    from gateway.hosted_room_peer import catalog_mapping
    unsigned = {"version": 1, "target_profile": profile, "enabled_toolsets": sorted(toolsets),
                "approval_mode": "manual", "max_iterations": 10}
    policy = {**unsigned, "policy_digest": hashlib.sha256(json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode("ascii")).hexdigest()}
    return catalog_mapping(installation_id=installation_id, protocol_versions=(2,), link_modes=("direct",),
                           persistent_process=True, text=True, attachments=False,
                           endpoint={"available": True, "url": "http://127.0.0.1:19001/links/unit-link",
                                     "transport_security": "loopback"},
                           target_profile=profile, execution_policy=policy)


def _inputs() -> dict:
    if FIXTURE.exists():
        return json.loads(FIXTURE.read_text())["inputs"]
    with tempfile.TemporaryDirectory(dir=os.environ.get("MC_TEST_TMP")) as tmp:
        bundles = {}
        for side, install in (("a", INSTALL_A), ("b", INSTALL_B)):
            bundles[side] = identity_mod.initialize_identity(Path(tmp) / side, installation_id=install, profile="default")
    return {"now": NOW, "a": {"installation_id": INSTALL_A, "profile": "default", "seed": (b"\x01" * 32).hex(),
                              "ca_pem": bundles["a"].ca_pem, "leaf_fingerprint": bundles["a"].leaf_fingerprint,
                              "catalog": make_catalog(INSTALL_A)},
            "b": {"installation_id": INSTALL_B, "profile": "default", "seed": (b"\x02" * 32).hex(),
                  "ca_pem": bundles["b"].ca_pem, "leaf_fingerprint": bundles["b"].leaf_fingerprint,
                  "catalog": make_catalog(INSTALL_B)},
            "nonce_a": NONCE_A.hex(), "r_a": R_A.hex(), "r_b": R_B.hex(),
            "offer_ttl": 600, "accept_ttl": 500, "reveal_ttl": 400}


def identity_of(side: dict) -> PeerIdentity:
    key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(side["seed"]))
    return PeerIdentity(side["installation_id"], side["profile"], copy.deepcopy(side["catalog"]), _pub(key),
                        side["ca_pem"], side["leaf_fingerprint"])


def run_session(inp: dict) -> dict:
    now = inp["now"]
    key_a = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(inp["a"]["seed"]))
    key_b = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(inp["b"]["seed"]))
    r_a, r_b = bytes.fromhex(inp["r_a"]), bytes.fromhex(inp["r_b"])
    offer = pc.build_offer(key_a, identity_of(inp["a"]), nonce=bytes.fromhex(inp["nonce_a"]), r_a=r_a,
                           expires_at=now + inp["offer_ttl"], now=now)
    accept = pc.build_accept(key_b, identity_of(inp["b"]), pc.parse_offer(offer.wire, now=now), r_b=r_b,
                             expires_at=now + inp["accept_ttl"], now=now)
    a_accept = pc.parse_accept(accept.wire, offer=offer, now=now)
    reveal = pc.build_reveal(key_a, offer, a_accept, r_a=r_a, expires_at=now + inp["reveal_ttl"], now=now)
    b_reveal = pc.parse_reveal(reveal.wire, offer=pc.parse_offer(offer.wire, now=now), accept=accept, now=now)
    return {"offer": offer, "accept": accept, "a_accept": a_accept, "reveal": reveal, "b_reveal": b_reveal,
            "sas_a": pc.sas_for(offer.digest, a_accept.digest, r_a),
            "sas_b": pc.sas_for(offer.digest, accept.digest, b_reveal.r_a),
            "endpoint_a": pc.endpoint_id_for(offer.digest, a_accept.digest),
            "endpoint_b": pc.endpoint_id_for(offer.digest, accept.digest)}


def vector_outputs(inp: dict) -> dict:
    s = run_session(inp)
    return {"commit": s["offer"].commitment, "offer_wire": s["offer"].wire, "offer_hash": s["offer"].digest_hex,
            "accept_wire": s["accept"].wire, "accept_hash": s["accept"].digest_hex,
            "reveal_wire": s["reveal"].wire, "reveal_hash": s["reveal"].digest.hex(),
            "sas": s["sas_a"], "endpoint_id": s["endpoint_a"]}


def forge(kind: str, payload: bytes, key: Ed25519PrivateKey = KEY_A, *, sig: bytes | None = None) -> str:
    """A correctly SIGNED code around arbitrary payload bytes: only the payload is wrong."""
    signature = sig if sig is not None else key.sign(pc._sign_message(kind, payload))
    return f"{pc.PREFIX}.{kind}.{pc.b64e(payload)}.{pc.b64e(signature)}"


def _hkdf(ikm: bytes, salt: bytes, info: bytes, length: int) -> bytes:
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    out, block = b"", b""
    for counter in range(1, -(-length // 32) + 1):
        block = hmac.new(prk, block + info + bytes([counter]), hashlib.sha256).digest()
        out += block
    return out[:length]


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.inp = _inputs()
        cls.s = run_session(cls.inp)
        cls.offer, cls.accept = cls.s["offer"], cls.s["accept"]

    def assertGeneric(self, code, fn, *args, **kwargs):
        with self.assertRaises(PairingCodecError) as cm:
            fn(*args, **kwargs)
        self.assertEqual(str(cm.exception), code)
        self.assertEqual(cm.exception.code, code)
        self.assertIn(code, GENERIC)
        self.assertIsNone(cm.exception.__cause__)  # nothing from the input leaks through chaining
        return cm.exception

    def offer_value(self) -> dict:
        return json.loads(self.offer.payload)

    def accept_value(self) -> dict:
        return json.loads(self.accept.payload)

    def parse_offer(self, wire):
        return pc.parse_offer(wire, now=NOW)


class RoundTripAndVectorTests(Base):
    def test_three_codes_round_trip_and_both_sides_derive_the_same_sas_and_endpoint(self):
        s = self.s
        self.assertEqual(s["sas_a"], s["sas_b"])
        self.assertRegex(s["sas_a"], r"^\d{6}$")
        self.assertEqual(s["endpoint_a"], s["endpoint_b"])
        self.assertRegex(s["endpoint_a"], r"^link-[0-9a-f]{32}$")
        self.assertEqual(s["a_accept"].identity, s["accept"].identity)
        self.assertEqual(s["b_reveal"].r_a, bytes.fromhex(self.inp["r_a"]))
        self.assertNotIn(self.inp["r_a"], self.offer.wire)  # r_A never rides in the offer
        self.assertNotIn(pc.b64e(bytes.fromhex(self.inp["r_a"])), self.offer.wire)
        for kind, code in (("offer", s["offer"]), ("accept", s["accept"]), ("reveal", s["reveal"])):
            head, kd, _payload, sig = code.wire.split(".")
            self.assertEqual((head, kd), ("mcpair1", kind))
            self.assertEqual(len(sig), 86)
            self.assertNotIn("=", code.wire)

    def test_vectors_regenerate_exactly_and_match_an_independent_kdf(self):
        if os.environ.get("MC_REGEN_PAIRING_VECTORS") == "1":  # maintainer switch; never set in CI
            FIXTURE.parent.mkdir(exist_ok=True)
            FIXTURE.write_text(json.dumps({"inputs": self.inp, "outputs": vector_outputs(self.inp)}, indent=1, sort_keys=True) + "\n")
        stored = json.loads(FIXTURE.read_text())
        outputs = vector_outputs(stored["inputs"])
        self.assertEqual(outputs, stored["outputs"])
        inp = stored["inputs"]
        h_offer = hashlib.sha256(pc._b64d(outputs["offer_wire"].split(".")[2])).digest()
        h_accept = hashlib.sha256(pc._b64d(outputs["accept_wire"].split(".")[2])).digest()
        r_a = bytes.fromhex(inp["r_a"])
        self.assertEqual(outputs["commit"], hashlib.sha256(b"mc-pair-commit-v1" + r_a).hexdigest())
        sas = int.from_bytes(_hkdf(h_offer + h_accept + r_a, b"mc-pair-sas-salt-v1", b"mc-pair-sas-v1", 8), "big") % 10 ** 6
        self.assertEqual(outputs["sas"], f"{sas:06d}")
        self.assertEqual(outputs["endpoint_id"],
                         "link-" + _hkdf(h_offer + h_accept, b"mc-pair-link-salt-v1", b"mc-pair-link-v1", 16).hex())
        self.assertEqual(outputs["offer_hash"], h_offer.hex())

    def test_sas_is_zero_padded_to_six_digits(self):
        seen_leading_zero = False
        for i in range(400):
            sas = pc.sas_for(b"\x01" * 32, b"\x02" * 32, i.to_bytes(32, "big"))
            self.assertRegex(sas, r"^\d{6}$")
            seen_leading_zero = seen_leading_zero or sas.startswith("0")
        self.assertTrue(seen_leading_zero)

    def test_the_session_hash_binds_every_code(self):
        other = copy.deepcopy(self.inp)
        other["r_b"] = (b"\x44" * 32).hex()
        o = run_session(other)
        self.assertEqual(o["offer"].wire, self.offer.wire)  # the offer is independent of B's randomness
        self.assertNotEqual(o["accept"].wire, self.accept.wire)
        self.assertNotEqual(o["endpoint_a"], self.s["endpoint_a"])
        self.assertNotEqual(o["sas_a"], self.s["sas_a"])


class SizeGateTests(Base):
    """Real CA PEM + leaf fingerprint from the strict profile and a realistic catalog must fit the cap."""

    def test_real_identity_and_three_bot_catalog_fit_under_the_cap_for_every_kind(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("MC_TEST_TMP")) as tmp:
            a = identity_mod.initialize_identity(Path(tmp) / "a", installation_id=INSTALL_A, profile="default")
            b = identity_mod.initialize_identity(Path(tmp) / "b", installation_id=INSTALL_B, profile="default")
        self.assertGreater(len(a.ca_pem), 900)  # a real RSA-2048 CA, not a stub
        cat_a = make_catalog(INSTALL_A, toolsets=("bot_room", "web", "file", "terminal"))
        cat_b = make_catalog(INSTALL_B, toolsets=("bot_room", "web", "file"))
        offer = pc.build_offer(a.consent_key(), a.peer_identity(cat_a), nonce=NONCE_A, r_a=R_A, expires_at=NOW + 900, now=NOW)
        accept = pc.build_accept(b.consent_key(), b.peer_identity(cat_b), offer, r_b=R_B, expires_at=NOW + 900, now=NOW)
        reveal = pc.build_reveal(a.consent_key(), offer, accept, r_a=R_A, expires_at=NOW + 900, now=NOW)
        for kind, code in (("offer", offer), ("accept", accept), ("reveal", reveal)):
            self.assertLessEqual(len(code.payload), pc.max_payload_bytes(kind))
            self.assertLess(len(code.wire), pc.MAX_WIRE_CHARS)
        self.assertLess(len(accept.wire), pc.MAX_WIRE_CHARS // 2)  # real pair uses well under half of the budget
        self.assertLess(len(reveal.wire), 512)
        self.assertEqual(pc.parse_accept(accept.wire, offer=offer, now=NOW).identity.ca_pem, b.ca_pem)

    def test_per_kind_budget_is_computed_on_the_whole_wire_string(self):
        for kind in pc.KINDS:
            budget = pc.max_payload_bytes(kind)
            overhead = len("mcpair1.") + len(kind) + 1 + 1 + 86
            self.assertLessEqual(overhead + -(-budget * 4 // 3), pc.MAX_WIRE_CHARS)
            self.assertLessEqual(budget, pc.MAX_PAYLOAD_BYTES)
        self.assertEqual(pc.MAX_WIRE_CHARS, 12 * 1024)
        self.assertEqual(pc.MAX_PAYLOAD_BYTES, 9 * 1024)

    def test_oversize_wire_is_refused_before_any_decoding(self):
        self.assertGeneric(pc.INVALID, self.parse_offer, "x" * (pc.MAX_WIRE_CHARS + 1))
        head, kind, payload, sig = self.offer.wire.split(".")
        fat = ".".join((head, kind, "A" * 12_250, sig))  # every segment individually in bounds, whole string over the cap
        self.assertGreater(len(fat), pc.MAX_WIRE_CHARS)
        with unittest.mock.patch.object(pc, "_b64d", side_effect=AssertionError("decoded before the cap check")):
            self.assertGeneric(pc.INVALID, self.parse_offer, fat)
        self.assertGeneric(pc.INVALID, self.parse_offer, self.offer.wire + "A" * pc.MAX_WIRE_CHARS)
        base = self.offer_value()
        base["catalog"]["pad"] = "p" * 2000
        for i in range(8):
            base["catalog"][f"pad{i}"] = "q" * 2000
        raw = canonical(base)
        self.assertGreater(len(raw), pc.max_payload_bytes("offer") - 8000)
        big = copy.deepcopy(base)
        for i in range(8, 40):
            big["catalog"][f"pad{i}"] = "r" * 2000
        self.assertGreater(len(canonical(big)), pc.max_payload_bytes("offer"))
        self.assertGeneric(pc.INVALID, self.parse_offer, forge("offer", canonical(big)))

    def test_builder_refuses_a_catalog_that_would_not_fit(self):
        ident = identity_of(self.inp["a"])
        huge = dict(ident.catalog)
        for i in range(60):
            huge[f"k{i}"] = "z" * 2000
        bad = PeerIdentity(ident.installation_id, ident.profile, huge, ident.consent_public_key, ident.ca_pem,
                           ident.leaf_fingerprint)
        self.assertGeneric(pc.INVALID, pc.build_offer, KEY_A, bad, nonce=NONCE_A, r_a=R_A, expires_at=NOW + 60, now=NOW)


class NegativeTests(Base):
    def test_tampered_payload_signature_and_segments(self):
        head, kind, payload, sig = self.offer.wire.split(".")
        flipped = payload[:20] + ("A" if payload[20] != "A" else "B") + payload[21:]
        cases = {
            "payload": ".".join((head, kind, flipped, sig)),
            "signature": ".".join((head, kind, payload, sig[:-2] + ("AA" if sig[-2:] != "AA" else "BB"))),
            "prefix": ".".join(("mcpair2", kind, payload, sig)),
            "kind": ".".join((head, "accept", payload, sig)),
            "kind_unknown": ".".join((head, "bogus", payload, sig)),
            "three_segments": ".".join((head, kind, payload)),
            "five_segments": ".".join((head, kind, payload, sig, "x")),
            "empty_signature": ".".join((head, kind, payload, "")),
            "empty_payload": ".".join((head, kind, "", sig)),
            "empty": "",
            "non_string": None,
        }
        for label, wire in cases.items():
            with self.subTest(label):
                self.assertGeneric(pc.INVALID, self.parse_offer, wire)

    def test_alphabet_padding_and_canonical_base64(self):
        head, kind, payload, sig = self.offer.wire.split(".")
        self.assertEqual(pc._b64d("-_-_"), bytes.fromhex("fbffbf"))  # url-safe alphabet is the only one
        for std in ("+/+/", "-_-_=", "-_-_ ", "AAAA\n", "A", "AAAAA"):
            with self.subTest(std=std):
                self.assertGeneric(pc.INVALID, pc._b64d, std)
        cases = {
            "padded_payload": ".".join((head, kind, payload + "=" * (-len(payload) % 4 or 1), sig)),
            "standard_alphabet_char_in_signature": ".".join((head, kind, payload, sig[:5] + "+" + sig[6:])),
            "padded_signature": ".".join((head, kind, payload, sig + "==")),
            "space": ".".join((head, kind, payload[:10] + " " + payload[11:], sig)),
            "newline": ".".join((head, kind, payload + "\n", sig)),
            "unicode": ".".join((head, kind, payload[:10] + "é" + payload[11:], sig)),
            "length_mod_4_is_1": ".".join((head, kind, payload + "A" if len(payload) % 4 == 0 else payload[:-1] if len(payload) % 4 == 2 else payload + "A" * 2, sig)),
        }
        for label, wire in cases.items():
            with self.subTest(label):
                self.assertGeneric(pc.INVALID, self.parse_offer, wire)
        # non-canonical trailing bits in the signature segment (86 chars: last char carries 4 spare bits)
        alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        alt = alphabet[alphabet.index(sig[-1]) ^ 1]  # flips one of the 4 spare bits: same 64 bytes, other text
        raw = pc._b64d(sig)
        noncanon = sig[:-1] + alt
        self.assertEqual(base64.urlsafe_b64decode(noncanon + "=="), raw)  # a lenient decoder WOULD accept it
        self.assertGeneric(pc.INVALID, self.parse_offer, ".".join((head, kind, payload, noncanon)))
        self.assertEqual(len(raw), 64)

    def test_signature_must_be_exactly_64_bytes(self):
        payload = self.offer.payload
        for size in (0, 1, 63, 65, 96):
            with self.subTest(size):
                self.assertGeneric(pc.INVALID, self.parse_offer, forge("offer", payload, sig=b"\x00" * size))
        self.assertEqual(self.parse_offer(forge("offer", payload)).digest, self.offer.digest)  # control

    def test_strict_json_schema_negatives(self):
        v = self.offer_value()

        def with_(**changes):
            out = copy.deepcopy(v)
            out.update(changes)
            return canonical(out)

        def without(key):
            out = copy.deepcopy(v)
            del out[key]
            return canonical(out)

        deep = copy.deepcopy(v)
        deep["catalog"]["execution_policy"]["enabled_toolsets"] = [["bot_room"]]  # depth 5
        raw = canonical(v)
        cases = {
            "bool_as_int_version": with_(v=True),
            "bool_as_int_expiry": with_(expires_at=True),
            "version_2": with_(v=2),
            "wrong_kind_field": with_(kind="accept"),
            "extra_key": with_(extra=1),
            "unknown_nested_tls_key": with_(tls={**v["tls"], "x": "y"}),
            "missing_key": without("commit"),
            "negative_expiry": with_(expires_at=-1),
            "huge_expiry": with_(expires_at=2 ** 41),
            "nonce_31_bytes": with_(nonce=pc.b64e(b"\x01" * 31)),
            "nonce_33_bytes": with_(nonce=pc.b64e(b"\x01" * 33)),
            "nonce_padded": with_(nonce=pc.b64e(b"\x01" * 32) + "="),
            "nonce_not_string": with_(nonce=list(b"\x01" * 32)),
            "commit_uppercase": with_(commit=v["commit"].upper()),
            "commit_short": with_(commit=v["commit"][:-2]),
            "pubkey_31_bytes": with_(consent_public_key=pc.b64e(b"\x01" * 31)),
            "fingerprint_uppercase": with_(tls={**v["tls"], "leaf_fingerprint": v["tls"]["leaf_fingerprint"].upper()}),
            "ca_pem_not_pem": with_(tls={**v["tls"], "ca_pem": "not a certificate"}),
            "ca_pem_trailing": with_(tls={**v["tls"], "ca_pem": v["tls"]["ca_pem"] + "x"}),
            "ca_pem_non_ascii": with_(tls={**v["tls"], "ca_pem": v["tls"]["ca_pem"].replace("A", "\u00c0", 1)}),
            "installation_with_slash": with_(installation_id="install:a/b"),
            "profile_empty": with_(profile=""),
            "profile_too_long": with_(profile="p" * 65),
            "catalog_installation_mismatch": with_(installation_id=INSTALL_B),
            "catalog_empty": with_(catalog={}),
            "catalog_not_object": with_(catalog=[1]),
            "catalog_null_value": with_(catalog={**v["catalog"], "x": None}),
            "catalog_float": raw.replace(b'"persistent_process":true', b'"persistent_process":1.5'),

            "catalog_long_string": with_(catalog={**v["catalog"], "x": "s" * 2049}),
            "catalog_huge_int": with_(catalog={**v["catalog"], "x": 2 ** 60}),
            "catalog_big_list": with_(catalog={**v["catalog"], "x": list(range(65))}),
            "depth_5": canonical(deep),
            "float": raw.replace(b'"v":1', b'"v":1.0'),
            "float_exponent": raw.replace(b'"v":1', b'"v":1e0'),
            "nan": raw.replace(b'"v":1', b'"v":NaN'),
            "infinity": raw.replace(b'"v":1', b'"v":Infinity'),
            "duplicate_key": raw.replace(b'"v":1', b'"v":1,"v":1'),
            "whitespace": raw.replace(b'"v":1}', b'"v": 1}'),
            "unsorted_keys": json.dumps(v, separators=(",", ":")).encode() if list(v) != sorted(v) else None,
            "escaped_non_canonical": raw.replace(b'"offer"', b'"\\u006fffer"', 1),
            "trailing_garbage": raw + b" ",
            "bom": b"\xef\xbb\xbf" + raw,
            "not_utf8": b"\xff\xfe" + raw,
            "array_root": b"[]",
            "scalar_root": b"1",
            "empty_object": b"{}",
        }
        for label, payload in cases.items():
            if payload is None:
                continue
            with self.subTest(label):
                self.assertGeneric(pc.INVALID, self.parse_offer, forge("offer", payload))
        self.assertEqual(self.parse_offer(forge("offer", raw)).digest, self.offer.digest)  # control: forge() is sound

    def test_depth_four_is_accepted_and_exactly_the_catalog_policy_toolsets_reach_it(self):
        self.assertEqual(pc.MAX_DEPTH, 4)
        v = self.offer_value()
        self.assertIsInstance(v["catalog"]["execution_policy"]["enabled_toolsets"], list)  # root>catalog>policy>list
        self.assertTrue(pc._depth_ok(canonical(v)))
        v["catalog"]["execution_policy"]["enabled_toolsets"] = [["x"]]
        self.assertFalse(pc._depth_ok(canonical(v)))
        self.assertTrue(pc._depth_ok(b'{"a":"[[[[[[[]]]]]]]\\"[[["}'))  # brackets inside strings are not nesting

    def test_expired_and_far_future_offers(self):
        self.assertGeneric(pc.UNAVAILABLE, pc.parse_offer, self.offer.wire, now=NOW + 601)
        self.assertEqual(pc.parse_offer(self.offer.wire, now=NOW + 600).digest, self.offer.digest)  # boundary inclusive
        self.assertGeneric(pc.INVALID, pc.parse_offer, self.offer.wire, now=NOW - 10_000)  # > now + 15 min
        v = self.offer_value()
        v["expires_at"] = NOW + 901
        self.assertGeneric(pc.INVALID, self.parse_offer, forge("offer", canonical(v)))
        self.assertGeneric(pc.INVALID, pc.build_offer, KEY_A, identity_of(self.inp["a"]), nonce=NONCE_A, r_a=R_A,
                           expires_at=NOW + 901, now=NOW)
        self.assertGeneric(pc.UNAVAILABLE, pc.build_offer, KEY_A, identity_of(self.inp["a"]), nonce=NONCE_A, r_a=R_A,
                           expires_at=NOW - 1, now=NOW)

    def test_journal_reload_skips_only_time_never_signature_or_schema(self):
        self.assertEqual(pc.parse_offer(self.offer.wire, now=None).digest, self.offer.digest)
        self.assertGeneric(pc.INVALID, pc.parse_offer, self.offer.wire[:-3] + "AAA", now=None)

    def test_builders_reject_bad_keys_and_nonces(self):
        ident = identity_of(self.inp["a"])
        self.assertGeneric(pc.INVALID, pc.build_offer, KEY_X, ident, nonce=NONCE_A, r_a=R_A, expires_at=NOW + 60, now=NOW)
        for bad in (b"\x00" * 31, b"\x00" * 33, "x" * 32, bytearray(32), None):
            with self.subTest(repr(type(bad))):
                self.assertGeneric(pc.INVALID, pc.build_offer, KEY_A, ident, nonce=bad, r_a=R_A, expires_at=NOW + 60, now=NOW)
                self.assertGeneric(pc.INVALID, pc.build_offer, KEY_A, ident, nonce=NONCE_A, r_a=bad, expires_at=NOW + 60, now=NOW)

    def test_accept_is_bound_to_its_offer_and_cannot_outlive_or_self_pair(self):
        other = copy.deepcopy(self.inp)
        other["nonce_a"] = (b"\x55" * 32).hex()
        other_offer = run_session(other)["offer"]
        self.assertNotEqual(other_offer.digest, self.offer.digest)
        self.assertGeneric(pc.UNAVAILABLE, pc.parse_accept, self.accept.wire, offer=other_offer, now=NOW)
        # accept that outlives the offer
        v = self.accept_value()
        v["expires_at"] = self.offer.expires_at + 1
        self.assertGeneric(pc.INVALID, pc.parse_accept, forge("accept", canonical(v), KEY_B), offer=self.offer, now=NOW)
        self.assertGeneric(pc.INVALID, pc.build_accept, KEY_B, identity_of(self.inp["b"]), self.offer, r_b=R_B,
                           expires_at=self.offer.expires_at + 1, now=NOW)
        # same installation on both sides
        same = copy.deepcopy(self.inp["a"])
        same["seed"] = (b"\x02" * 32).hex()
        self.assertGeneric(pc.INVALID, pc.build_accept, KEY_B, identity_of(same), self.offer, r_b=R_B,
                           expires_at=NOW + 60, now=NOW)
        # expired accept / expired offer
        self.assertGeneric(pc.UNAVAILABLE, pc.parse_accept, self.accept.wire, offer=self.offer, now=NOW + 501)
        self.assertGeneric(pc.INVALID, pc.parse_accept, "mcpair1.offer.x.y", offer=self.offer, now=NOW)
        self.assertGeneric(pc.INVALID, pc.parse_accept, self.offer.wire, offer=self.offer, now=NOW)  # wrong kind
        self.assertGeneric(pc.INVALID, pc.parse_accept, self.accept.wire, offer="not an offer", now=NOW)

    def test_replayed_reveal_from_another_session_is_unavailable(self):
        other = copy.deepcopy(self.inp)
        other["nonce_a"], other["r_b"] = (b"\x66" * 32).hex(), (b"\x77" * 32).hex()
        o = run_session(other)
        offer = pc.parse_offer(self.offer.wire, now=NOW)
        self.assertGeneric(pc.UNAVAILABLE, pc.parse_reveal, o["reveal"].wire, offer=offer, accept=self.accept, now=NOW)
        # same offer, different accept (B retried with a new r_b): the old reveal does not apply either
        retry = copy.deepcopy(self.inp)
        retry["r_b"] = (b"\x88" * 32).hex()
        r = run_session(retry)
        self.assertEqual(r["offer"].digest, self.offer.digest)
        self.assertGeneric(pc.UNAVAILABLE, pc.parse_reveal, self.s["reveal"].wire, offer=offer, accept=r["accept"], now=NOW)
        self.assertGeneric(pc.UNAVAILABLE, pc.parse_reveal, self.s["reveal"].wire, offer=o["offer"], accept=o["accept"], now=NOW)
        self.assertEqual(pc.parse_reveal(self.s["reveal"].wire, offer=offer, accept=self.accept, now=NOW).digest,
                         self.s["reveal"].digest)  # control
        self.assertGeneric(pc.UNAVAILABLE, pc.parse_reveal, self.s["reveal"].wire, offer=offer, accept=self.accept, now=NOW + 401)

    def test_reveal_must_open_the_commitment_and_be_signed_by_a(self):
        v = json.loads(self.s["reveal"].payload)
        v["nonce"] = pc.b64e(b"\x99" * 32)  # a different r_A, validly signed by A's key
        self.assertGeneric(pc.INVALID, pc.parse_reveal, forge("reveal", canonical(v), KEY_A), offer=self.offer,
                           accept=self.accept, now=NOW)
        self.assertGeneric(pc.INVALID, pc.parse_reveal, forge("reveal", self.s["reveal"].payload, KEY_X),
                           offer=self.offer, accept=self.accept, now=NOW)
        self.assertGeneric(pc.INVALID, pc.build_reveal, KEY_A, self.offer, self.accept, r_a=b"\x99" * 32,
                           expires_at=NOW + 60, now=NOW)
        self.assertGeneric(pc.INVALID, pc.build_reveal, KEY_X, self.offer, self.accept, r_a=R_A, expires_at=NOW + 60, now=NOW)
        late = json.loads(self.s["reveal"].payload)
        late["expires_at"] = self.accept.expires_at + 1
        self.assertGeneric(pc.INVALID, pc.parse_reveal, forge("reveal", canonical(late), KEY_A), offer=self.offer,
                           accept=self.accept, now=NOW)

    def test_substituted_accept_with_matching_public_fields_cannot_be_ground_to_the_honest_sas(self):
        """Finding 02: the attacker sees B's accept, copies every public field, and picks a new r_B hoping to
        collide the 6-digit SAS offline.  The commitment fixes r_A before the accept exists, so the attacker can
        neither learn nor choose it; the reveal binds H(accept)."""
        key_a = KEY_A
        honest_sas = self.s["sas_a"]
        offer = pc.parse_offer(self.offer.wire, now=NOW)
        forged_accepts = []
        for i in range(60):  # offline grinding of the substitute's nonce
            v = self.accept_value()
            v["nonce"] = pc.b64e(hashlib.sha256(b"grind" + i.to_bytes(4, "big")).digest())
            v["consent_public_key"] = pc.b64e(_pub(KEY_X))  # only the signing key must be the attacker's
            wire = forge("accept", canonical(v), KEY_X)
            forged_accepts.append(pc.parse_accept(wire, offer=offer, now=NOW))
        for forged in forged_accepts:
            for name in ("installation_id", "profile", "catalog", "ca_pem", "leaf_fingerprint"):
                self.assertEqual(getattr(forged.identity, name), getattr(self.accept.identity, name))  # matching public fields
            self.assertNotEqual(forged.digest, self.accept.digest)
        # 1. A's real reveal is bound to H(accept_forged): B, holding the honest accept, refuses it
        for forged in forged_accepts[:5]:
            a_reveal = pc.build_reveal(key_a, offer, forged, r_a=R_A, expires_at=NOW + 400, now=NOW)
            self.assertGeneric(pc.UNAVAILABLE, pc.parse_reveal, a_reveal.wire, offer=offer, accept=self.accept, now=NOW)
            self.assertNotEqual(pc.sas_for(offer.digest, forged.digest, R_A), honest_sas)  # SASes differ -> abort
        # 2. the attacker cannot produce a reveal that B accepts for the honest accept: wrong r_A fails the
        #    commitment, the right r_A is unknown to it (only C is public) and the key is A's
        guess = hashlib.sha256(b"guess-r_a").digest()
        self.assertGeneric(pc.INVALID, pc.build_reveal, key_a, offer, self.s["a_accept"], r_a=guess, expires_at=NOW + 60, now=NOW)
        attacker_reveal = {"v": 1, "kind": "reveal", "expires_at": NOW + 60, "nonce": pc.b64e(guess),
                           "offer_hash": offer.digest_hex, "accept_hash": self.accept.digest_hex}
        self.assertGeneric(pc.INVALID, pc.parse_reveal, forge("reveal", canonical(attacker_reveal), KEY_X),
                           offer=offer, accept=self.accept, now=NOW)
        self.assertGeneric(pc.INVALID, pc.parse_reveal, forge("reveal", canonical(attacker_reveal), KEY_A),
                           offer=offer, accept=self.accept, now=NOW)  # even a validly signed wrong r_A
        self.assertNotIn(R_A.hex(), self.offer.wire + self.accept.wire)

    def test_sas_and_endpoint_refuse_malformed_inputs(self):
        for args in ((b"\x00" * 31, b"\x00" * 32, R_A), (b"\x00" * 32, b"x" * 31, R_A), (b"\x00" * 32, b"\x00" * 32, b"\x00" * 31),
                     ("00" * 32, b"\x00" * 32, R_A)):
            with self.subTest(args=[type(a).__name__ for a in args]):
                self.assertGeneric(pc.INVALID, pc.sas_for, *args)
        self.assertGeneric(pc.INVALID, pc.endpoint_id_for, b"\x00" * 31, b"\x00" * 32)
        self.assertGeneric(pc.INVALID, pc.commitment_for, b"\x00" * 31)
        other = copy.deepcopy(self.inp)
        other["nonce_a"] = (b"\x55" * 32).hex()
        other_offer = run_session(other)["offer"]
        self.assertGeneric(pc.UNAVAILABLE, pc.sas_for_session, other_offer, self.accept, R_A)
        self.assertGeneric(pc.UNAVAILABLE, pc.endpoint_id_for_session, other_offer, self.accept)
        self.assertEqual(pc.endpoint_id_for_session(self.offer, self.accept), self.s["endpoint_a"])

    def test_errors_never_echo_input(self):
        secret = "SECRET-PEM-MARKER"
        v = self.offer_value()
        v["tls"]["ca_pem"] = secret
        try:
            self.parse_offer(forge("offer", canonical(v)))
        except PairingCodecError as exc:
            self.assertEqual(str(exc), pc.INVALID)
            self.assertNotIn(secret, repr(exc))
            self.assertNotIn(self.s["sas_a"], repr(exc))
        else:
            self.fail("expected failure")


if __name__ == "__main__":
    unittest.main()
