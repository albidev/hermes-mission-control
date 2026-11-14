"""S2 follow-up: owner-explicit orphan reconciliation (lobby_reconcile).

Real MintJournal/OperationJournal files, real OwnerSurface HTTP, real SQLite
files shaped like the native tables. Only the native RPC is a fake that applies
the documented native semantics (disband: stop + route revoke via target, which
fails when the target is unreachable, then tombstone; peer.revoke: needs the
exact bearer). Nothing here touches a native service or a fixed port.
"""
import asyncio
from contextlib import closing
import json
from pathlib import Path
import socket
import sqlite3
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(HERE), str(HERE.parents[1] / 'server')]
from lobby_activation import OnceActivation, OperationJournal  # noqa: E402
from lobby_control import ControlError  # noqa: E402
from lobby_material import MintJournal, TargetMint  # noqa: E402
from lobby_native_rpc import BoundedNativeRpc, NativeRpcDenied  # noqa: E402
from lobby_owner import OwnerSurface  # noqa: E402
import lobby_reconcile as lr  # noqa: E402
from room_lobby_service import NativeRpcRejected  # noqa: E402

NATIVE_DDL = (
    'CREATE TABLE hosted_rooms(room_id TEXT PRIMARY KEY, disbanded_at REAL)',
    "CREATE TABLE hosted_room_links(room_id TEXT, member_id TEXT, grant TEXT, status TEXT DEFAULT 'ready', "
    'PRIMARY KEY(room_id, member_id))',
    'CREATE TABLE hosted_room_peer_reservations(room_id TEXT, member_id TEXT, target_profile TEXT, '
    'expires_at REAL, revoked_at REAL, PRIMARY KEY(room_id, member_id, target_profile))',
)
BINDING = {'endpoint_id': 'ep-b', 'fingerprint': 'f' * 64, 'target_install_id': 'gw-target', 'target_profile': 'default'}
FAULTS = ('create-after_intent', 'create-after_native_reply', 'register-after_intent',
          'register-after_native_reply', 'mint-after_intent', 'mint-after_native_reply')


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


class FakeNative:
    """Applies the documented native semantics to RO-readable SQLite files."""

    def __init__(self, home_db, target_db, clock):
        self.home_db, self.target_db, self.clock = home_db, target_db, clock
        self.calls = []
        self.reachable = True
        self.bearers = {}  # bearer -> (room_id, member_id, profile)

    def _w(self, path):
        return closing(sqlite3.connect(path, isolation_level=None))

    async def host(self, method, params):
        self.calls.append(('host', method, sorted(params)))
        if method != 'groups.disband' or set(params) != {'room_id'}:
            raise AssertionError('unexpected host call')
        room = params['room_id']
        with self._w(self.home_db) as db:
            row = db.execute('SELECT disbanded_at FROM hosted_rooms WHERE room_id=?', (room,)).fetchone()
            if row is None:
                raise NativeRpcRejected()
            if row[0] is None:
                links = db.execute('SELECT member_id FROM hosted_room_links WHERE room_id=?', (room,)).fetchall()
                if links and not self.reachable:
                    raise NativeRpcRejected()  # route revoke failed: room and link stay intact
                for (member,) in links:
                    with self._w(self.target_db) as t:
                        t.execute('UPDATE hosted_room_peer_reservations SET revoked_at=? WHERE room_id=? '
                                  'AND member_id=? AND expires_at>?', (self.clock(), room, member, self.clock()))
                db.execute('DELETE FROM hosted_room_links WHERE room_id=?', (room,))
                db.execute('UPDATE hosted_rooms SET disbanded_at=? WHERE room_id=?', (self.clock(), room))
        return {'tombstone': {'room_id': room}}

    async def target(self, method, params):
        self.calls.append(('target', method, sorted(params)))
        if method != 'groups.peer.revoke':
            raise AssertionError('unexpected target call')
        scope = self.bearers.get(params.get('grant'))
        if scope is None or params.get('profile') != scope[2]:
            raise NativeRpcRejected()
        with self._w(self.target_db) as db:
            row = db.execute('SELECT expires_at FROM hosted_room_peer_reservations WHERE room_id=? AND member_id=? '
                             'AND target_profile=?', scope).fetchone()
            if row is None or row[0] <= self.clock():
                raise NativeRpcRejected()  # expired grant does not decode
            db.execute('UPDATE hosted_room_peer_reservations SET revoked_at=? WHERE room_id=? AND member_id=? '
                       'AND target_profile=?', (self.clock(), *scope))
        return {'revoked': True}


def make_scope(i):
    return {'lobby_id': f'lobby-{i}', 'invite_id': f'invite-{i}', 'room_id': f'invite-room-{i}',
            'member_id': 'peer-b', 'binding': dict(BINDING), 'host_id': 'gw-home'}


class Lab:
    def __init__(self, root):
        self.root = Path(root)
        self.clock = Clock()
        self.home_db, self.target_db = self.root / 'home-native.db', self.root / 'target-native.db'
        for path in (self.home_db, self.target_db):
            with closing(sqlite3.connect(path)) as db:
                for ddl in NATIVE_DDL:
                    db.execute(ddl)
                db.commit()
        self.native = FakeNative(self.home_db, self.target_db, self.clock)
        self.homes, self.targets, self.scopes, self.secrets = [], [], {}, []

    def native_exec(self, path, sql, args=()):
        with closing(sqlite3.connect(path, isolation_level=None)) as db:
            db.execute(sql, args)

    def add(self, name, i):
        scope = make_scope(i)
        self.scopes[name] = scope
        op = OperationJournal(self.root / name / 'activation')
        mint = MintJournal(self.root / name / 'target-private')
        bearer = f'HermesRoom-bearer-{i}-' + 'x' * 48
        intent = dict(scope, spec={'room_id': scope['room_id'], 'members': ['peer-b']})
        room = scope['room_id']
        live_until = self.clock() + 600
        if name.startswith('mint-'):
            mint.begin(scope)
            if name == 'mint-after_native_reply':  # native reserved; bearer never journaled
                self.native_exec(self.target_db, 'INSERT INTO hosted_room_peer_reservations VALUES(?,?,?,?,NULL)',
                                 (room, 'peer-b', 'default', live_until))
                self.native.bearers['lost-' + bearer] = (room, 'peer-b', 'default')
        else:
            mint.begin(scope)
            mint.complete(scope, {'scope': scope, 'grant': bearer, 'catalog': {}, 'signature': 'sig'})
            self.native.bearers[bearer] = (room, 'peer-b', 'default')
            self.secrets.append(bearer)
            self.native_exec(self.target_db, 'INSERT INTO hosted_room_peer_reservations VALUES(?,?,?,?,NULL)',
                             (room, 'peer-b', 'default', live_until))
            op.begin(intent, 'create')
            if name != 'create-after_intent':
                self.native_exec(self.home_db, 'INSERT INTO hosted_rooms VALUES(?,NULL)', (room,))
            if name.startswith('register-'):
                op.complete(intent, 'create', {'room_id': room})
                op.begin(intent, 'register:peer-b')
                if name == 'register-after_native_reply':
                    self.native_exec(self.home_db, "INSERT INTO hosted_room_links VALUES(?,?,?,'ready')",
                                     (room, 'peer-b', bearer))
            if name == 'complete':
                op.complete(intent, 'create', {'room_id': room})
                op.begin(intent, 'register:peer-b')
                op.complete(intent, 'register:peer-b', {'room_id': room, 'member_id': 'peer-b'})
                op.begin(scope, 'send')
                op.complete(scope, 'send', {'status': 'sent'})
        self.homes.append(lr.HomeSide(op, self.native.host, self.home_db))
        self.targets.append(lr.TargetSide(mint, self.native.target, self.target_db))
        return scope

    def reconciler(self):
        return lr.Reconciler(self.root / 'owner', homes=self.homes, targets=self.targets, clock=self.clock)


def journal_dump(storage):
    with storage._db() as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        out = {'material': db.execute('SELECT scope,grant_id,state,ciphertext FROM material ORDER BY scope').fetchall()}
        if 'operations' in tables:
            out['operations'] = db.execute('SELECT * FROM operations ORDER BY scope,operation').fetchall()
    return out


def run(coro):
    return asyncio.run(coro)


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.lab = Lab(self.tmp.name)
        for i, name in enumerate(FAULTS + ('complete',)):
            self.lab.add(name, i)
        self.rec = self.lab.reconciler()
        self.ids = {name: lr.scope_id(scope) for name, scope in self.lab.scopes.items()}

    def tearDown(self):
        self.tmp.cleanup()

    def view(self, name):
        return next(s for s in self.rec.inventory()['scopes'] if s['scope_id'] == self.ids[name])

    def confirm(self, name):
        return (self.rec.root / 'reconcile-confirm' / (self.ids[name] + '.token')).read_text()

    def test_inventory_classification(self):
        expected = {
            'create-after_intent': ['create_intent_no_room', 'reservation_bearer_kept'],
            'create-after_native_reply': ['room_without_link', 'reservation_bearer_kept'],
            'register-after_intent': ['room_without_link', 'reservation_bearer_kept'],
            'register-after_native_reply': ['room_with_ready_link', 'reservation_bearer_kept'],
            'mint-after_intent': ['mint_intent_no_reservation'],
            'mint-after_native_reply': ['reservation_bearer_lost'],
        }
        for name, classes in expected.items():
            view = self.view(name)
            self.assertEqual(view['classes'], classes, name)
            self.assertTrue(view['orphan'], name)
            self.assertFalse(view['quarantined'])
        healthy = self.view('complete')
        self.assertFalse(healthy['orphan'])
        self.assertEqual(self.view('register-after_native_reply')['native']['home'][0]['links'],
                         [{'member_id': 'peer-b', 'status': 'ready'}])
        self.assertEqual(len(self.rec.inventory()['scopes']), 7)
        self.assertEqual(self.lab.native.calls, [])  # inventory is read-only

    def test_inventory_is_read_only_and_sanitized(self):
        before = [journal_dump(s.journal) for s in self.lab.homes + self.lab.targets]
        dbs = [p.read_bytes() for p in (self.lab.home_db, self.lab.target_db)]
        out = json.dumps(self.rec.inventory())
        for bearer in self.lab.secrets:
            self.assertNotIn(bearer, out)
        self.assertNotIn('HermesRoom', out)
        self.assertNotIn('grant', out.replace('grant_id', ''))
        self.assertEqual(before, [journal_dump(s.journal) for s in self.lab.homes + self.lab.targets])
        self.assertEqual(dbs, [p.read_bytes() for p in (self.lab.home_db, self.lab.target_db)])
        for side in self.lab.homes + self.lab.targets:  # no ledger table created by reads
            with side.journal._db() as db:
                self.assertFalse(lr._ledger_exists(db))

    def test_quarantine_is_durable_append_only_and_refuses_wrappers(self):
        name = 'register-after_native_reply'
        scope = self.lab.scopes[name]
        self.assertEqual(self.rec.quarantine({'scope_id': self.ids[name]})['status'], 'quarantined')
        self.assertTrue(self.rec.quarantine({'scope_id': self.ids[name]})['already'])  # no second marker
        home, target = self.lab.homes[3], self.lab.targets[3]
        self.assertEqual([r['kind'] for r in lr.ledger_rows(home.journal, self.ids[name])], ['quarantined'])
        # Durable: a fresh journal object (restart) still sees the marker.
        fresh = OperationJournal(home.journal.storage.root)
        self.assertTrue(lr.is_quarantined(fresh.storage, scope))
        with home.journal._db() as db, self.assertRaises(sqlite3.DatabaseError):
            db.execute("UPDATE reconcile_ledger SET kind='x'")
        with home.journal._db() as db, self.assertRaises(sqlite3.DatabaseError):
            db.execute('DELETE FROM reconcile_ledger')
        # Wrapper refusal before any planning/native call (activation + send + mint).
        activation = OnceActivation.__new__(OnceActivation)
        activation.scope, activation.journal = json.loads(lr.canonical(scope)), fresh
        activation.planner = None  # would raise AttributeError if reached
        payload = {'lobby_id': scope['lobby_id'], 'invite_id': scope['invite_id']}
        with self.assertRaises(lr.ScopeQuarantined):
            run(activation.activate(payload))
        with self.assertRaises(lr.ScopeQuarantined):
            run(activation.first_send(dict(payload, text='hello')))
        calls = []

        async def rpc(method, params):
            calls.append(method)
        mint = TargetMint(None, MintJournal(target.journal.root), rpc)
        mint.approved_scope = lambda p: scope
        with self.assertRaises(lr.ScopeQuarantined):
            run(mint.mint(payload))
        self.assertEqual(calls, [])
        # Over the owner HTTP surface the refusal is typed.
        root = Path(self.tmp.name) / 'surface'
        with OwnerSurface(root, {'activate': lambda p: run(activation.activate(p))}) as owner:
            self.assertEqual(post(owner, dict(payload, op='activate')), (409, {'error': 'scope_quarantined'}))
        # Other scopes are not affected.
        self.assertFalse(lr.is_quarantined(self.lab.homes[2].journal.storage, self.lab.scopes['register-after_intent']))

    def test_quarantine_and_reconcile_preconditions(self):
        with self.assertRaises(ControlError) as cm:
            self.rec.quarantine({'scope_id': self.ids['complete']})
        self.assertEqual(str(cm.exception), 'scope_not_orphan')
        for bad in ({}, {'scope_id': self.ids['mint-after_intent'], 'all': True}, {'all': True}, None):
            with self.assertRaises(ControlError):
                self.rec.quarantine(bad)
        with self.assertRaises(ControlError) as cm:
            self.rec.quarantine({'scope_id': 'rs-unknown'})
        self.assertEqual(str(cm.exception), 'unknown_scope')
        with self.assertRaises(ControlError) as cm:
            run(self.rec.reconcile({'scope_id': self.ids['mint-after_intent'], 'confirm': 'x'}))
        self.assertEqual(str(cm.exception), 'scope_not_quarantined')
        self.assertEqual(self.lab.native.calls, [])

    def test_reconcile_all_fault_scopes_and_idempotency(self):
        name_lost = 'mint-after_native_reply'
        before = [journal_dump(s.journal) for s in self.lab.homes + self.lab.targets]
        results = {}
        for name in FAULTS:
            self.rec.quarantine({'scope_id': self.ids[name]})
        for name in FAULTS:
            payload = {'scope_id': self.ids[name], 'confirm': self.confirm(name)}
            if name == name_lost:
                with self.assertRaises(lr.ReconcileBlocked) as cm:
                    run(self.rec.reconcile(payload))
                self.assertEqual(cm.exception.code, 'reconcile_awaiting_expiry')
                continue
            results[name] = run(self.rec.reconcile(payload))
            self.assertEqual(results[name]['status'], 'reconciled', name)
        self.assertEqual(results['create-after_intent']['outcome'], {'room': ['absent'], 'reservation': ['revoked']})
        self.assertEqual(results['register-after_native_reply']['outcome'],
                         {'room': ['disbanded'], 'reservation': ['revoked']})
        self.assertEqual([a['action'] for a in results['register-after_native_reply']['actions']],
                         ['groups.disband'])  # route revoke covered the reservation
        self.assertEqual([a['action'] for a in results['register-after_intent']['actions']],
                         ['groups.disband', 'groups.peer.revoke'])
        self.assertEqual(results['mint-after_intent']['outcome'], {'room': ['absent'], 'reservation': ['absent']})
        self.assertEqual(results['mint-after_intent']['actions'], [])
        # Lost bearer: wait until proven expired, then honest expired_unrevocable.
        self.lab.clock.now += 601
        lost = run(self.rec.reconcile({'scope_id': self.ids[name_lost], 'confirm': self.confirm(name_lost)}))
        self.assertEqual(lost['outcome'], {'room': ['absent'], 'reservation': ['expired_unrevocable']})
        self.assertEqual(lost['actions'], [])
        revoke_calls = [c for c in self.lab.native.calls if c[1] == 'groups.peer.revoke']
        self.assertEqual(len(revoke_calls), 3)  # exactly the three kept bearers whose reservation stayed live
        # Native readback: every Room tombstoned, no link, no live reservation.
        with closing(sqlite3.connect(self.lab.home_db)) as db:
            self.assertEqual(db.execute('SELECT room_id FROM hosted_rooms WHERE disbanded_at IS NULL').fetchall(),
                             [(self.lab.scopes['complete']['room_id'],)])  # healthy scope untouched
            self.assertEqual(db.execute('SELECT count(*) FROM hosted_room_links').fetchone()[0], 0)
        with closing(sqlite3.connect(self.lab.target_db)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM hosted_room_peer_reservations WHERE revoked_at IS NULL '
                                        'AND expires_at>?', (self.lab.clock(),)).fetchone()[0], 0)
            # 5 fault reservations + the healthy scope's: rows are never deleted by reconcile.
            self.assertEqual(db.execute('SELECT count(*) FROM hosted_room_peer_reservations').fetchone()[0], 6)
        # Journals intact (no update/delete), ledger only appended.
        self.assertEqual(before, [journal_dump(s.journal) for s in self.lab.homes + self.lab.targets])
        ledgers = {n: [r['kind'] for r in lr.ledger_rows(self.lab.targets[i].journal, self.ids[n])]
                   for i, n in enumerate(FAULTS)}
        self.assertEqual(ledgers[name_lost], ['quarantined', 'reconcile_blocked', 'reconciled'])
        self.assertEqual(ledgers['create-after_intent'], ['quarantined', 'reconciled'])
        # Idempotent: second run is a no-op with identical readback.
        calls = len(self.lab.native.calls)
        for name in FAULTS:
            again = run(self.rec.reconcile({'scope_id': self.ids[name], 'confirm': self.confirm(name)}))
            self.assertEqual(again['status'], 'already_reconciled')
            self.assertTrue(again['identical_readback'], name)
        self.assertEqual(len(self.lab.native.calls), calls)
        self.assertEqual(ledgers, {n: [r['kind'] for r in lr.ledger_rows(self.lab.targets[i].journal, self.ids[n])]
                                   for i, n in enumerate(FAULTS)})
        # Sanitized results.
        blob = json.dumps([results, lost]) + json.dumps(self.rec.inventory())
        for bearer in self.lab.secrets:
            self.assertNotIn(bearer, blob)
        for name in FAULTS:
            self.assertNotIn(self.confirm(name), blob)

    def test_unattributed_live_room_is_never_disbanded(self):
        name = 'mint-after_intent'
        self.lab.native_exec(self.lab.home_db, 'INSERT INTO hosted_rooms VALUES(?,NULL)',
                             (self.lab.scopes[name]['room_id'],))
        self.assertIn('room_without_link', self.view(name)['classes'])
        self.rec.quarantine({'scope_id': self.ids[name]})
        with self.assertRaises(lr.ReconcileBlocked) as cm:
            run(self.rec.reconcile({'scope_id': self.ids[name], 'confirm': self.confirm(name)}))
        self.assertEqual(cm.exception.code, 'reconcile_home_not_verified')
        self.assertEqual(self.lab.native.calls, [])

    def test_unreachable_target_is_typed_and_never_false_success(self):
        name = 'register-after_native_reply'
        self.rec.quarantine({'scope_id': self.ids[name]})
        payload = {'scope_id': self.ids[name], 'confirm': self.confirm(name)}
        self.lab.native.reachable = False
        for _ in range(2):
            with self.assertRaises(lr.ReconcileBlocked) as cm:
                run(self.rec.reconcile(payload))
            self.assertEqual(cm.exception.code, 'reconcile_blocked_target_unreachable')
        view = self.view(name)
        self.assertEqual(view['native']['home'][0]['room'], 'live')
        self.assertEqual(view['native']['target'][0]['reservation'], 'live')
        self.assertFalse(view['reconciled'])
        self.assertFalse(any(c[1] == 'groups.peer.revoke' for c in self.lab.native.calls))
        self.lab.native.reachable = True
        self.assertEqual(run(self.rec.reconcile(payload))['status'], 'reconciled')

    def test_owner_auth_and_confirmation_over_http(self):
        name = 'create-after_native_reply'
        self.rec.quarantine({'scope_id': self.ids[name]})
        other = 'register-after_intent'
        self.rec.quarantine({'scope_id': self.ids[other]})
        root = Path(self.tmp.name) / 'reconcile-surface'
        with OwnerSurface(root, lr.owner_operations(self.rec, run)) as owner:
            sid = self.ids[name]
            # Owner bearer: missing / wrong / non-ASCII -> 403 before any operation.
            self.assertEqual(raw_post(owner, {'op': 'inventory'}, None)[0], 403)
            self.assertEqual(post(owner, {'op': 'inventory'}, 'wrong'), (403, {'error': 'owner_required'}))
            self.assertEqual(raw_post(owner, {'op': 'inventory'}, 'Bearer ' + owner.token[:-1] + '\xe9')[0], 403)
            # Per-scope confirmation: missing / wrong / non-ASCII / wrong type / other scope's token -> 403.
            for confirm in (None, '', 'wrong', self.confirm(name)[:-1] + '\u00e9', 7, ['x'], self.confirm(other)):
                body = {'op': 'reconcile', 'scope_id': sid}
                if confirm is not None:
                    body['confirm'] = confirm
                self.assertEqual(post(owner, body), (403, {'error': 'owner_confirmation_required'}), confirm)
            self.assertEqual(self.lab.native.calls, [])
            status, inv = post(owner, {'op': 'inventory'})
            self.assertEqual(status, 200)
            self.assertNotIn(self.confirm(name), json.dumps(inv))
            self.assertEqual(post(owner, {'op': 'reconcile', 'scope_id': sid, 'confirm': self.confirm(name)})[1]['status'],
                             'reconciled')
            self.lab.native.reachable = False
            self.rec.quarantine({'scope_id': self.ids['register-after_native_reply']})
            self.assertEqual(post(owner, {'op': 'reconcile', 'scope_id': self.ids['register-after_native_reply'],
                                          'confirm': self.confirm('register-after_native_reply')}),
                             (409, {'error': 'reconcile_blocked_target_unreachable'}))
            self.assertEqual(post(owner, {'op': 'quarantine', 'scope_id': self.ids['complete']}),
                             (409, {'error': 'scope_not_orphan'}))

    def test_bounded_rpc_allows_only_exact_disband(self):
        added = set(__import__('lobby_native_rpc')._FIELDS) - {
            'groups.capabilities', 'groups.state', 'groups.log', 'groups.create', 'groups.peer.invite',
            'groups.peer.register', 'groups.peer.revoke', 'groups.send'}
        self.assertEqual(added, {'groups.disband'})
        rpc = BoundedNativeRpc(object())
        for method, params in (('groups.disband', {}), ('groups.disband', {'room_id': 'r', 'cancel_id': 'c'}),
                               ('groups.delete', {'room_id': 'r'}), ('groups.stop', {'room_id': 'r'})):
            with self.assertRaises(NativeRpcDenied):
                run(rpc(method, params))

    def test_target_only_owner_withdrawal_is_explicit_and_honest(self):
        # Owner B sees only its own MintJournals + native DB: it cannot observe home intents.
        owner_b = lr.Reconciler(Path(self.tmp.name) / 'owner-b', targets=self.lab.targets, clock=self.lab.clock)
        sid = self.ids['complete']
        view = next(v for v in owner_b.inventory()['scopes'] if v['scope_id'] == sid)
        self.assertFalse(view['orphan'])
        self.assertTrue(view['target_withdrawable'])
        self.assertEqual(view['native']['home'], [])
        self.assertEqual(owner_b.quarantine({'scope_id': sid})['status'], 'quarantined')
        record = lr.ledger_rows(self.lab.targets[6].journal, sid)[0]['record']
        self.assertEqual(record['basis'], 'target_owner_withdrawal')  # honest: not a proven orphan
        confirm = (owner_b.root / 'reconcile-confirm' / (sid + '.token')).read_text()
        out = run(owner_b.reconcile({'scope_id': sid, 'confirm': confirm}))
        self.assertEqual(out['outcome'], {'room': [], 'reservation': ['revoked']})
        self.assertEqual([c[1] for c in self.lab.native.calls], ['groups.peer.revoke'])
        with closing(sqlite3.connect(self.lab.home_db)) as db:  # target owner never touches the home Room
            self.assertEqual(db.execute('SELECT disbanded_at FROM hosted_rooms WHERE room_id=?',
                                        (self.lab.scopes['complete']['room_id'],)).fetchone(), (None,))
        self.assertTrue(lr.is_quarantined(self.lab.targets[6].journal, self.lab.scopes['complete']))
        # Nothing live left to withdraw (expired, bearer kept, no intent) -> typed refusal.
        self.lab.clock.now += 601
        later = lr.Reconciler(Path(self.tmp.name) / 'owner-b2', targets=[self.lab.targets[0]], clock=self.lab.clock)
        with self.assertRaises(ControlError) as cm:
            later.quarantine({'scope_id': self.ids['create-after_intent']})
        self.assertEqual(str(cm.exception), 'scope_not_orphan')
        # (The joined, home-visible view refusing a healthy scope is covered by
        # test_quarantine_and_reconcile_preconditions.)


def post(owner, payload, token=None):
    import urllib.error
    import urllib.request
    request = urllib.request.Request(owner.base + '/owner', data=json.dumps(payload).encode(), headers={
        'Content-Type': 'application/json', 'Authorization': 'Bearer ' + (owner.token if token is None else token)})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=15) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.load(exc)
        finally:
            exc.close()


def raw_post(owner, payload, authorization):
    """Verbatim bytes (latin-1 header values), so non-ASCII reaches the handler."""
    from urllib.parse import urlsplit
    body = json.dumps(payload).encode()
    head = ['POST /owner HTTP/1.1', 'Host: 127.0.0.1', 'Content-Type: application/json',
            'Content-Length: %d' % len(body), 'Connection: close']
    if authorization is not None:
        head.append('Authorization: ' + authorization)
    port = urlsplit(owner.base).port
    with socket.create_connection(('127.0.0.1', port), timeout=10) as sock:
        sock.sendall(('\r\n'.join(head) + '\r\n\r\n').encode('latin-1') + body)
        data = b''
        while chunk := sock.recv(65536):
            data += chunk
    status = int(data.split(b' ', 2)[1])
    return status, json.loads(data.split(b'\r\n\r\n', 1)[1])


if __name__ == '__main__':
    unittest.main()
