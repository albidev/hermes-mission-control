"""SOL-01 cancellation/deadline lifetime regressions."""
from __future__ import annotations

import asyncio
import threading
import unittest

from invite_rooms import peer_registry as pr


class AdmissionPhysicalLifetimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_waiters_cannot_grow_physical_queue(self):
        executor = pr.AdmissionExecutor(workers=4, queue_cap=8, reserved=1)
        release = threading.Event()
        entered = [threading.Event() for _ in range(executor.workers)]

        def block(index):
            entered[index].set()
            release.wait(5.0)

        live = [asyncio.create_task(executor.run(block, index, lane=pr.LANE_CALLBACK))
                for index in range(executor.workers)]
        try:
            for event in entered:
                self.assertTrue(await asyncio.to_thread(event.wait, 2.0))
            for _ in range(40):
                task = asyncio.create_task(executor.run(lambda: None, lane=pr.LANE_CALLBACK))
                await asyncio.sleep(0)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except pr.PeerRegistryError as exc:
                    self.assertEqual(str(exc), "admission_overloaded")
            queued = executor._pool._work_queue.qsize()
            self.assertLessEqual(queued, executor.queue_cap)
        finally:
            release.set()
            await asyncio.gather(*live, return_exceptions=True)
            executor.shutdown()

    async def test_sequential_material_cancellation_retains_capacity(self):
        executor = pr.AdmissionExecutor()
        release = threading.Event()
        entered = [threading.Event() for _ in range(executor.workers)]
        jobs = []

        def material(index):
            entered[index].set()
            release.wait(5.0)

        try:
            material_capacity = executor.workers - executor.reserved
            for index in range(material_capacity):
                task = asyncio.create_task(executor.run(material, index))
                jobs.append(task)
                self.assertTrue(await asyncio.to_thread(entered[index].wait, 2.0))
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            for _ in range(40):
                with self.assertRaises(pr.PeerRegistryError) as cm:
                    await executor.run(material, material_capacity)
                self.assertEqual(str(cm.exception), "admission_overloaded")
            self.assertEqual(sum(event.is_set() for event in entered), material_capacity)
            self.assertEqual(await asyncio.wait_for(
                executor.run(lambda: "callback", lane=pr.LANE_CALLBACK), 0.5), "callback")
            self.assertFalse(entered[material_capacity].is_set())
        finally:
            release.set()
            await asyncio.gather(*jobs, return_exceptions=True)
            executor.shutdown()

    async def test_cancelled_queued_callback_retains_capacity_and_executes(self):
        executor = pr.AdmissionExecutor(workers=1, queue_cap=1, reserved=0)
        release, started, written = (threading.Event() for _ in range(3))

        def block():
            started.set()
            release.wait(5.0)

        live = asyncio.create_task(executor.run(block, lane=pr.LANE_CALLBACK))
        queued = None
        try:
            self.assertTrue(await asyncio.to_thread(started.wait, 2.0))
            queued = asyncio.create_task(executor.run(written.set, lane=pr.LANE_CALLBACK))
            await asyncio.sleep(0)
            queued.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await queued
            self.assertFalse(written.is_set())
            with self.assertRaises(pr.PeerRegistryError) as cm:
                await executor.run(lambda: None, lane=pr.LANE_CALLBACK)
            self.assertEqual(str(cm.exception), "admission_overloaded")
            release.set()
            self.assertTrue(await asyncio.to_thread(written.wait, 2.0))
            await live
        finally:
            release.set()
            pending = [live] if queued is None else [live, queued]
            await asyncio.gather(*pending, return_exceptions=True)
            executor.shutdown()

    async def test_cancelled_callback_owns_work_until_completion(self):
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def callback():
            started.set()
            release.wait(5.0)
            finished.set()

        executor = pr.AdmissionExecutor()
        task = asyncio.create_task(executor.run(callback, lane=pr.LANE_CALLBACK))
        try:
            self.assertTrue(await asyncio.to_thread(started.wait, 2.0))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertFalse(finished.is_set())
            release.set()
            self.assertTrue(await asyncio.to_thread(finished.wait, 2.0))
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            executor.shutdown()


if __name__ == "__main__":
    unittest.main()
