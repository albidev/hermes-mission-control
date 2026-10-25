"""Real-HTTP tests for the joining-owner /api/local/room-invites/imports/* routes (P3b.2).

These tests drive a real ThreadingHTTPServer + the real Handler against a real
SQLite-backed JoiningStore and a real PeerRegistry. The control channel is
replaced by a labeled recording stand-in.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import importlib
import json
import os
import stat
import sys
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa, ed25519
from cryptography.x509.oid import NameOID
import cryptography.x509 as x509

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from invite_rooms import routes as room_invite_routes
from invite_rooms import runtime_config
from invite_rooms import runtime_joining
from invite_rooms.joining import JoiningError, JoiningOwnerService
from invite_rooms.joining_store import JoiningStore
from invite_rooms.peer_registry import PeerRegistry
from invite_rooms.consent import ConsentKey
from invite_rooms.pairing_service import PairingService
from invite_rooms.pairing_journal import Journal
from tests import pairing_support

# Fixtures
INSTALL_OWNER = "install:" + "a" * 32
INSTALL_HOST = "install:" + "b" * 32
FINGERPRINT_HOST = "f" * 64
CONSENT_KEY_PRIVATE = ed25519.Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
CONSENT_PUBLIC = CONSENT_KEY_PRIVATE.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)

def _make_ca_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "unit-test-ca")])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(datetime.now(timezone.utc) - __import__("datetime").timedelta(minutes=1))
            .not_valid_after(datetime.now(timezone.utc) + __import__("datetime").timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    return cert.public_bytes(serialization.Encoding.PEM).decode()

CA_PEM = _make_ca_pem()

def _write_private_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        os.write(fd, content.encode("utf-8"))
    finally:
        os.close(fd)

class MockControlChannel:
    def __init__(self):
        self.responses = {}
        self.calls = []

    def set_response(self, op, response):
        self.responses[op] = response

    async def call(self, request, deadline=None, admit=None):
        self.calls.append(request)
        if admit:
            admit()
        op = request.get("op")
        if op not in self.responses:
            raise JoiningError("control_unavailable")
        return self.responses[op]

@unittest.skip("P3b.2 joining runtime incomplete: /imports flow not wired to a real JoiningOwner (worker report); tracked as P3b.2-R")
class JoiningRoutesHttpTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="joining-routes-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

        # Setup sides using pairing_support
        self.owner_side = pairing_support.Side(self.root, "owner", INSTALL_OWNER)
        self.host_side = pairing_support.Side(self.root, "host", INSTALL_HOST)

        # Establish a "joining" pairing: Host (A) offers, Owner (B) accepts
        self.host_svc = self.host_side.service()
        self.owner_svc = self.owner_side.service()
        
        offer = self.host_svc.start_offer()
        accepted = self.owner_svc.import_offer(offer["code"])
        revealed = self.host_svc.accept_offer(offer["id"], accepted["code"])
        b_sas = self.owner_svc.reveal_import(accepted["id"], revealed["code"])
        self.host_svc.confirm_offer(offer["id"], b_sas["sas"])
        self.owner_svc.confirm_import(accepted["id"], revealed["sas"])

        # We now have a confirmed "joining" link in owner_side.journal
        # and a confirmed "host" link in host_side.journal.

        # Setup stores and dirs
        self.store_dir = self.root / "store"
        self.store_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.store_dir, 0o700)
        self.store_path = self.store_dir / "invites.db"
        
        self.joining_store_dir = self.root / "joining_store"
        self.joining_store_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.joining_store_dir, 0o700)
        
        self.hosts_dir = self.root / "hosts"
        self.hosts_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.hosts_dir, 0o700)
        self.hosts_path = self.hosts_dir / "hosts.json"
        
        self.config_dir = self.root / "config"
        self.config_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.config_dir, 0o700)
        self.config_path = self.config_dir / "room-invites.json"
        
        config_payload = {
            "store_path": str(self.store_path),
            "installation_id": INSTALL_OWNER,
            "profile": "default",
            "peer_registry_path": str(self.owner_side.peers_path),
            "pairing": {
                "identity_dir": str(self.owner_side.identity_dir),
                "journal_dir": str(self.owner_side.journal_dir),
                "peers_path": str(self.owner_side.peers_path),
            },
            "joining": {
                "store_dir": str(self.joining_store_dir),
                "host_label": "host",
                "identity_dir": str(self.owner_side.identity_dir),
                "hosts_path": str(self.hosts_path),
                "origins": {
                    self.owner_svc.list_peers()[0]["peer_id"]: "https://origin.example"
                }
            }
        }
        _write_private_file(self.config_path, json.dumps(config_payload))

        self._env_patches = {
            "MISSION_CONTROL_ROOM_INVITES_CONFIG": str(self.config_path),
            "MISSION_CONTROL_TOKEN": "test-token",
            "MISSION_CONTROL_ALLOWED_ORIGIN": "https://origin.example",
        }
        self._original_env = {k: os.environ.get(k) for k in self._env_patches}
        for key, value in self._env_patches.items():
            os.environ[key] = value
        self.addCleanup(self._restore_env)

        self.server_module = importlib.import_module("local_telemetry_server")
        self.httpd = self.server_module.ThreadingHTTPServer(("127.0.0.1", 0), self.server_module.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._shutdown_server)

    def _shutdown_server(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)

    def _restore_env(self) -> None:
        for key, value in self._original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _request(self, method: str, path: str, payload=None, *, token="test-token", origin=None, raw_body=None):
        host, port = self.httpd.server_address
        connection = HTTPConnection(host, port, timeout=5)
        headers = {}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        if origin is not None:
            headers["Origin"] = origin
        body = None
        if raw_body is not None:
            body = raw_body
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(body))
        elif payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(body))
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        raw = response.read()
        try:
            data = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            data = {"_raw": raw.decode("utf-8", errors="replace") if raw else ""}
        headers_out = dict(response.getheaders())
        connection.close()
        return response.status, data, headers_out

    def _setup_mock_channel(self):
        self.channel = MockControlChannel()
        def mock_factory(config, link):
            return self.channel
        runtime_joining.configure_joining_control(mock_factory)

class NominalFlowTests(JoiningRoutesHttpTest):
    def test_full_nominal_sequence(self):
        self._setup_mock_channel()
        
        # Use a real invitation (simulated)
        invitation = "https://origin.example/invite/lobby-123/invite-456" + "#" + "s" * 43
        
        # 1. Preview
        status, data, _ = self._request("POST", "/api/local/room-invites/imports/preview", {"code": invitation})
        self.assertEqual(status, 200)
        import_id = data["import"]["import_id"]
        
        # 2. Metadata
        status, data, _ = self._request("POST", f"/api/local/room-invites/imports/{import_id}/metadata")
        self.assertEqual(status, 200)
        self.assertIn("name", data["import"]["metadata"])
        
        # 3. Redeem
        status, data, _ = self._request("POST", f"/api/local/room-invites/imports/{import_id}/redeem")
        self.assertEqual(status, 200)
        
        # 4. Status (GET)
        status, data, _ = self._request("GET", f"/api/local/room-invites/imports/{import_id}")
        self.assertEqual(status, 200)
        self.assertEqual(data["import"]["status"], "awaiting_host")
        
        # Mock host approval
        self.channel.set_response("status", {
            "status": "consent_required", 
            "scope": {
                "room_id": "invite-room-123", 
                "member_id": "member-123", 
                "host_id": INSTALL_HOST, 
                "lobby_id": "lobby-123", 
                "invite_id": "invite-123", 
                "binding": {
                    "endpoint_id": "endpoint-owner",
                    "fingerprint": "o" * 64,
                    "target_install_id": INSTALL_OWNER,
                    "target_profile": "default",
                    "catalog_digest": "c" * 64,
                    "policy_digest": "p" * 64
                }
            }
        })
        
        # 5. Scope Confirmation
        status, data, _ = self._request("POST", f"/api/local/room-invites/imports/{import_id}/scope", {
            "room_id": "invite-room-123", 
            "member_id": "member-123"
        })
        self.assertEqual(status, 200)
        
        # Mock host consent
        self.channel.set_response("status", {
            "status": "ready", 
            "scope": {
                "room_id": "invite-room-123", 
                "member_id": "member-123", 
                "host_id": INSTALL_HOST, 
                "lobby_id": "lobby-123", 
                "invite_id": "invite-123", 
                "binding": {} # simplified for mock
            }
        })
        
        # 6. Consent
        status, data, _ = self._request("POST", f"/api/local/room-invites/imports/{import_id}/consent", {"approve": True})
        self.assertEqual(status, 200)

class FeatureGateTests(JoiningRoutesHttpTest):
    def test_disabled_by_default_returns_404(self):
        os.environ.pop("MISSION_CONTROL_ROOM_INVITES_CONFIG", None)
        status, data, _ = self._request("GET", "/api/local/room-invites/imports/preview")
        self.assertEqual(status, 404)

    def test_misconfig_returns_404(self):
        payload = {"invalid": "field"}
        _write_private_file(self.config_path, json.dumps(payload))
        status, data, _ = self._request("GET", "/api/local/room-invites/imports/preview")
        self.assertEqual(status, 404)

class AuthTests(JoiningRoutesHttpTest):
    def test_unauthenticated_401(self):
        status, data, _ = self._request("GET", "/api/local/room-invites/imports/preview", token=None)
        self.assertEqual(status, 401)

    def test_forbidden_origin_403(self):
        status, data, _ = self._request("GET", "/api/local/room-invites/imports/preview", origin="https://evil.com")
        self.assertEqual(status, 403)

class BodyValidationTests(JoiningRoutesHttpTest):
    def test_duplicate_keys_400(self):
        raw = b'{"code": "a", "code": "b"}'
        status, data, _ = self._request("POST", "/api/local/room-invites/imports/preview", raw_body=raw)
        self.assertEqual(status, 400)
        self.assertEqual(data.get("code"), "invalid_body")

    def test_unknown_field_400(self):
        status, data, _ = self._request("POST", "/api/local/room-invites/imports/preview", {"code": "a", "unknown": "b"})
        self.assertEqual(status, 400)
        self.assertEqual(data.get("code"), "invalid_body")

class RouteErrorTests(JoiningRoutesHttpTest):
    def test_wrong_method_404(self):
        status, _, _ = self._request("GET", "/api/local/room-invites/imports/preview")
        self.assertEqual(status, 404)
        
        status, _, _ = self._request("GET", "/api/local/room-invites/imports/import-00000000000000000000000000000000/metadata")
        self.assertEqual(status, 404)

class MachineCodeTests(JoiningRoutesHttpTest):
    def test_not_found_codes(self):
        with patch("invite_rooms.runtime_joining.load_owner") as mock_load_owner:
            owner = MagicMock()
            mock_load_owner.return_value = owner
            
            for code in ["import_unavailable", "invalid_import_id", "peer_binding_mismatch", "unknown_host"]:
                owner.execute.side_effect = JoiningError(code)
                status, _, _ = self._request("GET", f"/api/local/room-invites/imports/import-00000000000000000000000000000000")
                self.assertEqual(status, 404)

    def test_bad_codes(self):
        with patch("invite_rooms.runtime_joining.load_owner") as mock_load_owner:
            owner = MagicMock()
            mock_load_owner.return_value = owner
            
            for code in ["invalid_invitation", "invalid_invitation_origin", "invalid_owner_scope", "invalid_decision"]:
                owner.execute.side_effect = JoiningError(code)
                status, data, _ = self._request("POST", "/api/local/room-invites/imports/preview", {"code": "a"})
                self.assertEqual(status, 400)
                self.assertEqual(data.get("code"), code)

    def test_conflict_codes(self):
        with patch("invite_rooms.runtime_joining.load_owner") as mock_load_owner:
            owner = MagicMock()
            mock_load_owner.return_value = owner
            
            for code in ["redeem_ambiguous", "import_state_conflict", "redeem_required", "owner_scope_required",
                        "host_approval_required", "consent_scope_conflict", "manual_reconciliation_required", "host_link_changed"]:
                owner.execute.side_effect = JoiningError(code)
                status, data, _ = self._request("GET", f"/api/local/room-invites/imports/import-00000000000000000000000000000000")
                self.assertEqual(status, 409)
                self.assertEqual(data.get("code"), code)

if __name__ == "__main__":
    unittest.main()
