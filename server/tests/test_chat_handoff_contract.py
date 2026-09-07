"""Composed cross-device contract for the sidecar pointer and chat relay."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import last_chat_store
from chat_sync_relay import ChatSyncRelay, user_message_dedupe_key


class ChatHandoffContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self._original_state_file = last_chat_store._STATE_FILE
        self._temp_dir = tempfile.TemporaryDirectory()
        last_chat_store._STATE_FILE = Path(self._temp_dir.name) / "last_chat.json"
        last_chat_store.clear_last_chat()
        self.relay = ChatSyncRelay(buffer_max=32)

    def tearDown(self) -> None:
        last_chat_store.clear_last_chat()
        last_chat_store._STATE_FILE = self._original_state_file
        self._temp_dir.cleanup()

    def test_desktop_pointer_mobile_bootstrap_partial_resume_and_stale_claim(self) -> None:
        desktop = last_chat_store.set_last_chat(
            {"sessionId": "desktop-session", "sessionKey": "desktop-key"},
        )
        self.assertTrue(desktop["accepted"])
        desktop_pointer = desktop["lastChat"]

        # A cold iPhone has stale local state, but bootstrap is read/adopt only:
        # it must not write that candidate before it has seen the canonical pointer.
        iphone_local = {
            "sessionId": "stale-iphone",
            "revision": 99,
            "updatedAt": 10**15,
        }
        iphone_bootstrap = last_chat_store.get_last_chat()
        if iphone_bootstrap is None:
            self.fail("sidecar did not return the desktop pointer")
        self.assertNotEqual(iphone_local["sessionId"], iphone_bootstrap["sessionId"])
        self.assertEqual(iphone_bootstrap, desktop_pointer)
        self.assertEqual(last_chat_store.get_last_chat(), desktop_pointer)

        mobile_queue, replay, latest = self.relay.subscribe(
            "desktop-session", "iphone", since=0
        )
        self.assertEqual(replay, [])
        self.assertEqual(latest, 0)

        durable_snapshot = [
            {"id": "durable-user", "role": "user", "text": "first question"}
        ]
        relay_message = {
            "id": "relay-user",
            "role": "user",
            "text": "message sent while mobile resumed",
        }
        self.relay.publish(
            "desktop-session",
            "desktop",
            "user_message",
            relay_message,
            user_message_dedupe_key("desktop-session", relay_message["id"]),
        )

        # The relay event arrives while the mobile session.resume is in flight.
        # Its partial snapshot omits that event; the client-side merge contract
        # is covered by the Node test, while this asserts the sidecar leg kept it.
        relayed = self.relay.wait(mobile_queue, timeout=0.1)
        if relayed is None:
            self.fail("mobile did not receive the relay event during resume")
        self.assertEqual(relayed["payload"], relay_message)
        self.assertEqual(self.relay.replay_since("desktop-session", 0)[0]["payload"], relay_message)
        visible_ids = [entry["id"] for entry in durable_snapshot]
        visible_ids.append(relayed["payload"]["id"])
        self.assertEqual(visible_ids, ["durable-user", "relay-user"])

        # A newer intentional claim advances the server revision. The stale
        # iPhone claim then receives, rather than replacing, the canonical one.
        newer_desktop = last_chat_store.set_last_chat(
            {"sessionId": "desktop-session-2"},
            expected_revision=desktop_pointer["revision"],
        )
        self.assertTrue(newer_desktop["accepted"])
        stale_claim = last_chat_store.set_last_chat(
            {"sessionId": "stale-iphone", "updatedAt": 10**15},
            expected_revision=desktop_pointer["revision"],
        )
        self.assertFalse(stale_claim["accepted"])
        self.assertTrue(stale_claim["conflict"])
        self.assertEqual(stale_claim["lastChat"], newer_desktop["lastChat"])
        self.assertEqual(last_chat_store.get_last_chat(), newer_desktop["lastChat"])


if __name__ == "__main__":
    unittest.main()
