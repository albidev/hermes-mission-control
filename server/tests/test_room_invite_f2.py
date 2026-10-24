"""F2 regressions: actual opened storage and non-blocking bounded admission."""
import asyncio
import json
import os
import sqlite3
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import invite_rooms.material_receipts as mr
from invite_rooms.native_rpc import BoundedNativeRpc
from room_lobby_service import REGISTER_ADMISSION
try:
    from server.tests import test_room_invite_material_receipts as M
    from server.tests.test_room_invite_admission import Harness, MemorySocket, RecordingRpc, INSTALL_A
except ModuleNotFoundError:
    import test_room_invite_material_receipts as M
    from test_room_invite_admission import Harness, MemorySocket, RecordingRpc, INSTALL_A


class ActualStorageTests(M.TmpCase):
    def test_sqlite_swap_open_restore_cannot_redirect_admit_or_load(self):
        for write in (False, True):
            with self.subTest(write=write):
                reg, store = M.new_registry(self.tmp / str(write))
                scope = M.make_scope(reg)
                if not write:
                    store.admit(scope, M.material_for(scope, M.GRANT_A))
                # Literal R2 hook: with per-file storage SQLite must never be reached.
                decoy = self.tmp / ('outside-' + str(write))
                if store.path.exists():
                    decoy.write_bytes(store.path.read_bytes())
                else:
                    decoy.write_bytes(b'untouched')
                decoy.chmod(0o600)
                before = decoy.read_bytes()
                saved = store.path.with_name('saved.sqlite')
                real = sqlite3.connect
                calls = []
                def opening(path, *a, **kw):
                    calls.append(1)
                    store.path.rename(saved)
                    store.path.symlink_to(decoy)
                    try:
                        return real(path, *a, **kw)
                    finally:
                        store.path.unlink()
                        saved.rename(store.path)
                with patch.object(sqlite3, 'connect', opening):
                    if write:
                        reg.admit_signed_material(scope, M.envelope_for(scope, M.GRANT_A))
                    else:
                        self.assertEqual(store.load(scope)['grant'], M.GRANT_A)
                self.assertEqual(decoy.read_bytes(), before, 'no write outside retained directory')
                self.assertEqual(calls, [], 'SQLite path-opening window must not exist')
                self.assertEqual(store.load(scope)['grant'], M.GRANT_A)
                with self.assertRaises(mr.MaterialReceiptError):
                    store.admit(scope, M.material_for(scope, M.GRANT_B))

    def test_key_and_receipt_open_nonblocking_then_clear_only_after_regular_validation(self):
        import fcntl
        import stat
        reg, store = M.new_registry(self.tmp / 'flags'); scope = M.make_scope(reg)
        store.admit(scope, M.material_for(scope, M.GRANT_A))
        real_open, real_read = os.open, os.read
        seen, reads = [], []
        def opening(name, flags, *a, **kw):
            if name in (mr.KEY_NAME, store.receipt_name(scope)):
                seen.append(str(name))
                self.assertTrue(flags & os.O_NONBLOCK)
                self.assertTrue(flags & os.O_NOFOLLOW)
            return real_open(name, flags, *a, **kw)
        def reading(fd, count):
            reads.append(1)
            self.assertTrue(stat.S_ISREG(os.fstat(fd).st_mode))
            self.assertFalse(fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_NONBLOCK)
            return real_read(fd, count)
        with patch.object(os, 'open', opening), patch.object(os, 'read', reading):
            reopened = mr.MaterialReceiptStore(store.root)
            self.assertEqual(reopened.load(scope)['grant'], M.GRANT_A)
        self.assertIn(mr.KEY_NAME, seen)
        self.assertIn(store.receipt_name(scope), seen)
        self.assertTrue(reads)

    def test_fifo_key_is_refused_without_blocking(self):
        import subprocess
        import sys
        store = mr.MaterialReceiptStore(self.tmp / 'fifo-key')
        store.key_path.unlink(); os.mkfifo(store.key_path, 0o600)
        program = ('import sys; from invite_rooms.material_receipts import MaterialReceiptStore, MaterialReceiptError; '
                   'exec("try:\\n MaterialReceiptStore(sys.argv[1])\\nexcept MaterialReceiptError as exc:\\n print(str(exc))")')
        env = dict(os.environ, PYTHONPATH=str(Path(mr.__file__).resolve().parents[1]))
        try:
            result = subprocess.run([sys.executable, '-B', '-c', program, str(store.root)],
                                    env=env, capture_output=True, text=True, timeout=2)
        except subprocess.TimeoutExpired:
            self.fail('opening FIFO key blocked instead of failing closed')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'private_storage_required')
        self.assertEqual(store.key_path.stat().st_mode & 0o777, 0o600)

    def test_hardlinked_key_is_refused_before_reading(self):
        store = mr.MaterialReceiptStore(self.tmp / 'hardlink-key')
        original = store.key_path.read_bytes()
        alias = self.tmp / 'external-key'; os.link(store.key_path, alias)
        with self.assertRaisesRegex(mr.MaterialReceiptError, '^private_storage_required$'):
            mr.MaterialReceiptStore(store.root)
        self.assertEqual(alias.read_bytes(), original)
        self.assertEqual(store.key_path.stat().st_nlink, 2)

    def test_empty_initialized_store_with_missing_key_never_regenerates(self):
        store = mr.MaterialReceiptStore(self.tmp / 'empty-ready')
        store.key_path.unlink()
        with patch.object(mr.AESGCM, 'generate_key', side_effect=AssertionError('must not regenerate')):
            with self.assertRaisesRegex(mr.MaterialReceiptError, '^receipt_key_missing$'):
                mr.MaterialReceiptStore(store.root)
        self.assertFalse(store.key_path.exists())

    def test_existing_sqlite_is_refused_without_reading_or_key_generation(self):
        root = self.tmp / 'legacy'
        root.mkdir(mode=0o700)
        (root / mr.DB_NAME).write_bytes(b'legacy database sentinel')
        with patch.object(mr.AESGCM, 'generate_key', side_effect=AssertionError('regeneration')):
            with self.assertRaisesRegex(mr.MaterialReceiptError, '^private_storage_required$'):
                mr.MaterialReceiptStore(root)
        self.assertFalse((root / mr.KEY_NAME).exists())


class AdmissionDeadlineTests(unittest.IsolatedAsyncioTestCase):
    async def test_contended_admission_is_responsive_and_never_emits_after_deadline(self):
        h = Harness(self)
        sock = MemorySocket(RecordingRpc(INSTALL_A)); sock.release.set()
        native = BoundedNativeRpc(sock, timeout=0.05, require_register_admission=True)
        held, release = threading.Event(), threading.Event()
        def writer():
            with h.reg._source.write_lock():
                held.set(); release.wait(0.35)
        thread = threading.Thread(target=writer); thread.start()
        self.assertTrue(held.wait(5))
        ticks = []
        async def watchdog():
            await asyncio.sleep(0.01); ticks.append(time.monotonic())
        before = time.monotonic()
        watch = asyncio.create_task(watchdog())
        token = REGISTER_ADMISSION.set(lambda: h.reg.check_admission('owner-b', 1, h.scope.binding))
        try:
            try:
                await native('groups.peer.register', dict(room_id='room-r2', member_id='peer-b',
                    target_url='http://127.0.0.1:19001/links/x', target_profile='default',
                    grant=M.GRANT_A, catalog=M.CATALOG))
            except Exception:
                pass
            await watch
            self.assertLess(ticks[0] - before, 0.15, 'event loop blocked by admission')
            self.assertIsNone(sock.sent, 'expired request emitted a register')
            self.assertFalse(native._poisoned, 'unsent timeout must not poison the transport')
        finally:
            REGISTER_ADMISSION.reset(token)
            release.set(); thread.join(5)


class DescriptorRaceTests(M.TmpCase):
    def test_directory_swap_open_restore_load_admit_and_bootstrap(self):
        for operation in ('load', 'admit', 'bootstrap'):
            with self.subTest(operation=operation):
                base = self.tmp / operation; base.mkdir(mode=0o700)
                reg, store = M.new_registry(base)
                scope = M.make_scope(reg)
                if operation == 'load':
                    store.admit(scope, M.material_for(scope, M.GRANT_A))
                root = store.root
                if operation == 'bootstrap':
                    store.close()
                    (root / mr.KEY_NAME).unlink()
                    (root / mr.LOCK_NAME).write_bytes(b'')  # fixture models interrupted, not completed bootstrap
                moved, outside = base / 'moved', base / 'outside'
                outside.mkdir(mode=0o700)
                real = os.open; calls = []
                def opening(name, flags, *a, **kw):
                    # Intercept the actual open, AFTER directory validation; restore before return.
                    wanted = (str(name).endswith('.receipt') if operation == 'load'
                              else '.tmp-' in str(name))
                    if wanted and kw.get('dir_fd') is not None:
                        calls.append(str(name)); root.rename(moved); root.symlink_to(outside)
                        try:
                            return real(name, flags, *a, **kw)
                        finally:
                            root.unlink(); moved.rename(root)
                    return real(name, flags, *a, **kw)
                with patch.object(os, 'open', opening):
                    if operation == 'bootstrap':
                        store = mr.MaterialReceiptStore(root)
                    elif operation == 'admit':
                        reg.admit_signed_material(scope, M.envelope_for(scope, M.GRANT_A))
                    else:
                        self.assertEqual(store.load(scope)['grant'], M.GRANT_A)
                self.assertTrue(calls, 'race window must actually be exercised')
                self.assertEqual(list(outside.iterdir()), [])
                reopened = mr.MaterialReceiptStore(root)
                if operation == 'bootstrap':
                    self.assertEqual(len(store.key_path.read_bytes()), 32)
                    store.admit(scope, M.material_for(scope, M.GRANT_A))
                self.assertEqual(reopened.load(scope)['grant'], M.GRANT_A)

    def test_final_file_swap_open_restore_refuses_load_and_admit(self):
        for operation in ('load', 'admit'):
            with self.subTest(operation=operation):
                reg, store = M.new_registry(self.tmp / operation)
                scope = M.make_scope(reg); store.admit(scope, M.material_for(scope, M.GRANT_A))
                path = M.receipt_path(store, scope)
                outside, saved = self.tmp / ('outside-' + operation), path.with_suffix('.saved')
                outside.write_bytes(path.read_bytes()); outside.chmod(0o600)
                before = outside.read_bytes(); real = os.open; calls = []
                def opening(name, flags, *a, **kw):
                    if name == path.name:
                        calls.append(1); path.rename(saved); path.symlink_to(outside)
                        try:
                            return real(name, flags, *a, **kw)
                        finally:
                            path.unlink(); saved.rename(path)
                    return real(name, flags, *a, **kw)
                with patch.object(os, 'open', opening):
                    with self.assertRaises(mr.MaterialReceiptError):
                        if operation == 'load': store.load(scope)
                        else: store.admit(scope, M.material_for(scope, M.GRANT_B))
                self.assertTrue(calls)
                self.assertEqual(outside.read_bytes(), before)
                self.assertEqual(store.load(scope)['grant'], M.GRANT_A)

    def test_key_file_swap_open_restore_during_bootstrap_never_reads_external_key(self):
        store = mr.MaterialReceiptStore(self.tmp / 'key-swap'); store.close()
        path = store.key_path; saved = path.with_suffix('.saved')
        outside = self.tmp / 'outside-key'; outside.write_bytes(path.read_bytes()); outside.chmod(0o600)
        before = outside.read_bytes(); real = os.open; calls = []
        def opening(name, flags, *a, **kw):
            if name == mr.KEY_NAME:
                calls.append(1); path.rename(saved); path.symlink_to(outside)
                try: return real(name, flags, *a, **kw)
                finally: path.unlink(); saved.rename(path)
            return real(name, flags, *a, **kw)
        with patch.object(os, 'open', opening):
            with self.assertRaises(mr.MaterialReceiptError): mr.MaterialReceiptStore(store.root)
        self.assertTrue(calls)
        self.assertEqual(outside.read_bytes(), before)
        mr.MaterialReceiptStore(store.root).close()

    def test_tmp_swap_before_actual_open_for_key_and_receipt_is_exclusive(self):
        for operation in ('admit', 'bootstrap'):
            with self.subTest(operation=operation):
                reg, store = M.new_registry(self.tmp / operation); scope = M.make_scope(reg)
                if operation == 'bootstrap':
                    store.close(); store.key_path.unlink(); (store.root / mr.LOCK_NAME).write_bytes(b'')
                outside = self.tmp / ('tmp-outside-' + operation)
                outside.write_bytes(b'unchanged-external'); outside.chmod(0o600)
                real = os.open; calls = []
                def opening(name, flags, *a, **kw):
                    if '.tmp-' in str(name):
                        calls.append(1)
                        os.symlink(outside, name, dir_fd=kw['dir_fd'])
                        try: return real(name, flags, *a, **kw)
                        finally: os.unlink(name, dir_fd=kw['dir_fd'])
                    return real(name, flags, *a, **kw)
                with patch.object(os, 'open', opening):
                    with self.assertRaises((mr.MaterialReceiptError, OSError)):
                        if operation == 'bootstrap': mr.MaterialReceiptStore(store.root)
                        else: store.admit(scope, M.material_for(scope, M.GRANT_A))
                self.assertTrue(calls)
                self.assertEqual(outside.read_bytes(), b'unchanged-external')
                self.assertEqual(list(store.root.glob('*.receipt')), [])

    def test_tmp_swap_link_restore_cannot_publish_an_unwritten_inode(self):
        for operation in ('admit', 'bootstrap'):
            for symlink in (True, False):
                with self.subTest(operation=operation, symlink=symlink):
                    reg, store = M.new_registry(self.tmp / (operation + str(symlink)))
                    scope = M.make_scope(reg)
                    if operation == 'bootstrap': store.close(); store.key_path.unlink(); (store.root / mr.LOCK_NAME).write_bytes(b'')
                    outside = self.tmp / ('link-outside-' + operation + str(symlink))
                    outside.write_bytes(b'unchanged-external'); outside.chmod(0o600)
                    real = os.link; calls = []
                    def linking(src, dst, **kw):
                        calls.append(1); dfd = kw['src_dir_fd']; saved = src + '.saved'
                        os.rename(src, saved, src_dir_fd=dfd, dst_dir_fd=dfd)
                        if symlink: os.symlink(outside, src, dir_fd=dfd)
                        else: real(outside, src, dst_dir_fd=dfd)
                        try: return real(src, dst, **kw)
                        finally:
                            os.unlink(src, dir_fd=dfd)
                            os.rename(saved, src, src_dir_fd=dfd, dst_dir_fd=dfd)
                    with patch.object(os, 'link', linking):
                        with self.assertRaises(mr.MaterialReceiptError):
                            if operation == 'bootstrap': mr.MaterialReceiptStore(store.root)
                            else: store.admit(scope, M.material_for(scope, M.GRANT_A))
                    self.assertTrue(calls)
                    self.assertEqual(outside.read_bytes(), b'unchanged-external')

    def test_hardlinked_receipts_and_keys_are_refused(self):
        reg, store = M.new_registry(self.tmp); scope = M.make_scope(reg)
        store.admit(scope, M.material_for(scope, M.GRANT_A))
        os.link(M.receipt_path(store, scope), self.tmp / 'receipt-alias')
        with self.assertRaisesRegex(mr.MaterialReceiptError, '^private_storage_required$'): store.load(scope)
        os.link(store.key_path, self.tmp / 'key-alias')
        with self.assertRaisesRegex(mr.MaterialReceiptError, '^private_storage_required$'): mr.MaterialReceiptStore(store.root)

    def test_receipt_files_are_private_hashed_regular_and_unsafe_types_are_refused(self):
        import re
        import stat
        for variant in ('mode', 'foreign', 'directory', 'fifo'):
            with self.subTest(variant=variant):
                reg, store = M.new_registry(self.tmp / variant); scope = M.make_scope(reg)
                store.admit(scope, M.material_for(scope, M.GRANT_A))
                path = M.receipt_path(store, scope); info = path.stat()
                self.assertRegex(path.name, r'^[0-9a-f]{64}\.receipt$')
                self.assertEqual(stat.S_IMODE(info.st_mode), 0o600)
                self.assertEqual(info.st_uid, os.getuid())
                self.assertEqual(info.st_nlink, 1)
                if variant == 'mode': path.chmod(0o644)
                elif variant == 'directory': path.unlink(); path.mkdir(mode=0o700)
                elif variant == 'fifo': path.unlink(); os.mkfifo(path, 0o600)
                real = os.fstat
                def foreign(fd):
                    result = real(fd)
                    if (result.st_dev, result.st_ino) == (info.st_dev, info.st_ino):
                        from types import SimpleNamespace
                        return SimpleNamespace(st_mode=result.st_mode, st_uid=os.getuid()+4242, st_nlink=1)
                    return result
                hook = patch.object(os, 'fstat', foreign) if variant == 'foreign' else patch.object(os, 'getuid', wraps=os.getuid)
                with hook:
                    with self.assertRaisesRegex(mr.MaterialReceiptError, '^private_storage_required$'): store.load(scope)

    def test_tmp_name_swap_during_actual_write_never_redirects_the_open_fd(self):
        for operation in ('admit', 'bootstrap'):
            with self.subTest(operation=operation):
                reg, store = M.new_registry(self.tmp / operation); scope = M.make_scope(reg)
                if operation == 'bootstrap': store.close(); store.key_path.unlink(); (store.root / mr.LOCK_NAME).write_bytes(b'')
                outside = self.tmp / ('write-outside-' + operation)
                outside.write_bytes(b'unchanged-external'); outside.chmod(0o600)
                real = os.write; calls = []
                def writing(fd, data):
                    path = next(store.root.glob('*.tmp-*'))
                    saved = path.with_name(path.name + '.saved')
                    calls.append(1); path.rename(saved); path.symlink_to(outside)
                    try: return real(fd, data)
                    finally: path.unlink(); saved.rename(path)
                with patch.object(os, 'write', writing):
                    if operation == 'bootstrap': store = mr.MaterialReceiptStore(store.root)
                    else: store.admit(scope, M.material_for(scope, M.GRANT_A))
                self.assertTrue(calls)
                self.assertEqual(outside.read_bytes(), b'unchanged-external')
                if operation == 'bootstrap': store.admit(scope, M.material_for(scope, M.GRANT_A))
                self.assertEqual(mr.MaterialReceiptStore(store.root).load(scope)['grant'], M.GRANT_A)

    def test_concurrent_bootstrap_and_first_writes_share_one_key_and_one_immutable_receipt(self):
        from concurrent.futures import ThreadPoolExecutor
        root = self.tmp / 'concurrent'
        with ThreadPoolExecutor(max_workers=8) as pool:
            stores = list(pool.map(lambda _: mr.MaterialReceiptStore(root), range(16)))
        reg, _ = M.new_registry(self.tmp / 'scope'); scope = M.make_scope(reg)
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda s: s.admit(scope, M.material_for(scope, M.GRANT_A)), stores))
        self.assertEqual(len(list(root.glob('*.receipt'))), 1)
        self.assertEqual(list(root.glob('*.tmp-*')), [])
        for store in stores:
            self.assertEqual(store.load(scope)['grant'], M.GRANT_A)
            with self.assertRaisesRegex(mr.MaterialReceiptError, '^material_scope_conflict$'):
                store.admit(scope, M.material_for(scope, M.GRANT_B))
            store.close()

    def test_receipt_faults_never_return_success_without_durable_publication(self):
        for phase in ('write', 'file-fsync', 'link', 'dir-fsync', 'zero-write'):
            with self.subTest(phase=phase):
                reg, store = M.new_registry(self.tmp / phase); scope = M.make_scope(reg)
                real_fsync = os.fsync
                def syncing(fd):
                    import stat
                    isdir = stat.S_ISDIR(os.fstat(fd).st_mode)
                    if isdir == (phase == 'dir-fsync'): raise OSError('injected-fsync')
                    return real_fsync(fd)
                if phase == 'write': hook = patch.object(os, 'write', side_effect=OSError('injected-write'))
                elif phase == 'zero-write': hook = patch.object(os, 'write', return_value=0)
                elif phase == 'link': hook = patch.object(os, 'link', side_effect=OSError('injected-link'))
                else: hook = patch.object(os, 'fsync', syncing)
                with hook:
                    with self.assertRaises(mr.MaterialReceiptError): store.admit(scope, M.material_for(scope, M.GRANT_A))
                self.assertEqual(list(store.root.glob('*.tmp-*')), [])
                # A dir-fsync failure may leave a complete final receipt, but never a partial one.
                store.admit(scope, M.material_for(scope, M.GRANT_A))
                self.assertEqual(mr.MaterialReceiptStore(store.root).load(scope)['grant'], M.GRANT_A)


class PostAwaitAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_change_after_worker_read_before_await_returns_is_rejected(self):
        h = Harness(self); sock = MemorySocket(RecordingRpc(INSTALL_A)); sock.release.set()
        native = BoundedNativeRpc(sock, require_register_admission=True)
        real_thread = asyncio.to_thread
        from test_room_invite_admission import bump_generation
        async def cutover_after_worker(fn, *a, **kw):
            result = await real_thread(fn, *a, **kw)
            h.rewrite_peers(bump_generation)
            return result
        token = REGISTER_ADMISSION.set(lambda: h.reg.check_admission('owner-b', 1, h.scope.binding))
        try:
            with patch.object(asyncio, 'to_thread', cutover_after_worker):
                with self.assertRaises(Exception):
                    await native('groups.peer.register', dict(room_id='room-r2', member_id='peer-b',
                        target_url='http://127.0.0.1:19001/links/x', target_profile='default', grant=M.GRANT_A, catalog=M.CATALOG))
            self.assertIsNone(sock.sent)
        finally: REGISTER_ADMISSION.reset(token)

    async def test_slow_registry_parse_does_not_block_the_loop_or_emit_late(self):
        h = Harness(self); sock = MemorySocket(RecordingRpc(INSTALL_A)); sock.release.set()
        native = BoundedNativeRpc(sock, timeout=0.05, require_register_admission=True)
        loader = h.reg._source._loader
        entered, release = threading.Event(), threading.Event()
        def slow(path):
            entered.set(); release.wait(0.25)
            return loader(path)
        token = REGISTER_ADMISSION.set(lambda: h.reg.check_admission('owner-b', 1, h.scope.binding))
        ticks = []
        async def ticker():
            await asyncio.sleep(0.01); ticks.append(time.monotonic())
        start = time.monotonic(); watch = asyncio.create_task(ticker())
        try:
            with patch.object(h.reg._source, '_loader', slow):
                with self.assertRaises(Exception):
                    await native('groups.peer.register', dict(room_id='room-r2', member_id='peer-b',
                        target_url='http://127.0.0.1:19001/links/x', target_profile='default', grant=M.GRANT_A, catalog=M.CATALOG))
                self.assertTrue(entered.is_set())
                await watch
                self.assertLess(ticks[0] - start, 0.15)
                self.assertIsNone(sock.sent)
        finally:
            REGISTER_ADMISSION.reset(token); release.set()
        # The abandoned worker has no send capability. Wait off-loop until it releases the read lock.
        await asyncio.to_thread(lambda: h.reg._source.replace(json.loads(h.peers.read_bytes())))
        self.assertIsNone(sock.sent)

    async def test_writer_contending_after_worker_await_fails_closed_without_waiting(self):
        h = Harness(self); sock = MemorySocket(RecordingRpc(INSTALL_A)); sock.release.set()
        native = BoundedNativeRpc(sock, require_register_admission=True)
        real_thread = asyncio.to_thread
        held, release = threading.Event(), threading.Event()
        def writer():
            with h.reg._source.write_lock(): held.set(); release.wait(1)
        thread = threading.Thread(target=writer)
        async def worker_then_writer(fn, *a, **kw):
            result = await real_thread(fn, *a, **kw)
            thread.start()
            self.assertTrue(held.wait(5))
            return result
        token = REGISTER_ADMISSION.set(lambda: h.reg.check_admission('owner-b', 1, h.scope.binding))
        before = time.monotonic()
        try:
            with patch.object(asyncio, 'to_thread', worker_then_writer):
                with self.assertRaises(Exception):
                    await native('groups.peer.register', dict(room_id='room-r2', member_id='peer-b',
                        target_url='http://127.0.0.1:19001/links/x', target_profile='default', grant=M.GRANT_A, catalog=M.CATALOG))
            self.assertLess(time.monotonic()-before, 0.3)
            self.assertIsNone(sock.sent)
        finally:
            REGISTER_ADMISSION.reset(token); release.set(); thread.join(5)

    async def test_expiry_after_final_check_and_before_send_is_rechecked(self):
        sock = MemorySocket(RecordingRpc(INSTALL_A)); sock.release.set()
        native = BoundedNativeRpc(sock, timeout=0.03, require_register_admission=True)
        def final(): time.sleep(0.05)
        token = REGISTER_ADMISSION.set(lambda: final)
        try:
            with self.assertRaises(Exception):
                await native('groups.peer.register', dict(room_id='room-r2', member_id='peer-b',
                    target_url='http://127.0.0.1:19001/links/x', target_profile='default', grant=M.GRANT_A, catalog=M.CATALOG))
            self.assertIsNone(sock.sent)
        finally: REGISTER_ADMISSION.reset(token)


if __name__ == '__main__':
    unittest.main()
