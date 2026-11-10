"""ROOM-LOG-1 bounded spike: real loopback mTLS listener + explicit owner policy.

The native reader below is a synthetic local fixture.  mTLS identity and the
control listener are real; no mocked peer identity is used.  This deliberately
stops short of claiming a live Core installation E2E.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pairing_support as ps
import test_control_listener_w2a as w2a
from invite_rooms.room_log import InMemoryOwnerReadAuthority, OwnerRoomLogHandler, RoomLogAuthorization, RoomLogDenied
from invite_rooms.pairing_journal import JournalLinkAuthority

from test_control_peer_context_w2b import H1Base
import test_control_peer_context_w2b as w2b

w2b.setUpModule()


class CoreFixture:
    def __init__(self):
        self.calls = []
        self.epoch = 7
        self.page = {
            "events": [
                {"seq": 1, "event_id": "journal-original-A-001", "kind": "message.user",
                 "room_id": "room-a", "authority_epoch": 7,
                 "actor": {"kind": "member", "id": "member-a"}, "payload": {"text": "hello"},
                 "private": {"grant": "SECRET-CANARY"}},
                {"seq": 2, "event_id": "control-hidden", "kind": "authority.claimed",
                 "room_id": "room-a", "authority_epoch": 7,
                 "actor": {"kind": "system", "id": "authority"}, "payload": {"text": "hidden"}},
                {"seq": 3, "event_id": "journal-original-A-003", "kind": "message.member",
                 "room_id": "room-a", "authority_epoch": 7,
                 "actor": {"kind": "member", "id": "member-b"}, "payload": {"text": "world"},
                 "private": {"Authorization": "Bearer secret"}},
            ], "cursor": 3, "latest_seq": 3, "has_more": False,
            "authority": {"gateway_id": "gateway-a", "epoch": 7},
        }
        self.cutover = False

    async def __call__(self, method, params):
        self.calls.append((method, dict(params)))
        if method == "groups.state":
            return {"room": {"room_id": "room-a", "home_install_id": ps.INSTALL_A,
                              "authority_gateway_id": "gateway-a", "authority_epoch": self.epoch,
                              "status": "live", "members": [{"member_id": "member-a"}, {"member_id": "member-b"}]}}
        if method == "groups.log":
            if self.cutover:
                self.epoch = 8
            since = params["since_seq"]
            limit = params["limit"]
            selected = [event for event in self.page["events"] if event["seq"] > since]
            selected = selected[:limit]
            return {**self.page, "events": selected,
                    "cursor": selected[-1]["seq"] if selected else since,
                    "has_more": len([event for event in self.page["events"] if event["seq"] > since]) > len(selected),
                    "authority": {"gateway_id": "gateway-a", "epoch": self.epoch}}
        raise AssertionError(method)


class RoomLogSliceTest(H1Base):
    def setUp(self):
        super().setUp()
        self.endpoint, self.generation = self.ack(self.a, self.b, self.paired_b)
        link = self.a.journal.snapshot().links[self.endpoint]
        peer = link["peer"]
        self.core = CoreFixture()
        self.owner = InMemoryOwnerReadAuthority(RoomLogAuthorization(
            authorization_id="read-a", generation=1, peer_id=peer["peer_id"], endpoint_id=self.endpoint,
            pairing_generation=self.generation, room_id="room-a", home_install_id=ps.INSTALL_A,
            authority_gateway_id="gateway-a", authority_epoch=7, member_id="member-b",
            admission_generation=1, expires_at_monotonic=time.monotonic() + 30),
            allow_legacy_without_consent=True)
        self.handler = OwnerRoomLogHandler(read_authority=self.owner, native_rpc=self.core, clock=time.monotonic)

    def _send_log(self, item, *, since_seq=0, limit=10):
        return self.send(item, client=self.bb, server=self.ba,
                         request={"op": "room_log", "room_id": "room-a", "since_seq": since_seq, "limit": limit},
                         endpoint_id=self.endpoint, generation=self.generation)

    def test_real_mtls_context_reaches_owner_policy_and_preserves_ids(self):
        item = self.listen_host({"room_log": self.handler})
        result = self._send_log(item)
        self.assertEqual(result[:2], ("ok", 200), result)
        body = json.loads(result[2])
        self.assertEqual([e["event_id"] for e in body["events"]],
                         ["journal-original-A-001", "journal-original-A-003"])
        self.assertEqual([e["seq"] for e in body["events"]], [1, 3])
        self.assertEqual([name for name, _ in self.core.calls],
                         ["groups.state", "groups.log", "groups.state"])
        encoded = result[2]
        self.assertNotIn(b"SECRET-CANARY", encoded)
        self.assertNotIn(b"Authorization", encoded)

    def test_execution_grant_or_wrong_room_cannot_create_read_authority(self):
        self.owner.revoke()
        item = self.listen_host({"room_log": self.handler})
        result = self._send_log(item)
        self.assertEqual(result[:2], ("ok", 403), result)
        self.assertEqual(self.core.calls, [])

    def test_pagination_preserves_cursor_and_allows_empty_filtered_page(self):
        item = self.listen_host({"room_log": self.handler})
        first = self._send_log(item, limit=1)
        self.assertEqual(first[:2], ("ok", 200), first)
        first_body = json.loads(first[2])
        self.assertEqual([event["seq"] for event in first_body["events"]], [1])
        self.assertEqual((first_body["cursor"], first_body["has_more"]), (1, True))

        empty = self._send_log(item, since_seq=1, limit=1)
        self.assertEqual(empty[:2], ("ok", 200), empty)
        empty_body = json.loads(empty[2])
        self.assertEqual(empty_body["events"], [])
        self.assertEqual((empty_body["cursor"], empty_body["has_more"]), (2, True))

        final = self._send_log(item, since_seq=2, limit=1)
        self.assertEqual(final[:2], ("ok", 200), final)
        final_body = json.loads(final[2])
        self.assertEqual([event["seq"] for event in final_body["events"]], [3])
        self.assertEqual((final_body["cursor"], final_body["has_more"]), (3, False))

    def test_history_off_enforces_join_boundary_even_when_guest_requests_zero(self):
        current = self.owner.current()
        self.owner = InMemoryOwnerReadAuthority(dataclasses.replace(
            current, history_policy="off", from_seq_inclusive=3),
            allow_legacy_without_consent=True)
        self.handler.read_authority = self.owner
        item = self.listen_host({"room_log": self.handler})
        result = self._send_log(item)
        self.assertEqual(result[:2], ("ok", 200), result)
        body = json.loads(result[2])
        self.assertEqual([e["event_id"] for e in body["events"]], ["journal-original-A-003"])
        self.assertEqual(self.core.calls[1][1]["since_seq"], 2)

    def test_revoke_or_epoch_cutover_after_native_io_denies_page(self):
        class CutoverCore(CoreFixture):
            async def __call__(inner, method, params):
                value = await super().__call__(method, params)
                if method == "groups.log":
                    self.owner.revoke()
                return value
        self.core = CutoverCore()
        self.handler.native_rpc = self.core
        item = self.listen_host({"room_log": self.handler})
        result = self._send_log(item)
        self.assertEqual(result[:2], ("ok", 403), result)
        self.assertNotIn(b"journal-original-A-001", result[2])

    def test_replacement_record_same_scope_is_denied_at_release(self):
        class ReplacementCore(CoreFixture):
            async def __call__(inner, method, params):
                value = await super().__call__(method, params)
                if method == "groups.log":
                    current = self.owner.current()
                    self.owner.issue(dataclasses.replace(
                        current, generation=current.generation + 1))
                return value
        self.core = ReplacementCore()
        self.handler.native_rpc = self.core
        item = self.listen_host({"room_log": self.handler})
        result = self._send_log(item)
        self.assertEqual(result[:2], ("ok", 403), result)
        self.assertNotIn(b"journal-original-A-001", result[2])

    def test_history_policy_cannot_change_after_creation(self):
        current = self.owner.current()
        with self.assertRaisesRegex(ValueError, "immutable_history_policy"):
            self.owner.cutover(from_seq_inclusive=3, history_policy="off")
        self.assertIs(self.owner.current(), current)

    def test_concurrent_issue_revoke_cannot_publish_prepared_authority(self):
        original_prepare = self.owner._prepare
        entered = threading.Event()
        release = threading.Event()

        def paused(authorization):
            prepared = original_prepare(authorization)
            entered.set()
            self.assertTrue(release.wait(2))
            return prepared

        self.owner._prepare = paused
        current = self.owner.current()
        issue = dataclasses.replace(current, generation=2, admission_generation=2,
                                    record_token="concurrent-new")
        worker = threading.Thread(target=self.owner.issue, args=(issue,))
        worker.start()
        self.assertTrue(entered.wait(2))
        revoked = []

        def revoke_after_publication():
            release.set()
            self.owner.revoke()
            revoked.append(self.owner.current())

        revoker = threading.Thread(target=revoke_after_publication)
        revoker.start()
        worker.join(2)
        revoker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertFalse(revoker.is_alive())
        self.assertEqual(len(revoked), 1)
        self.assertFalse(revoked[0].active)
        self.assertFalse(self.owner.current().active)
        self.assertGreaterEqual(self.owner.current().lifecycle_revision,
                                revoked[0].lifecycle_revision)

    def test_inflight_listener_read_denied_until_fresh_authorized_issue(self):
        entered = threading.Event()
        release = threading.Event()
        parent = self

        class BlockingCore(CoreFixture):
            async def __call__(inner, method, params):
                if method == "groups.log":
                    entered.set()
                    parent.assertTrue(release.wait(2))
                return await super().__call__(method, params)

        self.core = BlockingCore()
        self.handler.native_rpc = self.core
        item = self.listen_host({"room_log": self.handler})
        result = []
        worker = threading.Thread(target=lambda: result.append(self._send_log(item)))
        worker.start()
        self.assertTrue(entered.wait(2))
        self.owner.revoke()
        release.set()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result[0][:2], ("ok", 403), result)

        self.assertEqual(self._send_log(item)[:2], ("ok", 403))
        revoked = self.owner.current()
        fresh = dataclasses.replace(revoked, active=True, record_token="fresh-after-inflight",
                                    generation=revoked.generation + 1,
                                    admission_generation=revoked.admission_generation + 1)
        self.owner.issue(fresh)
        self.assertEqual(self._send_log(item)[:2], ("ok", 200))

    def test_spoofed_peer_and_expired_authorization_are_denied(self):
        self.owner._authorization = RoomLogAuthorization(
            authorization_id="read-a", generation=1, peer_id="peer-" + "f" * 32, endpoint_id=self.endpoint,
            pairing_generation=self.generation, room_id="room-a", home_install_id=ps.INSTALL_A,
            authority_gateway_id="gateway-a", authority_epoch=7, member_id="member-b",
            admission_generation=1, expires_at_monotonic=time.monotonic() - 1)
        item = self.listen_host({"room_log": self.handler})
        result = self._send_log(item)
        self.assertEqual(result[:2], ("ok", 403), result)
        self.assertEqual(self.core.calls, [])

    def test_revoke_reissue_original_object_is_rejected_but_fresh_admission_works(self):
        original = self.owner.current()
        self.owner.revoke()
        with self.assertRaisesRegex(ValueError, "revoked_authorization"):
            self.owner.issue(original)
        fresh = dataclasses.replace(original, active=True, record_token="fresh-token", generation=3, admission_generation=2)
        self.owner.issue(fresh)
        self.assertGreater(self.owner.current().lifecycle_revision, original.lifecycle_revision)
        item = self.listen_host({"room_log": self.handler})
        result = self._send_log(item)
        self.assertEqual(result[:2], ("ok", 200), result)

    def test_off_admission_boundary_can_change_without_changing_room_policy(self):
        current = dataclasses.replace(self.owner.current(), history_policy="off", from_seq_inclusive=2)
        self.owner = InMemoryOwnerReadAuthority(current, room_history_policy="off",
                                                allow_legacy_without_consent=True)
        self.owner.revoke()
        fresh = dataclasses.replace(current, active=True, record_token="fresh-off", generation=3, admission_generation=2,
                                    from_seq_inclusive=3)
        self.owner.issue(fresh)
        self.handler.read_authority = self.owner
        item = self.listen_host({"room_log": self.handler})
        result = self._send_log(item)
        self.assertEqual(result[:2], ("ok", 200), result)
        self.assertEqual([e["seq"] for e in json.loads(result[2])["events"]], [3])

    def test_nan_and_bad_page_metadata_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "invalid_authorization"):
            InMemoryOwnerReadAuthority(RoomLogAuthorization(
                authorization_id="x", generation=1, peer_id="p", endpoint_id="e", pairing_generation=1,
                room_id="r", home_install_id="h", authority_gateway_id="g", authority_epoch=1,
                member_id="m", admission_generation=1, expires_at_monotonic=float("nan")))
        class BadPage(CoreFixture):
            async def __call__(inner, method, params):
                page = await super().__call__(method, params)
                if method == "groups.log":
                    page["events"] = inner.page["events"]
                    page["cursor"] = 0
                return page
        self.core = BadPage()
        self.handler.native_rpc = self.core
        item = self.listen_host({"room_log": self.handler})
        result = self._send_log(item)
        self.assertEqual(result[:2], ("ok", 403), result)

    def test_constructor_and_same_admission_off_boundary_are_explicitly_validated(self):
        current = self.owner.current()
        with self.assertRaisesRegex(ValueError, "invalid_history_boundary"):
            InMemoryOwnerReadAuthority(dataclasses.replace(current, from_seq_inclusive=3))
        with self.assertRaisesRegex(ValueError, "invalid_history_policy"):
            InMemoryOwnerReadAuthority(current, room_history_policy="")

        off = InMemoryOwnerReadAuthority(
            dataclasses.replace(current, history_policy="off", from_seq_inclusive=3),
            room_history_policy="off")
        for boundary in (1, 4):
            with self.assertRaisesRegex(ValueError, "invalid_history_boundary"):
                off.cutover(from_seq_inclusive=boundary)


if __name__ == "__main__":
    unittest.main()
