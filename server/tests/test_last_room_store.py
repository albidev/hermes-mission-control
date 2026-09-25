"""Tests for the revisioned cross-device last-room pointer.

Mirror of ``test_last_chat_store.py``: the room pointer is the same CAS
protocol on a different key, so it needs the same coverage. The case that
matters most is ``test_missing_expected_revision_cannot_overwrite_existing_pointer``
— the UI legitimately sends no ``expectedRevision`` when switching to a room
other than the one it last saw, and the claim only succeeds because the client
re-issues it with the canonical revision (see ``claimLastRoomPointer``).
"""
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
last_room_store = importlib.import_module("last_room_store")


class LastRoomStoreProtocolTest(unittest.TestCase):
    def setUp(self) -> None:
        self._original_file = last_room_store._STATE_FILE
        last_room_store._STATE_FILE = Path(self._original_file.parent / "last_room_state.test.json")
        last_room_store.clear_last_room()

    def tearDown(self) -> None:
        last_room_store.clear_last_room()
        last_room_store._STATE_FILE = self._original_file

    def test_first_claim_creates_server_revision_and_metadata(self) -> None:
        result = last_room_store.set_last_room(
            {"roomId": "mc-triage", "roomName": "Example room", "updatedAt": 1},
        )

        self.assertTrue(result["accepted"])
        pointer = result["lastRoom"]
        self.assertEqual(pointer["roomId"], "mc-triage")
        self.assertEqual(pointer["roomName"], "Example room")
        self.assertEqual(pointer["revision"], 1)
        self.assertGreater(pointer["updatedAt"], 1)

        persisted = json.loads(last_room_store._STATE_FILE.read_text())
        self.assertEqual(persisted["revision"], 1)
        self.assertNotEqual(persisted["updatedAt"], 1)

    def test_matching_revision_advances_pointer(self) -> None:
        first = last_room_store.set_last_room({"roomId": "mc-triage"})
        second = last_room_store.set_last_room(
            {"roomId": "mc-delivery", "updatedAt": 1},
            expected_revision=first["lastRoom"]["revision"],
        )

        self.assertTrue(second["accepted"])
        self.assertFalse(second["conflict"])
        self.assertEqual(second["lastRoom"]["roomId"], "mc-delivery")
        self.assertEqual(second["lastRoom"]["revision"], 2)
        self.assertGreater(second["lastRoom"]["updatedAt"], first["lastRoom"]["updatedAt"])

    def test_stale_revision_returns_canonical_pointer_without_replacing_it(self) -> None:
        first = last_room_store.set_last_room({"roomId": "mc-triage"})
        current = last_room_store.set_last_room(
            {"roomId": "mc-delivery"},
            expected_revision=first["lastRoom"]["revision"],
        )
        stale = last_room_store.set_last_room(
            {"roomId": "stale-device", "updatedAt": 9999999999999},
            expected_revision=first["lastRoom"]["revision"],
        )

        self.assertFalse(stale["accepted"])
        self.assertTrue(stale["conflict"])
        self.assertEqual(stale["lastRoom"], current["lastRoom"])
        self.assertEqual(last_room_store.get_last_room(), current["lastRoom"])

    def test_client_wall_clock_does_not_control_server_ordering(self) -> None:
        first = last_room_store.set_last_room({"roomId": "mc-triage", "updatedAt": 10**15})
        second = last_room_store.set_last_room(
            {"roomId": "mc-delivery", "updatedAt": 1},
            expected_revision=first["lastRoom"]["revision"],
        )

        self.assertTrue(second["accepted"])
        self.assertNotEqual(second["lastRoom"]["updatedAt"], 1)
        self.assertGreater(second["lastRoom"]["updatedAt"], first["lastRoom"]["updatedAt"])

    def test_missing_expected_revision_cannot_overwrite_existing_pointer(self) -> None:
        """The store refuses a revisionless claim once a pointer exists.

        This is why the UI needs ``claimLastRoomPointer``: the first attempt of
        a genuine room switch conflicts by design, and only the retry with the
        canonical revision moves the pointer.
        """
        first = last_room_store.set_last_room({"roomId": "mc-triage"})
        blind = last_room_store.set_last_room({"roomId": "mc-delivery"})

        self.assertFalse(blind["accepted"])
        self.assertTrue(blind["conflict"])
        self.assertEqual(blind["lastRoom"]["revision"], first["lastRoom"]["revision"])
        self.assertEqual(blind["lastRoom"]["roomId"], "mc-triage")

        retried = last_room_store.set_last_room(
            {"roomId": "mc-delivery"},
            expected_revision=blind["lastRoom"]["revision"],
        )
        self.assertTrue(retried["accepted"])
        self.assertEqual(retried["lastRoom"]["roomId"], "mc-delivery")

    def test_room_name_survives_and_is_optional(self) -> None:
        last_room_store.set_last_room({"roomId": "mc-triage", "roomName": "Example room"})
        renamed = last_room_store.set_last_room(
            {"roomId": "mc-triage"},
            expected_revision=1,
        )

        self.assertTrue(renamed["accepted"])
        self.assertEqual(renamed["lastRoom"]["roomName"], "Example room")

    def test_get_returns_none_when_never_set(self) -> None:
        self.assertIsNone(last_room_store.get_last_room())

    def test_disallowed_keys_are_not_persisted(self) -> None:
        last_room_store.set_last_room({"roomId": "mc-triage", "roomPath": "/etc/passwd"})

        persisted = json.loads(last_room_store._STATE_FILE.read_text())
        self.assertNotIn("roomPath", persisted)


class LastRoomHttpBoundaryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server_module = importlib.import_module("local_telemetry_server")
        cls._original_token = os.environ.get("MISSION_CONTROL_TOKEN")
        os.environ["MISSION_CONTROL_TOKEN"] = "test-token"
        cls._original_file = last_room_store._STATE_FILE
        last_room_store._STATE_FILE = Path(cls._original_file.parent / "last_room_http.test.json")
        last_room_store.clear_last_room()
        cls.httpd = cls.server_module.ThreadingHTTPServer(("127.0.0.1", 0), cls.server_module.Handler)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=2)
        last_room_store.clear_last_room()
        last_room_store._STATE_FILE = cls._original_file
        if cls._original_token is None:
            os.environ.pop("MISSION_CONTROL_TOKEN", None)
        else:
            os.environ["MISSION_CONTROL_TOKEN"] = cls._original_token

    def _request(self, method: str, path: str, payload: object | None = None) -> tuple[int, dict]:
        host, port = self.httpd.server_address
        connection = HTTPConnection(host, port, timeout=2)
        headers = {"Authorization": "Bearer test-token"}
        body = None
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(body))
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        data = json.loads(response.read())
        connection.close()
        return response.status, data

    def setUp(self) -> None:
        last_room_store.clear_last_room()

    def test_get_preserves_last_room_shape_and_exposes_revision(self) -> None:
        status, created = self._request("POST", "/api/local/room/last", {"roomId": "mc-triage", "roomName": "Triage"})
        self.assertEqual(status, 200)

        status, fetched = self._request("GET", "/api/local/room/last")

        self.assertEqual(status, 200)
        self.assertEqual(fetched["lastRoom"], created["lastRoom"])
        self.assertEqual(fetched["lastRoom"]["revision"], 1)

    def test_get_returns_null_pointer_when_unset(self) -> None:
        status, fetched = self._request("GET", "/api/local/room/last")

        self.assertEqual(status, 200)
        self.assertIsNone(fetched["lastRoom"])

    def test_missing_room_id_remains_bad_request(self) -> None:
        status, payload = self._request("POST", "/api/local/room/last", {"roomName": "only-name"})

        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "bad_request")

    def test_non_integer_expected_revision_is_rejected(self) -> None:
        self._request("POST", "/api/local/room/last", {"roomId": "mc-triage"})
        status, payload = self._request(
            "POST", "/api/local/room/last", {"roomId": "mc-delivery", "expectedRevision": "1"}
        )

        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "bad_request")

    def test_revisionless_post_conflicts_then_retry_with_canonical_revision_wins(self) -> None:
        """HTTP-level proof of the cross-device claim the UI performs."""
        status, first = self._request("POST", "/api/local/room/last", {"roomId": "mc-triage"})
        self.assertEqual(status, 200)

        status, conflict = self._request("POST", "/api/local/room/last", {"roomId": "mc-delivery"})
        self.assertEqual(status, 409)
        self.assertEqual(conflict["error"], "revision_conflict")
        self.assertEqual(conflict["lastRoom"], first["lastRoom"])

        status, retried = self._request(
            "POST",
            "/api/local/room/last",
            {"roomId": "mc-delivery", "expectedRevision": conflict["lastRoom"]["revision"]},
        )
        self.assertEqual(status, 200)
        self.assertEqual(retried["lastRoom"]["roomId"], "mc-delivery")
        self.assertEqual(retried["lastRoom"]["revision"], 2)

    def test_stale_post_returns_conflict_and_canonical_pointer(self) -> None:
        status, first = self._request("POST", "/api/local/room/last", {"roomId": "mc-triage"})
        self.assertEqual(status, 200)
        status, second = self._request(
            "POST",
            "/api/local/room/last",
            {"roomId": "mc-delivery", "expectedRevision": first["lastRoom"]["revision"]},
        )
        self.assertEqual(status, 200)
        status, stale = self._request(
            "POST",
            "/api/local/room/last",
            {"roomId": "stale-device", "expectedRevision": first["lastRoom"]["revision"]},
        )

        self.assertEqual(status, 409)
        self.assertEqual(stale["lastRoom"], second["lastRoom"])

    def test_delete_room_vault_clears_the_registry_entry(self) -> None:
        import tempfile

        import room_vault_store

        original_file = room_vault_store._STATE_FILE
        with tempfile.TemporaryDirectory() as tmp:
            room_vault_store._STATE_FILE = Path(tmp) / "room_vaults.json"
            try:
                room_vault_store.set_room_vault("mc-triage", "example-bot")
                status, payload = self._request("DELETE", "/api/local/room/vault?room_id=mc-triage")

                self.assertEqual(status, 200)
                self.assertTrue(payload["removed"])
                self.assertEqual(room_vault_store.get_room_vault("mc-triage"), "")

                status, again = self._request("DELETE", "/api/local/room/vault?room_id=mc-triage")
                self.assertEqual(status, 200)
                self.assertFalse(again["removed"])
            finally:
                room_vault_store._STATE_FILE = original_file

    def test_delete_room_vault_requires_room_id(self) -> None:
        status, payload = self._request("DELETE", "/api/local/room/vault")

        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "bad_request")


if __name__ == "__main__":
    unittest.main()
