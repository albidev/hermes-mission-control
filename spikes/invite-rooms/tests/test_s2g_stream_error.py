"""S2 follow-up D-O1: a relay STREAM_ERROR ends exactly one stream, stream-locally.

Behaviour contracts (real socketpairs, real pumps, real ``_dispatch``/``_session``;
only the outer websocket is a fake):
- the aborted stream is unmapped even if its pump never ran a single step;
- late same-id DATA after STREAM_ERROR never raises, never tears down the shared
  session, never resurrects the stream (no new callback) and is answered with a
  stream-local CLOSE for that id;
- a sibling stream on the same session keeps working;
- wrong-route frames are still rejected as a session error;
- the R1 replacement contract is untouched.
"""
import asyncio
import base64
import json
from pathlib import Path
import sys
import threading
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(HERE), str(HERE.parents[1] / 'server')]
import connector  # noqa: E402
from connector import ConnectorError, RelayClient, _Stream  # noqa: E402


def frame(kind, sid='s1', payload=b'opaque', **override):
    f = dict(type=kind, source='host', target='target', room_id='room', link_id='link', stream_id=sid)
    if kind == 'DATA':
        f['payload'] = base64.b64encode(payload).decode('ascii')
    if kind == 'STREAM_ERROR':
        f = dict(type='STREAM_ERROR', stream_id=sid, error='target offline')
    f.update(override)
    return f


def make_client(serve):
    client = RelayClient(relay_url='ws://127.0.0.1:1/data', identity='target', peer_identity='host',
                         room_id='room', link_id='link', credential='s2g-test-only', on_new_stream=serve)
    client.sent = []

    async def send(kind, sid, raw=None):
        client.sent.append((kind, sid))
    client._send = send
    return client


class _Blocking:
    """Application callback that records its streams and blocks until released."""

    def __init__(self):
        self.release = threading.Event()
        self.calls = []
        self.lock = threading.Lock()

    def __call__(self, sock, sid):
        with self.lock:
            self.calls.append(sid)
        self.release.wait(5)


async def _cleanup(client, streams, app):
    app.release.set()
    for s in streams:
        s.abort()
    await asyncio.gather(*(s.pump for s in streams if s.pump), return_exceptions=True)
    for t in client.application_threads:
        t.join(3)
    for s in streams:
        s.app_end.close()


class StreamErrorIsStreamLocal(unittest.IsolatedAsyncioTestCase):
    async def test_stream_error_before_first_pump_step_unmaps_and_late_data_is_local(self):
        app = _Blocking()
        c = make_client(app)
        await c._dispatch(frame('DATA'))                 # create stream + pump (not yet stepped)
        s = c._streams['s1']
        try:
            await c._dispatch(frame('STREAM_ERROR'))     # same loop turn: pump never stepped
            await asyncio.sleep(0.05)
            self.assertTrue(s.pump.done())
            self.assertIsNone(c._streams.get('s1'))      # aborted stream is no longer mapped
            self.assertEqual(s.relay_end.fileno(), -1)
            await c._dispatch(frame('DATA', payload=b'late'))   # must not raise
            self.assertIsNone(c._streams.get('s1'))      # not resurrected
            self.assertEqual(app.calls, ['s1'])          # no second application callback
            self.assertEqual(c.sent, [('CLOSE', 's1')])  # stream-local answer for that id
            await c._dispatch(frame('CLOSE'))            # late CLOSE: no-op, no reply
            self.assertEqual(c.sent, [('CLOSE', 's1')])
        finally:
            await _cleanup(c, [s], app)
        self.assertEqual(c.application_threads, ())

    async def test_stream_error_on_started_pump_then_buffered_data(self):
        app = _Blocking()
        c = make_client(app)
        await c._dispatch(frame('DATA'))
        s = c._streams['s1']
        try:
            await asyncio.sleep(0)                       # pump suspended in sock_recv
            self.assertFalse(s.pump.done())
            await c._dispatch(frame('STREAM_ERROR'))
            await c._dispatch(frame('DATA', payload=b'late'))  # before the cancel lands; must not raise
            self.assertIsNone(c._streams.get('s1'))
            await asyncio.sleep(0.05)
            self.assertTrue(s.pump.done())
            self.assertEqual(c._streams, {})
            self.assertEqual(app.calls, ['s1'])
            self.assertEqual(c.sent, [('CLOSE', 's1')])  # the aborted pump itself sends nothing
        finally:
            await _cleanup(c, [s], app)

    async def test_wrong_route_for_aborted_id_is_still_a_session_error(self):
        app = _Blocking()
        c = make_client(app)
        await c._dispatch(frame('DATA'))
        s = c._streams['s1']
        try:
            await c._dispatch(frame('STREAM_ERROR'))
            for bad in (dict(link_id='other'), dict(room_id='other'), dict(source='intruder'),
                        dict(target='someone-else')):
                with self.assertRaises(ConnectorError):
                    await c._dispatch(frame('DATA', payload=b'late', **bad))
            with self.assertRaises(ConnectorError):
                await c._dispatch(frame('PING'))
        finally:
            await _cleanup(c, [s], app)

    async def test_stream_error_for_unknown_id_is_ignored(self):
        app = _Blocking()
        c = make_client(app)
        await c._dispatch(frame('STREAM_ERROR', sid='never-seen'))
        self.assertEqual(c._streams, {})
        self.assertEqual(c.sent, [])

    async def test_r1_replacement_still_survives_old_finalizer(self):
        app = _Blocking()
        c = make_client(app)
        old = _Stream(c, 's1'); c._streams['s1'] = old; old.start()
        fresh = None
        try:
            await asyncio.sleep(0)
            old.app_end.close()
            await c._dispatch(frame('DATA'))             # BrokenPipe: abort old, CLOSE sent
            await c._dispatch(frame('DATA'))             # replacement is still allowed after BrokenPipe
            fresh = c._streams['s1']
            self.assertIsNot(fresh, old)
            await asyncio.sleep(0.02)
            self.assertIs(c._streams.get('s1'), fresh)
        finally:
            await _cleanup(c, [s for s in (old, fresh) if s is not None], app)


class _FakeWS:
    """Outer websocket stand-in: scripted inbound frames, recorded outbound frames."""

    def __init__(self, script):
        self.script, self.out = script, []
        self.consumed = False

    async def send(self, raw):
        self.out.append(json.loads(raw))

    async def recv(self):
        return json.dumps({'type': 'READY', 'incarnation': 1})

    async def close(self, code=1000, reason=''):
        pass

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        for step in self.script:
            if callable(step):
                await step()
                continue
            yield json.dumps(step)
        self.consumed = True


class _Connect:
    def __init__(self, ws):
        self.ws = ws

    def __call__(self, *a, **k):
        return self

    async def __aenter__(self):
        return self.ws

    async def __aexit__(self, *exc):
        return False


class SharedSessionSurvivesLateData(unittest.IsolatedAsyncioTestCase):
    async def test_sibling_stream_survives_stream_error_plus_late_data(self):
        received = {}
        release = threading.Event()
        calls = []

        def serve(sock, sid):
            calls.append(sid)
            sock.settimeout(3)
            chunks = []
            try:
                while True:
                    data = sock.recv(64)
                    if not data:
                        break
                    chunks.append(data)
                    if sid == 's2' and b''.join(chunks).endswith(b'more'):
                        break
            except OSError:
                pass
            received[sid] = b''.join(chunks)
            release.wait(3)

        async def settle():
            await asyncio.sleep(0.05)

        ws = _FakeWS([frame('DATA', 's1', b'one'), frame('DATA', 's2', b'two-'), settle,
                      frame('STREAM_ERROR', 's1'), frame('DATA', 's1', b'late'),
                      frame('DATA', 's2', b'more'), settle])
        c = RelayClient(relay_url='ws://127.0.0.1:1/data', identity='target', peer_identity='host',
                        room_id='room', link_id='link', credential='s2g-test-only', on_new_stream=serve)
        session_error = None
        with mock.patch('websockets.asyncio.client.connect', _Connect(ws)):
            try:
                await c._session()
            except Exception as exc:  # record type only
                session_error = type(exc).__name__
        for _ in range(100):
            if 's2' in received:
                break
            await asyncio.sleep(0.02)
        release.set()
        for t in c.application_threads:
            t.join(3)
        await asyncio.sleep(0.05)
        self.assertIsNone(session_error)
        self.assertTrue(ws.consumed)
        self.assertEqual(received.get('s2'), b'two-more')   # sibling stream kept working
        self.assertEqual(sorted(calls), ['s1', 's2'])         # s1 was never resurrected
        closes = [f['stream_id'] for f in ws.out if f['type'] == 'CLOSE']
        self.assertIn('s1', closes)                           # stream-local answer to late DATA
        self.assertEqual(c._streams, {})
        self.assertEqual(c.application_threads, ())


if __name__ == '__main__':
    unittest.main()
