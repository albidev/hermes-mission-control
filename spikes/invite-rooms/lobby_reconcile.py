"""Owner-explicit, scope-specific reconciliation of S2 fault orphans. Spike only.

After a killed owner process (create/register/mint intents) the wrapper stays
correctly blocked, but native leftovers persist: non-disbanded Rooms, stored
ready links, live target reservations. This module never auto-cleans. It gives
a local owner three explicit operations:

- ``inventory``: read-only join of OperationJournal + MintJournal + native RO DB
  reads, one sanitized classification per scope (never a bearer/grant/ciphertext);
- ``quarantine``: durable append-only marker for ONE orphan scope; the wrappers
  (OnceActivation / TargetMint) then refuse with ``scope_quarantined``;
- ``reconcile``: ONE quarantined scope, explicit per-scope owner confirmation.
  Room -> native ``groups.disband`` (stop + route revoke + tombstone); target
  reservation with a kept bearer -> native ``groups.peer.revoke`` with exactly
  that bearer; a lost bearer is never "revoked" by id: the reservation must be
  proven expired and is recorded ``expired_unrevocable``. Success only after RO
  readback. Journals are never updated or deleted; a ``reconciled`` record is
  appended to an append-only ledger (SQLite triggers reject UPDATE/DELETE).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import sqlite3
import time
from contextlib import closing

from lobby_control import ControlError
from lobby_consent import canonical

LEDGER_DDL = (
    'CREATE TABLE IF NOT EXISTS reconcile_ledger(seq INTEGER PRIMARY KEY AUTOINCREMENT, scope_id TEXT NOT NULL, '
    'kind TEXT NOT NULL, record TEXT NOT NULL, at REAL NOT NULL)',
    "CREATE TRIGGER IF NOT EXISTS reconcile_ledger_no_update BEFORE UPDATE ON reconcile_ledger "
    "BEGIN SELECT RAISE(ABORT, 'reconcile_ledger_append_only'); END",
    "CREATE TRIGGER IF NOT EXISTS reconcile_ledger_no_delete BEFORE DELETE ON reconcile_ledger "
    "BEGIN SELECT RAISE(ABORT, 'reconcile_ledger_append_only'); END",
)
_BASE_KEYS = frozenset({'lobby_id', 'invite_id', 'room_id', 'member_id', 'binding', 'host_id'})
ORPHAN_CLASSES = ('create_intent_no_room', 'room_without_link', 'room_with_ready_link', 'room_with_link',
                  'reservation_bearer_kept', 'reservation_bearer_lost', 'mint_intent_no_reservation')


class ScopeQuarantined(ControlError):
    def __init__(self):
        super().__init__('scope_quarantined')


class OwnerConfirmationRequired(ControlError):
    def __init__(self):
        super().__init__('owner_confirmation_required')


class ReconcileBlocked(ControlError):
    """Typed non-success. ``code`` is safe to return; ``record`` is sanitized."""
    def __init__(self, code, record=None):
        super().__init__(code)
        self.code, self.record = code, record or {}


def scope_id(scope):
    base = {k: scope[k] for k in _BASE_KEYS}
    return 'rs-' + hashlib.sha256(canonical(base)).hexdigest()[:32]


def _base(scope):
    if not isinstance(scope, dict) or not _BASE_KEYS <= set(scope):
        return None
    return {k: scope[k] for k in _BASE_KEYS}


# ------------------------------------------------------------------ ledger

def _ledger_exists(db):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='reconcile_ledger'").fetchone() is not None


def ledger_rows(storage, sid=None):
    """Read-only; never creates the table."""
    with storage._db() as db:
        if not _ledger_exists(db):
            return []
        sql, args = 'SELECT seq,scope_id,kind,record,at FROM reconcile_ledger', ()
        if sid is not None:
            sql, args = sql + ' WHERE scope_id=?', (sid,)
        return [dict(seq=r[0], scope_id=r[1], kind=r[2], record=json.loads(r[3]), at=r[4])
                for r in db.execute(sql + ' ORDER BY seq', args)]


def ledger_append(storage, sid, kind, record):
    with storage._db() as db:
        for ddl in LEDGER_DDL:
            db.execute(ddl)
        db.execute('BEGIN IMMEDIATE')
        db.execute('INSERT INTO reconcile_ledger(scope_id,kind,record,at) VALUES(?,?,?,?)',
                   (sid, kind, canonical(record).decode(), time.time()))
        db.commit()


def is_quarantined(storage, scope):
    """Wrapper guard: any durable quarantine marker for this exact base scope."""
    base = _base(scope)
    if base is None:
        return False
    sid = scope_id(base)
    return any(r['kind'] == 'quarantined' for r in ledger_rows(storage, sid))


def refuse_if_quarantined(storage, scope):
    if is_quarantined(storage, scope):
        raise ScopeQuarantined()


# ------------------------------------------------------------------ native RO reads
# A configured native DB that is missing, uninitialised or unreadable proves nothing:
# it reads as 'unknown' (never 'absent') and reconcile refuses to act or to record
# 'reconciled' on it. The native store creates all three tables in one schema, so an
# initialised store always has them; a missing table means "not the room store".
_UNKNOWN = 'unknown'


def _ro(path):
    path = Path(path)
    if not path.is_file():
        return None
    return closing(sqlite3.connect('file:' + str(path) + '?mode=ro', uri=True))


def _tables(db):
    return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def read_home(db_path, room_id, member_id):
    """Room + link projection. The link grant column is never selected."""
    out = {'room': 'absent', 'disbanded_at': None, 'links': []}
    unknown = {'room': _UNKNOWN, 'disbanded_at': None, 'links': []}
    handle = _ro(db_path)
    if handle is None:
        return unknown
    try:
        with handle as db:
            if not {'hosted_rooms', 'hosted_room_links'} <= _tables(db):
                return unknown
            row = db.execute('SELECT disbanded_at FROM hosted_rooms WHERE room_id=?', (room_id,)).fetchone()
            if row is not None:
                out['room'] = 'live' if row[0] is None else 'disbanded'
                out['disbanded_at'] = row[0]
            out['links'] = [dict(member_id=r[0], status=r[1]) for r in db.execute(
                'SELECT member_id,status FROM hosted_room_links WHERE room_id=? ORDER BY member_id', (room_id,))]
    except sqlite3.Error:
        return unknown
    return out


def read_target(db_path, room_id, member_id, target_profile, now=None):
    now = time.time() if now is None else now
    out = {'reservation': 'absent', 'expires_at': None, 'revoked_at': None}
    unknown = {'reservation': _UNKNOWN, 'expires_at': None, 'revoked_at': None}
    handle = _ro(db_path)
    if handle is None:
        return unknown
    try:
        with handle as db:
            if 'hosted_room_peer_reservations' not in _tables(db):
                return unknown
            row = db.execute('SELECT expires_at,revoked_at FROM hosted_room_peer_reservations '
                             'WHERE room_id=? AND member_id=? AND target_profile=?',
                             (room_id, member_id, target_profile)).fetchone()
            if row is not None:
                out['expires_at'], out['revoked_at'] = row
                out['reservation'] = ('revoked' if row[1] is not None else
                                      'expired' if row[0] <= now else 'live')
    except sqlite3.Error:
        return unknown
    return out


def _unavailable(readback):
    return (any(h['room'] == _UNKNOWN for h in readback['home'])
            or any(t['reservation'] == _UNKNOWN for t in readback['target']))


# ------------------------------------------------------------------ journals

def _home_ops(journal):
    """{base-scope-json: {operation: state}} from an OperationJournal (no results emitted)."""
    out = {}
    with journal._db() as db:
        if 'operations' not in _tables(db):
            return out
        for scope_text, operation, state in db.execute('SELECT scope,operation,state FROM operations'):
            base = _base(json.loads(scope_text))
            if base is not None:
                out.setdefault(canonical(base).decode(), {})[operation] = state
    return out


def _target_mints(journal):
    out = {}
    with journal._db() as db:
        for scope_text, state in db.execute('SELECT scope,state FROM material'):
            base = _base(json.loads(scope_text))
            if base is not None:
                out[canonical(base).decode()] = state
    return out


class HomeSide:
    def __init__(self, journal, rpc, native_db):
        self.journal, self.rpc, self.native_db = journal, rpc, Path(native_db)


class TargetSide:
    def __init__(self, journal, rpc, native_db):
        self.journal, self.rpc, self.native_db = journal, rpc, Path(native_db)


class Reconciler:
    """Explicit owner tool over a fixed, owner-supplied set of journals. No global op."""

    def __init__(self, owner_root, *, homes=(), targets=(), clock=time.time):
        self.root = Path(owner_root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.homes, self.targets, self.clock = list(homes), list(targets), clock

    # ---------------------------------------------------------------- discovery
    def _scopes(self):
        scopes = {}
        for side in self.homes:
            for key, ops in _home_ops(side.journal).items():
                entry = scopes.setdefault(key, {'homes': [], 'targets': []})
                entry['homes'].append((side, ops))
        for side in self.targets:
            for key, state in _target_mints(side.journal).items():
                entry = scopes.setdefault(key, {'homes': [], 'targets': []})
                entry['targets'].append((side, state))
        return {scope_id(json.loads(k)): dict(v, scope=json.loads(k)) for k, v in scopes.items()}

    def _entry(self, sid):
        if not isinstance(sid, str) or not sid.isascii():
            raise ControlError('unknown_scope')
        entry = self._scopes().get(sid)
        if entry is None:
            raise ControlError('unknown_scope')
        return entry

    def _readback(self, entry):
        scope = entry['scope']
        profile = scope['binding']['target_profile']
        # A scope without home journal rows (mint faults) is still checked against every
        # configured home native DB, read-only: never assume "no Room" from a missing journal.
        dbs = ([side.native_db for side, _ in entry['homes']] if entry['homes'] else
               sorted({side.native_db for side in self.homes}))
        home = [read_home(db, scope['room_id'], scope['member_id']) for db in dbs]
        target = [read_target(side.native_db, scope['room_id'], scope['member_id'], profile, self.clock())
                  for side, _ in entry['targets']]
        return {'home': home, 'target': target}

    def _classify(self, entry, readback):
        classes = []
        ops = {}
        for _, o in entry['homes']:
            ops.update(o)
        mints = [state for _, state in entry['targets']]
        rooms = [h['room'] for h in readback['home']]
        links = [l for h in readback['home'] for l in h['links']]
        if _unavailable(readback):
            classes.append('readback_unavailable')
        if ops.get('create') == 'intent' and all(r == 'absent' for r in rooms):
            classes.append('create_intent_no_room')
        if 'live' in rooms:
            if not links:
                classes.append('room_without_link')
            elif all(l['status'] == 'ready' for l in links):
                classes.append('room_with_ready_link')
            else:
                classes.append('room_with_link')
        reservations = [t['reservation'] for t in readback['target']]
        present = [r for r in reservations if r not in ('absent', _UNKNOWN)]
        if present and 'ready' in mints:
            classes.append('reservation_bearer_kept')
        if present and 'ready' not in mints and 'intent' in mints:
            classes.append('reservation_bearer_lost')
        if not present and _UNKNOWN not in reservations and 'intent' in mints:
            classes.append('mint_intent_no_reservation')
        intents = [k for k, v in ops.items() if v == 'intent'] + (['mint'] if 'intent' in mints else [])
        return classes, sorted(intents), ops, mints

    def _ledger(self, entry, sid):
        rows = []
        for side, _ in entry['homes'] + entry['targets']:
            rows.extend(ledger_rows(side.journal, sid))
        return rows

    def _public(self, sid, entry, readback):
        classes, intents, ops, mints = self._classify(entry, readback)
        ledger = self._ledger(entry, sid)
        scope = entry['scope']
        return {'scope_id': sid, 'lobby_id': scope['lobby_id'], 'invite_id': scope['invite_id'],
                'room_id': scope['room_id'], 'member_id': scope['member_id'],
                'home_operations': ops, 'target_mint': mints, 'open_intents': intents,
                'orphan': bool(intents), 'target_withdrawable': self._withdrawable(entry, readback, classes),
                'classes': classes, 'native': readback,
                'quarantined': any(r['kind'] == 'quarantined' for r in ledger),
                'reconciled': any(r['kind'] == 'reconciled' for r in ledger)}

    @staticmethod
    def _withdrawable(entry, readback, classes):
        """Target-owner-only view: a live reservation whose bearer B kept, with no home journal here.

        The target owner cannot observe the home's journals/DB, so it can never *prove* the home
        scope is orphaned. Withdrawing its own live reservation (= withdrawing its own consent) is
        nevertheless always the target owner's prerogative. Explicit, per scope, never global; the
        ledger records the basis honestly as ``target_owner_withdrawal``.
        """
        return (not entry['homes'] and 'reservation_bearer_kept' in classes
                and any(t['reservation'] == 'live' for t in readback['target']))

    # ---------------------------------------------------------------- operations
    def inventory(self, payload=None):
        if payload not in (None, {}):
            raise ControlError('invalid_owner_scope')
        out = []
        for sid, entry in sorted(self._scopes().items()):
            out.append(self._public(sid, entry, self._readback(entry)))
        return {'scopes': out}

    def _confirm_path(self, sid):
        return self.root / 'reconcile-confirm' / (sid + '.token')

    def quarantine(self, payload):
        if not isinstance(payload, dict) or set(payload) != {'scope_id'}:
            raise ControlError('invalid_owner_scope')
        sid = payload['scope_id']
        entry = self._entry(sid)
        view = self._public(sid, entry, self._readback(entry))
        if view['quarantined']:
            return {'status': 'quarantined', 'scope_id': sid, 'already': True}
        if not view['orphan'] and not view['target_withdrawable']:
            raise ControlError('scope_not_orphan')
        token = secrets.token_urlsafe(32)
        folder = self._confirm_path(sid).parent
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self._confirm_path(sid), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as out:
            out.write(token)
            out.flush()
            os.fsync(out.fileno())
        record = {'classes': view['classes'], 'open_intents': view['open_intents'],
                  'basis': 'open_intent' if view['orphan'] else 'target_owner_withdrawal',
                  'confirm_sha256': hashlib.sha256(token.encode()).hexdigest()}
        for side, _ in entry['homes'] + entry['targets']:
            ledger_append(side.journal, sid, 'quarantined', record)
        return {'status': 'quarantined', 'scope_id': sid, 'already': False}

    def _check_confirmation(self, entry, sid, confirm):
        digests = {r['record'].get('confirm_sha256') for r in self._ledger(entry, sid) if r['kind'] == 'quarantined'}
        if not digests:
            raise ControlError('scope_not_quarantined')
        # Type/ASCII guard BEFORE compare_digest (non-ASCII str raises TypeError there).
        if not isinstance(confirm, str) or not confirm or not confirm.isascii() or len(confirm) > 256:
            raise OwnerConfirmationRequired()
        presented = hashlib.sha256(confirm.encode('ascii')).hexdigest()
        if not any(hmac.compare_digest(presented, d) for d in digests if isinstance(d, str)):
            raise OwnerConfirmationRequired()

    def _terminal(self, readback):
        return (all(h['room'] in ('absent', 'disbanded') and not h['links'] for h in readback['home'])
                and all(t['reservation'] in ('absent', 'revoked', 'expired') for t in readback['target']))

    def _append_all(self, entry, sid, kind, record):
        for side, _ in entry['homes'] + entry['targets']:
            ledger_append(side.journal, sid, kind, record)

    def _blocked(self, entry, sid, code, actions, readback, **detail):
        record = dict(code=code, actions=actions, readback=readback, **detail)
        self._append_all(entry, sid, 'reconcile_blocked', record)
        raise ReconcileBlocked(code, record)

    async def reconcile(self, payload):
        from room_lobby_service import NativeRpcRejected
        if not isinstance(payload, dict) or set(payload) != {'scope_id', 'confirm'}:
            raise OwnerConfirmationRequired()
        sid = payload['scope_id']
        entry = self._entry(sid)
        self._check_confirmation(entry, sid, payload['confirm'])
        scope = entry['scope']
        readback = self._readback(entry)
        if _unavailable(readback):
            self._blocked(entry, sid, 'reconcile_readback_unavailable', [], readback)
        done = [r for r in self._ledger(entry, sid) if r['kind'] == 'reconciled']
        if done:
            recorded = done[-1]['record']
            if not self._terminal(readback):
                raise ReconcileBlocked('reconcile_conflict', {'readback': readback})
            return {'status': 'already_reconciled', 'scope_id': sid, 'readback': readback,
                    'identical_readback': recorded['readback'] == readback, 'outcome': recorded['outcome']}
        actions = []
        # 1. Home: a live Room is disbanded natively (stop + route revoke + tombstone).
        for (side, _), home in zip(entry['homes'], readback['home']):
            if home['room'] != 'live':
                continue
            try:
                result = await side.rpc('groups.disband', {'room_id': scope['room_id']})
                ok = isinstance(result, dict) and isinstance(result.get('tombstone'), dict) \
                    and result['tombstone'].get('room_id') == scope['room_id']
                actions.append({'action': 'groups.disband', 'native': 'accepted' if ok else 'unexpected_reply'})
            except NativeRpcRejected:
                after = self._readback(entry)
                had_link = bool(home['links'])
                actions.append({'action': 'groups.disband', 'native': 'rejected'})
                if had_link and any(h['room'] == 'live' and h['links'] for h in after['home']):
                    # Native leaves Room+link intact when the target cannot confirm revoke.
                    self._blocked(entry, sid, 'reconcile_blocked_target_unreachable', actions, after,
                                  cause='route_revoke_not_confirmed')
                if any(h['room'] == 'live' for h in after['home']):
                    self._blocked(entry, sid, 'reconcile_blocked_native_rejected', actions, after)
            except Exception:
                actions.append({'action': 'groups.disband', 'native': 'ambiguous'})
                self._blocked(entry, sid, 'reconcile_ambiguous', actions, self._readback(entry))
        readback = self._readback(entry)
        if not all(h['room'] in ('absent', 'disbanded') and not h['links'] for h in readback['home']):
            self._blocked(entry, sid, 'reconcile_home_not_verified', actions, readback)
        # 2. Target: revoke a still-live reservation ONLY with the exact kept bearer.
        outcome = {'room': [h['room'] for h in readback['home']]}
        reservation_outcomes = []
        for (side, state), target in zip(entry['targets'], readback['target']):
            if target['reservation'] == 'live':
                if state != 'ready':
                    self._blocked(entry, sid, 'reconcile_awaiting_expiry', actions, readback,
                                  expires_at=target['expires_at'])
                try:
                    bearer = side.journal.load(scope)['grant']
                except Exception:
                    self._blocked(entry, sid, 'reconcile_bearer_unavailable', actions, readback)
                try:
                    reply = await side.rpc('groups.peer.revoke', {'grant': bearer,
                                                                   'profile': scope['binding']['target_profile']})
                    actions.append({'action': 'groups.peer.revoke',
                                    'native': 'accepted' if reply == {'revoked': True} else 'unexpected_reply'})
                except NativeRpcRejected:
                    actions.append({'action': 'groups.peer.revoke', 'native': 'rejected'})
                except Exception:
                    actions.append({'action': 'groups.peer.revoke', 'native': 'ambiguous'})
                    self._blocked(entry, sid, 'reconcile_ambiguous', actions, self._readback(entry))
                finally:
                    bearer = None
        readback = self._readback(entry)
        if _unavailable(readback):
            self._blocked(entry, sid, 'reconcile_readback_unavailable', actions, readback)
        for (side, state), target in zip(entry['targets'], readback['target']):
            if target['reservation'] == 'live':
                code = 'reconcile_awaiting_expiry' if state != 'ready' else 'reconcile_revoke_not_verified'
                self._blocked(entry, sid, code, actions, readback, expires_at=target['expires_at'])
            reservation_outcomes.append(
                'revoked' if target['reservation'] == 'revoked' else
                'absent' if target['reservation'] == 'absent' else
                'expired_unrevocable' if state != 'ready' else 'expired')
        outcome['reservation'] = reservation_outcomes
        if not self._terminal(readback):
            self._blocked(entry, sid, 'reconcile_not_verified', actions, readback)
        record = {'outcome': outcome, 'actions': actions, 'readback': readback}
        self._append_all(entry, sid, 'reconciled', record)
        return {'status': 'reconciled', 'scope_id': sid, 'outcome': outcome, 'actions': actions,
                'readback': readback}


def owner_operations(reconciler, run):
    """OwnerSurface wiring. ``run`` executes a coroutine on the owner's RPC loop."""
    return {'inventory': reconciler.inventory, 'quarantine': reconciler.quarantine,
            'reconcile': lambda payload: run(reconciler.reconcile(payload))}
