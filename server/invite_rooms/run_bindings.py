"""Product run verification, not native execution authority or a native DB adapter.

Target: confirmed local admission receipts AND the unchanged original READY grant.
Host: immutable, private ack bindings enrolled only by the firewall's validated
record_dispatch_ack hook. record_ack cannot create a binding (notably unknown).
No prompts, grants, tokens or native database paths are stored here.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from pathlib import Path
from types import MappingProxyType

from invite_rooms.admission_receipts import AdmissionReceiptStore
from invite_rooms.anchored_sqlite import AnchoredSQLite
from invite_rooms.consent import canonical
from invite_rooms.control.firewall import _validated_scope
from invite_rooms.material_validators import validate_scope_attributes, validate_scope_dict
from invite_rooms.peer_registry import MaterialScope
from invite_rooms.target_material import TargetMaterialSource, ResolvedTargetMaterial, _claim_payload

_RUN = re.compile(r"run_[0-9a-f]{32}\Z")
_TASK = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,255}\Z")
_VERSION = "mc-host-ack-binding-v1"
_MAX = 4096
_MAX_BYTES = 16384


class RunBindingError(ValueError):
    """Sanitized binding/storage failure."""


def _fail():
    raise RunBindingError("run_binding_unavailable")


def _exact(left, right):
    try:
        return canonical(dict(left)) == canonical(dict(right))
    except (ValueError, TypeError):
        return False


def _coordinates(run_id, task, generation, key):
    if (type(run_id) is not str or not _RUN.fullmatch(run_id)
            or type(task) is not str or not _TASK.fullmatch(task)
            or type(generation) is not int or generation < 1
            or type(key) is not str or key != f"room:{task}:{generation}"
            or len(key) > 255):
        _fail()


def _binding(scope, expected_scope):
    if type(scope) is not MaterialScope:
        _fail()
    value = validate_scope_attributes(scope, generation=scope.pairing_generation, profile=scope.target_profile)
    expected = _validated_scope(expected_scope)
    fixed = dict(room_id=value['planned_room_id'], home_install_id=value['owner_installation_id'],
                 member_id=value['member_id'], target_install_id=value['target_install_id'],
                 target_profile=value['target_profile'])
    if any(expected[k] != v for k, v in fixed.items()):
        _fail()
    return value, expected


class TargetReceiptVerifier:
    """Exact full-scope receipt read; revalidate READY original authority on every use.

    AdmissionReceiptStore intentionally contains no grant authority fields. Thus
    a receipt alone CANNOT prove the seven-field grant namespace. The concrete
    source must still load the same locally consented immutable material. A
    missing/corrupt/stale source or journal cutover always denies verification.
    """
    def __init__(self, *, store, source, resolved, expected_scope):
        if (type(store) is not AdmissionReceiptStore or type(source) is not TargetMaterialSource
                or type(resolved) is not ResolvedTargetMaterial):
            _fail()
        _binding(resolved.scope, expected_scope)
        self._store, self._source, self._resolved = store, source, resolved
        self._expected = MappingProxyType(_validated_scope(expected_scope))
        self._profile = source._target_http.profile
        self._base_url = source._target_http.base_url

    def _find(self, run_id, expected, key=None):
        if not _exact(expected, self._expected):
            return None
        deadline = time.monotonic() + 5
        scope = self._resolved.scope
        try:
            row = self._store.find_confirmed_run(scope=scope, generation=scope.pairing_generation,
                profile=self._profile, base_url=self._base_url, run_id=run_id,
                idempotency_key=key, deadline=deadline)
            if row is None:
                return None
            material = self._source.load(scope, credential_ref=self._resolved.credential_ref, deadline=deadline)
            claims = _claim_payload(material['grant'])
            observed = {k: claims.get(k) for k in self._expected}
            if not _exact(observed, self._expected) or time.monotonic() >= deadline:
                return None
            return row
        except Exception:
            return None

    def __call__(self, run_id, expected_scope):
        return self._find(run_id, expected_scope) is not None

    def exact_replay(self, run_id, expected_scope, task_id, execution_generation, idempotency_key):
        try:
            _coordinates(run_id, task_id, execution_generation, idempotency_key)
        except RunBindingError:
            return False
        return self._find(run_id, expected_scope, idempotency_key) is not None


class HostAckBindingStore:
    """MC-owned encrypted rows, bounded and first-ack immutable across processes.

    Uses the existing anchored SQLite storage contract (including its documented
    same-UID pathname/ABA threat-model limit), never a native DB. BEGIN IMMEDIATE
    serializes enrollment; every read validates ALL bounded rows, not first-match.
    """
    def __init__(self, root: str | Path, *, anchor: Path):
        self._lock = threading.Lock()
        self._storage = AnchoredSQLite(root, 'host_ack_bindings.sqlite', 'host_ack_bindings.key', RunBindingError, anchor=anchor)
        self.path = self._storage.path
        try:
            with self._storage.initialize(self._schema) as (fresh, aes):
                self._aes = aes
                with self._storage.connection() as conn:
                    if fresh:
                        conn.execute('CREATE TABLE bindings (run_id TEXT PRIMARY KEY, dispatch_key TEXT UNIQUE NOT NULL, record BLOB NOT NULL)')
                        conn.execute('PRAGMA user_version=1')
                    self._schema(conn)
                    self._rows(conn)
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _schema(conn):
        if (conn.execute('PRAGMA user_version').fetchone()[0] != 1
                or [r[1] for r in conn.execute('PRAGMA table_info(bindings)')] != ['run_id', 'dispatch_key', 'record']):
            _fail()

    def _rows(self, conn):
        rows = conn.execute('SELECT run_id,dispatch_key,record FROM bindings LIMIT ?', (_MAX+1,)).fetchall()
        if len(rows) > _MAX:
            _fail()
        result = []
        try:
            for run, key, raw in rows:
                if type(raw) is not bytes or not 28 <= len(raw) <= _MAX_BYTES:
                    _fail()
                value = json.loads(self._aes.decrypt(raw[:12], raw[12:], canonical([_VERSION, run, key])))
                if type(value) is not dict or set(value) != {'version','scope','expected_scope','run_id','task_id','execution_generation','idempotency_key'} or value['version'] != _VERSION:
                    _fail()
                validate_scope_dict(value['scope'])
                _binding(MaterialScope(**value['scope']), value['expected_scope'])
                _coordinates(value['run_id'],value['task_id'],value['execution_generation'],value['idempotency_key'])
                if value['run_id'] != run or value['idempotency_key'] != key:
                    _fail()
                result.append(value)
        except Exception:
            _fail()
        return result

    def _read(self):
        with self._lock, self._storage.connection(timeout=.5) as conn:
            self._schema(conn)
            return self._rows(conn)

    def _record(self, record):
        import secrets
        with self._lock, self._storage.connection(timeout=.5) as conn:
            conn.execute('BEGIN IMMEDIATE')
            try:
                self._schema(conn)
                rows = self._rows(conn)
                for old in rows:
                    if old['run_id'] == record['run_id'] or old['idempotency_key'] == record['idempotency_key']:
                        if old != record:
                            _fail()
                        conn.commit()
                        return
                if len(rows) >= _MAX:
                    _fail()
                nonce = secrets.token_bytes(12)
                raw = nonce + self._aes.encrypt(nonce, canonical(record), canonical([_VERSION, record['run_id'], record['idempotency_key']]))
                if len(raw) > _MAX_BYTES:
                    _fail()
                conn.execute('INSERT INTO bindings VALUES (?,?,?)', (record['run_id'], record['idempotency_key'], raw))
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def verifier(self, *, scope, expected_scope):
        value, expected = _binding(scope, expected_scope)
        return HostAckVerifier(self, value, expected)

    def close(self):
        with self._lock:
            self._storage.close()


class HostAckVerifier:
    def __init__(self, store, scope, expected):
        self._store = store
        self._scope = MappingProxyType(dict(scope))
        self._expected = MappingProxyType(dict(expected))

    def _find(self, run_id, expected):
        if not _exact(expected, self._expected) or type(run_id) is not str or not _RUN.fullmatch(run_id):
            return None
        try:
            rows = self._store._read()
            matches = [r for r in rows if r['run_id'] == run_id]
            if len(matches) != 1:
                return None
            row = matches[0]
            if row['scope'] == dict(self._scope) and _exact(row['expected_scope'], self._expected):
                return row
        except Exception:
            pass
        return None

    def __call__(self, run_id, expected_scope):
        return self._find(run_id, expected_scope) is not None

    def exact_replay(self, run_id, expected_scope, task_id, execution_generation, idempotency_key):
        try:
            _coordinates(run_id, task_id, execution_generation, idempotency_key)
        except RunBindingError:
            return False
        row = self._find(run_id, expected_scope)
        return (row is not None and row['task_id'] == task_id and row['execution_generation'] == execution_generation
                and row['idempotency_key'] == idempotency_key)

    def record_dispatch_ack(self, run_id, expected_scope, task_id, execution_generation, idempotency_key):
        # Called only after the firewall validates a non-unknown native ack.
        _coordinates(run_id, task_id, execution_generation, idempotency_key)
        if not _exact(expected_scope, self._expected):
            _fail()
        self._store._record(dict(version=_VERSION, scope=dict(self._scope), expected_scope=dict(self._expected),
            run_id=run_id, task_id=task_id, execution_generation=execution_generation, idempotency_key=idempotency_key))

    def record_ack(self, run_id, expected_scope):
        # The two-argument callback conveys no dispatch coordinates. Never enroll.
        if not self(run_id, expected_scope):
            _fail()
