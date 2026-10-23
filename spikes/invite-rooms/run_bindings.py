"""Fixed local scope receipts. Never read grants, keys, prompts or request URLs."""
from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3
from room_firewall import _validated_scope, _RUN_ID

_FIELDS = ('room_id', 'home_install_id', 'authority_gateway_id', 'authority_epoch',
           'member_id', 'target_install_id', 'target_profile')

def scope_digest(scope):
    clean = _validated_scope(scope)
    return hashlib.sha256('\0'.join(str(clean[k]) for k in _FIELDS).encode()).hexdigest()

class NativeReceiptVerifier:
    """Fresh read-only lookup in the configured target's actual native store."""
    def __init__(self, path):
        self.path = Path(path)
        if not self.path.is_absolute():
            raise ValueError('receipt path must be owner-fixed and absolute')

    def exact_replay(self, run_id, scope, task_id, dispatch_generation, key):
        # Native normalization requires this literal task/generation key before
        # reserving. Runtime status.execution_generation is a different domain.
        if key != f'room:{task_id}:{dispatch_generation}':
            return False
        try:
            with closing(sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True, timeout=.5)) as db:
                db.execute('PRAGMA query_only=ON')
                return db.execute('SELECT 1 FROM run_idempotency WHERE scope=? AND idempotency_key=? AND run_id=?',
                    (scope_digest(scope), key, run_id)).fetchone() is not None
        except (sqlite3.Error, OSError, ValueError):
            return False

    def __call__(self, run_id, scope):
        if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
            return False
        try:
            with closing(sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True, timeout=.5)) as db:
                db.execute('PRAGMA query_only=ON')
                db.execute('PRAGMA busy_timeout=500')
                return db.execute('SELECT 1 FROM run_idempotency WHERE scope=? AND run_id=? LIMIT 1',
                                  (scope_digest(scope), run_id)).fetchone() is not None
        except (sqlite3.Error, OSError, ValueError):
            return False

class HostRunBindings(NativeReceiptVerifier):
    """Private, bounded host receipts enrolled only by the validated ack hook."""
    def __init__(self, path):
        import os
        super().__init__(path)
        fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600) if not self.path.exists() else None
        if fd is not None:
            os.close(fd)
        if self.path.is_symlink() or self.path.stat().st_mode & 0o077:
            raise ValueError('host binding store must be private regular owner storage')
        with closing(sqlite3.connect(self.path, timeout=.5)) as db:
            db.execute('CREATE TABLE IF NOT EXISTS host_run_bindings(scope TEXT NOT NULL, run_id TEXT NOT NULL, PRIMARY KEY(scope,run_id))')
            db.execute('CREATE TABLE IF NOT EXISTS host_dispatch_bindings(scope TEXT NOT NULL, idempotency_key TEXT NOT NULL, task_id TEXT NOT NULL, dispatch_generation INTEGER NOT NULL, run_id TEXT NOT NULL, PRIMARY KEY(scope,idempotency_key))')
            db.commit()

    def exact_replay(self, run_id, scope, task_id, dispatch_generation, key):
        try:
            with closing(sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True, timeout=.5)) as db:
                db.execute('PRAGMA query_only=ON')
                return db.execute('SELECT 1 FROM host_dispatch_bindings WHERE scope=? AND idempotency_key=? AND task_id=? AND dispatch_generation=? AND run_id=?',
                    (scope_digest(scope), key, task_id, dispatch_generation, run_id)).fetchone() is not None
        except (sqlite3.Error, OSError, ValueError):
            return False

    def record_dispatch_ack(self, run_id, scope, task_id, dispatch_generation, key):
        # Called only after a validated non-unknown acknowledgement. Never
        # updates/replaces an immutable binding and never writes native rows.
        coordinates = (task_id, dispatch_generation, run_id)
        with closing(sqlite3.connect(self.path.as_uri() + '?mode=rw', uri=True, timeout=.5)) as db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT task_id,dispatch_generation,run_id FROM host_dispatch_bindings WHERE scope=? AND idempotency_key=?',
                (scope_digest(scope), key)).fetchone()
            if existing is not None and existing != coordinates:
                raise ValueError('conflicting immutable dispatch acknowledgement')
            if existing is None:
                if db.execute('SELECT count(*) FROM host_dispatch_bindings').fetchone()[0] >= 100000:
                    raise ValueError('host dispatch binding capacity reached')
                db.execute('INSERT INTO host_dispatch_bindings VALUES (?,?,?,?,?)',
                    (scope_digest(scope), key, *coordinates))
            db.commit()

    def __call__(self, run_id, scope):
        try:
            with closing(sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True, timeout=.5)) as db:
                db.execute('PRAGMA query_only=ON')
                return db.execute('SELECT 1 FROM host_run_bindings WHERE scope=? AND run_id=? LIMIT 1',
                                  (scope_digest(scope), run_id)).fetchone() is not None
        except (sqlite3.Error, OSError, ValueError):
            return False

    def record_ack(self, run_id, scope):
        if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
            raise ValueError('invalid acknowledged run')
        with closing(sqlite3.connect(self.path.as_uri() + '?mode=rw', uri=True, timeout=.5)) as db:
            db.execute('BEGIN IMMEDIATE')
            key = (scope_digest(scope), run_id)
            if not db.execute('SELECT 1 FROM host_run_bindings WHERE scope=? AND run_id=?', key).fetchone():
                if db.execute('SELECT count(*) FROM host_run_bindings').fetchone()[0] >= 100000:
                    raise ValueError('host binding store capacity reached')
                db.execute('INSERT INTO host_run_bindings VALUES (?,?)', key)
            db.commit()
