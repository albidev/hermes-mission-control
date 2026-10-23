"""S2 follow-up native proof: owner-explicit orphan reconciliation after SIGKILL faults.

One-machine real boundaries only (same lab as gate_s2e_native). Never production-imported.
Reproduces the six SIGKILL fault scopes (create/register x after_intent/after_native_reply
via s2e_activation_faults, mint x after_intent/after_native_reply via s2c_mint_faults), then
per scope: inventory (RO) -> quarantine -> reconcile with explicit per-scope owner
confirmation over the real loopback OwnerSurface, separately for the home owner (A: Rooms,
links) and the joining/target owner (B: reservations). Native DBs are read RO only.
The adapter route of the linked scope is withdrawn once to prove the typed
``reconcile_blocked_target_unreachable`` (no false success) before the real reconcile.
Finally a brand-new invite (q5) must still activate, send and settle, and every
reconcile re-run must be a no-op with identical readback.
"""
import asyncio
import json
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import sys
import time
from contextlib import closing
from types import SimpleNamespace

import aiohttp

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parents[1] / 'server')]
import gate_s1_native as gs  # noqa: E402
from gate_s2b_native import post, hashes  # noqa: E402
from gate_p2_p4_local import ready_socket  # noqa: E402
import gate_s2e_native as s2e  # noqa: E402
from gate_s2e_native import Scenario, await_on  # noqa: E402
from lobby_native_rpc import BoundedNativeRpc  # noqa: E402
from lobby_material import MintJournal  # noqa: E402
from lobby_activation import OperationJournal, OnceActivation  # noqa: E402
from lobby_owner import OwnerSurface  # noqa: E402
import lobby_reconcile as lr  # noqa: E402

LINK_SCOPE = 'register-after_native_reply'
ACTIVATION_SCOPES = ('create-after_intent', 'create-after_native_reply', 'register-after_intent', LINK_SCOPE)
MINT_SCOPES = ('after_intent', 'after_native_reply')


def ro_rows(path, sql, args=()):
    path = Path(path)
    if not path.exists():
        return []
    with closing(sqlite3.connect('file:' + str(path) + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        return [dict(r) for r in db.execute(sql, args)]


def journal_rows(storage):
    """Exact journal content (ciphertext digested) to prove no update/delete."""
    import hashlib
    with storage._db() as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        out = {'material': [[s, g, st, hashlib.sha256(c).hexdigest() if c else None]
                            for s, g, st, c in db.execute('SELECT scope,grant_id,state,ciphertext FROM material ORDER BY scope')]}
        if 'operations' in tables:
            out['operations'] = [list(r) for r in db.execute('SELECT * FROM operations ORDER BY scope,operation')]
    return out


class Owners:
    """Home (A) and joining/target (B) owner reconcilers plus a joined RO inventory."""

    def __init__(self, lab, hr, tr, loop):
        self.lab, self.loop = lab, loop
        self.home_db, self.target_db = lab.home / 'shared-state.db', lab.target / 'shared-state.db'
        self.hr, self.tr = hr, tr
        self.homes, self.targets = [], []
        self.names = {}
        self.grants = []

    def add(self, name, scope, home_journal=None, target_journal=None):
        sid = lr.scope_id(scope)
        self.names[name] = dict(scope=scope, scope_id=sid, home=home_journal, target=target_journal)
        if home_journal is not None:
            self.homes.append(lr.HomeSide(OperationJournal(home_journal), self.hr, self.home_db))
        if target_journal is not None:
            self.targets.append(lr.TargetSide(MintJournal(target_journal), self.tr, self.target_db))

    def reconcilers(self):
        root = self.lab.out / 'reconcile-owners'
        self.home = lr.Reconciler(root / 'home-a', homes=self.homes)
        self.target = lr.Reconciler(root / 'target-b', targets=self.targets)
        self.joined = lr.Reconciler(root / 'joined-ro', homes=self.homes, targets=self.targets)

    def surface(self, side):
        rec = self.home if side == 'home' else self.target
        return OwnerSurface(self.lab.out / ('reconcile-surface-' + side + '-' + secrets.token_hex(4)),
                            lr.owner_operations(rec, lambda coro: await_on(self.loop, coro)))

    def confirm(self, side, name):
        rec = self.home if side == 'home' else self.target
        return (rec.root / 'reconcile-confirm' / (self.names[name]['scope_id'] + '.token')).read_text()

    def journals(self):
        out = {}
        for name, item in self.names.items():
            for key in ('home', 'target'):
                if item[key] is not None:
                    storage = OperationJournal(item[key]).storage if key == 'home' else MintJournal(item[key])
                    out[name + ':' + key] = journal_rows(storage)
        return out

    def ledgers(self):
        out = {}
        for name, item in self.names.items():
            for key in ('home', 'target'):
                if item[key] is not None:
                    storage = OperationJournal(item[key]).storage if key == 'home' else MintJournal(item[key])
                    out[name + ':' + key] = [r['kind'] for r in lr.ledger_rows(storage, item['scope_id'])]
        return out

    def native(self, name):
        scope = self.names[name]['scope']
        return {'rooms': ro_rows(self.home_db, 'SELECT room_id,disbanded_at FROM hosted_rooms WHERE room_id=?', (scope['room_id'],)),
                'links': ro_rows(self.home_db, 'SELECT member_id,status FROM hosted_room_links WHERE room_id=?', (scope['room_id'],))
                if ro_rows(self.home_db, "SELECT 1 FROM sqlite_master WHERE name='hosted_room_links'") else [],
                'reservations': ro_rows(self.target_db, 'SELECT member_id,target_profile,expires_at,revoked_at '
                                        'FROM hosted_room_peer_reservations WHERE room_id=?', (scope['room_id'],))}


def make_scenario_class(lab, owners):
    class ReconcileScenario(Scenario):
        """Records each fault scope; the linked scope is reconciled while its route is reachable."""

        def __exit__(self, *args):
            if args[0] is None and self.name in ACTIVATION_SCOPES:
                try:
                    owners.add(self.name, self.scope, home_journal=self.root / 'fault-owner' / 'journal',
                               target_journal=self.root / 'target-private')
                    owners.grants.append(self.material.grant)
                    if self.name == LINK_SCOPE:
                        link_stage(lab, owners, self)
                except BaseException:
                    super().__exit__(*sys.exc_info())
                    raise
            return super().__exit__(*args)
    return ReconcileScenario


def link_stage(lab, owners, s):
    """Unreachable-then-reachable reconcile of the Room+ready-link scope (home owner A)."""
    owners.reconcilers()
    name, sid = LINK_SCOPE, owners.names[LINK_SCOPE]['scope_id']
    view = next(v for v in owners.joined.inventory()['scopes'] if v['scope_id'] == sid)
    lab.check('s2g_link_inventory', view['classes'] == ['room_with_ready_link', 'reservation_bearer_kept']
              and view['orphan'] and not view['quarantined'], view=view)
    before = owners.native(name)
    lab.check('s2g_link_native_before', len(before['rooms']) == 1 and before['rooms'][0]['disbanded_at'] is None
              and before['links'] == [{'member_id': 'peer-b', 'status': 'ready'}]
              and len(before['reservations']) == 1 and before['reservations'][0]['revoked_at'] is None, native=before)
    with owners.surface('home') as owner:
        lab.secret_values.append(owner.token)
        lab.check('s2g_link_quarantine', post(owner, dict(op='quarantine', scope_id=sid)) ==
                  (200, {'status': 'quarantined', 'scope_id': sid, 'already': False}))
        confirm = owners.confirm('home', name)
        lab.secret_values.append(confirm)
        for bad in ({}, {'confirm': 'wrong'}, {'confirm': confirm[:-1] + '\u00e9'}):
            lab.check('s2g_link_confirmation_required_' + str(len(bad) and len(bad['confirm'])),
                      post(owner, dict(op='reconcile', scope_id=sid, **bad)) == (403, {'error': 'owner_confirmation_required'}))
        lab.check('s2g_link_owner_token_required', post(owner, dict(op='inventory'), 'not-the-owner') == (403, {'error': 'owner_required'}))
        # Quarantined wrapper: activation of the old scope is a typed refusal, no native call.
        resumed = OnceActivation(s.store, s.scope, s.registry, OperationJournal(s.root / 'fault-owner' / 'journal'), owners.hr)
        rows_before = s2e.method_rows(lab, s.scope['room_id'])
        with OwnerSurface(s.root / 'quarantined-owner', {'activate': lambda p: await_on(owners.loop, resumed.activate(p))}) as blocked:
            lab.secret_values.append(blocked.token)
            lab.check('s2g_link_wrapper_refuses_quarantined',
                      post(blocked, dict(s.payload, op='activate')) == (409, {'error': 'scope_quarantined'})
                      and s2e.method_rows(lab, s.scope['room_id']) == rows_before)
        # Target unreachable: the adapter route is withdrawn; native must keep Room+link.
        firewall = s.adapter._routes.pop('s2e')
        try:
            blocked = [post(owner, dict(op='reconcile', scope_id=sid, confirm=confirm)) for _ in range(2)]
        finally:
            s.adapter._routes['s2e'] = firewall
        during = owners.native(name)
        lab.check('s2g_link_unreachable_typed_no_false_success',
                  blocked == [(409, {'error': 'reconcile_blocked_target_unreachable'})] * 2
                  and during == before and owners.ledgers()[name + ':home'][-1] == 'reconcile_blocked',
                  responses=blocked, native=during)
        status, result = post(owner, dict(op='reconcile', scope_id=sid, confirm=confirm))
        after = owners.native(name)
        lab.report.setdefault('s2g_reconcile', {})[name + ':home'] = result
        lab.check('s2g_link_reconciled', status == 200 and result['status'] == 'reconciled'
                  # home-owner reconciler holds no target journal: reservation outcome is empty here
                  and result['outcome'] == {'room': ['disbanded'], 'reservation': []}
                  and [a['action'] for a in result['actions']] == ['groups.disband'], result=result)
        lab.check('s2g_link_native_after', after['rooms'][0]['disbanded_at'] is not None and after['links'] == []
                  and after['reservations'][0]['revoked_at'] is not None, native=after)


async def native(lab):
    from s2e_activation_faults import run_activation_faults
    from s2c_mint_faults import run_faults
    async with aiohttp.ClientSession(trust_env=False, timeout=aiohttp.ClientTimeout(total=200)) as session:
        host = await ready_socket(session, 'http://127.0.0.1:18791', 18790, lab.keys[1], lab.keys[2], lab.children)
        deadline = time.monotonic() + 150
        while True:
            try:
                target = await session.ws_connect(f'ws://127.0.0.1:{lab.target_tui_port}/api/ws?token={lab.target_ws_key}')
                break
            except (OSError, aiohttp.ClientError):
                if time.monotonic() > deadline or lab.target_tui.poll() is not None:
                    await host.close()
                    raise RuntimeError('target_native_readiness') from None
                await asyncio.sleep(.5)
        async with host, target:
            hr, tr = BoundedNativeRpc(host), BoundedNativeRpc(target)
            caps = await tr('groups.capabilities', {})
            lab.check('actual_native_target_identity', caps['authority_gateway_id'] == lab.report['identity']['second_gateway_id'])
            loop = asyncio.get_running_loop()
            # Owner reconcile uses its OWN native sockets: a poisoned/ambiguous owner RPC
            # can never poison the lab's activation/send sockets (and vice versa).
            async with session.ws_connect(f'ws://127.0.0.1:18790/api/ws?token={lab.keys[2]}') as rhost, \
                    session.ws_connect(f'ws://127.0.0.1:{lab.target_tui_port}/api/ws?token={lab.target_ws_key}') as rtarget:
                rhr, rtr = BoundedNativeRpc(rhost), BoundedNativeRpc(rtarget)
                rcaps = await rtr('groups.capabilities', {})
                lab.check('s2g_owner_native_sockets', rcaps['authority_gateway_id'] == caps['authority_gateway_id']
                          and (await rhr('groups.capabilities', {}))['authority_gateway_id'] == lab.report['identity']['gateway_id'])
                owners = Owners(lab, rhr, rtr, loop)
                # 1. Reproduce the six SIGKILL fault scopes with the unchanged S2 harnesses.
                await asyncio.to_thread(run_activation_faults, lab, hr, tr, loop, make_scenario_class(lab, owners))
                await asyncio.to_thread(run_faults, lab, tr, loop)
                for row in lab.report['mint_faults']:
                    owners.add('mint-' + row['boundary'], row['scope'], target_journal=lab.out / row['boundary'] / 'journal')
                # 2. inventory -> quarantine -> reconcile, per scope, per owner.
                await asyncio.to_thread(reconcile_all, lab, owners)
                # 3. A brand-new invite still works end to end after reconciliation.
                await asyncio.to_thread(q5_after, lab, owners, hr, tr, loop)


def reconcile_all(lab, owners):
    owners.reconcilers()
    journals_before = owners.journals()
    lab.report['s2g_journals_before'] = {k: {t: len(v) for t, v in j.items()} for k, j in journals_before.items()}
    inventory = owners.joined.inventory()
    lab.report['s2g_inventory_before'] = inventory
    views = {name: next(v for v in inventory['scopes'] if v['scope_id'] == item['scope_id'])
             for name, item in owners.names.items()}
    expected = {
        'create-after_intent': ['create_intent_no_room', 'reservation_bearer_kept'],
        'create-after_native_reply': ['room_without_link', 'reservation_bearer_kept'],
        'register-after_intent': ['room_without_link', 'reservation_bearer_kept'],
        LINK_SCOPE: ['reservation_bearer_kept'],  # Room+link already reconciled by owner A in link_stage
        'mint-after_intent': ['mint_intent_no_reservation'],
        'mint-after_native_reply': ['reservation_bearer_lost'],
    }
    lab.check('s2g_inventory_classification', {n: v['classes'] for n, v in views.items()} == expected
              and all(v['orphan'] for v in views.values()), classes={n: v['classes'] for n, v in views.items()})
    blob = json.dumps(inventory)
    lab.check('s2g_inventory_sanitized', all(g not in blob for g in owners.grants) and 'HermesRoom ' not in blob)
    results = {}
    # Owner B (target-only view) of the linked scope: owner A's disband already revoked that
    # reservation through the native route revoke, so B has nothing live to withdraw and no open
    # intent -> quarantine is refused (typed), never a fake reconcile.
    nothing_to_do = {(LINK_SCOPE, 'target')}
    with owners.surface('home') as home, owners.surface('target') as target:
        lab.secret_values.extend([home.token, target.token])
        target_view = {v['scope_id']: v for v in owners.target.inventory()['scopes']}
        lab.report['s2g_target_only_views'] = {
            n: {k: target_view[i['scope_id']][k] for k in ('classes', 'orphan', 'target_withdrawable')}
            for n, i in owners.names.items() if i['target'] is not None}
        for name, item in owners.names.items():
            sid = item['scope_id']
            for side, owner in (('home', home), ('target', target)):
                if item[side] is None:
                    continue
                q = post(owner, dict(op='quarantine', scope_id=sid))
                if (name, side) in nothing_to_do:
                    lab.check(f's2g_{name}_{side}_nothing_to_withdraw', q == (409, {'error': 'scope_not_orphan'})
                              and owners.native(name)['reservations'][0]['revoked_at'] is not None, response=q)
                    continue
                lab.check(f's2g_{name}_{side}_quarantine', q[0] == 200 and q[1]['status'] == 'quarantined', response=q)
        # Wrapper refusal for every quarantined target mint scope (typed, no native mint).
        for name, item in owners.names.items():
            if item['target'] is not None and (name, 'target') not in nothing_to_do:
                lab.check(f's2g_{name}_target_journal_quarantined', lr.is_quarantined(MintJournal(item['target']), item['scope']))
        lost = 'mint-after_native_reply'
        for name, item in owners.names.items():
            for side, owner in (('home', home), ('target', target)):
                if item[side] is None or (name == LINK_SCOPE and side == 'home') or (name, side) in nothing_to_do:
                    continue
                confirm = owners.confirm(side, name)
                lab.secret_values.append(confirm)
                status, body = post(owner, dict(op='reconcile', scope_id=item['scope_id'], confirm=confirm))
                results[name + ':' + side] = (status, body)
                if name == lost:
                    lab.check('s2g_lost_bearer_awaiting_expiry_not_revoked',
                              (status, body) == (409, {'error': 'reconcile_awaiting_expiry'})
                              and owners.native(lost)['reservations'][0]['revoked_at'] is None, response=body)
                else:
                    lab.check(f's2g_{name}_{side}_reconciled', status == 200 and body['status'] == 'reconciled', response=body)
        # Lost bearer: no revoke-by-id. Wait for proven expiry, then record expired_unrevocable.
        expires_at = owners.native(lost)['reservations'][0]['expires_at']
        lab.report['s2g_lost_bearer_wait'] = {'expires_at': expires_at, 'waited_from': time.time()}
        lab.flush()
        while time.time() <= expires_at + 1:
            time.sleep(min(15, max(.5, expires_at + 1.5 - time.time())))
        confirm = owners.confirm('target', lost)
        status, body = post(target, dict(op='reconcile', scope_id=owners.names[lost]['scope_id'], confirm=confirm))
        results[lost + ':target'] = (status, body)
        lab.check('s2g_lost_bearer_expired_unrevocable', status == 200
                  and body['outcome'] == {'room': [], 'reservation': ['expired_unrevocable']} and body['actions'] == []
                  and owners.native(lost)['reservations'][0]['revoked_at'] is None, response=body)
        lab.report.setdefault('s2g_reconcile', {}).update({k: v[1] for k, v in results.items()})
        # Native RO readback of every scope.
        native = {name: owners.native(name) for name in owners.names}
        lab.report['s2g_native_after'] = native
        rooms = {n: v['rooms'] for n, v in native.items() if v['rooms']}
        lab.check('s2g_native_rooms_tombstoned', sorted(rooms) == ['create-after_native_reply', 'register-after_intent', LINK_SCOPE]
                  and all(r[0]['disbanded_at'] is not None for r in rooms.values()), rooms=rooms)
        lab.check('s2g_native_links_removed', all(v['links'] == [] for v in native.values()))
        now = time.time()
        states = {n: ['revoked' if r['revoked_at'] is not None else 'expired' if r['expires_at'] <= now else 'live'
                      for r in v['reservations']] for n, v in native.items()}
        lab.check('s2g_native_reservations_non_live', states == {
            'create-after_intent': ['revoked'], 'create-after_native_reply': ['revoked'], 'register-after_intent': ['revoked'],
            LINK_SCOPE: ['revoked'], 'mint-after_intent': [], 'mint-after_native_reply': ['expired']}, states=states)
        revoke_calls = sum(a['action'] == 'groups.peer.revoke' for r in lab.report['s2g_reconcile'].values()
                           if isinstance(r, dict) for a in r.get('actions', []))
        lab.check('s2g_exactly_kept_bearers_revoked', revoke_calls == 3, revoke_calls=revoke_calls)
        # Journals intact; ledger only appended.
        lab.check('s2g_journals_intact', owners.journals() == journals_before)
        ledgers = owners.ledgers()
        lab.report['s2g_ledgers'] = ledgers
        lab.check('s2g_ledger_appended', ledgers[LINK_SCOPE + ':home'] == ['quarantined', 'reconcile_blocked', 'reconcile_blocked', 'reconciled']
                  and ledgers[lost + ':target'] == ['quarantined', 'reconcile_blocked', 'reconciled']
                  and ledgers[LINK_SCOPE + ':target'] == []
                  and all(v[-1] == 'reconciled' for k, v in ledgers.items() if k != LINK_SCOPE + ':target'),
                  ledgers=ledgers)
        # Idempotent re-run: no native call, identical readback, no ledger growth.
        again = {}
        for name, item in owners.names.items():
            for side, owner in (('home', home), ('target', target)):
                if item[side] is not None and (name, side) not in nothing_to_do:
                    again[name + ':' + side] = post(owner, dict(op='reconcile', scope_id=item['scope_id'],
                                                                confirm=owners.confirm(side, name)))
        lab.check('s2g_rerun_idempotent', all(s == 200 and b['status'] == 'already_reconciled' and b['identical_readback']
                                              for s, b in again.values())
                  and owners.ledgers() == ledgers and {n: owners.native(n) for n in owners.names} == native, rerun=again)
        blob = json.dumps([results, again, owners.joined.inventory()])
        lab.check('s2g_outputs_sanitized', all(g not in blob for g in owners.grants) and 'HermesRoom ' not in blob)
    lab.report['s2g_inventory_after'] = owners.joined.inventory()


def q5_after(lab, owners, hr, tr, loop):
    with Scenario(lab, 'q5-new-invite', hr, tr, loop) as recovery:
        recovery.check('new_owner_confirmed_scope', all(recovery.scope[k] != item['scope'][k]
                       for item in owners.names.values() for k in ('lobby_id', 'invite_id', 'room_id')))
        recovery.prepare()
        recovery.succeed()
    # Evidence for the idle-backlog fix: the lab sockets sat idle through the lost-bearer
    # wait while owner reconcile used its own sockets; their queued notifications are drained.
    lab.report['s2g_idle_backlog_drained'] = {'host': hr.drained_notifications, 'target': tr.drained_notifications}
    # Old scopes stay quarantined + reconciled after the new invite; nothing was replayed.
    for name, item in owners.names.items():
        if item['home'] is not None:
            lab.check(f's2g_{name}_still_quarantined', lr.is_quarantined(OperationJournal(item['home']).storage, item['scope']))
    final = {n: owners.native(n) for n in owners.names}
    lab.check('s2g_old_scopes_still_terminal_after_q5', all(v['links'] == [] for v in final.values())
              and all(r['disbanded_at'] is not None for v in final.values() for r in v['rooms'])
              and all(r['revoked_at'] is not None or r['expires_at'] <= time.time() for v in final.values() for r in v['reservations']),
              native=final)
    mints = [r for r in map(json.loads, lab.audit.read_text().splitlines()) if r.get('event') == 'native_issue_room_grant_return']
    counts = {r['room_id']: sum(m['room_id'] == r['room_id'] for m in mints) for r in mints}
    lab.check('s2g_all_scopes_no_duplicate_mint', bool(counts) and set(counts.values()) == {1}, counts=counts)


def main():
    from s2e_activation_faults import install_native_observer
    lab = gs.NativeLab()
    lab.out = gs.gc.ROOT / 'evidence/s2/followups' / ('s2g-native-' + lab.stamp)
    lab.out.mkdir(parents=True, mode=0o700)
    lab.report['scope'] = 'S2 follow-up: owner-explicit orphan reconciliation; one-machine actual boundaries; F1 CLOSED'
    lab.secret_values = []
    lab.secret_bytes = []
    start = hashes()
    lab.report['s2g_source_start'] = start
    try:
        with gs.native_signals(lab.report):
            install_native_observer(gs)
            lab.setup()
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                lab.target_tui_port = sock.getsockname()[1]
            lab.target_ws_key = secrets.token_hex(32)
            token = lab.target / '.hermes/desktop-ssh' / secrets.token_hex(16) / (secrets.token_hex(8) + '.token')
            token.parent.mkdir(parents=True, mode=0o700)
            token.write_text(lab.target_ws_key)
            token.chmod(0o600)
            lab.target_token_file = lab.out / 'target-native-private.token'
            lab.target_token_file.write_text(lab.target_ws_key)
            lab.target_token_file.chmod(0o600)
            lab.host_token_file = lab.out / 'host-native-private.token'
            lab.host_token_file.write_text(lab.keys[2])
            lab.host_token_file.chmod(0o600)
            lab.args['target-tui'] = ['serve', '--isolated', '--host', '127.0.0.1', '--port', str(lab.target_tui_port),
                                      '--ssh-session-token-file', str(token)]
            lab.target_tui = lab.spawn('target-tui')
            asyncio.run(native(lab))
    except BaseException as exc:
        lab.report['error_type'] = type(exc).__name__
        if isinstance(exc, AssertionError):
            lab.report['failed_check'] = str(exc) if str(exc) in lab.report['checks'] else 'unclassified_assertion'
    finally:
        lab.close()
    if hasattr(lab, 'target_tui_port'):
        lab.check('extra_native_port_free', gs.gc.pf.check_port_available(lab.target_tui_port)['available'], port=lab.target_tui_port)
    logs = list(lab.out.rglob('*.log')) + list(lab.out.rglob('report.json')) + list(lab.out.rglob('*.jsonl'))
    values = [v.encode() for v in lab.secret_values + lab.keys + ([lab.target_ws_key] if hasattr(lab, 'target_ws_key') else [])]
    values.extend(lab.secret_bytes)
    for p in lab.out.rglob('*'):
        if p.is_file() and (p.suffix in ('.key', '.token') or p.parent.name == 'creds'):
            values.append(p.read_bytes() if p.suffix == '.key' else p.read_bytes().strip())
    import re
    lab.report['secret_scan'] = {'files': len(logs), 'exact_hits': sum(v in p.read_bytes() for v in values if v for p in logs),
        'grant_or_private_key_hits': sum(bool(re.search(rb'HermesRoom [A-Za-z0-9_.-]{40,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----', p.read_bytes())) for p in logs)}
    lab.report['s2g_source_end'] = hashes()
    lab.report['native_checks_passed'] = bool(not lab.report.get('error_type') and lab.report.get('teardown_complete')
        and all(c['ok'] for c in lab.report['checks'].values()) and start == hashes()
        and lab.report['secret_scan']['exact_hits'] == lab.report['secret_scan']['grant_or_private_key_hits'] == 0
        and lab.report['source_start']['source_sha256'] == lab.report['source_end']['source_sha256'])
    lab.flush()
    print(json.dumps({'report': str(lab.out / 'report.json'), 'native_checks_passed': lab.report['native_checks_passed'],
                      'checks': len(lab.report['checks']), 'error_type': lab.report.get('error_type'),
                      'failed_check': lab.report.get('failed_check')}))
    return 0 if lab.report['native_checks_passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
