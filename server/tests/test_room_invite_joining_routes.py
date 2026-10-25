"""P3b.2 real HTTP + real F4 store/journal/identity + schema-validating dispatcher.
Only the control wire/host lobby behavior is simulated (NOT mTLS/native E2E).
"""
from __future__ import annotations
import asyncio
import contextlib
import copy
import importlib
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pairing_support as ps
from invite_rooms import runtime_config
try:
    from invite_rooms import runtime_joining as rj
except ImportError:
    rj = None  # unchanged-base RED exercises real HTTP, not an import failure
from invite_rooms.control.dispatcher import ControlDispatcher
from invite_rooms.joining_store import JoiningStore

P = "/api/local/room-invites"
LOBBY = "lobby-" + "1" * 32
INVITE = "invite-" + "2" * 32
SECRET = "s" * 43
CODE = "https://host.example/invite/" + LOBBY + "/" + INVITE + "#" + SECRET


def setUpModule():
    global TEMPLATES
    TEMPLATES = {i: ps.identity_template(i) for i in (ps.INSTALL_A, ps.INSTALL_B, ps.INSTALL_C)}


def tearDownModule():
    for p in TEMPLATES.values():
        shutil.rmtree(p.parent)


def private_json(path, value):
    ps.private_dir(path.parent)
    path.write_text(json.dumps(value))
    path.chmod(0o600)


class JoiningHttpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.tmp.chmod(0o700)
        self.addCleanup(shutil.rmtree, self.tmp)
        self.A = ps.Side(self.tmp, "a", ps.INSTALL_A, template=TEMPLATES[ps.INSTALL_A])
        self.B = ps.Side(self.tmp, "b", ps.INSTALL_B, template=TEMPLATES[ps.INSTALL_B])
        self.a, self.b = self.A.service(), self.B.service()
        self.paired = ps.drive_pairing(self.a, self.b)
        d = ps.dispatcher_for(self.b)
        try:
            self.a.send_pair_ack(self.paired["a_id"], ps.link_sender(d))
        finally:
            asyncio.run(d.close())
        self.ep = self.A.peers()[0]["endpoint_id"]
        self.peer = "peer-" + "a" * 32
        self.cfg_path = self.B.root / "cfg" / "runtime.json"
        self.joinroot = self.B.root / "joining"
        self.hosts = self.B.root / "cfg" / "hosts.json"
        self.cfg = {"store_path": str(self.B.root / "invites" / "host.sqlite"), "installation_id": ps.INSTALL_B,
                    "profile": "default", "peer_registry_path": str(self.B.peers_path),
                    "pairing": {"identity_dir": str(self.B.identity_dir), "journal_dir": str(self.B.journal_dir), "peers_path": str(self.B.peers_path)},
                    "joining": {"store_dir": str(self.joinroot), "identity_dir": str(self.B.identity_dir),
                                "hosts_path": str(self.hosts), "origins": {self.peer: "https://host.example"},
                                "host_label": "host", "own_bot_id": "self"}}
        self.save_config()
        self.env = patch.dict(os.environ, {"MISSION_CONTROL_ROOM_INVITES_CONFIG": str(self.cfg_path),
                                          "MISSION_CONTROL_TOKEN": "unit-token"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.old_origin = os.environ.pop("MISSION_CONTROL_ALLOWED_ORIGIN", None)
        self.addCleanup(self.restore_origin)
        self.calls = []
        self.approved = False
        self.redeemed = False
        self.active = False
        self.lost_redeem = False
        self.lost_consent = False
        self.offline = False
        self.before_send = None
        self.after_reply = None
        self.bad_scope = None
        self.metadata_reply = None
        self.remote_status = None
        self.receipts = []
        if rj:
            rj.configure_joining_control(self.factory)
            self.addCleanup(rj.configure_joining_control)
        mod = importlib.import_module("local_telemetry_server")
        self.httpd = mod.ThreadingHTTPServer(("127.0.0.1", 0), mod.Handler)
        self.thread = threading.Thread(target=lambda: self.httpd.serve_forever(poll_interval=0.01), daemon=True)
        self.thread.start()
        self.addCleanup(self.shutdown)

    def restore_origin(self):
        if self.old_origin is None:
            os.environ.pop("MISSION_CONTROL_ALLOWED_ORIGIN", None)
        else:
            os.environ["MISSION_CONTROL_ALLOWED_ORIGIN"] = self.old_origin

    def shutdown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(2)

    def save_config(self):
        private_json(self.cfg_path, self.cfg)

    def scope(self):
        binding = runtime_config.build_peer_registry(self.A.peers_path).binding_for("peer-" + "b" * 32)
        value = {"room_id": "invite-room-approved", "member_id": "joining-b", "lobby_id": LOBBY,
                 "invite_id": INVITE, "host_id": ps.INSTALL_A, "binding": binding}
        if self.bad_scope:
            self.bad_scope(value)
        return value

    def host(self, request):
        self.calls.append(copy.deepcopy(request))
        op = request["op"]
        if op == "preview":
            return self.metadata_reply if self.metadata_reply is not None else {"name": "Shared room", "expires_at": 1900000000}
        if op == "redeem":
            self.assertEqual(request["secret"], SECRET)
            # The real store intent is committed before any host handler runs.
            s = JoiningStore(self.joinroot)
            try:
                self.assertEqual(s.find_import(LOBBY, INVITE)["state"], "redeeming")
            finally:
                s.close()
            self.redeemed = True
            if self.lost_redeem:
                raise OSError("simulated lost ack")
            return {"status": "awaiting_host_approval"}
        if op == "status":
            if self.remote_status is not None:
                return self.remote_status
            return {"status": "active" if self.active else "consent_required" if self.approved else "awaiting_host_approval",
                    "scope": self.scope()}
        if op == "joining_consent":
            from invite_rooms.consent import verify
            peer = runtime_config.build_peer_registry(self.A.peers_path).lookup("peer-" + "b" * 32)
            verify(peer.consent_public_key, request["receipt"], request["signature"],
                   dict(self.scope(), purpose="joining-owner-consent-v1"))
            self.receipts.append(copy.deepcopy(request))
            s = JoiningStore(self.joinroot)
            try:
                rec = s.find_import(LOBBY, INVITE)
                self.assertEqual(s.get_consent(rec["import_id"]), {"receipt": request["receipt"], "signature": request["signature"]})
            finally:
                s.close()
            if self.lost_consent:
                raise OSError("simulated lost ack")
            return {"status": "ready"}
        raise AssertionError("unexpected op")

    def factory(self, config, link):
        test = self
        self.assertEqual(link["endpoint_id"], self.ep)
        class Wire:
            async def call(self, request, *, deadline, admit):
                await asyncio.sleep(0)
                if test.before_send:
                    test.before_send()
                admit()
                if test.offline:
                    raise OSError("simulated offline")
                dispatcher = ControlDispatcher(test.a.authority, handlers={k: test.host for k in ("preview", "redeem", "status", "joining_consent")},
                    direction="B_to_A", owner_verify=lambda ep, req: ep == test.ep and req.get("lobby_id") == LOBBY and req.get("invite_id") == INVITE,
                    admit=lambda ep, gen, op: test.a.authority.current(ep).protocol_state == "acknowledged")
                try:
                    result = await dispatcher.dispatch(test.ep, 1, request)
                    if test.after_reply:
                        test.after_reply()
                    return result
                finally:
                    await dispatcher.close()
        return Wire()

    def req(self, method, suffix, payload=None, *, token="unit-token", origin=None, raw=None, ctype="application/json", extra=None):
        conn = HTTPConnection(*self.httpd.server_address, timeout=8)
        headers = dict(extra or {})
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        if origin is not None:
            headers["Origin"] = origin
        body = raw if raw is not None else json.dumps(payload).encode() if payload is not None else None
        if body is not None:
            headers["Content-Type"] = ctype
        try:
            conn.request(method, P + suffix, body, headers)
            reply = conn.getresponse()
            value = json.loads(reply.read())
            return reply.status, value, dict(reply.getheaders())
        finally:
            conn.close()

    def preview(self):
        status, value, _ = self.req("POST", "/imports/preview", {"code": CODE})
        self.assertEqual(status, 200, value)
        self.id = value["import"]["id"]
        return value["import"]

    def action(self, action, body=None):
        return self.req("GET" if action == "get" else "POST", "/imports/" + self.id + ("" if action == "get" else "/" + action),
                        None if action == "get" else body if body is not None else {})

    def redeem(self):
        self.preview()
        status, value, _ = self.action("redeem")
        self.assertEqual(status, 200, value)

    def confirmed(self):
        self.redeem()
        self.approved = True
        status, value, _ = self.action("get")
        self.assertEqual(status, 200, value)
        scope = value["import"]["scope"]
        status, value, _ = self.action("scope", {"room_id": scope["room_id"], "member_id": scope["member_id"]})
        self.assertEqual(status, 200, value)

    def state(self):
        s = JoiningStore(self.joinroot)
        try:
            return s.get_import(self.id)["state"]
        finally:
            s.close()

    def test_full_nominal_separate_scope_and_consent(self):
        first = self.preview()
        self.assertEqual(first["status"], "preview")
        self.assertEqual(self.calls, [])
        status, v, _ = self.action("metadata")
        self.assertEqual((status, v["import"]["status"]), (200, "metadata"))
        self.assertTrue(v["import"]["metadata"]["expires_at"].endswith("Z"))
        self.assertEqual(self.action("redeem")[0], 200)
        self.assertEqual(self.action("get")[1]["import"]["status"], "awaiting_host")
        self.approved = True
        approved = self.action("get")[1]["import"]
        self.assertEqual(approved["status"], "scope")
        self.assertEqual(approved["scope"]["binding"], self.scope()["binding"])
        self.assertEqual(approved["scope"]["permissions"], ["bot_room"])
        self.assertEqual(self.receipts, [])
        self.assertEqual(self.action("scope", {"room_id": "invite-room-approved", "member_id": "joining-b"})[1]["import"]["status"], "consent")
        self.assertEqual(self.action("consent", {"approve": True})[1]["import"]["status"], "activating")
        self.assertEqual(self.state(), "consent_given")
        self.active = True
        final = self.action("get")[1]["import"]
        self.assertEqual((final["status"], final["room_id"]), ("active", "invite-room-approved"))
        self.assertTrue(all(c["op"] in {"preview", "redeem", "status", "joining_consent"} for c in self.calls))

    def test_feature_off_404(self):
        os.environ.pop("MISSION_CONTROL_ROOM_INVITES_CONFIG")
        self.assertEqual(self.req("POST", "/imports/preview", {"code": CODE})[0], 404)

    def test_auth_first_off(self):
        os.environ.pop("MISSION_CONTROL_ROOM_INVITES_CONFIG")
        self.assertEqual(self.req("POST", "/imports/preview", {"code": CODE}, token=None)[0], 401)

    def test_auth_first_on(self):
        self.assertEqual(self.req("POST", "/imports/preview", {"code": CODE}, token=None)[0], 401)
        self.assertFalse(self.hosts.exists())

    def test_incomplete_owner_keeps_guest(self):
        del self.cfg["joining"]["identity_dir"]
        self.save_config()
        self.assertEqual(self.req("POST", "/imports/preview", {"code": CODE})[0], 404)
        self.assertEqual(self.req("GET", "/guest-rooms")[0], 200)

    def test_different_identity_dir_404(self):
        self.cfg["joining"]["identity_dir"] = str(self.tmp / "other")
        self.save_config()
        self.assertEqual(self.req("POST", "/imports/preview", {"code": CODE})[0], 404)

    def test_missing_origin_404(self):
        self.cfg["joining"]["origins"] = {}
        self.save_config()
        self.assertEqual(self.req("POST", "/imports/preview", {"code": CODE})[0], 404)

    def test_origin_exact_match(self):
        os.environ["MISSION_CONTROL_ALLOWED_ORIGIN"] = "https://ui.example"
        self.assertEqual(self.req("POST", "/imports/preview", {"code": CODE}, origin="https://ui.example")[0], 200)
        self.assertEqual(self.req("POST", "/imports/preview", {"code": CODE}, origin="https://ui.example.attacker")[0], 403)

    def test_origin_unconfigured_rejected(self):
        self.assertEqual(self.req("POST", "/imports/preview", {"code": CODE}, origin="https://ui.example")[0], 403)

    def test_unknown_json_key(self):
        self.assertEqual(self.req("POST", "/imports/preview", {"code": CODE, "url": "http://127.0.0.1"})[0], 400)

    def test_duplicate_json_keys(self):
        self.assertEqual(self.req("POST", "/imports/preview", raw=b'{"code":"a","code":"b"}')[0], 400)

    def test_json_array_rejected(self):
        self.assertEqual(self.req("POST", "/imports/preview", [CODE])[0], 400)

    def test_bad_utf8_rejected(self):
        self.assertEqual(self.req("POST", "/imports/preview", raw=b'\xff')[0], 400)

    def test_bad_json_rejected(self):
        self.assertEqual(self.req("POST", "/imports/preview", raw=b'{')[0], 400)

    def test_body_bound(self):
        self.assertEqual(self.req("POST", "/imports/preview", raw=b' ' * 16385)[0], 413)

    def test_content_type(self):
        self.assertEqual(self.req("POST", "/imports/preview", {"code": CODE}, ctype="text/plain")[0], 400)

    def test_missing_code(self):
        self.assertEqual(self.req("POST", "/imports/preview", {})[0], 400)

    def test_wrong_host_selection(self):
        self.assertEqual(self.req("POST", "/imports/preview", {"code": CODE, "host_id": "other"})[0], 404)
        self.assertEqual(self.calls, [])

    def test_selected_host_is_resolved_locally(self):
        self.assertEqual(self.req("POST", "/imports/preview", {"code": CODE, "host_id": self.peer})[0], 200)
        self.assertEqual(self.calls, [])

    def test_no_dns_in_preview(self):
        with patch("socket.getaddrinfo", side_effect=AssertionError("DNS forbidden")):
            # HTTPConnection with numeric loopback also uses getaddrinfo; call the
            # real adapter synchronously against the parsed real config instead.
            cfg = runtime_config._load_config_file(str(self.cfg_path))
            owner = rj.load_owner(cfg) if rj else None
            self.assertIsNotNone(owner)
            try:
                view = owner.execute("preview", body={"code": CODE})
                self.assertEqual(view["status"], "preview")
            finally:
                owner.close()
        self.assertEqual(self.calls, [])

    def test_invitation_ssrf_origin_tamper(self):
        for origin in ("http://127.0.0.1", "https://127.0.0.1", "https://host.example.attacker", "https://user@host.example"):
            with self.subTest(origin=origin):
                self.assertEqual(self.req("POST", "/imports/preview", {"code": CODE.replace("https://host.example", origin)})[0], 400)
        self.assertEqual(self.calls, [])

    def test_invitation_query_rejected(self):
        self.assertEqual(self.req("POST", "/imports/preview", {"code": CODE.replace("#", "?x=y#")})[0], 400)

    def test_preview_idempotent(self):
        first = self.preview()["id"]
        self.assertEqual(self.preview()["id"], first)

    def test_foreign_import_hidden(self):
        self.preview()
        s = JoiningStore(self.joinroot)
        try:
            foreign = s.create_import(lobby_id="lobby-other", invite_id="invite-other", secret=SECRET, host_peer_id="foreign")
        finally:
            s.close()
        self.assertEqual(self.req("GET", "/imports/" + foreign)[0], 404)
        self.assertEqual(self.calls, [])

    def test_unknown_import_hidden(self):
        self.assertEqual(self.req("GET", "/imports/import-" + "0" * 32)[0], 404)

    def test_bad_id_no_lookup(self):
        self.assertEqual(self.req("GET", "/imports/../mint")[0], 404)

    def test_metadata_non_consuming_redacted(self):
        self.preview()
        status, value, headers = self.action("metadata")
        self.assertEqual(status, 200)
        self.assertNotIn(SECRET, json.dumps(value))
        self.assertEqual(self.state(), "previewed")
        self.assertNotIn("secret", self.calls[0])
        self.assertEqual(headers["Cache-Control"], "no-store")

    def test_status_does_not_fetch_preview(self):
        self.preview()
        self.assertEqual(self.action("get")[0], 200)
        self.assertEqual(self.calls, [])

    def test_redeem_once(self):
        self.redeem()
        self.assertEqual(self.action("redeem")[0], 200)
        self.assertEqual(len([c for c in self.calls if c["op"] == "redeem"]), 1)

    def test_secret_erased_after_redeem(self):
        self.redeem()
        s = JoiningStore(self.joinroot)
        try:
            with self.assertRaises(ValueError):
                s.load_secret(self.id)
        finally:
            s.close()

    def test_lost_redeem_ack_visible_and_no_retry(self):
        self.preview()
        self.lost_redeem = True
        status, value, _ = self.action("redeem")
        self.assertEqual((status, value.get("code")), (409, "redeem_ambiguous"))
        self.assertEqual(self.action("get")[1]["import"]["status"], "redeem_ambiguous")
        self.assertEqual(self.action("redeem")[0], 409)
        self.assertEqual(len([c for c in self.calls if c["op"] == "redeem"]), 1)

    def test_resolve_empty_is_not_evidence(self):
        self.preview()
        self.lost_redeem = True
        self.action("redeem")
        status, value, _ = self.action("resolve")
        self.assertEqual((status, value.get("code")), (409, "manual_reconciliation_required"))
        self.assertEqual(self.state(), "redeem_ambiguous")

    def test_browser_confirmed_redeemed_not_evidence(self):
        self.preview()
        self.assertEqual(self.action("resolve", {"confirmed_redeemed": True})[0], 400)

    def test_scope_before_redeem(self):
        self.preview()
        self.assertEqual(self.action("scope", {"room_id": "invite-room-approved", "member_id": "joining-b"})[0], 409)

    def test_scope_before_host_approval(self):
        self.redeem()
        self.assertEqual(self.action("scope", {"room_id": "invite-room-approved", "member_id": "joining-b"})[0], 409)

    def test_scope_guessed_values_rejected(self):
        self.redeem()
        self.approved = True
        self.assertEqual(self.action("scope", {"room_id": "invite-room-guessed", "member_id": "joining-b"})[0], 409)
        self.assertEqual(self.receipts, [])

    def test_scope_six_field_substitution(self):
        self.redeem()
        self.approved = True
        self.bad_scope = lambda s: s["binding"].update(endpoint_id="wrong")
        self.assertEqual(self.action("get")[0], 409)
        self.assertEqual(self.action("scope", {"room_id": "invite-room-approved", "member_id": "joining-b"})[0], 409)

    def test_scope_other_invite_rejected(self):
        self.redeem()
        self.approved = True
        self.bad_scope = lambda s: s.update(invite_id="invite-other")
        self.assertEqual(self.action("get")[0], 409)

    def test_consent_before_confirm(self):
        self.redeem()
        self.approved = True
        self.assertEqual(self.action("consent", {"approve": True})[0], 409)
        self.assertEqual(self.receipts, [])

    def test_consent_boolean_strict(self):
        self.preview()
        for val in (1, "true", None):
            self.assertEqual(self.action("consent", {"approve": val})[0], 400)

    def test_refusal_local_unsigned(self):
        self.redeem()
        self.assertEqual(self.action("consent", {"approve": False})[1]["import"]["status"], "rejected")
        self.assertEqual(self.receipts, [])

    def test_lost_consent_restart_identical_signature(self):
        self.confirmed()
        self.lost_consent = True
        self.assertEqual(self.action("consent", {"approve": True})[0], 503)
        self.assertEqual(self.state(), "consent_pending_delivery")
        first = copy.deepcopy(self.receipts[0])
        # Every HTTP request reconstructs config, identity, journal and F4 store.
        self.lost_consent = False
        with patch.object(rj._IdentityConsentKey, "sign", side_effect=AssertionError("must reuse persisted signature")):
            self.assertEqual(self.action("consent", {"approve": True})[0], 200)
        self.assertEqual(self.receipts[-1], first)

    def test_refusal_cannot_undo_signed_pending(self):
        self.confirmed()
        self.lost_consent = True
        self.action("consent", {"approve": True})
        self.assertEqual(self.action("consent", {"approve": False})[0], 409)
        self.assertEqual(self.state(), "consent_pending_delivery")

    def test_host_offline(self):
        self.preview()
        self.offline = True
        self.assertEqual(self.action("metadata")[0], 503)
        self.assertEqual(self.state(), "previewed")

    def test_missing_control_factory_unavailable(self):
        self.preview()
        rj.configure_joining_control() if rj else None
        self.assertEqual(self.action("metadata")[0], 503)
        self.assertEqual(self.calls, [])

    def test_unpair_during_queue_no_redeem_send(self):
        self.preview()
        self.before_send = lambda: self.b.unpair(self.peer)
        self.assertEqual(self.action("redeem")[0], 409)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.state(), "redeem_ambiguous")

    def test_unpair_during_status_prevents_scope_confirmation(self):
        self.redeem()
        self.approved = True
        self.after_reply = lambda: self.b.unpair(self.peer)
        self.assertEqual(self.action("scope", {"room_id": "invite-room-approved", "member_id": "joining-b"})[0], 503)
        s = JoiningStore(self.joinroot)
        try:
            self.assertIsNone(s.confirmed_scope(self.id))
        finally:
            s.close()

    def test_unpaired_old_import_no_access(self):
        self.preview()
        self.b.unpair(self.peer)
        self.assertEqual(self.action("get")[0], 404)

    def test_unacknowledged_pairing_refused(self):
        # New confirmed link has no ack. Existing first host is revoked.
        self.b.unpair(self.peer)
        self.a.unpair("peer-" + "b" * 32)
        ps.drive_pairing(self.a, self.b)
        self.assertEqual(self.req("POST", "/imports/preview", {"code": CODE})[0], 404)

    def test_second_host_refused(self):
        C = ps.Side(self.tmp, "c", ps.INSTALL_C, template=TEMPLATES[ps.INSTALL_C])
        ps.drive_pairing(C.service(), self.b)
        self.cfg["joining"]["origins"]["peer-" + "c" * 32] = "https://second.example"
        self.save_config()
        self.assertEqual(self.req("POST", "/imports/preview", {"code": CODE, "host_id": self.peer})[0], 404)

    def test_host_projection_manual_edit_rejected(self):
        self.preview()
        projection = json.loads(self.hosts.read_text())
        projection["hosts"][0]["binding"]["fingerprint"] = "f" * 64
        private_json(self.hosts, projection)
        self.assertEqual(self.action("get")[0], 404)

    def test_hosts_projection_symlink_rejected(self):
        self.hosts.symlink_to(self.B.peers_path)
        self.assertEqual(self.req("POST", "/imports/preview", {"code": CODE})[0], 404)
        self.assertEqual(json.loads(self.B.peers_path.read_text()), [])

    def test_null_projection_is_tampering_not_missing(self):
        self.preview()
        private_json(self.hosts, None)
        self.assertEqual(self.action("get")[0], 404)
        self.assertIsNone(json.loads(self.hosts.read_text()))

    def test_no_browser_mint_endpoint(self):
        self.preview()
        for suffix in ("/imports/" + self.id + "/material", "/imports/" + self.id + "/mint", "/material", "/mint"):
            self.assertEqual(self.req("POST", suffix, {})[0], 404)
        self.assertEqual(self.calls, [])

    def test_no_secrets_response_or_logs(self):
        import logging
        records = []
        class Recorder(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())
        logger = logging.getLogger()
        h = Recorder()
        logger.addHandler(h)
        try:
            results = [self.preview(), self.action("metadata")[1], self.action("redeem")[1], self.action("get")[1]]
        finally:
            logger.removeHandler(h)
        encoded = json.dumps(results) + "\n".join(records)
        for secret in (SECRET, "consent.key", "PRIVATE KEY", "ciphertext", "signature"):
            self.assertNotIn(secret, encoded)

    def test_metadata_bad_schema_fails_closed(self):
        self.preview()
        self.metadata_reply = {"name": "x", "expires_at": 1900000000, "secret": SECRET}
        self.assertEqual(self.action("metadata")[0], 503)

    def test_host_scope_not_implicitly_confirmed(self):
        self.redeem()
        self.approved = True
        self.action("get")
        s = JoiningStore(self.joinroot)
        try:
            self.assertIsNone(s.confirmed_scope(self.id))
        finally:
            s.close()
        self.assertEqual(self.receipts, [])

    def test_arbitrary_query_cannot_select_profile(self):
        self.preview()
        self.assertEqual(self.req("GET", "/imports/" + self.id + "?profile=other")[0], 400)

    def test_post_unknown_empty_action_key_rejected(self):
        self.preview()
        self.assertEqual(self.action("metadata", {"endpoint": "http://127.0.0.1"})[0], 400)
        self.assertEqual(self.calls, [])

    def test_scope_not_exposed_for_pending_host_even_when_wire_includes_it(self):
        self.redeem()
        result = self.action("get")[1]["import"]
        self.assertEqual(result["status"], "awaiting_host")
        self.assertNotIn("scope", result)

    def test_host_denied_mapping(self):
        self.redeem()
        self.remote_status = {"status": "denied", "scope": self.scope()}
        result = self.action("get")[1]["import"]
        self.assertEqual(result["status"], "rejected")
        self.assertNotIn("scope", result)

    def test_host_expired_mapping(self):
        self.redeem()
        self.remote_status = {"status": "expired", "scope": None}
        self.assertEqual(self.action("get")[1]["import"]["status"], "expired")

    def test_malformed_status_is_unavailable_not_internal(self):
        self.redeem()
        self.remote_status = {"status": [], "scope": None}
        self.assertEqual(self.action("get")[0], 503)

    def test_status_extra_secret_field_never_returned(self):
        self.redeem()
        self.remote_status = {"status": "awaiting_host_approval", "scope": None, "secret": SECRET}
        response = self.action("get")
        self.assertEqual(response[0], 503)
        self.assertNotIn(SECRET, json.dumps(response[1]))

    def test_generation_cutover_while_consent_queued(self):
        self.confirmed()
        self.before_send = lambda: self.b.unpair(self.peer)
        status, value, _ = self.action("consent", {"approve": True})
        self.assertEqual(status, 503, value)
        self.assertEqual(self.receipts, [])

    def test_signed_pending_delivery_blocked_by_unpair_after_status(self):
        self.confirmed()
        # Only the consent send is stopped: status has already returned and
        # the receipt was persisted. No unsigned/refused rollback is possible.
        def boundary():
            if self.state() == "consent_pending_delivery":
                self.b.unpair(self.peer)
        self.before_send = boundary
        self.assertEqual(self.action("consent", {"approve": True})[0], 503)
        self.assertEqual(self.state(), "consent_pending_delivery")
        self.assertEqual(self.receipts, [])

    def test_repair_new_generation_cannot_reassociate_old_import(self):
        self.preview()
        self.b.unpair(self.peer)
        self.a.unpair("peer-" + "b" * 32)
        new = ps.drive_pairing(self.a, self.b)
        dispatcher = ps.dispatcher_for(self.b)
        try:
            self.a.send_pair_ack(new["a_id"], ps.link_sender(dispatcher))
        finally:
            asyncio.run(dispatcher.close())
        # Initial confirmation=1, unpair=2, new confirmation=3.
        self.assertEqual(self.b.journal.snapshot().active_links()[0]["generation"], 3)
        self.assertEqual(self.action("get")[0], 404)
        self.assertEqual(self.action("consent", {"approve": True})[0], 404)

    def test_deadline_after_queue_prevents_emission(self):
        self.preview()
        # Use a short request budget; queue sleep is only in the test wire.
        with patch.object(rj, "TIMEOUT", 0.05) if rj else contextlib.nullcontext():
            self.before_send = lambda: time.sleep(0.06)
            self.assertEqual(self.action("metadata")[0], 503)
        self.assertEqual(self.calls, [])

    def test_nan_json_is_rejected(self):
        self.assertEqual(self.req("POST", "/imports/preview", raw=b'{"code":NaN}')[0], 400)

    def test_deep_json_rejected_without_500(self):
        self.assertEqual(self.req("POST", "/imports/preview", raw=b'{"code":' + b'[' * 1200 + b'0' + b']' * 1200 + b'}')[0], 400)

    def test_get_body_cannot_trigger_an_effect(self):
        self.preview()
        self.assertEqual(self.req("GET", "/imports/" + self.id, {"op": "redeem"})[0], 400)
        self.assertEqual(self.calls, [])

    def test_missing_pairing_disables_only_imports(self):
        del self.cfg["pairing"]
        self.save_config()
        self.assertEqual(self.req("POST", "/imports/preview", {"code": CODE})[0], 404)
        self.assertEqual(self.req("GET", "/guest-rooms")[0], 200)

    def test_projection_private_permissions(self):
        self.preview()
        self.assertEqual(self.hosts.stat().st_mode & 0o777, 0o600)
        self.hosts.chmod(0o644)
        self.assertEqual(self.action("get")[0], 404)

    def test_identity_missing_no_silent_reinitialization(self):
        shutil.rmtree(self.B.identity_dir / "current")
        self.assertEqual(self.req("POST", "/imports/preview", {"code": CODE})[0], 404)
        self.assertFalse((self.B.identity_dir / "current").exists())

    def test_admission_deadline_rechecked_after_journal_lock_wait(self):
        cfg = runtime_config._load_config_file(str(self.cfg_path))
        owner = rj.load_owner(cfg) if rj else None
        self.assertIsNotNone(owner)
        self.addCleanup(owner.close)
        entered, release = threading.Event(), threading.Event()
        def hold():
            with self.b.journal.transaction():
                entered.set()
                release.wait(2)
        t = threading.Thread(target=hold)
        t.start()
        self.assertTrue(entered.wait(1))
        owner.deadline = time.monotonic() + 0.05
        timer = threading.Timer(0.15, release.set)
        timer.start()
        try:
            with self.assertRaisesRegex(ValueError, "control_unavailable"):
                owner.admit()
        finally:
            release.set()
            timer.cancel()
            timer.join()
            t.join(2)
