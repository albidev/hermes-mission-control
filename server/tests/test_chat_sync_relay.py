"""Tests for the Mission Control cross-device chat relay."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from chat_sync_relay import ChatSyncRelay, core_event_dedupe_key, user_message_dedupe_key


class ChatSyncRelayTest(unittest.TestCase):
    def setUp(self) -> None:
        self.relay = ChatSyncRelay(buffer_max=32)

    def test_broadcasts_to_other_session_viewers(self) -> None:
        desktop, desktop_replay, _ = self.relay.subscribe("s1", "desktop")
        mobile, mobile_replay, _ = self.relay.subscribe("s1", "mobile")
        self.assertEqual(desktop_replay, [])
        self.assertEqual(mobile_replay, [])

        event = {"type": "reasoning.delta", "session_id": "s1", "seq": 4}
        result = self.relay.publish(
            "s1", "desktop", "gateway_event", event,
            core_event_dedupe_key("s1", event),
        )
        self.assertEqual(result["relay_seq"], 1)
        self.assertIsNone(self.relay.wait(desktop, timeout=0.01))
        mobile_event = self.relay.wait(mobile, timeout=0.01)
        if mobile_event is None:
            self.fail("mobile did not receive the mirrored event")
        self.assertEqual(mobile_event["payload"], event)

    def test_core_events_are_deduplicated_across_publishers(self) -> None:
        event = {"type": "tool.progress", "session_id": "s1", "seq": 9}
        key = core_event_dedupe_key("s1", event)
        first = self.relay.publish("s1", "desktop", "gateway_event", event, key)
        second = self.relay.publish("s1", "mobile", "gateway_event", event, key)
        self.assertEqual(first["relay_seq"], second["relay_seq"])
        self.assertTrue(second["deduplicated"])
        self.assertEqual(self.relay.stats()["events"], 1)

    def test_replays_only_after_requested_watermark(self) -> None:
        for seq in range(1, 4):
            event = {"type": "message.delta", "session_id": "s1", "seq": seq}
            self.relay.publish("s1", "desktop", "gateway_event", event, core_event_dedupe_key("s1", event))
        _queue, replay, latest = self.relay.subscribe("s1", "mobile", since=1)
        self.assertEqual(latest, 3)
        self.assertEqual([item["relay_seq"] for item in replay], [2, 3])

    def test_user_message_key_is_stable(self) -> None:
        key = user_message_dedupe_key("s1", "user-123")
        self.assertEqual(key, "user:s1:user-123")
        self.assertEqual(key, user_message_dedupe_key("s1", "user-123"))


if __name__ == "__main__":
    unittest.main()
