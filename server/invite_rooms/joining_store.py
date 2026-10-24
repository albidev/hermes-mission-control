"""Durable joining-owner-side state: pending imports, consent receipts and the
target-side material mint journal.

One joining-owner profile tracks invitations received from pre-approved host
peers. Explicit `schema_version`, fail-closed on any version this code does
not recognize exactly like room_invite_store.py's discipline for R1: there is
no automatic migration here either. File/parent permissions are enforced
(0700 parent dir, 0600 db file and key file) and the loader refuses rather
than silently relaxing them.

The invitation secret is encrypted at rest (AES-GCM, 256-bit key, 0600 key
file distinct from the consent signing key in consent.py and from any
transport/TLS credential) and is only ever decrypted by `load_secret`, which
callers must use exactly once per redeem attempt. It is never returned by any
other method and never appears in __repr__ of any dataclass view.
"""
from __future__ import annotations

import json
import os
import re
import secrets as _secrets
import stat
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class JoiningStoreError(ValueError):
    """Machine code only: never interpolates remote values or secrets."""


SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE schema_version(version INTEGER NOT NULL);
INSERT INTO schema_version VALUES (1);
CREATE TABLE imports(
 import_id TEXT PRIMARY KEY, lobby_id TEXT NOT NULL, invite_id TEXT NOT NULL,
 host_peer_id TEXT NOT NULL, state TEXT NOT NULL,
 created_at REAL NOT NULL, updated_at REAL NOT NULL,
 UNIQUE(lobby_id, invite_id)
);
CREATE TABLE import_secrets(
 import_id TEXT PRIMARY KEY REFERENCES imports(import_id),
 nonce BLOB NOT NULL, ciphertext BLOB NOT NULL
);
CREATE TABLE confirmed_scope(
 import_id TEXT PRIMARY KEY REFERENCES imports(import_id),
 scope_json TEXT NOT NULL
);
CREATE TABLE consents(
 import_id TEXT PRIMARY KEY REFERENCES imports(import_id),
 receipt_json TEXT NOT NULL, signature TEXT NOT NULL
);
CREATE TABLE material(
 scope_key TEXT PRIMARY KEY, grant_id TEXT NOT NULL, state TEXT NOT NULL, nonce BLOB, ciphertext BLOB
);
"""

_SCHEMA_TABLES = {
    "schema_version": ("version",),
    "imports": ("import_id", "lobby_id", "invite_id", "host_peer_id", "state", "created_at", "updated_at"),
    "import_secrets": ("import_id", "nonce", "ciphertext"),
    "confirmed_scope": ("import_id", "scope_json"),
    "consents": ("import_id", "receipt_json", "signature"),
    "material": ("scope_key", "grant_id", "state", "nonce", "ciphertext"),
}

# Structural contract, not merely a column-name check.  Keep this explicit so
# an unversioned or drifted database can never be adopted by accident.
_SCHEMA_LAYOUT = {
    "schema_version": (("version", "INTEGER", 1, 0),),
    "imports": (("import_id", "TEXT", 0, 1), ("lobby_id", "TEXT", 1, 0), ("invite_id", "TEXT", 1, 0),
                 ("host_peer_id", "TEXT", 1, 0), ("state", "TEXT", 1, 0), ("created_at", "REAL", 1, 0), ("updated_at", "REAL", 1, 0)),
    "import_secrets": (("import_id", "TEXT", 0, 1), ("nonce", "BLOB", 1, 0), ("ciphertext", "BLOB", 1, 0)),
    "confirmed_scope": (("import_id", "TEXT", 0, 1), ("scope_json", "TEXT", 1, 0)),
    "consents": (("import_id", "TEXT", 0, 1), ("receipt_json", "TEXT", 1, 0), ("signature", "TEXT", 1, 0)),
    "material": (("scope_key", "TEXT", 0, 1), ("grant_id", "TEXT", 1, 0), ("state", "TEXT", 1, 0), ("nonce", "BLOB", 0, 0), ("ciphertext", "BLOB", 0, 0)),
}
_SCHEMA_INDEXES = {
    "schema_version": (), "imports": ((True, ("import_id",)), (True, ("lobby_id", "invite_id"))),
    "import_secrets": ((True, ("import_id",)),), "confirmed_scope": ((True, ("import_id",)),),
    "consents": ((True, ("import_id",)),), "material": ((True, ("scope_key",)),),
}
_SCHEMA_FKS = {
    "schema_version": (), "imports": (), "import_secrets": (("imports", "import_id", "import_id"),),
    "confirmed_scope": (("imports", "import_id", "import_id"),), "consents": (("imports", "import_id", "import_id"),),
    "material": (),
}

_IMPORT_ID = re.compile(r"import-[0-9a-f]{32}")
_TOKEN = r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}"
_LOBBY_ID = r"lobby-[a-zA-Z0-9-]{1,64}"
_INVITE_ID = r"invite-[a-zA-Z0-9-]{1,64}"


def _require_private_dir(path: Path) -> None:
    if stat.S_IMODE(os.stat(path).st_mode) != 0o700:
        raise JoiningStoreError("private_storage_required")


def _require_private_file(fd_or_path) -> None:
    mode = os.fstat(fd_or_path).st_mode if isinstance(fd_or_path, int) else os.stat(fd_or_path).st_mode
    if stat.S_IMODE(mode) != 0o600:
        raise JoiningStoreError("private_storage_required")


class JoiningStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        if self.root.exists() and not self.root.is_dir():
            raise JoiningStoreError("private_storage_required")
        _existed = self.root.exists()
        self.root.mkdir(parents=True, exist_ok=True)
        if not _existed:
            # 0700 only for a directory THIS code created; a pre-existing
            # parent with a different mode is rejected, never silently
            # relaxed (R6).
            os.chmod(self.root, 0o700)
        _require_private_dir(self.root)
        self._cipher = self._load_or_create_key(self.root / "joining-secret.key")
        self.path = self.root / "joining.sqlite"
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        _require_private_file(self.path)
        with self._connection() as conn:
            exists = conn.execute("SELECT 1 FROM sqlite_master WHERE name='schema_version'").fetchone()
            if not exists:
                # A non-empty database without a schema_version table is a
                # foreign/unversioned layout: fail closed, never adopt it as
                # a fresh version-1 store (R5).
                other_tables = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                ).fetchone()
                if other_tables is not None:
                    raise JoiningStoreError("schema_version_unsupported")
                conn.execute("BEGIN IMMEDIATE")
                try:
                    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='schema_version'").fetchone():
                        for statement in _SCHEMA.split(";"):
                            if statement.strip():
                                conn.execute(statement)
                    conn.commit()
                except BaseException:
                    conn.rollback()
                    raise
            self._check_schema_version(conn)
            conn.execute("PRAGMA journal_mode=WAL")

    @staticmethod
    def _load_or_create_key(keyfile: Path) -> AESGCM:
        try:
            fd = os.open(keyfile, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "wb") as out:
                out.write(AESGCM.generate_key(bit_length=256))
                out.flush()
                os.fsync(out.fileno())
        fd = os.open(keyfile, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            _require_private_file(fd)
            with os.fdopen(fd, "rb") as inp:
                key = inp.read(33)
        except BaseException:
            raise
        if len(key) != 32:
            raise JoiningStoreError("private_storage_required")
        return AESGCM(key)

    @contextmanager
    def _connection(self):
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _read(self):
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._check_schema_version(conn)
                yield conn
            finally:
                conn.rollback()

    @contextmanager
    def _write(self):
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._check_schema_version(conn)
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    @staticmethod
    def _check_schema_version(conn) -> None:
        """Validate the complete live layout before any schema-dependent use."""
        try:
            versions = conn.execute("SELECT version FROM schema_version").fetchall()
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )}
            if tables != set(_SCHEMA_TABLES) or len(versions) != 1:
                raise JoiningStoreError("schema_version_unsupported")
            if type(versions[0][0]) is not int or versions[0][0] != SCHEMA_VERSION:
                raise JoiningStoreError("schema_version_unsupported")
            for table, expected in _SCHEMA_TABLES.items():
                actual = tuple(r[1] for r in conn.execute(f"PRAGMA table_info({table})"))
                if actual != expected:
                    raise JoiningStoreError("schema_version_unsupported")
                layout = tuple((r[1], r[2].upper(), r[3], r[5])
                               for r in conn.execute(f"PRAGMA table_info({table})"))
                if layout != _SCHEMA_LAYOUT[table]:
                    raise JoiningStoreError("schema_version_unsupported")
                indexes = []
                for row in conn.execute(f"PRAGMA index_list({table})"):
                    columns = tuple(r[2] for r in conn.execute(f"PRAGMA index_info({row[1]!r})"))
                    indexes.append((bool(row[2]), columns))
                if tuple(sorted(indexes)) != tuple(sorted(_SCHEMA_INDEXES[table])):
                    raise JoiningStoreError("schema_version_unsupported")
                fks = tuple(sorted((r[2], r[3], r[4])
                                   for r in conn.execute(f"PRAGMA foreign_key_list({table})")))
                if fks != tuple(sorted(_SCHEMA_FKS[table])):
                    raise JoiningStoreError("schema_version_unsupported")
        except (sqlite3.DatabaseError, JoiningStoreError):
            raise JoiningStoreError("schema_version_unsupported") from None

    @staticmethod
    def _canonical(value) -> str:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)

    def _seal(self, aad: str, plaintext: bytes):
        nonce = os.urandom(12)
        return nonce, self._cipher.encrypt(nonce, plaintext, aad.encode())

    def _open(self, aad: str, nonce: bytes, ciphertext: bytes) -> bytes:
        return self._cipher.decrypt(nonce, ciphertext, aad.encode())

    # --- imports ---

    def create_import(self, *, lobby_id: str, invite_id: str, secret: str, host_peer_id: str) -> str:
        if not re.fullmatch(_LOBBY_ID, lobby_id) or not re.fullmatch(_INVITE_ID, invite_id):
            raise JoiningStoreError("invalid_import")
        if not isinstance(secret, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", secret):
            raise JoiningStoreError("invalid_import")
        if not re.fullmatch(_TOKEN, host_peer_id):
            raise JoiningStoreError("invalid_import")
        import_id = "import-" + uuid.uuid4().hex
        now = time.time()
        with self._write() as conn:
            existing = conn.execute(
                "SELECT import_id FROM imports WHERE lobby_id=? AND invite_id=?", (lobby_id, invite_id)
            ).fetchone()
            if existing is not None:
                return existing["import_id"]
            conn.execute(
                "INSERT INTO imports(import_id,lobby_id,invite_id,host_peer_id,state,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (import_id, lobby_id, invite_id, host_peer_id, "previewed", now, now),
            )
            nonce, ct = self._seal(import_id, secret.encode())
            conn.execute("INSERT INTO import_secrets VALUES(?,?,?)", (import_id, nonce, ct))
        return import_id

    def get_import(self, import_id: str) -> dict:
        _check_import_id(import_id)
        with self._read() as conn:
            row = conn.execute("SELECT * FROM imports WHERE import_id=?", (import_id,)).fetchone()
            if row is None:
                raise JoiningStoreError("import_unavailable")
            return dict(row)

    def list_imports(self) -> list[dict]:
        """Return redacted joining records for the local guest read model."""
        with self._read() as conn:
            rows = conn.execute("SELECT * FROM imports ORDER BY created_at, import_id").fetchall()
        return [dict(row) for row in rows]

    def find_import(self, lobby_id: str, invite_id: str):
        """Read-only lookup by (lobby_id, invite_id). Returns None if unknown
        rather than raising, so handlers can map an inbound payload without
        an existence oracle leaking through an exception message."""
        with self._read() as conn:
            row = conn.execute(
                "SELECT * FROM imports WHERE lobby_id=? AND invite_id=?", (lobby_id, invite_id)
            ).fetchone()
        return dict(row) if row is not None else None

    def load_secret(self, import_id: str) -> str:
        """Decrypt and return the invitation secret. Callers must not log it."""
        _check_import_id(import_id)
        with self._read() as conn:
            row = conn.execute(
                "SELECT nonce,ciphertext FROM import_secrets WHERE import_id=?", (import_id,)
            ).fetchone()
        if row is None:
            raise JoiningStoreError("import_unavailable")
        try:
            return self._open(import_id, row["nonce"], row["ciphertext"]).decode()
        except Exception:
            raise JoiningStoreError("import_unavailable") from None

    def begin_redeem(self, import_id: str) -> None:
        """Durable redeem INTENT before the secret is ever sent (R4): a lost
        reply leaves this visible as 'redeeming', never silently reverted to
        'previewed' (which would invite a blind resend of the secret)."""
        _check_import_id(import_id)
        with self._write() as conn:
            row = conn.execute("SELECT state FROM imports WHERE import_id=?", (import_id,)).fetchone()
            if row is None:
                raise JoiningStoreError("import_unavailable")
            if row["state"] != "previewed":
                raise JoiningStoreError("import_state_conflict")
            conn.execute(
                "UPDATE imports SET state='redeeming',updated_at=? WHERE import_id=?", (time.time(), import_id)
            )

    def mark_redeemed(self, import_id: str) -> None:
        _check_import_id(import_id)
        with self._write() as conn:
            row = conn.execute("SELECT state FROM imports WHERE import_id=?", (import_id,)).fetchone()
            if row is None:
                raise JoiningStoreError("import_unavailable")
            if row["state"] not in ("previewed", "redeeming", "redeemed"):
                raise JoiningStoreError("import_state_conflict")
            conn.execute(
                "UPDATE imports SET state='redeemed',updated_at=? WHERE import_id=?", (time.time(), import_id)
            )
            # R9: the invitation secret is no longer needed once redeem is
            # durably confirmed; delete it in THIS same transaction. Kept
            # only while pending ('previewed'/'redeeming') or ambiguous.
            conn.execute("DELETE FROM import_secrets WHERE import_id=?", (import_id,))

    def mark_redeem_ambiguous(self, import_id: str) -> None:
        """The reply to a redeem attempt was lost: record this visibly
        rather than leaving the import looking un-redeemed (R4)."""
        _check_import_id(import_id)
        with self._write() as conn:
            row = conn.execute("SELECT state FROM imports WHERE import_id=?", (import_id,)).fetchone()
            if row is None:
                raise JoiningStoreError("import_unavailable")
            if row["state"] not in ("redeeming", "redeem_ambiguous"):
                raise JoiningStoreError("import_state_conflict")
            conn.execute(
                "UPDATE imports SET state='redeem_ambiguous',updated_at=? WHERE import_id=?",
                (time.time(), import_id),
            )

    def resolve_redeem_ambiguous(self, import_id: str, *, redeemed: bool) -> None:
        """Explicit owner-directed recovery ONLY: never a blind automatic
        retry, never a second send of the secret. The owner must have
        authenticated, out-of-band, whether the host actually redeemed this
        exact invite before calling this (R4)."""
        _check_import_id(import_id)
        with self._write() as conn:
            row = conn.execute("SELECT state FROM imports WHERE import_id=?", (import_id,)).fetchone()
            if row is None:
                raise JoiningStoreError("import_unavailable")
            if row["state"] not in ("redeeming", "redeem_ambiguous"):
                raise JoiningStoreError("import_state_conflict")
            new_state = "redeemed" if redeemed else "previewed"
            conn.execute(
                "UPDATE imports SET state=?,updated_at=? WHERE import_id=?", (new_state, time.time(), import_id)
            )
            if redeemed:
                conn.execute("DELETE FROM import_secrets WHERE import_id=?", (import_id,))

    def mark_refused(self, import_id: str) -> None:
        _check_import_id(import_id)
        with self._write() as conn:
            row = conn.execute("SELECT state FROM imports WHERE import_id=?", (import_id,)).fetchone()
            if row is None:
                raise JoiningStoreError("import_unavailable")
            if row["state"] not in ("redeemed", "refused"):
                raise JoiningStoreError("import_state_conflict")
            conn.execute(
                "UPDATE imports SET state='refused',updated_at=? WHERE import_id=?", (time.time(), import_id)
            )

    # --- owner scope confirmation (out-of-band manual review before signing) ---

    def confirm_scope(self, import_id: str, scope: dict) -> None:
        """Owner-private manual provisioning: the owner reviews the exact
        room_id/member_id binding BEFORE any signature can be produced for
        it. Re-confirming the same import with a different scope conflicts;
        it never silently overwrites a prior confirmation."""
        _check_import_id(import_id)
        encoded = self._canonical(scope)
        with self._write() as conn:
            if conn.execute("SELECT 1 FROM imports WHERE import_id=?", (import_id,)).fetchone() is None:
                raise JoiningStoreError("import_unavailable")
            row = conn.execute("SELECT scope_json FROM confirmed_scope WHERE import_id=?", (import_id,)).fetchone()
            if row is None:
                conn.execute("INSERT INTO confirmed_scope VALUES(?,?)", (import_id, encoded))
            elif row["scope_json"] != encoded:
                raise JoiningStoreError("consent_scope_conflict")

    def confirmed_scope(self, import_id: str):
        _check_import_id(import_id)
        with self._read() as conn:
            row = conn.execute("SELECT scope_json FROM confirmed_scope WHERE import_id=?", (import_id,)).fetchone()
        return json.loads(row["scope_json"]) if row is not None else None

    # --- consent receipts ---

    def begin_consent(self, import_id: str, *, receipt: dict, signature: str) -> dict:
        """Persist the signed receipt ATOMICALLY BEFORE it is ever sent to
        the host (R3): state becomes 'consent_pending_delivery', distinct
        from the acknowledged 'consent_given'. A retry after a lost ack or a
        restart reuses these exact persisted bytes rather than re-signing;
        once persisted here the import can never be re-marked 'refused'
        (mark_refused only accepts 'redeemed'/'refused')."""
        _check_import_id(import_id)
        encoded = self._canonical(receipt)
        with self._write() as conn:
            row = conn.execute("SELECT state FROM imports WHERE import_id=?", (import_id,)).fetchone()
            if row is None:
                raise JoiningStoreError("import_unavailable")
            if row["state"] not in ("redeemed", "consent_pending_delivery", "consent_given"):
                raise JoiningStoreError("import_state_conflict")
            existing = conn.execute(
                "SELECT receipt_json,signature FROM consents WHERE import_id=?", (import_id,)
            ).fetchone()
            if existing is None:
                conn.execute("INSERT INTO consents VALUES(?,?,?)", (import_id, encoded, signature))
                result_signature = signature
            elif existing["receipt_json"] != encoded:
                raise JoiningStoreError("consent_scope_conflict")
            else:
                result_signature = existing["signature"]
            if row["state"] == "redeemed":
                conn.execute(
                    "UPDATE imports SET state='consent_pending_delivery',updated_at=? WHERE import_id=?",
                    (time.time(), import_id),
                )
        return {"receipt": receipt, "signature": result_signature}

    def mark_consent_delivered(self, import_id: str) -> None:
        """Explicit host acknowledgement observed: move the already-durable
        receipt from 'consent_pending_delivery' to 'consent_given'."""
        _check_import_id(import_id)
        with self._write() as conn:
            row = conn.execute("SELECT state FROM imports WHERE import_id=?", (import_id,)).fetchone()
            if row is None:
                raise JoiningStoreError("import_unavailable")
            if row["state"] not in ("consent_pending_delivery", "consent_given"):
                raise JoiningStoreError("import_state_conflict")
            conn.execute(
                "UPDATE imports SET state='consent_given',updated_at=? WHERE import_id=?", (time.time(), import_id)
            )

    def get_consent(self, import_id: str):
        _check_import_id(import_id)
        with self._read() as conn:
            row = conn.execute(
                "SELECT receipt_json,signature FROM consents WHERE import_id=?", (import_id,)
            ).fetchone()
        if row is None:
            return None
        return {"receipt": json.loads(row["receipt_json"]), "signature": row["signature"]}

    # --- target-side mint journal (durable, encrypted; never re-mints with
    # new IDs — ambiguity stays visible) ---

    def mint_state(self, scope_key: str):
        with self._read() as conn:
            row = conn.execute("SELECT state FROM material WHERE scope_key=?", (scope_key,)).fetchone()
        return row["state"] if row else None

    def mint_grant_id(self, scope_key: str):
        with self._read() as conn:
            row = conn.execute("SELECT grant_id FROM material WHERE scope_key=?", (scope_key,)).fetchone()
        return row["grant_id"] if row else None

    def mint_begin(self, scope_key: str) -> str:
        """Durable exclusive intent BEFORE any native call. Returns a fresh
        grant_id the first time; a scope that already has a journal entry is
        ambiguous and is never silently retried with a new grant_id."""
        grant_id = "grant-" + _secrets.token_hex(16)
        with self._write() as conn:
            if conn.execute("SELECT 1 FROM material WHERE scope_key=?", (scope_key,)).fetchone() is not None:
                raise JoiningStoreError("mint_ambiguous")
            conn.execute("INSERT INTO material VALUES(?,?,?,?,?)", (scope_key, grant_id, "intent", None, None))
        return grant_id

    def mint_complete(self, scope_key: str, value: bytes) -> None:
        nonce, ct = self._seal(scope_key, value)
        with self._write() as conn:
            updated = conn.execute(
                "UPDATE material SET state='ready',nonce=?,ciphertext=? WHERE scope_key=? AND state='intent'",
                (nonce, ct, scope_key),
            ).rowcount
            if updated != 1:
                raise JoiningStoreError("material_unavailable")

    def mint_load(self, scope_key: str) -> bytes:
        with self._read() as conn:
            row = conn.execute(
                "SELECT nonce,ciphertext FROM material WHERE scope_key=? AND state='ready'", (scope_key,)
            ).fetchone()
        if row is None:
            raise JoiningStoreError("material_unavailable")
        try:
            return self._open(scope_key, row["nonce"], row["ciphertext"])
        except Exception:
            raise JoiningStoreError("material_unavailable") from None


def _check_import_id(import_id: str) -> None:
    if not isinstance(import_id, str) or not _IMPORT_ID.fullmatch(import_id):
        raise JoiningStoreError("invalid_import_id")
