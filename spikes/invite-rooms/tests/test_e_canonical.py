"""Structural validator regressions, not native evidence."""
import copy
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gate_s1_native as native


class ECanonicalTests(unittest.TestCase):
    def test_target_connector_requires_exact_task_receipt_and_canonical_reply(self):
        pairs = [{'seq': 1, 'event_id': 'baseline'}, {'seq': 2, 'event_id': 'user'},
                 {'seq': 3, 'event_id': 'reply'}]
        reply = dict(seq=3, event_id='reply', kind='message.member',
                     payload=dict(task_id='task', member_id='peer-b'))
        e = dict(user_event_id='user', user_seq=2, run_id='run', task_id='task',
                 dispatch_generation=1, idempotency_key='room:task:1', final='completed',
                 running_before_fault=[dict(http=200, status='running', run_id='run', execution_generation=7)],
                 receipts=[['run']], receipts_before=[['run']], before=pairs[:1], canonical=pairs,
                 db=pairs, reply=reply, task_replies=[reply],
                 driver_continuity=dict(observer_baseline=1, window_start=1, window_end=2,
                     events=[], lease_before=[dict(lease_generation=1)],
                     lease_after=[dict(lease_generation=1)], task=[dict(indeterminate_at=None)]),
                 fault=dict(kind='target_connector', old_incarnation='old', new_incarnation='new',
                            old_stopped=True, relay_unchanged=True, elapsed=1, bound_seconds=30))
        self.assertTrue(native.valid_e_canonical(e))
        for mutate in (
            lambda x: x.pop('driver_continuity'),
            lambda x: x['driver_continuity'].update(observer_baseline=0),
            lambda x: x['driver_continuity'].update(events=[dict(method='_drop_lease.enter')]),
            lambda x: x['driver_continuity'].update(events=[dict(method='_mark_ambiguous.enter')]),
            lambda x: x['driver_continuity'].update(lease_after=[dict(lease_generation=2)]),
            lambda x: x['driver_continuity'].update(task=[dict(indeterminate_at=1)]),
            lambda x: x.update(task_id='other'),
            lambda x: x.update(idempotency_key='room:task:7'),
            lambda x: x.update(receipts_before=[]),
            lambda x: x.update(task_replies=[reply, reply]),
            lambda x: x['fault'].update(kind='relay'),
            lambda x: x['fault'].update(old_stopped=False),
            lambda x: x['fault'].update(relay_unchanged=False),
            lambda x: x['fault'].update(elapsed=31),
            lambda x: x['db'].reverse(),
            lambda x: x.update(running_before_fault=[]),
            lambda x: x.update(canonical=x['before']),
        ):
            bad = copy.deepcopy(e)
            mutate(bad)
            self.assertFalse(native.valid_e_canonical(bad))


if __name__ == '__main__': unittest.main()
