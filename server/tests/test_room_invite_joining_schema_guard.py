"""MVP.2b-fix4 regression tests for R5 (JoiningStore schema guard).

Behaviour-only: disposable SQLite DBs under tempfile.TemporaryDirectory(),
no network/native. Ports fix4's external review probe
(evidence/mvp2b-rereview2/review_checks.py R5_variants + R5_read_guard_gap)
into the in-repo suite so the fix is proven by a committed regression test,
not only by an external script.

R5 (pre-fix4): the schema guard only checked table NAMES + row/version
count, so same-named columns with a different TYPE/no PK/no NOT
NULL/missing index/FK were silently accepted, and a read used two separate
transactions (schema check, then the actual SELECT) so a version bump
injected in that gap was invisible to the read. fix4 adds a full structural
layout check (type/notnull/pk per column, indexes, foreign keys) and folds
the version check into the SAME transaction as the read/write it guards.
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from invite_rooms.joining_store import JoiningStore, JoiningStoreError


def _make_store(tmp):
    store = JoiningStore(Path(tmp) / "joining")
    import_id = store.create_import(
        lobby_id="lobby-a", invite_id="invite-a", secret="a" * 43, host_peer_id="host-a"
    )
    return store, import_id


def _dump(path):
    with sqlite3.connect(path) as db:
        return "\n".join(db.iterdump())


class SchemaStructuralGuardTests(unittest.TestCase):
    """R5: structurally-wrong-but-same-named columns/constraints must be
    refused by the schema guard, and the on-disk bytes must be unchanged."""

    def _assert_rejected_and_unchanged(self, mutate):
        with tempfile.TemporaryDirectory() as tmp:
            store, import_id = _make_store(tmp)
            before = _dump(store.path)
            with sqlite3.connect(store.path) as db:
                mutate(db)
            after_mutation = _dump(store.path)
            self.assertNotEqual(before, after_mutation, "mutation must actually change on-disk bytes")

            with self.assertRaisesRegex(JoiningStoreError, "^schema_version_unsupported$"):
                store.load_secret(import_id)
            with self.assertRaisesRegex(JoiningStoreError, "^schema_version_unsupported$"):
                store.create_import(
                    lobby_id="lobby-b", invite_id="invite-b", secret="b" * 43, host_peer_id="host-a"
                )
            with self.assertRaisesRegex(JoiningStoreError, "^schema_version_unsupported$"):
                JoiningStore(Path(tmp) / "joining")

            unchanged = _dump(store.path)
            self.assertEqual(after_mutation, unchanged, "a rejected guard must never mutate DB bytes")

    def test_wrong_column_type_same_name_rejected(self):
        """Same column NAMES, wrong TYPE: a pre-R5 name-only check would pass this."""
        def mutate(db):
            db.execute("DROP TABLE material")
            db.execute(
                "CREATE TABLE material(scope_key BLOB, grant_id BLOB, state BLOB, nonce BLOB, ciphertext BLOB)"
            )
        self._assert_rejected_and_unchanged(mutate)

    def test_missing_primary_key_rejected(self):
        """Same columns, no PK on scope_key: structural contract broken."""
        def mutate(db):
            db.execute("DROP TABLE material")
            db.execute(
                "CREATE TABLE material(scope_key TEXT, grant_id TEXT NOT NULL, state TEXT NOT NULL, "
                "nonce BLOB, ciphertext BLOB)"
            )
        self._assert_rejected_and_unchanged(mutate)

    def test_missing_not_null_rejected(self):
        """Same columns/types, but a required NOT NULL constraint dropped."""
        def mutate(db):
            db.execute("DROP TABLE imports")
            db.execute(
                "CREATE TABLE imports(import_id TEXT PRIMARY KEY, lobby_id TEXT, invite_id TEXT NOT NULL, "
                "host_peer_id TEXT NOT NULL, state TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL)"
            )
        self._assert_rejected_and_unchanged(mutate)

    def test_missing_unique_index_rejected(self):
        """Same columns/types/PK, but the UNIQUE(lobby_id, invite_id) table
        constraint is dropped: dedup guarantee silently lost. SQLite refuses
        to DROP INDEX on an autoindex backing a table constraint, so the
        table is recreated without that constraint instead."""
        def mutate(db):
            rows = db.execute("SELECT import_id,lobby_id,invite_id,host_peer_id,state,created_at,updated_at FROM imports").fetchall()
            db.execute("DROP TABLE imports")
            db.execute(
                "CREATE TABLE imports(import_id TEXT PRIMARY KEY, lobby_id TEXT NOT NULL, invite_id TEXT NOT NULL, "
                "host_peer_id TEXT NOT NULL, state TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL)"
            )
            db.executemany("INSERT INTO imports VALUES(?,?,?,?,?,?,?)", rows)
        self._assert_rejected_and_unchanged(mutate)

    def test_missing_foreign_key_rejected(self):
        """Drop the FK from import_secrets to imports: orphaned secrets become possible."""
        def mutate(db):
            db.execute("DROP TABLE import_secrets")
            db.execute("CREATE TABLE import_secrets(import_id TEXT PRIMARY KEY, nonce BLOB NOT NULL, ciphertext BLOB NOT NULL)")
        self._assert_rejected_and_unchanged(mutate)


class SchemaReadGuardAtomicityTests(unittest.TestCase):
    """R5: a deterministic version bump landing in the gap between the schema
    check and the dependent read must be caught by that SAME read, not just
    by the next unrelated call. No secret is ever returned once the guard
    would reject the live layout."""

    def test_external_version_bump_between_guard_and_read_is_rejected(self):
        """Fix4 folds the version check into the SAME transaction as the
        read it guards (BEGIN IMMEDIATE before the check), so an externally
        injected bump mid-guard either serializes behind the held lock or is
        observed by the check itself — either way the read must never
        succeed and no secret is ever returned."""
        with tempfile.TemporaryDirectory() as tmp:
            store, import_id = _make_store(tmp)
            original_check = store._check_schema_version

            def check_then_bump(conn):
                original_check(conn)
                with sqlite3.connect(store.path) as external:
                    external.execute("UPDATE schema_version SET version=99")

            with patch.object(store, "_check_schema_version", side_effect=check_then_bump):
                with self.assertRaises(Exception):
                    store.load_secret(import_id)

    def test_external_version_bump_between_guard_and_get_import_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, import_id = _make_store(tmp)
            original_check = store._check_schema_version

            def check_then_bump(conn):
                original_check(conn)
                with sqlite3.connect(store.path) as external:
                    external.execute("UPDATE schema_version SET version=99")

            with patch.object(store, "_check_schema_version", side_effect=check_then_bump):
                with self.assertRaises(Exception):
                    store.get_import(import_id)


class CrashRestartSingleSendTests(unittest.TestCase):
    """R4 invariant kept by the joining store across fix4: a crash-restart
    after a durable redeem intent must still deliver exactly one send."""

    def test_crash_restart_still_one_send(self):
        W = Path(__file__).resolve().parents[1]
        sys.path.insert(0, str(W))
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import test_room_invite_joining as t

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fx = t.make_fixture(root)
            svc = fx["service"]
            inv = fx["invite"]
            iid = svc.preview(
                t._invitation_text(fx["lobby"]["lobby_id"], inv.invite_id, inv.secret)
            )["import_id"]
            original = fx["channel"]

            class AbruptInterruption(BaseException):
                pass

            class Interrupted:
                def call(self, req):
                    response = original.call(req)
                    if req["op"] == "redeem":
                        raise AbruptInterruption()
                    return response

            svc._channel = Interrupted()
            try:
                svc.redeem(iid)
            except AbruptInterruption:
                pass

            reopened = JoiningStore(root / "joining")
            resumed = t.JoiningOwnerService(
                reopened, fx["registry"], host_peer_id="host-a", origin=t.ORIGIN,
                consent_key=fx["consent_key"], channel=original, self_binding=t.SELF_BINDING,
            )
            resumed.resolve_redeem_ambiguous(iid, confirmed_redeemed=True)
            second = resumed.redeem(iid)
            self.assertEqual(second["status"], "already_redeemed")

            sends = sum(c["op"] == "redeem" for c in original.calls)
            self.assertEqual(sends, 1)


if __name__ == "__main__":
    unittest.main()
