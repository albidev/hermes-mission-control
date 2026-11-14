from __future__ import annotations

import base64
import json
import os
import unittest
from unittest.mock import patch

from test_room_invite_host_scope_w2b import H2Base, dump
import test_control_peer_context_w2b as h1
import pairing_support as ps
from invite_rooms import consent
from invite_rooms.control.dispatcher import ControlError
from invite_rooms.host_control import build_host_handlers
from invite_rooms.control import listener as listener_module
from invite_rooms import runtime_config, runtime_joining as rj
from invite_rooms.joining import JoiningError
from invite_rooms.service import OwnerContext
from room_invite_store import RoomInviteStore


def setUpModule():
    h1.setUpModule()


def tearDownModule():
    h1.tearDownModule()


class ProductionJoiningBase(h1.H1Base):
    """Real runtime owner B -> pinned mTLS -> injected H3 host A."""

    def setUp(self):
        super().setUp()
        self.endpoint, self.generation = self.ack(self.a, self.b, self.paired_b)
        self.link_b = self.link(self.endpoint)
        self.host_peer_id = self.b.journal.snapshot().links[self.endpoint]["peer"]["peer_id"]
        self.binding = h1.binding_for(self.link_b)
        self.host_store = RoomInviteStore(self.tmp / "production-host.sqlite", clock=self.clock)
        self.host_store.bind_owner_scope(ps.INSTALL_A, "default")
        target_peer = self.link_b["peer"]["peer_id"]
        roster = [
            {"member_id": "local-a", "profile": "default", "handle": "local"},
            {"member_id": "peer-b", "profile": "default", "handle": "peerb",
             "target": {"kind": "peer", "peer_id": target_peer, "installation_id": ps.INSTALL_B,
                         "profile": "default"}},
        ]
        self.host_lobby = self.host_store.create_lobby(name="  Known owners  ", roster=roster)
        self.lobby_id = self.host_lobby["lobby_id"]
        self.issued = self.host_store.issue_invite(self.lobby_id, "peer-b", invitee_label="B")
        self.host_handlers = build_host_handlers(self.host_store, OwnerContext(ps.INSTALL_A, "default"))
        self.host_calls = {"preview": 0, "redeem": 0, "status": 0, "consent": 0}
        self.host_results = []
        for key, counter in (("preview", "preview"), ("redeem", "redeem"),
                             ("status", "status"), ("joining_consent", "consent")):
            real = self.host_handlers[key]
            def count(context, request, *, real=real, counter=counter):
                self.host_calls[counter] += 1
                result = real(context, request)
                self.host_results.append((counter, result))
                return result
            self.host_handlers[key] = count
        self.host_listener = self.listen_host(self.host_handlers)
        rj.configure_joining_control(rj.default_joining_control)
        self.addCleanup(rj.configure_joining_control)
        self.addCleanup(rj.configure_joining_transport)
        self.cfg_path = self.B.root / "cfg" / "runtime.json"
        self.cfg = {
            "store_path": str(self.B.root / "invites" / "unused.sqlite"),
            "installation_id": ps.INSTALL_B, "profile": "default",
            "peer_registry_path": str(self.B.peers_path),
            "pairing": {"identity_dir": str(self.B.identity_dir), "journal_dir": str(self.B.journal_dir),
                        "peers_path": str(self.B.peers_path)},
            "joining": {"store_dir": str(self.B.root / "joining"), "identity_dir": str(self.B.identity_dir),
                        "hosts_path": str(self.B.root / "cfg" / "joining-hosts.json"),
                        "origins": {self.host_peer_id: "https://host.example"}, "host_label": "host", "own_bot_id": "self",
                        "control_endpoints": {self.host_peer_id: {"host": "127.0.0.1", "port": self.host_listener.address[1]}},
                    },
        }
        self.cfg_path.write_text(json.dumps(self.cfg))
        self.cfg_path.chmod(0o600)
        self.config_env = patch.dict(os.environ, {"MISSION_CONTROL_ROOM_INVITES_CONFIG": str(self.cfg_path)})
        self.config_env.start()
        self.addCleanup(self.config_env.stop)
        loaded = runtime_config.load_service()
        self.assertIsNotNone(loaded)
        self.owner = rj.load_owner(loaded[2])
        self.assertIsNotNone(self.owner)
        self.addCleanup(self.owner.close)
        invitation = f"https://host.example/invite/{self.lobby_id}/{self.issued.invite_id}#{self.issued.secret}"
        self.preview = self.owner.execute("preview", body={"code": invitation})
        self.import_id = self.preview["import_id"]

    def restart_owner(self):
        self.owner.close()
        loaded = runtime_config.load_service()
        self.owner = rj.load_owner(loaded[2])
        self.assertIsNotNone(self.owner)
        return self.owner


class ProductionJoiningLossProof(ProductionJoiningBase):
    def test_production_execute_full_sequence_and_lost_redeem_is_durable_no_resend(self):
        metadata = self.owner.execute("metadata", self.import_id)
        self.assertEqual(metadata["metadata"]["name"], "Known owners")
        before = dump(self.host_store.path)
        real_send = listener_module.send_message
        dropped = []

        def drop_response(frames, payload):
            if not dropped:
                dropped.append(1)
                raise OSError("dropped after redeem commit")
            return real_send(frames, payload)

        with patch.object(listener_module, "send_message", drop_response):
            with self.assertRaises(JoiningError) as error:
                self.owner.execute("redeem", self.import_id)
        self.assertEqual(str(error.exception), "redeem_ambiguous")
        self.assertEqual(self.owner.store.get_import(self.import_id)["state"], "redeem_ambiguous")
        after = dump(self.host_store.path)
        self.assertNotEqual(after, before)
        self.assertEqual(self.host_calls["redeem"], 1)

        owner = self.restart_owner()
        with self.assertRaises(JoiningError) as error:
            owner.execute("redeem", self.import_id)
        self.assertEqual(str(error.exception), "redeem_ambiguous")
        self.assertEqual(self.host_calls["redeem"], 1)
        self.assertEqual(dump(self.host_store.path), after)

    def test_production_execute_consent_loss_reuses_durable_receipt_after_restart(self):
        self.owner.execute("metadata", self.import_id)
        self.owner.execute("redeem", self.import_id)
        self.host_store.decide_host(self.lobby_id, self.issued.invite_id, binding=self.binding, approved=True)
        scope = self.host_store.peer_scope_status(self.lobby_id, self.issued.invite_id, peer_id=self.link_b["peer"]["peer_id"],
                                                  binding=self.binding, before_return=lambda: True)["scope"]
        self.owner.execute("scope", self.import_id, {"room_id": scope["room_id"], "member_id": scope["member_id"]})
        self.assertEqual(self.owner.store.confirmed_scope(self.import_id), {
            "room_id": scope["room_id"], "member_id": scope["member_id"], "lobby_id": self.lobby_id,
            "host_id": ps.INSTALL_A, "binding": self.owner.self_binding,
        })
        self.assertEqual(dict(self.owner.store.confirmed_scope(self.import_id), invite_id=self.issued.invite_id),
                         self.host_results[-1][1]["scope"])
        original_sign = rj._IdentityConsentKey.sign
        sign_calls = []

        def sign_once(key, receipt):
            sign_calls.append(1)
            return original_sign(key, receipt)

        real_send = listener_module.send_message
        dropped = []

        def drop_consent_response(frames, payload):
            if self.host_calls["consent"] and not dropped:
                dropped.append(1)
                raise OSError("dropped after consent commit")
            return real_send(frames, payload)

        with patch.object(rj._IdentityConsentKey, "sign", sign_once), \
             patch.object(listener_module, "send_message", drop_consent_response):
            with self.assertRaises(JoiningError) as error:
                self.owner.execute("consent", self.import_id, {"approve": True})
        self.assertEqual(str(error.exception), "control_unavailable", (self.owner.store.get_import(self.import_id), self.host_calls, self.host_results))
        persisted = self.owner.store.get_consent(self.import_id)
        self.assertIsNotNone(persisted, (self.owner.store.get_import(self.import_id), self.host_calls, self.host_results))
        self.assertEqual(self.owner.store.get_import(self.import_id)["state"], "consent_pending_delivery")
        self.assertEqual(len(sign_calls), 1)
        before_retry = dump(self.host_store.path)

        owner = self.restart_owner()
        with patch.object(rj._IdentityConsentKey, "sign", sign_once):
            result = owner.execute("consent", self.import_id, {"approve": True})
        self.assertEqual(result["status"], "activating")
        self.assertEqual(owner.store.get_import(self.import_id)["state"], "consent_given")
        self.assertEqual(owner.store.get_consent(self.import_id), persisted)
        self.assertEqual(len(sign_calls), 1)
        self.assertEqual(self.host_calls["consent"], 2)
        self.assertEqual(dump(self.host_store.path), before_retry)



class DefaultH3MtlsRegression(H2Base):
    """RED/GREEN network regression: accepted H1/H2 listener with default empty H3 map stays fail-closed."""

    def setUp(self):
        super().setUp()
        self.lobby()
        self.issue()
        self.listener = self.listen_host(None)
        self.before = dump(self.path)

    def ask_wire(self, request):
        result = self.send(self.listener, client=self.bb, server=self.ba, request=request,
                           endpoint_id=self.endpoint, generation=self.generation)
        self.assertEqual(result[0:2], ("ok", 403), result)
        self.assertEqual(result[2], b'{"error":"control denied"}')

    def test_injected_preview_uses_real_context_and_store(self):
        listener = self.listen_host(build_host_handlers(self.store, OwnerContext(ps.INSTALL_A, "default")))
        response = self.send(listener, client=self.bb, server=self.ba,
                             request={"op": "preview", "lobby_id": self.lobby_id,
                                      "invite_id": self.issued.invite_id},
                             endpoint_id=self.endpoint, generation=self.generation)
        self.assertEqual(response[0:2], ("ok", 200), response)
        self.assertEqual(json.loads(response[2]), {"name": "Known owners", "expires_at": self.issued.expires_at})
        self.assertEqual(dump(self.path), self.before)

        requests = [
            {"op": "preview", "lobby_id": self.lobby_id, "invite_id": self.issued.invite_id},
            {"op": "redeem", "lobby_id": self.lobby_id, "invite_id": self.issued.invite_id, "secret": self.issued.secret},
            {"op": "status", "lobby_id": self.lobby_id, "invite_id": self.issued.invite_id},
            {"op": "consent", "lobby_id": self.lobby_id, "invite_id": self.issued.invite_id,
             "receipt": {}, "signature": "x" * 88},
        ]
        for request in requests:
            self.ask_wire(request)
        self.assertEqual(dump(self.path), self.before)
        self.assertEqual(self.listener.dispatched, len(requests))

    def test_factory_rejects_missing_unbound_and_foreign_owner_without_adopting_store(self):
        with self.assertRaises(ValueError):
            build_host_handlers(self.store, None)
        bare = RoomInviteStore(self.tmp / "bare.sqlite", clock=self.clock)
        before_bare = dump(bare.path)
        with self.assertRaises(ValueError):
            build_host_handlers(bare, OwnerContext(ps.INSTALL_A, "default"))
        self.assertEqual(dump(bare.path), before_bare)

        foreign = RoomInviteStore(self.tmp / "foreign.sqlite", clock=self.clock)
        foreign.bind_owner_scope(ps.INSTALL_C, "default")
        before_foreign = dump(foreign.path)
        with self.assertRaises(ValueError):
            build_host_handlers(foreign, OwnerContext(ps.INSTALL_A, "default"))
        self.assertEqual(dump(foreign.path), before_foreign)


class RealH3WireFlow(H2Base):
    """The H3 contract exercised through the real pinned loopback mTLS listener."""

    def setUp(self):
        super().setUp()
        self.lobby()
        self.issue()

    def wire(self, listener, request, *, client=None, endpoint=None, generation=None):
        result = self.send(listener, client=client or self.bb, server=self.ba, request=request,
                           endpoint_id=endpoint or self.endpoint, generation=generation or self.generation)
        self.assertEqual(result[0], "ok", result)
        return result[1], json.loads(result[2])

    def request(self, op, **fields):
        return {"op": op, "lobby_id": fields.pop("lobby_id", self.lobby_id),
                "invite_id": fields.pop("invite_id", self.issued.invite_id), **fields}

    def test_real_mtls_nominal_flow_retry_and_negative_consent_are_fail_closed(self):
        listener = self.listen_host(build_host_handlers(self.store, OwnerContext(ps.INSTALL_A, "default")))
        status, body = self.wire(listener, self.request("preview"))
        self.assertEqual((status, body), (200, {"name": "Known owners", "expires_at": self.issued.expires_at}))
        before_redeem = dump(self.path)
        status, body = self.wire(listener, self.request("redeem", secret=self.issued.secret))
        self.assertEqual((status, body), (200, {"status": "awaiting_host_approval"}))
        after_redeem = dump(self.path)
        self.assertNotEqual(after_redeem, before_redeem)
        # A duplicate redemption is refused on the wire and cannot write a second row/update.
        status, body = self.wire(listener, self.request("redeem", secret=self.issued.secret))
        self.assertEqual((status, body), (403, {"error": "control denied"}))
        self.assertEqual(dump(self.path), after_redeem)

        status, body = self.wire(listener, self.request("status"))
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "awaiting_host_approval")
        scope = body["scope"]
        self.store.decide_host(self.lobby_id, self.issued.invite_id, binding=self.binding, approved=True)
        status, body = self.wire(listener, self.request("status"))
        self.assertEqual((status, body["status"]), (200, "consent_required"))
        self.assertEqual(body["scope"], scope)

        receipt = dict(scope, purpose="joining-owner-consent-v1")
        signature = base64.b64encode(self.bb.consent_key().sign(consent.canonical(receipt))).decode()
        before_bad = dump(self.path)
        bad = dict(receipt, purpose="wrong-purpose")
        bad_signature = base64.b64encode(self.bb.consent_key().sign(consent.canonical(bad))).decode()
        status, body = self.wire(listener, self.request("consent", receipt=bad, signature=bad_signature))
        self.assertEqual((status, body), (403, {"error": "control denied"}))
        self.assertEqual(dump(self.path), before_bad)
        status, body = self.wire(listener, self.request("consent", receipt=receipt, signature=signature[:-1] + "A"))
        self.assertEqual((status, body), (403, {"error": "control denied"}))
        self.assertEqual(dump(self.path), before_bad)

        status, body = self.wire(listener, self.request("consent", receipt=receipt, signature=signature))
        self.assertEqual((status, body), (200, {"status": "ready"}))
        after_consent = dump(self.path)
        # Exact retry is read-only in H2: no second callback/effect or database mutation.
        status, body = self.wire(listener, self.request("consent", receipt=receipt, signature=signature))
        self.assertEqual((status, body), (200, {"status": "ready"}))
        self.assertEqual(dump(self.path), after_consent)
        self.assertIsNone(self.store.get_lobby(self.lobby_id)["room_id"])

    def test_real_mtls_wrong_peer_and_pending_consent_have_same_fixed_403_and_zero_mutation(self):
        self.redeem_as_b()
        listener = self.listen_host(build_host_handlers(self.store, OwnerContext(ps.INSTALL_A, "default")))
        before = dump(self.path)
        paired_c = self.pair_c()
        endpoint_c, generation_c = self.ack(self.a, self.c, paired_c)
        status, body = self.wire(listener, self.request("status"), client=self.bc,
                                 endpoint=endpoint_c, generation=generation_c)
        self.assertEqual((status, body), (403, {"error": "control denied"}))
        self.assertEqual(dump(self.path), before)
        # Pending host approval is not a remotely consentable state.  A malformed receipt
        # reaches the same generic denial and never causes an effect.
        status, body = self.wire(listener, self.request("consent", receipt={}, signature="x" * 88))
        self.assertEqual((status, body), (403, {"error": "control denied"}))
        self.assertEqual(dump(self.path), before)

    def test_real_mtls_lost_consent_acknowledgement_retries_without_second_mutation(self):
        self.redeem_as_b()
        self.store.decide_host(self.lobby_id, self.issued.invite_id, binding=self.binding, approved=True)
        status_listener = self.listen_host(build_host_handlers(self.store, OwnerContext(ps.INSTALL_A, "default")))
        _, status_body = self.wire(status_listener, self.request("status"))
        scope = status_body["scope"]
        receipt = dict(scope, purpose="joining-owner-consent-v1")
        signature = base64.b64encode(self.bb.consent_key().sign(consent.canonical(receipt))).decode()
        real = build_host_handlers(self.store, OwnerContext(ps.INSTALL_A, "default"))["joining_consent"]
        calls = []

        def lose_ack(context, request):
            calls.append(1)
            result = real(context, request)
            if len(calls) == 1:
                raise ControlError("simulated_lost_reply")
            return result

        lost_listener = self.listen_host({"joining_consent": lose_ack})
        before = dump(self.path)
        status, body = self.wire(lost_listener, self.request("consent", receipt=receipt, signature=signature))
        self.assertEqual((status, body), (403, {"error": "control denied"}))
        after_first = dump(self.path)
        self.assertNotEqual(after_first, before)
        status, body = self.wire(lost_listener, self.request("consent", receipt=receipt, signature=signature))
        self.assertEqual((status, body), (200, {"status": "ready"}))
        self.assertEqual(dump(self.path), after_first)
        self.assertEqual(calls, [1, 1])

    def test_real_mtls_response_drop_after_redeem_commit_is_transport_loss(self):
        listener = self.listen_host(build_host_handlers(self.store, OwnerContext(ps.INSTALL_A, "default")))
        real_send = listener_module.send_message
        dropped = []

        def drop_first_response(frames, payload):
            if not dropped:
                dropped.append(1)
                # The handler has already committed before _serve reaches this
                # response boundary; close the TLS stream instead of emitting a
                # product-level 403.
                raise OSError("response dropped")
            return real_send(frames, payload)

        before = dump(self.path)
        with patch.object(listener_module, "send_message", drop_first_response):
            result = self.send(listener, client=self.bb, server=self.ba,
                               request=self.request("redeem", secret=self.issued.secret),
                               endpoint_id=self.endpoint, generation=self.generation)
        self.assertEqual(result[0], "err")
        after = dump(self.path)
        self.assertNotEqual(after, before)
        self.assertEqual(len(after["redemptions"]), 1)
        self.assertEqual(len(after["invitations"]), 1)
        # A second application request cannot replay the secret after the
        # committed redemption; the store's final state is already consumed.
        status, body = self.wire(listener, self.request("redeem", secret=self.issued.secret))
        self.assertEqual((status, body), (403, {"error": "control denied"}))
        self.assertEqual(dump(self.path), after)


if __name__ == "__main__":
    unittest.main()
