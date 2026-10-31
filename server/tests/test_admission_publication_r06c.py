import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from invite_rooms.admission_receipts import AdmissionReceiptError, AdmissionReceiptStore
from test_target_http_w2d_card1 import Scope
from test_admission_publication_recovery_w2d import receipt


class AdmissionPublicationR06CTests(unittest.TestCase):
    def test_cleanup_failure_after_commit_is_acknowledged_and_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "admissions"
            store = AdmissionReceiptStore(root)
            store.put(**receipt(Scope(), 1))
            pending = receipt(Scope(), 2)
            store.reserve(**{key: value for key, value in pending.items()
                             if key not in {"run_id", "status", "replayed"}})
            real_unlink = os.unlink
            failures = {"count": 0}

            def fail_guard_cleanup(name, *args, **kwargs):
                if str(name).endswith(".guard") and failures["count"] == 0:
                    failures["count"] += 1
                    raise OSError("post-commit cleanup")
                return real_unlink(name, *args, **kwargs)

            with mock.patch("invite_rooms.admission_receipts.os.unlink", fail_guard_cleanup):
                result = store.put(**receipt(Scope(), 2))
            self.assertEqual(result["idempotency_key"], "key-2")
            self.assertEqual(store.find_idempotency_key("key-2")["run_id"], "run_" + "0" * 31 + "2")
            store.close()

            reopened = AdmissionReceiptStore(root)
            try:
                self.assertEqual(reopened.find_idempotency_key("key-2")["idempotency_key"], "key-2")
                self.assertEqual(reopened._read_pending(), [])
            finally:
                reopened.close()

    def test_old_state_is_independent_before_guard_use_and_survives_in_place_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "admissions"
            store = AdmissionReceiptStore(root)
            store.put(**receipt(Scope(), 1))
            old_bytes = store.path.read_bytes()
            old_rows = store._read()
            real_link = os.link

            def mutate_guard_after_link(source, target, *args, **kwargs):
                result = real_link(source, target, *args, **kwargs)
                if str(target).endswith(".guard"):
                    fd = os.open(target, os.O_WRONLY, dir_fd=kwargs["dst_dir_fd"])
                    try:
                        os.pwrite(fd, b"X", 0)
                    finally:
                        os.close(fd)
                return result

            with mock.patch("invite_rooms.admission_receipts.os.link", mutate_guard_after_link):
                with self.assertRaises(AdmissionReceiptError):
                    store.put(**receipt(Scope(), 2))
            store.close()

            reopened = AdmissionReceiptStore(root)
            try:
                self.assertEqual(reopened.path.read_bytes(), old_bytes)
                self.assertEqual(reopened._read(), old_rows)
                self.assertIsNone(reopened.find_idempotency_key("key-2"))
            finally:
                reopened.close()

    def test_final_mutation_before_return_cannot_be_acknowledged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "admissions"
            store = AdmissionReceiptStore(root)
            store.put(**receipt(Scope(), 1))
            old_bytes = store.path.read_bytes()
            real_fsync = os.fsync
            mutated = {"done": False}

            def mutate_at_directory_sync(fd):
                result = real_fsync(fd)
                if not mutated["done"] and os.fstat(fd).st_mode & 0o170000 == 0o040000:
                    mutated["done"] = True
                    target = os.open("admission_receipts.json.enc", os.O_WRONLY, dir_fd=fd)
                    try:
                        os.pwrite(target, b"X", 0)
                    finally:
                        os.close(target)
                return result

            with mock.patch("invite_rooms.admission_receipts.os.fsync", mutate_at_directory_sync):
                with self.assertRaises(AdmissionReceiptError):
                    store.put(**receipt(Scope(), 2))
            store.close()
            reopened = AdmissionReceiptStore(root)
            try:
                self.assertEqual(reopened.path.read_bytes(), old_bytes)
                self.assertIsNone(reopened.find_idempotency_key("key-2"))
            finally:
                reopened.close()

    def test_pending_cleanup_failure_is_a_bounded_retry_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "admissions"
            store = AdmissionReceiptStore(root)
            pending = receipt(Scope(), 1)
            store.reserve(**{key: value for key, value in pending.items()
                             if key not in {"run_id", "status", "replayed"}})
            real_unlink = os.unlink
            failures = {"count": 0}

            def fail_pending_cleanup(name, *args, **kwargs):
                if str(name) == "admission_pending.json.enc" and failures["count"] == 0:
                    failures["count"] += 1
                    raise OSError("deferred pending cleanup")
                return real_unlink(name, *args, **kwargs)

            with mock.patch("invite_rooms.admission_receipts.os.unlink", fail_pending_cleanup):
                store.put(**pending)
                self.assertTrue((root / "admission_pending.json.enc").exists())
            store.put(**pending)
            self.assertEqual(store._read_pending(), [])
            store.close()


if __name__ == "__main__":
    unittest.main()
