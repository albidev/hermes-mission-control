"""Journal unit boundaries, NOT native interoperability/admission substitutes."""
import concurrent.futures
from pathlib import Path
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(HERE), str(HERE.parents[1] / 'server')]
ROOT = Path('/Users/albi/.hermes/cache/scratch/room-network-spike-20261011-164930')


class JournalTests(unittest.TestCase):
    def test_local_owner_retains_typed_ambiguous_activation_codes(self):
        from lobby_activation import OperationAmbiguous
        from lobby_owner import OwnerSurface
        from gate_s2b_native import post
        with tempfile.TemporaryDirectory(dir=ROOT, prefix='s2d-owner-') as tmp:
            def blocked(payload):
                raise OperationAmbiguous(payload['stage'])
            with OwnerSurface(Path(tmp), {'activate': blocked}) as owner:
                for stage, code in [('create', 'creation_ambiguous'), ('register:peer-b', 'registration_ambiguous'), ('send', 'send_ambiguous')]:
                    self.assertEqual(post(owner, {'op': 'activate', 'stage': stage}), (409, {'error': code}))

    def test_durable_exclusive_activation_intent_never_replays(self):
        from lobby_activation import OperationJournal, OperationAmbiguous
        with tempfile.TemporaryDirectory(dir=ROOT, prefix='s2d-unit-') as tmp:
            journal = OperationJournal(Path(tmp))
            scope = {'lobby_id': 'journal-unit', 'room_id': 'storage-only'}
            def begin(_):
                try:
                    return journal.begin(scope, 'create')
                except OperationAmbiguous as exc:
                    return str(exc)
            with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
                results = list(pool.map(begin, range(20)))
            self.assertEqual(results.count(None), 1)
            self.assertEqual(results.count('creation_ambiguous'), 19)
            resumed = OperationJournal(Path(tmp))
            with self.assertRaisesRegex(OperationAmbiguous, '^creation_ambiguous$'):
                resumed.begin(scope, 'create')
            journal.complete(scope, 'create', {'room_id': 'storage-only'})
            self.assertEqual(resumed.begin(scope, 'create'), {'room_id': 'storage-only'})
            with self.assertRaisesRegex(OperationAmbiguous, '^registration_ambiguous$'):
                resumed.begin(scope, 'register:peer-b')
                OperationJournal(Path(tmp)).begin(scope, 'register:peer-b')
            self.assertEqual(journal.path.stat().st_mode & 0o777, 0o600)


if __name__ == '__main__':
    unittest.main()
