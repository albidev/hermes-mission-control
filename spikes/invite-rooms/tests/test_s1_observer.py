"""Generated observer contract tests; fixtures are not native acceptance proof."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gate_s1_native import NATIVE_ENTRY

ROOT = Path('/Users/albi/.hermes/cache/scratch/room-network-spike-20261011-164930')

class ObserverEntryTests(unittest.TestCase):
    def test_main_import_reset_cannot_discard_observer_and_jsonl_is_framed(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            root = Path(directory)
            (root / 'gateway').mkdir()
            (root / 'hermes_cli').mkdir()
            (root / 'tui_gateway').mkdir()
            (root / 'tui_gateway/hosted_room_driver.py').write_text(
                'class HostedRoomRuntime: pass\n'
                'for name in ("_mark_ambiguous", "_drop_lease", "_record_task_error", "_record_error", "_ensure_lease", "_renew_lease_if_needed"):\n'
                '    setattr(HostedRoomRuntime, name, lambda *a, **k: None)\n')
            for package in ('gateway', 'hermes_cli', 'tui_gateway'):
                (root / package / '__init__.py').write_text('')
            (root / 'gateway/hosted_room_peer.py').write_text(
                'def issue_room_grant(*a, **k): return "PRIVATE-TOKEN"\n'
                'def decode_room_grant(*a, **k): return {"room_id":"fixture-room"}\n')
            (root / 'hermes_cli/main.py').write_text(
                'from gateway import hosted_room_peer as native\n'
                'def reset(*a, **k): return "PRIVATE-TOKEN"\n'
                'native.issue_room_grant = reset\n'
                'def main():\n'
                '    native.issue_room_grant(b"PRIVATE-KEY", grant_id="grant-fixture")\n'
                '    native.issue_room_grant(b"PRIVATE-KEY", grant_id="grant-refresh-fixture")\n'
                '    return 0\n')
            entry, audit = root / 'entry.py', root / 'audit.jsonl'
            entry.write_text(NATIVE_ENTRY)
            compile(entry.read_bytes(), str(entry), 'exec')
            result = subprocess.run([sys.executable, '-B', str(entry), str(root), str(audit), '--help'],
                                    env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'), capture_output=True)
            self.assertEqual(result.returncode, 0)
            data = audit.read_bytes() if audit.exists() else b''
            self.assertNotIn(b'PRIVATE', data)
            self.assertTrue(data.endswith(b'\n'), 'each real JSONL record must end in LF')
            self.assertNotIn(b'\\n', data)
            rows = [json.loads(line) for line in data.splitlines()]
            self.assertEqual([r['kind'] for r in rows if r['event']=='native_issue_room_grant_return'],
                             ['owner_mint', 'refresh'])
            self.assertEqual(rows[0]['event'], 'observer_installed')
            self.assertTrue(all(r['pid'] == rows[0]['pid'] for r in rows))

if __name__ == '__main__':
    unittest.main()
