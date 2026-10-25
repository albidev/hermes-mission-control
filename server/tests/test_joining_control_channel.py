"""Focused tests for the REAL joining control channel (P3b.2-R).

``runtime_joining.PinnedControlChannel`` is the production path from
``JoiningOwnerService`` to the host: it schema-checks the request with the
dispatcher's own allowlist, encodes exactly one forwarded POST on a fixed path,
hands it to the bounded pinned connector for the verified endpoint/generation
and returns the decoded reply.  These tests drive it against a labelled
connector stand-in -- the mTLS transport itself is P3b.3c's.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from invite_rooms import runtime_joining as rj
from invite_rooms.control.dispatcher import ControlError
from invite_rooms.control.firewall import FirewallDenied, UpstreamAmbiguous, UpstreamResponse

LINK = {"endpoint_id": "link-" + "a" * 32, "generation": 2, "role": "joining",
        "protocol_state": "acknowledged", "session_id": "pairing-" + "1" * 32,
        "peer": {"peer_id": "peer-" + "b" * 32}}
STATUS_REQUEST = {"op": "status", "lobby_id": "lobby-" + "1" * 32, "invite_id": "invite-" + "2" * 32}


def _json_response(payload, *, status=200):
    return UpstreamResponse(status, (("content-type", "application/json"),),
                            json.dumps(payload).encode())


class Connector:
    """Labelled stand-in for ``HostConnector`` (one bounded send, explicit close)."""

    def __init__(self, reply=None, *, error=None, admit_error=None):
        self.reply, self.error, self.admit_error = reply, error, admit_error
        self.requests, self.closed, self.admitted = [], False, 0

    def forward(self, request, admit=None):
        self.requests.append(request)
        if admit is not None:
            self.admitted += 1
            admit()
        if self.admit_error is not None:
            raise self.admit_error
        if self.error is not None:
            raise self.error
        return self.reply

    def close(self):
        self.closed = True


class PinnedControlChannelTest(unittest.TestCase):
    def build(self, connector, *, timeout=rj.TIMEOUT):
        self.built = []

        def factory(link, remaining):
            self.built.append((dict(link), remaining))
            return connector

        return rj.PinnedControlChannel(LINK, connector_factory=factory, timeout=timeout), time.monotonic() + 5.0

    def call(self, channel, request, deadline, admit=None):
        return asyncio.run(channel.call(request, deadline=deadline, admit=admit or (lambda: True)))

    # -- happy path -------------------------------------------------------------------------------------------
    def test_returns_decoded_dict_and_forwards_one_fixed_post(self):
        connector = Connector(_json_response({"status": "ready", "scope": None}))
        channel, deadline = self.build(connector)
        self.assertEqual(self.call(channel, dict(STATUS_REQUEST), deadline),
                         {"status": "ready", "scope": None})
        self.assertEqual(len(connector.requests), 1)
        request = connector.requests[0]
        self.assertEqual((request.method, request.path), ("POST", rj.CONTROL_PATH))
        self.assertEqual(set(json.loads(request.body)), {"op", "lobby_id", "invite_id"})
        self.assertLessEqual(self.built[0][1], rj.TIMEOUT)          # per-call remaining budget
        self.assertTrue(connector.closed)                            # released deterministically

    def test_admission_callback_is_invoked_before_the_single_send(self):
        connector = Connector(_json_response({"status": "ready", "scope": None}))
        channel, deadline = self.build(connector)
        seen = []
        self.call(channel, dict(STATUS_REQUEST), deadline, admit=lambda: seen.append("admit"))
        self.assertEqual(seen, ["admit"])
        self.assertEqual(connector.admitted, 1)

    def test_owner_admission_failure_surfaces_as_unavailable(self):
        connector = Connector(_json_response({"status": "ready", "scope": None}))

        def admit():
            raise FirewallDenied("link generation changed")

        channel, deadline = self.build(connector)
        with self.assertRaises(ControlError):
            self.call(channel, dict(STATUS_REQUEST), deadline, admit=admit)
        self.assertEqual(connector.admitted, 1)      # the connector ran the owner check before sending
        self.assertTrue(connector.closed)

    # -- fail closed ------------------------------------------------------------------------------------------
    def test_unknown_operation_is_rejected_without_touching_the_connector(self):
        connector = Connector(_json_response({}))
        channel, deadline = self.build(connector)
        with self.assertRaises(ControlError):
            self.call(channel, {"op": "shell", "lobby_id": "lobby-1", "invite_id": "invite-1"}, deadline)
        self.assertEqual(connector.requests, [])

    def test_extra_field_is_rejected(self):
        connector = Connector(_json_response({}))
        channel, deadline = self.build(connector)
        with self.assertRaises(ControlError):
            self.call(channel, dict(STATUS_REQUEST, url="http://127.0.0.1"), deadline)
        self.assertEqual(connector.requests, [])

    def test_expired_deadline_emits_nothing(self):
        connector = Connector(_json_response({}))
        channel, _ = self.build(connector)
        with self.assertRaises(ControlError):
            self.call(channel, dict(STATUS_REQUEST), time.monotonic() - 1)
        self.assertEqual(connector.requests, [])
        self.assertEqual(self.built, [])

    def test_missing_transport_is_unavailable(self):
        channel = rj.PinnedControlChannel(LINK)
        rj.configure_joining_transport()
        with self.assertRaises(ControlError):
            self.call(channel, dict(STATUS_REQUEST), time.monotonic() + 5.0)

    def test_transport_hook_is_used_when_no_explicit_factory(self):
        connector = Connector(_json_response({"status": "ready", "scope": None}))
        rj.configure_joining_transport(lambda link, remaining: connector)
        self.addCleanup(rj.configure_joining_transport)
        channel = rj.PinnedControlChannel(LINK)
        self.assertEqual(self.call(channel, dict(STATUS_REQUEST), time.monotonic() + 5.0),
                         {"status": "ready", "scope": None})
        self.assertTrue(connector.closed)

    def test_default_joining_control_never_invents_a_transport(self):
        rj.configure_joining_transport()
        channel = rj.default_joining_control(None, LINK)
        self.assertIsInstance(channel, rj.PinnedControlChannel)
        with self.assertRaises(ControlError):
            self.call(channel, dict(STATUS_REQUEST), time.monotonic() + 5.0)

    def test_connector_ambiguity_is_control_unavailable(self):
        connector = Connector(error=UpstreamAmbiguous("room link outcome unknown"))
        channel, deadline = self.build(connector)
        with self.assertRaises(ControlError):
            self.call(channel, dict(STATUS_REQUEST), deadline)
        self.assertTrue(connector.closed)

    def test_non_200_reply_is_unavailable(self):
        channel, deadline = self.build(Connector(_json_response({"error": "denied"}, status=403)))
        with self.assertRaises(ControlError):
            self.call(channel, dict(STATUS_REQUEST), deadline)

    def test_non_object_reply_is_unavailable(self):
        channel, deadline = self.build(Connector(_json_response(["ready"])))
        with self.assertRaises(ControlError):
            self.call(channel, dict(STATUS_REQUEST), deadline)

    def test_duplicate_keys_in_reply_are_unavailable(self):
        raw = b'{"status":"ready","status":"ready"}'
        channel, deadline = self.build(Connector(UpstreamResponse(200, (), raw)))
        with self.assertRaises(ControlError):
            self.call(channel, dict(STATUS_REQUEST), deadline)

    def test_oversized_reply_is_unavailable(self):
        body = json.dumps({"status": "x" * (rj.CONTROL_RESPONSE_MAX + 1)}).encode()
        channel, deadline = self.build(Connector(UpstreamResponse(200, (), body)))
        with self.assertRaises(ControlError):
            self.call(channel, dict(STATUS_REQUEST), deadline)

    def test_channel_rejects_a_link_without_a_verified_endpoint(self):
        with self.assertRaises(ControlError):
            rj.PinnedControlChannel({"peer": {"peer_id": "peer-1"}})

    def test_close_is_idempotent_and_holds_no_state(self):
        channel, _ = self.build(Connector(_json_response({})))
        asyncio.run(channel.close())
        asyncio.run(channel.close())


if __name__ == "__main__":
    unittest.main()
