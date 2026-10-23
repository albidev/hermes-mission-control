"""Private approved peer registry — security boundary tests.

Verifies: caller selects pre-paired peer IDs (not URL/fingerprint/catalog/grant),
exact binding validation, unknown peers fail closed, defensive snapshots prevent
input/output mutation, material resolution only at activation, URL-alone trust
rejected, grant repr private, consent public-key seam present but not authenticated.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


from invite_rooms.peer_registry import (
    PeerRegistry,
    PeerRecord,
    PeerRegistryError,
)
from room_lobby_service import PeerMaterial
from gateway.hosted_room_peer import catalog_mapping

PEER_ID = "owner-b"
INSTALL_ID = "install:" + "b" * 32
FINGERPRINT = "b" * 64
ADAPTER_URL = "http://127.0.0.1:19001/links/unit-link"
GRANT = "unit-fixture-grant-secret"
_POLICY_UNSIGNED = {"version": 1, "target_profile": "default", "enabled_toolsets": ["bot_room"], "approval_mode": "manual", "max_iterations": 10}
_POLICY = {**_POLICY_UNSIGNED, "policy_digest": hashlib.sha256(json.dumps(_POLICY_UNSIGNED, sort_keys=True, separators=(",", ":")).encode("ascii")).hexdigest()}
CATALOG = catalog_mapping(installation_id=INSTALL_ID, protocol_versions=(2,), link_modes=("direct",), persistent_process=True, text=True, attachments=False, endpoint={"available": True, "url": ADAPTER_URL, "transport_security": "loopback"}, target_profile="default", execution_policy=_POLICY)
CATALOG_DIGEST = CATALOG["catalog_digest"]
POLICY_DIGEST = CATALOG["execution_policy"]["policy_digest"]
CONSENT_PUBKEY = b"\x00" * 32  # seam: not authenticated yet


def make_registry():
    reg = PeerRegistry()
    reg.register(
        peer_id=PEER_ID,
        installation_id=INSTALL_ID,
        profile="default",
        fingerprint=FINGERPRINT,
        endpoint_id="endpoint-b",
        catalog=copy.deepcopy(CATALOG),
        adapter_url=ADAPTER_URL,
        grant=GRANT,
        consent_public_key=CONSENT_PUBKEY,
    )
    return reg


class RegistrationTests(unittest.TestCase):
    def test_register_and_lookup_by_peer_id(self):
        reg = make_registry()
        rec = reg.lookup(PEER_ID)
        self.assertIsInstance(rec, PeerRecord)
        self.assertEqual(rec.peer_id, PEER_ID)
        self.assertEqual(rec.installation_id, INSTALL_ID)
        self.assertEqual(rec.profile, "default")
        self.assertEqual(rec.fingerprint, FINGERPRINT)
        self.assertEqual(rec.catalog_digest, CATALOG_DIGEST)

    def test_lookup_unknown_peer_fails_closed(self):
        reg = make_registry()
        with self.assertRaises(PeerRegistryError):
            reg.lookup("unknown-peer")

    def test_lookup_returns_no_secret_material(self):
        reg = make_registry()
        rec = reg.lookup(PEER_ID)
        # PeerRecord must not expose grant, adapter_url, or catalog internals.
        for attr in ("grant", "adapter_url", "catalog"):
            self.assertFalse(hasattr(rec, attr), f"PeerRecord must not have {attr}")

    def test_defensive_copy_on_register(self):
        reg = PeerRegistry()
        catalog = copy.deepcopy(CATALOG)
        reg.register(
            peer_id=PEER_ID, installation_id=INSTALL_ID, profile="default",
            fingerprint=FINGERPRINT, endpoint_id="endpoint-b",
            catalog=catalog, adapter_url=ADAPTER_URL, grant=GRANT,
        )
        # Mutate the original input — registry must be unaffected.
        catalog["catalog_digest"] = "e" * 64
        rec = reg.lookup(PEER_ID)
        self.assertEqual(rec.catalog_digest, CATALOG_DIGEST)

    def test_duplicate_registration_rejected(self):
        reg = make_registry()
        with self.assertRaises(PeerRegistryError):
            reg.register(
                peer_id=PEER_ID, installation_id=INSTALL_ID, profile="default",
                fingerprint=FINGERPRINT, endpoint_id="endpoint-b",
                catalog=copy.deepcopy(CATALOG), adapter_url=ADAPTER_URL, grant=GRANT,
            )

    def test_invalid_peer_id_rejected(self):
        reg = PeerRegistry()
        with self.assertRaises(PeerRegistryError):
            reg.register(
                peer_id="", installation_id=INSTALL_ID, profile="default",
                fingerprint=FINGERPRINT, endpoint_id="endpoint-b",
                catalog=copy.deepcopy(CATALOG), adapter_url=ADAPTER_URL, grant=GRANT,
            )

    def test_invalid_installation_id_rejected(self):
        reg = PeerRegistry()
        with self.assertRaises(PeerRegistryError):
            reg.register(
                peer_id=PEER_ID, installation_id="not-an-install-id", profile="default",
                fingerprint=FINGERPRINT, endpoint_id="endpoint-b",
                catalog=copy.deepcopy(CATALOG), adapter_url=ADAPTER_URL, grant=GRANT,
            )


class MaterialResolutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_resolve_material_returns_peer_material(self):
        reg = make_registry()
        binding = {
            "endpoint_id": "endpoint-b",
            "fingerprint": FINGERPRINT,
            "target_install_id": INSTALL_ID,
            "target_profile": "default",
            "catalog_digest": CATALOG_DIGEST,
            "policy_digest": POLICY_DIGEST,
        }
        material = await reg.resolve_material(PEER_ID, expected_binding=binding)
        self.assertIsInstance(material, PeerMaterial)
        self.assertEqual(material.binding, binding)
        self.assertEqual(material.grant, GRANT)
        self.assertEqual(material.adapter_url, ADAPTER_URL)
        self.assertEqual(material.catalog["catalog_digest"], CATALOG_DIGEST)

    async def test_resolve_unknown_peer_fails_closed(self):
        reg = make_registry()
        with self.assertRaises(PeerRegistryError):
            await reg.resolve_material("unknown", expected_binding={
                "endpoint_id": "endpoint-b", "fingerprint": FINGERPRINT,
                "target_install_id": INSTALL_ID, "target_profile": "default", "catalog_digest": CATALOG_DIGEST, "policy_digest": POLICY_DIGEST})

    async def test_resolve_mismatched_fingerprint_fails_closed(self):
        reg = make_registry()
        with self.assertRaises(PeerRegistryError):
            await reg.resolve_material(PEER_ID, expected_binding={
                "endpoint_id": "endpoint-b", "fingerprint": "c" * 64,
                "target_install_id": INSTALL_ID, "target_profile": "default", "catalog_digest": CATALOG_DIGEST, "policy_digest": POLICY_DIGEST})

    async def test_resolve_mismatched_install_fails_closed(self):
        reg = make_registry()
        with self.assertRaises(PeerRegistryError):
            await reg.resolve_material(PEER_ID, expected_binding={
                "endpoint_id": "endpoint-b", "fingerprint": FINGERPRINT,
                "target_install_id": "install:" + "c" * 32, "target_profile": "default", "catalog_digest": CATALOG_DIGEST, "policy_digest": POLICY_DIGEST})

    async def test_resolve_mismatched_profile_fails_closed(self):
        reg = make_registry()
        with self.assertRaises(PeerRegistryError):
            await reg.resolve_material(PEER_ID, expected_binding={
                "endpoint_id": "endpoint-b", "fingerprint": FINGERPRINT,
                "target_install_id": INSTALL_ID, "target_profile": "other"})

    async def test_resolve_mismatched_endpoint_fails_closed(self):
        reg = make_registry()
        with self.assertRaises(PeerRegistryError):
            await reg.resolve_material(PEER_ID, expected_binding={
                "endpoint_id": "wrong-endpoint", "fingerprint": FINGERPRINT,
                "target_install_id": INSTALL_ID, "target_profile": "default", "catalog_digest": CATALOG_DIGEST, "policy_digest": POLICY_DIGEST})

    async def test_resolve_catalog_mismatch_fails_closed(self):
        """Catalog digest drift between registration and stored catalog is caught."""
        reg = PeerRegistry()
        bad_catalog = copy.deepcopy(CATALOG)
        bad_catalog["catalog_digest"] = "x" * 64
        # This should fail at registration because catalog_digest doesn't match
        # the catalog's own digest field.
        with self.assertRaises(PeerRegistryError):
            reg.register(
                peer_id=PEER_ID, installation_id=INSTALL_ID, profile="default",
                fingerprint=FINGERPRINT, endpoint_id="endpoint-b",
                catalog=bad_catalog, adapter_url=ADAPTER_URL, grant=GRANT,
            )

    async def test_resolve_material_defensive_copy(self):
        reg = make_registry()
        binding = {
            "endpoint_id": "endpoint-b", "fingerprint": FINGERPRINT,
            "target_install_id": INSTALL_ID, "target_profile": "default", "catalog_digest": CATALOG_DIGEST, "policy_digest": POLICY_DIGEST,
        }
        material = await reg.resolve_material(PEER_ID, expected_binding=binding)
        # Mutate returned material's catalog — registry must be unaffected.
        material.catalog["catalog_digest"] = "z" * 64
        material2 = await reg.resolve_material(PEER_ID, expected_binding=binding)
        self.assertEqual(material2.catalog["catalog_digest"], CATALOG_DIGEST)

    async def test_url_alone_does_not_imply_trust(self):
        """A loopback adapter URL is valid but never sufficient for trust."""
        reg = make_registry()
        # Cannot resolve by URL — must use peer_id.
        with self.assertRaises(PeerRegistryError):
            await reg.resolve_material(ADAPTER_URL, expected_binding={
                "endpoint_id": "endpoint-b", "fingerprint": FINGERPRINT,
                "target_install_id": INSTALL_ID, "target_profile": "default", "catalog_digest": CATALOG_DIGEST, "policy_digest": POLICY_DIGEST})

    async def test_grant_repr_is_private(self):
        """Grant must not appear in repr of any registry output."""
        reg = make_registry()
        rec = reg.lookup(PEER_ID)
        self.assertNotIn(GRANT, repr(rec))
        self.assertNotIn("grant", repr(rec))
        binding = {
            "endpoint_id": "endpoint-b", "fingerprint": FINGERPRINT,
            "target_install_id": INSTALL_ID, "target_profile": "default", "catalog_digest": CATALOG_DIGEST, "policy_digest": POLICY_DIGEST,
        }
        material = await reg.resolve_material(PEER_ID, expected_binding=binding)
        # PeerMaterial.grant has repr=False in the dataclass.
        self.assertNotIn(GRANT, repr(material))


class ConsentSeamTests(unittest.TestCase):
    def test_consent_public_key_stored(self):
        reg = make_registry()
        rec = reg.lookup(PEER_ID)
        self.assertEqual(rec.consent_public_key, CONSENT_PUBKEY)

    def test_consent_public_key_optional(self):
        reg = PeerRegistry()
        reg.register(
            peer_id=PEER_ID, installation_id=INSTALL_ID, profile="default",
            fingerprint=FINGERPRINT, endpoint_id="endpoint-b",
            catalog=copy.deepcopy(CATALOG), adapter_url=ADAPTER_URL, grant=GRANT,
        )
        rec = reg.lookup(PEER_ID)
        self.assertIsNone(rec.consent_public_key)

    def test_owner_flag_does_not_authenticate(self):
        """A dict with an owner flag must not be treated as authenticated consent."""
        reg = make_registry()
        rec = reg.lookup(PEER_ID)
        # The record has consent_public_key as a seam but NO consent_authenticated field.
        # There is no method to mark consent as authenticated.
        self.assertFalse(hasattr(rec, "consent_authenticated"))
        self.assertFalse(hasattr(rec, "is_authenticated"))


class BindingViewTests(unittest.TestCase):
    def test_binding_for_roster(self):
        reg = make_registry()
        binding = reg.binding_for(PEER_ID)
        self.assertEqual(binding["endpoint_id"], "endpoint-b")
        self.assertEqual(binding["fingerprint"], FINGERPRINT)
        self.assertEqual(binding["target_install_id"], INSTALL_ID)
        self.assertEqual(binding["target_profile"], "default")

    def test_binding_for_unknown_peer_fails(self):
        reg = make_registry()
        with self.assertRaises(PeerRegistryError):
            reg.binding_for("unknown")

    def test_binding_for_returns_defensive_copy(self):
        reg = make_registry()
        b = reg.binding_for(PEER_ID)
        b["fingerprint"] = "x" * 64
        b2 = reg.binding_for(PEER_ID)
        self.assertEqual(b2["fingerprint"], FINGERPRINT)


if __name__ == "__main__":
    unittest.main()