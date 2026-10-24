"""P3b.0-F R-04 (malformed grant), R-03 (receipt storage anchoring) and R-06 (atomic key bootstrap)."""
from __future__ import annotations

import asyncio
import copy
import os
import sqlite3
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import invite_rooms.material_receipts as mr
from invite_rooms.consent import canonical
from invite_rooms.material_receipts import MaterialReceiptError, MaterialReceiptStore
from invite_rooms.peer_registry import PeerRegistryError

try:
    import server.tests.test_room_invite_material_receipts as MR
except ModuleNotFoundError:
    import test_room_invite_material_receipts as MR

GRANT_A, GRANT_B = MR.GRANT_A, MR.GRANT_B
LOCK = ".material-receipts.lock"


def names(root):
    return sorted(p.name for p in Path(root).iterdir())


class MalformedGrantTests(MR.TmpCase):
    BAD = (("null", None), ("bool", True), ("int", 12), ("float", 1.5), ("list", ["x"]), ("empty", ""),
           ("oversize", "g" * 8193), ("whitespace", "grant with space"), ("control", "grant\x00nul"))

    def test_malformed_signed_grant_leaves_zero_rows_and_a_valid_one_is_admitted_afterwards(self):
        for label, grant in self.BAD:
            with self.subTest(grant=label):
                reg, store = MR.new_registry(self.tmp / label, route_provider=lambda _s: 19001)
                scope = MR.make_scope(reg)
                with self.assertRaises(PeerRegistryError) as cm:
                    reg.admit_signed_material(scope, MR.envelope_for(scope, grant))
                self.assertEqual(str(cm.exception), "invalid_material")
                self.assertEqual(MR.raw_rows(store), [])
                reg.admit_signed_material(scope, MR.envelope_for(scope, GRANT_A))  # not poisoned
                self.assertEqual(len(MR.raw_rows(store)), 1)
                material = asyncio.run(reg.resolve_scoped_material(scope))
                self.assertEqual(material.grant, GRANT_A)

    def test_grant_cap_boundary_is_8192_the_target_side_limit(self):
        reg, store = MR.new_registry(self.tmp, route_provider=lambda _s: 19001)
        scope = MR.make_scope(reg)
        reg.admit_signed_material(scope, MR.envelope_for(scope, "g" * 8192))
        self.assertEqual(len(asyncio.run(reg.resolve_scoped_material(scope)).grant), 8192)

    def test_store_admit_rejects_malformed_material_directly(self):
        reg, store = MR.new_registry(self.tmp)
        scope = MR.make_scope(reg)
        for label, grant in self.BAD:
            with self.subTest(grant=label):
                with self.assertRaises(MaterialReceiptError) as cm:
                    store.admit(scope, MR.material_for(scope, grant))
                self.assertEqual(str(cm.exception), "invalid_material")
                self.assertEqual(MR.raw_rows(store), [])

    def test_malformed_envelope_shape_is_rejected_before_any_row(self):
        reg, store = MR.new_registry(self.tmp)
        scope = MR.make_scope(reg)
        good = MR.envelope_for(scope, GRANT_A)
        cases = {"extra_key": dict(good, extra=1), "missing_signature": {k: v for k, v in good.items() if k != "signature"},
                 "non_dict": ["x"], "scope_not_dict": dict(good, scope="x"), "catalog_not_dict": dict(good, catalog="x"),
                 "signature_not_str": dict(good, signature=7)}
        for label, env in cases.items():
            with self.subTest(case=label):
                with self.assertRaises(PeerRegistryError):
                    reg.admit_signed_material(scope, env)
                self.assertEqual(MR.raw_rows(store), [])

    def test_load_revalidates_a_row_that_somehow_holds_a_malformed_grant(self):
        reg, store = MR.new_registry(self.tmp, route_provider=lambda _s: 19001)
        scope = MR.make_scope(reg)
        for label, grant in (("null", None), ("empty", ""), ("int", 5)):
            with self.subTest(grant=label):
                with sqlite3.connect(store.path) as db:
                    db.execute("DELETE FROM receipts")
                plaintext = canonical(MR.material_for(scope, grant))
                import hashlib
                nonce = b"\x07" * 12
                blob = store._aes.encrypt(nonce, plaintext, store._aad(scope))
                with sqlite3.connect(store.path) as db:
                    db.execute("INSERT INTO receipts VALUES(?,?,?,?,?)", (store.key_for(scope), 1, nonce, blob, hashlib.sha256(plaintext).digest()))
                with self.assertRaises(MaterialReceiptError) as cm:
                    store.load(scope)
                self.assertEqual(str(cm.exception), "material_unavailable")
                with self.assertRaises(PeerRegistryError):
                    asyncio.run(reg.resolve_scoped_material(scope))

    def test_registry_material_record_rejects_empty_or_oversize_grant(self):
        reg, store = MR.new_registry(self.tmp, route_provider=lambda _s: 19001)
        scope = MR.make_scope(reg)
        for label, grant in (("empty", ""), ("oversize", "g" * 8193), ("none", None)):
            with self.subTest(grant=label):
                with patch.object(store, "load", return_value=MR.material_for(scope, grant)):
                    with self.assertRaises(PeerRegistryError):
                        asyncio.run(reg.resolve_scoped_material(scope))


class AnchoredStorageTests(MR.TmpCase):
    def test_symlinked_parent_of_the_store_root_is_refused_and_nothing_is_written_through_it(self):
        real = self.tmp / "real-parent"
        real.mkdir(mode=0o700)
        alias = self.tmp / "parent-alias"
        alias.symlink_to(real, target_is_directory=True)
        with self.assertRaises(MaterialReceiptError) as cm:
            MaterialReceiptStore(alias / "receipts")
        self.assertEqual(str(cm.exception), "private_storage_required")
        self.assertEqual(names(real), [])

    def test_symlinked_ancestor_below_the_trusted_anchor_is_refused(self):
        real = self.tmp / "real"
        (real / "mid").mkdir(parents=True, mode=0o700)
        os.chmod(real, 0o700)
        (self.tmp / "link").symlink_to(real, target_is_directory=True)
        with self.assertRaises(MaterialReceiptError) as cm:
            MaterialReceiptStore(self.tmp / "link" / "mid" / "receipts", anchor=self.tmp)
        self.assertEqual(str(cm.exception), "private_storage_required")
        self.assertEqual(names(real / "mid"), [])

    def test_ancestors_above_the_anchor_may_be_symlinks_or_system_owned(self):
        # The anchor itself is validated; its own ancestors (e.g. /var -> /private/var) are not our business.
        real = self.tmp / "real-above"
        real.mkdir(mode=0o700)
        (self.tmp / "above-link").symlink_to(real, target_is_directory=True)
        anchor = self.tmp / "above-link" / "anchor"
        (real / "anchor").mkdir(mode=0o700)
        store = MaterialReceiptStore(anchor / "nested" / "receipts", anchor=anchor)
        self.assertTrue((real / "anchor" / "nested" / "receipts" / "material_receipts.key").exists())
        self.assertEqual(stat.S_IMODE(store.root.stat().st_mode), 0o700)

    def test_intermediate_directory_group_or_world_writable_is_refused(self):
        mid = self.tmp / "wide-mid"
        mid.mkdir(mode=0o700)
        os.chmod(mid, 0o720)
        with self.assertRaises(MaterialReceiptError):
            MaterialReceiptStore(mid / "receipts", anchor=self.tmp)
        self.assertEqual(names(mid), [])

    def test_root_swapped_for_a_symlink_during_bootstrap_never_redirects_key_or_database(self):
        root, dest, moved = self.tmp / "swap", self.tmp / "dest", self.tmp / "moved"
        dest.mkdir(mode=0o700)
        original = mr.AESGCM.generate_key

        def swapping(*args, **kwargs):
            root.rename(moved)
            root.symlink_to(dest, target_is_directory=True)
            return original(*args, **kwargs)

        with patch.object(mr.AESGCM, "generate_key", swapping):
            with self.assertRaises(MaterialReceiptError):
                MaterialReceiptStore(root)
        self.assertEqual(names(dest), [])

    def test_root_replaced_after_construction_is_not_followed_by_later_operations(self):
        reg, store = MR.new_registry(self.tmp)
        scope = MR.make_scope(reg)
        store.admit(scope, MR.material_for(scope, GRANT_A))
        old = store.root.with_name("receipts-moved")
        store.root.rename(old)
        store.root.mkdir(mode=0o700)
        with self.assertRaises(MaterialReceiptError):
            store.load(scope)
        with self.assertRaises(MaterialReceiptError):
            store.admit(scope, MR.material_for(scope, GRANT_B))
        self.assertEqual(names(store.root), [])
        self.assertEqual(len(MR.raw_rows_at(old / "material_receipts.sqlite")), 1)

    def test_database_replaced_by_symlink_after_init_makes_load_and_admit_fail(self):
        reg, store = MR.new_registry(self.tmp)
        scope = MR.make_scope(reg)
        store.admit(scope, MR.material_for(scope, GRANT_A))
        decoy = self.tmp / "decoy.sqlite"
        decoy.write_bytes(store.path.read_bytes())
        os.chmod(decoy, 0o600)
        store.path.unlink()
        store.path.symlink_to(decoy)
        with self.assertRaises(MaterialReceiptError) as cm:
            store.load(scope)
        self.assertEqual(str(cm.exception), "private_storage_required")
        with self.assertRaises(MaterialReceiptError):
            store.admit(scope, MR.material_for(scope, GRANT_B))
        self.assertEqual(len(MR.raw_rows_at(decoy)), 1)

    def test_database_replaced_by_a_different_regular_file_after_init_is_refused(self):
        reg, store = MR.new_registry(self.tmp)
        scope = MR.make_scope(reg)
        store.admit(scope, MR.material_for(scope, GRANT_A))
        swapped = store.root / "swapped.sqlite"
        swapped.write_bytes(store.path.read_bytes())
        os.chmod(swapped, 0o600)
        os.replace(swapped, store.path)  # same bytes and mode, different inode
        with self.assertRaises(MaterialReceiptError):
            store.load(scope)

    def test_existing_broad_lock_file_is_refused_and_left_unchanged(self):
        root = self.tmp / "lockbroad"
        root.mkdir(mode=0o700)
        lock = root / LOCK
        lock.write_bytes(b"")
        os.chmod(lock, 0o666)
        with self.assertRaises(MaterialReceiptError) as cm:
            MaterialReceiptStore(root)
        self.assertEqual(str(cm.exception), "private_storage_required")
        self.assertEqual(stat.S_IMODE(lock.stat().st_mode), 0o666)
        self.assertEqual(names(root), [LOCK])

    def test_symlinked_or_non_regular_lock_is_refused(self):
        for label in ("symlink", "directory"):
            with self.subTest(lock=label):
                root = self.tmp / ("lock-" + label)
                root.mkdir(mode=0o700)
                if label == "symlink":
                    target = self.tmp / "lock-target"
                    target.write_bytes(b"")
                    os.chmod(target, 0o600)
                    (root / LOCK).symlink_to(target)
                else:
                    (root / LOCK).mkdir(mode=0o700)
                with self.assertRaises(MaterialReceiptError) as cm:
                    MaterialReceiptStore(root)
                self.assertEqual(str(cm.exception), "private_storage_required")
                self.assertFalse((root / "material_receipts.key").exists())

    def test_foreign_owned_lock_is_refused(self):
        root = self.tmp / "lockforeign"
        root.mkdir(mode=0o700)
        MaterialReceiptStore(root)
        real_fstat = os.fstat

        class Foreign:
            def __init__(self, st): self._st = st
            def __getattr__(self, name): return getattr(self._st, name)
            st_uid = os.getuid() + 4242

        def fake_fstat(fd, *a, **k):
            st = real_fstat(fd, *a, **k)
            if stat.S_ISREG(st.st_mode) and st.st_size == 0 and stat.S_IMODE(st.st_mode) == 0o600:
                return Foreign(st)  # only the (empty) lock file looks foreign
            return st

        with patch.object(mr.os, "fstat", fake_fstat):
            with self.assertRaises(MaterialReceiptError):
                MaterialReceiptStore(root)


class AtomicKeyBootstrapTests(MR.TmpCase):
    def fresh_root(self, label):
        return self.tmp / label

    def assert_no_partial_state(self, root):
        self.assertFalse((root / "material_receipts.key").exists(), names(root))
        self.assertFalse((root / "material_receipts.sqlite").exists(), names(root))
        self.assertLessEqual(set(names(root)), {LOCK}, names(root))

    def assert_retry_works(self, root):
        store = MaterialReceiptStore(root)
        self.assertEqual(len((root / "material_receipts.key").read_bytes()), 32)
        self.assertEqual(stat.S_IMODE((root / "material_receipts.key").stat().st_mode), 0o600)
        return store

    def test_fault_before_key_generation(self):
        root = self.fresh_root("gen")
        with patch.object(mr.AESGCM, "generate_key", side_effect=RuntimeError("inject-generate")):
            with self.assertRaises(Exception):
                MaterialReceiptStore(root)
        self.assert_no_partial_state(root)
        self.assert_retry_works(root)

    def test_fault_while_writing_the_staging_key(self):
        root = self.fresh_root("write")
        with patch.object(mr.os, "write", side_effect=OSError("inject-write")):
            with self.assertRaises(Exception):
                MaterialReceiptStore(root)
        self.assert_no_partial_state(root)
        self.assert_retry_works(root)

    def test_short_write_is_completed_never_published_truncated(self):
        root = self.fresh_root("short")
        real_write = os.write
        calls = []

        def short(fd, data):
            calls.append(len(data))
            return real_write(fd, bytes(data)[:5]) if len(calls) == 1 else real_write(fd, data)

        with patch.object(mr.os, "write", short):
            store = MaterialReceiptStore(root)
        self.assertGreaterEqual(len(calls), 2)
        self.assertEqual(len((root / "material_receipts.key").read_bytes()), 32)

    def test_fault_at_fsync_of_the_staging_key(self):
        root = self.fresh_root("fsync")
        with patch.object(mr.os, "fsync", side_effect=OSError("inject-fsync")):
            with self.assertRaises(Exception):
                MaterialReceiptStore(root)
        self.assert_no_partial_state(root)
        self.assert_retry_works(root)

    def test_fault_at_publication(self):
        root = self.fresh_root("publish")
        with patch.object(mr.os, "link", side_effect=OSError("inject-publish")):
            with self.assertRaises(Exception):
                MaterialReceiptStore(root)
        self.assert_no_partial_state(root)
        self.assert_retry_works(root)

    def test_fault_at_directory_fsync_never_leaves_a_partial_final_key(self):
        root = self.fresh_root("dirfsync")
        real_fsync = os.fsync
        count = []

        def second_fails(fd):
            count.append(fd)
            if len(count) == 2:
                raise OSError("inject-dir-fsync")
            return real_fsync(fd)

        with patch.object(mr.os, "fsync", second_fails):
            with self.assertRaises(Exception):
                MaterialReceiptStore(root)
        key = root / "material_receipts.key"
        self.assertTrue(not key.exists() or len(key.read_bytes()) == 32)  # complete or absent, never partial
        self.assertFalse((root / "material_receipts.sqlite").exists())
        self.assertEqual([n for n in names(root) if n.endswith(".tmp")], [])
        self.assert_retry_works(root)

    def test_staging_key_is_private_and_published_by_link_never_overwriting(self):
        root = self.fresh_root("staging")
        seen = []
        real_link = os.link

        def spy(src, dst, **kw):
            seen.append((stat.S_IMODE(os.stat(src, dir_fd=kw.get("src_dir_fd"), follow_symlinks=False).st_mode),
                         os.stat(src, dir_fd=kw.get("src_dir_fd"), follow_symlinks=False).st_size))
            return real_link(src, dst, **kw)

        with patch.object(mr.os, "link", spy):
            MaterialReceiptStore(root)
        self.assertEqual(seen, [(0o600, 32)])  # fully written before it can become the final name
        self.assertEqual(names(root), [LOCK, "material_receipts.key", "material_receipts.sqlite"])

    def test_existing_database_without_key_never_regenerates(self):
        reg, store = MR.new_registry(self.tmp)
        scope = MR.make_scope(reg)
        store.admit(scope, MR.material_for(scope, GRANT_A))
        store.key_path.unlink()
        with patch.object(mr.AESGCM, "generate_key", side_effect=AssertionError("must not regenerate")):
            with self.assertRaises(MaterialReceiptError) as cm:
                MaterialReceiptStore(store.root)
        self.assertEqual(str(cm.exception), "receipt_key_missing")
        self.assertFalse(store.key_path.exists())
        self.assertEqual([n for n in names(store.root) if n.endswith(".tmp")], [])


if __name__ == "__main__":
    unittest.main()
