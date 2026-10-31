import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import invite_rooms.admission_receipts as ar
from invite_rooms.admission_receipts import AdmissionReceiptError, AdmissionReceiptStore
from test_target_http_w2d_card1 import Scope
from test_admission_publication_recovery_w2d import receipt


class AdmissionPublicationR06DTests(unittest.TestCase):
    def _kw(self, number):
        return receipt(Scope(), number)

    def _seed(self, root):
        store = AdmissionReceiptStore(root)
        store.put(**self._kw(1))
        store.put(**self._kw(2))
        pending = self._kw(3)
        store.reserve(**{key: value for key, value in pending.items()
                         if key not in {"run_id", "status", "replayed"}})
        return store, pending

    def _child_read(self, root, timeout=4):
        code = (
            "import json, sys\n"
            "from invite_rooms.admission_receipts import AdmissionReceiptStore\n"
            "root=sys.argv[1]\n"
            "try:\n"
            " s=AdmissionReceiptStore(root)\n"
            " print(json.dumps({'ok': True, 'rows': s._read()}))\n"
            " s.close()\n"
            "except Exception as e:\n"
            " print(json.dumps({'ok': False, 'error': str(e)}))\n"
        )
        env = {"HOME": str(root / "home"), "TMPDIR": str(root / "tmp"),
               "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
               "PYTHONPATH": os.pathsep.join(("server", "server/tests")),
               "PYTHONDONTWRITEBYTECODE": "1"}
        proc = subprocess.Popen(
            [sys.executable, "-B", "-W", "error::ResourceWarning", "-c", code, str(root)],
            cwd=os.getcwd(), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        start = time.monotonic()
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
        return {"exit": proc.returncode, "stdout": stdout, "stderr": stderr,
                "elapsed": time.monotonic() - start}

    def test_stage_unlink_failure_rolls_back_with_replayable_authority(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "admissions"
            store, pending = self._seed(root)
            old_bytes = store.path.read_bytes()
            old_rows = store._read()
            old_pending = store._read_pending()
            real_unlink = os.unlink
            reached = []

            def mutate_stage_unlink(name, *args, **kwargs):
                if str(name).endswith(".tmp"):
                    reached.append("stage-unlink")
                    fd = os.open(ar._FILE_NAME, os.O_WRONLY, dir_fd=kwargs["dir_fd"])
                    try:
                        os.pwrite(fd, b"X", 0)
                    finally:
                        os.close(fd)
                return real_unlink(name, *args, **kwargs)

            with mock.patch.object(ar.os, "unlink", mutate_stage_unlink):
                with self.assertRaises(AdmissionReceiptError):
                    store.put(**self._kw(3))
            store.close()
            self.assertEqual(reached, ["stage-unlink"])
            reopened = AdmissionReceiptStore(root)
            try:
                self.assertEqual(reopened.path.read_bytes(), old_bytes)
                self.assertEqual(reopened._read(), old_rows)
                self.assertEqual(reopened._read_pending(), old_pending)
                self.assertIsNone(reopened.find_idempotency_key("key-3"))
            finally:
                reopened.close()
            again = self._child_read(root)
            self.assertEqual(again["exit"], 0, again)
            self.assertIn('"ok": true', again["stdout"])

    def test_second_directory_sync_keeps_old_recovery_until_ack(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "admissions"
            store, _ = self._seed(root)
            old_bytes = store.path.read_bytes()
            real_fsync = os.fsync
            directory_syncs = 0
            reached = []

            def mutate_second_sync(fd):
                nonlocal directory_syncs
                if stat.S_ISDIR(os.fstat(fd).st_mode):
                    directory_syncs += 1
                    if directory_syncs == 2:
                        reached.append("second-directory-fsync")
                        target = os.open(ar._FILE_NAME, os.O_WRONLY, dir_fd=fd)
                        try:
                            os.pwrite(target, b"X", 0)
                        finally:
                            os.close(target)
                return real_fsync(fd)

            with mock.patch.object(ar.os, "fsync", mutate_second_sync):
                with self.assertRaises(AdmissionReceiptError):
                    store.put(**self._kw(3))
            store.close()
            self.assertEqual(reached, ["second-directory-fsync"])
            reopened = AdmissionReceiptStore(root)
            try:
                self.assertEqual(reopened.path.read_bytes(), old_bytes)
                self.assertIsNone(reopened.find_idempotency_key("key-3"))
            finally:
                reopened.close()

    def test_unknown_guard_alias_is_rejected_without_deleting_any_role(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "admissions"
            store = AdmissionReceiptStore(root)
            store.put(**self._kw(1))
            store.put(**self._kw(2))
            real_replace = os.replace
            def interrupt_replace(source, target, *args, **kwargs):
                if target == ar._FILE_NAME:
                    raise OSError("simulated producer interruption")
                return real_replace(source, target, *args, **kwargs)
            with mock.patch.object(ar.os, "replace", interrupt_replace):
                with self.assertRaises(AdmissionReceiptError):
                    store.put(**self._kw(3))
            store.close()
            lock = root / ar._LOCK_NAME
            raw = lock.read_bytes()
            parts = raw.split(b"|")
            guard = parts[15].decode()
            key_before = (root / ar._KEY_NAME).read_bytes()
            final_before = (root / ar._FILE_NAME).read_bytes()
            (root / guard).unlink()
            forged = root / ".foreign.guard"
            forged.write_bytes(b"forged")
            forged.chmod(0o600)
            parts[15] = forged.name.encode()
            lock.write_bytes(b"|".join(parts))
            with self.assertRaises(AdmissionReceiptError):
                AdmissionReceiptStore(root)
            self.assertEqual((root / ar._KEY_NAME).read_bytes(), key_before)
            self.assertEqual((root / ar._FILE_NAME).read_bytes(), final_before)
            self.assertTrue(forged.exists())
            self.assertTrue(lock.exists())

    def test_fifo_stage_reopen_is_bounded_and_non_destructive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "admissions"
            store = AdmissionReceiptStore(root)
            store.put(**self._kw(1))
            store.put(**self._kw(2))
            real_replace = os.replace
            def interrupt_replace(source, target, *args, **kwargs):
                if target == ar._FILE_NAME:
                    raise OSError("simulated producer interruption")
                return real_replace(source, target, *args, **kwargs)
            with mock.patch.object(ar.os, "replace", interrupt_replace):
                with self.assertRaises(AdmissionReceiptError):
                    store.put(**self._kw(3))
            store.close()
            lock = root / ar._LOCK_NAME
            parts = lock.read_bytes().split(b"|")
            stage = parts[2].decode()
            (root / stage).unlink()
            os.mkfifo(root / stage, 0o600)
            self.assertTrue(stat.S_ISFIFO((root / stage).stat().st_mode))
            before = {p.name: p.stat().st_ino for p in root.iterdir()}
            result = self._child_read(root)
            self.assertEqual(result["exit"], 0, result)
            self.assertLess(result["elapsed"], 4)
            self.assertIn("admission_storage_corrupt", result["stdout"])
            self.assertEqual({p.name: p.stat().st_ino for p in root.iterdir()}, before)


if __name__ == "__main__":
    unittest.main()
