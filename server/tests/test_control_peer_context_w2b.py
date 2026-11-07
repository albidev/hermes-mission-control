"""P3b-W2b-H1: journal-authenticated, immutable per-request host context at actual handler execution.

Real scratch pairing journals + identity bundles, real loopback mTLS and real executor workers.  Nothing here installs
an invitation handler (H3) or touches a store (H2): the handlers are test spies that record the context they receive.
"""
from __future__ import annotations

import asyncio
import base64
import dataclasses
import json
import math
import os
import pickle
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pairing_support as ps
import test_control_listener_w2a as w2a
from invite_rooms import identity as identity_module
from invite_rooms import pairing_journal as pj
from invite_rooms.control import tls as tls_module
from invite_rooms.control.dispatcher import AuthenticatedPeerContext, ControlDispatcher, ControlError
from invite_rooms.control.listener import ControlListener
from invite_rooms.pairing_journal import JournalLinkAuthority, PeerTrust, binding_for

PEER_B = w2a.PEER_B
LOBBY, INVITE = w2a.LOBBY, w2a.INVITE
STATUS = {"op": "status", "lobby_id": LOBBY, "invite_id": INVITE}
DENIED = b'{"error":"control denied"}'
INSTALL_FOREIGN = "install:" + "d" * 32
OBS_DIR = os.environ.get("H1_OBS_DIR")


def setUpModule():
    global TEMPLATES, FOREIGN
    TEMPLATES = {i: ps.identity_template(i) for i in (ps.INSTALL_A, ps.INSTALL_B, ps.INSTALL_C)}
    # A genuinely distinct CA/leaf (real key generation, not the pooled keys) that no journal ever pinned.
    root = Path(tempfile.mkdtemp())
    os.chmod(root, 0o700)
    identity_module.initialize_identity(root / "identity", installation_id=INSTALL_FOREIGN, profile=ps.PROFILE)
    FOREIGN = root / "identity"


def tearDownModule():
    for p in TEMPLATES.values():
        shutil.rmtree(p.parent)
    shutil.rmtree(FOREIGN.parent)


def observe(name, value):
    if OBS_DIR:
        with open(os.path.join(OBS_DIR, "observations.jsonl"), "a") as fh:
            fh.write(json.dumps({"name": name, "value": value}, sort_keys=True, default=str) + "\n")


class H1Base(unittest.TestCase):
    """Host A with peer B (confirmed).  ``ack`` promotes a link to ``acknowledged`` exactly as the W2a/W2b0 flow does."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.tmp.chmod(0o700)
        self.addCleanup(shutil.rmtree, self.tmp)
        self.clock = ps.Clock(time.time())
        self.A = ps.Side(self.tmp, "a", ps.INSTALL_A, template=TEMPLATES[ps.INSTALL_A])
        self.B = ps.Side(self.tmp, "b", ps.INSTALL_B, template=TEMPLATES[ps.INSTALL_B])
        self.C = ps.Side(self.tmp, "c", ps.INSTALL_C, template=TEMPLATES[ps.INSTALL_C])
        self.a, self.b, self.c = (s.service(self.clock) for s in (self.A, self.B, self.C))
        self.paired_b = ps.drive_pairing(self.a, self.b)
        self.ba = identity_module.load_identity(self.A.identity_dir, installation_id=ps.INSTALL_A, profile="default")
        self.bb = identity_module.load_identity(self.B.identity_dir, installation_id=ps.INSTALL_B, profile="default")
        self.bc = identity_module.load_identity(self.C.identity_dir, installation_id=ps.INSTALL_C, profile="default")
        self.authority = JournalLinkAuthority(self.a.journal)
        self.listeners = []
        self.addCleanup(self._close_all)

    def _close_all(self):
        for item in self.listeners:
            try:
                item.close()
            except Exception:
                pass

    # -- helpers ----------------------------------------------------------------------------------------
    def ack(self, host, peer, paired):
        req = host.ack_request(paired["a_id"])
        host.complete_ack(paired["a_id"], peer.handle_pair_ack(req.request))
        return req.endpoint_id, req.generation

    def pair_c(self):
        self.paired_c = ps.drive_pairing(self.a, self.c)
        return self.paired_c

    def trust(self, endpoint_id, generation, role="host"):
        return self.authority.peer_trust(endpoint_id, generation, role=role)

    def listen_host(self, handlers, side=None, bundle=None, service=None, **kw):
        side, bundle, service = side or self.A, bundle or self.ba, service or self.a
        item = ControlListener(host="127.0.0.1", port=w2a.free_port(),
                               authority=JournalLinkAuthority(service.journal),
                               identity=lambda: (str(bundle.leaf_cert_file), str(bundle.leaf_key_file)),
                               pair_ack_handler=lambda request: service.handle_pair_ack(request),
                               host_handlers=handlers, timeout=kw.pop("timeout", 3.0), **kw)
        self.listeners.append(item)
        item.start()
        return item

    def send(self, item, *, client, server, request=None, endpoint_id, generation, **kw):
        return w2a.wire(item.address[1], client_bundle=client, server_bundle=server, request=request or STATUS,
                        endpoint_id=endpoint_id, generation=generation, **kw)

    def spy(self):
        calls = []

        def status(context, request):
            calls.append((context, request))
            return {"peer": context.peer_id, "generation": context.generation}
        return calls, status

    def link(self, endpoint_id):
        return self.a.journal.snapshot().links[endpoint_id]


class ContextualHostInvocationTest(H1Base):
    def test_baseline_default_listener_refuses_status_on_acknowledged_host_link(self):
        endpoint, generation = self.ack(self.a, self.b, self.paired_b)
        item = self.listen_host(None)                      # default: no host handlers
        res = self.send(item, client=self.bb, server=self.ba, endpoint_id=endpoint, generation=generation)
        self.assertEqual(res[0:2], ("ok", 403), res)
        self.assertEqual(res[2], DENIED)
        self.assertEqual(item.dispatched, 1)               # real behavioural refusal at the dispatcher, not setup

    def test_acknowledged_host_link_delivers_journal_authenticated_immutable_context(self):
        endpoint, generation = self.ack(self.a, self.b, self.paired_b)
        calls, status = self.spy()
        item = self.listen_host({"status": status})
        res = self.send(item, client=self.bb, server=self.ba, endpoint_id=endpoint, generation=generation)
        self.assertEqual(res[0:2], ("ok", 200), res)
        self.assertEqual(len(calls), 1)
        ctx, request = calls[0]
        link = self.link(endpoint)
        peer = link["peer"]
        self.assertIsInstance(ctx, AuthenticatedPeerContext)
        self.assertEqual((ctx.endpoint_id, ctx.generation, ctx.role, ctx.protocol_state, ctx.peer_id, ctx.operation),
                         (endpoint, generation, "host", "acknowledged", peer["peer_id"], "status"))
        self.assertEqual(dict(ctx.binding), binding_for(link))
        self.assertEqual(len(dict(ctx.binding)), 6)
        self.assertEqual((ctx.fingerprint, ctx.ca_pem), (peer["fingerprint"], peer["ca_pem"]))
        self.assertEqual(ctx.fingerprint, self.bb.leaf_fingerprint)
        self.assertEqual(ctx.consent_public_key, base64.b64decode(peer["consent_public_key_b64"]))
        self.assertEqual(len(ctx.consent_public_key), 32)
        self.assertEqual(request, STATUS)                  # the legacy request shape is unchanged: no context in it
        self.assertEqual(json.loads(res[2]), {"generation": generation, "peer": peer["peer_id"]})
        with self.assertRaises(dataclasses.FrozenInstanceError):
            ctx.peer_id = "peer-" + "f" * 32
        with self.assertRaises(TypeError):
            pickle.dumps(ctx)
        with self.assertRaises(TypeError):
            json.dumps(ctx)
        shown = repr(ctx)
        self.assertNotIn("BEGIN CERTIFICATE", shown)
        self.assertNotIn(peer["consent_public_key_b64"], shown)
        self.assertTrue(ctx.readmit())
        observe("tls_peer_context", {"peer_id": ctx.peer_id, "endpoint_id": ctx.endpoint_id, "generation": ctx.generation,
                                     "role": ctx.role, "state": ctx.protocol_state, "fingerprint": ctx.fingerprint,
                                     "binding_fields": sorted(dict(ctx.binding)), "key_len": len(ctx.consent_public_key)})

    def test_two_acknowledged_links_get_distinct_contexts_under_concurrency_and_forged_bodies_do_not_matter(self):
        eb = self.ack(self.a, self.b, self.paired_b)
        ec = self.ack(self.a, self.c, self.pair_c())
        self.assertNotEqual(eb[0], ec[0])
        barrier = threading.Barrier(2, timeout=5)
        seen, lock = [], threading.Lock()

        def status(context, request):
            barrier.wait()                                  # both handlers are in flight at the same time
            with lock:
                seen.append((context, request, threading.current_thread().name))
            return {"peer": context.peer_id, "endpoint": context.endpoint_id}
        item = self.listen_host({"status": status})
        out = {}

        def run(name, client, link, body):
            out[name] = self.send(item, client=client, server=self.ba, request=body, endpoint_id=link[0], generation=link[1])
        # request selectors deliberately name the OTHER link's lobby/invite: they are selectors, never authority
        t1 = threading.Thread(target=run, args=("b", self.bb, eb, {"op": "status", "lobby_id": "lobby-" + "c" * 32,
                                                                   "invite_id": "invite-" + "c" * 32}))
        t2 = threading.Thread(target=run, args=("c", self.bc, ec, {"op": "status", "lobby_id": "lobby-" + "b" * 32,
                                                                   "invite_id": "invite-" + "b" * 32}))
        for t in (t1, t2):
            t.start()
        for t in (t1, t2):
            t.join(10)
        self.assertEqual((out["b"][0:2], out["c"][0:2]), (("ok", 200), ("ok", 200)), out)
        peer_b = self.link(eb[0])["peer"]["peer_id"]
        peer_c = self.link(ec[0])["peer"]["peer_id"]
        self.assertNotEqual(peer_b, peer_c)
        self.assertEqual(json.loads(out["b"][2]), {"endpoint": eb[0], "peer": peer_b})
        self.assertEqual(json.loads(out["c"][2]), {"endpoint": ec[0], "peer": peer_c})
        self.assertEqual({c.endpoint_id for c, _r, _t in seen}, {eb[0], ec[0]})
        self.assertEqual(len({t for _c, _r, t in seen}), 2)
        # forged identity fields in the body are not accepted at all: generic denial, zero handler invocation
        n = len(seen)
        forged = self.send(item, client=self.bb, server=self.ba, endpoint_id=eb[0], generation=eb[1],
                           request={**STATUS, "peer_id": peer_c, "generation": ec[1], "endpoint_id": ec[0]})
        self.assertEqual((forged[0:2], forged[2]), (("ok", 403), DENIED))
        self.assertEqual(len(seen), n)

    def test_pair_ack_roundtrip_stays_on_the_legacy_one_argument_lane(self):
        """B (joining) with host handlers configured: pair_ack still works over the legacy handler; status is refused."""
        calls, status = self.spy()
        legacy = []

        def handle(request):
            legacy.append(request)
            return self.b.handle_pair_ack(request)
        item = ControlListener(host="127.0.0.1", port=w2a.free_port(), authority=JournalLinkAuthority(self.b.journal),
                               identity=lambda: (str(self.bb.leaf_cert_file), str(self.bb.leaf_key_file)),
                               pair_ack_handler=handle, host_handlers={"status": status}, timeout=3.0)
        self.listeners.append(item)
        item.start()
        req = self.a.ack_request(self.paired_b["a_id"])
        res = self.send(item, client=self.ba, server=self.bb, request=req.request, endpoint_id=req.endpoint_id,
                        generation=req.generation)
        self.assertEqual(res[0:2], ("ok", 200), res)
        self.assertEqual(self.a.complete_ack(self.paired_b["a_id"], json.loads(res[2]))["state"], "acknowledged")
        self.assertEqual(len(legacy), 1)
        # wrong direction: the joining-role link (peer is host A) never reaches the contextual lane
        res = self.send(item, client=self.ba, server=self.bb, endpoint_id=req.endpoint_id, generation=req.generation)
        self.assertEqual((res[0:2], res[2]), (("ok", 403), DENIED))
        self.assertEqual(calls, [])

    def test_confirmed_only_link_refuses_non_pair_ack_with_zero_invocation(self):
        link = next(iter(self.a.journal.snapshot().links.values()))
        generation = self.a.journal.snapshot().link_generation(link)
        calls, status = self.spy()
        item = self.listen_host({"status": status})
        res = self.send(item, client=self.bb, server=self.ba, endpoint_id=link["endpoint_id"], generation=generation)
        self.assertEqual((res[0:2], res[2]), (("ok", 403), DENIED))
        self.assertEqual(calls, [])

    def test_foreign_ca_foreign_pinned_leaf_and_cross_endpoint_envelope_never_invoke(self):
        eb = self.ack(self.a, self.b, self.paired_b)
        ec = self.ack(self.a, self.c, self.pair_c())
        calls, status = self.spy()
        item = self.listen_host({"status": status})
        foreign = identity_module.load_identity(FOREIGN, installation_id=INSTALL_FOREIGN, profile="default")
        res = self.send(item, client=foreign, server=self.ba, endpoint_id=eb[0], generation=eb[1])
        self.assertEqual(res[0], "err", res)
        # B's real leaf but C's endpoint/generation in the envelope
        res = self.send(item, client=self.bb, server=self.ba, endpoint_id=ec[0], generation=ec[1])
        self.assertEqual(res[0], "err", res)
        # right endpoint, stale generation
        res = self.send(item, client=self.bb, server=self.ba, endpoint_id=eb[0], generation=eb[1] + 1)
        self.assertEqual(res[0], "err", res)
        time.sleep(.1)
        self.assertEqual(calls, [])

    def test_malformed_or_missing_consent_key_never_invokes(self):
        endpoint, generation = self.ack(self.a, self.b, self.paired_b)
        good = base64.b64encode(bytes(range(32))).decode()
        original = pj._verify
        for label, value in (("missing", None), ("short", base64.b64encode(b"x" * 31).decode()),
                             ("not-base64", "***"), ("not-a-point-type", 7)):
            calls, status = self.spy()
            item = self.listen_host({"status": status})

            def tampered(raw, value=value):
                state = original(raw)
                peer = state.links[endpoint]["peer"] if endpoint in state.links else None
                if peer is not None:
                    if value is None:
                        peer.pop("consent_public_key_b64", None)
                    else:
                        peer["consent_public_key_b64"] = value
                return state
            with patch.object(pj, "_verify", tampered):
                res = self.send(item, client=self.bb, server=self.ba, endpoint_id=endpoint, generation=generation)
            self.assertEqual((label, res[0:2], res[2]), (label, ("ok", 403), DENIED))
            self.assertEqual((label, calls), (label, []))
        self.assertTrue(good)

    def test_unpair_between_first_frame_check_and_dispatch_never_invokes(self):
        endpoint, generation = self.ack(self.a, self.b, self.paired_b)
        calls, status = self.spy()
        test = self

        class UnpairOnSecondCheck(ControlListener):
            n = 0

            def _still_current(inner, trust):
                inner.n += 1
                if inner.n == 2:
                    test.a.unpair(PEER_B)
                return super()._still_current(trust)
        item = UnpairOnSecondCheck(host="127.0.0.1", port=w2a.free_port(), authority=self.authority,
                                   identity=lambda: (str(self.ba.leaf_cert_file), str(self.ba.leaf_key_file)),
                                   pair_ack_handler=self.a.handle_pair_ack, host_handlers={"status": status}, timeout=3.0)
        self.listeners.append(item)
        item.start()
        res = self.send(item, client=self.bb, server=self.ba, endpoint_id=endpoint, generation=generation)
        self.assertEqual(res[0], "err", res)
        self.assertEqual(calls, [])

    def test_busy_journal_at_listener_level_refuses_without_invocation(self):
        endpoint, generation = self.ack(self.a, self.b, self.paired_b)
        calls, status = self.spy()
        item = self.listen_host({"status": status})
        with LockHolder(self.A.journal_dir) as holder:
            res = self.send(item, client=self.bb, server=self.ba, endpoint_id=endpoint, generation=generation)
            self.assertEqual(res[0], "err", res)
        self.assertEqual(calls, [])
        self.assertEqual(holder.returncode, 0)
        # contention gone: the very same request is served
        res = self.send(item, client=self.bb, server=self.ba, endpoint_id=endpoint, generation=generation)
        self.assertEqual(res[0:2], ("ok", 200), res)

    def test_corrupt_journal_refuses_without_invocation(self):
        endpoint, generation = self.ack(self.a, self.b, self.paired_b)
        calls, status = self.spy()
        item = self.listen_host({"status": status})
        path = self.A.journal_dir / "pairing.journal"
        path.write_bytes(path.read_bytes() + b"garbage\n")
        res = self.send(item, client=self.bb, server=self.ba, endpoint_id=endpoint, generation=generation)
        self.assertEqual(res[0], "err", res)
        self.assertEqual(calls, [])


class LockHolder:
    """A SEPARATE process holding an exclusive flock on the journal lock file; always reaped."""

    def __init__(self, journal_dir):
        self.path = str(Path(journal_dir) / ".pairing.lock")
        self.proc = None
        self.returncode = None

    def __enter__(self):
        code = ("import fcntl,os,sys\nfd=os.open(sys.argv[1],os.O_RDWR)\nfcntl.flock(fd,fcntl.LOCK_EX)\n"
                "sys.stdout.write('ready\\n');sys.stdout.flush()\nsys.stdin.readline()\n")
        self.proc = subprocess.Popen([sys.executable, "-c", code, self.path], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, text=True)
        if self.proc.stdout.readline().strip() != "ready":
            self.__exit__(None, None, None)
            raise AssertionError("lock holder did not start")
        return self

    def __exit__(self, *exc):
        proc = self.proc
        try:
            if proc.stdin and not proc.stdin.closed:
                try:
                    proc.stdin.write("\n")
                    proc.stdin.flush()
                except OSError:
                    pass
                proc.stdin.close()
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(5)
        finally:
            if proc.stdout:
                proc.stdout.close()
            self.returncode = proc.returncode
        return False


class DispatcherContextTest(H1Base):
    """Direct dispatcher tests with the real journal authority: worker-side admission, queue barrier, event loop."""

    def setUp(self):
        super().setUp()
        self.endpoint, self.generation = self.ack(self.a, self.b, self.paired_b)
        self.expected = self.trust(self.endpoint, self.generation)
        self.assertEqual(self.expected.protocol_state, "acknowledged")
        self.dispatchers = []
        self.addCleanup(lambda: [d.drain(5) for d in self.dispatchers])

    def dispatcher(self, handlers=None, contextual=None, direction="B_to_A", **kw):
        d = ControlDispatcher(self.authority, handlers=handlers or {}, contextual_handlers=contextual,
                              admit=self.authority.admit, direction=direction, timeout=kw.pop("timeout", 5.0), **kw)
        self.dispatchers.append(d)
        return d

    def run_dispatch(self, d, request=STATUS, expected="default", endpoint=None, generation=None):
        expected = self.expected if expected == "default" else expected
        return asyncio.run(d.dispatch(endpoint or self.endpoint, self.generation if generation is None else generation,
                                      request, expected_trust=expected))

    def test_contextual_lane_is_only_allowed_on_b_to_a_and_must_not_shadow_legacy(self):
        calls, status = self.spy()
        with self.assertRaises(ValueError):
            ControlDispatcher(self.authority, handlers={}, contextual_handlers={"status": status}, direction="A_to_B")
        with self.assertRaises(ValueError):
            ControlDispatcher(self.authority, handlers={"status": lambda r: {}}, contextual_handlers={"status": status},
                              direction="B_to_A")
        with self.assertRaises(ValueError):
            ControlDispatcher(self.authority, handlers={}, contextual_handlers={"pair_ack": status}, direction="B_to_A")
        with self.assertRaises(ValueError):
            ControlDispatcher(self.authority, handlers={}, contextual_handlers={"room_log": status}, direction="B_to_A")
        with self.assertRaises(ValueError):
            ControlDispatcher(object(), handlers={}, contextual_handlers={"status": status}, direction="B_to_A")
        self.assertEqual(calls, [])

    def test_missing_or_mismatched_expected_trust_never_invokes_and_never_falls_back(self):
        calls, status = self.spy()
        legacy = []
        d = self.dispatcher(contextual={"status": status})
        with self.assertRaises(ControlError):
            self.run_dispatch(d, expected=None)
        variants = {
            "fingerprint": dataclasses.replace(self.expected, fingerprint="0" * 64),
            "generation": dataclasses.replace(self.expected, generation=self.generation + 1),
            "peer_id": dataclasses.replace(self.expected, peer_id="peer-" + "f" * 32),
            "role": dataclasses.replace(self.expected, role="joining"),
            "state": dataclasses.replace(self.expected, protocol_state="confirmed"),
            "ca": dataclasses.replace(self.expected, ca_pem="x"),
            "binding": dataclasses.replace(self.expected, binding=tuple(sorted({**dict(self.expected.binding),
                                                                                  "target_profile": "other"}.items()))),
            "endpoint": dataclasses.replace(self.expected, endpoint_id="ep-other"),
            "type": "not-a-trust",
        }
        for label, bad in variants.items():
            with self.assertRaises(ControlError, msg=label):
                self.run_dispatch(d, expected=bad)
            self.assertIsNone(self.authority.authenticate_peer(bad, "status"), label)
        self.assertEqual((calls, legacy), ([], []))
        # a legacy handler registered for the same op is never used as a fallback: construction forbids it
        # and an op without a contextual handler still has none.
        d2 = self.dispatcher(handlers={}, contextual={"status": status})
        with self.assertRaises(ControlError):
            self.run_dispatch(d2, request={"op": "preview", "lobby_id": LOBBY, "invite_id": INVITE})

    def test_op_allowlist_and_wrong_direction_state_are_refused_by_the_journal(self):
        for op in ("pair_ack", "material_request", "material_receipt", "room_log", "member_left", "bogus", "consent", 7):
            self.assertIsNone(self.authority.authenticate_peer(self.expected, op), op)
        for op in ("preview", "redeem", "joining_consent", "status"):
            ctx = self.authority.authenticate_peer(self.expected, op)
            self.assertIsInstance(ctx, AuthenticatedPeerContext, op)
            self.assertEqual(ctx.operation, op)
        # confirmed-only host link: a second pairing that is NOT acknowledged
        paired = self.pair_c()
        req = self.a.ack_request(paired["a_id"])
        confirmed = self.authority.peer_trust(req.endpoint_id, req.generation, role="host")
        self.assertEqual(confirmed.protocol_state, "confirmed")
        self.assertIsNone(self.authority.authenticate_peer(confirmed, "status"))

    def test_context_creation_runs_on_the_executor_worker_not_on_the_event_loop_thread(self):
        threads = []
        real = self.authority.authenticate_peer

        def spy_auth(*args, **kw):
            threads.append(threading.current_thread().name)
            return real(*args, **kw)
        calls, status = self.spy()
        d = self.dispatcher(contextual={"status": status})
        with patch.object(self.authority, "authenticate_peer", spy_auth):
            self.run_dispatch(d)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(threads), 1)
        self.assertTrue(threads[0].startswith("invite-control"), threads)

    def test_coroutine_handler_is_readmitted_immediately_before_invocation(self):
        seen = []

        async def status(context, request):
            seen.append(context)
            return {"ok": True}
        d = self.dispatcher(contextual={"status": status})
        self.assertEqual(self.run_dispatch(d), {"ok": True})
        self.assertEqual(seen[0].endpoint_id, self.endpoint)
        # unpaired: even the coroutine lane refuses with zero invocation
        self.a.unpair(PEER_B)
        with self.assertRaises(ControlError):
            self.run_dispatch(d)
        self.assertEqual(len(seen), 1)

    def test_queue_barrier_unpair_and_repair_before_the_queued_task_begins_refuses_with_zero_effect(self):
        calls, status = self.spy()
        d = self.dispatcher(contextual={"status": status}, max_workers=1)
        release = threading.Event()
        blocker = d._executor.submit(release.wait, 10)          # the only worker is occupied
        self.addCleanup(release.set)
        outcome = []

        async def main():
            task = asyncio.ensure_future(d.dispatch(self.endpoint, self.generation, STATUS, expected_trust=self.expected))
            for _ in range(200):                                # until the operation is queued behind the blocker
                await asyncio.sleep(0.01)
                if d._executor._work_queue.qsize() >= 1:
                    break
            self.assertGreaterEqual(d._executor._work_queue.qsize(), 1)
            # the loop-side preflight already passed (the task is past admission): now unpair + re-pair
            self.b.unpair(w2a.PEER_A)
            self.a.unpair(PEER_B)
            self.clock.advance(7 * 24 * 3600)
            self.paired_b = ps.drive_pairing(self.a, self.b)
            self.ack(self.a, self.b, self.paired_b)
            release.set()
            blocker.result(5)
            try:
                outcome.append(("ok", await task))
            except ControlError as exc:
                outcome.append(("denied", exc.code))
        asyncio.run(main())
        self.assertEqual(outcome[0][0], "denied", outcome)
        self.assertEqual(calls, [])

    def test_handler_admitted_just_before_unpair_may_finish_but_readmit_refuses_afterwards(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        seen = []

        def status(context, request):
            seen.append(context)
            entered.set()
            release.wait(10)
            return {"finished": True, "readmit_after_unpair": context.readmit()}
        d = self.dispatcher(contextual={"status": status})

        async def main():
            task = asyncio.ensure_future(d.dispatch(self.endpoint, self.generation, STATUS, expected_trust=self.expected))
            for _ in range(200):
                await asyncio.sleep(0.01)
                if entered.is_set():
                    break
            self.assertTrue(entered.is_set())
            self.assertTrue(seen[0].readmit())
            self.a.unpair(PEER_B)                               # not cancellation: the admitted effect continues
            release.set()
            return await task
        result = asyncio.run(main())
        self.assertEqual(result, {"finished": True, "readmit_after_unpair": False})

    def test_readmit_is_bound_to_the_exact_link_and_an_absolute_deadline_and_is_nonblocking(self):
        ctx = self.authority.authenticate_peer(self.expected, "status", deadline=time.monotonic() + 0.3)
        self.assertTrue(ctx.readmit())
        self.assertIsInstance(ctx.deadline, float)
        time.sleep(0.35)
        self.assertFalse(ctx.readmit())                          # past the absolute deadline
        self.assertIsNone(self.authority.authenticate_peer(self.expected, "status", deadline=time.monotonic() - 1))
        ctx = self.authority.authenticate_peer(self.expected, "status", deadline=time.monotonic() + 30)
        with LockHolder(self.A.journal_dir) as holder:
            started = time.monotonic()
            self.assertFalse(ctx.readmit())                      # busy => one nonblocking refusal
            self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(holder.returncode, 0)
        self.assertTrue(ctx.readmit())
        self.assertIsNone(self.authority.authenticate_peer(self.expected, "status", deadline="soon"))

    def test_busy_journal_refuses_one_nonblocking_and_the_event_loop_stays_responsive(self):
        calls, status = self.spy()
        d = self.dispatcher(contextual={"status": status})
        ticks, errors = [], []

        async def main():
            async def timer():
                while True:
                    ticks.append(time.monotonic())
                    await asyncio.sleep(0.01)
            t = asyncio.ensure_future(timer())
            await asyncio.sleep(0.05)
            started = time.monotonic()
            try:
                await d.dispatch(self.endpoint, self.generation, STATUS, expected_trust=self.expected)
            except ControlError as exc:
                errors.append(exc.code)
            elapsed = time.monotonic() - started
            await asyncio.sleep(0.05)
            t.cancel()
            return elapsed
        with LockHolder(self.A.journal_dir) as holder:
            elapsed = asyncio.run(main())
        self.assertEqual(holder.returncode, 0)
        self.assertEqual(len(errors), 1)
        self.assertLess(elapsed, 0.5)
        gaps = [b - a for a, b in zip(ticks, ticks[1:])]
        self.assertLess(max(gaps), 0.25, max(gaps))
        self.assertEqual(calls, [])
        observe("event_loop_timer_busy_journal", {"max_gap_s": max(gaps), "ticks": len(ticks), "refusals": errors})

    def test_busy_journal_when_the_queued_task_begins_refuses_with_zero_effect(self):
        calls, status = self.spy()
        d = self.dispatcher(contextual={"status": status}, max_workers=1)
        release = threading.Event()
        blocker = d._executor.submit(release.wait, 10)
        self.addCleanup(release.set)
        outcome = []

        async def main(holder):
            task = asyncio.ensure_future(d.dispatch(self.endpoint, self.generation, STATUS, expected_trust=self.expected))
            for _ in range(200):
                await asyncio.sleep(0.01)
                if d._executor._work_queue.qsize() >= 1:
                    break
            holder.__enter__()                                  # a foreign process now holds the journal lock
            release.set()
            blocker.result(5)
            try:
                outcome.append(await task)
            except ControlError as exc:
                outcome.append(exc.code)
        holder = LockHolder(self.A.journal_dir)
        try:
            asyncio.run(main(holder))
        finally:
            holder.__exit__(None, None, None)
        self.assertEqual(len(outcome), 1)
        self.assertIsInstance(outcome[0], str)
        self.assertEqual(calls, [])

    def test_legacy_one_argument_handlers_remain_compatible(self):
        legacy = []

        def pair_ack(request):
            legacy.append(request)
            return {"legacy": True}
        d = ControlDispatcher(self.authority, handlers={"pair_ack": pair_ack}, admit=self.authority.admit,
                              direction="A_to_B", timeout=5.0)
        self.dispatchers.append(d)
        req = self.a.ack_request(self.paired_b["a_id"])
        self.assertEqual(asyncio.run(d.dispatch(req.endpoint_id, req.generation, req.request)), {"legacy": True})
        self.assertEqual(legacy, [req.request])

    def test_admit_bool_api_is_unchanged_and_returns_literal_booleans(self):
        self.assertIs(self.authority.admit(self.endpoint, self.generation, "status"), True)
        self.assertIs(self.authority.admit(self.endpoint, self.generation + 1, "status"), False)
        self.assertIs(self.authority.admit(self.endpoint, self.generation, "bogus"), False)


def _public_fields(ctx):
    """Every publicly constructible field of a context (never the private capability)."""
    return {f.name: getattr(ctx, f.name) for f in dataclasses.fields(ctx) if f.init and not f.name.startswith("_")}


class R01Test(H1Base):
    """P3b-W2b-H1-R01: re-admission at the scheduled coroutine body, ONE absolute deadline, issuer-bound contexts."""

    def setUp(self):
        super().setUp()
        self.endpoint, self.generation = self.ack(self.a, self.b, self.paired_b)
        self.expected = self.trust(self.endpoint, self.generation)
        self.dispatchers = []
        self.addCleanup(lambda: [d.drain(5) for d in self.dispatchers])

    def dispatcher(self, contextual, **kw):
        d = ControlDispatcher(self.authority, handlers={}, contextual_handlers=contextual,
                              admit=self.authority.admit, direction="B_to_A", timeout=kw.pop("timeout", 5.0), **kw)
        self.dispatchers.append(d)
        return d

    def cutover_authority(self):
        """An authority whose ``admit`` (called synchronously by the dispatcher right before it schedules the handler)
        durably unpairs the peer via ``loop.call_soon``: the cutover lands strictly between the dispatcher's last
        synchronous decision and the first scheduled step of the handler."""
        test = self

        class Cutover(JournalLinkAuthority):
            fired = 0

            def admit(inner, endpoint_id, generation, operation):
                ok = super().admit(endpoint_id, generation, operation)
                if ok and operation == "status" and inner.fired == 0:
                    inner.fired += 1
                    asyncio.get_running_loop().call_soon(test.a.unpair, PEER_B)
                return ok
        return Cutover(self.a.journal)

    # -- R1: admission at the actual scheduled coroutine body --------------------------------------------------
    def test_r1_direct_async_cutover_before_the_scheduled_body_refuses_with_zero_invocation(self):
        calls = []

        async def status(context, request):
            calls.append(context)
            return {"ok": True}
        authority = self.cutover_authority()
        d = ControlDispatcher(authority, handlers={}, contextual_handlers={"status": status}, admit=authority.admit,
                              direction="B_to_A", timeout=5.0)
        self.dispatchers.append(d)
        outcome = []

        async def main():
            try:
                outcome.append(("ok", await d.dispatch(self.endpoint, self.generation, STATUS, expected_trust=self.expected)))
            except ControlError as exc:
                outcome.append(("denied", exc.code))
        asyncio.run(main())
        self.assertEqual(authority.fired, 1)
        self.assertEqual(outcome[0][0], "denied", outcome)
        self.assertEqual(calls, [])

    def test_r1_real_mtls_async_cutover_before_the_scheduled_body_is_refused_with_zero_invocation(self):
        calls = []

        async def status(context, request):
            calls.append(context)
            return {"ok": True}
        authority = self.cutover_authority()
        item = ControlListener(host="127.0.0.1", port=w2a.free_port(), authority=authority,
                               identity=lambda: (str(self.ba.leaf_cert_file), str(self.ba.leaf_key_file)),
                               pair_ack_handler=self.a.handle_pair_ack, host_handlers={"status": status}, timeout=3.0)
        self.listeners.append(item)
        item.start()
        res = self.send(item, client=self.bb, server=self.ba, endpoint_id=self.endpoint, generation=self.generation)
        self.assertEqual(authority.fired, 1)
        self.assertEqual((res[0:2], res[2]), (("ok", 403), DENIED))
        self.assertEqual(calls, [])

    def test_r1_control_coroutine_already_executing_may_finish_after_cutover(self):
        seen = []

        async def status(context, request):
            self.a.unpair(PEER_B)                                # cutover AFTER the body began: not cancellation
            await asyncio.sleep(0)
            seen.append(context.readmit())
            return {"finished": True}
        d = self.dispatcher({"status": status})
        result = asyncio.run(d.dispatch(self.endpoint, self.generation, STATUS, expected_trust=self.expected))
        self.assertEqual((result, seen), ({"finished": True}, [False]))

    # -- R2: one absolute deadline, never reset by a wait --------------------------------------------------------
    def test_r2_direct_semaphore_plus_executor_queue_wait_never_starts_the_handler_after_the_budget(self):
        calls, elapsed = [], []

        def status(context, request):
            calls.append((time.monotonic(), context.deadline, context.readmit()))
            return {"ok": True}
        d = self.dispatcher({"status": status}, max_workers=1, queue_size=1, timeout=0.4)
        outcome = []

        async def main():
            loop = asyncio.get_running_loop()
            await d._slots.acquire()
            loop.call_later(0.25, d._slots.release)           # semaphore wait ~0.25 s counts against the budget
            t0 = time.monotonic()
            blocker = d._executor.submit(time.sleep, 0.5)      # the only worker is busy until ~0.5 s after entry
            try:
                outcome.append(("ok", await d.dispatch(self.endpoint, self.generation, STATUS, expected_trust=self.expected)))
            except ControlError as exc:
                outcome.append(("denied", exc.code))
            elapsed.append(time.monotonic() - t0)
            await loop.run_in_executor(None, blocker.result, 5)
            await asyncio.sleep(0.2)                            # a bugged late start would happen here
        asyncio.run(main())
        self.assertEqual(outcome[0][0], "denied", (outcome, calls))
        self.assertLess(elapsed[0], 0.5)
        self.assertEqual(calls, [])

    def test_r2_context_deadline_is_the_dispatch_entry_budget_not_a_reset_after_waits(self):
        calls = []

        def status(context, request):
            calls.append(context)
            return {"ok": True}
        d = self.dispatcher({"status": status}, queue_size=1, timeout=2.0)

        async def main():
            loop = asyncio.get_running_loop()
            await d._slots.acquire()
            loop.call_later(0.3, d._slots.release)
            entry = time.monotonic()
            await d.dispatch(self.endpoint, self.generation, STATUS, expected_trust=self.expected)
            return entry
        entry = asyncio.run(main())
        self.assertEqual(len(calls), 1)
        self.assertTrue(math.isfinite(calls[0].deadline))
        self.assertLessEqual(calls[0].deadline, entry + 2.0 + 0.05)

    def test_r2_real_listener_context_deadline_is_no_later_than_the_listener_request_deadline(self):
        calls, deadlines = [], []

        def status(context, request):
            calls.append(context)
            return {"ok": True}

        class Delayed(ControlListener):
            def _context(inner, trusts):
                time.sleep(0.3)                                  # controlled TLS-context setup delay
                return super()._context(trusts)

            def _dispatch(inner, trust, body, deadline):
                deadlines.append(deadline)
                return super()._dispatch(trust, body, deadline)
        item = Delayed(host="127.0.0.1", port=w2a.free_port(), authority=self.authority,
                       identity=lambda: (str(self.ba.leaf_cert_file), str(self.ba.leaf_key_file)),
                       pair_ack_handler=self.a.handle_pair_ack, host_handlers={"status": status}, timeout=3.0)
        self.listeners.append(item)
        item.start()
        res = self.send(item, client=self.bb, server=self.ba, endpoint_id=self.endpoint, generation=self.generation)
        self.assertEqual(res[0:2], ("ok", 200), res)
        self.assertEqual((len(calls), len(deadlines)), (1, 1))
        self.assertLessEqual(calls[0].deadline, deadlines[0])

    def test_r2_real_listener_queued_work_never_first_runs_after_the_request_expired(self):
        calls = []
        release = threading.Event()

        def status(context, request):
            calls.append((time.monotonic(), context.readmit()))
            return {"ok": True}

        class Delayed(ControlListener):
            def _context(inner, trusts):
                time.sleep(0.3)
                return super()._context(trusts)
        item = Delayed(host="127.0.0.1", port=w2a.free_port(), authority=self.authority,
                       identity=lambda: (str(self.ba.leaf_cert_file), str(self.ba.leaf_key_file)),
                       pair_ack_handler=self.a.handle_pair_ack, host_handlers={"status": status}, timeout=0.8)
        self.listeners.append(item)
        self.addCleanup(release.set)
        item.start()
        executor = item._dispatchers["B_to_A"]._executor
        blockers = [executor.submit(release.wait, 10) for _ in range(4)]    # every worker occupied
        res = self.send(item, client=self.bb, server=self.ba, endpoint_id=self.endpoint, generation=self.generation)
        self.assertNotEqual(res[0:2], ("ok", 200), res)
        release.set()
        for b in blockers:
            b.result(5)
        time.sleep(0.3)                                          # a late first execution would land here
        self.assertEqual(calls, [])

    def test_r2_authenticate_peer_requires_a_finite_numeric_future_deadline(self):
        now = time.monotonic()
        for label, bad in (("inf", float("inf")), ("-inf", float("-inf")), ("nan", float("nan")), ("bool", True),
                           ("past", now - 1), ("str", "soon"), ("object", object()), ("zero", 0)):
            self.assertIsNone(self.authority.authenticate_peer(self.expected, "status", deadline=bad), label)
        self.assertIsInstance(self.authority.authenticate_peer(self.expected, "status", deadline=now + 5),
                              AuthenticatedPeerContext)

    # -- R3: contexts are issuer-bound capabilities, not caller-supplied values ----------------------------------
    def real_context(self, expected=None, op="status", deadline=None):
        return self.authority.authenticate_peer(expected or self.expected, op, deadline=deadline or time.monotonic() + 30)

    def test_r3_public_constructor_never_accepts_a_callback_and_a_bare_value_never_readmits(self):
        ctx = self.real_context()
        fields = _public_fields(ctx)
        with self.assertRaises(TypeError):
            AuthenticatedPeerContext(**{**fields, "_readmit": lambda _: True})
        with self.assertRaises(TypeError):
            AuthenticatedPeerContext(**{**fields, "peer_id": "fabricated", "_readmit": lambda _: True})
        bare = AuthenticatedPeerContext(**fields)                # fields alone carry no authority
        self.assertFalse(bare.readmit())
        self.assertTrue(ctx.readmit())                           # control: the authority-issued one is live

    def test_r3_dataclasses_replace_never_keeps_or_retargets_authority(self):
        ctx = self.real_context()
        self.assertTrue(ctx.readmit())
        for label, change in (("operation", {"operation": "redeem"}),
                              ("deadline", {"deadline": ctx.deadline + 1000.0}),
                              ("peer", {"peer_id": "peer-" + "f" * 32}),
                              ("generation", {"generation": ctx.generation + 1}),
                              ("noop", {})):
            try:
                other = dataclasses.replace(ctx, **change)
            except (TypeError, ValueError):
                continue
            self.assertFalse(other.readmit(), label)
        self.assertTrue(ctx.readmit())

    def test_r3_context_cannot_be_retargeted_to_a_second_real_peer(self):
        ec = self.ack(self.a, self.c, self.pair_c())
        ctx_b = self.real_context()
        ctx_c = self.real_context(self.trust(*ec))
        self.assertNotEqual(ctx_b.endpoint_id, ctx_c.endpoint_id)
        self.assertTrue(ctx_c.readmit())
        for label, extra in (("same-deadline", {}), ("extended-deadline", {"deadline": ctx_b.deadline + 500.0})):
            try:
                hybrid = dataclasses.replace(ctx_b, **{**_public_fields(ctx_c), **extra})
            except (TypeError, ValueError):
                continue
            self.assertFalse(hybrid.readmit(), label)
        # contexts are independent: unpairing C refuses C's context and leaves B's live
        self.a.unpair(self.link(ec[0])["peer"]["peer_id"])
        self.assertFalse(ctx_c.readmit())
        self.assertTrue(ctx_b.readmit())

    def test_r3_valid_context_readmits_while_live_and_refuses_after_unpair_and_expiry(self):
        ctx = self.real_context(deadline=time.monotonic() + 0.4)
        self.assertTrue(ctx.readmit())
        time.sleep(0.45)
        self.assertFalse(ctx.readmit())
        live = self.real_context()
        self.assertTrue(live.readmit())
        self.a.unpair(PEER_B)
        self.assertFalse(live.readmit())
        self.assertNotIn("BEGIN CERTIFICATE", repr(live))


if __name__ == "__main__":
    unittest.main()
