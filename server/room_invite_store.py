"""Private, local-owner lobby state. Not an HTTP ingress or execution authority.

Callers must authenticate the local owner, or the pinned connector peer, BEFORE
calling this module. Explicit database paths keep labs and installations isolated.
No native grant, relay credential, TLS key or admin token belongs in this store.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time
import unicodedata
import uuid


class InviteError(ValueError):
    """Machine code only: never interpolate remote values or secrets."""


@dataclass(frozen=True)
class IssuedInvite:
    invite_id: str
    expires_at: float
    secret: str = field(repr=False)


_SCHEMA = """
CREATE TABLE schema_version(version INTEGER NOT NULL);
INSERT INTO schema_version VALUES (2);
CREATE TABLE owner_scope(installation_id TEXT NOT NULL, profile TEXT NOT NULL);
CREATE TABLE lobbies(
 lobby_id TEXT PRIMARY KEY, room_id TEXT UNIQUE,
 planned_room_id TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
 state TEXT NOT NULL, roster_json TEXT NOT NULL, native_spec_json TEXT,
 created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE invitations(
 invite_id TEXT PRIMARY KEY, lobby_id TEXT NOT NULL REFERENCES lobbies(lobby_id),
 member_id TEXT NOT NULL, secret_sha256 TEXT NOT NULL UNIQUE, invitee_label TEXT NOT NULL,
 state TEXT NOT NULL, expires_at REAL NOT NULL, redeemed_at REAL,
 redeemed_by_endpoint TEXT, pinned_fingerprint TEXT, version INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE redemptions(
 invite_id TEXT PRIMARY KEY REFERENCES invitations(invite_id),
 endpoint_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
 target_install_id TEXT NOT NULL, target_profile TEXT NOT NULL,
 catalog_digest TEXT, policy_digest TEXT,
 host_decision TEXT NOT NULL, joining_consent TEXT NOT NULL, decided_at REAL
);
CREATE TABLE peer_links(
 lobby_id TEXT NOT NULL REFERENCES lobbies(lobby_id), member_id TEXT NOT NULL,
 state TEXT NOT NULL, PRIMARY KEY(lobby_id,member_id)
);
"""


def _text(value, *, pattern=None, limit=128):
    if not isinstance(value, str) or not 0 < len(value) <= limit or not value.strip():
        return False
    return not any(unicodedata.category(c).startswith("C") for c in value) and (pattern is None or re.fullmatch(pattern, value) is not None)


def _validate_roster(name, roster):
    token = r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}"
    profile = r"[a-z0-9][a-z0-9_-]{0,63}"
    if not _text(name) or not isinstance(roster, list) or not 2 <= len(roster) <= 6:
        raise InviteError("invalid_lobby")
    ids, handles, targets = set(), {"all", "everyone"}, set()
    local_count = 0
    for member in roster:
        if not isinstance(member, dict) or not set(member) <= {"member_id", "profile", "handle", "display_name", "target"}:
            raise InviteError("invalid_lobby")
        if any(not _text(member.get(k), pattern=p) for k, p in (("member_id", token), ("profile", profile), ("handle", token))):
            raise InviteError("invalid_lobby")
        if member["member_id"].casefold() in ids or member["handle"].casefold() in handles:
            raise InviteError("invalid_lobby")
        ids.add(member["member_id"].casefold())
        handles.add(member["handle"].casefold())
        if "display_name" in member and not _text(member["display_name"]):
            raise InviteError("invalid_lobby")
        if "target" not in member:
            key = ("local", member["profile"])
            if key in targets:
                raise InviteError("invalid_lobby")
            targets.add(key)
            local_count += 1
            continue
        target = member["target"]
        if not isinstance(target, dict) or not {"kind", "peer_id", "installation_id", "profile"} <= set(target) or not set(target) <= {"kind", "peer_id", "installation_id", "profile", "capability_digest"}:
            raise InviteError("invalid_lobby")
        fields = (("peer_id", token), ("installation_id", r"install:[0-9a-f]{32}"), ("profile", profile))
        if target["kind"] != "peer" or target["profile"] != member["profile"] or any(not _text(target.get(k), pattern=p) for k, p in fields):
            raise InviteError("invalid_lobby")
        key = ("peer", target["peer_id"], target["installation_id"], target["profile"])
        if key in targets:
            raise InviteError("invalid_lobby")
        targets.add(key)
        if "capability_digest" in target and not _text(target["capability_digest"], pattern=r"[0-9a-f]{64}"):
            raise InviteError("invalid_lobby")
    if not 0 < local_count < len(roster):
        raise InviteError("invalid_lobby")


class RoomInviteStore:
    def __init__(self, path: Path, *, clock=time.time):
        self.path = Path(path)
        self.clock = clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        with self._connection() as conn:
            exists = conn.execute("SELECT 1 FROM sqlite_master WHERE name='schema_version'").fetchone()
            initialized = False
            if not exists:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='schema_version'").fetchone():
                        for statement in _SCHEMA.split(";"):
                            if statement.strip():
                                conn.execute(statement)
                        initialized = True
                    conn.commit()
                except BaseException:
                    conn.rollback()
                    raise
            versions = conn.execute("SELECT version FROM schema_version").fetchall()
            if len(versions) != 1 or type(versions[0][0]) is not int or versions[0][0] < 1:
                raise InviteError("invalid_schema")
            if versions[0][0] == 1:
                # Both historical and the unversioned P1 layout used version 1.
                # Neither proves owner adoption or approval of a full policy.
                # Do not write, infer ownership, or silently retrofit consent.
                raise InviteError("legacy_schema_recovery_required")
            self.read_only = versions[0][0] > 2
            if initialized:
                conn.execute("PRAGMA journal_mode=WAL")

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
    def _write(self):
        if self.read_only:
            raise InviteError("schema_too_new")
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                version = conn.execute("SELECT version FROM schema_version").fetchall()
                if len(version) != 1 or version[0][0] > 2:
                    raise InviteError("schema_too_new")
                if version[0][0] != 2:
                    raise InviteError("legacy_schema_recovery_required")
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def create_lobby(self, *, name: str, roster: list[dict]) -> dict:
        _validate_roster(name, roster)
        # Match native display-label semantics BEFORE freezing any spec. IDs are
        # validated literally and are never stripped, case-folded or repaired.
        name, roster = name.strip(), copy.deepcopy(roster)
        for member in roster:
            if "display_name" in member:
                member["display_name"] = member["display_name"].strip()
        lobby_id = "lobby-" + uuid.uuid4().hex
        now = self.clock()
        with self._write() as conn:
            conn.execute("INSERT INTO lobbies VALUES(?,NULL,?,?,'open',?,NULL,?,?)",
                         (lobby_id, "invite-room-" + uuid.uuid4().hex, name, json.dumps(roster), now, now))
        return self.get_lobby(lobby_id)

    def get_lobby(self, lobby_id: str) -> dict:
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM lobbies WHERE lobby_id=?", (lobby_id,)).fetchone()
            if row is None:
                raise InviteError("lobby_unavailable")
            return {key: value for key, value in dict(row).items() if key not in {"roster_json", "native_spec_json"}} | {"roster": json.loads(row["roster_json"])}

    def issue_invite(self, lobby_id: str, member_id: str, *, invitee_label: str, ttl_seconds=86400) -> IssuedInvite:
        if not _text(invitee_label):
            raise InviteError("invalid_invite")
        if type(ttl_seconds) not in (int, float) or not math.isfinite(ttl_seconds) or not 0 < ttl_seconds <= 604800:
            raise InviteError("invalid_invite")
        invite_id, secret = "invite-" + uuid.uuid4().hex, secrets.token_urlsafe(32)
        expiry = self.clock() + ttl_seconds
        with self._write() as conn:
            lobby = self._open_lobby(conn, lobby_id)
            self._peer_member(lobby, member_id)
            active = conn.execute("SELECT 1 FROM invitations i JOIN redemptions r USING(invite_id) WHERE i.lobby_id=? AND i.member_id=? AND r.host_decision!='denied' AND r.joining_consent!='refused'", (lobby_id, member_id)).fetchone()
            if active:
                raise InviteError("member_already_redeemed")
            conn.execute("UPDATE invitations SET state='revoked',version=version+1 WHERE lobby_id=? AND member_id=? AND state='open'", (lobby_id, member_id))
            conn.execute("INSERT INTO invitations(invite_id,lobby_id,member_id,secret_sha256,invitee_label,state,expires_at) VALUES(?,?,?,?,?,'open',?)",
                         (invite_id, lobby_id, member_id, hashlib.sha256(secret.encode()).hexdigest(), invitee_label, expiry))
        return IssuedInvite(invite_id, expiry, secret)

    def revoke_invite(self, lobby_id: str, invite_id: str) -> bool:
        with self._write() as conn:
            self._open_lobby(conn, lobby_id)
            row = conn.execute("SELECT state FROM invitations WHERE lobby_id=? AND invite_id=?", (lobby_id, invite_id)).fetchone()
            if row is None or row["state"] not in {"open", "revoked"}:
                raise InviteError("invite_unavailable")
            conn.execute("UPDATE invitations SET state='revoked',version=version+1 WHERE invite_id=? AND state='open'", (invite_id,))
        return True

    @staticmethod
    def _open_lobby(conn, lobby_id):
        row = conn.execute("SELECT * FROM lobbies WHERE lobby_id=? AND state='open'", (lobby_id,)).fetchone()
        if row is None:
            raise InviteError("invite_unavailable")
        return row

    @staticmethod
    def _peer_member(lobby, member_id):
        member = next((m for m in json.loads(lobby["roster_json"]) if m["member_id"] == member_id and m.get("target")), None)
        if member is None:
            raise InviteError("invite_unavailable")
        return member

    @staticmethod
    def _normalize_binding(binding):
        if not isinstance(binding, dict) or set(binding) not in ({"endpoint_id", "fingerprint", "target_install_id", "target_profile"}, {"endpoint_id", "fingerprint", "target_install_id", "target_profile", "catalog_digest", "policy_digest"}):
            raise InviteError("invalid_peer_binding")
        binding = copy.deepcopy(binding)
        legacy = "catalog_digest" not in binding
        binding.setdefault("catalog_digest", None)
        binding.setdefault("policy_digest", None)
        patterns = {"endpoint_id": r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", "fingerprint": r"[0-9a-f]{64}",
                    "target_install_id": r"install:[0-9a-f]{32}", "target_profile": r"[a-z0-9][a-z0-9_-]{0,63}",
                    "catalog_digest": r"[0-9a-f]{64}", "policy_digest": r"[0-9a-f]{64}"}
        if any(not isinstance(binding[key], str) or not re.fullmatch(pattern, binding[key])
               for key, pattern in patterns.items() if not (legacy and key in {"catalog_digest", "policy_digest"})):
            raise InviteError("invalid_peer_binding")
        return binding

    @staticmethod
    def _validate_binding(binding):
        """Compatibility validator for the spike's private control adapter."""
        RoomInviteStore._normalize_binding(binding)

    def redeem(self, lobby_id: str, invite_id: str, secret: str, *, binding: dict) -> dict:
        binding = self._normalize_binding(binding)
        if not isinstance(secret, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", secret):
            raise InviteError("invite_unavailable")
        with self._write() as conn:
            lobby = self._open_lobby(conn, lobby_id)
            invite = conn.execute("SELECT member_id FROM invitations WHERE lobby_id=? AND invite_id=?", (lobby_id, invite_id)).fetchone()
            if invite is None:
                raise InviteError("invite_unavailable")
            target = self._peer_member(lobby, invite["member_id"])["target"]
            if (binding["target_install_id"], binding["target_profile"]) != (target["installation_id"], target["profile"]):
                raise InviteError("invite_unavailable")
            result = conn.execute(
                "UPDATE invitations SET state='redeemed',redeemed_at=?,redeemed_by_endpoint=?,pinned_fingerprint=?,version=version+1 "
                "WHERE lobby_id=? AND invite_id=? AND state='open' AND expires_at>? AND secret_sha256=?",
                (self.clock(), binding["endpoint_id"], binding["fingerprint"], lobby_id, invite_id,
                 self.clock(), hashlib.sha256(secret.encode()).hexdigest()))
            if result.rowcount != 1:
                raise InviteError("invite_unavailable")
            conn.execute("INSERT INTO redemptions VALUES(?,?,?,?,?,?,?,'pending','pending',NULL)",
                         (invite_id, binding["endpoint_id"], binding["fingerprint"],
                          binding["target_install_id"], binding["target_profile"],
                          binding["catalog_digest"], binding["policy_digest"]))
        return self.get_redemption(lobby_id, invite_id)

    def get_redemption(self, lobby_id: str, invite_id: str) -> dict:
        with self._connection() as conn:
            row = conn.execute("SELECT i.invite_id,i.member_id,r.* FROM invitations i JOIN redemptions r USING(invite_id) WHERE i.lobby_id=? AND i.invite_id=?",
                               (lobby_id, invite_id)).fetchone()
            if row is None:
                raise InviteError("invite_unavailable")
            data = dict(row)
            data["status"] = self._consent_status(data)
            return data

    def list_redemptions(self, lobby_id: str) -> list[dict]:
        """Read-only projection of all redemptions for a lobby. Safe for product views."""
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT i.invite_id,i.member_id,r.* FROM invitations i "
                "JOIN redemptions r USING(invite_id) WHERE i.lobby_id=? ORDER BY i.member_id",
                (lobby_id,)).fetchall()
        return [dict(row) | {"status": self._consent_status(dict(row))} for row in rows]

    @staticmethod
    def _consent_status(row):
        if row["host_decision"] == "denied":
            return "denied"
        if row["joining_consent"] == "refused":
            return "refused"
        if row["host_decision"] == "pending":
            return "awaiting_host_approval"
        return "ready" if row["joining_consent"] == "given" else "consent_required"

    def decide_host(self, lobby_id: str, invite_id: str, *, binding: dict, approved: bool) -> dict:
        if type(approved) is not bool:
            raise InviteError("invalid_decision")
        return self._decision(lobby_id, invite_id, binding, "host_decision", "approved" if approved else "denied")

    def consent_joining(self, lobby_id: str, invite_id: str, *, binding: dict, consented: bool) -> dict:
        if type(consented) is not bool:
            raise InviteError("invalid_decision")
        return self._decision(lobby_id, invite_id, binding, "joining_consent", "given" if consented else "refused")

    def _decision(self, lobby_id, invite_id, binding, column, value):
        binding = self._normalize_binding(binding)
        with self._write() as conn:
            self._open_lobby(conn, lobby_id)
            row = conn.execute("SELECT r.* FROM invitations i JOIN redemptions r USING(invite_id) WHERE i.lobby_id=? AND i.invite_id=?", (lobby_id, invite_id)).fetchone()
            if row is None or any(row[key] != binding[key] for key in binding):
                raise InviteError("fingerprint_mismatch")
            if column == "joining_consent" and row["host_decision"] != "approved":
                raise InviteError("host_approval_required")
            if row[column] not in {"pending", value}:
                raise InviteError("decision_final")
            conn.execute(f"UPDATE redemptions SET {column}=?,decided_at=? WHERE invite_id=?", (value, self.clock(), invite_id))
        return self.get_redemption(lobby_id, invite_id)

    def seal_lobby(self, lobby_id: str) -> dict:
        with self._write() as conn:
            row = conn.execute("SELECT * FROM lobbies WHERE lobby_id=?", (lobby_id,)).fetchone()
            if row is None or row["state"] not in {"open", "sealed", "created"}:
                raise InviteError("lobby_unavailable")
            peers = [dict(r) for r in conn.execute("SELECT i.member_id,r.* FROM invitations i JOIN redemptions r USING(invite_id) WHERE i.lobby_id=? AND r.host_decision='approved' AND r.joining_consent='given' ORDER BY i.member_id", (lobby_id,))]
            expected = {m["member_id"] for m in json.loads(row["roster_json"]) if m.get("target")}
            if len(peers) != len(expected) or {p["member_id"] for p in peers} != expected:
                raise InviteError("consent_required")
            if row["state"] == "open":
                conn.execute("UPDATE lobbies SET state='sealed',updated_at=? WHERE lobby_id=?", (self.clock(), lobby_id))
        return self.get_lobby(lobby_id) | {"peers": peers}

    def pin_native_spec(self, lobby_id: str, spec: dict) -> None:
        encoded = json.dumps(spec, sort_keys=True, separators=(",", ":"))
        with self._write() as conn:
            row = conn.execute("SELECT * FROM lobbies WHERE lobby_id=?", (lobby_id,)).fetchone()
            if row is None or row["state"] not in {"sealed", "created"} or spec.get("room_id") != row["planned_room_id"]:
                raise InviteError("lobby_unavailable")
            if row["native_spec_json"] is not None and row["native_spec_json"] != encoded:
                raise InviteError("native_spec_conflict")
            conn.execute("UPDATE lobbies SET native_spec_json=? WHERE lobby_id=?", (encoded, lobby_id))

    def mark_created(self, lobby_id: str, room_id: str) -> None:
        with self._write() as conn:
            row = conn.execute("SELECT * FROM lobbies WHERE lobby_id=?", (lobby_id,)).fetchone()
            if row is None or row["state"] not in {"sealed", "created"} or row["planned_room_id"] != room_id or row["native_spec_json"] is None:
                raise InviteError("native_spec_conflict")
            conn.execute("UPDATE lobbies SET room_id=?,state='created',updated_at=? WHERE lobby_id=?", (room_id, self.clock(), lobby_id))

    def mark_peer_registered(self, lobby_id: str, member_id: str) -> None:
        with self._write() as conn:
            row = conn.execute("SELECT * FROM lobbies WHERE lobby_id=? AND state='created'", (lobby_id,)).fetchone()
            if row is None:
                raise InviteError("lobby_unavailable")
            self._peer_member(row, member_id)
            conn.execute("INSERT INTO peer_links(lobby_id,member_id,state) VALUES(?,?,'registered') ON CONFLICT(lobby_id,member_id) DO UPDATE SET state='registered'", (lobby_id, member_id))

    def preview(self, invite_id: str, secret: str) -> dict:
        with self._connection() as conn:
            row = conn.execute("SELECT l.name,i.expires_at FROM invitations i JOIN lobbies l USING(lobby_id) WHERE invite_id=? AND secret_sha256=? AND i.state='open' AND expires_at>?",
                               (invite_id, hashlib.sha256(secret.encode()).hexdigest(), self.clock())).fetchone()
            if row is None:
                raise InviteError("invite_unavailable")
            return dict(row)

    def bind_owner_scope(self, installation_id: str, profile: str) -> None:
        """Bind this explicit store path once; a restart cannot silently rebind it."""
        if not isinstance(installation_id, str) or not isinstance(profile, str):
            raise InviteError("invalid_owner_scope")
        with self._write() as conn:
            row = conn.execute("SELECT installation_id,profile FROM owner_scope").fetchone()
            if row is None:
                conn.execute("INSERT INTO owner_scope VALUES(?,?)", (installation_id, profile))
            elif row["installation_id"] != installation_id or row["profile"] != profile:
                raise InviteError("owner_scope_mismatch")
