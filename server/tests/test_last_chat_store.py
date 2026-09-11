"""Tests for the revisioned cross-device last-chat pointer."""
from __future__ import annotations

import importlib
import json
import os
import sys
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
last_chat_store = importlib.import_module("last_chat_store")


class LastChatStoreProtocolTest(unittest.TestCase):
    def setUp(self) -> None:
        self._original_file = last_chat_store._STATE_FILE
        last_chat_store._STATE_FILE = Path(self._original_file.parent / "last_chat_state.test.json")
        last_chat_store.clear_last_chat()

    def tearDown(self) -> None:
        last_chat_store.clear_last_chat()
        last_chat_store._STATE_FILE = self._original_file

    def test_first_claim_creates_server_revision_and_metadata(self) -> None:
        result = last_chat_store.set_last_chat(
            {"sessionId": "desktop-1", "sessionKey": "key-1", "profile": "crossnection", "updatedAt": 1},
        )

        self.assertTrue(result["accepted"])
        pointer = result["lastChat"]
        self.assertEqual(pointer["sessionId"], "desktop-1")
        self.assertEqual(pointer["profile"], "crossnection")
        self.assertEqual(pointer["revision"], 1)
        self.assertGreater(pointer["updatedAt"], 1)

        persisted = json.loads(last_chat_store._STATE_FILE.read_text())
        self.assertEqual(persisted["revision"], 1)
        self.assertEqual(persisted["profile"], "crossnection")
        self.assertNotEqual(persisted["updatedAt"], 1)

    def test_matching_revision_advances_pointer(self) -> None:
        first = last_chat_store.set_last_chat({"sessionId": "desktop-1"})
        second = last_chat_store.set_last_chat(
            {"sessionId": "mobile-1", "updatedAt": 1},
            expected_revision=first["lastChat"]["revision"],
        )

        self.assertTrue(second["accepted"])
        self.assertFalse(second["conflict"])
        self.assertEqual(second["lastChat"]["sessionId"], "mobile-1")
        self.assertEqual(second["lastChat"]["revision"], 2)
        self.assertGreater(second["lastChat"]["updatedAt"], first["lastChat"]["updatedAt"])

    def test_stale_revision_returns_canonical_pointer_without_replacing_it(self) -> None:
        first = last_chat_store.set_last_chat({"sessionId": "desktop-1"})
        current = last_chat_store.set_last_chat(
            {"sessionId": "mobile-1"},
            expected_revision=first["lastChat"]["revision"],
        )
        stale = last_chat_store.set_last_chat(
            {"sessionId": "stale-device", "updatedAt": 9999999999999},
            expected_revision=first["lastChat"]["revision"],
        )

        self.assertFalse(stale["accepted"])
        self.assertTrue(stale["conflict"])
        self.assertEqual(stale["lastChat"], current["lastChat"])
        self.assertEqual(last_chat_store.get_last_chat(), current["lastChat"])

    def test_client_wall_clock_does_not_control_server_ordering(self) -> None:
        first = last_chat_store.set_last_chat({"sessionId": "desktop-1", "updatedAt": 10**15})
        second = last_chat_store.set_last_chat(
            {"sessionId": "mobile-1", "updatedAt": 1},
            expected_revision=first["lastChat"]["revision"],
        )

        self.assertTrue(second["accepted"])
        self.assertNotEqual(second["lastChat"]["updatedAt"], 1)
        self.assertGreater(second["lastChat"]["updatedAt"], first["lastChat"]["updatedAt"])

    def test_missing_expected_revision_cannot_overwrite_existing_pointer(self) -> None:
        first = last_chat_store.set_last_chat({"sessionId": "desktop-1"})
        stale = last_chat_store.set_last_chat({"sessionId": "stale-device"})

        self.assertFalse(stale["accepted"])
        self.assertEqual(stale["lastChat"]["revision"], first["lastChat"]["revision"])
        self.assertEqual(stale["lastChat"]["sessionId"], "desktop-1")


class LastChatHttpBoundaryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server_module = importlib.import_module("local_telemetry_server")
        cls._original_token = os.environ.get("MISSION_CONTROL_TOKEN")
        os.environ["MISSION_CONTROL_TOKEN"] = "test-token"
        cls._original_file = last_chat_store._STATE_FILE
        last_chat_store._STATE_FILE = Path(cls._original_file.parent / "last_chat_http.test.json")
        last_chat_store.clear_last_chat()
        cls.httpd = cls.server_module.ThreadingHTTPServer(("127.0.0.1", 0), cls.server_module.Handler)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=2)
        last_chat_store.clear_last_chat()
        last_chat_store._STATE_FILE = cls._original_file
        if cls._original_token is None:
            os.environ.pop("MISSION_CONTROL_TOKEN", None)
        else:
            os.environ["MISSION_CONTROL_TOKEN"] = cls._original_token

    def _post(self, payload: object) -> tuple[int, dict]:
        host, port = self.httpd.server_address
        connection = HTTPConnection(host, port, timeout=2)
        body = json.dumps(payload).encode("utf-8")
        connection.request(
            "POST",
            "/api/local/chat/last",
            body=body,
            headers={
                "Authorization": "Bearer test-token",
                "Content-Type": "application/json",
                "Content-Length": str(len(body)),
            },
        )
        response = connection.getresponse()
        data = json.loads(response.read())
        connection.close()
        return response.status, data

    def _get(self) -> tuple[int, dict]:
        host, port = self.httpd.server_address
        connection = HTTPConnection(host, port, timeout=2)
        connection.request("GET", "/api/local/chat/last", headers={"Authorization": "Bearer test-token"})
        response = connection.getresponse()
        data = json.loads(response.read())
        connection.close()
        return response.status, data

    def setUp(self) -> None:
        last_chat_store.clear_last_chat()

    def test_get_preserves_last_chat_shape_and_exposes_revision(self) -> None:
        status, created = self._post({"sessionId": "desktop-1", "sessionKey": "key-1"})
        self.assertEqual(status, 200)

        status, fetched = self._get()

        self.assertEqual(status, 200)
        self.assertEqual(fetched["lastChat"], created["lastChat"])
        self.assertEqual(fetched["lastChat"]["sessionId"], "desktop-1")
        self.assertIn("updatedAt", fetched["lastChat"])
        self.assertEqual(fetched["lastChat"]["revision"], 1)

    def test_missing_session_id_remains_bad_request(self) -> None:
        status, payload = self._post({"sessionKey": "only-key"})

        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "bad_request")

    def test_stale_post_returns_conflict_and_canonical_pointer(self) -> None:
        status, first = self._post({"sessionId": "desktop-1"})
        self.assertEqual(status, 200)
        status, second = self._post(
            {"sessionId": "mobile-1", "expectedRevision": first["lastChat"]["revision"]}
        )
        self.assertEqual(status, 200)
        status, stale = self._post(
            {"sessionId": "stale-device", "expectedRevision": first["lastChat"]["revision"]}
        )

        self.assertEqual(status, 409)
        self.assertEqual(stale["error"], "revision_conflict")
        self.assertEqual(stale["lastChat"], second["lastChat"])


if __name__ == "__main__":
    unittest.main()
