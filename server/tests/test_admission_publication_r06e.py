"""R06E store-protocol regressions.

Every case drives the public store API (put/reserve/constructor), reaches an
actual in-call seam or a real producer crash (os._exit in a child), and then
verifies through a clean child process and an independent next writer.
"""
import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import invite_rooms.admission_receipts as ar
from invite_rooms.admission_receipts import AdmissionReceiptError, AdmissionReceiptStore
from test_admission_publication_recovery_w2d import receipt
from test_target_http_w2d_card1 import Scope

CHILD_READ = r'''
import json, sys
from invite_rooms.admission_receipts import AdmissionReceiptStore
try:
    store = AdmissionReceiptStore(sys.argv[1])
    try:
        out = {"ok": True, "rows": store._read(), "pending": store._read_pending()}
        if len(sys.argv) > 2:
            from test_admission_publication_recovery_w2d import receipt
            from test_target_http_w2d_card1 import Scope
            store.put(**receipt(Scope(), int(sys.argv[2])))
            out["rows"] = store._read()
            out["pending"] = store._read_pending()
    finally:
        store.close()
except Exception as error:
    out = {"ok": False, "error": str(error), "type": type(error).__name__}
print(json.dumps(out))
'''

CHILD_CRASH = r'''
import os, sys
import invite_rooms.admission_receipts as ar
from unittest.mock import patch
from test_admission_publication_recovery_w2d import receipt
from test_target_http_w2d_card1 import Scope
store = ar.AdmissionReceiptStore(sys.argv[1])
real_replace = os.replace
def replace(src, dst, *args, **kwargs):
    if dst == ar._FILE_NAME:
        os.write(1, b"CRASH_SEAM=before-replace\n")
        os._exit(77)
    return real_replace(src, dst, *args, **kwargs)
with patch.object(ar.os, "replace", replace):
    store.put(**receipt(Scope(), 3))
raise SystemExit("crash seam not reached")
'''

EFFECTS = ("link", "replace", "rename", "unlink", "fsync", "ftruncate", "pwrite", "write", "open", "mkdir")


def snapshot(root):
    out = {}
    for path in sorted(Path(root).iterdir()):
        info = path.lstat()
        regular = stat.S_ISREG(info.st_mode)
        out[path.name] = (info.st_ino, info.st_nlink, info.st_size, info.st_mode,
                          hashlib.sha256(path.read_bytes()).hexdigest() if regular else None)
    return out


def flip_final(root):
    fd = os.open(os.path.join(str(root), ar._FILE_NAME), os.O_RDWR)
    try:
        byte = os.pread(fd, 1, 0)
        os.pwrite(fd, bytes([byte[0] ^ 0xFF]), 0)
    finally:
        os.close(fd)


def counting(callback):
    """Patch every filesystem effect of the module under test with a counter."""
    state = {"n": 0}
    real = {name: getattr(os, name) for name in EFFECTS}

    def wrap(name):
        def effect(*args, **kwargs):
            if name == "open":
                flags = args[1] if len(args) > 1 else kwargs.get("flags", 0)
                if not flags & os.O_CREAT:
                    return real[name](*args, **kwargs)
            state["n"] += 1
            callback(state["n"], name)
            return real[name](*args, **kwargs)
        return effect

    return state, real, {name: wrap(name) for name in EFFECTS}


class AdmissionPublicationR06ETests(unittest.TestCase):
    def setUp(self):
        self._dirs = tempfile.TemporaryDirectory()
        self.base = Path(self._dirs.name)
        self.addCleanup(self._dirs.cleanup)
        self._counter = 0

    # -- helpers ---------------------------------------------------------
    def new_root(self):
        self._counter += 1
        return self.base / f"admissions-{self._counter}"

    def kw(self, number):
        return receipt(Scope(), number)

    def seed(self, root):
        store = AdmissionReceiptStore(root)
        try:
            store.put(**self.kw(1))
            store.put(**self.kw(2))
            pending = self.kw(3)
            store.reserve(**{key: value for key, value in pending.items()
                             if key not in {"run_id", "status", "replayed"}})
            return {"bytes": store.path.read_bytes(), "rows": store._read(),
                    "pending": store._read_pending()}
        finally:
            store.close()

    def row(self, old, number):
        value = dict(old["rows"][0])
        value.update(idempotency_key=f"key-{number}", request_fingerprint=f"{number:064x}",
                     run_id="run_" + f"{number:032x}")
        return value

    def child(self, root, *extra, script=CHILD_READ, timeout=6):
        proc = subprocess.run([sys.executable, "-B", "-W", "error::ResourceWarning", "-c", script,
                               str(root), *extra], env=dict(os.environ), capture_output=True, text=True,
                              timeout=timeout)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def crash_fixture(self, root):
        old = self.seed(root)
        proc = subprocess.run([sys.executable, "-B", "-W", "error::ResourceWarning", "-c", CHILD_CRASH, str(root)],
                              env=dict(os.environ), capture_output=True, text=True, timeout=20)
        self.assertEqual(proc.returncode, 77, proc.stderr)
        self.assertIn("CRASH_SEAM=before-replace", proc.stdout)
        return old

    def assert_old_and_writer(self, root, old):
        for _ in range(2):
            result = self.child(root)
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["rows"], old["rows"])
            self.assertEqual(result["pending"], old["pending"])
        writer = self.child(root, "4")
        self.assertTrue(writer["ok"], writer)
        self.assertEqual(writer["rows"], old["rows"] + [self.row(old, 4)])
        self.assertEqual(writer["pending"], old["pending"])

    # -- finding 2: genuine producer crash, normal hardlink topology -------
    def test_genuine_pre_replace_crash_topology_recovers_and_next_writer_binds(self):
        root = self.new_root()
        old = self.crash_fixture(root)
        parts = (root / ar._LOCK_NAME).read_bytes().rstrip(b" ").split(b"|")
        stage, guard = (root / parts[2].decode()), (root / parts[15].decode())
        commit = root / parts[3].decode()
        # The normal producer shares one inode between stage and commit, and
        # between the old final and the guard.  This is the positive control.
        self.assertEqual((stage.stat().st_nlink, commit.stat().st_nlink), (2, 2))
        self.assertEqual(stage.stat().st_ino, commit.stat().st_ino)
        self.assertEqual((guard.stat().st_nlink, (root / ar._FILE_NAME).stat().st_nlink), (2, 2))
        self.assert_old_and_writer(root, old)
        leftovers = [p.name for p in root.iterdir() if p.name.endswith((".tmp", ".commit", ".guard"))]
        self.assertEqual(leftovers, [])

    # -- finding 3: empty journal with an existing graph -------------------
    def test_empty_journal_with_existing_graph_is_refused_untouched(self):
        root = self.new_root()
        self.crash_fixture(root)
        (root / ar._LOCK_NAME).write_bytes(b"")
        before = snapshot(root)
        for _ in range(2):
            result = self.child(root)
            self.assertFalse(result["ok"], result)
            self.assertEqual(result["error"], "admission_storage_corrupt")
            self.assertEqual(snapshot(root), before)
        writer = self.child(root, "4")
        self.assertFalse(writer["ok"], writer)
        self.assertEqual(snapshot(root), before)

    def test_empty_journal_beside_clean_store_is_refused_untouched_and_fresh_root_still_bootstraps(self):
        root = self.new_root()
        self.seed(root)
        (root / ar._LOCK_NAME).write_bytes(b"")
        before = snapshot(root)
        for _ in range(2):
            result = self.child(root)
            self.assertEqual((result["ok"], result.get("error")), (False, "admission_storage_corrupt"))
            self.assertEqual(snapshot(root), before)
        (root / ar._LOCK_NAME).unlink()
        missing = snapshot(root)
        result = self.child(root)
        self.assertEqual((result["ok"], result.get("error")), (False, "admission_storage_corrupt"))
        self.assertEqual(snapshot(root), missing)
        fresh = self.new_root()
        result = self.child(fresh, "1")
        self.assertTrue(result["ok"], result)
        self.assertEqual(len(result["rows"]), 1)
        self.assertEqual((fresh / ar._LOCK_NAME).read_bytes(), ar._MARKER)

    # -- finding 1: acknowledgement boundary spans the whole call ----------
    def test_late_mutations_cannot_be_acknowledged_and_restore_every_old_artifact(self):
        for mode in ("journal-clear-fsync", "pending-unlink", "pending-dirsync"):
            with self.subTest(mode=mode):
                root = self.new_root()
                store = AdmissionReceiptStore(root)
                try:
                    store.put(**self.kw(1))
                    store.put(**self.kw(2))
                    pending = self.kw(3)
                    store.reserve(**{k: v for k, v in pending.items() if k not in {"run_id", "status", "replayed"}})
                    old = {"bytes": store.path.read_bytes(), "rows": store._read(), "pending": store._read_pending(),
                           "pending_bytes": (root / ar._PENDING_NAME).read_bytes()}
                    real_unlink, real_fsync = os.unlink, os.fsync
                    hooks, removed = [], []
                    lock_ino = (root / ar._LOCK_NAME).stat().st_ino

                    def unlink(name, *args, **kwargs):
                        if name == ar._PENDING_NAME:
                            removed.append(True)
                            if mode == "pending-unlink":
                                hooks.append(mode)
                                flip_final(root)
                        return real_unlink(name, *args, **kwargs)

                    def fsync(fd):
                        info = os.fstat(fd)
                        if not hooks:
                            if mode == "pending-dirsync" and removed and stat.S_ISDIR(info.st_mode):
                                hooks.append(mode)
                                flip_final(root)
                            if (mode == "journal-clear-fsync" and stat.S_ISREG(info.st_mode)
                                    and info.st_ino == lock_ino and os.pread(fd, 4097, 0) == ar._MARKER):
                                hooks.append(mode)
                                flip_final(root)
                        return real_fsync(fd)

                    with mock.patch.object(ar.os, "unlink", unlink), mock.patch.object(ar.os, "fsync", fsync):
                        with self.assertRaises(AdmissionReceiptError):
                            store.put(**pending)
                    self.assertEqual(hooks, [mode])
                    self.assertEqual(store.path.read_bytes(), old["bytes"])
                    self.assertEqual((root / ar._PENDING_NAME).read_bytes(), old["pending_bytes"])
                    self.assertEqual((store._read(), store._read_pending()), (old["rows"], old["pending"]))
                finally:
                    store.close()
                self.assert_old_and_writer(root, old)

    def test_successful_calls_return_only_authenticated_durable_state(self):
        root = self.new_root()
        store = AdmissionReceiptStore(root)
        try:
            for number in (1, 2, 3):
                store.put(**self.kw(number))
                result = self.child(root)
                self.assertTrue(result["ok"], result)
                self.assertEqual([r["idempotency_key"] for r in result["rows"]],
                                 [f"key-{i}" for i in range(1, number + 1)])
                self.assertEqual(store.path.read_bytes(), (root / ar._FILE_NAME).read_bytes())
            names = sorted(p.name for p in root.iterdir())
            transient = [n for n in names if n.endswith((".tmp", ".commit", ".guard"))]
            retained = [n for n in names if n.endswith(".backup")]
            self.assertEqual(transient, [])
            self.assertLessEqual(len(retained), 1)
            self.assertEqual((root / ar._LOCK_NAME).read_bytes(), ar._MARKER)
        finally:
            store.close()

    # -- finding 3/4: whole-graph validation before any mutation ----------
    def test_unknown_graphs_are_refused_before_any_mutation_with_positive_control(self):
        def tamper_copy(role):
            def run(root, parts):
                path = root / parts[role].decode()
                data = path.read_bytes()
                path.unlink()
                path.write_bytes(data)
                path.chmod(0o600)
            return run

        def alias(role):
            def run(root, parts):
                os.link(root / parts[role].decode(), root / "unrelated-alias")
            return run

        def unrelated(root, parts):
            forged = root / (".admission_receipts.json.enc." + "d" * 32 + ".tmp")
            forged.write_bytes(b"x")
            forged.chmod(0o600)

        def wrong_target(root, parts):
            raw = (root / ar._LOCK_NAME).read_bytes().replace(b"|admission_receipts.json.enc|",
                                                               b"|admission_pending.json.enc|", 1)
            (root / ar._LOCK_NAME).write_bytes(raw)

        def fifo_backup(root, parts):
            path = root / parts[9].decode()
            path.unlink()
            os.mkfifo(path, 0o600)

        cases = {
            "stage-wrong-inode-valid-backup": tamper_copy(2),
            "commit-alias": alias(3),
            "guard-wrong-inode": tamper_copy(15),
            "backup-wrong-inode": tamper_copy(9),
            "extra-backup-alias": alias(9),
            "unrelated-artifact": unrelated,
            "wrong-target-name": wrong_target,
            "backup-fifo": fifo_backup,
        }
        control_root = self.new_root()
        control_old = self.crash_fixture(control_root)
        self.assert_old_and_writer(control_root, control_old)
        for label, mutate in cases.items():
            with self.subTest(case=label):
                root = self.new_root()
                self.crash_fixture(root)
                parts = (root / ar._LOCK_NAME).read_bytes().rstrip(b" ").split(b"|")
                mutate(root, parts)
                before = snapshot(root)
                for _ in range(2):
                    result = self.child(root, timeout=6)
                    self.assertEqual((result["ok"], result.get("error")), (False, "admission_storage_corrupt"))
                    self.assertEqual(snapshot(root), before)
                writer = self.child(root, "4")
                self.assertFalse(writer["ok"], writer)
                self.assertEqual(snapshot(root), before)

    # -- real producer crash matrix ---------------------------------------
    def fork_crash(self, root, k, operation):
        pid = os.fork()
        if pid == 0:
            code = 3
            try:
                def callback(n, name):
                    if n == k:
                        os._exit(77)
                state, real, wrapped = counting(callback)
                holder = {}
                if operation == "construct":
                    for name, function in wrapped.items():
                        setattr(os, name, function)
                    holder["store"] = AdmissionReceiptStore(root)
                else:
                    holder["store"] = AdmissionReceiptStore(root)
                    for name, function in wrapped.items():
                        setattr(os, name, function)
                    if operation == "put":
                        holder["store"].put(**self.kw(3))
                    elif operation == "put-first":
                        holder["store"].put(**self.kw(1))
                    elif operation == "reserve":
                        pending = self.kw(3)
                        holder["store"].reserve(**{key: value for key, value in pending.items()
                                                   if key not in {"run_id", "status", "replayed"}})
                    else:
                        raise AssertionError(operation)
                code = 0
            except BaseException:
                code = 3
            finally:
                os._exit(code)
        _, status = os.waitpid(pid, 0)
        return os.waitstatus_to_exitcode(status)

    def sweep(self, operation, prepare, allowed, limit=400):
        crashes = 0
        for k in range(1, limit):
            root = self.new_root()
            old = prepare(root)
            code = self.fork_crash(root, k, operation)
            if code == 0:
                self.assertGreater(crashes, 5, "sweep ended before reaching real effects")
                return crashes
            self.assertEqual(code, 77, f"effect {k}: child failed unexpectedly")
            crashes += 1
            store = AdmissionReceiptStore(root)
            try:
                state = (store._read(), store._read_pending())
                self.assertIn(json.dumps(state, sort_keys=True), allowed(old),
                              f"effect {k}: recovered state is not an old/new state")
                store.put(**self.kw(4))
                final = [r["idempotency_key"] for r in store._read()]
                self.assertIn("key-4", final)
            finally:
                store.close()
            again = self.child(root) if k % 7 == 0 else {"ok": True}
            self.assertTrue(again["ok"], (k, again))
        self.fail("sweep did not terminate")

    @staticmethod
    def dump(rows, pending):
        return json.dumps((rows, pending), sort_keys=True)

    def test_every_producer_crash_cut_of_put_has_a_valid_recovery(self):
        def allowed(old):
            new = old["rows"] + [self.row(old, 3)]
            return {self.dump(old["rows"], old["pending"]), self.dump(new, old["pending"]), self.dump(new, [])}
        self.sweep("put", self.seed, allowed)

    def test_every_producer_crash_cut_of_pending_reserve_has_a_valid_recovery(self):
        def prepare(root):
            store = AdmissionReceiptStore(root)
            store.put(**self.kw(1))
            rows = store._read()
            store.close()
            return {"rows": rows, "pending": []}

        def allowed(old):
            pending = {k: v for k, v in self.kw(3).items() if k not in {"run_id", "status", "replayed"}}
            expected = dict(old["rows"][0])
            pending["scope"] = expected["scope"]
            return {self.dump(old["rows"], []), self.dump(old["rows"], [pending])}
        self.sweep("reserve", prepare, allowed)

    def test_every_producer_crash_cut_of_first_receipt_create_has_a_valid_recovery(self):
        def prepare(root):
            AdmissionReceiptStore(root).close()
            return {"rows": [], "pending": []}

        def allowed(old):
            probe = self.new_root()
            store = AdmissionReceiptStore(probe)
            store.put(**self.kw(1))
            rows = store._read()
            store.close()
            return {self.dump([], []), self.dump(rows, [])}
        self.sweep("put-first", prepare, allowed)

    def test_every_producer_crash_cut_of_key_bootstrap_has_a_valid_recovery(self):
        crashes = 0
        for k in range(1, 200):
            root = self.new_root()
            code = self.fork_crash(root, k, "construct")
            if code == 0:
                break
            self.assertEqual(code, 77, f"effect {k}")
            crashes += 1
            store = AdmissionReceiptStore(root)
            try:
                store.put(**self.kw(1))
                self.assertEqual(len(store._read()), 1)
            finally:
                store.close()
            self.assertTrue(self.child(root)["ok"])
        self.assertGreater(crashes, 5)

    # -- in-call mutation at every effect after the old state is protected -
    def test_final_mutation_at_every_protected_effect_is_rejected_with_exact_old_state_or_valid_new(self):
        probe_root = self.new_root()
        old = self.seed(probe_root)
        events = []
        state, real, wrapped = counting(lambda n, name: events.append(name))
        store = AdmissionReceiptStore(probe_root)
        try:
            with mock.patch.multiple(os, **wrapped):
                store.put(**self.kw(3))
        finally:
            store.close()
        total = state["n"]
        self.assertGreater(total, 20)
        guard_link = events.index("link") + 1
        rejected = accepted = 0
        for k in range(guard_link, total + 1):
            root = self.new_root()
            old = self.seed(root)
            fired = []

            def callback(n, name, k=k, root=root, fired=fired):
                if n == k:
                    fired.append(name)
                    flip = real_flip
                    flip(root)

            real_flip = flip_final
            state, real, wrapped = counting(callback)
            store = AdmissionReceiptStore(root)
            try:
                with mock.patch.multiple(os, **wrapped):
                    try:
                        store.put(**self.kw(3))
                        outcome = "accepted"
                    except AdmissionReceiptError:
                        outcome = "rejected"
                self.assertEqual(len(fired), 1, f"effect {k}")
                if outcome == "rejected":
                    rejected += 1
                    self.assertEqual(store.path.read_bytes(), old["bytes"], f"effect {k}")
                    self.assertEqual((store._read(), store._read_pending()), (old["rows"], old["pending"]),
                                     f"effect {k}")
                else:
                    accepted += 1
                    new = old["rows"] + [self.row(old, 3)]
                    self.assertEqual(store._read(), new, f"effect {k}")
            finally:
                store.close()
            if outcome == "rejected":
                self.assert_old_and_writer(root, old)
            else:
                result = self.child(root)
                self.assertTrue(result["ok"], (k, result))
                self.assertEqual(result["rows"], old["rows"] + [self.row(old, 3)])
        type(self).mutation_report = {"effects": total, "first_protected": guard_link,
                                      "rejected": rejected, "accepted": accepted}
        self.assertGreater(rejected, 5)
        self.assertEqual(accepted + rejected, total - guard_link + 1)


if __name__ == "__main__":
    unittest.main()
