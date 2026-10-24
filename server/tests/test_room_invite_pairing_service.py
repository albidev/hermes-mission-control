"""P3b.1a-2: pairing journal, projector, LinkAuthority and service (in-process A<->B with the real codec/identity)."""
from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pairing_support as ps  # noqa: E402
from invite_rooms import identity as ident  # noqa: E402
from invite_rooms import pairing_codec as pc  # noqa: E402
from invite_rooms import pairing_journal as pj  # noqa: E402
from invite_rooms import pairing_service as psvc  # noqa: E402
from invite_rooms import runtime_config  # noqa: E402
from invite_rooms.control.dispatcher import ControlError  # noqa: E402
from invite_rooms.pairing_journal import JournalError  # noqa: E402
from invite_rooms.pairing_service import PairingError  # noqa: E402

SERVER_DIR = str(Path(__file__).resolve().parents[1])
TESTS_DIR = str(Path(__file__).resolve().parent)


class Crash(BaseException):
    """Simulated power loss: not an Exception, so no handler may clean up."""


def setUpModule():
    global TEMPLATES
    TEMPLATES = {i: ps.identity_template(i) for i in (ps.INSTALL_A, ps.INSTALL_B, ps.INSTALL_C)}


def tearDownModule():
    for path in TEMPLATES.values():
        shutil.rmtree(path.parent, ignore_errors=True)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(dir=os.environ.get("MC_TEST_TMP")))
        os.chmod(self.tmp, 0o700)
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.addCleanup(setattr, pj, "_FAULT", None)
        self.clock = ps.Clock()
        self.A = ps.Side(self.tmp, "a", ps.INSTALL_A, template=TEMPLATES[ps.INSTALL_A])
        self.B = ps.Side(self.tmp, "b", ps.INSTALL_B, template=TEMPLATES[ps.INSTALL_B])
        self.a, self.b = self.A.service(self.clock), self.B.service(self.clock)

    def code(self, fn, *args, **kw):
        with self.assertRaises(PairingError) as cm:
            fn(*args, **kw)
        self.assertEqual(str(cm.exception), cm.exception.code)
        self.assertIsNone(cm.exception.__cause__)
        return cm.exception.code

    def pair(self):
        return ps.drive_pairing(self.a, self.b)

    def endpoint(self):
        return self.A.peers()[0]["endpoint_id"]

    def ack(self, r):
        return self.a.send_pair_ack(r["a_id"], ps.link_sender(ps.dispatcher_for(self.b)))


class FullPairingTest(Base):
    def test_full_exchange_publishes_a_registry_the_real_reader_accepts(self):
        r = self.pair()
        self.assertEqual(r["revealed"]["sas"], r["b_sas"]["sas"])          # both owners read the same 6 digits
        self.assertRegex(r["revealed"]["sas"], r"^\d{6}$")
        self.assertEqual((r["row_a"]["state"], r["row_b"]["state"]), ("confirmed", "confirmed"))
        self.assertEqual((r["row_a"]["role"], r["row_b"]["role"]), ("host", "joining"))
        entries = self.A.peers()
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(set(entry), runtime_config._PEER_FIELDS)            # exact reader schema, no grant / adapter_url
        self.assertEqual((entry["installation_id"], entry["pairing_generation"]), (ps.INSTALL_B, 1))
        registry = runtime_config.build_peer_registry(self.A.peers_path)     # parity: the EXISTING reader
        record = registry.lookup(entry["peer_id"])
        self.assertEqual((record.endpoint_id, record.pairing_generation, record.installation_id),
                         (entry["endpoint_id"], 1, ps.INSTALL_B))
        self.assertEqual(registry.binding_for(entry["peer_id"])["endpoint_id"], entry["endpoint_id"])
        # B pins A in its own journal; hosts.json is P3b.2 (nothing is projected for the joining role).
        self.assertEqual(self.B.peers(), [])
        link_b = self.b.authority.current(entry["endpoint_id"])
        self.assertEqual((link_b.protocol_state, link_b.generation), ("confirmed", 1))
        self.assertEqual(link_b.binding["target_install_id"], ps.INSTALL_A)

    def test_codes_are_public_and_carry_no_grant_or_secret(self):
        r = self.pair()
        sess = self.a.journal.snapshot().sessions[r["a_id"]]
        for wire in (r["offer"]["code"], r["accepted"]["code"], r["revealed"]["code"]):
            payload = base64.urlsafe_b64decode(wire.split(".")[2] + "==").decode()
            self.assertNotIn("grant", payload.lower())
            self.assertNotIn("private", payload.lower())
        offer_payload = base64.urlsafe_b64decode(r["offer"]["code"].split(".")[2] + "==").decode()
        self.assertNotIn(sess["r_a"], offer_payload)                         # r_A only in the owner-private journal

    def test_ack_moves_both_sides_to_acknowledged(self):
        r = self.pair()
        ep = self.endpoint()
        row = self.ack(r)
        self.assertEqual(row["state"], "acknowledged")
        self.assertEqual(self.a.authority.current(ep).protocol_state, "acknowledged")
        self.assertEqual(self.b.authority.current(ep).protocol_state, "acknowledged")

    def test_self_pairing_is_refused_without_side_effects(self):
        offer = self.a.start_offer()
        before = self.A.journal_bytes()
        self.assertEqual(self.code(self.a.import_offer, offer["code"]), psvc.INVALID)
        self.assertEqual(self.A.journal_bytes(), before)

    def test_own_and_peer_catalogs_are_core_validated(self):
        wide = ps.Side(self.tmp, "w", ps.INSTALL_C, template=TEMPLATES[ps.INSTALL_C],
                       catalog=ps.make_catalog(ps.INSTALL_C, toolsets=("bot_room", "web", "file")))
        svc = wide.service(self.clock)
        self.assertEqual(self.code(svc.start_offer), psvc.NATIVE)            # own catalog must be bot_room/manual
        wrong = ps.Side(self.tmp, "x", ps.INSTALL_C, template=TEMPLATES[ps.INSTALL_C],
                        catalog=ps.make_catalog(ps.INSTALL_B))
        self.assertEqual(self.code(wrong.service(self.clock).start_offer), psvc.NATIVE)  # installation mismatch
        offer = self.a.start_offer()
        self.assertEqual(self.code(wide.service(self.clock).import_offer, offer["code"]), psvc.NATIVE)
        self.assertFalse(wide.journal_dir.exists())                          # refused before any storage was touched


class SasAndExpiryTest(Base):
    def _hostile(self):
        c = ps.Side(self.tmp, "h", ps.INSTALL_C, template=TEMPLATES[ps.INSTALL_C])
        bundle = ident.load_identity(c.identity_dir, installation_id=ps.INSTALL_C, profile=ps.PROFILE,
                                     now=self.clock())
        wide = ps.make_catalog(ps.INSTALL_C, toolsets=("bot_room", "web", "file"))
        return bundle, bundle.peer_identity(wide)

    def test_hostile_peer_catalog_is_refused_before_anything_is_journaled(self):
        bundle, who = self._hostile()
        now = int(self.clock())
        hostile_offer = pc.build_offer(bundle.consent_key(), who, nonce=os.urandom(32), r_a=os.urandom(32),
                                       expires_at=now + 900, now=now)
        self.assertEqual(self.code(self.b.import_offer, hostile_offer.wire), psvc.INVALID)
        self.assertFalse(self.B.journal_dir.exists())
        offer = self.a.start_offer()
        parsed = pc.parse_offer(offer["code"], now=now)
        hostile_accept = pc.build_accept(bundle.consent_key(), who, parsed, r_b=os.urandom(32),
                                         expires_at=parsed.expires_at, now=now)
        before = self.A.journal_bytes()
        self.assertEqual(self.code(self.a.accept_offer, offer["id"], hostile_accept.wire), psvc.INVALID)
        self.assertEqual(self.A.journal_bytes(), before)
        self.assertEqual(self.a.list_pairings()[0]["state"], "offered")

    def _to_revealed(self):
        offer = self.a.start_offer()
        accepted = self.b.import_offer(offer["code"])
        revealed = self.a.accept_offer(offer["id"], accepted["code"])
        return offer, accepted, revealed

    def test_sas_mismatch_aborts_and_publishes_nothing(self):
        offer, accepted, revealed = self._to_revealed()
        wrong = "000000" if revealed["sas"] != "000000" else "000001"
        self.assertEqual(self.code(self.a.confirm_offer, offer["id"], wrong), psvc.SAS_MISMATCH)
        self.assertEqual(self.A.peers(), [])
        self.assertIsNone(self.a.authority.current(pc.endpoint_id_for_session(
            pc.parse_offer(offer["code"], now=None), pc.parse_accept(accepted["code"], offer=pc.parse_offer(offer["code"], now=None), now=None))))
        self.assertEqual(self.a.list_pairings()[0]["state"], "aborted")
        # the right SAS cannot resurrect an aborted attempt (tombstone)
        self.assertEqual(self.code(self.a.confirm_offer, offer["id"], revealed["sas"]), psvc.UNAVAILABLE)
        self.assertEqual(self.A.peers(), [])
        self.assertEqual(self.code(self.a.accept_offer, offer["id"], accepted["code"]), psvc.UNAVAILABLE)

    def test_sas_must_be_six_digits_and_does_not_burn_the_attempt(self):
        offer, accepted, revealed = self._to_revealed()
        for bad in ("12345", "1234567", "abcdef", "12 456", "", None, 123456):
            self.assertEqual(self.code(self.a.confirm_offer, offer["id"], bad), psvc.SAS_INVALID)
        self.assertEqual(self.a.list_pairings()[0]["state"], "revealed")
        self.assertEqual(self.a.confirm_offer(offer["id"], revealed["sas"])["state"], "confirmed")

    def test_three_failed_comparisons_per_24h_trigger_a_cooldown(self):
        for _ in range(3):
            offer = self.a.start_offer()
            accepted = self.b.import_offer(offer["code"])
            revealed = self.a.accept_offer(offer["id"], accepted["code"])
            self.code(self.a.confirm_offer, offer["id"], "999999" if revealed["sas"] != "999999" else "888888")
            self.b.cancel_import(accepted["id"])                         # B's owner abandons the attempt too
            self.clock.advance(1)
        self.assertEqual(self.code(self.a.start_offer), psvc.COOLDOWN)
        self.clock.advance(24 * 3600 + 5)
        self.assertEqual(self.a.start_offer()["id"].startswith("pairing-"), True)

    def test_code_expires_after_15_minutes_and_confirmation_obeys_it(self):
        offer, accepted, revealed = self._to_revealed()
        self.assertEqual(offer["expires_at"] - int(self.clock()), 900)
        self.clock.advance(901)
        self.assertEqual(self.code(self.a.confirm_offer, offer["id"], revealed["sas"]), psvc.UNAVAILABLE)
        self.assertEqual(self.code(self.b.reveal_import, accepted["id"], revealed["code"]), psvc.UNAVAILABLE)
        self.assertEqual(self.A.peers(), [])
        self.assertEqual(self.a.list_pairings()[0]["state"], "expired")
        # an expired offer cannot be imported at all
        offer2 = self.a.start_offer()
        self.clock.advance(901)
        self.assertEqual(self.code(self.b.import_offer, offer2["code"]), psvc.UNAVAILABLE)

    def test_offer_is_single_use_and_a_variant_accept_aborts(self):
        offer = self.a.start_offer()
        accepted = self.b.import_offer(offer["code"])
        self.a.accept_offer(offer["id"], accepted["code"])
        # another party imports the SAME offer: B-side tombstone is per installation, A-side by accept hash
        c = ps.Side(self.tmp, "c", ps.INSTALL_C, template=TEMPLATES[ps.INSTALL_C]).service(self.clock)
        other = c.import_offer(offer["code"])
        self.assertEqual(self.code(self.a.accept_offer, offer["id"], other["code"]), psvc.UNAVAILABLE)
        self.assertEqual(self.a.list_pairings()[0]["state"], "aborted")        # a second, different accept kills it
        self.assertEqual(self.code(self.a.accept_offer, offer["id"], accepted["code"]), psvc.UNAVAILABLE)

    def test_consumed_offer_is_tombstoned_after_the_pairing(self):
        r = self.pair()
        self.assertEqual(self.code(self.b.import_offer, r["offer"]["code"]), psvc.UNAVAILABLE)
        self.assertEqual(self.code(self.a.accept_offer, r["a_id"], r["accepted"]["code"]), psvc.UNAVAILABLE)
        self.assertEqual(self.code(self.b.reveal_import, r["b_id"], r["revealed"]["code"]), psvc.UNAVAILABLE)

    def test_accept_for_another_offer_is_refused_and_leaves_no_trace(self):
        o1 = self.a.start_offer()
        acc1 = self.b.import_offer(o1["code"])
        self.a.cancel_offer(o1["id"])
        o2 = self.a.start_offer()
        before = self.A.journal_bytes()
        self.assertEqual(self.code(self.a.accept_offer, o2["id"], acc1["code"]), psvc.UNAVAILABLE)
        self.assertEqual(self.A.journal_bytes(), before)

    def test_retried_transitions_return_the_same_stored_bytes(self):
        offer = self.a.start_offer()
        accepted = self.b.import_offer(offer["code"])
        again = self.b.import_offer(offer["code"])
        self.assertEqual((again["id"], again["code"]), (accepted["id"], accepted["code"]))   # no new r_B
        first = self.a.accept_offer(offer["id"], accepted["code"])
        second = self.a.accept_offer(offer["id"], accepted["code"])
        self.assertEqual(first, second)
        s1 = self.b.reveal_import(accepted["id"], first["code"])
        self.assertEqual(s1, self.b.reveal_import(accepted["id"], first["code"]))
        sess = self.a.journal.snapshot().sessions[offer["id"]]
        self.assertEqual(sess["state"], "revealed")

    def test_one_pending_attempt_cancel_then_new_and_randomness_is_fresh(self):
        o1 = self.a.start_offer()
        self.assertEqual(self.code(self.a.start_offer), psvc.PENDING)
        self.a.cancel_offer(o1["id"])
        self.a.cancel_offer(o1["id"])                                   # idempotent
        o2 = self.a.start_offer()
        snap = self.a.journal.snapshot().sessions
        self.assertNotEqual(snap[o1["id"]]["r_a"], snap[o2["id"]]["r_a"])
        self.assertNotEqual(snap[o1["id"]]["nonce"], snap[o2["id"]]["nonce"])
        self.assertEqual(self.a.list_pairings()[1]["state"], "aborted")
        # the joining side also allows a single pending attempt
        self.b.import_offer(o2["code"])
        c = ps.Side(self.tmp, "c", ps.INSTALL_C, template=TEMPLATES[ps.INSTALL_C]).service(self.clock)
        o4 = c.start_offer()
        self.assertEqual(self.code(self.b.import_offer, o4["code"]), psvc.PENDING)
        self.b.cancel_import(self.b.list_pairings()[0]["id"])
        self.assertEqual(self.b.list_pairings()[0]["state"], "aborted")
        self.assertEqual(self.b.import_offer(o4["code"])["id"].startswith("pairing-"), True)


class GrindingTest(Base):
    def test_substituted_accept_cannot_complete_the_honest_side(self):
        offer = self.a.start_offer()
        honest = self.b.import_offer(offer["code"])
        c = ps.Side(self.tmp, "c", ps.INSTALL_C, template=TEMPLATES[ps.INSTALL_C]).service(self.clock)
        attacker = c.import_offer(offer["code"])                           # same public fields, other key
        revealed = self.a.accept_offer(offer["id"], attacker["code"])      # A is fed the substitute accept
        self.assertEqual(self.code(self.b.reveal_import, honest["id"], revealed["code"]), psvc.UNAVAILABLE)
        # even handing B the SAS A shows cannot confirm: B never reached reveal_imported
        self.assertEqual(self.code(self.b.confirm_import, honest["id"], revealed["sas"]), psvc.UNAVAILABLE)
        self.assertEqual(self.B.peers(), [])
        self.assertEqual(self.b.journal.snapshot().sessions[honest["id"]]["state"], "accepted")
        self.assertEqual(self.B.journal_dir.exists(), True)
        # A, if its owner confirms the digits B reads (B has none), would be pinning the attacker: B's owner has no SAS
        self.assertEqual(self.A.peers(), [])


class LinkAuthorityTest(Base):
    def test_confirmed_link_accepts_only_pair_ack(self):
        r = self.pair()
        ep = self.endpoint()
        seen = []
        d = ps.dispatcher_for(self.b, owner_verify=lambda *a: True)
        d.handlers["status"] = lambda req: seen.append(req) or {"ok": True}
        send = ps.link_sender(d)
        state = self.b.authority.current(ep)
        request = {"op": "status", "lobby_id": "lobby-x", "invite_id": "invite-x"}
        with self.assertRaises(ControlError):
            send(ep, state.generation, request)
        self.assertEqual(seen, [])
        self.ack(r)                                                      # now acknowledged -> status admitted
        self.assertEqual(send(ep, state.generation, request), {"ok": True})
        self.assertEqual(len(seen), 1)

    def test_unpair_bumps_generation_revokes_the_link_and_republishes(self):
        r = self.pair()
        self.ack(r)
        ep = self.endpoint()
        peer_id = self.A.peers()[0]["peer_id"]
        out = self.a.unpair(peer_id)
        self.assertEqual(out["generation"], 2)
        self.assertIn("does NOT revoke", out["notice"])
        self.assertIn("stale record", out["notice"])
        state = self.a.authority.current(ep)
        self.assertEqual((state.protocol_state, state.generation), ("revoked", 2))
        self.assertEqual(self.A.peers(), [])
        with self.assertRaises(Exception):
            runtime_config.build_peer_registry(self.A.peers_path).lookup(peer_id)
        self.assertEqual(self.code(self.a.unpair, peer_id), psvc.UNAVAILABLE)
        d = ps.dispatcher_for(self.a)
        for generation in (1, 2):
            with self.assertRaises(ControlError):
                asyncio.run(d.dispatch(ep, generation, {"op": "pair_ack", "ack": {}}))
        self.assertEqual(self.a.list_pairings()[0]["state"], "unpaired")
        self.assertEqual(self.a.list_peers(), [])

    def test_repair_increases_generation_with_a_fresh_endpoint(self):
        r1 = self.pair()
        ep1 = self.endpoint()
        peer_id = self.A.peers()[0]["peer_id"]
        self.a.unpair(peer_id)
        r2 = self.pair()
        entry = self.A.peers()[0]
        self.assertEqual(entry["pairing_generation"], 3)
        self.assertNotEqual(entry["endpoint_id"], ep1)
        self.assertEqual(self.a.authority.current(ep1).protocol_state, "revoked")
        self.assertEqual(self.a.authority.current(entry["endpoint_id"]).generation, 3)
        # re-pair without unpair supersedes the active link and bumps again
        self.pair()
        self.assertEqual(self.A.peers()[0]["pairing_generation"], 4)
        self.assertEqual(self.a.authority.current(entry["endpoint_id"]).protocol_state, "revoked")
        del r1, r2

    def test_unpair_on_the_joining_side_revokes_without_touching_peers_json(self):
        r = self.pair()
        self.ack(r)
        ep = self.endpoint()
        self.b.unpair(pj_peer := self.b.list_peers()[0]["peer_id"])
        self.assertEqual(self.b.authority.current(ep).protocol_state, "revoked")
        self.assertEqual(len(self.A.peers()), 1)                          # A keeps its (now one-sided) record
        self.assertTrue(pj_peer.startswith("peer-"))

    def test_unknown_or_malformed_endpoint_has_no_state(self):
        self.pair()
        for value in ("link-" + "0" * 32, "", None, 7, "../x"):
            self.assertIsNone(self.a.authority.current(value))


class AckRecoveryTest(Base):
    def test_ack_is_persisted_and_idempotent_and_variants_are_conflicts(self):
        r = self.pair()
        ep = self.endpoint()
        req1 = self.a.ack_request(r["a_id"])
        req2 = self.a.ack_request(r["a_id"])                              # lost response: same stored bytes
        self.assertEqual(req1, req2)
        self.assertEqual((req1.endpoint_id, req1.generation), (ep, 1))
        self.assertEqual(req1.request["op"], "pair_ack")
        reply1 = self.b.handle_pair_ack(req1.request)
        self.assertEqual(reply1, self.b.handle_pair_ack(req1.request))     # identical duplicate -> same stored reply
        self.assertEqual(reply1["role"], "joining")
        tampered = json.loads(json.dumps(req1.request))
        tampered["ack"]["signature"] = pc.b64e(b"\x00" * 64)
        self.assertEqual(self.code(self.b.handle_pair_ack, tampered), psvc.UNAVAILABLE)
        self.assertEqual(self.a.complete_ack(r["a_id"], reply1)["state"], "acknowledged")
        self.assertEqual(self.a.complete_ack(r["a_id"], reply1)["state"], "acknowledged")
        variant = dict(reply1, signature=pc.b64e(b"\x01" * 64))
        self.assertEqual(self.code(self.a.complete_ack, r["a_id"], variant), psvc.UNAVAILABLE)

    def test_ack_bound_to_roles_hashes_and_endpoint(self):
        r = self.pair()
        req = self.a.ack_request(r["a_id"])
        for field, value in (("role", "joining"), ("offer_hash", "0" * 64), ("accept_hash", "1" * 64),
                             ("endpoint_id", "link-" + "2" * 32), ("v", 2),
                             ("signature", pc.b64e(b"\x00" * 64)), ("signature", "not-base64!"),
                             ("signature", pc.b64e(b"\x00" * 63))):
            bad = {"op": "pair_ack", "ack": dict(req.request["ack"], **{field: value})}
            self.assertEqual(self.code(self.b.handle_pair_ack, bad), psvc.UNAVAILABLE, field)
        # a validly formed signature by the WRONG key (the joining side's own key) is refused too
        forged = dict(req.request["ack"], signature=pc.b64e(self.b._bundle(create=False).consent_key().sign(
            self.b._ack_message("host", req.request["ack"]["offer_hash"], req.request["ack"]["accept_hash"],
                                req.request["ack"]["endpoint_id"]))))
        self.assertEqual(self.code(self.b.handle_pair_ack, {"op": "pair_ack", "ack": forged}), psvc.UNAVAILABLE)
        self.assertEqual(self.b.list_peers()[0]["state"], "confirmed")           # nothing advanced
        self.assertEqual(self.code(self.b.handle_pair_ack, {"op": "pair_ack", "ack": {}}), psvc.UNAVAILABLE)
        self.assertEqual(self.code(self.b.handle_pair_ack, "x"), psvc.UNAVAILABLE)
        # a reply claiming the host role is refused by A
        reply = self.b.handle_pair_ack(req.request)
        self.assertEqual(self.code(self.a.complete_ack, r["a_id"], dict(reply, role="host")), psvc.UNAVAILABLE)
        # state of the journals is untouched by every refusal
        self.assertEqual(self.b.list_peers()[0]["state"], "acknowledged")

    def test_ack_before_the_peer_confirmed_is_not_admitted_and_recovers_later(self):
        offer = self.a.start_offer()
        accepted = self.b.import_offer(offer["code"])
        revealed = self.a.accept_offer(offer["id"], accepted["code"])
        b_sas = self.b.reveal_import(accepted["id"], revealed["code"])
        self.a.confirm_offer(offer["id"], b_sas["sas"])
        send = ps.link_sender(ps.dispatcher_for(self.b))
        with self.assertRaises(ControlError):                             # B only 'pending': nothing but refusal
            self.a.send_pair_ack(offer["id"], send)
        self.assertEqual(self.a.list_pairings()[0]["state"], "confirmed")
        self.b.confirm_import(accepted["id"], revealed["sas"])
        self.assertEqual(self.a.send_pair_ack(offer["id"], send)["state"], "acknowledged")

    def test_ack_after_unpair_is_refused(self):
        r = self.pair()
        req = self.a.ack_request(r["a_id"])
        self.b.unpair(self.b.list_peers()[0]["peer_id"])
        self.assertEqual(self.code(self.b.handle_pair_ack, req.request), psvc.UNAVAILABLE)
        self.a.unpair(self.A.peers()[0]["peer_id"])
        self.assertEqual(self.code(self.a.ack_request, r["a_id"]), psvc.UNAVAILABLE)


class FailClosedTest(Base):
    def _corrupt(self, mutate):
        self.pair()
        path = self.A.journal_dir / "pairing.journal"
        raw = path.read_bytes()
        path.write_bytes(mutate(raw))
        return path.read_bytes()

    def _assert_closed(self, before):
        for call in (self.a.start_offer, self.a.list_pairings, self.a.reconcile, lambda: self.a.unpair("peer-x"),
                     self.a.list_peers):
            with self.assertRaises(JournalError) as cm:
                call()
            self.assertEqual(cm.exception.code, pj.E_CORRUPT)
        self.assertIsNone(self.a.authority.current(self.endpoint()))
        self.assertEqual((self.A.journal_dir / "pairing.journal").read_bytes(), before)   # never repaired

    def test_flipped_byte_is_corruption(self):
        before = self._corrupt(lambda raw: raw[:200] + bytes([raw[200] ^ 1]) + raw[201:])
        self._assert_closed(before)

    def test_torn_tail_and_trailing_garbage_are_corruption(self):
        self._assert_closed(self._corrupt(lambda raw: raw[:-7]))

    def test_removed_middle_record_breaks_the_chain(self):
        def drop(raw):
            lines = raw.split(b"\n")
            del lines[1]
            return b"\n".join(lines)
        self._assert_closed(self._corrupt(drop))

    def test_reordered_records_and_rewritten_but_consistent_hash_are_refused(self):
        def swap(raw):
            lines = raw.split(b"\n")
            lines[1], lines[2] = lines[2], lines[1]
            return b"\n".join(lines)
        self._assert_closed(self._corrupt(swap))

    def test_hand_written_invalid_transition_with_valid_chain_is_corruption(self):
        self.pair()
        path = self.A.journal_dir / "pairing.journal"
        raw = path.read_bytes()
        state = pj._verify(raw)
        rec = {"seq": state.seq + 1, "prev": state.last_hash, "ts": 1, "type": "revealed",
               "data": {"id": "pairing-" + "0" * 32, "reveal": "x"}}
        rec["hash"] = pj.record_hash(rec)
        path.write_bytes(raw + (pj._dumps(rec) + "\n").encode())
        with self.assertRaises(JournalError) as cm:
            self.a.list_pairings()
        self.assertEqual(cm.exception.code, pj.E_CORRUPT)

    def test_hand_written_confirmation_with_a_skipped_generation_is_corruption(self):
        offer = self.a.start_offer()
        accepted = self.b.import_offer(offer["code"])
        self.a.accept_offer(offer["id"], accepted["code"])
        path = self.A.journal_dir / "pairing.journal"
        raw = path.read_bytes()
        state = pj._verify(raw)
        rec = {"seq": state.seq + 1, "prev": state.last_hash, "ts": 1, "type": "confirmed",
               "data": {"id": offer["id"], "generation": 7}}
        rec["hash"] = pj.record_hash(rec)
        path.write_bytes(raw + (pj._dumps(rec) + "\n").encode())
        with self.assertRaises(JournalError) as cm:
            self.a.list_pairings()
        self.assertEqual(cm.exception.code, pj.E_CORRUPT)
        self.assertEqual(self.A.peers(), [])

    def test_a_reformatted_but_hash_valid_line_is_not_the_canonical_record(self):
        self.pair()
        path = self.A.journal_dir / "pairing.journal"
        lines = path.read_bytes().decode().splitlines()
        lines[0] = json.dumps(json.loads(lines[0]), sort_keys=True)          # same content, different bytes
        self.assertNotEqual(lines[0], json.dumps(json.loads(lines[0]), sort_keys=True, separators=(",", ":")))
        path.write_bytes(("\n".join(lines) + "\n").encode())
        with self.assertRaises(JournalError) as cm:
            self.a.list_pairings()
        self.assertEqual(cm.exception.code, pj.E_CORRUPT)

    def test_private_storage_is_enforced_and_not_repaired(self):
        self.pair()
        jfile = self.A.journal_dir / "pairing.journal"
        os.chmod(jfile, 0o644)
        with self.assertRaises(JournalError) as cm:
            self.a.list_pairings()
        self.assertEqual(cm.exception.code, pj.E_STORAGE)
        self.assertEqual(oct(jfile.stat().st_mode & 0o777), "0o644")
        os.chmod(jfile, 0o600)
        os.chmod(self.A.journal_dir, 0o755)
        with self.assertRaises(JournalError):
            self.a.list_pairings()
        os.chmod(self.A.journal_dir, 0o700)
        moved = self.tmp / "real.journal"
        jfile.rename(moved)
        jfile.symlink_to(moved)
        with self.assertRaises(JournalError):
            self.a.list_pairings()


    def test_tampered_projection_is_never_trusted_nor_overwritten(self):
        self.pair()
        rogue = json.dumps([dict(self.A.peers()[0], peer_id="peer-evil")]).encode()
        self.A.peers_path.write_bytes(rogue)
        with self.assertRaises(JournalError) as cm:
            self.a.reconcile()
        self.assertEqual(cm.exception.code, pj.E_PROJECTION)
        self.assertEqual(self.A.peers_path.read_bytes(), rogue)
        with self.assertRaises(JournalError):
            self.a.list_pairings()
        # a manually added entry with no journal row fails closed too, even on a fresh installation
        self.B.peers_path.write_bytes(rogue)
        with self.assertRaises(JournalError):
            self.b.reconcile()

    def test_garbled_peers_json_fails_closed(self):
        self.pair()
        self.A.peers_path.write_bytes(b"{not json")
        with self.assertRaises(JournalError) as cm:
            self.a.reconcile()
        self.assertEqual(cm.exception.code, pj.E_PROJECTION)


class CrashRecoveryTest(Base):
    """Power loss at EVERY durable step of a full pairing (+ ack + unpair): re-open is consistent and the flow resumes."""

    def _scenario(self, crash_at):
        counter = [0]

        def fault(point):
            counter[0] += 1
            if counter[0] == crash_at:
                raise Crash(point)
        pj._FAULT = fault if crash_at else (lambda point: counter.__setitem__(0, counter[0] + 1))
        ctx = {}
        a, b = self.a, self.b

        def step(name, fn):
            nonlocal a, b
            try:
                ctx[name] = fn()
            except Crash:
                pj._FAULT = None
                a, b = self.A.service(self.clock), self.B.service(self.clock)   # fresh process
                for side, svc in ((self.A, a), (self.B, b)):
                    state = svc.journal.snapshot()                                # verifies the whole chain
                    self.assertGreaterEqual(state.seq, 0)
                    if side.journal_dir.exists():
                        svc.reconcile()                                           # finishes a pending publication
                        self.assertEqual(side.peers(), svc.journal.snapshot().projection_entries())
                if name == "offer":
                    sessions = a.journal.snapshot().sessions
                    if sessions:
                        sid, sess = next(iter(sessions.items()))
                        ctx[name] = {"id": sid, "code": sess["offer"], "expires_at": sess["expires_at"]}
                    else:
                        ctx[name] = a.start_offer()
                elif name == "unpair":
                    state = a.journal.snapshot()
                    if not any(l["protocol_state"] == "revoked" for l in state.links.values()):
                        ctx[name] = a.unpair(ctx["peer"])
                elif name == "unpair":
                    pass
                else:
                    ctx[name] = fn()
            return ctx.get(name)

        step("offer", lambda: a.start_offer())
        step("accepted", lambda: b.import_offer(ctx["offer"]["code"]))
        step("revealed", lambda: a.accept_offer(ctx["offer"]["id"], ctx["accepted"]["code"]))
        step("b_sas", lambda: b.reveal_import(ctx["accepted"]["id"], ctx["revealed"]["code"]))
        step("row_a", lambda: a.confirm_offer(ctx["offer"]["id"], ctx["b_sas"]["sas"]))
        step("row_b", lambda: b.confirm_import(ctx["accepted"]["id"], ctx["revealed"]["sas"]))
        step("ack", lambda: a.send_pair_ack(ctx["offer"]["id"], ps.link_sender(ps.dispatcher_for(b))))
        self.assertEqual(ctx["ack"]["state"], "acknowledged")
        ctx["peer"] = self.A.peers()[0]["peer_id"]
        step("unpair", lambda: a.unpair(ctx["peer"]))
        return counter[0], a, b

    def test_crash_at_every_durable_point_is_consistent_and_resumable(self):
        total, _a, _b = self._scenario(0)
        self.assertGreater(total, 40)
        shutil.rmtree(self.A.journal_dir); shutil.rmtree(self.B.journal_dir)
        for crash_at in range(1, total + 1):
            with self.subTest(crash_at=crash_at):
                for side in (self.A, self.B):
                    shutil.rmtree(side.journal_dir, ignore_errors=True)
                    side.peers_path.write_bytes(b"[]")
                self.a, self.b = self.A.service(self.clock), self.B.service(self.clock)
                _n, a, b = self._scenario(crash_at)
                for side, svc in ((self.A, a), (self.B, b)):
                    state = svc.journal.snapshot()
                    self.assertEqual(side.peers(), state.projection_entries())
                    self.assertEqual(state.published_revision, state.projection_revision)
                self.assertEqual(self.A.peers(), [])                           # ended unpaired
                gens = a.journal.snapshot().peer_gen
                self.assertEqual(list(gens.values()), [2])

    def test_crash_between_confirmed_and_publication_is_reconciled_by_the_next_request(self):
        offer = self.a.start_offer()
        accepted = self.b.import_offer(offer["code"])
        revealed = self.a.accept_offer(offer["id"], accepted["code"])
        b_sas = self.b.reveal_import(accepted["id"], revealed["code"])

        def fault(point):
            if point == "project:before_replace":
                raise Crash(point)
        pj._FAULT = fault
        with self.assertRaises(Crash):
            self.a.confirm_offer(offer["id"], b_sas["sas"])
        pj._FAULT = None
        state = self.a.journal.snapshot()
        self.assertEqual((state.projection_revision, state.published_revision), (1, 0))
        self.assertEqual(self.A.peers(), [])                               # confirmed in the journal, not yet published
        fresh = self.A.service(self.clock)
        self.assertEqual(fresh.list_pairings()[0]["state"], "confirmed")    # the next request reconciles
        self.assertEqual(self.A.peers()[0]["peer_id"], "peer-" + "b" * 32)
        self.assertEqual(fresh.journal.snapshot().published_revision, 1)

    def test_crash_after_rename_before_published_record_is_idempotent(self):
        offer = self.a.start_offer()
        accepted = self.b.import_offer(offer["code"])
        revealed = self.a.accept_offer(offer["id"], accepted["code"])
        b_sas = self.b.reveal_import(accepted["id"], revealed["code"])
        pj._FAULT = lambda point: (_ for _ in ()).throw(Crash(point)) if point == "project:after_replace" else None
        with self.assertRaises(Crash):
            self.a.confirm_offer(offer["id"], b_sas["sas"])
        pj._FAULT = None
        written = self.A.peers_path.read_bytes()
        self.A.service(self.clock).reconcile()
        self.assertEqual(self.A.peers_path.read_bytes(), written)
        self.assertEqual(self.A.service(self.clock).journal.snapshot().published_revision, 1)

    def test_failed_projection_does_not_lose_the_decision(self):
        offer = self.a.start_offer()
        accepted = self.b.import_offer(offer["code"])
        revealed = self.a.accept_offer(offer["id"], accepted["code"])
        b_sas = self.b.reveal_import(accepted["id"], revealed["code"])
        os.chmod(self.A.peers_path.parent, 0o500)
        try:
            row = self.a.confirm_offer(offer["id"], b_sas["sas"])
        finally:
            os.chmod(self.A.peers_path.parent, 0o700)
        self.assertEqual(row["state"], "confirmed")
        self.assertEqual(self.A.peers(), [])
        self.a.reconcile()
        self.assertEqual(len(self.A.peers()), 1)


_CHILD = r"""
import sys, time, json, os
sys.path.insert(0, %(server)r); sys.path.insert(0, %(tests)r)
import pairing_support as ps
from pathlib import Path
root = Path(%(root)r)
side = ps.Side.__new__(ps.Side)
from types import SimpleNamespace
side.config = SimpleNamespace(identity_dir=root/'a'/'identity', journal_dir=root/'a'/'journal', peers_path=root/'a'/'cfg'/'peers.json')
side.catalog = ps.make_catalog(ps.INSTALL_A)
svc = ps.PairingService(side.config, installation_id=ps.INSTALL_A, profile=ps.PROFILE, catalog_source=lambda: side.catalog)
go = root/'go'
while not go.exists(): time.sleep(0.002)
mode = sys.argv[1]
try:
    if mode == 'offer':
        svc.start_offer(); print('ok')
    else:
        svc.reconcile(); print('ok')
except Exception as exc:
    print(getattr(exc, 'code', type(exc).__name__))
"""


class ConcurrencyTest(Base):
    def _spawn(self, mode, n):
        code = _CHILD % {"server": SERVER_DIR, "tests": TESTS_DIR, "root": str(self.tmp)}
        procs = [subprocess.Popen([sys.executable, "-B", "-c", code, mode], stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True) for _ in range(n)]
        (self.tmp / "go").write_text("")
        outs = []
        for p in procs:
            out, err = p.communicate(timeout=60)
            self.assertEqual(p.returncode, 0, err)
            outs.append(out.strip())
        return outs

    def test_concurrent_writer_processes_keep_one_pending_attempt_and_an_intact_chain(self):
        outs = self._spawn("offer", 5)
        self.assertEqual(sorted(outs), ["ok"] + [psvc.PENDING] * 4)
        state = self.a.journal.snapshot()                                  # full verification of the chain
        self.assertEqual((state.seq, len(state.sessions)), (1, 1))

    def test_two_projectors_publish_a_pending_revision_exactly_once(self):
        offer = self.a.start_offer()
        accepted = self.b.import_offer(offer["code"])
        revealed = self.a.accept_offer(offer["id"], accepted["code"])
        b_sas = self.b.reveal_import(accepted["id"], revealed["code"])
        pj._FAULT = lambda point: (_ for _ in ()).throw(Crash(point)) if point == "project:before_replace" else None
        with self.assertRaises(Crash):
            self.a.confirm_offer(offer["id"], b_sas["sas"])
        pj._FAULT = None
        outs = self._spawn("reconcile", 4)
        self.assertEqual(outs, ["ok"] * 4)
        raw = self.A.journal_bytes().decode().splitlines()
        published = [json.loads(l) for l in raw if json.loads(l)["type"] == "published"]
        self.assertEqual([p["data"]["revision"] for p in published], [1])
        self.assertEqual(len(self.A.peers()), 1)
        runtime_config.build_peer_registry(self.A.peers_path)


class JournalLayoutTest(Base):
    def test_every_record_is_written_whole_then_fsynced(self):
        from unittest.mock import patch
        from invite_rooms import private_fs as fs
        events = []
        real_write, real_fsync = fs.write_all, fs.fsync
        with patch.object(fs, "write_all", side_effect=lambda fd, d: (events.append("w"), real_write(fd, d))[1]), \
                patch.object(fs, "fsync", side_effect=lambda fd: (events.append("f"), real_fsync(fd))[1]):
            self.a.start_offer()
            self.a.list_pairings()
        self.assertEqual(events.count("w"), 1)
        self.assertEqual(events[events.index("w") + 1], "f")                # the record is durable before we return
        raw = self.A.journal_bytes()
        self.assertEqual(raw.count(b"\n"), 1)                               # exactly one line per record

    def test_journal_is_private_append_only_and_secrets_stay_in_it(self):
        r = self.pair()
        names = sorted(p.name for p in self.A.journal_dir.iterdir())
        self.assertEqual(names, [".pairing.lock", "pairing.journal"])
        self.assertEqual(oct(self.A.journal_dir.stat().st_mode & 0o777), "0o700")
        for n in names:
            self.assertEqual(oct((self.A.journal_dir / n).stat().st_mode & 0o777), "0o600")
        raw = self.A.journal_bytes()
        records = [json.loads(line) for line in raw.decode().splitlines()]
        self.assertEqual([x["seq"] for x in records], list(range(1, len(records) + 1)))
        for earlier, later in zip(records, records[1:]):
            self.assertEqual(later["prev"], earlier["hash"])
        self.a.list_pairings()
        self.assertTrue(self.A.journal_bytes().startswith(raw))            # reads never rewrite
        rows = json.dumps(self.a.list_pairings())
        sess = self.a.journal.snapshot().sessions[r["a_id"]]
        for secret in (sess["r_a"], sess["nonce"], r["offer"]["code"], r["revealed"]["code"], r["revealed"]["sas"]):
            self.assertNotIn(secret, rows)                                  # redacted read model


if __name__ == "__main__":
    unittest.main()
