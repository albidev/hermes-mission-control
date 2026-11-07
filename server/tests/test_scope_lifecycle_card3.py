"""Card3-A transactional target ownership: deterministic failure/interleaving witnesses."""
import importlib.util
import socket
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from invite_rooms.control import connector as c


def bare_connector():
    # Exercise actual start/accept/close without TLS/native construction or requests.
    target = c.TargetConnector.__new__(c.TargetConnector)
    target._slots = threading.BoundedSemaphore(2)
    target._lock = threading.Lock()
    target._workers = set()
    target._sockets = set()
    target._listener = target._thread = None
    target._stopping = threading.Event()
    target.timeout = .1
    target.accepted = target.peak_active = 0
    return target


def free_bind():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()


class TargetTransactionTests(unittest.TestCase):
    def failure(self, factory):
        target = bare_connector()
        bind = free_bind()
        primary = RuntimeError('primary-start-failure')
        try:
            with mock.patch.object(c.threading, 'Thread', factory(primary)):
                with self.assertRaises(RuntimeError) as caught:
                    target.start(host=bind[0], port=bind[1])
            self.assertIs(caught.exception, primary)
            # No reliance on eventual GC to release the bound port.
            with socket.socket() as probe:
                probe.bind(bind)
            self.assertIsNone(target._listener)
            self.assertIsNone(target._thread)
            target.close()
        finally:
            # Baseline cleanup, including the never-started join defect.
            if target._thread is not None and target._thread.ident is None:
                target._thread = None
            target.close()

    def test_thread_constructor_failure_after_bind_releases_socket(self):
        self.failure(lambda error: mock.Mock(side_effect=error))

    def test_never_started_thread_is_not_joined_and_primary_survives(self):
        real = threading.Thread
        def factory(error):
            def make(*a, **kw):
                thread = real(*a, **kw)
                thread.start = mock.Mock(side_effect=error)
                thread.join = mock.Mock(side_effect=AssertionError('never-started join'))
                return thread
            return make
        self.failure(factory)

    def test_started_before_raise_is_reaped(self):
        real = threading.Thread
        def factory(error):
            def make(*a, **kw):
                thread = real(*a, **kw)
                start = thread.start
                def broken():
                    start()
                    raise error
                thread.start = broken
                return thread
            return make
        self.failure(factory)

    def test_close_timeout_retains_thread_for_retry(self):
        target = bare_connector()
        release = threading.Event()
        worker = threading.Thread(target=lambda: release.wait(5))
        worker.start()
        target._thread = worker
        join = worker.join
        try:
            with mock.patch.object(worker, 'join', lambda timeout: None):
                with self.assertRaisesRegex(RuntimeError, 'incomplete'):
                    target.close()
            self.assertIs(target._thread, worker)
            release.set()
            join(2)
            target.close()
            self.assertIsNone(target._thread)
        finally:
            release.set()
            join(2)


class TargetCleanupRetryTests(unittest.TestCase):
    def test_constructor_primary_retains_failed_socket_close_until_retry(self):
        target = bare_connector()
        real_socket = socket.socket
        primary = RuntimeError('constructor-primary')
        sockets = []

        class Socket(real_socket):
            def close(self):
                if not getattr(self, 'failed_once', False):
                    self.failed_once = True
                    raise OSError('one-shot-close')
                super().close()

        def make_socket(*args, **kwargs):
            sock = Socket(*args, **kwargs)
            sockets.append(sock)
            return sock

        bind = free_bind()
        try:
            with mock.patch.object(c.socket, 'socket', make_socket), \
                    mock.patch.object(c.threading, 'Thread', side_effect=primary):
                try:
                    target.start(host=bind[0], port=bind[1])
                except RuntimeError as caught:
                    self.assertIs(caught, primary)
                    self.assertIsNotNone(caught.__traceback__)
                else:
                    self.fail('constructor must fail after bind')
            self.assertIs(target._listener, sockets[0])
            with real_socket() as probe:
                with self.assertRaises(OSError):
                    probe.bind(bind)
            target.close()
            with real_socket() as probe:
                probe.bind(bind)
            self.assertIsNone(target._listener)
        finally:
            for sock in sockets:
                real_socket.close(sock)
            target.close()


class ScopeLifecycleTests(unittest.TestCase):
    def api(self):
        self.assertIsNotNone(importlib.util.find_spec('invite_rooms.scope_lifecycle'),
                             'product scope lifecycle missing')
        from invite_rooms.scope_lifecycle import ScopeLifecycle
        return ScopeLifecycle

    def test_inert_and_duplicate_scope_shares_one_attempt(self):
        cls = self.api()
        entered, release = threading.Event(), threading.Event()
        calls, closes, results, errors = [], [], [], []
        manager = None
        def prepare(**kwargs):
            self.assertFalse(manager._lock.locked())
            calls.append(kwargs)
            entered.set()
            self.assertTrue(release.wait(2))
            return self.prepared(closes)
        manager = cls(prepare=prepare, check=lambda prepared, deadline: True)
        self.assertEqual(calls, [])
        manager.start()
        def invoke():
            try:
                results.append(manager.ensure_target(endpoint_id='ep', generation=1, lobby_id='l', invite_id='i', deadline=time.monotonic()+4))
            except BaseException as exc:
                errors.append(exc)
        threads = [threading.Thread(target=invoke) for _ in range(2)]
        try:
            threads[0].start()
            self.assertTrue(entered.wait(1))
            threads[1].start()
            release.set()
            for thread in threads:
                thread.join(3)
                self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(len(calls), 1)
            self.assertIs(results[0], results[1])
        finally:
            release.set()
            for thread in threads:
                if thread.ident is not None:
                    thread.join(3)
            manager.close()
        self.assertEqual(closes, ['close'])

    def prepared(self, closes, *, lobby='l', start=None, close=None):
        from test_run_bindings_card3 import scope, expected
        from invite_rooms.sidecar_data_plane import PreparedTargetConnector
        from invite_rooms.peer_registry import adapter_id_for
        s = scope(lobby_id=lobby)
        connector = SimpleNamespace(start=start or (lambda **kw: None), close=close or (lambda: closes.append('close')))
        return PreparedTargetConnector(connector, ('127.0.0.1', 12345), adapter_id_for(s), s, expected(s))

    def test_close_during_prepare_or_start_cannot_publish_late_resource(self):
        cls = self.api()
        for phase in ('prepare', 'start'):
            with self.subTest(phase=phase):
                entered, release = threading.Event(), threading.Event()
                closes, errors = [], []
                def barrier():
                    entered.set()
                    self.assertTrue(release.wait(3))
                def start(**kw):
                    if phase == 'start': barrier()
                def prepare(**kw):
                    if phase == 'prepare': barrier()
                    return self.prepared(closes, start=start)
                manager = cls(prepare=prepare, check=lambda p, d: True)
                manager.start()
                def invoke():
                    try:
                        manager.ensure_target(endpoint_id='ep', generation=1, lobby_id='l', invite_id='i', deadline=time.monotonic()+4)
                    except BaseException as exc:
                        errors.append(exc)
                thread = threading.Thread(target=invoke)
                thread.start()
                try:
                    self.assertTrue(entered.wait(1))
                    # Close is bounded, marks cutover before returning incomplete.
                    with self.assertRaisesRegex(Exception, 'incomplete'):
                        manager.close(timeout=.02)
                    release.set()
                    thread.join(3)
                    self.assertFalse(thread.is_alive())
                    self.assertEqual(len(errors), 1)
                    manager.close()
                    self.assertEqual(closes, ['close'])
                    self.assertEqual(manager.snapshot(), ())
                finally:
                    release.set()
                    thread.join(3)
                    manager.close()

    def test_siblings_retire_independently_and_failed_cleanup_is_retryable(self):
        cls = self.api()
        closes, attempts = [], []
        def close():
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError('release-failure')
            closes.append('first')
        def prepare(**kw):
            return self.prepared(closes, lobby=kw['lobby_id'], close=close if kw['lobby_id']=='l' else lambda: closes.append('second'))
        manager = cls(prepare=prepare, check=lambda p,d: True)
        manager.start()
        first = manager.ensure_target(endpoint_id='ep', generation=1, lobby_id='l', invite_id='i', deadline=time.monotonic()+3)
        second = manager.ensure_target(endpoint_id='ep', generation=1, lobby_id='sibling', invite_id='i', deadline=time.monotonic()+3)
        self.assertNotEqual(first.adapter_id, second.adapter_id)
        with self.assertRaisesRegex(RuntimeError, 'release-failure'):
            manager.retire(first.scope)
        self.assertEqual(len(manager.snapshot()), 2)
        manager.close()
        self.assertCountEqual(closes, ['first', 'second'])
        self.assertEqual(manager.snapshot(), ())

    def test_conflicting_authority_never_replaces_and_callbacks_hold_no_lock(self):
        from test_run_bindings_card3 import scope, expected
        calls = []
        manager = None

        def unlocked(label):
            self.assertFalse(manager._lock.locked())
            calls.append(label)

        def prepare(**kw):
            unlocked('prepare')
            return self.prepared([], start=lambda **kw: unlocked('start'), close=lambda: unlocked('close'))

        def check(prepared, deadline):
            unlocked('check')
            return True

        manager = self.api()(prepare=prepare, check=check)
        manager.start()
        e = expected(scope())
        args = dict(endpoint_id='ep', generation=1, lobby_id='l', invite_id='i')
        try:
            first = manager.ensure_target(**args, expected_scope=e, deadline=time.monotonic()+3)
            with self.assertRaisesRegex(Exception, 'conflict'):
                manager.ensure_target(**args, expected_scope=dict(e, authority_epoch=2), deadline=time.monotonic()+3)
            self.assertIs(manager.ensure_target(**args, expected_scope=e, deadline=time.monotonic()+3), first)
            self.assertEqual(calls.count('prepare'), 1)
            self.assertEqual(calls.count('start'), 1)
        finally:
            manager.close()
        self.assertEqual(calls.count('close'), 1)

    def test_reconcile_unreadable_metadata_retains_owner_but_confirmed_cutover_retires(self):
        closes = []
        manager = self.api()(prepare=lambda **kw: self.prepared(closes), check=lambda p,d: True)
        manager.start()
        manager.ensure_target(endpoint_id='ep', generation=1, lobby_id='l', invite_id='i', deadline=time.monotonic()+3)
        try:
            with mock.patch.object(manager, '_check', side_effect=OSError('unreadable')):
                with self.assertRaises(OSError):
                    manager.reconcile(deadline=time.monotonic()+3)
            self.assertEqual(len(manager.snapshot()), 1)
            self.assertEqual(closes, [])
            with mock.patch.object(manager, '_check', return_value=False):
                manager.reconcile(deadline=time.monotonic()+3)
            self.assertEqual(manager.snapshot(), ())
            self.assertEqual(closes, ['close'])
        finally:
            manager.close()


if __name__ == '__main__':
    unittest.main()
