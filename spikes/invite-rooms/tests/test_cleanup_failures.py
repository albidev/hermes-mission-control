"""Failure-path ownership only; never launches native/model processes."""
import asyncio
import contextlib
import io
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from connector import ConnectorError, RelayClient, _Stream
import gate_f1_local as net
import gate_s1_native as native


class CleanupFailures(unittest.TestCase):
    def test_suspended_close_is_bounded_and_owned_resources_released(self):
        refs = {}
        closing = threading.Event()
        class SlowClose(RelayClient):
            async def _run(self):
                stream = refs['stream'] = _Stream(self, 'cleanup-only')
                self._streams[stream.stream_id] = stream
                stream.start()
                self._connected.set()
                await asyncio.Future()
            async def _send(self, *args, **kwargs):
                refs['release'] = asyncio.get_running_loop().create_future()
                closing.set()
                await refs['release']
        client = SlowClose(relay_url='ws://127.0.0.1:1/data', identity='unit',
                           peer_identity='peer', link_id='unit', room_id='unit', credential='test-only')
        try:
            client.start(wait=2)
            start = time.monotonic()
            try:
                client.stop()
            except ConnectorError as exc:
                self.assertNotIn('test-only', str(exc))
            self.assertTrue(closing.wait(1))
            self.assertLess(time.monotonic() - start, 4)
            self.assertFalse(client._thread.is_alive())
            self.assertTrue(client._loop.is_closed())
            self.assertEqual(refs['stream'].relay_end.fileno(), -1)
            self.assertTrue(refs['stream'].pump.done())
        finally:
            if client._thread.is_alive():
                def release():
                    if not refs['release'].done():
                        refs['release'].set_result(None)
                client._loop.call_soon_threadsafe(release)
                client._thread.join(3)
            refs['stream'].app_end.close()

    def test_noncooperative_close_reports_incomplete_without_destroying_tasks(self):
        refs = {}
        release = threading.Event()
        class Client(RelayClient):
            async def _run(self):
                stream = refs['stream'] = _Stream(self, 'incomplete')
                self._streams[stream.stream_id] = stream
                stream.start()
                self._connected.set()
                await asyncio.Future()
            async def _send(self, *args, **kwargs):
                while not release.is_set():
                    try:
                        await asyncio.sleep(.01)
                    except asyncio.CancelledError:
                        pass  # adversarial fixture, not production cancellation policy
        client = Client(relay_url='ws://127.0.0.1:1/data', identity='unit', peer_identity='peer',
                        link_id='unit', room_id='unit', credential='test-only')
        try:
            client.start(wait=2)
            with self.assertRaisesRegex(ConnectorError, 'teardown_incomplete'):
                client.stop()
            self.assertFalse(client._thread.is_alive())
            self.assertFalse(client._loop.is_closed())
            self.assertFalse(refs['stream'].pump.done())
        finally:
            release.set()
            client._thread.join(4)
            if not client._thread.is_alive() and not client._loop.is_closed():
                async def finish():
                    await asyncio.gather(refs['stream'].pump, return_exceptions=True)
                client._loop.run_until_complete(finish())
                client._loop.close()
            refs['stream'].app_end.close()

    def test_native_main_flushes_failure_after_all_owned_cleanup_attempts(self):
        with tempfile.TemporaryDirectory() as directory:
            lab = native.NativeLab.__new__(native.NativeLab)
            lab.out = Path(directory)
            lab.report = {'checks': {}, 'matrix_complete': False}
            calls = []
            class Transport:
                loop = asyncio.new_event_loop()
                thread = None
                clients = []
                def close(self):
                    calls.append('transport')
                    raise RuntimeError('SENSITIVE diagnostic must never be persisted')
            class Proc:
                def __init__(self, fail=False):
                    self.returncode = None
                    self.fail = fail
                def poll(self): return self.returncode
                def terminate(self):
                    calls.append('terminate')
                    if self.fail: raise OSError('SENSITIVE')
                    self.returncode = -15
                def wait(self, timeout): return self.returncode
            class Log:
                closed = False
                def close(self):
                    calls.append('log')
                    self.closed = True
            lab.transports = [Transport()]
            lab.owned = [(Proc(True), {'pgid': -123, 'signals': []}),
                         (Proc(), {'pgid': -124, 'signals': []})]
            lab.logs = [Log()]
            lab.setup = lambda: None
            async def workflow(): pass
            lab.workflow = workflow
            try:
                with patch.object(native, 'NativeLab', return_value=lab), \
                     patch.object(native, 'processes', return_value=[]), \
                     patch.object(native, 'snapshot', return_value={}), \
                     patch.object(native.gc.pf, 'check_port_available', return_value={'available': True}), \
                     contextlib.redirect_stdout(io.StringIO()):
                    code = native.main()
                self.assertEqual(calls.count('terminate'), 2)
                self.assertTrue(lab.logs[0].closed)
                report = json.loads((lab.out / 'report.json').read_text())
                self.assertEqual(code, 1)
                self.assertFalse(report['teardown_complete'])
                self.assertEqual(lab.owned[1][1]['returncode'], -15)
                self.assertTrue(report['cleanup_failures'])
                self.assertNotIn('SENSITIVE', json.dumps(report))
            finally:
                lab.transports[0].loop.close()

    def test_main_persists_report_if_close_itself_raises(self):
        with tempfile.TemporaryDirectory() as directory:
            lab = native.NativeLab.__new__(native.NativeLab)
            lab.out = Path(directory)
            lab.report = {'checks': {}, 'matrix_complete': False}
            lab.setup = lambda: None
            async def workflow(): pass
            lab.workflow = workflow
            def close(): raise RuntimeError('SENSITIVE')
            lab.close = close
            with patch.object(native, 'NativeLab', return_value=lab), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(native.main(), 1)
            report = json.loads((lab.out / 'report.json').read_text())
            self.assertFalse(report['teardown_complete'])
            self.assertEqual(report['cleanup_failures'], [{'code': 'lab_close', 'type': 'RuntimeError'}])
            self.assertNotIn('SENSITIVE', json.dumps(report))

    def test_network_close_restores_spy_and_attempts_clients_after_adapter_failure(self):
        transport = net.NetworkTransport()
        calls = []
        original = net.relay_mod.Relay.route
        transport._original_route = original
        net.relay_mod.Relay.route = lambda *args: None
        class Adapter:
            def close(self):
                calls.append('adapter')
                raise RuntimeError('SENSITIVE')
        class Client:
            _thread = None
            def stop(self):
                calls.append('client')
                raise OSError('SENSITIVE')
        transport.host_adapter = Adapter()
        transport.clients = [Client(), Client()]
        try:
            with self.assertRaises(RuntimeError) as raised:
                transport.close()
            self.assertEqual(calls, ['adapter', 'client', 'client'])
            self.assertIs(net.relay_mod.Relay.route, original)
            self.assertTrue(transport.loop.is_closed())
            self.assertNotIn('SENSITIVE', str(raised.exception))
        finally:
            net.relay_mod.Relay.route = original
            if not transport.loop.is_closed(): transport.loop.close()


if __name__ == '__main__':
    unittest.main()
