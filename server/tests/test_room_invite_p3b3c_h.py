"""P3b.3c-H: R3B-02 (deadline is an admission check) and R3B-05 (fresh-generation receipt write).

Every test here is BEHAVIOURAL and exercises only APIs that already existed before the fix, so on
the pre-fix snapshot it fails with the DEFECT observed -- an already-expired ``admit`` publishing, a
stale registry persisting a newly scoped receipt -- rather than with a missing symbol.
"""
from __future__ import annotations

import asyncio
import copy
import threading
import time
import unittest
from unittest.mock import patch

try:
    from server.tests import test_room_invite_material_receipts as M
    from server.tests.test_room_invite_admission import CATALOG as HARNESS_CATALOG, Harness, bump_generation
except ModuleNotFoundError:
    import test_room_invite_material_receipts as M
    from test_room_invite_admission import CATALOG as HARNESS_CATALOG, Harness, bump_generation

from invite_rooms.material_receipts import ADMISSION_DEADLINE, MaterialReceiptError, MaterialReceiptStore
from invite_rooms.peer_registry import MaterialScope, PeerRegistry, PeerRegistryError, run_admission


class ExpiredDeadlineIsAnAdmissionCheckTests(M.TmpCase):
    """R3B-02: expiry refuses BEFORE the first effect, even when both locks are free."""

    def assert_nothing_written(self, store, scope):
        self.assertFalse((store.root / store.receipt_name(scope)).exists())
        self.assertEqual(list(store.root.glob("*.receipt")), [])
        self.assertEqual(list(store.root.glob("*.tmp-*")), [])

    def test_expired_explicit_deadline_on_free_locks_performs_no_write(self):
        reg, store = M.new_registry(self.tmp)
        scope = M.make_scope(reg)
        with self.assertRaises(MaterialReceiptError) as cm:
            store.admit(scope, M.material_for(scope, M.GRANT_A), deadline=time.monotonic() - 1.0)
        self.assertEqual(str(cm.exception), "receipt_busy")
        self.assert_nothing_written(store, scope)
        # Not latched and not poisoned: the same store admits once the request is inside its deadline.
        store.admit(scope, M.material_for(scope, M.GRANT_A))
        self.assertEqual(store.load(scope)["grant"], M.GRANT_A)

    def test_expired_context_deadline_performs_no_write(self):
        reg, store = M.new_registry(self.tmp)
        scope = M.make_scope(reg)
        token = ADMISSION_DEADLINE.set(time.monotonic() - 1.0)
        try:
            with self.assertRaises(MaterialReceiptError) as cm:
                store.admit(scope, M.material_for(scope, M.GRANT_A))
        finally:
            ADMISSION_DEADLINE.reset(token)
        self.assertEqual(str(cm.exception), "receipt_busy")
        self.assert_nothing_written(store, scope)

    def test_expiry_between_the_mutex_and_the_flock_performs_no_write(self):
        reg, store = M.new_registry(self.tmp)
        scope = M.make_scope(reg)
        started, release = threading.Event(), threading.Event()

        def holder():
            store._lock.acquire()
            started.set()
            release.wait(2.0)
            store._lock.release()

        thread = threading.Thread(target=holder)
        thread.start()
        self.assertTrue(started.wait(2.0))
        real = store._lock

        class DelayedAcquire:
            """The mutex is granted only after the deadline has already passed."""

            def acquire(self, timeout):
                release.set()
                result = real.acquire(timeout=timeout)
                time.sleep(0.04)
                return result

            def release(self):
                real.release()

        try:
            with patch.object(store, "_lock", DelayedAcquire()):
                with self.assertRaises(MaterialReceiptError) as cm:
                    store.admit(scope, M.material_for(scope, M.GRANT_A), deadline=time.monotonic() + 0.02)
        finally:
            release.set()
            thread.join(2.0)
        self.assertEqual(str(cm.exception), "receipt_busy")
        self.assert_nothing_written(store, scope)
        self.assertTrue(real.acquire(timeout=0.05), "the operation mutex must be released on refusal")
        real.release()

    def test_load_is_refused_when_the_deadline_already_expired(self):
        reg, store = M.new_registry(self.tmp)
        scope = M.make_scope(reg)
        store.admit(scope, M.material_for(scope, M.GRANT_A))
        with self.assertRaises(MaterialReceiptError) as cm:
            store.load(scope, deadline=time.monotonic() - 1.0)
        self.assertEqual(str(cm.exception), "receipt_busy")

    def test_expiry_after_missing_receipt_lookup_refuses_before_publication(self):
        reg, store = M.new_registry(self.tmp)
        scope = M.make_scope(reg)
        real_read = store._read_receipt

        def delayed_missing(receipt_scope):
            try:
                return real_read(receipt_scope)
            except MaterialReceiptError as exc:
                if str(exc) == "material_scope_unapproved":
                    time.sleep(0.05)
                raise

        with patch.object(store, "_read_receipt", delayed_missing):
            with self.assertRaises(MaterialReceiptError) as cm:
                store.admit(scope, M.material_for(scope, M.GRANT_A), deadline=time.monotonic() + 0.02)
        self.assertEqual(str(cm.exception), "receipt_busy")
        self.assert_nothing_written(store, scope)

    def test_expiry_during_final_receipt_name_computation_refuses_before_publication(self):
        reg, store = M.new_registry(self.tmp)
        scope = M.make_scope(reg)
        expected_name = store.receipt_name(scope)
        real_name = store.receipt_name
        real_publish = store._publish
        name_calls = []
        publish_entries = []

        def delayed_final_name(receipt_scope):
            name_calls.append(time.monotonic())
            if len(name_calls) == 2:  # the real missing lookup already computed the name once
                time.sleep(0.05)
            return real_name(receipt_scope)

        def recording_publish(name, data):
            publish_entries.append(time.monotonic())
            return real_publish(name, data)

        with patch.object(store, "receipt_name", delayed_final_name), patch.object(store, "_publish", recording_publish):
            with self.assertRaises(MaterialReceiptError) as cm:
                store.admit(scope, M.material_for(scope, M.GRANT_A), deadline=time.monotonic() + 0.02)
        self.assertEqual(str(cm.exception), "receipt_busy")
        self.assertGreaterEqual(len(name_calls), 2)
        self.assertEqual(publish_entries, [])
        self.assertFalse((store.root / expected_name).exists())
        self.assertEqual(list(store.root.glob("*.receipt")), [])
        self.assertEqual(list(store.root.glob("*.tmp-*")), [])

    def test_identical_existing_receipt_remains_idempotent_after_deadline_during_lookup(self):
        reg, store = M.new_registry(self.tmp)
        scope = M.make_scope(reg)
        material = M.material_for(scope, M.GRANT_A)
        store.admit(scope, material)
        receipt_path = store.root / store.receipt_name(scope)
        before = receipt_path.read_bytes()
        real_read = store._read_receipt

        def delayed_existing(receipt_scope):
            row = real_read(receipt_scope)
            time.sleep(0.05)
            return row

        with patch.object(store, "_read_receipt", delayed_existing):
            store.admit(scope, material, deadline=time.monotonic() + 0.02)
        self.assertEqual(receipt_path.read_bytes(), before)

    def test_inherited_deadline_reaches_envelope_publication_boundary(self):
        reg, store = M.new_registry(self.tmp)
        scope = M.make_scope(reg)
        envelope = M.envelope_for(scope, M.GRANT_A)
        expected = {"scope": envelope["scope"], "grant": envelope["grant"],
                    "catalog": envelope["catalog"], "purpose": "joining-owner-material-v1"}
        real_read = store._read_receipt

        def delayed_missing(receipt_scope):
            try:
                return real_read(receipt_scope)
            except MaterialReceiptError as exc:
                if str(exc) == "material_scope_unapproved":
                    time.sleep(0.05)
                raise

        token = ADMISSION_DEADLINE.set(time.monotonic() + 0.02)
        try:
            with patch.object(store, "_read_receipt", delayed_missing):
                with self.assertRaises(MaterialReceiptError) as cm:
                    store.admit_envelope(scope=scope, envelope=envelope,
                                         consent_public_key=M.CONSENT_PUBLIC, expected_signed=expected)
        finally:
            ADMISSION_DEADLINE.reset(token)
        self.assertEqual(str(cm.exception), "receipt_busy")
        self.assert_nothing_written(store, scope)


class StaleRegistryWriteTests(M.TmpCase):
    """R3B-05: the signed-material write re-reads the authoritative generation before any effect."""

    def fresh_scope(self, harness, invite_id="invite-r3b05"):
        return MaterialScope(**dict(harness.scope.as_dict(), invite_id=invite_id))

    def envelope(self, scope):
        return M.envelope_for(scope, M.GRANT_A, catalog=HARNESS_CATALOG)

    @staticmethod
    def receipt(harness, scope):
        store = harness.reg._receipt_store
        return store.root / store.receipt_name(scope)

    def test_generation_bumped_between_validation_and_write_is_refused_with_nothing_persisted(self):
        harness = Harness(self)
        scope = self.fresh_scope(harness)
        real = harness.reg._receipt_store.admit_envelope

        def bump_then_write(**kwargs):
            harness.rewrite_peers(bump_generation)  # the cutover lands after validation, before the effect
            return real(**kwargs)

        with patch.object(harness.reg._receipt_store, "admit_envelope", bump_then_write):
            with self.assertRaises(PeerRegistryError) as cm:
                harness.reg.admit_signed_material(scope, self.envelope(scope))
        self.assertEqual(str(cm.exception), "peer_binding_changed")
        self.assertFalse(self.receipt(harness, scope).exists())
        self.assertEqual(len(M.raw_rows(harness.reg._receipt_store)), 1)  # only the harness's own receipt

    def test_stale_registry_cannot_persist_after_the_disk_generation_moved(self):
        harness = Harness(self)
        scope = self.fresh_scope(harness)
        harness.rewrite_peers(bump_generation)  # on disk generation 2; this instance still says 1
        with self.assertRaises(PeerRegistryError) as cm:
            harness.reg.admit_signed_material(scope, self.envelope(scope))
        self.assertEqual(str(cm.exception), "peer_binding_changed")
        self.assertFalse(self.receipt(harness, scope).exists())
        self.assertEqual(len(M.raw_rows(harness.reg._receipt_store)), 1)

    def test_unchanged_authoritative_generation_still_admits_exactly_once(self):
        harness = Harness(self)
        scope = self.fresh_scope(harness)
        harness.reg.admit_signed_material(scope, self.envelope(scope))
        self.assertTrue(self.receipt(harness, scope).exists())
        harness.reg.admit_signed_material(scope, self.envelope(scope))  # identical re-admit stays idempotent
        self.assertEqual(len(M.raw_rows(harness.reg._receipt_store)), 2)

    def test_registry_without_an_authoritative_source_fails_closed(self):
        store = MaterialReceiptStore(self.tmp / "receipts")
        reg = PeerRegistry(receipt_store=store)
        reg.register(peer_id=M.PEER_ID, installation_id=M.INSTALL_ID, profile="default",
                     fingerprint=M.FINGERPRINT, endpoint_id="endpoint-b", catalog=copy.deepcopy(M.CATALOG),
                     consent_public_key=M.CONSENT_PUBLIC, tls_ca_pem=M.CA_PEM, pairing_generation=1)
        scope = M.make_scope(reg)
        with self.assertRaises(PeerRegistryError) as cm:
            reg.admit_signed_material(scope, M.envelope_for(scope, M.GRANT_A))
        self.assertEqual(str(cm.exception), "admission_unavailable")
        self.assertEqual(M.raw_rows(store), [])


class SaturatedMaterialLaneTests(M.TmpCase, unittest.IsolatedAsyncioTestCase):
    """R3B-04: a saturated admission pool must refuse the request, not serve it after its deadline."""

    async def test_saturated_material_lane_refuses_the_next_read_instead_of_queueing_it(self):
        reg, store = M.new_registry(self.tmp, route_provider=lambda _: 19001)
        scope = M.make_scope(reg)
        store.admit(scope, M.material_for(scope, M.GRANT_A))
        go = threading.Event()
        entered = [threading.Event() for _ in range(3)]

        def block(index):
            entered[index].set()
            go.wait(5.0)

        jobs = [asyncio.create_task(run_admission(block, index)) for index in range(3)]
        try:
            for event in entered:
                self.assertTrue(await asyncio.to_thread(event.wait, 5.0))
            token = ADMISSION_DEADLINE.set(time.monotonic() + 0.02)
            start = time.monotonic()
            try:
                with self.assertRaises(PeerRegistryError) as cm:
                    await reg.resolve_scoped_material(scope)
            finally:
                ADMISSION_DEADLINE.reset(token)
            elapsed = time.monotonic() - start
        finally:
            go.set()
            await asyncio.gather(*jobs)
        self.assertEqual(str(cm.exception), "admission_overloaded")
        self.assertLess(elapsed, 0.1, "a saturated pool must refuse, never queue the read past its deadline")


if __name__ == "__main__":
    unittest.main()
