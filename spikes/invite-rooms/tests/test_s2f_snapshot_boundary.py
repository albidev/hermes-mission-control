"""Native transcript snapshot must wait for discussion completion, not one turn."""
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gate_s2e_native as gate


class DiscussionSnapshotBoundary(unittest.TestCase):
    def test_turn_settled_alone_is_not_a_complete_discussion(self):
        terminal = {'kind': 'turn.settled', 'payload': {}}
        self.assertFalse(gate.discussion_complete([terminal], 'user:one'))

    def test_matching_final_activity_completes_the_snapshot(self):
        events = [
            {'kind': 'turn.settled', 'payload': {}},
            {'kind': 'room.activity', 'payload': {
                'status': 'settled', 'reason_code': 'silent_round', 'discussion_event_id': 'user:one'}},
        ]
        self.assertTrue(gate.discussion_complete(events, 'user:one'))

    def test_another_discussion_activity_cannot_release_the_snapshot(self):
        events = [
            {'kind': 'turn.settled', 'payload': {}},
            {'kind': 'room.activity', 'payload': {
                'status': 'settled', 'discussion_event_id': 'user:other'}},
        ]
        self.assertFalse(gate.discussion_complete(events, 'user:one'))

    def test_final_activity_without_terminal_turn_is_not_enough(self):
        events = [{'kind': 'room.activity', 'payload': {
            'status': 'settled', 'discussion_event_id': 'user:one'}}]
        self.assertFalse(gate.discussion_complete(events, 'user:one'))


if __name__ == '__main__':
    unittest.main()
