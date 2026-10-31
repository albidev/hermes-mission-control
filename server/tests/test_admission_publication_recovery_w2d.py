import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from invite_rooms.admission_receipts import AdmissionReceiptError, AdmissionReceiptStore
from test_target_http_w2d_card1 import Scope


PYTHON = sys.executable


def receipt(scope, index=1):
    return {
        "scope": scope,
        "generation": 7,
        "profile": "default",
        "base_url": "http://127.0.0.1:12345",
        "idempotency_key": f"key-{index}",
        "request_fingerprint": f"{index:064x}",
        "run_id": "run_" + f"{index:032x}",
        "status": "started",
        "replayed": False,
    }


def snapshot(root):
    return {
        path.name: (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_nlink)
        for path in root.iterdir()
        if path.is_file()
    }


class AdmissionPublicationRecoveryW2DTests(unittest.TestCase):
    def test_replacement_keeps_exact_old_rows_when_guard_is_substituted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "admissions"
            store = AdmissionReceiptStore(root)
            store.put(**receipt(Scope(), 1))
            old_bytes = store.path.read_bytes()
            old_rows = store._read()
            real_link = os.link

            def substitute_guard(source, target, *args, **kwargs):
                result = real_link(source, target, *args, **kwargs)
                if str(target).endswith(".guard"):
                    os.unlink(target, dir_fd=kwargs["dst_dir_fd"])
                    fd = os.open(
                        target,
                        os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                        0o600,
                        dir_fd=kwargs["dst_dir_fd"],
                    )
                    try:
                        os.write(fd, b"attacker-guard")
                    finally:
                        os.close(fd)
                return result

            with mock.patch("invite_rooms.admission_receipts.os.link", substitute_guard):
                with self.assertRaises(AdmissionReceiptError):
                    store.put(**receipt(Scope(), 2))
            store.close()

            try:
                reopened = AdmissionReceiptStore(root)
            except AdmissionReceiptError as error:
                self.assertEqual(str(error), "admission_storage_corrupt")
                self.assertEqual((root / "admission_receipts.json.enc").read_bytes(), old_bytes)
            else:
                try:
                    self.assertEqual(reopened.path.read_bytes(), old_bytes)
                    self.assertEqual(reopened._read(), old_rows)
                    self.assertEqual(reopened.count(), 1)
                finally:
                    reopened.close()

    def test_rollback_failure_leaves_durable_recovery_authority(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "admissions"
            store = AdmissionReceiptStore(root)
            store.put(**receipt(Scope(), 1))
            old_bytes = store.path.read_bytes()
            old_rows = store._read()
            real_replace = os.replace
            real_rename = os.rename

            def corrupt_commit(source, target, *args, **kwargs):
                if target == "admission_receipts.json.enc":
                    os.unlink(source, dir_fd=kwargs["src_dir_fd"])
                    fd = os.open(
                        source,
                        os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                        0o600,
                        dir_fd=kwargs["src_dir_fd"],
                    )
                    try:
                        os.write(fd, b"attacker-commit")
                    finally:
                        os.close(fd)
                return real_replace(source, target, *args, **kwargs)

            def fail_restore(source, target, *args, **kwargs):
                if str(source).endswith((".backup", ".guard")):
                    raise OSError("injected rollback failure")
                return real_rename(source, target, *args, **kwargs)

            with mock.patch("invite_rooms.admission_receipts.os.replace", corrupt_commit), mock.patch(
                "invite_rooms.admission_receipts.os.rename", fail_restore
            ):
                with self.assertRaises(AdmissionReceiptError):
                    store.put(**receipt(Scope(), 2))
            store.close()

            # Recovery must either restore the exact old transaction or refuse
            # without destroying the journal/last-good artifact graph.
            try:
                reopened = AdmissionReceiptStore(root)
            except AdmissionReceiptError as error:
                self.fail(f"fresh recovery refused the durable old state: {error}")
            try:
                self.assertEqual(reopened.path.read_bytes(), old_bytes)
                self.assertEqual(reopened._read(), old_rows)
            finally:
                reopened.close()

    def test_pending_publication_recovers_without_losing_pending_row(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "admissions"
            store = AdmissionReceiptStore(root)
            pending = {
                key: value
                for key, value in receipt(Scope().as_dict(), 1).items()
                if key not in {"run_id", "status", "replayed"}
            }
            store._write_blob("admission_pending.json.enc", [pending])
            before = store._read_pending()
            store.close()
            reopened = AdmissionReceiptStore(root)
            try:
                self.assertEqual(reopened._read_pending(), before)
            finally:
                reopened.close()

    def test_journal_parser_reads_complete_record_and_rejects_truncation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "admissions"
            store = AdmissionReceiptStore(root)
            store.put(**receipt(Scope(), 1))
            store.put(**receipt(Scope(), 2))
            journal = (root / "admission_receipts.lock").read_bytes()
            self.assertEqual(journal, b"\x01")
            fields = [
                "\x01",
                "admission_receipts.json.enc",
                ".admission_receipts.json.enc." + "a" * 32 + ".tmp",
                ".admission_receipts.json.enc." + "a" * 32 + ".commit",
                "1",
                "2",
                "3",
                "4",
                "a" * 64,
                ".admission_receipts.json.enc." + "b" * 32 + ".backup",
                "1",
                "2",
                "3",
                "4",
                "b" * 64,
                ".admission_receipts.json.enc." + "c" * 32 + ".guard",
                "1",
                "2",
                "3",
                "4",
                "c" * 64,
            ]
            raw = "|".join(fields).encode("ascii")
            self.assertGreater(len(raw), 512)
            (root / fields[2]).write_bytes(b"stage")
            (root / fields[2]).chmod(0o600)
            (root / "admission_receipts.lock").write_bytes(raw[:512])
            store.close()
            with self.assertRaises(AdmissionReceiptError) as error:
                AdmissionReceiptStore(root)
            self.assertEqual(str(error.exception), "admission_storage_corrupt")

    def test_unknown_artifact_is_rejected_without_cleaning_previous_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "admissions"
            store = AdmissionReceiptStore(root)
            store.put(**receipt(Scope(), 1))
            store.close()
            forged = root / (".admission_receipts.json.enc." + "d" * 32 + ".tmp")
            forged.write_bytes(b"forged")
            forged.chmod(0o600)
            before = snapshot(root)
            with self.assertRaises(AdmissionReceiptError):
                AdmissionReceiptStore(root)
            self.assertEqual(snapshot(root), before)
            self.assertEqual(forged.read_bytes(), b"forged")

    def test_real_child_crash_before_commit_reopens_old_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "admissions"
            store = AdmissionReceiptStore(root)
            store.put(**receipt(Scope(), 1))
            old_rows = store._read()
            store.close()
            script = r'''
import os, sys
from unittest.mock import patch
from invite_rooms.admission_receipts import AdmissionReceiptStore
from test_admission_publication_recovery_w2d import receipt
from test_target_http_w2d_card1 import Scope
root = sys.argv[1]
real_fsync = os.fsync
def crash(fd):
    real_fsync(fd)
    if os.pread(fd, 1, 0) == b"\x01":
        os._exit(77)
with patch("invite_rooms.admission_receipts.os.fsync", crash):
    store = AdmissionReceiptStore(root)
    store.put(**receipt(Scope(), 2))
'''
            process = subprocess.run(
                [PYTHON, "-B", "-W", "error::ResourceWarning", "-c", script, str(root)],
                cwd=os.getcwd(),
                env=dict(os.environ),
                capture_output=True,
                text=True,
            )
            self.assertEqual(process.returncode, 77, process.stderr)
            reopened = AdmissionReceiptStore(root)
            try:
                self.assertEqual(reopened._read(), old_rows)
            finally:
                reopened.close()


if __name__ == "__main__":
    unittest.main()
