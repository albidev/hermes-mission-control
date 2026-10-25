"""P3b.3c-H: R3B-04 capacity/lane/off-loop guarantees and the executor's deterministic owner shutdown.

These tests exercise capacity that does not exist on the pre-fix snapshot (bounded submissions, a
reserved callback lane, an owner shutdown), so they cannot be RED there by design; the pre-fix
behavioural RED lives in ``test_room_invite_p3b3c_h.py``.
"""
from __future__ import annotations

import asyncio
import threading
import time
import unittest
from unittest.mock import patch

try:
    from server.tests import test_room_invite_material_receipts as M
except ModuleNotFoundError:
    import test_room_invite_material_receipts as M

from invite_rooms import peer_registry as pr
from invite_rooms.material_receipts import ADMISSION_DEADLINE
from invite_rooms.peer_registry import (
    LANE_CALLBACK, LANE_MATERIAL, PeerRegistryError, admission_executor, run_admission,
    shutdown_admission_executor,
)


def worker_threads():
    return {t for t in threading.enumerate() if t.name.startswith("invite-admission")}


class BlockingJobs:
    """Occupy admission workers deterministically until ``release`` is called."""

    def __init__(self, count, lane=LANE_MATERIAL):
        self.go = threading.Event()
        self.entered = [threading.Event() for _ in range(count)]
        self.jobs = [asyncio.create_task(run_admission(self._block, index, lane=lane)) for index in range(count)]

    def _block(self, index):
        self.entered[index].set()
        self.go.wait(5.0)

    async def wait_entered(self, count=None):
        for event in self.entered[:count or len(self.entered)]:
            if not await asyncio.to_thread(event.wait, 5.0):
                raise AssertionError("an admission worker never started")

    async def release(self):
        self.go.set()
        await asyncio.gather(*self.jobs)


class ExecutorCapacityTests(M.TmpCase, unittest.IsolatedAsyncioTestCase):
    async def test_all_workers_busy_returns_a_refusal_not_a_late_success(self):
        reg, store = M.new_registry(self.tmp, route_provider=lambda _: 19001)
        scope = M.make_scope(reg)
        store.admit(scope, M.material_for(scope, M.GRANT_A))
        blockers = BlockingJobs(admission_executor().workers, lane=LANE_CALLBACK)
        try:
            await blockers.wait_entered()
            token = ADMISSION_DEADLINE.set(time.monotonic() + 0.02)
            start = time.monotonic()
            try:
                with self.assertRaises(PeerRegistryError) as cm:
                    await reg.resolve_scoped_material(scope)
            finally:
                ADMISSION_DEADLINE.reset(token)
            elapsed = time.monotonic() - start
        finally:
            await blockers.release()
        self.assertEqual(str(cm.exception), "material_unavailable")
        self.assertLess(elapsed, 0.12, "the read must expire at its deadline, not return ~150ms later")

    async def test_a_receipt_write_runs_while_every_material_slot_is_busy(self):
        reg, store = M.new_registry(self.tmp, route_provider=lambda _: 19001)
        scope = M.make_scope(reg)
        material_slots = admission_executor().workers - admission_executor().reserved
        blockers = BlockingJobs(material_slots, lane=LANE_MATERIAL)
        seen = []
        real = reg.admit_signed_material

        def spy(*args, **kwargs):
            seen.append(threading.current_thread())
            return real(*args, **kwargs)

        try:
            await blockers.wait_entered()
            with patch.object(reg, "admit_signed_material", spy):
                await asyncio.wait_for(
                    reg.admit_signed_material_async(scope, M.envelope_for(scope, M.GRANT_A)), 3.0)
        finally:
            await blockers.release()
        self.assertTrue((store.root / store.receipt_name(scope)).exists(), "the reserved lane must admit")
        self.assertEqual(len(seen), 1)
        self.assertTrue(seen[0].name.startswith("invite-admission"), "the write must run off the loop thread")
        self.assertIsNot(seen[0], threading.main_thread())

    async def test_submissions_beyond_the_queue_cap_are_refused_with_a_stable_code(self):
        executor = admission_executor()
        capacity = executor.workers + executor.queue_cap
        blockers = BlockingJobs(capacity, lane=LANE_CALLBACK)
        try:
            await blockers.wait_entered(executor.workers)
            with self.assertRaises(PeerRegistryError) as cm:
                await run_admission(lambda: None, lane=LANE_CALLBACK)
            self.assertEqual(str(cm.exception), "admission_overloaded")
        finally:
            await blockers.release()
        self.assertEqual(await run_admission(lambda: "fits-again", lane=LANE_CALLBACK), "fits-again")

    async def test_an_expired_deadline_is_refused_before_submission(self):
        with self.assertRaises(PeerRegistryError) as cm:
            await run_admission(lambda: "never", lane=LANE_CALLBACK, end_deadline=time.monotonic() - 1.0)
        self.assertEqual(str(cm.exception), "admission_deadline_exceeded")

    def test_unknown_lane_is_refused(self):
        with self.assertRaises(PeerRegistryError) as cm:
            asyncio.run(run_admission(lambda: None, lane="receipt"))
        self.assertEqual(str(cm.exception), "admission_lane_invalid")


class OwnerShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_shutdown_refuses_work_on_the_old_owner_and_the_next_owner_is_fresh(self):
        executor = admission_executor()
        shutdown_admission_executor()
        with self.assertRaises(PeerRegistryError) as cm:
            await executor.run(lambda: None)
        self.assertEqual(str(cm.exception), "admission_unavailable")
        self.assertEqual(await run_admission(lambda: "fresh"), "fresh")

    async def test_shutdown_joins_every_admission_worker(self):
        self.assertEqual(await run_admission(lambda: "warm"), "warm")
        self.assertTrue(worker_threads(), "the executor must actually own worker threads")
        shutdown_admission_executor()
        self.assertFalse(worker_threads(), "admission workers outlived the owner shutdown")


if __name__ == "__main__":
    unittest.main()
