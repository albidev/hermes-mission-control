"""Durable, purpose-separated Room history read consent.

This store is intentionally independent from invitation execution consent and
material grants.  It is a small sidecar so v1 joining/room databases remain
readable without an implicit migration.  Every authorization is bound to the
canonical room/home/authority/member/recipient scope and requires both signed
owner approval and signed guest acceptance.
"""
from __future__ import annotations

import json
import hashlib
import os
import stat
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Mapping

from invite_rooms.consent import canonical, verify


PURPOSE_OWNER = "room-history-owner-read-v1"
PURPOSE_GUEST = "room-history-guest-read-v1"
_FIELDS = {
    "room_id", "home_install_id", "authority_gateway_id", "authority_epoch",
    "member_id", "recipient_id", "pairing_generation", "history_policy", "from_seq_inclusive",
}


class HistoryConsentError(ValueError):
    pass


def _scope(scope: Mapping) -> dict:
    if not isinstance(scope, Mapping) or set(scope) != _FIELDS:
        raise HistoryConsentError("invalid_history_scope")
    if (any(not isinstance(scope[k], str) or not scope[k] for k in
            ("room_id", "home_install_id", "authority_gateway_id", "member_id", "recipient_id"))
            or type(scope["authority_epoch"]) is not int or scope["authority_epoch"] < 1
            or type(scope["pairing_generation"]) is not int or scope["pairing_generation"] < 1
            or scope["history_policy"] not in {"on", "off"}
            or type(scope["from_seq_inclusive"]) is not int or scope["from_seq_inclusive"] < 1
            or (scope["history_policy"] == "on" and scope["from_seq_inclusive"] != 1)):
        raise HistoryConsentError("invalid_history_scope")
    return dict(scope)


class HistoryConsentStore:
    """SQLite-backed consent ledger; no row is inferred from an invite/grant."""
    def __init__(self, path: Path, *, owner_public_key: bytes, guest_public_key: bytes, clock=time.time):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # The pathname is part of the trust boundary. Reject a symlinked
        # parent and require a private owner-controlled directory before any
        # database open; following a parent link would move signed consent to
        # an attacker-selected store.
        try:
            self._assert_private_path(require_file=False)
            if not os.path.lexists(self.path):
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                os.close(fd)
            st = os.lstat(self.path)
            if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid()
                    or st.st_nlink != 1 or stat.S_IMODE(st.st_mode) != 0o600):
                raise HistoryConsentError("private_storage_required")
        except OSError:
            raise HistoryConsentError("private_storage_required") from None
        self.owner_public_key = bytes(owner_public_key)
        self.guest_public_key = bytes(guest_public_key)
        if len(self.owner_public_key) != 32 or len(self.guest_public_key) != 32:
            raise HistoryConsentError("invalid_history_key")
        self.clock = clock
        with self._conn() as db:
            db.execute("PRAGMA foreign_keys=ON")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS history_consent_schema(version INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS history_read_consent(
                  scope_key TEXT PRIMARY KEY, scope_json TEXT NOT NULL,
                  owner_receipt_json TEXT, owner_signature TEXT,
                  guest_receipt_json TEXT, guest_signature TEXT,
                  owner_key_id TEXT NOT NULL, guest_key_id TEXT NOT NULL,
                  revoked INTEGER NOT NULL DEFAULT 0,
                  revision INTEGER NOT NULL DEFAULT 1,
                  updated_at REAL NOT NULL
                );
            """)
            row = db.execute("SELECT version FROM history_consent_schema").fetchone()
            if row is None:
                db.execute("INSERT INTO history_consent_schema VALUES (1)")
            elif type(row[0]) is not int or row[0] != 1:
                raise HistoryConsentError("schema_too_new")
            columns = {r[1] for r in db.execute("PRAGMA table_info(history_read_consent)")}
            if "owner_key_id" not in columns or "guest_key_id" not in columns:
                raise HistoryConsentError("schema_migration_required")
            os.chmod(self.path, 0o600)

    @contextmanager
    def _conn(self):
        # Re-check on every open as well as construction. This closes the
        # ordinary same-UID pathname substitution case (parent/file symlink or
        # replacement) without pretending pathname SQLite access is a
        # descriptor-pinned VFS. A racing same-UID rename remains outside this
        # boundary and is fail-closed only when observed by this check.
        self._assert_private_path(require_file=True)
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            yield db
        finally:
            db.close()

    def _assert_private_path(self, *, require_file: bool) -> None:
        parent_st = os.lstat(self.path.parent)
        if (not stat.S_ISDIR(parent_st.st_mode) or stat.S_ISLNK(parent_st.st_mode)
                or parent_st.st_uid != os.getuid() or stat.S_IMODE(parent_st.st_mode) & 0o022):
            raise HistoryConsentError("private_storage_required")
        if not os.path.lexists(self.path):
            if require_file:
                raise HistoryConsentError("private_storage_required")
            return
        st = os.lstat(self.path)
        if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid()
                or st.st_nlink != 1 or stat.S_IMODE(st.st_mode) != 0o600):
            raise HistoryConsentError("private_storage_required")

    @staticmethod
    def _key(scope: dict) -> str:
        return canonical(scope).decode()

    def _row(self, scope):
        scope = _scope(scope)
        with self._conn() as db:
            row = db.execute("SELECT * FROM history_read_consent WHERE scope_key=?", (self._key(scope),)).fetchone()
        return dict(row) if row else None

    def owner_approve(self, scope: Mapping, receipt: Mapping, signature: str) -> None:
        scope = _scope(scope)
        expected = dict(scope, purpose=PURPOSE_OWNER)
        try:
            verify(self.owner_public_key, dict(receipt), signature, expected)
        except Exception:
            raise HistoryConsentError("invalid_owner_read_approval") from None
        self._write(scope, owner=(dict(receipt), signature))

    def guest_accept(self, scope: Mapping, receipt: Mapping, signature: str) -> None:
        scope = _scope(scope)
        expected = dict(scope, purpose=PURPOSE_GUEST)
        try:
            verify(self.guest_public_key, dict(receipt), signature, expected)
        except Exception:
            raise HistoryConsentError("invalid_guest_read_acceptance") from None
        self._write(scope, guest=(dict(receipt), signature))

    def _write(self, scope, *, owner=None, guest=None):
        key = self._key(scope)
        with self._conn() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM history_read_consent WHERE scope_key=?", (key,)).fetchone()
            if row is None:
                db.execute("INSERT INTO history_read_consent(scope_key,scope_json,owner_key_id,guest_key_id,updated_at) VALUES(?,?,?,?,?)",
                           (key, key, self._key_id(self.owner_public_key), self._key_id(self.guest_public_key), self.clock()))
                row = db.execute("SELECT * FROM history_read_consent WHERE scope_key=?", (key,)).fetchone()
            if row["revoked"]:
                db.rollback(); raise HistoryConsentError("history_read_revoked")
            values = []
            if owner is not None:
                if row["owner_receipt_json"] and row["owner_receipt_json"] != canonical(owner[0]).decode():
                    db.rollback(); raise HistoryConsentError("history_read_scope_conflict")
                values += ["owner_receipt_json=?", "owner_signature=?"]
            if guest is not None:
                if row["guest_receipt_json"] and row["guest_receipt_json"] != canonical(guest[0]).decode():
                    db.rollback(); raise HistoryConsentError("history_read_scope_conflict")
                values += ["guest_receipt_json=?", "guest_signature=?"]
            params = []
            if owner is not None: params += [canonical(owner[0]).decode(), owner[1]]
            if guest is not None: params += [canonical(guest[0]).decode(), guest[1]]
            params += [self.clock(), key]
            db.execute("UPDATE history_read_consent SET " + ",".join(values + ["updated_at=?", "revision=revision+1"]) + " WHERE scope_key=?", params)
            db.commit()

    def revoke(self, scope: Mapping) -> None:
        scope = _scope(scope)
        with self._conn() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("UPDATE history_read_consent SET revoked=1,revision=revision+1,updated_at=? WHERE scope_key=?", (self.clock(), self._key(scope))).rowcount != 1:
                db.rollback(); raise HistoryConsentError("history_read_unavailable")
            db.commit()

    def _checked(self, scope: Mapping, row: dict) -> dict:
        """Re-verify durable bytes on every read; presence alone is not consent."""
        scope = _scope(scope)
        if (row.get("scope_json") != self._key(scope)
                or row.get("owner_key_id") != self._key_id(self.owner_public_key)
                or row.get("guest_key_id") != self._key_id(self.guest_public_key)
                or type(row.get("revoked")) is not int
                or row["revoked"] not in (0, 1) or type(row.get("revision")) is not int
                or row["revision"] < 1):
            raise HistoryConsentError("history_read_corrupt")
        for receipt_json, signature, purpose, key in (
                (row.get("owner_receipt_json"), row.get("owner_signature"), PURPOSE_OWNER, self.owner_public_key),
                (row.get("guest_receipt_json"), row.get("guest_signature"), PURPOSE_GUEST, self.guest_public_key)):
            if receipt_json is None and signature is None:
                continue
            if not isinstance(receipt_json, str) or not isinstance(signature, str):
                raise HistoryConsentError("history_read_corrupt")
            try:
                receipt = json.loads(receipt_json)
                verify(key, receipt, signature, dict(scope, purpose=purpose))
            except Exception:
                raise HistoryConsentError("history_read_corrupt") from None
        return row

    @staticmethod
    def _key_id(key: bytes) -> str:
        return hashlib.sha256(key).hexdigest()

    def authorized(self, scope: Mapping) -> bool:
        row = self._row(scope)
        if row is None:
            return False
        try:
            checked = self._checked(scope, row)
        except HistoryConsentError:
            return False
        return bool(not checked["revoked"] and checked["owner_receipt_json"] and checked["guest_receipt_json"])

    def snapshot(self, scope: Mapping) -> dict:
        row = self._row(scope)
        if row is None:
            raise HistoryConsentError("history_read_unavailable")
        return self._checked(scope, row)
