"""F4 storage probes. All hostile objects and decoys are disposable, never live state."""
from __future__ import annotations

import multiprocessing
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from invite_rooms.joining_store import JoiningStore, JoiningStoreError
from invite_rooms.joining_material import MintJournal, MaterialError
from room_invite_store import RoomInviteStore

CASES = ((JoiningStore, JoiningStoreError, 'joining.sqlite', 'joining-secret.key'),
         (MintJournal, MaterialError, 'material.sqlite', 'material.key'))
CODES = {'private_storage_required', 'schema_version_unsupported', 'material_unavailable'}


def read(store):
    return store.list_imports() if isinstance(store, JoiningStore) else store.state({'probe': 1})


def write(store):
    if isinstance(store, JoiningStore):
        return store.create_import(lobby_id='lobby-probe', invite_id='invite-probe',
                                   secret='s' * 43, host_peer_id='host-probe')
    return store.begin({'probe': 1})


def close(store):
    if hasattr(store, 'close'):
        store.close()


def hostile_probe(root, cls, name, kind, phase, send):
    """Child process permits killing a FIFO open rather than hanging the suite."""
    store = cls(root)
    target = root / name
    if target.exists() or target.is_symlink():
        target.unlink()
    outside = root.parent / 'outside'
    outside.write_bytes(b'x' * 32)
    outside.chmod(0o600)
    if kind == 'fifo':
        os.mkfifo(target, 0o600)
    elif kind == 'symlink':
        target.symlink_to(outside)
    elif kind == 'dangling':
        target.symlink_to(root.parent / 'absent')
    elif kind == 'hardlink':
        os.link(outside, target)
    elif kind == 'public':
        target.write_bytes(b'x' * 32)
        target.chmod(0o644)
    elif kind == 'directory':
        target.mkdir(mode=0o700)
    before = outside.read_bytes()
    try:
        if phase == 'reopen':
            other = cls(root)
            close(other)
        else:
            read(store)
        send.send(('accepted', outside.read_bytes() == before))
    except Exception as exc:
        send.send((type(exc).__name__, str(exc), outside.read_bytes() == before))
    finally:
        close(store)
        send.close()


class StorageHardening(unittest.TestCase):
    def refusal(self, call, error):
        with self.assertRaises(error) as caught:
            call()
        self.assertIn(str(caught.exception), CODES)
        return str(caught.exception)

    def test_directory_swap_hammer_240_operations_per_store(self):
        for cls, error, dbname, key in CASES:
            with self.subTest(store=cls.__name__), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp) / 'store'
                store = cls(root)
                decoy = Path(tmp) / 'decoy'
                other = cls(decoy)
                write(other)
                close(other)
                before = {p.name: p.read_bytes() for p in decoy.iterdir() if p.is_file()}
                for i in range(240):
                    parked = Path(tmp) / 'parked'
                    root.rename(parked)
                    if i % 2:
                        root.symlink_to(decoy, target_is_directory=True)
                    else:
                        shutil.copytree(decoy, root)
                    try:
                        self.refusal(lambda: read(store) if i % 3 else write(store), error)
                    finally:
                        if root.is_symlink():
                            root.unlink()
                        else:
                            shutil.rmtree(root)
                        parked.rename(root)
                self.assertEqual(before, {p.name: p.read_bytes() for p in decoy.iterdir() if p.is_file()})
                self.assertEqual(read(store), [] if cls is JoiningStore else None)
                close(store)

    def test_swap_during_connect_refused_before_any_sql(self):
        for cls, error, dbname, key in CASES:
            for swap in ('root', 'db'):
                with self.subTest(store=cls.__name__, swap=swap), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp) / 'store'
                    store = cls(root)
                    decoy = Path(tmp) / 'decoy'
                    other = cls(decoy)
                    write(other)
                    close(other)
                    real = sqlite3.connect
                    statements = []
                    conns = []
                    parked = Path(tmp) / 'parked'

                    def swapped(*args, **kwargs):
                        if swap == 'root':
                            root.rename(parked)
                            root.symlink_to(decoy, target_is_directory=True)
                        else:
                            (root / dbname).rename(parked)
                            shutil.copyfile(decoy / dbname, root / dbname)
                            (root / dbname).chmod(0o600)
                        conn = real(*args, **kwargs)
                        conn.set_trace_callback(statements.append)
                        conns.append(conn)
                        return conn

                    try:
                        with patch('sqlite3.connect', side_effect=swapped):
                            self.assertEqual(self.refusal(lambda: read(store), error), 'private_storage_required')
                        self.assertEqual(statements, [], 'no SQL, not even pragmas, before post-connect check')
                        for conn in conns:
                            with self.assertRaises(sqlite3.ProgrammingError):
                                conn.execute('SELECT 1')
                    finally:
                        if swap == 'root' and root.is_symlink():
                            root.unlink()
                            parked.rename(root)
                        close(store)

    def test_parent_symlink_refused(self):
        for cls, error, dbname, key in CASES:
            with self.subTest(store=cls.__name__), tempfile.TemporaryDirectory() as tmp:
                real = Path(tmp) / 'real'
                real.mkdir(mode=0o700)
                link = Path(tmp) / 'link'
                link.symlink_to(real, target_is_directory=True)
                self.refusal(lambda: cls(link / 'store'), error)
                self.assertEqual(list(real.iterdir()), [])

    def test_foreign_and_legacy_db_never_modified(self):
        for cls, error, dbname, key in CASES:
            for kind in ('garbage', 'empty', 'foreign', 'legacy'):
                with self.subTest(store=cls.__name__, kind=kind), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp) / 'store'
                    store = cls(root)
                    close(store)
                    db = root / dbname
                    db.unlink()
                    if kind in ('foreign', 'legacy'):
                        conn = sqlite3.connect(db)
                        try:
                            conn.execute('CREATE TABLE foreign_table(value TEXT)')
                            if kind == 'legacy':
                                conn.execute('CREATE TABLE schema_version(version INTEGER)')
                                conn.execute('INSERT INTO schema_version VALUES(0)')
                            conn.commit()
                        finally:
                            conn.close()
                    else:
                        db.write_bytes(b'not a database' if kind == 'garbage' else b'')
                    db.chmod(0o600)
                    before = db.read_bytes()
                    self.refusal(lambda: cls(root), error)
                    self.assertEqual(db.read_bytes(), before)

    def test_key_loss_never_regenerates(self):
        for cls, error, dbname, key in CASES:
            with self.subTest(store=cls.__name__), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp) / 'store'
                store = cls(root)
                close(store)
                (root / key).unlink()
                self.refusal(lambda: cls(root), error)
                self.assertFalse((root / key).exists())

    def test_connect_os_and_sqlite_errors_sanitized(self):
        for cls, error, dbname, key in CASES:
            with self.subTest(store=cls.__name__), tempfile.TemporaryDirectory() as tmp:
                store = cls(Path(tmp) / 'store')
                for exc in (OSError('secret /private/path'), sqlite3.OperationalError('secret /private/path')):
                    with patch('sqlite3.connect', side_effect=exc):
                        self.refusal(lambda: read(store), error)
                close(store)


def make_hostile_test(case, suffix, kind, phase):
    cls, error, dbname, key = case
    name = key if suffix == 'key' else ('initialize.lock' if suffix == 'lock' else dbname + suffix)

    def test(self):
        with tempfile.TemporaryDirectory() as tmp:
            recv, send = multiprocessing.get_context('fork').Pipe(duplex=False)
            proc = multiprocessing.get_context('fork').Process(
                target=hostile_probe, args=(Path(tmp) / 'store', cls, name, kind, phase, send))
            proc.start()
            send.close()
            try:
                proc.join(3)
                self.assertFalse(proc.is_alive(), 'unsafe object blocked beyond 3 seconds')
                self.assertEqual(proc.exitcode, 0)
                self.assertTrue(recv.poll(), 'child must report outcome')
                result = recv.recv()
                self.assertEqual(result, (error.__name__, 'private_storage_required', True))
            finally:
                if proc.is_alive():
                    proc.kill()
                    proc.join()
                proc.close()
                recv.close()
    return test


for case in CASES:
    for suffix in ('', '-wal', '-shm', '-journal', 'key', 'lock'):
        for kind in ('fifo', 'symlink', 'hardlink', 'public', 'dangling', 'directory'):
            for phase in ('reopen', 'live'):
                setattr(StorageHardening, f'test_{case[0].__name__}_{suffix or "db"}_{kind}_{phase}',
                        make_hostile_test(case, suffix, kind, phase))


class WalInit(unittest.TestCase):
    def run_fault(self, errors):
        real = sqlite3.connect
        calls = []

        class Proxy:
            def __init__(self, conn):
                object.__setattr__(self, 'conn', conn)
            def __getattr__(self, key):
                return getattr(self.conn, key)
            def __setattr__(self, key, value):
                setattr(self.conn, key, value)
            def execute(self, sql, *args):
                if sql == 'PRAGMA journal_mode=WAL':
                    calls.append(sql)
                    if errors:
                        raise errors.pop(0)
                return self.conn.execute(sql, *args)

        with tempfile.TemporaryDirectory() as tmp:
            with patch('room_invite_store.sqlite3.connect', side_effect=lambda *a, **kw: Proxy(real(*a, **kw))):
                RoomInviteStore(Path(tmp) / 'invites.sqlite')
        return calls

    def test_wal_busy_retried_only_for_journal_mode(self):
        self.assertEqual(len(self.run_fault([sqlite3.OperationalError('database is locked') for _ in range(2)])), 3)

    def test_wal_nonbusy_error_not_retried(self):
        errors = [sqlite3.OperationalError('disk I/O error')] * 2
        with self.assertRaisesRegex(sqlite3.OperationalError, 'disk I/O error'):
            self.run_fault(errors)
        self.assertEqual(len(errors), 1)

    def test_wal_retry_bounded(self):
        errors = [sqlite3.OperationalError('database is locked') for _ in range(100)]
        with self.assertRaisesRegex(sqlite3.OperationalError, 'database is locked'):
            self.run_fault(errors)
        self.assertLessEqual(100 - len(errors), 20)
        self.assertGreater(100 - len(errors), 1)
