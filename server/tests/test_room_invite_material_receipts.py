"""P3b.0-T: durable encrypted material receipts, scoped registry resolution, strict peer config.

Restores (on the receipt/registry path) the coverage of the static-grant tests that P3b.0
removed, and covers server/invite_rooms/material_receipts.py directly.  Sentinels are
distinct realistic strings so a leak is detectable by substring search.
"""
from __future__ import annotations

import base64
import copy
import json
import logging
import os
import sqlite3
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from invite_rooms.consent import canonical
from invite_rooms.material_receipts import MaterialReceiptError, MaterialReceiptStore
from invite_rooms.peer_registry import (
    MaterialScope, PeerRegistry, PeerRegistryError, ScopedPeerMaterial, adapter_id_for,
)
from invite_rooms import runtime_config
from invite_rooms.runtime_config import RuntimeConfigError, build_peer_registry, load_service
from room_lobby_service import PeerMaterial

try:
    from server.tests.test_room_invite_registry import CATALOG, FINGERPRINT, INSTALL_ID, PEER_ID
    from server.tests.test_room_invite_routes import (
        CA_PEM, CONSENT_PUBLIC, _CONSENT_SIGNER, _peer_registry_payload, target_signed_envelope, target_wire_scope)
except ModuleNotFoundError:
    from test_room_invite_registry import CATALOG, FINGERPRINT, INSTALL_ID, PEER_ID
    from test_room_invite_routes import CA_PEM, CONSENT_PUBLIC, _CONSENT_SIGNER, _peer_registry_payload, target_signed_envelope, target_wire_scope

OWNER_INSTALL = "install:" + "a" * 32
GRANT_A = "gA7q-Zk93LwT7vXe2NpA81cBdRm4"
GRANT_B = "gB2h-Qx58MvY1uRt6KsD94eFjWn7"
GENERIC_BINDING = "binding_mismatch"


def new_registry(root, *, route_provider=None, generation=1):
    store = MaterialReceiptStore(Path(root) / "receipts")
    reg = PeerRegistry(receipt_store=store, route_provider=route_provider)
    reg.register(peer_id=PEER_ID, installation_id=INSTALL_ID, profile="default", fingerprint=FINGERPRINT,
                 endpoint_id="endpoint-b", catalog=copy.deepcopy(CATALOG), consent_public_key=CONSENT_PUBLIC,
                 tls_ca_pem=CA_PEM, pairing_generation=generation)
    return reg, store


def make_scope(reg, **over):
    base = dict(owner_installation_id=OWNER_INSTALL, owner_profile="default", lobby_id="lobby-r1",
                planned_room_id="room-r1", member_id="member-r1", invite_id="invite-r1", peer_id=PEER_ID,
                pairing_generation=1, **reg.binding_for(PEER_ID))
    base.update(over)
    return MaterialScope(**base)


def material_for(scope, grant):
    return {"binding": dict(scope.binding), "catalog": copy.deepcopy(CATALOG), "grant": grant}


def envelope_for(scope, grant, *, catalog=None, signer=_CONSENT_SIGNER):
    return target_signed_envelope(scope, grant, CATALOG if catalog is None else catalog, signer=signer)


def raw_rows(store):
    with sqlite3.connect(store.path) as db:
        return db.execute("SELECT scope_key, generation, nonce, ciphertext, digest FROM receipts ORDER BY scope_key").fetchall()


def raw_rows_at(path):
    with sqlite3.connect(path) as db:
        return db.execute("SELECT scope_key, generation, nonce, ciphertext, digest FROM receipts ORDER BY scope_key").fetchall()


class TmpCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="mat-receipts-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def store(self, name="receipts"):
        return MaterialReceiptStore(self.tmp / name)


class ReceiptStorePermissionTests(TmpCase):
    def test_new_storage_is_private_0700_dir_0600_files(self):
        store = self.store()
        self.assertEqual(stat.S_IMODE(store.root.stat().st_mode), 0o700)
        for name in ("material_receipts.sqlite", "material_receipts.key", ".material-receipts.lock"):
            self.assertEqual(stat.S_IMODE((store.root / name).stat().st_mode), 0o600, name)

    def test_existing_broader_directory_is_refused_and_nothing_written(self):
        root = self.tmp / "wide"
        root.mkdir(mode=0o755)
        os.chmod(root, 0o755)
        with self.assertRaises(MaterialReceiptError) as cm:
            MaterialReceiptStore(root)
        self.assertEqual(str(cm.exception), "private_storage_required")
        self.assertEqual(list(root.iterdir()), [])

    def test_symlinked_root_directory_is_refused(self):
        real = self.tmp / "real"
        real.mkdir(mode=0o700)
        link = self.tmp / "link"
        link.symlink_to(real)
        with self.assertRaises(MaterialReceiptError) as cm:
            MaterialReceiptStore(link)
        self.assertEqual(str(cm.exception), "private_storage_required")
        self.assertEqual(list(real.iterdir()), [])

    def test_existing_broader_key_file_is_refused(self):
        store = self.store()
        os.chmod(store.key_path, 0o644)
        with self.assertRaises(MaterialReceiptError) as cm:
            MaterialReceiptStore(store.root)
        self.assertEqual(str(cm.exception), "private_storage_required")

    def test_existing_broader_database_file_is_refused_and_unchanged(self):
        store = self.store()
        before = store.path.read_bytes()
        os.chmod(store.path, 0o644)
        with self.assertRaises(MaterialReceiptError) as cm:
            MaterialReceiptStore(store.root)
        self.assertEqual(str(cm.exception), "private_storage_required")
        self.assertEqual(store.path.read_bytes(), before)

    def test_symlinked_key_file_is_refused(self):
        store = self.store()
        decoy = self.tmp / "decoy.key"
        decoy.write_bytes(store.key_path.read_bytes())
        os.chmod(decoy, 0o600)
        store.key_path.unlink()
        store.key_path.symlink_to(decoy)
        with self.assertRaises((MaterialReceiptError, OSError)):
            MaterialReceiptStore(store.root)

    def test_symlinked_database_file_is_refused(self):
        store = self.store()
        decoy = self.tmp / "decoy.sqlite"
        decoy.write_bytes(store.path.read_bytes())
        os.chmod(decoy, 0o600)
        store.path.unlink()
        store.path.symlink_to(decoy)
        with self.assertRaises((MaterialReceiptError, OSError)):
            MaterialReceiptStore(store.root)

    def test_foreign_owner_is_refused_and_nothing_written(self):
        root = self.tmp / "owned"
        root.mkdir(mode=0o700)
        with patch.object(os, "getuid", return_value=os.getuid() + 4242):
            with self.assertRaises(MaterialReceiptError) as cm:
                MaterialReceiptStore(root)
        self.assertEqual(str(cm.exception), "private_storage_required")
        self.assertEqual(list(root.iterdir()), [])


class ReceiptStoreCryptoTests(TmpCase):
    def setUp(self):
        super().setUp()
        self.reg, self.store_ = new_registry(self.tmp)
        self.scope_a = make_scope(self.reg)
        self.scope_b = make_scope(self.reg, invite_id="invite-r2", member_id="member-r2")

    def test_missing_key_file_fails_closed_without_regeneration(self):
        self.store_.admit(self.scope_a, material_for(self.scope_a, GRANT_A))
        self.store_.key_path.unlink()
        with self.assertRaises(MaterialReceiptError) as cm:
            MaterialReceiptStore(self.store_.root)
        self.assertEqual(str(cm.exception), "receipt_key_missing")
        self.assertFalse(self.store_.key_path.exists())

    def test_invalid_key_length_fails_closed(self):
        os.chmod(self.store_.key_path, 0o600)
        self.store_.key_path.write_bytes(b"\x01" * 31)
        with self.assertRaises(MaterialReceiptError) as cm:
            MaterialReceiptStore(self.store_.root)
        self.assertEqual(str(cm.exception), "receipt_key_invalid")

    def test_replaced_key_cannot_read_existing_receipts(self):
        self.store_.admit(self.scope_a, material_for(self.scope_a, GRANT_A))
        self.store_.key_path.write_bytes(bytes(range(32, 64)))
        reopened = MaterialReceiptStore(self.store_.root)
        with self.assertRaises(MaterialReceiptError) as cm:
            reopened.load(self.scope_a)
        self.assertEqual(str(cm.exception), "material_unavailable")

    def test_receipts_are_encrypted_at_rest_and_key_is_a_separate_file(self):
        self.store_.admit(self.scope_a, material_for(self.scope_a, GRANT_A))
        key = self.store_.key_path.read_bytes()
        self.assertEqual(len(key), 32)
        blob = self.store_.path.read_bytes()
        self.assertNotIn(GRANT_A.encode(), blob)
        self.assertNotIn(b"grant", blob)
        self.assertNotIn(key, blob)
        for path in (self.store_.path, self.store_.root / ".material-receipts.lock"):
            self.assertNotIn(key, path.read_bytes())

    def test_same_plaintext_uses_a_fresh_nonce_each_write(self):
        other_reg, other_store = new_registry(self.tmp / "second")
        # Same key material is irrelevant: even under one key two writes of identical plaintext differ.
        scope_a2 = make_scope(self.reg, invite_id="invite-r9")
        plain = material_for(self.scope_a, GRANT_A)
        self.store_.admit(self.scope_a, plain)
        self.store_.admit(scope_a2, material_for(scope_a2, GRANT_A))
        rows = raw_rows(self.store_)
        self.assertEqual(len(rows), 2)
        self.assertEqual({len(r[2]) for r in rows}, {12})
        self.assertNotEqual(rows[0][2], rows[1][2])
        self.assertNotEqual(rows[0][3], rows[1][3])
        # And across two stores writing the identical scope+plaintext.
        other_scope = make_scope(other_reg)
        other_store.admit(other_scope, plain)
        self.assertNotEqual(raw_rows(other_store)[0][2], [r for r in rows if r[0] == self.store_.key_for(self.scope_a)][0][2])

    def test_ciphertext_is_bound_to_scope_aad(self):
        self.store_.admit(self.scope_a, material_for(self.scope_a, GRANT_A))
        row = raw_rows(self.store_)[0]
        with sqlite3.connect(self.store_.path) as db:
            db.execute("INSERT INTO receipts VALUES(?,?,?,?,?)",
                       (self.store_.key_for(self.scope_b), 1, row[2], row[3], row[4]))
        with self.assertRaises(MaterialReceiptError) as cm:
            self.store_.load(self.scope_b)
        self.assertEqual(str(cm.exception), "material_unavailable")
        self.assertEqual(self.store_.load(self.scope_a)["grant"], GRANT_A)

    def test_ciphertext_is_bound_to_pairing_generation_aad(self):
        self.store_.admit(self.scope_a, material_for(self.scope_a, GRANT_A))
        row = raw_rows(self.store_)[0]
        gen2 = make_scope(self.reg, pairing_generation=2)
        with sqlite3.connect(self.store_.path) as db:
            db.execute("INSERT INTO receipts VALUES(?,?,?,?,?)",
                       (self.store_.key_for(gen2), 2, row[2], row[3], row[4]))
        with self.assertRaises(MaterialReceiptError) as cm:
            self.store_.load(gen2)
        self.assertEqual(str(cm.exception), "material_unavailable")

    def test_swapping_two_receipt_rows_between_scopes_fails(self):
        self.store_.admit(self.scope_a, material_for(self.scope_a, GRANT_A))
        self.store_.admit(self.scope_b, material_for(self.scope_b, GRANT_B))
        ka, kb = self.store_.key_for(self.scope_a), self.store_.key_for(self.scope_b)
        rows = {r[0]: r for r in raw_rows(self.store_)}
        with sqlite3.connect(self.store_.path) as db:
            for key, src in ((ka, rows[kb]), (kb, rows[ka])):
                db.execute("UPDATE receipts SET nonce=?, ciphertext=?, digest=? WHERE scope_key=?", (src[2], src[3], src[4], key))
        for scope in (self.scope_a, self.scope_b):
            with self.assertRaises(MaterialReceiptError) as cm:
                self.store_.load(scope)
            self.assertEqual(str(cm.exception), "material_unavailable")

    def test_flipped_ciphertext_or_tag_byte_fails(self):
        self.store_.admit(self.scope_a, material_for(self.scope_a, GRANT_A))
        row = raw_rows(self.store_)[0]
        ct = bytes(row[3])
        positions = {"ciphertext": len(ct) - 16 - 3, "tag": len(ct) - 1}
        for label, pos in positions.items():
            with self.subTest(part=label):
                tampered = bytearray(ct)
                tampered[pos] ^= 0x01
                with sqlite3.connect(self.store_.path) as db:
                    db.execute("UPDATE receipts SET ciphertext=?", (bytes(tampered),))
                with self.assertRaises(MaterialReceiptError) as cm:
                    self.store_.load(self.scope_a)
                self.assertEqual(str(cm.exception), "material_unavailable")
                with sqlite3.connect(self.store_.path) as db:
                    db.execute("UPDATE receipts SET ciphertext=?", (ct,))
        self.assertEqual(self.store_.load(self.scope_a)["grant"], GRANT_A)

    def test_first_receipt_is_immutable_and_conflict_leaves_it_intact(self):
        self.store_.admit(self.scope_a, material_for(self.scope_a, GRANT_A))
        before = raw_rows(self.store_)
        with self.assertRaises(MaterialReceiptError) as cm:
            self.store_.admit(self.scope_a, material_for(self.scope_a, GRANT_B))
        self.assertEqual(str(cm.exception), "material_scope_conflict")
        self.assertEqual(raw_rows(self.store_), before)
        self.assertEqual(self.store_.load(self.scope_a)["grant"], GRANT_A)

    def test_identical_rewrite_is_idempotent_with_no_side_effects(self):
        self.store_.admit(self.scope_a, material_for(self.scope_a, GRANT_A))
        before_rows, before_db = raw_rows(self.store_), self.store_.path.read_bytes()
        self.assertIsNone(self.store_.admit(self.scope_a, material_for(self.scope_a, GRANT_A)))
        self.assertEqual(raw_rows(self.store_), before_rows)
        self.assertEqual(self.store_.path.read_bytes(), before_db)

    def test_extra_adapter_url_field_is_rejected_and_nothing_written(self):
        material = dict(material_for(self.scope_a, GRANT_A), adapter_url="http://127.0.0.1:19002/links/unapproved")
        with self.assertRaises(MaterialReceiptError) as cm:
            self.store_.admit(self.scope_a, material)
        self.assertEqual(str(cm.exception), "material_scope_mismatch")
        self.assertEqual(raw_rows(self.store_), [])

    def test_store_round_trip_returns_exact_material_and_defensive_copy(self):
        material = material_for(self.scope_a, GRANT_A)
        self.store_.admit(self.scope_a, copy.deepcopy(material))
        first = self.store_.load(self.scope_a)
        self.assertEqual(first, material)
        first["grant"] = "mutated-by-caller"
        first["catalog"]["catalog_digest"] = "0" * 64
        self.assertEqual(self.store_.load(self.scope_a), material)


class RegistryResolutionTests(TmpCase, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        TmpCase.setUp(self)
        self.routes = []
        self.reg, self.store_ = new_registry(self.tmp, route_provider=lambda scope: self.routes.append(scope) or 19001)

    def admit(self, scope, grant):
        self.reg.admit_signed_material(scope, envelope_for(scope, grant))

    async def test_resolve_material_returns_exact_material_for_the_right_scope(self):
        a = make_scope(self.reg)
        b = make_scope(self.reg, invite_id="invite-r2", member_id="member-r2")
        self.admit(a, GRANT_A)
        self.admit(b, GRANT_B)
        got_a = await self.reg.resolve_scoped_material(a)
        got_b = await self.reg.resolve_scoped_material(b)
        self.assertIsInstance(got_a, PeerMaterial)
        self.assertEqual((got_a.grant, got_b.grant), (GRANT_A, GRANT_B))
        self.assertEqual(got_a.binding, a.binding)
        self.assertEqual(got_a.catalog, CATALOG)
        self.assertEqual(got_a.adapter_url, f"http://127.0.0.1:19001/links/{adapter_id_for(a)}")
        self.assertNotEqual(adapter_id_for(a), adapter_id_for(b))

    async def test_resolve_material_returns_defensive_copy(self):
        scope = make_scope(self.reg)
        self.admit(scope, GRANT_A)
        first = await self.reg.resolve_scoped_material(scope)
        first.catalog["catalog_digest"] = "z" * 64
        first.binding["fingerprint"] = "z" * 64
        self.assertEqual(self.reg._peers[PEER_ID].catalog, CATALOG)
        second = await self.reg.resolve_scoped_material(scope)
        self.assertEqual(second.catalog, CATALOG)
        self.assertEqual(second.binding, scope.binding)

    async def _assert_binding_rejected(self, bad_scope):
        scope = make_scope(self.reg)
        self.admit(scope, GRANT_A)
        loads = []
        original = self.store_.load
        self.store_.load = lambda s: loads.append(s) or original(s)
        self.routes.clear()
        with self.assertRaises(PeerRegistryError) as cm:
            await self.reg.resolve_scoped_material(bad_scope)
        self.assertEqual(str(cm.exception), GENERIC_BINDING)
        self.assertEqual((loads, self.routes), ([], []))

    async def test_resolve_mismatched_endpoint_fails_closed(self):
        await self._assert_binding_rejected(make_scope(self.reg, endpoint_id="wrong-endpoint"))

    async def test_resolve_mismatched_fingerprint_fails_closed(self):
        await self._assert_binding_rejected(make_scope(self.reg, fingerprint="c" * 64))

    async def test_resolve_mismatched_install_fails_closed(self):
        await self._assert_binding_rejected(make_scope(self.reg, target_install_id="install:" + "c" * 32))

    async def test_resolve_mismatched_profile_fails_closed(self):
        await self._assert_binding_rejected(make_scope(self.reg, target_profile="other"))

    async def test_resolve_catalog_mismatch_fails_closed(self):
        await self._assert_binding_rejected(make_scope(self.reg, catalog_digest="e" * 64))

    async def test_resolve_policy_mismatch_fails_closed(self):
        await self._assert_binding_rejected(make_scope(self.reg, policy_digest="e" * 64))

    async def test_resolve_wrong_pairing_generation_fails_closed(self):
        await self._assert_binding_rejected(make_scope(self.reg, pairing_generation=2))

    async def test_resolve_unknown_peer_fails_closed(self):
        scope = make_scope(self.reg)
        self.admit(scope, GRANT_A)
        ghost = make_scope(self.reg, peer_id="ghost-peer")
        with self.assertRaises(PeerRegistryError) as cm:
            await self.reg.resolve_scoped_material(ghost)
        self.assertEqual(str(cm.exception), "unknown_peer")
        self.assertEqual(self.routes, [])

    async def test_url_alone_does_not_imply_trust(self):
        scope = make_scope(self.reg)
        # A loopback route exists, but there is no receipt: nothing is granted.
        with self.assertRaises(PeerRegistryError) as cm:
            await self.reg.resolve_scoped_material(scope)
        self.assertEqual(str(cm.exception), "material_scope_unapproved")
        self.assertEqual(self.routes, [])
        # No receipt store at all: also nothing.
        bare = PeerRegistry(route_provider=lambda _s: 19001)
        bare.register(peer_id=PEER_ID, installation_id=INSTALL_ID, profile="default", fingerprint=FINGERPRINT,
                      endpoint_id="endpoint-b", catalog=copy.deepcopy(CATALOG), consent_public_key=CONSENT_PUBLIC,
                      tls_ca_pem=CA_PEM, pairing_generation=1)
        with self.assertRaises(PeerRegistryError):
            await bare.resolve_scoped_material(make_scope(bare))
        # No registry entry: even with a receipt row for that scope key nothing resolves.
        empty = PeerRegistry(receipt_store=self.store_, route_provider=lambda _s: 19001)
        with self.assertRaises(PeerRegistryError) as cm2:
            await empty.resolve_scoped_material(scope)
        self.assertEqual(str(cm2.exception), "unknown_peer")

    async def test_receipt_admission_does_not_touch_route_provider(self):
        scope = make_scope(self.reg)
        self.admit(scope, GRANT_A)
        self.assertEqual(self.routes, [])
        await self.reg.resolve_scoped_material(scope)
        self.assertEqual(self.routes, [scope])


class PrivacyTests(TmpCase):
    def test_receipt_and_registry_representations_never_leak_secrets(self):
        records = []
        handler = logging.Handler()
        handler.emit = lambda r: records.append(r.getMessage())
        root_logger = logging.getLogger()
        old_level = root_logger.level
        root_logger.addHandler(handler)
        root_logger.setLevel(1)
        self.addCleanup(lambda: (root_logger.removeHandler(handler), root_logger.setLevel(old_level)))
        reg, store = new_registry(self.tmp, route_provider=lambda _s: 19001)
        scope = make_scope(reg)
        reg.admit_signed_material(scope, envelope_for(scope, GRANT_A))
        import asyncio
        material = asyncio.run(reg.resolve_scoped_material(scope))
        errors = []
        for fn in (lambda: store.admit(scope, material_for(scope, GRANT_B)), lambda: store.load(make_scope(reg, invite_id="nope"))):
            try:
                fn()
            except Exception as exc:
                errors.append(exc)
        key = store.key_path.read_bytes()
        secrets_ = [GRANT_A, GRANT_B, key.hex(), base64.b64encode(key).decode(), repr(key),
                    canonical(material_for(scope, GRANT_A)).decode()]
        objects = [store, vars(store), reg, reg._peers[PEER_ID], reg.lookup(PEER_ID), scope,
                   ScopedPeerMaterial(scope, material), material, str(material), *errors,
                   [str(e) for e in errors], [repr(e) for e in errors], records]
        for obj in objects:
            for text in (repr(obj), str(obj)):
                for secret in secrets_:
                    self.assertNotIn(secret, text)
        self.assertEqual(material.grant, GRANT_A)
        self.assertTrue(errors)


class ConsentKeyRequiredTests(unittest.TestCase):
    def test_register_without_consent_public_key_fails_closed(self):
        reg = PeerRegistry()
        with self.assertRaises(PeerRegistryError) as cm:
            reg.register(peer_id=PEER_ID, installation_id=INSTALL_ID, profile="default", fingerprint=FINGERPRINT,
                         endpoint_id="endpoint-b", catalog=copy.deepcopy(CATALOG), consent_public_key=None,
                         tls_ca_pem=CA_PEM, pairing_generation=1)
        self.assertEqual(str(cm.exception), "invalid_consent_public_key")
        with self.assertRaises(PeerRegistryError):
            reg.lookup(PEER_ID)

    def test_consent_public_key_is_stored_as_given(self):
        key = bytes(range(100, 132))
        reg = PeerRegistry()
        reg.register(peer_id=PEER_ID, installation_id=INSTALL_ID, profile="default", fingerprint=FINGERPRINT,
                     endpoint_id="endpoint-b", catalog=copy.deepcopy(CATALOG), consent_public_key=key,
                     tls_ca_pem=CA_PEM, pairing_generation=1)
        self.assertEqual(reg.lookup(PEER_ID).consent_public_key, key)


class StrictPeerConfigTests(TmpCase):
    def entry(self):
        return json.loads(_peer_registry_payload())[0]

    def build(self, entries):
        path = self.tmp / "peers.json"
        fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(entries, f)
        return build_peer_registry(path)

    def assert_closed(self, entries):
        with self.assertRaises(RuntimeConfigError) as cm:
            self.build(entries)
        self.assertEqual(str(cm.exception), "peer_registry_invalid")

    def test_valid_entry_builds(self):
        self.assertEqual(self.build([self.entry()]).lookup(PEER_ID).pairing_generation, 1)

    def test_static_grant_key_fails_closed(self):
        self.assert_closed([dict(self.entry(), grant="static-fixture-grant-Qw83nT5kLp")])

    def test_adapter_url_key_fails_closed(self):
        self.assert_closed([dict(self.entry(), adapter_url="http://127.0.0.1:19001/links/static")])

    def test_unknown_key_fails_closed(self):
        self.assert_closed([dict(self.entry(), surprise_field="x")])

    def test_missing_consent_public_key_fails_closed(self):
        e = self.entry(); del e["consent_public_key_b64"]
        self.assert_closed([e])

    def test_missing_tls_ca_pem_fails_closed(self):
        e = self.entry(); del e["tls_ca_pem"]
        self.assert_closed([e])

    def test_missing_pairing_generation_fails_closed(self):
        e = self.entry(); del e["pairing_generation"]
        self.assert_closed([e])

    def test_consent_key_not_exactly_32_bytes_fails_closed(self):
        for label, raw in (("31", b"\x05" * 31), ("33", b"\x05" * 33), ("empty", b"")):
            with self.subTest(length=label):
                self.assert_closed([dict(self.entry(), consent_public_key_b64=base64.b64encode(raw).decode())])
        for label, value in (("not-base64", "!!!not-base64!!!"), ("not-str", 12345)):
            with self.subTest(kind=label):
                self.assert_closed([dict(self.entry(), consent_public_key_b64=value)])

    def test_load_service_is_disabled_when_peer_entry_carries_a_grant(self):
        def write(path, text):
            path.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(path.parent, 0o700)
            fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(text)
        outcomes = {}
        for label, entry in (("clean", self.entry()), ("grant", dict(self.entry(), grant="static-fixture-grant-Qw83nT5kLp"))):
            base = self.tmp / label
            write(base / "cfg" / "peers.json", json.dumps([entry]))
            write(base / "cfg" / "room-invites.json", json.dumps({
                "store_path": str(base / "store" / "invites.db"), "installation_id": OWNER_INSTALL, "profile": "default",
                "peer_registry_path": str(base / "cfg" / "peers.json")}))
            (base / "store").mkdir(mode=0o700)
            outcomes[label] = load_service({"MISSION_CONTROL_ROOM_INVITES_CONFIG": str(base / "cfg" / "room-invites.json")})
        self.assertIsNotNone(outcomes["clean"])
        self.assertIsNone(outcomes["grant"])


if __name__ == "__main__":
    unittest.main()
