"""Opt-in B packet; serial real native lab, never aggregate S1 acceptance."""
import asyncio
import os
import unittest
import test_s1_unrelated_native as binding_packet
from gate_s1_native import NativeLab, native_signals


@unittest.skipUnless(os.environ.get('S1_NATIVE_B_PACKET') == '1', 'explicit serial B native packet only')
class NativeBLifecycleTests(unittest.TestCase):
    def test_current_binding_reconnect_and_lifecycle_packet(self):
        lab = NativeLab()
        with native_signals(lab.report):
            try:
                lab.setup()
                asyncio.run(self.exercise(lab))
            finally:
                lab.close()
                lab.report['packet_scope'] = 'B authority, binding, reconnect and lifecycle only; C/D/E incomplete'
                lab.report['packet_complete'] = (
                    bool(lab.report.get('b_exercise_complete'))
                    and bool(lab.report.get('checks'))
                    and all(check['ok'] for check in lab.report['checks'].values())
                    and lab.report.get('teardown_complete') is True
                    and lab.report.get('source_start') == lab.report.get('source_end'))
                lab.flush()
                print('B_LIFECYCLE_REPORT=' + str(lab.out / 'report.json'), flush=True)
        self.assertTrue(lab.report['packet_complete'])
        self.assertFalse(lab.report['matrix_complete'])

    async def exercise(self, lab):
        await binding_packet.UnrelatedNativeScopeTests().exercise(lab)
        await lab.fault_checks()
        await lab.lifecycle_checks()
        await lab.lost_lifecycle_checks()
        await lab.canonical_readback()
        for name in ('lost_refresh_reply_native_fence_readback',
                     'lost_revoke_reply_native_fence_readback',
                     'native_no_mint_or_authority_change_on_reconnect',
                     'status_hard_horizon', 'refresh_revoke_race_native_fence',
                     'revoke_is_not_stop'):
            self.assertTrue(lab.report['checks'][name]['ok'], name)
        lab.report['b_exercise_complete'] = True


if __name__ == '__main__':
    unittest.main()
