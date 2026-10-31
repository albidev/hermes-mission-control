"""P3b.1a-2: pairing HTTP routes (real ThreadingHTTPServer + real Handler + real journal/codec/identity).

Only the owner's native-catalog source is replaced by a labelled stand-in (``routes._PAIRING_CATALOG_SOURCE``): the
native RPC transport itself is exercised by the existing P2 suites.
"""
from __future__ import annotations

import importlib
import json
import logging
import os
import shutil
import sys
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pairing_support as ps  # noqa: E402
from invite_rooms import pairing_journal as pj  # noqa: E402
from invite_rooms import routes as room_routes  # noqa: E402
from invite_rooms import runtime_config  # noqa: E402

PREFIX = "/api/local/room-invites"
ID0 = "pairing-" + "0" * 32


def setUpModule():
    global TEMPLATES
    TEMPLATES = {i: ps.identity_template(i) for i in (ps.INSTALL_A, ps.INSTALL_B)}


def tearDownModule():
    for path in TEMPLATES.values():
        shutil.rmtree(path.parent, ignore_errors=True)


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record):
        text = record.getMessage()
        if record.exc_info:
            text += " " + repr(record.exc_info[1])
        self.lines.append(text)


class PairingRoutesBase(unittest.TestCase):
    PAIRING = True

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(dir=os.environ.get("MC_TEST_TMP")))
        os.chmod(self.tmp, 0o700)
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.A = ps.Side(self.tmp, "a", ps.INSTALL_A, template=TEMPLATES[ps.INSTALL_A])
        self.B = ps.Side(self.tmp, "b", ps.INSTALL_B, template=TEMPLATES[ps.INSTALL_B])
        self.b = self.B.service()
        self.config_path = self.tmp / "a" / "cfg" / "room-invites.json"
        self.write_config()
        self._env = {k: os.environ.get(k) for k in ("MISSION_CONTROL_ROOM_INVITES_CONFIG", "MISSION_CONTROL_TOKEN",
                                                   "MISSION_CONTROL_ALLOWED_ORIGIN")}
        os.environ.pop("MISSION_CONTROL_ALLOWED_ORIGIN", None)
        os.environ["MISSION_CONTROL_ROOM_INVITES_CONFIG"] = str(self.config_path)
        os.environ["MISSION_CONTROL_TOKEN"] = "test-token"
        self.addCleanup(self._restore)
        self._source = room_routes._PAIRING_CATALOG_SOURCE
        room_routes._PAIRING_CATALOG_SOURCE = lambda config, ctx: self.A.catalog   # labelled stand-in for native RPC
        self.addCleanup(setattr, room_routes, "_PAIRING_CATALOG_SOURCE", self._source)
        runtime_config._warned_once.clear()
        self.capture = _Capture()
        root = logging.getLogger()
        root.addHandler(self.capture)
        self._level = root.level
        root.setLevel(logging.DEBUG)
        self.addCleanup(root.removeHandler, self.capture)
        self.addCleanup(root.setLevel, self._level)
        module = importlib.import_module("local_telemetry_server")
        self.httpd = module.ThreadingHTTPServer(("127.0.0.1", 0), module.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop)

    def _stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)

    def _restore(self):
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def write_config(self, pairing="default"):
        store = self.tmp / "a" / "store" / "invites.db"
        payload = {"store_path": str(store), "installation_id": ps.INSTALL_A, "profile": ps.PROFILE,
                   "peer_registry_path": str(self.A.peers_path)}
        if isinstance(pairing, str) and pairing == "default":
            if self.PAIRING:
                payload["pairing"] = {"identity_dir": str(self.A.identity_dir), "journal_dir": str(self.A.journal_dir),
                                      "peers_path": str(self.A.peers_path)}
        else:
            payload["pairing"] = pairing
        fd = os.open(self.config_path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        os.write(fd, json.dumps(payload).encode())
        os.close(fd)

    def req(self, method, path, payload=None, *, token="test-token", origin=None, raw=None,
            content_type="application/json"):
        host, port = self.httpd.server_address
        conn = HTTPConnection(host, port, timeout=10)
        headers = {}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        if origin is not None:
            headers["Origin"] = origin
        body = raw if raw is not None else (json.dumps(payload).encode() if payload is not None else None)
        if body is not None:
            headers["Content-Type"] = content_type
            headers["Content-Length"] = str(len(body))
        conn.request(method, PREFIX + path, body=body, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
        out = (resp.status, json.loads(data) if data else {}, dict(resp.getheaders()))
        conn.close()
        return out


ALL_ROUTES = (
    ("POST", "/pairing/offers", {}), ("POST", f"/pairing/offers/{ID0}/accept", {"code": "x"}),
    ("POST", f"/pairing/offers/{ID0}/confirm", {"sas": "123456"}), ("POST", f"/pairing/offers/{ID0}/cancel", {}),
    ("POST", "/pairing/imports", {"code": "x"}), ("POST", f"/pairing/imports/{ID0}/reveal", {"code": "x"}),
    ("POST", f"/pairing/imports/{ID0}/confirm", {"sas": "123456"}), ("GET", "/pairing", None), ("GET", "/peers", None),
    ("DELETE", "/peers/peer-x", None),
)


class PairingOffRoutesTest(PairingRoutesBase):
    PAIRING = False

    def test_every_pairing_route_is_404_when_the_block_is_absent(self):
        for method, path, body in ALL_ROUTES:
            status, data, headers = self.req(method, path, body)
            self.assertEqual((status, data["error"]), (404, "not_found"), (method, path))
            self.assertEqual(headers.get("Cache-Control"), "no-store")
        self.assertFalse(self.A.journal_dir.exists())                       # nothing was created
        self.assertEqual(self.req("GET", "/paired-peers")[0], 200)          # the existing feature is untouched

    def test_404_precedes_the_origin_check_and_401_precedes_everything(self):
        self.assertEqual(self.req("GET", "/pairing", origin="https://evil.example")[0], 404)
        self.assertEqual(self.req("GET", "/pairing", token=None)[0], 401)
        self.assertEqual(self.req("DELETE", "/peers/peer-x", token="wrong")[0], 401)

    def test_invalid_blocks_disable_pairing_but_not_the_rest(self):
        a = self.A
        cases = {
            "wrong_peers_path": {"identity_dir": str(a.identity_dir), "journal_dir": str(a.journal_dir),
                                 "peers_path": str(self.tmp / "elsewhere.json")},
            "relative": {"identity_dir": "identity", "journal_dir": str(a.journal_dir), "peers_path": str(a.peers_path)},
            "same_dir": {"identity_dir": str(a.journal_dir), "journal_dir": str(a.journal_dir),
                         "peers_path": str(a.peers_path)},
            "missing_field": {"identity_dir": str(a.identity_dir), "journal_dir": str(a.journal_dir)},
            "extra_field": {"identity_dir": str(a.identity_dir), "journal_dir": str(a.journal_dir),
                            "peers_path": str(a.peers_path), "x": 1},
            "not_object": ["a"], "bad_type": {"identity_dir": 1, "journal_dir": 2, "peers_path": 3},
            "peers_in_journal_dir": {"identity_dir": str(a.identity_dir), "journal_dir": str(a.peers_path.parent),
                                     "peers_path": str(a.peers_path)},
        }
        for name, block in cases.items():
            self.write_config(block)
            self.assertEqual(self.req("GET", "/pairing")[0], 404, name)
            self.assertEqual(self.req("GET", "/paired-peers")[0], 200, name)
        # a symlinked or world-readable existing directory is refused, not repaired
        real = self.tmp / "real-journal"
        real.mkdir(mode=0o700)
        os.symlink(real, a.journal_dir)
        self.write_config({"identity_dir": str(a.identity_dir), "journal_dir": str(a.journal_dir),
                           "peers_path": str(a.peers_path)})
        self.assertEqual(self.req("GET", "/pairing")[0], 404)
        self.assertIsNone(runtime_config._parse_pairing(      # the CONFIG itself refuses it (not only the first use)
            {"identity_dir": str(a.identity_dir), "journal_dir": str(a.journal_dir), "peers_path": str(a.peers_path)},
            a.peers_path))
        os.unlink(a.journal_dir)
        a.journal_dir.mkdir()
        os.chmod(a.journal_dir, 0o755)
        self.assertEqual(self.req("GET", "/pairing")[0], 404)
        os.chmod(a.journal_dir, 0o700)
        self.assertEqual(self.req("GET", "/pairing")[0], 200)
        self.assertIsNone(runtime_config._parse_pairing(None, a.peers_path))


class PairingRoutesTest(PairingRoutesBase):
    def test_auth_origin_and_no_store_on_every_route(self):
        for method, path, body in ALL_ROUTES:
            status, _data, _headers = self.req(method, path, body, token=None)
            self.assertEqual(status, 401, (method, path))
            status, data, _headers = self.req(method, path, body, origin="https://evil.example")
            self.assertEqual((status, data), (403, {"error": "forbidden_origin"}), (method, path))
            status, _data, headers = self.req(method, path, body)
            self.assertNotEqual(status, 401)
            self.assertEqual(headers.get("Cache-Control"), "no-store", (method, path))
        os.environ["MISSION_CONTROL_ALLOWED_ORIGIN"] = "https://mc.example"
        self.assertEqual(self.req("GET", "/pairing", origin="https://mc.example")[0], 200)
        self.assertEqual(self.req("GET", "/pairing", origin="https://other.example")[0], 403)

    def test_unknown_methods_paths_and_ids_are_generic(self):
        self.assertEqual(self.req("GET", "/pairing/offers")[0], 404)
        self.assertEqual(self.req("PUT", "/pairing")[0], 404)
        self.assertEqual(self.req("POST", "/pairing/offers/not-an-id/accept", {"code": "x"})[0], 404)
        self.assertEqual(self.req("DELETE", "/peers/bad%20id")[0], 404)
        self.assertEqual(self.req("DELETE", "/pairing/peers/peer-x")[0], 404)   # design path is /peers/{id}
        for path, body in ((f"/pairing/offers/{ID0}/accept", {"code": "x" * 10}),
                           (f"/pairing/offers/{ID0}/confirm", {"sas": "123456"}),
                           (f"/pairing/offers/{ID0}/cancel", {}),
                           (f"/pairing/imports/{ID0}/confirm", {"sas": "123456"})):
            self.assertEqual(self.req("POST", path, body)[1].get("code"), "pairing_unavailable", path)
        self.assertEqual(self.req("DELETE", "/peers/peer-" + "9" * 32)[1],
                         {"error": "conflict", "code": "pairing_unavailable"})

    def test_body_validation_is_strict(self):
        offer = self.req("POST", "/pairing/offers", {})[1]["offer"]
        path = f"/pairing/offers/{offer['id']}/accept"
        for raw, ctype, code in ((b'{"code": "x", "code": "y"}', "application/json", "invalid_body"),
                                 (b'{"code": "x", "extra": 1}', "application/json", "invalid_body"),
                                 (b'{"code": 5}', "application/json", "invalid_body"),
                                 (b'{"code": ""}', "application/json", "invalid_body"),
                                 (b'{"code": "' + b"x" * 13000 + b'"}', "application/json", "invalid_body"),
                                 (b"[]", "application/json", "invalid_body"),
                                 (b"\xff", "application/json", "invalid_body"),
                                 (b'{"code": "x"}', "text/plain", "invalid_content_type")):
            status, data, _ = self.req("POST", path, raw=raw, content_type=ctype)
            self.assertEqual((status, data.get("code")), (400, code), raw[:20])
        self.assertEqual(self.req("POST", path, raw=b"x" * 20000)[0], 413)
        self.assertEqual(self.req("POST", "/pairing/offers", {"x": 1})[0], 400)
        for bad in ("12345", 123456, None, "abcdef"):
            status, _data, _ = self.req("POST", f"/pairing/offers/{offer['id']}/confirm", {"sas": bad})
            self.assertEqual(status, 400, bad)
        status, data, _ = self.req("POST", f"/pairing/offers/{offer['id']}/confirm", {"sas": "12345"})
        self.assertEqual(data, {"error": "invalid_request", "code": "pairing_sas_invalid"})
        self.assertEqual(self.req("POST", "/pairing/imports", {"code": "not-a-code"})[1],
                         {"error": "invalid_request", "code": "pairing_code_invalid"})

    def test_full_pairing_over_http_with_parity_and_unpair(self):
        status, data, _ = self.req("POST", "/pairing/offers", {})
        self.assertEqual(status, 200)
        offer = data["offer"]
        self.assertEqual(set(offer), {"id", "code", "expires_at"})
        self.assertIsInstance(offer["expires_at"], int)
        self.assertEqual(self.req("POST", "/pairing/offers", {})[1], {"error": "conflict", "code": "pairing_pending_exists"})
        accepted = self.b.import_offer(offer["code"])
        status, data, _ = self.req("POST", f"/pairing/offers/{offer['id']}/accept", {"code": accepted["code"]})
        self.assertEqual(status, 200, data)
        reveal = data["reveal"]
        self.assertEqual(set(reveal), {"id", "code", "sas", "expires_at"})
        b_sas = self.b.reveal_import(accepted["id"], reveal["code"])
        listing = self.req("GET", "/pairing")[1]
        self.assertTrue(listing["available"])
        self.assertEqual([r["state"] for r in listing["pairings"]], ["revealed"])
        self.assertEqual(set(listing["pairings"][0]), {"id", "role", "state", "expires_at", "peer"})
        self.assertEqual(set(listing["pairings"][0]["peer"]),
                         {"peer_id", "installation_id", "profile", "fingerprint", "policy_digest", "catalog_digest"})
        status, data, _ = self.req("POST", f"/pairing/offers/{offer['id']}/confirm", {"sas": b_sas["sas"]})
        self.assertEqual(status, 202, data)
        self.assertEqual(set(data), {"pairing", "ack", "code"})
        self.assertEqual((data["ack"], data["code"]), ("pending", "pairing_ack_pending"))
        self.assertEqual(data["pairing"]["state"], "confirmed")
        self.b.confirm_import(accepted["id"], reveal["sas"])
        self.assertEqual(self.req("POST", f"/pairing/offers/{offer['id']}/cancel", {})[0], 409)   # decided
        peers = self.req("GET", "/peers")[1]["peers"]
        self.assertEqual([(p["installation_id"], p["state"], p["generation"]) for p in peers],
                         [(ps.INSTALL_B, "confirmed", 1)])
        registry = runtime_config.build_peer_registry(self.A.peers_path)      # the very reader the sidecar uses
        peer_id = peers[0]["peer_id"]
        self.assertEqual(registry.lookup(peer_id).pairing_generation, 1)
        self.assertEqual(self.req("GET", "/paired-peers")[1]["peers"][0]["peer_id"], peer_id)
        status, data, headers = self.req("DELETE", f"/peers/{peer_id}")
        self.assertEqual((status, data["unpaired"], data["generation"]), (200, True, 2))
        self.assertIn("does NOT revoke", data["notice"])
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        self.assertEqual(self.req("GET", "/peers")[1], {"peers": []})
        self.assertEqual(self.req("GET", "/pairing")[1]["pairings"][0]["state"], "unpaired")
        self.assertEqual(self.A.peers(), [])
        self.assertEqual(self.req("GET", "/paired-peers")[1]["peers"], [])

    def test_wrong_sas_over_http_aborts_and_publishes_nothing(self):
        offer = self.req("POST", "/pairing/offers", {})[1]["offer"]
        accepted = self.b.import_offer(offer["code"])
        reveal = self.req("POST", f"/pairing/offers/{offer['id']}/accept", {"code": accepted["code"]})[1]["reveal"]
        wrong = "000000" if reveal["sas"] != "000000" else "000001"
        status, data, _ = self.req("POST", f"/pairing/offers/{offer['id']}/confirm", {"sas": wrong})
        self.assertEqual((status, data), (409, {"error": "conflict", "code": "pairing_sas_mismatch"}))
        self.assertEqual(self.A.peers(), [])
        self.assertEqual(self.req("GET", "/pairing")[1]["pairings"][0]["state"], "aborted")
        self.assertEqual(self.req("POST", f"/pairing/offers/{offer['id']}/confirm", {"sas": reveal["sas"]})[0], 409)

    def test_joining_side_over_http_matches_the_ui_contract_shapes(self):
        # The same server acts as the joining installation: the offer comes from the (direct) B service.
        b_svc = self.B.service()
        b_offer = b_svc.start_offer()
        status, data, _ = self.req("POST", "/pairing/imports", {"code": b_offer["code"]})
        self.assertEqual(status, 200, data)
        accept = data["accept"]
        self.assertEqual(set(accept), {"id", "code", "identity", "expires_at"})
        self.assertEqual(accept["identity"]["installation_id"], ps.INSTALL_B)
        reveal = b_svc.accept_offer(b_offer["id"], accept["code"])
        status, data, _ = self.req("POST", f"/pairing/imports/{accept['id']}/reveal", {"code": reveal["code"]})
        self.assertEqual(status, 200, data)
        self.assertEqual(set(data), {"id", "sas", "expires_at"})              # unwrapped, as the UI client reads it
        self.assertEqual(data["sas"], reveal["sas"])
        status, data, _ = self.req("POST", f"/pairing/imports/{accept['id']}/confirm", {"sas": reveal["sas"]})
        self.assertEqual((status, data["pairing"]["role"], data["pairing"]["state"]), (200, "joining", "confirmed"))
        self.assertEqual(self.A.peers(), [])                                  # joining role: no peers.json projection
        self.assertEqual(self.req("GET", "/peers")[1]["peers"][0]["role"], "joining")

    def test_retry_and_reuse_over_http(self):
        offer = self.req("POST", "/pairing/offers", {})[1]["offer"]
        accepted = self.b.import_offer(offer["code"])
        first = self.req("POST", f"/pairing/offers/{offer['id']}/accept", {"code": accepted["code"]})
        again = self.req("POST", f"/pairing/offers/{offer['id']}/accept", {"code": accepted["code"]})
        self.assertEqual((first[0], again[0]), (200, 200))
        self.assertEqual(first[1], again[1])                                  # same stored bytes, no new randomness
        self.assertEqual(self.req("POST", f"/pairing/offers/{offer['id']}/cancel", {})[0], 200)
        self.assertEqual(self.req("POST", f"/pairing/offers/{offer['id']}/confirm",
                                  {"sas": first[1]["reveal"]["sas"]})[1]["code"], "pairing_unavailable")

    def test_native_catalog_failure_is_generic_503(self):
        def broken(config, ctx):
            raise RuntimeError("secret-path /tmp/x token=abc")
        room_routes._PAIRING_CATALOG_SOURCE = broken
        status, data, _ = self.req("POST", "/pairing/offers", {})
        self.assertEqual((status, data), (503, {"error": "unavailable", "code": "pairing_native_unavailable"}))
        self.assertFalse(any("secret-path" in line for line in self.capture.lines))
        room_routes._PAIRING_CATALOG_SOURCE = lambda config, ctx: {"installation_id": ps.INSTALL_B}
        self.assertEqual(self.req("POST", "/pairing/offers", {})[1]["code"], "pairing_native_unavailable")

    def test_corrupt_journal_or_tampered_projection_looks_like_an_absent_feature(self):
        offer = self.req("POST", "/pairing/offers", {})[1]["offer"]
        jfile = self.A.journal_dir / "pairing.journal"
        good = jfile.read_bytes()
        jfile.write_bytes(good[:-5])
        for method, path, body in (("GET", "/pairing", None), ("POST", "/pairing/offers", {}), ("GET", "/peers", None),
                                   ("POST", f"/pairing/offers/{offer['id']}/cancel", {})):
            status, data, _ = self.req(method, path, body)
            self.assertEqual((status, data["error"]), (404, "not_found"), (method, path))
        self.assertEqual(jfile.read_bytes(), good[:-5])                       # never repaired
        self.assertTrue(any("journal_corrupt" in line for line in self.capture.lines))
        jfile.write_bytes(good)
        self.assertEqual(self.req("GET", "/pairing")[0], 200)
        self.A.peers_path.write_bytes(b'[{"peer_id": "peer-evil"}]')
        self.assertEqual(self.req("GET", "/pairing")[0], 404)
        self.assertEqual(self.A.peers_path.read_bytes(), b'[{"peer_id": "peer-evil"}]')

    def test_codes_and_sas_never_reach_the_logs(self):
        offer = self.req("POST", "/pairing/offers", {})[1]["offer"]
        accepted = self.b.import_offer(offer["code"])
        reveal = self.req("POST", f"/pairing/offers/{offer['id']}/accept", {"code": accepted["code"]})[1]["reveal"]
        b_sas = self.b.reveal_import(accepted["id"], reveal["code"])
        self.req("POST", f"/pairing/offers/{offer['id']}/confirm",
                 {"sas": "000000" if b_sas["sas"] != "000000" else "000001"})
        self.req("POST", "/pairing/imports", {"code": offer["code"]})                  # self-pairing refusal path
        self.req("POST", f"/pairing/offers/{offer['id']}/accept", {"code": "mcpair1.accept.AAAA.BBBB"})
        sess = self.A.service().journal.snapshot().sessions[offer["id"]]
        blob = "\n".join(self.capture.lines)
        for secret in (offer["code"], accepted["code"], reveal["code"], reveal["sas"], b_sas["sas"], sess["r_a"],
                       sess["nonce"], "mcpair1.", str(self.tmp)):
            self.assertNotIn(secret, blob)

    def test_get_does_not_expose_codes_or_secrets(self):
        offer = self.req("POST", "/pairing/offers", {})[1]["offer"]
        accepted = self.b.import_offer(offer["code"])
        reveal = self.req("POST", f"/pairing/offers/{offer['id']}/accept", {"code": accepted["code"]})[1]["reveal"]
        text = json.dumps(self.req("GET", "/pairing")[1]) + json.dumps(self.req("GET", "/peers")[1])
        sess = self.A.service().journal.snapshot().sessions[offer["id"]]
        for secret in (offer["code"], accepted["code"], reveal["code"], reveal["sas"], sess["r_a"], "mcpair1."):
            self.assertNotIn(secret, text)

    def test_busy_journal_is_503_not_a_hang(self):
        import fcntl
        self.req("GET", "/pairing")
        fd = os.open(self.A.journal_dir / ".pairing.lock", os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX)
        old = pj.Journal.__init__.__kwdefaults__["lock_wait"]
        pj.Journal.__init__.__kwdefaults__["lock_wait"] = 0.2            # shorten the bounded wait for the test
        try:
            status, data, _ = self.req("POST", "/pairing/offers", {})
        finally:
            pj.Journal.__init__.__kwdefaults__["lock_wait"] = old
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        self.assertEqual((status, data), (503, {"error": "unavailable", "code": "pairing_busy"}))


if __name__ == "__main__":
    unittest.main()
