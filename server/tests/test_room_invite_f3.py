"""F3 regressions (review R3): staging/nlink recovery, receipt IO off the loop, tested deadline, pre-send expiry."""
import asyncio
import fcntl
import os
import stat
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import invite_rooms.material_receipts as mr
from invite_rooms.native_rpc import BoundedNativeRpc
from invite_rooms.peer_registry import ADMISSION_DEADLINE, PeerRegistryError
from room_lobby_service import REGISTER_ADMISSION, AdmissionDenied
try:
    from server.tests import test_room_invite_material_receipts as M
    from server.tests.test_room_invite_admission import Harness, MemorySocket, RecordingRpc, INSTALL_A
except ModuleNotFoundError:
    import test_room_invite_material_receipts as M
    from test_room_invite_admission import Harness, MemorySocket, RecordingRpc, INSTALL_A

REGISTER = dict(room_id='room-r2', member_id='peer-b', target_url='http://127.0.0.1:19001/links/x',
                target_profile='default', grant=M.GRANT_A, catalog=M.CATALOG)


def _names(root):
    return sorted(p.name for p in Path(root).iterdir())


def _stage(root, name, data=b'x', mode=0o600, age=None):
    path = Path(root) / (name + '.tmp-' + os.urandom(16).hex())
    path.write_bytes(data)
    path.chmod(mode)
    if age is not None:
        old = time.time() - age
        os.utime(path, (old, old))
    return path


class StagingRecoveryTests(M.TmpCase):
    def test_crash_between_link_and_unlink_of_the_key_is_recovered_on_next_open(self):
        root = self.tmp / 'receipts'
        store = MR_open(root)
        store.close()
        key = root / mr.KEY_NAME
        crash = root / (mr.KEY_NAME + '.tmp-' + os.urandom(16).hex())
        os.link(key, crash)  # exact post-crash state: staging and final name share the inode, nlink == 2
        self.assertEqual(key.stat().st_nlink, 2)
        reopened = MR_open(root)
        self.assertFalse(crash.exists())
        self.assertEqual(key.stat().st_nlink, 1)
        reg = M.PeerRegistry(receipt_store=reopened, route_provider=lambda _s: 19001)
        reg.register(peer_id=M.PEER_ID, installation_id=M.INSTALL_ID, profile='default', fingerprint=M.FINGERPRINT,
                     endpoint_id='endpoint-b', catalog=M.copy.deepcopy(M.CATALOG), consent_public_key=M.CONSENT_PUBLIC,
                     tls_ca_pem=M.CA_PEM, pairing_generation=1)
        scope = M.make_scope(reg)
        reopened.admit(scope, M.material_for(scope, M.GRANT_A))
        self.assertEqual(reopened.load(scope)['grant'], M.GRANT_A)

    def test_crash_between_link_and_unlink_of_a_receipt_is_recovered_on_next_open(self):
        reg, store = M.new_registry(self.tmp)
        scope = M.make_scope(reg)
        store.admit(scope, M.material_for(scope, M.GRANT_A))
        final = store.root / store.receipt_name(scope)
        crash = store.root / (final.name + '.tmp-' + os.urandom(16).hex())
        os.link(final, crash)
        with self.assertRaisesRegex(mr.MaterialReceiptError, '^private_storage_required$'):
            store.load(scope)  # refused until recovered (nlink rule unchanged)
        store.close()
        reopened = MR_open(store.root)
        self.assertFalse(crash.exists())
        self.assertEqual(reopened.load(scope)['grant'], M.GRANT_A)
        self.assertEqual(final.stat().st_nlink, 1)

    def test_pre_link_orphan_is_swept_only_when_older_than_the_bound(self):
        reg, store = M.new_registry(self.tmp)
        store.close()
        young = _stage(store.root, mr.KEY_NAME, age=5)
        old = _stage(store.root, 'a' * 64 + '.receipt', age=getattr(mr, "STAGING_MAX_AGE", 300.0) + 60)
        MR_open(store.root)
        self.assertTrue(young.exists(), 'a fresh staging file may belong to a live writer')
        self.assertFalse(old.exists(), 'orphan older than the bound must be swept')

    def test_external_hardlink_is_still_refused_for_receipt_and_key(self):
        reg, store = M.new_registry(self.tmp)
        scope = M.make_scope(reg)
        store.admit(scope, M.material_for(scope, M.GRANT_A))
        final = store.root / store.receipt_name(scope)
        foreign = self.tmp / 'foreign-link'
        os.link(final, foreign)
        store.close()
        reopened = MR_open(store.root)
        with self.assertRaisesRegex(mr.MaterialReceiptError, '^private_storage_required$'):
            reopened.load(scope)
        self.assertTrue(foreign.exists() and final.exists(), 'foreign link is never "recovered"')
        reopened.close()
        os.unlink(foreign)
        os.link(store.root / mr.KEY_NAME, foreign)
        with self.assertRaisesRegex(mr.MaterialReceiptError, '^private_storage_required$'):
            MR_open(store.root)
        self.assertTrue(foreign.exists())

    def test_symlink_and_fifo_staging_are_neither_followed_nor_opened(self):
        reg, store = M.new_registry(self.tmp)
        scope = M.make_scope(reg)
        store.admit(scope, M.material_for(scope, M.GRANT_A))
        store.close()
        outside = self.tmp / 'outside'
        outside.write_bytes(b'untouched'); outside.chmod(0o600)
        old = time.time() - getattr(mr, "STAGING_MAX_AGE", 300.0) - 60
        os.utime(outside, (old, old))
        link = store.root / (mr.KEY_NAME + '.tmp-' + 'a' * 32)
        link.symlink_to(outside)
        fifo = store.root / ('b' * 64 + '.receipt.tmp-' + 'c' * 32)
        os.mkfifo(fifo, 0o600)
        opened = []
        real_open = os.open
        def spy(path, *a, **kw):
            opened.append(str(path))
            return real_open(path, *a, **kw)
        with patch.object(mr.os, 'open', spy):
            MR_open(store.root)
        self.assertTrue(link.is_symlink() and stat.S_ISFIFO(fifo.lstat().st_mode), 'left untouched')
        self.assertEqual(outside.read_bytes(), b'untouched')
        self.assertFalse([n for n in opened if '.tmp-' in n], 'staging symlink/FIFO must not be opened')


def MR_open(root):
    return mr.MaterialReceiptStore(root)


class _LockHolder:
    """Holds the receipt operation flock from a separate fd/thread for ``seconds``."""
    def __init__(self, root, seconds):
        self.fd = os.open(Path(root) / mr.LOCK_NAME, os.O_RDWR)
        self.held, self.seconds = threading.Event(), seconds
        self.thread = threading.Thread(target=self._run)

    def _run(self):
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        self.held.set()
        time.sleep(self.seconds)
        fcntl.flock(self.fd, fcntl.LOCK_UN)

    def __enter__(self):
        self.thread.start()
        assert self.held.wait(5)
        return self

    def __exit__(self, *exc):
        self.thread.join(5)
        os.close(self.fd)


class ReceiptOffLoopTests(M.TmpCase, unittest.IsolatedAsyncioTestCase):
    async def _setup(self):
        reg, store = M.new_registry(self.tmp, route_provider=lambda _s: 19001)
        scope = M.make_scope(reg)
        store.admit(scope, M.material_for(scope, M.GRANT_A))
        return reg, store, scope

    async def test_receipt_lock_wait_never_stalls_the_event_loop(self):
        reg, store, scope = await self._setup()
        gaps, stop, last_tick = [], False, [time.monotonic()]
        async def heartbeat():
            last = time.monotonic()
            while not stop:
                await asyncio.sleep(0.005)
                now = time.monotonic(); gaps.append(now - last); last = now; last_tick[0] = now
        beat = asyncio.create_task(heartbeat())
        try:
            with _LockHolder(store.root, 0.4):
                material = await reg.resolve_scoped_material(scope)
        finally:
            stop = True; await beat; gaps.append(time.monotonic() - last_tick[0])
        self.assertEqual(material.grant, M.GRANT_A)
        self.assertLess(max(gaps), 0.08, 'event loop stalled by receipt IO')

    async def test_request_deadline_bounds_the_receipt_lock_wait(self):
        reg, store, scope = await self._setup()
        with _LockHolder(store.root, 0.8):
            token = ADMISSION_DEADLINE.set(time.monotonic() + 0.1)
            start = time.monotonic()
            try:
                with self.assertRaises(PeerRegistryError) as cm:
                    await reg.resolve_scoped_material(scope)
            finally:
                ADMISSION_DEADLINE.reset(token)
            self.assertLess(time.monotonic() - start, 0.4)
            self.assertEqual(str(cm.exception), 'material_unavailable')

    async def test_receipt_load_runs_on_the_dedicated_named_admission_executor(self):
        reg, store, scope = await self._setup()
        names, real = [], store.load
        def spy(*a, **kw):
            names.append(threading.current_thread().name)
            return real(*a, **kw)
        with patch.object(store, 'load', spy):
            await reg.resolve_scoped_material(scope)
        self.assertEqual(len(names), 1)
        self.assertTrue(names[0].startswith('invite-admission'), names)
        self.assertNotEqual(names[0], threading.main_thread().name)


class AdmissionDeadlineAndExpiryTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_lock_wait_is_capped_by_the_request_deadline(self):
        # Pins ADMISSION_DEADLINE.set(deadline): with set(None) the worker waits for the writer.
        h = Harness(self)
        sock = MemorySocket(RecordingRpc(INSTALL_A)); sock.release.set()
        native = BoundedNativeRpc(sock, timeout=0.06, require_register_admission=True)
        held, release = threading.Event(), threading.Event()
        def writer():
            with h.reg._source.write_lock():
                held.set(); release.wait(0.6)
        thread = threading.Thread(target=writer); thread.start()
        self.assertTrue(held.wait(5))
        ended, done = {}, threading.Event()
        def admission():
            try:
                return h.reg.check_admission('owner-b', 1, h.scope.binding)
            finally:
                ended['t'] = time.monotonic(); done.set()
        token = REGISTER_ADMISSION.set(admission)
        start = time.monotonic()
        try:
            with self.assertRaises(Exception):
                await native('groups.peer.register', dict(REGISTER))
            self.assertTrue(await asyncio.to_thread(done.wait, 3))
            self.assertLess(ended['t'] - start, 0.3, 'worker waited past the request deadline')
            self.assertIsNone(sock.sent)
        finally:
            REGISTER_ADMISSION.reset(token); release.set(); thread.join(5)

    async def test_expiry_before_send_is_admission_denied_not_ambiguous_and_does_not_poison(self):
        sock = MemorySocket(RecordingRpc(INSTALL_A)); sock.release.set()
        native = BoundedNativeRpc(sock, timeout=0.03, require_register_admission=True)
        def final(): time.sleep(0.05)
        token = REGISTER_ADMISSION.set(lambda: final)
        try:
            with self.assertRaises(AdmissionDenied):
                await native('groups.peer.register', dict(REGISTER))
            self.assertIsNone(sock.sent)
            self.assertFalse(native._poisoned)
        finally:
            REGISTER_ADMISSION.reset(token)

    async def test_deadline_firing_during_admission_wait_is_denied_not_ambiguous(self):
        sock = MemorySocket(RecordingRpc(INSTALL_A)); sock.release.set()
        native = BoundedNativeRpc(sock, timeout=0.03, require_register_admission=True)
        gate = threading.Event()
        def admit():
            gate.wait(0.2)
            return None
        token = REGISTER_ADMISSION.set(admit)
        try:
            with self.assertRaises(AdmissionDenied):
                await native('groups.peer.register', dict(REGISTER))
            self.assertIsNone(sock.sent)
            self.assertFalse(native._poisoned)
        finally:
            gate.set(); REGISTER_ADMISSION.reset(token)

    async def test_admission_callback_runs_on_the_dedicated_named_executor(self):
        sock = MemorySocket(RecordingRpc(INSTALL_A)); sock.release.set()
        native = BoundedNativeRpc(sock, require_register_admission=True)
        names = []
        def admit():
            names.append(threading.current_thread().name)
            return None
        token = REGISTER_ADMISSION.set(admit)
        try:
            try:
                await native('groups.peer.register', dict(REGISTER))
            except Exception:
                pass
        finally:
            REGISTER_ADMISSION.reset(token)
        self.assertEqual(len(names), 1)
        self.assertTrue(names[0].startswith('invite-admission'), names)


if __name__ == '__main__':
    unittest.main()
