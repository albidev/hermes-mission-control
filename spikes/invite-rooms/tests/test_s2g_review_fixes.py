"""Independent delta review (t_70d0c17e) findings ASTRA-R2 and ASTRA-R3.

R2: a configured native DB that is missing or uninitialised is NOT proof that the
    Room/reservation is absent. Readback must fail closed and reconcile must not
    act or record ``reconciled``.
R3: the pre-request idle drain has its own frame/byte budget and a monotonic
    deadline, and yields to the loop so cancellation/timeouts can fire.
"""
import asyncio
import contextlib
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

import aiohttp

HERE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(HERE), str(HERE.parents[1] / 'server')]

import lobby_reconcile as lr  # noqa: E402
from lobby_native_rpc import BoundedNativeRpc, NativeRpcAmbiguous  # noqa: E402

SCOPE = {'lobby_id': 'l', 'invite_id': 'i', 'room_id': 'invite-room-r2', 'member_id': 'm',
         'host_id': 'h', 'binding': {'target_profile': 'default'}}


class Journal:
    def __init__(self, path, table, row):
        self.path = path
        with self._db() as db:
            if table == 'operations':
                db.execute('CREATE TABLE operations(scope TEXT, operation TEXT, state TEXT)')
            else:
                db.execute('CREATE TABLE material(scope TEXT, state TEXT)')
            db.execute(f'INSERT INTO {table} VALUES({",".join("?" * len(row))})', row)

    @contextlib.contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, isolation_level=None)
        try:
            yield db
        finally:
            db.close()

    def load(self, scope):
        raise AssertionError('bearer must not be loaded when readback is unavailable')


def native_db(path, *, room=True, reservation=True):
    with closing_db(path) as db:
        db.execute('CREATE TABLE hosted_rooms(room_id TEXT, disbanded_at REAL)')
        db.execute('CREATE TABLE hosted_room_links(room_id TEXT, member_id TEXT, status TEXT)')
        db.execute('CREATE TABLE hosted_room_peer_reservations(room_id TEXT, member_id TEXT, '
                   'target_profile TEXT, expires_at REAL, revoked_at REAL)')
        if room:
            db.execute('INSERT INTO hosted_rooms VALUES(?,NULL)', (SCOPE['room_id'],))
        if reservation:
            db.execute('INSERT INTO hosted_room_peer_reservations VALUES(?,?,?,?,NULL)',
                       (SCOPE['room_id'], SCOPE['member_id'], 'default', time.time() + 600))


@contextlib.contextmanager
def closing_db(path):
    db = sqlite3.connect(path)
    try:
        yield db
        db.commit()
    finally:
        db.close()


class ReadbackFailsClosed(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='s2g-r2-', dir=os.environ.get('TMPDIR')))
        self.calls = []

    async def rpc(self, method, params):
        self.calls.append(method)
        return {'tombstone': {'room_id': SCOPE['room_id']}}

    def test_missing_and_uninitialised_dbs_are_unknown_not_absent(self):
        empty = self.tmp / 'empty.sqlite'
        sqlite3.connect(empty).close()
        for path in (self.tmp / 'missing.sqlite', empty):
            self.assertEqual(lr.read_home(path, SCOPE['room_id'], 'm')['room'], 'unknown')
            self.assertEqual(lr.read_target(path, SCOPE['room_id'], 'm', 'default')['reservation'], 'unknown')
        self.assertFalse((self.tmp / 'missing.sqlite').exists(), 'readback must never create a DB')

    def test_initialised_empty_db_is_still_proven_absent(self):
        db = self.tmp / 'native.sqlite'
        native_db(db, room=False, reservation=False)
        self.assertEqual(lr.read_home(db, SCOPE['room_id'], 'm')['room'], 'absent')
        self.assertEqual(lr.read_target(db, SCOPE['room_id'], 'm', 'default')['reservation'], 'absent')

    async def quarantined(self, home_db=None, target_db=None):
        homes, targets, journals = [], [], []
        if home_db is not None:
            j = Journal(self.tmp / 'home-journal.sqlite', 'operations', (json.dumps(SCOPE), 'create', 'intent'))
            homes.append(lr.HomeSide(j, self.rpc, home_db))
            journals.append(j)
        if target_db is not None:
            j = Journal(self.tmp / 'target-journal.sqlite', 'material', (json.dumps(SCOPE), 'ready'))
            targets.append(lr.TargetSide(j, self.rpc, target_db))
            journals.append(j)
        rec = lr.Reconciler(self.tmp / 'owner', homes=homes, targets=targets)
        sid = lr.scope_id(SCOPE)
        confirm = 'owner-confirmation'
        for j in journals:
            lr.ledger_append(j, sid, 'quarantined', {'confirm_sha256': hashlib.sha256(confirm.encode()).hexdigest()})
        return rec, sid, confirm, journals

    async def test_reconcile_blocks_without_native_action_when_home_db_unavailable(self):
        rec, sid, confirm, journals = await self.quarantined(home_db=self.tmp / 'missing-home.sqlite')
        with self.assertRaises(lr.ReconcileBlocked) as caught:
            await rec.reconcile({'scope_id': sid, 'confirm': confirm})
        self.assertEqual(caught.exception.code, 'reconcile_readback_unavailable')
        self.assertEqual(self.calls, [])
        self.assertFalse(any(r['kind'] == 'reconciled' for r in lr.ledger_rows(journals[0], sid)))

    async def test_reconcile_blocks_when_target_db_unavailable(self):
        home = self.tmp / 'home.sqlite'
        native_db(home, room=False, reservation=False)
        rec, sid, confirm, journals = await self.quarantined(home_db=home, target_db=self.tmp / 'missing-target.sqlite')
        with self.assertRaises(lr.ReconcileBlocked) as caught:
            await rec.reconcile({'scope_id': sid, 'confirm': confirm})
        self.assertEqual(caught.exception.code, 'reconcile_readback_unavailable')
        self.assertEqual(self.calls, [])
        for j in journals:
            self.assertFalse(any(r['kind'] == 'reconciled' for r in lr.ledger_rows(j, sid)))

    async def test_inventory_reports_unknown_instead_of_failing(self):
        rec, sid, _, _ = await self.quarantined(home_db=self.tmp / 'missing-home.sqlite')
        view = rec.inventory()['scopes'][0]
        self.assertEqual(view['native']['home'][0]['room'], 'unknown')
        self.assertIn('readback_unavailable', view['classes'])
        self.assertNotIn('create_intent_no_room', view['classes'])


class FakeWS:
    """Buffered receive semantics: returns synchronously while frames remain."""

    def __init__(self, count, size=64, per_frame_sleep=0.0):
        self.remaining, self.sent, self.per_frame_sleep = count, [], per_frame_sleep
        self.note = aiohttp.WSMessage(aiohttp.WSMsgType.TEXT, json.dumps(
            {'jsonrpc': '2.0', 'method': 'event', 'params': {'payload': 'x' * size}}), '')

    async def receive(self, timeout=None):
        if self.remaining:
            self.remaining -= 1
            if self.per_frame_sleep:
                time.sleep(self.per_frame_sleep)  # CPU-bound, never yields
            return self.note
        await asyncio.sleep(timeout or 1)
        raise TimeoutError()

    async def send_str(self, raw):
        self.sent.append(raw)


class DrainIsBounded(unittest.IsolatedAsyncioTestCase):
    async def test_frame_flood_is_capped_and_poisons_without_sending(self):
        ws = FakeWS(5000)
        rpc = BoundedNativeRpc(ws, timeout=5)
        with self.assertRaises(NativeRpcAmbiguous):
            await rpc('groups.capabilities', {})
        self.assertEqual(ws.sent, [])
        self.assertTrue(rpc._poisoned)
        self.assertLess(rpc.drained_notifications, 5000)

    async def test_byte_flood_is_capped_before_frame_cap(self):
        ws = FakeWS(200, size=16000)
        rpc = BoundedNativeRpc(ws, timeout=5)
        with self.assertRaises(NativeRpcAmbiguous):
            await rpc('groups.capabilities', {})
        self.assertEqual(ws.sent, [])
        self.assertLess(rpc.drained_notifications, 200)

    async def test_drain_respects_monotonic_deadline_even_without_yielding_receive(self):
        ws = FakeWS(100, per_frame_sleep=0.004)
        rpc = BoundedNativeRpc(ws, timeout=0.05)
        start = time.monotonic()
        with self.assertRaises(NativeRpcAmbiguous):
            await rpc('groups.capabilities', {})
        self.assertLess(time.monotonic() - start, 0.05 + 0.03)
        self.assertEqual(ws.sent, [])

    async def test_normal_backlog_still_drains_and_request_is_sent(self):
        class Answering(FakeWS):
            async def receive(self, timeout=None):
                if self.remaining:
                    return await super().receive(timeout)
                if self.sent:
                    rid = json.loads(self.sent[-1])['id']
                    return aiohttp.WSMessage(aiohttp.WSMsgType.TEXT, json.dumps(
                        {'jsonrpc': '2.0', 'id': rid, 'result': {'ok': True}}), '')
                return await super().receive(timeout)
        ws = Answering(40)
        rpc = BoundedNativeRpc(ws, timeout=5)
        self.assertEqual(await rpc('groups.capabilities', {}), {'ok': True})
        self.assertEqual(rpc.drained_notifications, 40)
        self.assertFalse(rpc._poisoned)


if __name__ == '__main__':
    unittest.main(verbosity=2)
