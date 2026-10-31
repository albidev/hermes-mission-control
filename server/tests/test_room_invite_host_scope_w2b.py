"""P3b-W2b-H2: peer-scoped invitation read model and post-wait final store admission.

Real scratch pairing journals + real loopback mTLS (the accepted H1 contextual seam) feed a REAL ``RoomInviteStore`` through
test-only contextual handlers.  Nothing here is a control handler (H3), touches native RPC, or creates a room.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pairing_support as ps
import test_control_listener_w2a as w2a
import test_control_peer_context_w2b as h1
from room_invite_store import InviteError, RoomInviteStore

OBS_DIR = os.environ.get("H2_OBS_DIR")
UNAVAILABLE = "invite_unavailable"


def setUpModule():
    h1.setUpModule()


def tearDownModule():
    h1.tearDownModule()


def observe(name, value):
    if OBS_DIR:
        with open(os.path.join(OBS_DIR, "observations.jsonl"), "a") as fh:
            fh.write(json.dumps({"name": name, "value": value}, sort_keys=True, default=str) + "\n")


def dump(path):
    with sqlite3.connect(path) as conn:
        return {t: conn.execute(f"SELECT * FROM {t} ORDER BY 1,2").fetchall()
                for t in ("lobbies", "invitations", "redemptions", "peer_links", "owner_scope")}


class H2Base(h1.H1Base):
    def setUp(self):
        super().setUp()
        self.path = self.tmp / "invites.sqlite"
        self.store = RoomInviteStore(self.path, clock=self.clock)
        self.store.bind_owner_scope(ps.INSTALL_A, "default")
        self.endpoint, self.generation = self.ack(self.a, self.b, self.paired_b)
        self.link_b = self.link(self.endpoint)
        self.peer_b = self.link_b["peer"]["peer_id"]
        self.binding = h1.binding_for(self.link_b)
        self.assertEqual(len(self.binding), 6)

    def roster(self, peers=None, **target_extra):
        """local + one peer-b member whose roster target is the journal's REAL peer id for B (optionally others)."""
        target = {"kind": "peer", "peer_id": self.peer_b, "installation_id": ps.INSTALL_B, "profile": "default"} | target_extra
        out = [{"member_id": "local-a", "profile": "default", "handle": "local"},
               {"member_id": "peer-b", "profile": "default", "handle": "peerb", "target": target}]
        for member_id, handle, peer_id, install in (peers or ()):
            out.append({"member_id": member_id, "profile": "default", "handle": handle,
                        "target": {"kind": "peer", "peer_id": peer_id, "installation_id": install, "profile": "default"}})
        return out

    def lobby(self, **kw):
        self.lob = self.store.create_lobby(name="  Known owners  ", roster=self.roster(**kw))
        self.lobby_id = self.lob["lobby_id"]
        return self.lob

    def issue(self, member="peer-b", ttl=3600):
        self.issued = self.store.issue_invite(self.lobby_id, member, invitee_label="Owner B secret label", ttl_seconds=ttl)
        return self.issued

    def handlers(self, calls=None, **kw):
        """Test-only contextual handlers: identity comes ONLY from the H1-issued context, never from the request."""
        store = self.store

        def guard(fn):
            def handler(context, request):
                if calls is not None:
                    calls.append((context.peer_id, request))
                try:
                    return fn(context, request)
                except InviteError as exc:
                    return {"error": str(exc)}
            return handler

        return {
            "preview": guard(lambda c, r: store.peer_preview(r["lobby_id"], r["invite_id"], peer_id=c.peer_id,
                                                              binding=c.binding_dict(), before_return=c.readmit)),
            "status": guard(lambda c, r: store.peer_scope_status(r["lobby_id"], r["invite_id"], peer_id=c.peer_id,
                                                                  binding=c.binding_dict(), before_return=c.readmit)),
        }

    def ask(self, item, op, *, client=None, endpoint=None, generation=None, lobby=None, invite=None):
        body = {"op": op, "lobby_id": lobby or self.lobby_id, "invite_id": invite or self.issued.invite_id}
        res = self.send(item, client=client or self.bb, server=self.ba, request=body,
                        endpoint_id=endpoint or self.endpoint, generation=generation or self.generation)
        self.assertEqual(res[0], "ok", res)
        return res[1], json.loads(res[2])

    def redeem_as_b(self, **kw):
        kw.setdefault("before_effect", lambda: True)
        return self.store.redeem(self.lobby_id, self.issued.invite_id, self.issued.secret, binding=self.binding,
                                 peer_id=self.peer_b, **kw)

    def invite_row(self):
        with sqlite3.connect(self.path) as conn:
            return conn.execute("SELECT * FROM invitations WHERE invite_id=?", (self.issued.invite_id,)).fetchone()


class SecretlessPeerPreviewTest(H2Base):
    def setUp(self):
        super().setUp()
        self.lobby()
        self.issue()

    def test_secretless_preview_returns_exact_name_and_expiry_without_mutation(self):
        before = dump(self.path)
        result = self.store.peer_preview(self.lobby_id, self.issued.invite_id, peer_id=self.peer_b, binding=self.binding,
                                         before_return=lambda: True)
        self.assertEqual(result, {"name": "Known owners", "expires_at": self.issued.expires_at})
        self.assertEqual(set(result), {"name", "expires_at"})
        self.assertEqual(dump(self.path), before)                       # state, secret hash, every row unchanged
        self.assertNotIn("Owner B secret label", json.dumps(result))
        self.assertEqual(self.invite_row()[5], "open")                  # nothing consumed
        # repeated (non consuming)
        self.assertEqual(self.store.peer_preview(self.lobby_id, self.issued.invite_id, peer_id=self.peer_b, binding=self.binding, before_return=lambda: True), result)

    def test_existing_secret_backed_preview_is_unchanged_and_still_validates_secret(self):
        self.assertEqual(self.store.preview(self.issued.invite_id, self.issued.secret),
                         {"name": "Known owners", "expires_at": self.issued.expires_at})
        for bad in ("x" * 43, "", self.issued.secret[:-1] + ("A" if self.issued.secret[-1] != "A" else "B")):
            with self.assertRaises(InviteError) as ctx:
                self.store.preview(self.issued.invite_id, bad)
            self.assertEqual(str(ctx.exception), UNAVAILABLE)

    def test_actual_mtls_contextual_handler_correct_peer_gets_metadata_second_paired_client_cannot(self):
        ec = self.ack(self.a, self.c, self.pair_c())
        calls = []
        item = self.listen_host(self.handlers(calls))
        status, body = self.ask(item, "preview")
        self.assertEqual((status, body), (200, {"name": "Known owners", "expires_at": self.issued.expires_at}))
        # second PAIRED client C supplies the same selector: denied via the real context, store gives the generic code
        status_c, body_c = self.ask(item, "preview", client=self.bc, endpoint=ec[0], generation=ec[1])
        self.assertEqual((status_c, body_c), (200, {"error": UNAVAILABLE}))
        self.assertEqual([c[0] for c in calls], [self.peer_b, self.link(ec[0])["peer"]["peer_id"]])
        self.assertNotIn("Known owners", json.dumps(body_c))
        observe("mtls_preview", {"b": body, "c": body_c})

    def test_same_install_and_profile_shaped_roster_with_wrong_peer_id_is_denied(self):
        ec = self.ack(self.a, self.c, self.pair_c())
        # roster member whose target has B's install/profile but a DIFFERENT peer id: B's authenticated peer must not match it
        store2 = RoomInviteStore(self.tmp / "other.sqlite", clock=self.clock)
        store2.bind_owner_scope(ps.INSTALL_A, "default")
        lob = store2.create_lobby(name="Same shape", roster=self.roster()[:1] + [
            {"member_id": "peer-b", "profile": "default", "handle": "peerb",
             "target": {"kind": "peer", "peer_id": "peer-" + "9" * 32, "installation_id": ps.INSTALL_B, "profile": "default"}}])
        inv = store2.issue_invite(lob["lobby_id"], "peer-b", invitee_label="x")
        with self.assertRaises(InviteError) as ctx:
            store2.peer_preview(lob["lobby_id"], inv.invite_id, peer_id=self.peer_b, binding=self.binding, before_return=lambda: True)
        self.assertEqual(str(ctx.exception), UNAVAILABLE)

    def test_every_negative_case_is_the_same_generic_unavailable(self):
        lobby_id, invite_id = self.lobby_id, self.issued.invite_id
        other = self.store.create_lobby(name="Other", roster=self.roster())
        other_inv = self.store.issue_invite(other["lobby_id"], "peer-b", invitee_label="o")
        wrong = lambda **kw: dict(self.binding, **kw)  # noqa: E731
        cases = {
            "wrong_lobby": (other["lobby_id"], invite_id, self.peer_b, self.binding),
            "unknown_lobby": ("lobby-" + "0" * 32, invite_id, self.peer_b, self.binding),
            "wrong_invite": (lobby_id, "invite-" + "0" * 32, self.peer_b, self.binding),
            "foreign_invite": (lobby_id, other_inv.invite_id, self.peer_b, self.binding),
            "wrong_peer": (lobby_id, invite_id, "peer-" + "e" * 32, self.binding),
            "install": (lobby_id, invite_id, self.peer_b, wrong(target_install_id=ps.INSTALL_C)),
            "profile": (lobby_id, invite_id, self.peer_b, wrong(target_profile="other")),
            "four_field_binding": (lobby_id, invite_id, self.peer_b, {k: self.binding[k] for k in (
                "endpoint_id", "fingerprint", "target_install_id", "target_profile")}),
        }
        before = dump(self.path)
        seen = {}
        for name, (lid, iid, pid, bind) in cases.items():
            with self.assertRaises(InviteError, msg=name) as ctx:
                self.store.peer_preview(lid, iid, peer_id=pid, binding=bind, before_return=lambda: True)
            seen[name] = str(ctx.exception)
        self.assertEqual(set(seen.values()), {UNAVAILABLE}, seen)
        self.assertEqual(dump(self.path), before)

    def test_optional_capability_digest_must_match_binding_catalog(self):
        self.lobby(capability_digest=self.binding["catalog_digest"])
        self.issue()
        self.store.peer_preview(self.lobby_id, self.issued.invite_id, peer_id=self.peer_b, binding=self.binding, before_return=lambda: True)
        self.lobby(capability_digest="a" * 64)
        self.issue()
        with self.assertRaises(InviteError) as ctx:
            self.store.peer_preview(self.lobby_id, self.issued.invite_id, peer_id=self.peer_b, binding=self.binding, before_return=lambda: True)
        self.assertEqual(str(ctx.exception), UNAVAILABLE)

    def test_expired_revoked_used_and_foreign_owner_scope_are_unavailable(self):
        iid = self.issued.invite_id
        ask = lambda: self.store.peer_preview(self.lobby_id, iid, peer_id=self.peer_b, binding=self.binding, before_return=lambda: True)  # noqa: E731
        self.clock.advance(3601)
        with self.assertRaises(InviteError) as ctx:
            ask()
        self.assertEqual(str(ctx.exception), UNAVAILABLE)
        self.clock.advance(-3601)
        ask()
        self.store.revoke_invite(self.lobby_id, iid)
        with self.assertRaises(InviteError) as ctx:
            ask()
        self.assertEqual(str(ctx.exception), UNAVAILABLE)
        self.issue()
        iid = self.issued.invite_id
        self.redeem_as_b()
        with self.assertRaises(InviteError) as ctx:
            ask()
        self.assertEqual(str(ctx.exception), UNAVAILABLE)
        # unbound owner scope (fresh store) -> unavailable, never silently trusted
        bare = RoomInviteStore(self.tmp / "bare.sqlite", clock=self.clock)
        lob = bare.create_lobby(name="n", roster=self.roster())
        inv = bare.issue_invite(lob["lobby_id"], "peer-b", invitee_label="x")
        with self.assertRaises(InviteError) as ctx:
            bare.peer_preview(lob["lobby_id"], inv.invite_id, peer_id=self.peer_b, binding=self.binding, before_return=lambda: True)
        self.assertEqual(str(ctx.exception), UNAVAILABLE)

    def test_before_return_runs_after_snapshot_and_false_or_raising_is_generic_unavailable(self):
        seen = []

        def good():
            # the read lock/connection is gone: a writer can proceed while the callback runs
            with sqlite3.connect(self.path, timeout=1) as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.rollback()
            seen.append(1)
            return True
        out = self.store.peer_preview(self.lobby_id, self.issued.invite_id, peer_id=self.peer_b, binding=self.binding,
                                      before_return=good)
        self.assertEqual((seen, out["name"]), ([1], "Known owners"))
        for cb in (lambda: False, lambda: None, lambda: 1, lambda: (_ for _ in ()).throw(RuntimeError("boom"))):
            with self.assertRaises(InviteError) as ctx:
                self.store.peer_preview(self.lobby_id, self.issued.invite_id, peer_id=self.peer_b, binding=self.binding,
                                        before_return=cb)
            self.assertEqual(str(ctx.exception), UNAVAILABLE)

    def test_unpair_between_snapshot_and_return_withholds_metadata_over_real_context(self):
        holder = {}
        gate = threading.Event()

        def preview(context, request):
            holder["ctx"] = context
            gate.wait(5)
            try:
                return self.store.peer_preview(request["lobby_id"], request["invite_id"], peer_id=context.peer_id,
                                               binding=context.binding_dict(), before_return=context.readmit)
            except InviteError as exc:
                return {"error": str(exc)}
        item = self.listen_host({"preview": preview})
        out = {}
        body = {"op": "preview", "lobby_id": self.lobby_id, "invite_id": self.issued.invite_id}
        t = threading.Thread(target=lambda: out.update(r=self.send(item, client=self.bb, server=self.ba, request=body,
                                                                   endpoint_id=self.endpoint, generation=self.generation)))
        t.start()
        try:
            time.sleep(0.4)
            self.a.unpair(self.peer_b)
            gate.set()
        finally:
            gate.set()
            t.join(10)
        self.assertFalse(t.is_alive())
        self.assertFalse(holder["ctx"].readmit())
        self.assertIn("r", out)
        self.assertNotIn(b"Known owners", out["r"][2] if out["r"][0] == "ok" else b"")


class StatusScopeTest(H2Base):
    def setUp(self):
        super().setUp()
        self.lobby()
        self.issue()

    def status(self, **kw):
        kw.setdefault("before_return", lambda: True)
        return self.store.peer_scope_status(self.lobby_id, self.issued.invite_id, peer_id=self.peer_b,
                                            binding=self.binding, **kw)

    def test_status_progression_and_exact_scope(self):
        self.redeem_as_b()
        out = self.status()
        self.assertEqual(set(out), {"status", "scope"})
        self.assertEqual(out["status"], "awaiting_host_approval")
        scope = out["scope"]
        self.assertEqual(set(scope), {"host_id", "room_id", "member_id", "lobby_id", "invite_id", "binding"})
        self.assertEqual(scope, {"host_id": ps.INSTALL_A, "room_id": self.lob["planned_room_id"], "member_id": "peer-b",
                                 "lobby_id": self.lobby_id, "invite_id": self.issued.invite_id, "binding": self.binding})
        self.assertIsNone(self.store.get_lobby(self.lobby_id)["room_id"])          # native room id is null; never used
        self.assertNotEqual(scope["room_id"], None)
        self.store.decide_host(self.lobby_id, self.issued.invite_id, binding=self.binding, approved=True)
        self.assertEqual(self.status()["status"], "consent_required")
        self.store.consent_joining(self.lobby_id, self.issued.invite_id, binding=self.binding, consented=True, peer_id=self.peer_b, before_effect=lambda: True)
        self.assertEqual(self.status()["status"], "ready")
        observe("status_scope", out)

    def test_denied_and_refused_use_store_vocabulary(self):
        self.redeem_as_b()
        self.store.decide_host(self.lobby_id, self.issued.invite_id, binding=self.binding, approved=False)
        self.assertEqual(self.status()["status"], "denied")
        self.issue("peer-b")  # member_already_redeemed? denied redemption allows re-issue
        self.redeem_as_b()
        self.store.decide_host(self.lobby_id, self.issued.invite_id, binding=self.binding, approved=True)
        self.store.consent_joining(self.lobby_id, self.issued.invite_id, binding=self.binding, consented=False, peer_id=self.peer_b, before_effect=lambda: True)
        self.assertEqual(self.status()["status"], "refused")

    def test_ready_means_consents_only_on_open_sealed_and_created_lobbies(self):
        self.redeem_as_b()
        self.store.decide_host(self.lobby_id, self.issued.invite_id, binding=self.binding, approved=True)
        self.store.consent_joining(self.lobby_id, self.issued.invite_id, binding=self.binding, consented=True, peer_id=self.peer_b, before_effect=lambda: True)
        self.assertEqual(self.status()["status"], "ready")
        # a roster with only B as remote peer can be sealed
        self.assertEqual(self.store.seal_lobby(self.lobby_id)["state"], "sealed")
        self.assertEqual(self.status()["status"], "ready")
        spec = {"room_id": self.lob["planned_room_id"]}
        self.store.pin_native_spec(self.lobby_id, spec)
        self.store.mark_created(self.lobby_id, self.lob["planned_room_id"])
        out = self.status()
        self.assertEqual(out["status"], "ready")                                   # never "active"/"created"
        self.assertEqual(out["scope"]["room_id"], self.lob["planned_room_id"])
        self.assertNotIn("state", json.dumps(out))

    def test_unredeemed_and_every_mismatch_is_the_same_generic_unavailable(self):
        with self.assertRaises(InviteError) as ctx:                                # nothing redeemed yet
            self.status()
        self.assertEqual(str(ctx.exception), UNAVAILABLE)
        self.redeem_as_b()
        other = self.store.create_lobby(name="Other", roster=self.roster())
        wrong = lambda **kw: dict(self.binding, **kw)  # noqa: E731
        iid = self.issued.invite_id
        cases = {"lobby": (other["lobby_id"], iid, self.peer_b, self.binding),
                 "invite": (self.lobby_id, "invite-" + "0" * 32, self.peer_b, self.binding),
                 "peer": (self.lobby_id, iid, "peer-" + "e" * 32, self.binding),
                 **{k: (self.lobby_id, iid, self.peer_b, wrong(**{k: v})) for k, v in
                    {"endpoint_id": "endpoint-x", "fingerprint": "f" * 64, "target_install_id": ps.INSTALL_C,
                     "target_profile": "other", "catalog_digest": "c" * 64, "policy_digest": "d" * 64}.items()}}
        before = dump(self.path)
        seen = {}
        for name, (lid, i, pid, bind) in cases.items():
            with self.assertRaises(InviteError, msg=name) as ctx:
                self.store.peer_scope_status(lid, i, peer_id=pid, binding=bind, before_return=lambda: True)
            seen[name] = str(ctx.exception)
        self.assertEqual(set(seen.values()), {UNAVAILABLE}, seen)
        self.assertEqual(dump(self.path), before)

    def test_status_via_actual_mtls_does_not_cross_peers(self):
        self.redeem_as_b()
        ec = self.ack(self.a, self.c, self.pair_c())
        item = self.listen_host(self.handlers())
        status, body = self.ask(item, "status")
        self.assertEqual((status, body["status"]), (200, "awaiting_host_approval"))
        self.assertEqual(body["scope"]["binding"], self.binding)
        status_c, body_c = self.ask(item, "status", client=self.bc, endpoint=ec[0], generation=ec[1])
        self.assertEqual((status_c, body_c), (200, {"error": UNAVAILABLE}))

    def test_status_does_not_mutate_and_does_not_need_secret_columns(self):
        self.redeem_as_b()
        before = dump(self.path)
        self.status()
        self.assertEqual(dump(self.path), before)
        self.assertNotIn(self.issued.secret, json.dumps(self.status()))
        self.assertNotIn(hashlib.sha256(self.issued.secret.encode()).hexdigest(), json.dumps(self.status()))


class PeerScopedRedeemTest(H2Base):
    def setUp(self):
        super().setUp()
        self.lobby(peers=[("peer-c", "peerc", "peer-" + "c" * 32, ps.INSTALL_C)])
        self.issue()

    def test_correct_peer_redeem_persists_exact_six_binding_fields_and_pending_decisions(self):
        out = self.redeem_as_b()
        with sqlite3.connect(self.path) as conn:
            rows = conn.execute("SELECT endpoint_id,fingerprint,target_install_id,target_profile,catalog_digest,policy_digest,"
                                "host_decision,joining_consent FROM redemptions").fetchall()
        self.assertEqual(rows, [tuple(self.binding[k] for k in ("endpoint_id", "fingerprint", "target_install_id", "target_profile",
                                                                "catalog_digest", "policy_digest")) + ("pending", "pending")])
        self.assertEqual(out["status"], "awaiting_host_approval")
        self.assertEqual(self.invite_row()[5], "redeemed")

    def test_cross_peer_same_install_and_binding_substitution_yield_zero_writes(self):
        before = dump(self.path)
        wrong = lambda **kw: dict(self.binding, **kw)  # noqa: E731
        cases = {
            "other_peer_same_install": dict(peer_id="peer-" + "c" * 32),             # C's real peer id, B's install binding
            "unknown_peer": dict(peer_id="peer-" + "e" * 32),
            "install": dict(binding=wrong(target_install_id=ps.INSTALL_C)),
            "profile": dict(binding=wrong(target_profile="other")),
            "four_field_binding": dict(binding={k: self.binding[k] for k in ("endpoint_id", "fingerprint", "target_install_id", "target_profile")}),
        }
        for name, over in cases.items():
            kw = dict(binding=self.binding, peer_id=self.peer_b, before_effect=lambda: True) | over
            with self.assertRaises(InviteError, msg=name) as ctx:
                self.store.redeem(self.lobby_id, self.issued.invite_id, self.issued.secret, **kw)
            self.assertEqual(str(ctx.exception), UNAVAILABLE, name)
            self.assertEqual(dump(self.path), before, name)

    def test_invalid_secret_expiry_revoke_and_replay_yield_zero_writes(self):
        before = dump(self.path)
        with self.assertRaises(InviteError) as ctx:
            self.store.redeem(self.lobby_id, self.issued.invite_id, "A" * 43, binding=self.binding,
                              peer_id=self.peer_b, before_effect=lambda: True)
        self.assertEqual(str(ctx.exception), UNAVAILABLE)
        self.assertEqual(dump(self.path), before)
        self.clock.advance(3601)
        with self.assertRaises(InviteError):
            self.redeem_as_b()
        self.assertEqual(dump(self.path), before)
        self.clock.advance(-3601)
        self.store.revoke_invite(self.lobby_id, self.issued.invite_id)
        revoked = dump(self.path)
        with self.assertRaises(InviteError):
            self.redeem_as_b()
        self.assertEqual(dump(self.path), revoked)
        self.issue()
        self.redeem_as_b()
        done = dump(self.path)
        with self.assertRaises(InviteError) as ctx:                                  # replay (lost reply): no redelivery
            self.redeem_as_b()
        self.assertEqual(str(ctx.exception), UNAVAILABLE)
        self.assertEqual(dump(self.path), done)

    def test_after_redeem_any_changed_binding_field_is_refused_by_consent_and_status_with_zero_writes(self):
        # endpoint/fingerprint/policy are journal-authoritative: the roster stores none, so redeem RECORDS them and every
        # later consent/status read must match all six stored fields exactly.
        self.redeem_as_b()
        self.store.decide_host(self.lobby_id, self.issued.invite_id, binding=self.binding, approved=True)
        before = dump(self.path)
        for key, value in (("endpoint_id", "endpoint-other"), ("fingerprint", "f" * 64), ("catalog_digest", "c" * 64),
                           ("policy_digest", "d" * 64), ("target_install_id", ps.INSTALL_C), ("target_profile", "other")):
            bad = dict(self.binding, **{key: value})
            with self.assertRaises(InviteError, msg=key) as ctx:
                self.store.consent_joining(self.lobby_id, self.issued.invite_id, binding=bad, consented=True,
                                           peer_id=self.peer_b, before_effect=lambda: True)
            self.assertEqual(str(ctx.exception), "fingerprint_mismatch", key)
            with self.assertRaises(InviteError, msg=key) as ctx:
                self.store.peer_scope_status(self.lobby_id, self.issued.invite_id, peer_id=self.peer_b, binding=bad,
                                             before_return=lambda: True)
            self.assertEqual(str(ctx.exception), UNAVAILABLE, key)
            self.assertEqual(dump(self.path), before, key)

    def test_roster_capability_digest_is_enforced_for_peer_scoped_redeem(self):
        self.lobby(capability_digest="a" * 64)
        self.issue()
        before = dump(self.path)
        with self.assertRaises(InviteError) as ctx:
            self.redeem_as_b()
        self.assertEqual(str(ctx.exception), UNAVAILABLE)
        self.assertEqual(dump(self.path), before)

    def test_legacy_callers_without_peer_id_or_callback_are_unchanged(self):
        legacy = {k: self.binding[k] for k in ("endpoint_id", "fingerprint", "target_install_id", "target_profile")}
        out = self.store.redeem(self.lobby_id, self.issued.invite_id, self.issued.secret, binding=legacy)
        self.assertEqual(out["status"], "awaiting_host_approval")
        self.store.decide_host(self.lobby_id, self.issued.invite_id, binding=legacy, approved=True)
        self.assertEqual(self.store.consent_joining(self.lobby_id, self.issued.invite_id, binding=legacy, consented=True)["status"], "ready")


class FinalAdmissionTest(H2Base):
    """The callback is the last gate: it runs after SQLite acquisition, exactly once, with no journal lock held."""

    def setUp(self):
        super().setUp()
        self.lobby()
        self.issue()

    def hold_sqlite(self):
        holder = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        holder.execute("BEGIN IMMEDIATE")
        self.addCleanup(self._release, holder)
        return holder

    @staticmethod
    def _release(holder):
        try:
            holder.execute("ROLLBACK")
        except Exception:
            pass
        holder.close()

    def run_blocked(self, op, ctx, *, unpair):
        """Run ``op(before_effect)`` in a worker while a second connection holds BEGIN IMMEDIATE; mutate the journal
        while the worker is blocked; then release SQLite.  Returns (result, callback_log)."""
        holder = self.hold_sqlite()
        log, out = [], {}

        def before_effect():
            log.append(("callback", time.monotonic(), ctx.readmit()))
            return log[-1][2]

        def work():
            try:
                out["value"] = op(before_effect)
            except BaseException as exc:                                              # noqa: BLE001
                out["error"] = exc
        t = threading.Thread(target=work, name="h2-blocked-store-worker")
        t.start()
        try:
            time.sleep(0.4)
            self.assertTrue(t.is_alive(), "worker must be blocked on SQLite")
            self.assertEqual(log, [], "callback must NOT run before the write lock is acquired")
            unpair()
            self._release(holder)
            t.join(15)
        finally:
            self._release(holder)
            t.join(15)
        self.assertFalse(t.is_alive())
        return out, log

    def test_unpair_while_redeem_is_blocked_on_sqlite_refuses_before_any_insert(self):
        item_ctx = self.fresh_context()
        before = dump(self.path)
        out, log = self.run_blocked(lambda cb: self.redeem_as_b(before_effect=cb), item_ctx,
                                    unpair=lambda: self.a.unpair(self.peer_b))
        self.assertIsInstance(out.get("error"), InviteError, out)
        self.assertEqual(str(out["error"]), UNAVAILABLE)
        self.assertEqual(len(log), 1)
        self.assertFalse(log[0][2])
        self.assertEqual(dump(self.path), before)
        observe("blocked_redeem", {"callback_calls": len(log), "readmit": log[0][2], "db_unchanged": True})

    def fresh_context(self):
        box = {}
        def status(context, request):
            box["ctx"] = context
            return {"ok": True}
        item = self.listen_host({"status": status})
        self.assertEqual(self.ask(item, "status")[0], 200)
        return box["ctx"]

    def test_unpair_while_consent_is_blocked_on_sqlite_refuses_before_update(self):
        self.redeem_as_b()
        self.store.decide_host(self.lobby_id, self.issued.invite_id, binding=self.binding, approved=True)
        ctx = self.fresh_context()
        before = dump(self.path)
        out, log = self.run_blocked(lambda cb: self.store.consent_joining(
            self.lobby_id, self.issued.invite_id, binding=self.binding, consented=True, peer_id=self.peer_b, before_effect=cb),
            ctx, unpair=lambda: self.a.unpair(self.peer_b))
        self.assertIsInstance(out.get("error"), InviteError, out)
        self.assertEqual(str(out["error"]), UNAVAILABLE)
        self.assertEqual(len(log), 1)
        self.assertEqual(dump(self.path), before)
        self.assertEqual(self.store.get_redemption(self.lobby_id, self.issued.invite_id)["joining_consent"], "pending")

    def test_control_admitted_then_unpaired_after_callback_may_commit_without_false_cancellation(self):
        ctx = self.fresh_context()
        log = []

        def before_effect():
            ok = ctx.readmit()
            log.append(ok)
            self.a.unpair(self.peer_b)           # unpair AFTER the final callback returned its admission: no lock is held
            return ok
        # unpair inside the callback would run while the store holds only its own SQLite lock (never the journal lock)
        out = self.store.redeem(self.lobby_id, self.issued.invite_id, self.issued.secret, binding=self.binding,
                                peer_id=self.peer_b, before_effect=before_effect)
        self.assertEqual(log, [True])
        self.assertEqual(out["status"], "awaiting_host_approval")                    # committed: already-admitted write stands
        self.assertFalse(ctx.readmit())

    def test_callback_exactly_once_none_preserves_legacy_and_false_or_raising_aborts_with_zero_mutation(self):
        calls = []
        self.redeem_as_b(before_effect=lambda: calls.append(1) or True)
        self.assertEqual(calls, [1])
        # failure to validate never reaches the callback
        for kind, cb in (("false", lambda: False), ("none", lambda: None), ("raise", lambda: (_ for _ in ()).throw(RuntimeError("x")))):
            self.lobby()
            self.issue()
            snap = dump(self.path)
            with self.assertRaises(InviteError, msg=kind) as ctx:
                self.redeem_as_b(before_effect=cb)
            self.assertEqual(str(ctx.exception), UNAVAILABLE)
            self.assertEqual(dump(self.path), snap, kind)
        # invalid secret: callback never called
        self.lobby()
        self.issue()
        never = []
        with self.assertRaises(InviteError):
            self.store.redeem(self.lobby_id, self.issued.invite_id, "B" * 43, binding=self.binding, peer_id=self.peer_b,
                              before_effect=lambda: never.append(1) or True)
        self.assertEqual(never, [])

    def test_consent_callback_boundary_idempotency_and_final_state(self):
        self.redeem_as_b()
        # host approval is required first: callback is not reached
        never = []
        with self.assertRaises(InviteError) as ctx:
            self.store.consent_joining(self.lobby_id, self.issued.invite_id, binding=self.binding, consented=True,
                                       peer_id=self.peer_b, before_effect=lambda: never.append(1) or True)
        self.assertEqual(str(ctx.exception), "host_approval_required")
        self.assertEqual(never, [])
        # decide_host is owner-only and never consults a peer callback
        self.store.decide_host(self.lobby_id, self.issued.invite_id, binding=self.binding, approved=True)
        calls = []
        out = self.store.consent_joining(self.lobby_id, self.issued.invite_id, binding=self.binding, consented=True,
                                         peer_id=self.peer_b, before_effect=lambda: calls.append(1) or True)
        self.assertEqual((calls, out["status"]), ([1], "ready"))
        # already-given exact consent: read-only, callback not needed
        snap = dump(self.path)
        again = self.store.consent_joining(self.lobby_id, self.issued.invite_id, binding=self.binding, consented=True,
                                           peer_id=self.peer_b, before_effect=lambda: calls.append(2) or True)
        self.assertEqual((calls, again["status"], dump(self.path)), ([1], "ready", snap))
        with self.assertRaises(InviteError) as ctx:                                  # opposite value stays final
            self.store.consent_joining(self.lobby_id, self.issued.invite_id, binding=self.binding, consented=False,
                                       peer_id=self.peer_b, before_effect=lambda: True)
        self.assertEqual(str(ctx.exception), "decision_final")

    def test_consent_peer_id_must_match_roster_peer(self):
        self.redeem_as_b()
        self.store.decide_host(self.lobby_id, self.issued.invite_id, binding=self.binding, approved=True)
        before = dump(self.path)
        with self.assertRaises(InviteError) as ctx:
            self.store.consent_joining(self.lobby_id, self.issued.invite_id, binding=self.binding, consented=True,
                                       peer_id="peer-" + "e" * 32, before_effect=lambda: True)
        self.assertEqual(str(ctx.exception), "fingerprint_mismatch")
        self.assertEqual(dump(self.path), before)

    def test_no_journal_lock_is_held_while_callback_runs(self):
        ctx = self.fresh_context()
        seen = {}

        def before_effect():
            t0 = time.monotonic()
            self.a.unpair(self.peer_b)          # takes the journal EXCLUSIVE lock; would deadlock if the store held it
            seen["dt"] = time.monotonic() - t0
            return True
        self.store.redeem(self.lobby_id, self.issued.invite_id, self.issued.secret, binding=self.binding,
                          peer_id=self.peer_b, before_effect=before_effect)
        self.assertLess(seen["dt"], 3)
        self.assertFalse(ctx.readmit())
        self.assertEqual([t.name for t in threading.enumerate() if t.name == "h2-blocked-store-worker"], [])

    def test_peer_aware_paths_fail_closed_when_final_callbacks_are_omitted(self):
        before = dump(self.path)
        with self.assertRaises(InviteError) as ctx:
            self.store.peer_preview(self.lobby_id, self.issued.invite_id, peer_id=self.peer_b, binding=self.binding)
        self.assertEqual(str(ctx.exception), UNAVAILABLE)
        with self.assertRaises(InviteError) as ctx:
            self.store.peer_scope_status(self.lobby_id, self.issued.invite_id, peer_id=self.peer_b,
                                         binding=self.binding)
        self.assertEqual(str(ctx.exception), UNAVAILABLE)
        with self.assertRaises(InviteError) as ctx:
            self.store.redeem(self.lobby_id, self.issued.invite_id, self.issued.secret,
                              binding=self.binding, peer_id=self.peer_b)
        self.assertEqual(str(ctx.exception), UNAVAILABLE)
        self.store.redeem(self.lobby_id, self.issued.invite_id, self.issued.secret,
                          binding=self.binding, peer_id=self.peer_b, before_effect=lambda: True)
        self.store.decide_host(self.lobby_id, self.issued.invite_id, binding=self.binding, approved=True)
        before_consent = dump(self.path)
        with self.assertRaises(InviteError) as ctx:
            self.store.consent_joining(self.lobby_id, self.issued.invite_id, binding=self.binding,
                                       consented=True, peer_id=self.peer_b)
        self.assertEqual(str(ctx.exception), UNAVAILABLE)
        self.assertEqual(dump(self.path), before_consent)
        self.assertNotEqual(dump(self.path), before)  # redeem with an explicit admission was the only write


if __name__ == "__main__":
    unittest.main()
