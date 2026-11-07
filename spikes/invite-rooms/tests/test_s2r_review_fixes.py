"""S2.R bounded regressions: real socket ownership and real HTTP auth rejection."""
import asyncio
import base64
from contextlib import redirect_stderr
import gc
from http.client import HTTPConnection
from io import StringIO
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from urllib.parse import urlsplit
import warnings

HERE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(HERE), str(HERE.parents[1] / 'server')]
from connector import RelayClient, _Stream
from lobby_owner import OwnerSurface


def frame(kind, payload=b'first'):
    return dict(type=kind, source='host', target='target', room_id='room',
                link_id='link', stream_id='reused',
                payload=base64.b64encode(payload).decode('ascii'))


class StreamOwnershipRegression(unittest.IsolatedAsyncioTestCase):
    async def test_started_cancelled_pump_preserves_replacement_data_close_and_teardown(self):
        release = threading.Event()
        entered = threading.Event()
        callback_timeouts = []

        def serve(sock, sid):
            entered.set()
            if not release.wait(5):
                callback_timeouts.append(True)

        client = RelayClient(relay_url='ws://127.0.0.1:1/data', identity='target',
                             peer_identity='host', room_id='room', link_id='link',
                             credential='regression-only', on_new_stream=serve)
        sent = []

        async def send(kind, sid, raw=None):
            # Instrument only the outer sends; real pumps/socketpairs/dispatch.
            sent.append((kind, sid, raw))

        client._send = send
        old = _Stream(client, 'reused')
        client._streams['reused'] = old
        old.start()
        fresh = None
        threads = ()
        try:
            await asyncio.sleep(0)  # old pump is actually suspended in sock_recv
            self.assertFalse(old.pump.done())
            old.app_end.close()
            await client._dispatch(frame('DATA'))  # BrokenPipe queues cancellation
            self.assertTrue(old.pump.cancelling())
            self.assertFalse(old.pump.done())
            await client._dispatch(frame('DATA'))  # buffered DATA before cancel runs
            fresh = client._streams['reused']
            threads = client.application_threads
            self.assertIsNot(old, fresh)
            self.assertTrue(entered.wait(1))
            self.assertEqual(len(threads), 1)
            fresh.app_end.settimeout(1)
            self.assertEqual(fresh.app_end.recv(5), b'first')
            await asyncio.sleep(0)  # old finalizer must not remove fresh mapping
            self.assertTrue(old.pump.done())
            self.assertIs(client._streams.get('reused'), fresh)
            self.assertFalse(fresh.pump.done())
            await client._dispatch(frame('DATA', b'second'))
            self.assertIs(client._streams.get('reused'), fresh)
            self.assertEqual(fresh.app_end.recv(6), b'second')
            self.assertEqual(client.application_threads, threads)  # no third callback
            await client._dispatch(frame('CLOSE'))
            self.assertTrue(fresh.remote_closed)
            self.assertEqual(fresh.app_end.recv(1), b'')
            release.set()
            for thread in threads:
                thread.join(2)
                self.assertFalse(thread.is_alive())
            await asyncio.wait_for(asyncio.shield(fresh.pump), 2)
            self.assertEqual(client._streams, {})
            self.assertEqual(client.application_threads, ())
            self.assertEqual(old.relay_end.fileno(), -1)
            self.assertEqual(fresh.relay_end.fileno(), -1)
            self.assertEqual(fresh.app_end.fileno(), -1)
            self.assertEqual([x[:2] for x in sent], [('CLOSE', 'reused')])
            self.assertEqual(callback_timeouts, [])
        finally:
            release.set()
            for stream in (old, fresh):
                if stream is not None:
                    stream.abort()
            await asyncio.gather(*(s.pump for s in (old, fresh) if s is not None),
                                 return_exceptions=True)
            for thread in set(threads) | set(client.application_threads):
                thread.join(2)
                self.assertFalse(thread.is_alive())
            for stream in (old, fresh):
                if stream is not None:
                    stream.app_end.close()
                    self.assertEqual(stream.relay_end.fileno(), -1)
            self.assertEqual(client.application_threads, ())

    async def test_finalizer_suspended_in_close_preserves_new_owner(self):
        client = RelayClient(relay_url='ws://127.0.0.1:1/data', identity='target',
                             peer_identity='host', room_id='room', link_id='link',
                             credential='regression-only')
        closing = asyncio.Event()
        release = asyncio.Event()

        async def send(kind, sid, raw=None):
            self.assertEqual(kind, 'CLOSE')
            closing.set()
            await release.wait()

        client._send = send
        old, fresh = _Stream(client, 'reused'), _Stream(client, 'reused')
        client._streams['reused'] = old
        old.start()
        fresh.start()
        try:
            await asyncio.sleep(0)
            old.app_end.close()
            await asyncio.wait_for(closing.wait(), 2)
            client._streams['reused'] = fresh
            release.set()
            await asyncio.wait_for(old.pump, 2)
            self.assertIs(client._streams.get('reused'), fresh)
            await client._dispatch(frame('DATA', b'live'))
            fresh.app_end.settimeout(1)
            self.assertEqual(fresh.app_end.recv(4), b'live')
        finally:
            release.set()
            for stream in (old, fresh):
                stream.app_end.close()
                stream.abort()
            await asyncio.gather(old.pump, fresh.pump, return_exceptions=True)
            self.assertEqual(client._streams, {})
            self.assertTrue(all(s.relay_end.fileno() == -1 for s in (old, fresh)))


class OwnerAuthRegression(unittest.TestCase):
    def test_real_http_missing_wrong_nonascii_then_valid_without_handler_errors(self):
        calls = []
        errors = StringIO()
        outcomes = []
        with warnings.catch_warnings(record=True) as caught, redirect_stderr(errors):
            warnings.simplefilter('always', ResourceWarning)
            with tempfile.TemporaryDirectory(prefix='s2r-owner-', dir=os.environ['TMPDIR']) as tmp:
                with OwnerSurface(Path(tmp), {'approve': lambda p: calls.append(p) or {'ok': True}}) as owner:
                    parts = urlsplit(owner.base)
                    for auth in (None, 'Bearer wrong', 'Bearer \u00e9', 'Bearer ' + owner.token):
                        connection = HTTPConnection(parts.hostname, parts.port, timeout=3)
                        try:
                            headers = {'Content-Type': 'application/json'}
                            if auth is not None:
                                headers['Authorization'] = auth
                            connection.request('POST', '/owner', body=b'{"op":"approve"}', headers=headers)
                            response = connection.getresponse()
                            body = response.read()
                            outcomes.append((response.status, json.loads(body)))
                            self.assertLess(len(body), 128)
                            self.assertEqual(response.getheader('Connection'), 'close')
                            self.assertEqual(response.getheader('Cache-Control'), 'no-store')
                            self.assertEqual(response.getheader('Content-Type'), 'application/json')
                        except OSError as exc:
                            # Keep going so post-rejection usability is tested even RED.
                            outcomes.append(type(exc).__name__)
                        finally:
                            connection.close()
                    token_leaked = owner.token in errors.getvalue()
                self.assertFalse(owner.thread.is_alive())
                self.assertEqual(owner.server.fileno(), -1)
            gc.collect()
        self.assertEqual(calls, [{}])  # only the final, authenticated operation
        self.assertEqual(outcomes[-1], (200, {'ok': True}))
        self.assertFalse(token_leaked)
        self.assertEqual(outcomes[:3], [(403, {'error': 'owner_required'})] * 3)
        # Boolean assertion avoids publishing malformed input/server diagnostics.
        self.assertFalse(bool(errors.getvalue()), 'unexpected server stderr')
        self.assertFalse(any(issubclass(w.category, ResourceWarning) for w in caught))
