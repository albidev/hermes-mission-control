"""Lab-only sanitized observation; no native acceptance is simulated here."""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lab_driver_observer import install_driver_observer

class DriverObserverTests(unittest.TestCase):
    def test_exception_context_and_generation_without_credentials(self):
        class Driver:
            def __init__(self): self._leases = {'room:test': SimpleNamespace(lease_generation=1)}
            def _mark_ambiguous(self, binding, attempt): self._drop_lease(binding.room_id)
            def _drop_lease(self, room_id): self._leases.pop(room_id, None)
            def _record_task_error(self, attempt, message): pass
            def _record_error(self, message): pass
            def _ensure_lease(self, binding): return SimpleNamespace(room_id=binding.room_id, lease_generation=2)
            def _renew_lease_if_needed(self, lease, force=False): return lease
        rows = []
        install_driver_observer(Driver, rows.append)
        driver = Driver()
        binding = SimpleNamespace(room_id='room:test')
        attempt = SimpleNamespace(identity=SimpleNamespace(room_id='room:test', task_id='task:test'))
        try:
            raise ConnectionError('Bearer PRIVATE-TOKEN grant=PRIVATE-GRANT')
        except ConnectionError:
            driver._mark_ambiguous(binding, attempt)
            driver._record_task_error(attempt, 'PRIVATE-TOKEN')
        driver._ensure_lease(binding)
        self.assertEqual(rows[0]['exception_class'], 'ConnectionError')
        self.assertEqual(rows[0]['lease_generation'], 1)
        self.assertEqual(rows[0]['task_id'], 'task:test')
        self.assertTrue(any(r['method'] == '_ensure_lease.return' and r['lease_generation'] == 2 for r in rows))
        self.assertNotIn('PRIVATE', str(rows))
        self.assertEqual(set(rows[0]), {'timestamp','method','room_id','task_id','lease_generation','exception_class','message'})
