"""P3b.0-F R-02: producer -> host parity.

The envelope is produced by the REAL ``TargetMaterialMint`` (never hand-built) and
admitted by the host registry.  The target signs
``{host_id, room_id, lobby_id, member_id, invite_id, binding}``; the host verifies the
signature over the envelope exactly as received and only then maps ``host_id`` and
``room_id`` onto its trusted local scope.
"""
from __future__ import annotations

import asyncio
import copy
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from invite_rooms.joining_material import TargetMaterialMint
from invite_rooms.material_receipts import MaterialReceiptStore
from invite_rooms.peer_registry import MaterialScope, PeerRegistry, PeerRegistryError

try:
    import server.tests.test_room_invite_joining as J
    from server.tests.test_room_invite_routes import CA_PEM
    from server.tests.test_room_invite_material_receipts import raw_rows
except ModuleNotFoundError:
    import test_room_invite_joining as J
    from test_room_invite_routes import CA_PEM
    from test_room_invite_material_receipts import raw_rows

PEER = "peer-host-b"


class RealMintParityTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="parity-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        helper = J.TargetMaterialMintTests("test_wrong_scope_payload_rejected")
        self.fx = helper._approved_fixture(self.tmp / "joining-side")

        async def rpc(method, params):
            scope = mint.approved_scope(self.fx["payload"])
            return {"target_profile": params["profile"], "endpoint": J.CATALOG["endpoint"],
                    "catalog": J.CATALOG, "grant": helper._fake_grant(scope)}

        mint = TargetMaterialMint(self.fx["joining_store"], consent_key=self.fx["consent_key"],
                                  channel=self.fx["channel"], rpc=rpc, host_peer_id="host-a")
        asyncio.run(mint.mint(self.fx["payload"]))
        self.envelope = copy.deepcopy(self.fx["channel"].material)  # exactly what the real mint emitted
        self.assertIsNotNone(self.envelope)
        self.store = MaterialReceiptStore(self.tmp / "host-side" / "receipts")
        self.reg = PeerRegistry(receipt_store=self.store, route_provider=lambda _s: 19001)
        self.reg.register(peer_id=PEER, installation_id=J.SELF_INSTALL, profile="default", fingerprint=J.FINGERPRINT,
                          endpoint_id="endpoint-b", catalog=copy.deepcopy(J.CATALOG),
                          consent_public_key=self.fx["consent_key"].public_bytes, tls_ca_pem=CA_PEM, pairing_generation=1)
        self.assertEqual(self.reg.binding_for(PEER), J.SELF_BINDING)
        lobby = self.fx["lobby"]
        self.scope = MaterialScope(owner_installation_id=J.HOST_INSTALL, owner_profile="default",
                                   lobby_id=lobby["lobby_id"], planned_room_id=lobby["planned_room_id"],
                                   member_id="peer-b", invite_id=self.fx["invite"].invite_id, peer_id=PEER,
                                   pairing_generation=1, **self.reg.binding_for(PEER))

    def resign(self, mutate):
        """Re-sign a mutated copy with the REAL consent key so only the scope mapping can reject it."""
        env = copy.deepcopy(self.envelope)
        mutate(env["scope"])
        signed = {"scope": env["scope"], "grant": env["grant"], "catalog": env["catalog"],
                  "purpose": "joining-owner-material-v1"}
        env["signature"] = self.fx["consent_key"].sign(signed)
        return env

    def test_real_target_envelope_shape_is_what_the_mint_signs(self):
        self.assertEqual(set(self.envelope["scope"]), {"host_id", "room_id", "lobby_id", "member_id", "invite_id", "binding"})

    def test_real_envelope_is_admitted_and_resolves_to_the_exact_material(self):
        self.reg.admit_signed_material(self.scope, self.envelope)
        self.assertEqual(len(raw_rows(self.store)), 1)
        material = asyncio.run(self.reg.resolve_scoped_material(self.scope))
        self.assertEqual(material.grant, self.envelope["grant"])
        self.assertEqual(material.binding, J.SELF_BINDING)
        # re-admitting the identical real envelope stays idempotent
        self.reg.admit_signed_material(self.scope, self.envelope)
        self.assertEqual(len(raw_rows(self.store)), 1)

    def assert_rejected(self, envelope, code="material_scope_mismatch"):
        with self.assertRaises(PeerRegistryError) as cm:
            self.reg.admit_signed_material(self.scope, envelope)
        self.assertEqual(str(cm.exception), code)
        self.assertEqual(raw_rows(self.store), [])

    def test_each_substituted_top_level_scope_field_is_rejected(self):
        for field in ("host_id", "room_id", "lobby_id", "member_id", "invite_id"):
            with self.subTest(field=field):
                value = "install:" + "c" * 32 if field == "host_id" else "other-" + field
                self.assert_rejected(self.resign(lambda s: s.__setitem__(field, value)))

    def test_each_substituted_binding_field_is_rejected(self):
        values = {"endpoint_id": "endpoint-evil", "fingerprint": "c" * 64, "target_install_id": "install:" + "c" * 32,
                  "target_profile": "other", "catalog_digest": "c" * 64, "policy_digest": "c" * 64}
        for field, value in values.items():
            with self.subTest(binding_field=field):
                self.assert_rejected(self.resign(lambda s: s["binding"].__setitem__(field, value)))

    def test_envelope_cannot_supply_host_local_fields(self):
        for extra in ("owner_installation_id", "owner_profile", "peer_id", "pairing_generation", "planned_room_id"):
            with self.subTest(extra=extra):
                self.assert_rejected(self.resign(lambda s: s.__setitem__(extra, "x")))

    def test_missing_scope_field_and_wrong_binding_shape_are_rejected(self):
        for field in ("host_id", "room_id", "lobby_id", "member_id", "invite_id", "binding"):
            with self.subTest(missing=field):
                self.assert_rejected(self.resign(lambda s: s.pop(field)))
        with self.subTest(binding="four-field"):
            self.assert_rejected(self.resign(lambda s: s["binding"].pop("policy_digest")))
        with self.subTest(binding="not-a-dict"):
            self.assert_rejected(self.resign(lambda s: s.__setitem__("binding", "endpoint-b")))

    def test_host_local_scope_dump_on_the_wire_is_no_longer_accepted(self):
        """The old (buggy) fixtures put MaterialScope.as_dict() on the wire. That shape is not
        what the target signs and must never be admitted."""
        legacy = lambda s: (s.clear(), s.update(self.scope.as_dict()))
        self.assert_rejected(self.resign(legacy))

    def test_tampered_signed_field_after_real_signature_is_invalid_consent(self):
        env = copy.deepcopy(self.envelope)
        env["scope"]["room_id"] = "room-other"  # NOT re-signed
        self.assert_rejected(env, "invalid_consent")


if __name__ == "__main__":
    unittest.main()
