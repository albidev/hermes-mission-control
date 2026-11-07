"""P3b-B1: ``JournalLinkAuthority.admit`` is the journal-authoritative per-effect admission (R1-05).

Real ``Journal`` / ``PairingService`` state only: no fixture authority.  The dispatcher / connector seams are driven
with the real authority, so a stale effect that slips past a permissive ``admit`` is actually invoked (and counted).
"""
from __future__ import annotations

import asyncio
import fcntl
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pairing_support as ps  # noqa: E402
from invite_rooms import pairing_journal as pj  # noqa: E402
from invite_rooms.control import dispatcher as dispatcher_mod  # noqa: E402
from invite_rooms.control.connector import FirewallDenied, LinkBinding, _Admission  # noqa: E402
from invite_rooms.control.dispatcher import ControlDispatcher, ControlError  # noqa: E402

TEMPLATES: dict = {}
ACK_OPS = sorted((dispatcher_mod._ALLOWED - {"consent"}) | {"joining_consent", "data_send", "data_native"})


def setUpModule():
    for i in (ps.INSTALL_A, ps.INSTALL_B):
        TEMPLATES[i] = ps.identity_template(i)


def tearDownModule():
    for path in TEMPLATES.values():
        shutil.rmtree(path.parent, ignore_errors=True)


class AdmissionBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(dir=os.environ.get("MC_TEST_TMP")))
        os.chmod(self.tmp, 0o700)
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.clock = ps.Clock()
        self.A = ps.Side(self.tmp, "a", ps.INSTALL_A, template=TEMPLATES[ps.INSTALL_A])
        self.B = ps.Side(self.tmp, "b", ps.INSTALL_B, template=TEMPLATES[ps.INSTALL_B])
        self.a, self.b = self.A.service(self.clock), self.B.service(self.clock)

    def confirmed(self):
        self.r = ps.drive_pairing(self.a, self.b)
        self.endpoint = self.A.peers()[0]["endpoint_id"]
        self.peer_id = self.A.peers()[0]["peer_id"]
        return self.r

    def acknowledged(self):
        self.confirmed()
        self.a.send_pair_ack(self.r["a_id"], ps.link_sender(ps.dispatcher_for(self.b)))
        self.assertEqual(self.a.authority.current(self.endpoint).protocol_state, "acknowledged")
        return self.r

    def dispatcher(self, handlers, **kw):
        return ControlDispatcher(self.a.authority, handlers=handlers, admit=self.a.authority.admit,
                                 owner_verify=lambda *_: True, **kw)


class AdmitMatrixTests(AdmissionBase):
    def test_acknowledged_exact_link_admits_every_allowed_operation_on_both_roles(self):
        self.acknowledged()
        for authority in (self.a.authority, self.b.authority):
            for op in ACK_OPS:
                self.assertIs(authority.admit(self.endpoint, 1, op), True, op)

    def test_confirmed_admits_only_pair_ack(self):
        self.confirmed()
        for authority in (self.a.authority, self.b.authority):
            self.assertEqual(authority.current(self.endpoint).protocol_state, "confirmed")
            self.assertIs(authority.admit(self.endpoint, 1, "pair_ack"), True)
            for op in ACK_OPS:
                if op != "pair_ack":
                    self.assertIs(authority.admit(self.endpoint, 1, op), False, op)

    def test_operation_set_is_the_dispatcher_allowlist_not_a_copy(self):
        self.assertEqual(pj._ADMIT_OPERATIONS,
                         (dispatcher_mod._ALLOWED - {"consent"}) | {"joining_consent", "data_send", "data_native"})
        self.acknowledged()
        for op in ("consent", "unpair", "pair", "", "STATUS", "status ", "data", "close"):
            self.assertIs(self.a.authority.admit(self.endpoint, 1, op), False, repr(op))

    def test_identity_and_type_failures_fail_closed(self):
        self.acknowledged()
        auth = self.a.authority
        for args in (("install:nope", 1, "status"), (self.endpoint, 2, "status"), (self.endpoint, 0, "status"),
                     (self.endpoint, True, "status"), (self.endpoint, "1", "status"), (self.endpoint, None, "status"),
                     (None, 1, "status"), (b"x", 1, "status"), (self.endpoint, 1, None), (self.endpoint, 1, b"status"),
                     (self.endpoint, 1, ["status"])):
            self.assertIs(auth.admit(*args), False, args)

    def test_pending_link_is_not_admitted(self):
        offer = self.a.start_offer()
        accepted = self.b.import_offer(offer["code"])
        self.a.accept_offer(offer["id"], accepted["code"])
        sess = self.a.journal.snapshot()
        self.assertEqual([l["protocol_state"] for l in sess.links.values()], ["pending"])
        ep = next(iter(sess.links))
        for gen in (0, 1, 2):
            self.assertIs(self.a.authority.admit(ep, gen, "pair_ack"), False)

    def test_unpair_cutover_refuses_old_and_new_generation(self):
        self.acknowledged()
        self.assertIs(self.a.authority.admit(self.endpoint, 1, "status"), True)
        self.a.unpair(self.peer_id)
        for gen in (1, 2):
            for op in ACK_OPS:
                self.assertIs(self.a.authority.admit(self.endpoint, gen, op), False, (gen, op))
        # the peer side that has not itself unpaired is unaffected: admission is per local journal.
        self.assertIs(self.b.authority.admit(self.endpoint, 1, "status"), True)

    def test_unpair_while_confirmed_refuses_pair_ack(self):
        self.confirmed()
        self.a.unpair(self.peer_id)
        self.assertIs(self.a.authority.admit(self.endpoint, 1, "pair_ack"), False)
        self.assertIs(self.a.authority.admit(self.endpoint, 2, "pair_ack"), False)

    def test_json_projection_alone_cannot_admit(self):
        self.acknowledged()
        projection = self.A.peers_path.read_bytes()
        self.a.unpair(self.peer_id)
        self.A.peers_path.write_bytes(projection)  # stale projection claims the link is still live
        self.assertIs(self.a.authority.admit(self.endpoint, 1, "status"), False)

    def test_corrupt_journal_fails_closed(self):
        self.acknowledged()
        path = self.A.journal_dir / "pairing.journal"
        data = path.read_bytes()
        path.write_bytes(data[:-5] + b"xxxx\n")
        self.assertIs(self.a.authority.admit(self.endpoint, 1, "status"), False)
        path.write_bytes(b"garbage\n")
        self.assertIs(self.a.authority.admit(self.endpoint, 1, "status"), False)

    def test_unavailable_journal_fails_closed(self):
        self.acknowledged()
        shutil.move(str(self.A.journal_dir), str(self.tmp / "gone"))
        self.assertIs(self.a.authority.admit(self.endpoint, 1, "status"), False)

    def test_busy_journal_refuses_within_the_admit_bound(self):
        self.acknowledged()
        lockfile = self.A.journal_dir / pj.LOCK_FILE
        fd = os.open(lockfile, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)  # a long-running writer holds the journal
            started = time.monotonic()
            self.assertIs(self.a.authority.admit(self.endpoint, 1, "status"), False)
            elapsed = time.monotonic() - started
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        self.assertLess(elapsed, 0.05)  # nonblocking: one flock attempt, no wait loop
        self.assertFalse(hasattr(pj, "ADMIT_LOCK_WAIT"))
        self.assertIs(self.a.authority.admit(self.endpoint, 1, "status"), True)  # recovers once the writer is gone

    def test_unmocked_journal_state_is_not_cached(self):
        self.acknowledged()
        self.assertIs(self.a.authority.admit(self.endpoint, 1, "status"), True)
        self.a.unpair(self.peer_id)
        self.assertIs(self.a.authority.admit(self.endpoint, 1, "status"), False)

    def test_existing_authority_surface_unchanged(self):
        self.acknowledged()
        state = self.a.authority.current(self.endpoint)
        self.assertEqual((state.generation, state.protocol_state), (1, "acknowledged"))
        self.assertTrue(callable(self.a.authority.receipt_admission))


class DispatcherAdmissionTests(AdmissionBase):
    def _race(self):
        """First request holds the only slot; the second waits in the queue; unpair lands; first is released."""
        self.acknowledged()
        started, release = threading.Event(), threading.Event()
        calls, locks_during_io = [], []

        def handler(request):
            calls.append(request["invite_id"])
            if request["invite_id"] == "inv-first":
                started.set()
                release.wait(10)
            return {"ok": request["invite_id"]}

        dispatcher = self.dispatcher({"status": handler}, queue_size=1, max_workers=2, timeout=5.0)

        async def scenario():
            req = lambda i: {"op": "status", "lobby_id": "lobby", "invite_id": i}
            first = asyncio.create_task(dispatcher.dispatch(self.endpoint, 1, req("inv-first")))
            await asyncio.to_thread(started.wait, 5)
            second = asyncio.create_task(dispatcher.dispatch(self.endpoint, 1, req("inv-second")))
            await asyncio.sleep(0.2)  # second passed the pre-queue check and is parked on the slot
            self.assertEqual(calls, ["inv-first"])
            began = time.monotonic()
            # the durable cutover must not wait on a journal lock held across the first handler's I/O
            await asyncio.to_thread(self.a.unpair, self.peer_id)
            locks_during_io.append(time.monotonic() - began)
            release.set()
            results = await asyncio.gather(first, second, return_exceptions=True)
            await dispatcher.close()
            return results

        results = asyncio.run(scenario())
        self.assertLess(locks_during_io[0], 1.0)
        return results, calls

    def test_queued_before_unpair_but_admitted_after_does_not_execute_while_admitted_first_finishes(self):
        results, calls = self._race()
        self.assertEqual(results[0], {"ok": "inv-first"})  # admitted before the cutover: allowed to finish
        self.assertIsInstance(results[1], ControlError)     # first admitted after the cutover: refused
        self.assertEqual(calls, ["inv-first"])               # the stale effect was never invoked

    def test_new_call_after_unpair_is_refused_with_zero_effects(self):
        self.acknowledged()
        calls = []
        dispatcher = self.dispatcher({"status": lambda r: calls.append(r) or {"ok": 1}})
        self.a.unpair(self.peer_id)
        req = {"op": "status", "lobby_id": "lobby", "invite_id": "inv"}
        for gen in (1, 2):
            with self.assertRaises(ControlError):
                asyncio.run(dispatcher.dispatch(self.endpoint, gen, req))
        self.assertEqual(calls, [])

    def test_false_admission_on_every_operation_means_zero_handler_effect(self):
        self.confirmed()  # confirmed: pair_ack only, so every other label is refused by the real journal
        calls = []
        dispatcher = self.dispatcher({"status": lambda r: calls.append(r), "pair_ack": lambda r: calls.append(r) or {}})
        # the dispatcher's own pre-check already refuses non-pair_ack in confirmed; force the admit seam alone:
        self.assertIs(dispatcher.admit(self.endpoint, 1, "status"), False)
        self.assertEqual(calls, [])

    def test_admission_lock_is_released_before_the_handler_runs(self):
        self.acknowledged()
        observed = []

        def handler(request):
            # an exclusive journal lock is obtainable from the handler thread: admit() released its shared lock
            fd = os.open(self.A.journal_dir / pj.LOCK_FILE, os.O_RDWR)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                observed.append("exclusive-lock-free")
            except OSError:
                observed.append("lock-held")
            finally:
                os.close(fd)
            return {"ok": 1}

        dispatcher = self.dispatcher({"status": handler})
        result = asyncio.run(dispatcher.dispatch(self.endpoint, 1, {"op": "status", "lobby_id": "l", "invite_id": "i"}))
        self.assertEqual(result, {"ok": 1})
        self.assertEqual(observed, ["exclusive-lock-free"])

    def test_room_log_result_is_discarded_when_unpair_lands_during_the_handler(self):
        self.acknowledged()
        started, release = threading.Event(), threading.Event()

        def log(request):
            started.set()
            release.wait(10)
            return {"entries": ["canonical-transcript"]}

        dispatcher = self.dispatcher({"room_log": log})

        async def scenario():
            task = asyncio.create_task(dispatcher.dispatch(
                self.endpoint, 1, {"op": "room_log", "room_id": "r", "since_seq": 0, "limit": 10}))
            await asyncio.to_thread(started.wait, 5)
            await asyncio.to_thread(self.a.unpair, self.peer_id)
            release.set()
            return (await asyncio.gather(task, return_exceptions=True))[0]

        self.assertIsInstance(asyncio.run(scenario()), ControlError)


class ConnectorAdmissionTests(AdmissionBase):
    def _admission(self):
        self.acknowledged()
        expected = pj.binding_for(self.a.journal.snapshot().links[self.endpoint])
        binding = LinkBinding(endpoint_id=self.endpoint, generation=1, address=("127.0.0.1", 9),
                              peer_ca_file="unused.pem", peer_fingerprint=expected["fingerprint"],
                              expected_binding=expected)
        return _Admission(binding, self.a.authority)

    def test_admission_with_real_authority_gates_data_effects(self):
        admission = self._admission()
        effects = []
        for label in ("data_send", "data_native"):
            admission.admit(label)
            effects.append(label)
        self.assertEqual(effects, ["data_send", "data_native"])
        self.a.unpair(self.peer_id)
        for label in ("data_send", "data_native"):
            with self.assertRaises(FirewallDenied):
                admission.admit(label)
                effects.append("STALE-" + label)
        self.assertEqual(effects, ["data_send", "data_native"])

    def test_admitted_data_effect_completes_without_a_journal_lock_across_io(self):
        admission = self._admission()
        admission.admit("data_native")  # admitted before the cutover
        began = time.monotonic()
        self.a.unpair(self.peer_id)     # unpair needs the exclusive lock: nothing held across the "I/O"
        self.assertLess(time.monotonic() - began, 1.0)
        with self.assertRaises(FirewallDenied):
            admission.admit("data_native")


if __name__ == "__main__":
    unittest.main()


_HOLDER = (
    "import fcntl, os, sys\n"
    "fd = os.open(sys.argv[1], os.O_RDWR)\n"
    "fcntl.flock(fd, fcntl.LOCK_EX)\n"
    "sys.stdout.write('locked\\n'); sys.stdout.flush()\n"
    "sys.stdin.readline()\n"
)


class ForeignHolder:
    """A separate process holding LOCK_EX on the real journal lock file (same-process flock is not a valid model)."""

    def __init__(self, lockfile):
        import subprocess
        self.proc = subprocess.Popen([sys.executable, "-B", "-c", _HOLDER, str(lockfile)], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, text=True)
        try:
            if self.proc.stdout.readline().strip() != "locked":
                raise RuntimeError("holder did not acquire the journal lock")
        except BaseException:
            self.release()
            raise

    def release(self):
        proc = self.proc
        try:
            if proc.poll() is None:
                try:
                    proc.stdin.write("\n")
                    proc.stdin.flush()
                except OSError:
                    pass
                try:
                    proc.wait(5)
                except Exception:
                    proc.kill()
                    proc.wait(5)
        finally:
            for stream in (proc.stdin, proc.stdout):
                try:
                    stream.close()
                except OSError:
                    pass
        self.returncode = proc.returncode


class NonblockingAdmissionTests(AdmissionBase):
    """B1-01: contention on the journal lock must never park the shared event-loop thread."""
    BOUND = 0.05

    def _holder(self):
        holder = ForeignHolder(self.A.journal_dir / pj.LOCK_FILE)
        self.addCleanup(holder.release)
        return holder

    def test_direct_admit_refuses_immediately_and_loop_timer_is_not_delayed_under_foreign_lock(self):
        self.acknowledged()
        holder = self._holder()
        out = {}

        async def scenario():
            loop = asyncio.get_running_loop()
            start = time.monotonic()
            fired = loop.create_future()
            loop.call_later(0.01, lambda: fired.set_result(time.monotonic() - start))
            began = time.monotonic()
            out["admit"] = self.a.authority.admit(self.endpoint, 1, "status")  # synchronous, on the loop thread
            out["admit_s"] = time.monotonic() - began
            out["timer_s"] = await asyncio.wait_for(fired, 5)

        try:
            asyncio.run(scenario())
        finally:
            holder.release()
        print("direct admit=%r %.1fms timer fired at %.1fms" % (out["admit"], out["admit_s"] * 1e3, out["timer_s"] * 1e3))
        self.assertIs(out["admit"], False)
        self.assertLess(out["admit_s"], self.BOUND)
        self.assertLess(out["timer_s"], self.BOUND)
        self.assertEqual(holder.returncode, 0)
        self.assertIs(self.a.authority.admit(self.endpoint, 1, "status"), True)  # recovers once the holder is reaped

    def test_dispatch_refuses_without_effect_and_loop_timer_runs_under_foreign_lock(self):
        self.acknowledged()
        cached = self.a.authority.current(self.endpoint)

        class CachedCurrent:  # isolates B1-01 from the separate (pre-existing) current() preflight wait
            def current(self, endpoint_id):
                return cached

        calls = []
        dispatcher = ControlDispatcher(CachedCurrent(), handlers={"status": lambda r: calls.append(r) or {"ok": 1}},
                                       admit=self.a.authority.admit, owner_verify=lambda *_: True, timeout=0.05)
        holder = self._holder()
        out = {}

        async def scenario():
            loop = asyncio.get_running_loop()
            start = time.monotonic()
            fired = loop.create_future()
            loop.call_later(0.01, lambda: fired.set_result(time.monotonic() - start))
            began = time.monotonic()
            try:
                await dispatcher.dispatch(self.endpoint, 1, {"op": "status", "lobby_id": "l", "invite_id": "i"})
                out["result"] = "admitted"
            except ControlError:
                out["result"] = "refused"
            out["dispatch_s"] = time.monotonic() - began
            out["timer_s"] = await asyncio.wait_for(fired, 5)
            await dispatcher.close()

        try:
            asyncio.run(scenario())
        finally:
            holder.release()
        print("dispatch %s %.1fms timer fired at %.1fms" % (out["result"], out["dispatch_s"] * 1e3, out["timer_s"] * 1e3))
        self.assertEqual(out["result"], "refused")
        self.assertLess(out["dispatch_s"], self.BOUND)
        self.assertLess(out["timer_s"], self.BOUND)
        self.assertEqual(calls, [])
        self.assertEqual(holder.returncode, 0)

    def test_foreign_shared_holder_does_not_block_admission(self):
        self.acknowledged()
        fd = os.open(self.A.journal_dir / pj.LOCK_FILE, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_SH)
            self.assertIs(self.a.authority.admit(self.endpoint, 1, "status"), True)
        finally:
            os.close(fd)

    def test_admit_attempts_the_lock_exactly_once_nonblocking(self):
        self.acknowledged()
        modes, sleeps = [], []
        real_flock, real_sleep = pj.fcntl.flock, pj.time.sleep

        def spy_flock(fd, mode):
            modes.append(mode)
            return real_flock(fd, mode)

        pj.fcntl.flock = spy_flock
        pj.time.sleep = lambda s: sleeps.append(s)
        try:
            self.assertIs(self.a.authority.admit(self.endpoint, 1, "status"), True)
        finally:
            pj.fcntl.flock, pj.time.sleep = real_flock, real_sleep
        self.assertEqual(modes, [fcntl.LOCK_SH | fcntl.LOCK_NB])
        self.assertEqual(sleeps, [])


class NonblockingCurrentTests(AdmissionBase):
    """B1-02: ``current()`` (the dispatcher preflight) must not park the event loop behind a foreign journal lock."""
    BOUND = 0.05

    def _holder(self):
        holder = ForeignHolder(self.A.journal_dir / pj.LOCK_FILE)
        self.addCleanup(holder.release)
        return holder

    def test_direct_current_is_none_immediately_and_loop_timer_not_delayed_under_foreign_lock(self):
        self.acknowledged()
        expected = self.a.authority.current(self.endpoint)
        self.assertIsNotNone(expected)
        holder = self._holder()
        out = {}

        async def scenario():
            loop = asyncio.get_running_loop()
            start = time.monotonic()
            fired = loop.create_future()
            loop.call_later(0.01, lambda: fired.set_result(time.monotonic() - start))
            began = time.monotonic()
            out["current"] = self.a.authority.current(self.endpoint)  # synchronous, on the loop thread
            out["current_s"] = time.monotonic() - began
            out["timer_s"] = await asyncio.wait_for(fired, 15)

        try:
            asyncio.run(scenario())
        finally:
            holder.release()
        print("direct current=%r %.1fms timer fired at %.1fms" % (out["current"], out["current_s"] * 1e3, out["timer_s"] * 1e3))
        self.assertIsNone(out["current"])
        self.assertLess(out["current_s"], self.BOUND)
        self.assertLess(out["timer_s"], self.BOUND)
        self.assertEqual(holder.returncode, 0)
        self.assertEqual(self.a.authority.current(self.endpoint), expected)  # exact LinkState after release

    def test_dispatch_forbidden_without_effect_and_loop_timer_runs_under_foreign_lock(self):
        self.acknowledged()
        calls = []
        dispatcher = self.dispatcher({"status": lambda r: calls.append(r) or {"ok": 1}}, timeout=0.05)
        request = {"op": "status", "lobby_id": "l", "invite_id": "i"}
        holder = self._holder()
        out = {}

        async def scenario():
            loop = asyncio.get_running_loop()
            start = time.monotonic()
            fired = loop.create_future()
            loop.call_later(0.01, lambda: fired.set_result(time.monotonic() - start))
            began = time.monotonic()
            try:
                await dispatcher.dispatch(self.endpoint, 1, request)
                out["result"] = "admitted"
            except ControlError as exc:
                out["result"] = "refused:%s" % (exc,)
            out["dispatch_s"] = time.monotonic() - began
            out["timer_s"] = await asyncio.wait_for(fired, 15)
            holder.release()
            out["after"] = await dispatcher.dispatch(self.endpoint, 1, request)
            await dispatcher.close()

        try:
            asyncio.run(scenario())
        finally:
            holder.release()
        print("dispatch %s %.1fms timer fired at %.1fms" % (out["result"], out["dispatch_s"] * 1e3, out["timer_s"] * 1e3))
        self.assertTrue(out["result"].startswith("refused"))
        self.assertLess(out["dispatch_s"], self.BOUND)
        self.assertLess(out["timer_s"], self.BOUND)
        self.assertEqual(holder.returncode, 0)
        self.assertEqual(out["after"], {"ok": 1})  # positive control: allowed op reaches its handler after release
        self.assertEqual(len(calls), 1)

    def test_current_attempts_the_lock_exactly_once_nonblocking(self):
        self.acknowledged()
        modes, sleeps = [], []
        real_flock, real_sleep = pj.fcntl.flock, pj.time.sleep

        def spy_flock(fd, mode):
            modes.append(mode)
            return real_flock(fd, mode)

        pj.fcntl.flock = spy_flock
        pj.time.sleep = lambda s: sleeps.append(s)
        try:
            self.assertIsNotNone(self.a.authority.current(self.endpoint))
        finally:
            pj.fcntl.flock, pj.time.sleep = real_flock, real_sleep
        self.assertEqual(modes, [fcntl.LOCK_SH | fcntl.LOCK_NB])
        self.assertEqual(sleeps, [])

    def test_snapshot_default_still_waits_for_the_lock(self):
        self.acknowledged()
        modes = []
        real_flock = pj.fcntl.flock
        pj.fcntl.flock = lambda fd, mode: (modes.append(mode), real_flock(fd, mode))[1]
        try:
            self.a.authority.journal.snapshot()
        finally:
            pj.fcntl.flock = real_flock
        self.assertEqual(modes, [fcntl.LOCK_SH | fcntl.LOCK_NB])  # polled try-lock loop (bounded wait), unchanged

    def test_unpaired_stale_and_corrupt_journals_stay_fail_closed(self):
        self.acknowledged()
        self.assertIsNone(self.a.authority.current("no-such-endpoint"))
        self.assertIsNone(self.a.authority.current(None))
        dispatcher = self.dispatcher({"status": lambda r: {"ok": 1}})

        async def stale():
            with self.assertRaises(ControlError):
                await dispatcher.dispatch(self.endpoint, 2, {"op": "status", "lobby_id": "l", "invite_id": "i"})
            await dispatcher.close()

        asyncio.run(stale())
        (self.A.journal_dir / pj.JOURNAL_FILE).write_bytes(b"corrupt")
        self.assertIsNone(self.a.authority.current(self.endpoint))
