"""P3b.1a-2: append-only pairing decision journal, ``peers.json`` projector and journal-backed ``LinkAuthority``.

The journal is the single source of truth (design section 1, R1-02/R1-03); ``peers.json`` is only a projection of it.

* One hash-chained JSON-lines file in an owner-private directory reached with ``private_fs`` (no-follow, 0700/0600,
  single link).  Every record is one ``write`` followed by ``fsync``; the whole file is re-verified on every open
  (sequence, previous hash, canonical bytes, strict state machine).  A journal that fails verification is NEVER
  repaired or truncated: every operation fails closed with ``journal_corrupt``.
* A single writer: an ``flock`` on a stable sibling lock file.  Lock order is always journal lock, then the registry
  lock taken inside ``RegistrySource.replace``; no network I/O is performed while either is held.
* ``protocol_state`` (what the two owners decided) is separate from ``projection_revision`` / ``published_revision``
  (what has been written to ``peers.json``).  A crash between the two is reconciled from the journal alone; a
  ``peers.json`` that differs from the journal projection is never trusted nor overwritten (fail closed).
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Mapping, NoReturn, Optional

from invite_rooms import private_fs as fs
from invite_rooms.control.dispatcher import LinkState
from invite_rooms.peer_registry import PeerRegistryError, RegistrySource

E_CORRUPT = "journal_corrupt"
E_STORAGE = "journal_storage_unavailable"
E_BUSY = "journal_busy"
E_REFUSED = "journal_transition_refused"
E_PROJECTION = "journal_projection_mismatch"

JOURNAL_FILE = "pairing.journal"
LOCK_FILE = ".pairing.lock"
FILE_MAX = 16 * 1024 * 1024
RECORD_MAX = 64 * 1024
LOCK_WAIT = 5.0
GENESIS = "0" * 64
FAILURE_WINDOW = 24 * 3600
FAILURE_BUDGET = 3

# Test seam: ``callable(point)`` invoked at every durable step; a raised BaseException simulates power loss.
_FAULT: Optional[Callable[[str], None]] = None


class JournalError(ValueError):
    """Sanitized machine code only (``str(exc) == code``); never carries paths, keys, codes or SAS."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _fault(point: str) -> None:
    if _FAULT is not None:
        _FAULT(point)


def _dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _no_dups(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate")
        out[key] = value
    return out


def _reject_constant(_name):
    raise ValueError("constant")


def record_hash(rec: Mapping[str, Any]) -> str:
    body = {k: rec[k] for k in ("seq", "prev", "ts", "type", "data")}
    return hashlib.sha256(_dumps(body).encode("ascii")).hexdigest()


# --------------------------------------------------------------------------------------------------------------
# State machine (pure fold over the verified records)
# --------------------------------------------------------------------------------------------------------------

_HOST_STATES = ("offered", "accept_imported", "revealed", "confirmed", "acknowledged", "aborted")
_JOIN_STATES = ("offer_imported", "accepted", "reveal_imported", "confirmed", "acknowledged", "aborted")
_PENDING_STATES = {"offered", "accept_imported", "revealed", "offer_imported", "accepted", "reveal_imported"}
_PEER_KEYS = {"peer_id", "installation_id", "profile", "fingerprint", "ca_pem", "consent_public_key_b64", "catalog",
              "catalog_digest", "policy_digest"}
_DATA_KEYS = {
    "offer_created": {"id", "expires_at", "nonce", "r_a", "offer", "offer_hash"},
    "accept_imported": {"id", "accept", "accept_hash", "endpoint_id", "peer"},
    "revealed": {"id", "reveal"},
    "offer_imported": {"id", "expires_at", "offer", "offer_hash", "peer"},
    "accepted": {"id", "accept", "accept_hash", "r_b", "endpoint_id"},
    "reveal_imported": {"id", "reveal"},
    "confirmed": {"id", "generation"},
    "ack_sent": {"id", "ack"},
    "acknowledged": {"id", "ack_recv", "ack_sent"},
    "aborted": {"id", "reason"},
    "unpaired": {"peer_id", "endpoint_id", "generation"},
    "published": {"revision"},
}
_ABORT_REASONS = {"cancelled", "sas_mismatch", "conflict"}


def _refuse() -> NoReturn:
    raise JournalError(E_REFUSED)


class State:
    """Current view derived from the journal.  ``apply`` is the ONLY mutator and is strict."""

    def __init__(self) -> None:
        self.seq = 0
        self.last_hash = GENESIS
        self.sessions: dict[str, dict] = {}
        self.order: list[str] = []
        self.links: dict[str, dict] = {}          # endpoint_id -> link
        self.peer_gen: dict[str, int] = {}
        self.offer_hashes: set[str] = set()
        self.accept_hashes: set[str] = set()
        self.dead_endpoints: set[str] = set()
        self.failures: list[int] = []
        self.projection_revision = 0
        self.published_revision = 0

    # ---- chain ----------------------------------------------------------------------------------------------
    def apply(self, rec: Mapping[str, Any]) -> None:
        if (set(rec) != {"seq", "prev", "ts", "type", "data", "hash"} or rec["seq"] != self.seq + 1
                or rec["prev"] != self.last_hash or type(rec["ts"]) is not int or rec["hash"] != record_hash(rec)):
            _refuse()
        kind, data = rec["type"], rec["data"]
        if kind not in _DATA_KEYS or not isinstance(data, dict) or set(data) != _DATA_KEYS[kind]:
            _refuse()
        getattr(self, "_on_" + kind)(data, rec["ts"])
        self.seq = rec["seq"]
        self.last_hash = rec["hash"]

    # ---- helpers --------------------------------------------------------------------------------------------
    def _session(self, data: Mapping, role: str, *states: str) -> dict:
        sess = self.sessions.get(data["id"])
        if sess is None or sess["role"] != role or sess["state"] not in states:
            _refuse()
        return sess

    @staticmethod
    def _check_peer(peer: Any) -> None:
        if not isinstance(peer, dict) or set(peer) != _PEER_KEYS:
            _refuse()

    def _new_link(self, endpoint_id: str, sess: dict) -> None:
        if (not isinstance(endpoint_id, str) or endpoint_id in self.links or endpoint_id in self.dead_endpoints):
            _refuse()
        self.links[endpoint_id] = {"endpoint_id": endpoint_id, "session_id": sess["id"], "role": sess["role"],
                                   "peer": sess["peer"], "generation": None, "protocol_state": "pending"}
        sess["endpoint_id"] = endpoint_id

    def _drop_pending_link(self, sess: dict) -> None:
        endpoint = sess.get("endpoint_id")
        link = self.links.get(endpoint) if endpoint else None
        if link is not None and link["protocol_state"] == "pending":
            del self.links[endpoint]
            self.dead_endpoints.add(endpoint)

    # ---- transitions ----------------------------------------------------------------------------------------
    def _on_offer_created(self, d, ts):
        if d["id"] in self.sessions or d["offer_hash"] in self.offer_hashes or type(d["expires_at"]) is not int:
            _refuse()
        self.offer_hashes.add(d["offer_hash"])
        self.sessions[d["id"]] = {"id": d["id"], "role": "host", "state": "offered", "created_at": ts,
                                  "expires_at": d["expires_at"], "offer": d["offer"], "offer_hash": d["offer_hash"],
                                  "nonce": d["nonce"], "r_a": d["r_a"], "accept": None, "accept_hash": None,
                                  "reveal": None, "r_b": None, "peer": None, "endpoint_id": None,
                                  "generation": None, "ack_sent": None, "ack_recv": None, "abort_reason": None}
        self.order.append(d["id"])

    def _on_accept_imported(self, d, ts):
        sess = self._session(d, "host", "offered")
        self._check_peer(d["peer"])
        if d["accept_hash"] in self.accept_hashes:
            _refuse()
        self.accept_hashes.add(d["accept_hash"])
        sess.update(state="accept_imported", accept=d["accept"], accept_hash=d["accept_hash"], peer=d["peer"])
        self._new_link(d["endpoint_id"], sess)

    def _on_revealed(self, d, ts):
        self._session(d, "host", "accept_imported").update(state="revealed", reveal=d["reveal"])

    def _on_offer_imported(self, d, ts):
        if d["id"] in self.sessions or d["offer_hash"] in self.offer_hashes or type(d["expires_at"]) is not int:
            _refuse()
        self._check_peer(d["peer"])
        self.offer_hashes.add(d["offer_hash"])
        self.sessions[d["id"]] = {"id": d["id"], "role": "joining", "state": "offer_imported", "created_at": ts,
                                  "expires_at": d["expires_at"], "offer": d["offer"], "offer_hash": d["offer_hash"],
                                  "nonce": None, "r_a": None, "accept": None, "accept_hash": None, "reveal": None,
                                  "r_b": None, "peer": d["peer"], "endpoint_id": None, "generation": None,
                                  "ack_sent": None, "ack_recv": None, "abort_reason": None}
        self.order.append(d["id"])

    def _on_accepted(self, d, ts):
        sess = self._session(d, "joining", "offer_imported")
        if d["accept_hash"] in self.accept_hashes:
            _refuse()
        self.accept_hashes.add(d["accept_hash"])
        sess.update(state="accepted", accept=d["accept"], accept_hash=d["accept_hash"], r_b=d["r_b"])
        self._new_link(d["endpoint_id"], sess)

    def _on_reveal_imported(self, d, ts):
        self._session(d, "joining", "accepted").update(state="reveal_imported", reveal=d["reveal"])

    def _on_confirmed(self, d, ts):
        sess = self.sessions.get(d["id"])
        if sess is None:
            _refuse()
        want = "revealed" if sess["role"] == "host" else "reveal_imported"
        if sess["state"] != want or type(d["generation"]) is not int:
            _refuse()
        link = self.links.get(sess["endpoint_id"])
        peer_id = sess["peer"]["peer_id"]
        if link is None or link["protocol_state"] != "pending" or d["generation"] != self.peer_gen.get(peer_id, 0) + 1:
            _refuse()
        for other in self.links.values():  # a re-pair supersedes any older active link of the same peer
            if (other is not link and other["peer"]["peer_id"] == peer_id
                    and other["protocol_state"] in ("confirmed", "acknowledged")):
                other["protocol_state"], other["generation"] = "revoked", d["generation"]
                self.dead_endpoints.add(other["endpoint_id"])
        sess.update(state="confirmed", generation=d["generation"])
        link.update(protocol_state="confirmed", generation=d["generation"])
        self.peer_gen[peer_id] = d["generation"]
        if sess["role"] == "host":
            self.projection_revision += 1

    def _active_link(self, sess: dict) -> dict:
        link = self.links.get(sess["endpoint_id"]) if sess["endpoint_id"] else None
        if link is None or link["protocol_state"] not in ("confirmed", "acknowledged"):
            _refuse()
        return link

    def _on_ack_sent(self, d, ts):
        sess = self._session(d, "host", "confirmed")
        self._active_link(sess)
        if sess["ack_sent"] is not None or not isinstance(d["ack"], dict):
            _refuse()
        sess["ack_sent"] = d["ack"]

    def _on_acknowledged(self, d, ts):
        sess = self.sessions.get(d["id"])
        if sess is None or sess["state"] != "confirmed":
            _refuse()
        link = self._active_link(sess)
        if not isinstance(d["ack_recv"], dict):
            _refuse()
        if sess["role"] == "host":
            if sess["ack_sent"] is None or d["ack_sent"] is not None:
                _refuse()
        else:
            if not isinstance(d["ack_sent"], dict) or sess["ack_sent"] is not None:
                _refuse()
            sess["ack_sent"] = d["ack_sent"]
        sess.update(state="acknowledged", ack_recv=d["ack_recv"])
        link["protocol_state"] = "acknowledged"

    def _on_aborted(self, d, ts):
        sess = self.sessions.get(d["id"])
        if sess is None or sess["state"] in ("confirmed", "acknowledged", "aborted") or d["reason"] not in _ABORT_REASONS:
            _refuse()
        self._drop_pending_link(sess)
        sess.update(state="aborted", abort_reason=d["reason"])
        if d["reason"] == "sas_mismatch":
            self.failures.append(ts)

    def _on_unpaired(self, d, ts):
        link = self.links.get(d["endpoint_id"])
        if (link is None or link["protocol_state"] not in ("confirmed", "acknowledged")
                or link["peer"]["peer_id"] != d["peer_id"] or type(d["generation"]) is not int
                or d["generation"] != self.peer_gen.get(d["peer_id"], 0) + 1):
            _refuse()
        link.update(protocol_state="revoked", generation=d["generation"])
        self.dead_endpoints.add(d["endpoint_id"])
        self.peer_gen[d["peer_id"]] = d["generation"]
        if link["role"] == "host":
            self.projection_revision += 1

    def _on_published(self, d, ts):
        rev = d["revision"]
        if type(rev) is not int or not self.published_revision <= rev <= self.projection_revision:
            _refuse()
        self.published_revision = rev

    # ---- queries --------------------------------------------------------------------------------------------
    def pending_sessions(self, now: int) -> list[dict]:
        return [s for s in self.sessions.values() if s["state"] in _PENDING_STATES and s["expires_at"] >= now]

    def cooldown_active(self, now: int) -> bool:
        return sum(1 for t in self.failures if now - t < FAILURE_WINDOW) >= FAILURE_BUDGET

    def active_link_for_peer(self, peer_id: str) -> Optional[dict]:
        for link in self.links.values():
            if link["peer"]["peer_id"] == peer_id and link["protocol_state"] in ("confirmed", "acknowledged"):
                return link
        return None

    def active_links(self) -> list[dict]:
        return sorted((l for l in self.links.values() if l["protocol_state"] in ("confirmed", "acknowledged")),
                      key=lambda l: l["peer"]["peer_id"])

    def link_generation(self, link: dict) -> int:
        if link["generation"] is not None:
            return link["generation"]
        return self.peer_gen.get(link["peer"]["peer_id"], 0) + 1  # prospective (pending)

    def projection_entries(self) -> list[dict]:
        out = []
        for link in self.active_links():
            if link["role"] != "host":
                continue
            p = link["peer"]
            out.append({"peer_id": p["peer_id"], "installation_id": p["installation_id"], "profile": p["profile"],
                        "fingerprint": p["fingerprint"], "endpoint_id": link["endpoint_id"],
                        "catalog": p["catalog"], "consent_public_key_b64": p["consent_public_key_b64"],
                        "tls_ca_pem": p["ca_pem"], "pairing_generation": link["generation"]})
        return out


def _verify(raw: bytes) -> State:
    state = State()
    if not raw:
        return state
    if not raw.endswith(b"\n"):
        raise JournalError(E_CORRUPT)
    try:
        for line in raw[:-1].split(b"\n"):
            if len(line) > RECORD_MAX:
                raise JournalError(E_CORRUPT)
            text = line.decode("ascii")
            rec = json.loads(text, object_pairs_hook=_no_dups, parse_constant=_reject_constant)
            if not isinstance(rec, dict) or _dumps(rec) != text:
                raise JournalError(E_CORRUPT)
            state.apply(rec)
    except JournalError:
        raise JournalError(E_CORRUPT) from None
    except (ValueError, TypeError, KeyError, UnicodeDecodeError, RecursionError):
        raise JournalError(E_CORRUPT) from None
    return state


# --------------------------------------------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------------------------------------------

class Txn:
    """Exclusive transaction: the journal lock is held, ``state`` is the verified current view."""

    def __init__(self, jfd: int, state: State, clock: Callable[[], float]):
        self._jfd, self.state, self._clock = jfd, state, clock

    def now(self) -> int:
        return int(self._clock())

    def append(self, kind: str, data: Mapping[str, Any]) -> None:
        rec = {"seq": self.state.seq + 1, "prev": self.state.last_hash, "ts": self.now(), "type": kind,
               "data": json.loads(_dumps(data))}
        rec["hash"] = record_hash(rec)
        line = (_dumps(rec) + "\n").encode("ascii")
        if len(line) > RECORD_MAX:
            _refuse()
        self.state.apply(rec)  # strict transition check BEFORE anything touches the disk
        _fault("before_append:" + kind)
        try:
            fs.write_all(self._jfd, line)
            _fault("after_write:" + kind)
            fs.fsync(self._jfd)
        except OSError:
            raise JournalError(E_STORAGE) from None
        _fault("after_fsync:" + kind)


class Journal:
    def __init__(self, directory, *, anchor=None, lock_wait: float = LOCK_WAIT, clock: Callable[[], float] = time.time):
        self.directory = Path(directory)
        self.anchor = Path(anchor) if anchor is not None else None
        self.lock_wait = lock_wait
        self.clock = clock

    @contextmanager
    def _locked(self, *, exclusive: bool):
        dfd = lockfd = jfd = None
        try:
            try:
                dfd = fs.open_private_dir(self.directory, self.anchor, create=True)
                lockfd = fs.open_regular(dfd, LOCK_FILE, create=True, write=True)
            except (fs.PrivateStorageError, OSError):
                raise JournalError(E_STORAGE) from None
            mode = (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB
            deadline = time.monotonic() + self.lock_wait
            while True:
                try:
                    fcntl.flock(lockfd, mode)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise JournalError(E_BUSY) from None
                    time.sleep(0.005)
            try:
                jfd = fs.open_regular(dfd, JOURNAL_FILE, create=True, write=True)
                raw = fs.read_all(jfd, FILE_MAX)
            except (fs.PrivateStorageError, OSError):
                raise JournalError(E_STORAGE) from None
            state = _verify(raw)
            if not raw:
                try:
                    fs.fsync(dfd)  # the (new) journal entry itself must be durable
                except OSError:
                    raise JournalError(E_STORAGE) from None
            yield jfd, state
        finally:
            for fd in (jfd, lockfd, dfd):
                if fd is not None:
                    try:
                        os.close(fd)
                    except OSError:
                        pass

    @contextmanager
    def transaction(self):
        with self._locked(exclusive=True) as (jfd, state):
            yield Txn(jfd, state, self.clock)

    def snapshot(self) -> State:
        with self._locked(exclusive=False) as (_jfd, state):
            return state


# --------------------------------------------------------------------------------------------------------------
# Projector
# --------------------------------------------------------------------------------------------------------------

class Projector:
    """Publishes ``peers.json`` from the journal (never the other way round)."""

    def __init__(self, peers_path):
        self.path = Path(peers_path)
        self._source = RegistrySource(self.path, lambda path: None)

    def _read_file(self):
        try:
            fd = fs.open_regular(fs.open_private_dir(self.path.parent, None, create=False), self.path.name)
        except fs.PrivateStorageError:
            return None
        try:
            return json.loads(fs.read_all(fd, 4 * 1024 * 1024).decode("utf-8"))
        except (ValueError, UnicodeDecodeError, fs.PrivateStorageError):
            raise JournalError(E_PROJECTION) from None
        finally:
            os.close(fd)

    def reconcile(self, txn: Txn) -> bool:
        """Under the journal lock: publish when the journal is ahead, otherwise verify.  Returns True if published."""
        state = txn.state
        expected = state.projection_entries()
        if state.published_revision < state.projection_revision:
            _fault("project:before_replace")
            try:
                self._source.replace(expected)
            except (PeerRegistryError, OSError):
                raise JournalError(E_STORAGE) from None
            _fault("project:after_replace")
            txn.append("published", {"revision": state.projection_revision})
            return True
        current = self._read_file()
        if (current if current is not None else []) != expected:
            raise JournalError(E_PROJECTION)  # manual edit / foreign writer: never trusted, never overwritten
        return False


# --------------------------------------------------------------------------------------------------------------
# LinkAuthority (P3b.3a Protocol)
# --------------------------------------------------------------------------------------------------------------

def binding_for(link: Mapping) -> dict:
    p = link["peer"]
    return {"endpoint_id": link["endpoint_id"], "fingerprint": p["fingerprint"], "target_install_id": p["installation_id"],
            "target_profile": p["profile"], "catalog_digest": p["catalog_digest"], "policy_digest": p["policy_digest"]}


class JournalLinkAuthority:
    """Real ``LinkAuthority``: every call re-reads the verified journal (no cache to go stale after unpair)."""

    def __init__(self, journal: Journal):
        self.journal = journal

    def current(self, endpoint_id: str) -> Optional[LinkState]:
        if not isinstance(endpoint_id, str):
            return None
        try:
            state = self.journal.snapshot()
        except Exception:
            return None  # corrupt/unavailable journal: no link is operational
        link = state.links.get(endpoint_id)
        if link is None:
            return None
        return LinkState(binding=binding_for(link), generation=state.link_generation(link),
                         protocol_state=link["protocol_state"])
