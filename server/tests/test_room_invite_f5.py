"""F5: exact recovery namespace, future mtimes and publication-name identity."""
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import invite_rooms.material_receipts as mr
try:
    from server.tests import test_room_invite_material_receipts as M
except ModuleNotFoundError:
    import test_room_invite_material_receipts as M


# Exit at the real unlink following a successful publication link, not a fabricated
# two-link fixture. Process exit releases the bootstrap/operation flock as a crash would.
_CRASH_AT_UNLINK = """
import os
import sys
from unittest.mock import patch
import invite_rooms.material_receipts as mr
import test_room_invite_material_receipts as M

root, operation = sys.argv[1:]
real_unlink = os.unlink

def crash_at_unlink(name, *args, **kwargs):
    if '.tmp-' in str(name):
        os._exit(73)
    return real_unlink(name, *args, **kwargs)

if operation == 'key':
    with patch.object(mr.os, 'unlink', crash_at_unlink):
        mr.MaterialReceiptStore(root)
else:
    reg, store = M.new_registry(root)
    try:
        scope = M.make_scope(reg)
        with patch.object(mr.os, 'unlink', crash_at_unlink):
            store.admit(scope, M.material_for(scope, M.GRANT_A))
    finally:
        store.close()
"""


class RecoveryNamespaceTests(M.TmpCase):
    def open_store(self, root):
        store = mr.MaterialReceiptStore(root)
        self.addCleanup(store.close)
        return store

    def stage(self, root, name, *, mtime):
        path = root / name
        path.write_bytes(b'preserve-stage')
        path.chmod(0o600)
        os.utime(path, (mtime, mtime))
        return path

    def test_non_producer_names_are_preserved_without_open_or_unlink(self):
        root = self.tmp / 'receipts'
        self.open_store(root).close()
        token = 'b' * 32
        receipt = 'a' * 64 + '.receipt'
        names = set()
        for base in (mr.KEY_NAME, receipt):
            for suffix in ('', 'b' * 31, 'b' * 33, 'g' * 32, 'B' * 32,
                           'OWNER_BACKUP', token + '.tmp-' + token, token + '\n'):
                names.add(base + '.tmp-' + suffix)
        for base in ('', 'x.receipt', 'a' * 63 + '.receipt', 'a' * 65 + '.receipt',
                     'A' * 64 + '.receipt', 'g' * 64 + '.receipt',
                     receipt + '.receipt', receipt + '.other', mr.KEY_NAME + '.other'):
            names.add(base + '.tmp-' + token)
        for name in names:
            self.stage(root, name, mtime=1)
        opened, unlinked = [], []
        real_open, real_unlink = os.open, os.unlink

        def opening(name, *args, **kwargs):
            opened.append(str(name))
            return real_open(name, *args, **kwargs)

        def unlinking(name, *args, **kwargs):
            unlinked.append(str(name))
            return real_unlink(name, *args, **kwargs)

        with patch.object(mr.os, 'open', opening), patch.object(mr.os, 'unlink', unlinking):
            self.open_store(root)
        # Check preservation first: the old broad parser fails by assertion here.
        for name in sorted(names):
            with self.subTest(name=name):
                self.assertTrue((root / name).exists(), 'non-generated name must not be swept')
                self.assertEqual((root / name).read_bytes(), b'preserve-stage')
        self.assertFalse(names.intersection(opened), 'outside-grammar entries must not be opened')
        self.assertFalse(names.intersection(unlinked), 'outside-grammar entries must not be unlinked')

    def test_valid_key_and_hashed_receipt_orphans_are_recovered(self):
        root = self.tmp / 'receipts'
        self.open_store(root).close()
        old = time.time() - mr.STAGING_MAX_AGE - 60
        paths = [self.stage(root, base + '.tmp-' + 'b' * 32, mtime=old)
                 for base in (mr.KEY_NAME, 'a' * 64 + '.receipt')]
        self.open_store(root)
        for path in paths:
            self.assertFalse(path.exists(), 'generated old orphan must still be recovered')

    def test_future_mtime_is_not_an_old_orphan(self):
        root = self.tmp / 'receipts'
        self.open_store(root).close()
        future = time.time() + mr.STAGING_MAX_AGE + 3600
        paths = [self.stage(root, base + '.tmp-' + 'b' * 32, mtime=future)
                 for base in (mr.KEY_NAME, 'a' * 64 + '.receipt')]
        self.open_store(root)
        for path in paths:
            self.assertTrue(path.exists(), 'future mtime is not an old orphan')
            self.assertEqual(path.read_bytes(), b'preserve-stage')

    def test_real_crash_after_link_recovers_key_and_receipt(self):
        server = Path(mr.__file__).resolve().parents[1]
        # Extend, never replace: CI puts the pinned Hermes core on PYTHONPATH and
        # the child needs it for `gateway` imports.
        env = dict(os.environ)
        env['PYTHONPATH'] = os.pathsep.join(
            part for part in (str(server), str(server / 'tests'), env.get('PYTHONPATH')) if part
        )
        for operation in ('key', 'receipt'):
            with self.subTest(operation=operation):
                base = self.tmp / operation
                root = base if operation == 'key' else base / 'receipts'
                result = subprocess.run([sys.executable, '-B', '-c', _CRASH_AT_UNLINK,
                                         str(base), operation], env=env, capture_output=True,
                                        text=True, timeout=10)
                self.assertEqual(result.returncode, 73, result.stdout + result.stderr)
                stages = list(root.glob('*.tmp-*'))
                self.assertEqual(len(stages), 1, 'child must crash at the actual publication seam')
                stage = stages[0]
                self.assertRegex(stage.name, r'^(?:material_receipts\.key|[0-9a-f]{64}\.receipt)\.tmp-[0-9a-f]{32}$')
                final = root / stage.name.partition('.tmp-')[0]
                self.assertEqual(stage.stat().st_ino, final.stat().st_ino)
                self.assertEqual(final.stat().st_nlink, 2)
                store = self.open_store(root)
                self.assertFalse(stage.exists())
                self.assertEqual(final.stat().st_nlink, 1)
                self.assertEqual(len(store.key_path.read_bytes()), 32)
                # A recovered key can publish/decrypt; a recovered receipt retains its grant.
                reg, check = M.new_registry(base if operation == 'receipt' else self.tmp / 'check-key')
                self.addCleanup(check.close)
                scope = M.make_scope(reg)
                if operation == 'key':
                    store.admit(scope, M.material_for(scope, M.GRANT_A))
                self.assertEqual(store.load(scope)['grant'], M.GRANT_A)


class PublicationIdentityTests(M.TmpCase):
    def test_final_name_swapped_after_link_is_refused(self):
        for operation in ('key', 'receipt'):
            with self.subTest(operation=operation):
                base = self.tmp / operation
                reg, store = M.new_registry(base)
                self.addCleanup(store.close)
                scope = M.make_scope(reg)
                if operation == 'key':
                    store.close()
                    store.key_path.unlink()
                    (store.root / mr.LOCK_NAME).write_bytes(b'')  # interrupted bootstrap fixture
                calls = []
                real_link = os.link

                def replace_final(src, dst, **kwargs):
                    real_link(src, dst, **kwargs)
                    calls.append(dst)
                    dirfd = kwargs['dst_dir_fd']
                    os.rename(dst, dst + '.moved', src_dir_fd=dirfd, dst_dir_fd=dirfd)
                    fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=dirfd)
                    self.addCleanup(os.close, fd)
                    os.write(fd, b'foreign-final')

                with patch.object(mr.os, 'link', replace_final):
                    with self.assertRaisesRegex(mr.MaterialReceiptError, '^private_storage_required$'):
                        if operation == 'key':
                            reopened = mr.MaterialReceiptStore(store.root)
                            self.addCleanup(reopened.close)
                        else:
                            store.admit(scope, M.material_for(scope, M.GRANT_A))
                expected = mr.KEY_NAME if operation == 'key' else store.receipt_name(scope)
                self.assertEqual(calls, [expected], 'the post-link swap must actually run')
                self.assertEqual((store.root / expected).read_bytes(), b'foreign-final')
                self.assertEqual((store.root / (expected + '.moved')).stat().st_nlink, 1)
                self.assertEqual(list(store.root.glob('*.tmp-*')), [])
