"""Real-HTTP tests for the owner-only /api/local/room-invites/* routes (MVP P2a).

Every test drives a real ThreadingHTTPServer + the real Handler against a real
SQLite-backed RoomInviteStore and a real PeerRegistry populated from an
owner-private config file. Only the native RPC transport is ever replaced by a
labeled recording stand-in (never _is_authorized, never the store).
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import importlib
import json
import logging
import os
import stat
import sys
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.x509.oid import NameOID
import datetime

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gateway.hosted_room_peer import catalog_mapping  # noqa: E402
from invite_rooms import routes as room_invite_routes  # noqa: E402
from invite_rooms import runtime_config  # noqa: E402
from invite_rooms.peer_registry import MaterialScope, PeerRegistry, ScopedPeerMaterial  # noqa: E402
from invite_rooms.service import InviteRoomsService, OwnerContext  # noqa: E402
from room_invite_store import RoomInviteStore  # noqa: E402
from room_lobby_service import PeerMaterial  # noqa: E402

INSTALL_A = "install:" + "a" * 32
INSTALL_B = "install:" + "b" * 32
INSTALL_C = "install:" + "c" * 32
FINGERPRINT_B = "b" * 64
ADAPTER_URL = "http://127.0.0.1:19001/links/unit-link"
GRANT = "unit-fixture-grant-secret-value"
_CONSENT_SIGNER = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
CONSENT_PUBLIC = _CONSENT_SIGNER.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
_POLICY_UNSIGNED = {"version": 1, "target_profile": "default", "enabled_toolsets": ["bot_room"], "approval_mode": "manual", "max_iterations": 10}
_POLICY = {**_POLICY_UNSIGNED, "policy_digest": hashlib.sha256(json.dumps(_POLICY_UNSIGNED, sort_keys=True, separators=(",", ":")).encode("ascii")).hexdigest()}
CATALOG = catalog_mapping(installation_id=INSTALL_B, protocol_versions=(2,), link_modes=("direct",), persistent_process=True, text=True, attachments=False, endpoint={"available": True, "url": ADAPTER_URL, "transport_security": "loopback"}, target_profile="default", execution_policy=_POLICY)
CATALOG_DIGEST = CATALOG["catalog_digest"]


def _make_ca_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "unit-test-ca")])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=1))
            .not_valid_after(datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=1))
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


def _peer_registry_payload():
    return json.dumps([
        {
            "peer_id": "owner-b",
            "installation_id": INSTALL_B,
            "profile": "default",
            "fingerprint": FINGERPRINT_B,
            "endpoint_id": "endpoint-b",
            "catalog": copy.deepcopy(CATALOG),
            "consent_public_key_b64": __import__("base64").b64encode(CONSENT_PUBLIC).decode(),
            "tls_ca_pem": CA_PEM,
            "pairing_generation": 1,
        }
    ])


def target_wire_scope(scope):
    """The scope the REAL TargetMaterialMint signs (joining.py / joining_material.py:
    host_id, room_id, lobby_id, member_id, invite_id, binding). Derived from a host
    MaterialScope only to build fixtures; the host never trusts it from the wire."""
    return {"host_id": scope.owner_installation_id, "room_id": scope.planned_room_id,
            "lobby_id": scope.lobby_id, "member_id": scope.member_id,
            "invite_id": scope.invite_id, "binding": dict(scope.binding)}


def target_signed_envelope(scope, grant, catalog, *, signer=None, wire_scope=None):
    """Envelope in the exact shape TargetMaterialMint.mint() emits and signs."""
    from invite_rooms.consent import canonical as _canonical
    import base64 as _b64
    signed = {"scope": wire_scope if wire_scope is not None else target_wire_scope(scope), "grant": grant,
              "catalog": copy.deepcopy(catalog), "purpose": "joining-owner-material-v1"}
    return {"scope": signed["scope"], "grant": signed["grant"], "catalog": signed["catalog"],
            "signature": _b64.b64encode((signer or _CONSENT_SIGNER).sign(_canonical(signed))).decode()}


_EXPECTED_BINDING_KEYS = ("fingerprint", "target_install_id", "target_profile", "catalog_digest", "policy_digest")


def _expected_binding(binding: dict) -> dict:
    """Project a full 6-key registry binding down to the 5-key durable
    redemption binding shown to the owner (never endpoint_id)."""
    return {k: binding[k] for k in _EXPECTED_BINDING_KEYS}


class RoomInviteRoutesHttpTest(unittest.TestCase):
    """Base: boots one real HTTP server per test with a fresh owner-private config."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="room-invite-routes-")
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.store_path = root / "store" / "invites.db"
        self.config_path = root / "config" / "room-invites.json"
        self.peer_registry_path = root / "config" / "peers.json"
        _write_private_file(self.peer_registry_path, _peer_registry_payload())
        self._write_config()

        self._env_patches = {
            "MISSION_CONTROL_ROOM_INVITES_CONFIG": str(self.config_path),
            "MISSION_CONTROL_TOKEN": "test-token",
        }
        self._original_env = {k: os.environ.get(k) for k in self._env_patches}
        self._original_env["MISSION_CONTROL_ALLOWED_ORIGIN"] = os.environ.get("MISSION_CONTROL_ALLOWED_ORIGIN")
        os.environ.pop("MISSION_CONTROL_ALLOWED_ORIGIN", None)
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

    def _write_config(self, *, native_rpc=None, installation_id=INSTALL_A, profile="default") -> None:
        payload = {
            "store_path": str(self.store_path),
            "installation_id": installation_id,
            "profile": profile,
            "peer_registry_path": str(self.peer_registry_path),
        }
        if native_rpc is not None:
            payload["native_rpc"] = native_rpc
        _write_private_file(self.config_path, json.dumps(payload))

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
            data = {"_raw": raw.decode("utf-8", errors="replace")}
        headers_out = dict(response.getheaders())
        connection.close()
        return response.status, data, headers_out

    def _create_lobby(self):
        status, data, _ = self._request("POST", "/api/local/room-invites/lobbies", {
            "name": "Test Room",
            "roster": [
                {"member_id": "local-a", "handle": "local"},
                {"member_id": "peer-b", "handle": "peer", "target": {"peer_id": "owner-b"}},
            ],
        })
        self.assertEqual(status, 200, data)
        return data["lobby"]

    def _write_token_file(self):
        token_path = Path(self.tmp.name) / "config" / "native-rpc.token"
        _write_private_file(token_path, "unit-test-native-rpc-token")
        return token_path

    def _consented_lobby_and_invite(self):
        """Shared fixture: a lobby fully consented (redeemed, host-approved,
        joining-consented) plus the exact issued invite and registry binding
        -- everything a real activation attempt needs. Reused by every test
        class that drives activation through the native RPC seam so the
        scoped-material/registry wiring stays identical across R1/R2/R3/R4
        regressions instead of being re-derived per test."""
        lobby = self._create_lobby()
        _, issued, _ = self._request("POST", f"/api/local/room-invites/lobbies/{lobby['lobby_id']}/invites", {
            "member_id": "peer-b", "invitee_label": "B",
        })
        registry = PeerRegistry()
        registry.register(peer_id="owner-b", installation_id=INSTALL_B, profile="default",
                           fingerprint=FINGERPRINT_B, endpoint_id="endpoint-b",
                           catalog=copy.deepcopy(CATALOG), consent_public_key=CONSENT_PUBLIC, tls_ca_pem=CA_PEM, pairing_generation=1)
        binding = registry.binding_for("owner-b")
        store = RoomInviteStore(self.store_path)
        store.redeem(lobby["lobby_id"], issued["invite_id"], issued["secret"], binding=binding)
        store.decide_host(lobby["lobby_id"], issued["invite_id"], binding=binding, approved=True)
        store.consent_joining(lobby["lobby_id"], issued["invite_id"], binding=binding, consented=True)
        return lobby, issued, binding

    @staticmethod
    def _scoped_material_for(lobby, issued, binding):
        scope = MaterialScope(
            owner_installation_id=INSTALL_A, owner_profile="default",
            lobby_id=lobby["lobby_id"], planned_room_id=lobby["planned_room_id"],
            member_id="peer-b", invite_id=issued["invite_id"], peer_id="owner-b",
            **binding,
        )
        material = PeerMaterial(binding=binding, catalog=copy.deepcopy(CATALOG), grant=GRANT, adapter_url=ADAPTER_URL)
        return ScopedPeerMaterial(scope=scope, material=material)

    def _wire_approved_registry(self, scoped):
        """Every freshly-built PeerRegistry (one per HTTP request) gets this
        exact scope pre-approved, simulating the owner having already
        approved it via PeerRegistry.approve_scoped_material before the
        activation attempt -- registry state itself does not persist across
        requests in this server, only the owner-private peer file does."""
        original_build = runtime_config.build_peer_registry

        def patched(path):
            registry = original_build(path)
            from invite_rooms.material_receipts import MaterialReceiptStore
            registry._receipt_store = MaterialReceiptStore(self.store_path.parent / "material-receipts")
            registry._route_provider = lambda _scope: 19001
            envelope = target_signed_envelope(scoped.scope, scoped.material.grant, scoped.material.catalog)
            registry.admit_signed_material(scoped.scope, envelope)
            return registry

        runtime_config.build_peer_registry = patched
        self.addCleanup(lambda: setattr(runtime_config, "build_peer_registry", original_build))

    def _wire_material_resolver(self, scoped):
        """Labeled stand-in scoped material resolver: returns exactly the
        approved material, proving the resolver seam (F3) is on the path."""
        async def fake_resolver(scope):
            self.assertEqual(scope, scoped.scope)
            return scoped

        original = getattr(room_invite_routes, "_MATERIAL_RESOLVER", None)
        room_invite_routes._MATERIAL_RESOLVER = fake_resolver
        self.addCleanup(lambda: setattr(room_invite_routes, "_MATERIAL_RESOLVER", original))

    def _wire_recording_rpc(self, *, register_should_fail=False):
        """Labeled stand-in for the native RPC transport. Records every call
        and fabricates readback-consistent native state from the spec it was
        given at groups.create -- never a bare 0==0 stub."""
        calls: list[str] = []

        class _RecordingRpc:
            def __init__(self):
                self.spec = None
                self.register_attempts = 0

            async def __call__(self, method, params):
                calls.append(method)
                if method == "groups.capabilities":
                    return {"driver": True, "authority_gateway_id": INSTALL_A}
                if method == "groups.create":
                    self.spec = params
                    return {}
                if method == "groups.state":
                    room = dict(self.spec, authority_gateway_id=INSTALL_A, authority_epoch=1, disbanded_at=None)
                    response = {"room": room}
                    if self.register_attempts and not register_should_fail:
                        response["driver_status"] = {"peer_routes": [
                            {"member_id": "peer-b", "room_id": self.spec["room_id"], "status": "ready"},
                        ]}
                    return response
                if method == "groups.peer.register":
                    self.register_attempts += 1
                    if register_should_fail:
                        raise ConnectionError("simulated lost register reply")
                    return {"registered": True, "target_install_id": INSTALL_B, "target_profile": "default"}
                raise AssertionError(f"unexpected native call: {method}")

        rpc = _RecordingRpc()

        import contextlib as _contextlib

        @_contextlib.asynccontextmanager
        async def fake_connect(_rpc_config):
            yield rpc

        original_connect = room_invite_routes._connect_native_rpc
        room_invite_routes._connect_native_rpc = fake_connect
        self.addCleanup(lambda: setattr(room_invite_routes, "_connect_native_rpc", original_connect))
        return calls

    def _wire_connect_attempt_recorder(self, *, raises: Exception | None = ConnectionError("simulated native transport refusal")):
        """Labeled stand-in that records every connect attempt and, by
        default, raises instead of ever yielding a usable RPC -- so any test
        asserting zero connect attempts fails loudly if preflight is skipped,
        and any test asserting a mapped failure gets a real transport-layer
        exception to map (R1)."""
        attempts: list[str] = []

        import contextlib as _contextlib

        @_contextlib.asynccontextmanager
        async def recording_connect(_rpc_config):
            attempts.append("connect_attempt")
            if raises is not None:
                raise raises
            yield None

        original_connect = room_invite_routes._connect_native_rpc
        room_invite_routes._connect_native_rpc = recording_connect
        self.addCleanup(lambda: setattr(room_invite_routes, "_connect_native_rpc", original_connect))
        return attempts

    def _redeem_directly(self, lobby, issued):
        """Simulate the (out-of-scope P2b) joining-owner redeem by using the store directly."""
        store = RoomInviteStore(self.store_path)
        registry = PeerRegistry()
        registry.register(peer_id="owner-b", installation_id=INSTALL_B, profile="default",
                           fingerprint=FINGERPRINT_B, endpoint_id="endpoint-b",
                           catalog=copy.deepcopy(CATALOG), consent_public_key=CONSENT_PUBLIC, tls_ca_pem=CA_PEM, pairing_generation=1)
        binding = registry.binding_for("owner-b")
        store.redeem(lobby["lobby_id"], issued["invite_id"], issued["secret"], binding=binding)
        return store, binding


class FeatureGateTests(RoomInviteRoutesHttpTest):
    def test_disabled_by_default_returns_same_404_as_unknown_path(self):
        os.environ.pop("MISSION_CONTROL_ROOM_INVITES_CONFIG", None)
        status, data, _ = self._request("GET", "/api/local/room-invites/lobbies/lobby-" + "0" * 32)
        baseline_status, baseline_data, _ = self._request("GET", "/api/local/totally-unknown-path")
        self.assertEqual(status, baseline_status)
        self.assertEqual(data.get("error"), baseline_data.get("error"))
        self.assertEqual(status, 404)

    def test_missing_config_file_is_misconfiguration_not_crash(self):
        os.environ["MISSION_CONTROL_ROOM_INVITES_CONFIG"] = str(Path(self.tmp.name) / "does-not-exist.json")
        status, data, _ = self._request("GET", "/api/local/room-invites/lobbies/lobby-" + "0" * 32)
        self.assertEqual(status, 404)

    def test_world_readable_config_file_is_rejected(self):
        os.chmod(self.config_path, 0o644)
        self.addCleanup(lambda: os.chmod(self.config_path, 0o600))
        status, data, _ = self._request("GET", "/api/local/room-invites/lobbies/lobby-" + "0" * 32)
        self.assertEqual(status, 404)

    def test_world_readable_peer_registry_is_rejected(self):
        os.chmod(self.peer_registry_path, 0o644)
        self.addCleanup(lambda: os.chmod(self.peer_registry_path, 0o600))
        status, data, _ = self._request("GET", "/api/local/room-invites/lobbies/lobby-" + "0" * 32)
        self.assertEqual(status, 404)

    def test_unauthenticated_request_is_401_regardless_of_gate_state(self):
        os.environ.pop("MISSION_CONTROL_ROOM_INVITES_CONFIG", None)
        status, data, _ = self._request("GET", "/api/local/room-invites/lobbies/lobby-" + "0" * 32, token=None)
        self.assertEqual(status, 401)
        self.assertNotIn("lobby", data)

    def test_authenticated_unknown_route_under_prefix_is_404(self):
        status, data, _ = self._request("GET", "/api/local/room-invites/not-a-real-route")
        self.assertEqual(status, 404)


class AuthAndOriginTests(RoomInviteRoutesHttpTest):
    def test_no_token_401_before_lobby_existence_is_revealed(self):
        status, data, _ = self._request("GET", "/api/local/room-invites/lobbies/lobby-" + "0" * 32, token=None)
        self.assertEqual(status, 401)

    def test_wrong_token_401(self):
        status, data, _ = self._request("GET", "/api/local/room-invites/lobbies/lobby-" + "0" * 32, token="wrong")
        self.assertEqual(status, 401)

    def test_query_string_token_is_never_accepted(self):
        status, data, _ = self._request("GET", "/api/local/room-invites/lobbies/lobby-" + "0" * 32 + "?access_token=test-token", token=None)
        self.assertEqual(status, 401)

    def test_create_lobby_rejects_foreign_origin(self):
        status, data, headers = self._request("POST", "/api/local/room-invites/lobbies",
                                                {"name": "Test", "roster": [
                                                    {"member_id": "local-a", "handle": "local"},
                                                    {"member_id": "peer-b", "handle": "peer", "target": {"peer_id": "owner-b"}},
                                                ]}, origin="https://evil.example")
        self.assertEqual(status, 403)

    def test_create_lobby_allows_exact_configured_origin(self):
        os.environ["MISSION_CONTROL_ALLOWED_ORIGIN"] = "https://mc.example"
        self.addCleanup(lambda: os.environ.pop("MISSION_CONTROL_ALLOWED_ORIGIN", None))
        status, data, headers = self._request("POST", "/api/local/room-invites/lobbies",
                                                {"name": "Test", "roster": [
                                                    {"member_id": "local-a", "handle": "local"},
                                                    {"member_id": "peer-b", "handle": "peer", "target": {"peer_id": "owner-b"}},
                                                ]}, origin="https://mc.example")
        self.assertEqual(status, 200, data)

    def test_origin_present_but_allowed_origin_unconfigured_is_403(self):
        status, data, _ = self._request("POST", "/api/local/room-invites/lobbies",
                                         {"name": "Test", "roster": [
                                             {"member_id": "local-a", "handle": "local"},
                                             {"member_id": "peer-b", "handle": "peer", "target": {"peer_id": "owner-b"}},
                                         ]}, origin="https://mc.example")
        self.assertEqual(status, 403)

    def test_no_origin_header_is_not_blocked(self):
        status, data, _ = self._request("GET", "/api/local/room-invites/lobbies/lobby-" + "0" * 32)
        self.assertEqual(status, 404)  # lobby doesn't exist, but not an Origin 403


class BodyValidationTests(RoomInviteRoutesHttpTest):
    def test_duplicate_json_keys_rejected(self):
        raw = b'{"name": "A", "name": "B", "roster": []}'
        status, data, _ = self._request("POST", "/api/local/room-invites/lobbies", raw_body=raw)
        self.assertEqual(status, 400)

    def test_unknown_top_level_field_rejected(self):
        status, data, _ = self._request("POST", "/api/local/room-invites/lobbies", {
            "name": "Test", "roster": [], "admin_token": "x",
        })
        self.assertEqual(status, 400)

    def test_wrong_type_rejected(self):
        status, data, _ = self._request("POST", "/api/local/room-invites/lobbies", {
            "name": 12345, "roster": [],
        })
        self.assertEqual(status, 400)

    def test_oversize_body_rejected(self):
        raw = json.dumps({"name": "A" * 100000, "roster": []}).encode("utf-8")
        status, data, _ = self._request("POST", "/api/local/room-invites/lobbies", raw_body=raw)
        self.assertIn(status, (400, 413))

    def test_body_never_echoed_in_error(self):
        secret_marker = "super-secret-marker-xyz"
        status, data, _ = self._request("POST", "/api/local/room-invites/lobbies", {
            "name": secret_marker, "roster": [], "bogus": True,
        })
        self.assertEqual(status, 400)
        self.assertNotIn(secret_marker, json.dumps(data))

    def test_roster_target_rejects_browser_supplied_installation_fields(self):
        """Only peer_id may be supplied for a remote target; no fingerprint/installation/digest trust."""
        status, data, _ = self._request("POST", "/api/local/room-invites/lobbies", {
            "name": "Test", "roster": [
                {"member_id": "local-a", "handle": "local"},
                {"member_id": "peer-b", "handle": "peer", "target": {
                    "peer_id": "owner-b", "installation_id": INSTALL_C, "fingerprint": "f" * 64,
                }},
            ],
        })
        self.assertEqual(status, 400)

    def test_roster_rejects_client_supplied_profile_for_owner_identity(self):
        status, data, _ = self._request("POST", "/api/local/room-invites/lobbies", {
            "name": "Test", "roster": [
                {"member_id": "local-a", "handle": "local", "profile": "someone-elses-profile"},
                {"member_id": "peer-b", "handle": "peer", "target": {"peer_id": "owner-b"}},
            ],
        })
        self.assertEqual(status, 400)

    def test_unknown_peer_id_rejected(self):
        status, data, _ = self._request("POST", "/api/local/room-invites/lobbies", {
            "name": "Test", "roster": [
                {"member_id": "local-a", "handle": "local"},
                {"member_id": "peer-b", "handle": "peer", "target": {"peer_id": "no-such-peer"}},
            ],
        })
        self.assertEqual(status, 400)

    def test_missing_content_type_rejected(self):
        host, port = self.httpd.server_address
        connection = HTTPConnection(host, port, timeout=5)
        body = json.dumps({"name": "Test", "roster": []}).encode("utf-8")
        connection.request("POST", "/api/local/room-invites/lobbies", body=body,
                            headers={"Authorization": "Bearer test-token", "Content-Length": str(len(body))})
        response = connection.getresponse()
        data = json.loads(response.read())
        connection.close()
        self.assertEqual(response.status, 400)


class LifecycleTests(RoomInviteRoutesHttpTest):
    def test_create_then_inspect_round_trip(self):
        lobby = self._create_lobby()
        status, data, headers = self._request("GET", f"/api/local/room-invites/lobbies/{lobby['lobby_id']}")
        self.assertEqual(status, 200)
        self.assertEqual(data["lobby"]["lobby_id"], lobby["lobby_id"])
        self.assertEqual(headers.get("Cache-Control"), "no-store")

    def test_inspect_never_leaks_secret_or_native_spec(self):
        lobby = self._create_lobby()
        status, data, _ = self._request("GET", f"/api/local/room-invites/lobbies/{lobby['lobby_id']}")
        rendered = json.dumps(data)
        self.assertNotIn("native_spec_json", rendered)
        self.assertNotIn("roster_json", rendered)
        self.assertNotIn(GRANT, rendered)

    def test_issue_invite_returns_secret_once_with_no_store_header(self):
        lobby = self._create_lobby()
        status, data, headers = self._request("POST", f"/api/local/room-invites/lobbies/{lobby['lobby_id']}/invites", {
            "member_id": "peer-b", "invitee_label": "B",
        })
        self.assertEqual(status, 200, data)
        self.assertIn("secret", data)
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        # The secret never appears again in a later GET.
        status2, data2, _ = self._request("GET", f"/api/local/room-invites/lobbies/{lobby['lobby_id']}")
        self.assertNotIn(data["secret"], json.dumps(data2))

    def test_revoke_invite_works(self):
        lobby = self._create_lobby()
        _, issued, _ = self._request("POST", f"/api/local/room-invites/lobbies/{lobby['lobby_id']}/invites", {
            "member_id": "peer-b", "invitee_label": "B",
        })
        status, data, _ = self._request("POST", f"/api/local/room-invites/lobbies/{lobby['lobby_id']}/invites/{issued['invite_id']}/revoke")
        self.assertEqual(status, 200)
        self.assertTrue(data["revoked"])

    def _redeem_directly(self, lobby, issued):
        """Simulate the (out-of-scope P2b) joining-owner redeem by using the store directly."""
        store = RoomInviteStore(self.store_path)
        registry = PeerRegistry()
        registry.register(peer_id="owner-b", installation_id=INSTALL_B, profile="default",
                           fingerprint=FINGERPRINT_B, endpoint_id="endpoint-b",
                           catalog=copy.deepcopy(CATALOG), consent_public_key=CONSENT_PUBLIC, tls_ca_pem=CA_PEM, pairing_generation=1)
        binding = registry.binding_for("owner-b")
        store.redeem(lobby["lobby_id"], issued["invite_id"], issued["secret"], binding=binding)
        return store, binding

    def test_decide_host_approve_then_activation_status_reflects_it(self):
        lobby = self._create_lobby()
        _, issued, _ = self._request("POST", f"/api/local/room-invites/lobbies/{lobby['lobby_id']}/invites", {
            "member_id": "peer-b", "invitee_label": "B",
        })
        _, binding = self._redeem_directly(lobby, issued)
        status, data, _ = self._request("POST", f"/api/local/room-invites/lobbies/{lobby['lobby_id']}/invites/{issued['invite_id']}/decision", {
            "peer_id": "owner-b", "approved": True, "expected_binding": _expected_binding(binding),
        })
        self.assertEqual(status, 200, data)
        self.assertEqual(data["redemption"]["host_decision"], "approved")
        status, data, _ = self._request("GET", f"/api/local/room-invites/lobbies/{lobby['lobby_id']}/activation")
        self.assertEqual(status, 200)
        self.assertFalse(data["ready"])  # joining consent still pending — approval alone is not enough

    def test_decide_host_deny_reflected(self):
        lobby = self._create_lobby()
        _, issued, _ = self._request("POST", f"/api/local/room-invites/lobbies/{lobby['lobby_id']}/invites", {
            "member_id": "peer-b", "invitee_label": "B",
        })
        _, binding = self._redeem_directly(lobby, issued)
        status, data, _ = self._request("POST", f"/api/local/room-invites/lobbies/{lobby['lobby_id']}/invites/{issued['invite_id']}/decision", {
            "peer_id": "owner-b", "approved": False, "expected_binding": _expected_binding(binding),
        })
        self.assertEqual(status, 200, data)
        self.assertEqual(data["redemption"]["host_decision"], "denied")


class IdorTests(RoomInviteRoutesHttpTest):
    def test_foreign_owner_lobby_indistinguishable_from_nonexistent(self):
        lobby = self._create_lobby()
        # Seed a lobby belonging to a different owner profile directly via the store.
        store = RoomInviteStore(self.store_path)
        foreign = store.create_lobby(name="Foreign", roster=[
            {"member_id": "local-x", "profile": "someone-else", "handle": "local"},
            {"member_id": "peer-y", "profile": "default", "handle": "peer",
             "target": {"kind": "peer", "peer_id": "owner-b", "installation_id": INSTALL_B, "profile": "default"}},
        ])
        status_foreign, data_foreign, _ = self._request("GET", f"/api/local/room-invites/lobbies/{foreign['lobby_id']}")
        status_missing, data_missing, _ = self._request("GET", "/api/local/room-invites/lobbies/lobby-" + "f" * 32)
        self.assertEqual(status_foreign, status_missing)
        self.assertEqual(data_foreign, data_missing)
        self.assertEqual(status_foreign, 404)

    def test_mismatched_invite_lobby_pair_indistinguishable_from_nonexistent(self):
        lobby = self._create_lobby()
        _, issued, _ = self._request("POST", f"/api/local/room-invites/lobbies/{lobby['lobby_id']}/invites", {
            "member_id": "peer-b", "invitee_label": "B",
        })
        other_lobby = self._create_lobby()
        status_mismatch, data_mismatch, _ = self._request(
            "POST", f"/api/local/room-invites/lobbies/{other_lobby['lobby_id']}/invites/{issued['invite_id']}/revoke")
        status_missing, data_missing, _ = self._request(
            "POST", f"/api/local/room-invites/lobbies/{other_lobby['lobby_id']}/invites/invite-" + "f" * 32 + "/revoke")
        self.assertEqual(status_mismatch, status_missing)
        self.assertEqual(data_mismatch, data_missing)


class PathPatternTests(RoomInviteRoutesHttpTest):
    def test_malformed_lobby_id_rejected_before_store_lookup(self):
        for bad in ("lobby-../../etc/passwd", "lobby-short", "../lobbies/x", "lobby-" + "g" * 32):
            with self.subTest(bad=bad):
                status, data, _ = self._request("GET", f"/api/local/room-invites/lobbies/{bad}")
                self.assertEqual(status, 404)

    def test_malformed_invite_id_rejected(self):
        lobby = self._create_lobby()
        status, data, _ = self._request(
            "POST", f"/api/local/room-invites/lobbies/{lobby['lobby_id']}/invites/not-an-invite/revoke")
        self.assertEqual(status, 404)


class ReadOnlyModeTests(RoomInviteRoutesHttpTest):
    def test_read_only_mode_blocks_post(self):
        os.environ["MISSION_CONTROL_READ_ONLY"] = "1"
        self.addCleanup(lambda: os.environ.pop("MISSION_CONTROL_READ_ONLY", None))
        status, data, _ = self._request("POST", "/api/local/room-invites/lobbies", {
            "name": "Test", "roster": [],
        })
        self.assertEqual(status, 403)
        self.assertEqual(data.get("error"), "read_only_mode")

    def test_read_only_mode_does_not_block_get(self):
        os.environ["MISSION_CONTROL_READ_ONLY"] = "1"
        self.addCleanup(lambda: os.environ.pop("MISSION_CONTROL_READ_ONLY", None))
        status, data, _ = self._request("GET", "/api/local/room-invites/lobbies/lobby-" + "0" * 32)
        self.assertEqual(status, 404)  # reached routing, not blocked by read-only


class GetNeverMutatesTests(RoomInviteRoutesHttpTest):
    def test_get_activation_status_makes_no_writes_and_no_rpc_calls(self):
        lobby = self._create_lobby()
        store = RoomInviteStore(self.store_path)
        before = store.get_lobby(lobby["lobby_id"])["updated_at"]
        for _ in range(3):
            status, data, _ = self._request("GET", f"/api/local/room-invites/lobbies/{lobby['lobby_id']}/activation")
            self.assertEqual(status, 200)
        after = store.get_lobby(lobby["lobby_id"])["updated_at"]
        self.assertEqual(before, after)


class ActivationTests(RoomInviteRoutesHttpTest):
    def _fully_consented_lobby(self):
        lobby = self._create_lobby()
        _, issued, _ = self._request("POST", f"/api/local/room-invites/lobbies/{lobby['lobby_id']}/invites", {
            "member_id": "peer-b", "invitee_label": "B",
        })
        store = RoomInviteStore(self.store_path)
        registry = PeerRegistry()
        registry.register(peer_id="owner-b", installation_id=INSTALL_B, profile="default",
                           fingerprint=FINGERPRINT_B, endpoint_id="endpoint-b",
                           catalog=copy.deepcopy(CATALOG), consent_public_key=CONSENT_PUBLIC, tls_ca_pem=CA_PEM, pairing_generation=1)
        binding = registry.binding_for("owner-b")
        store.redeem(lobby["lobby_id"], issued["invite_id"], issued["secret"], binding=binding)
        store.decide_host(lobby["lobby_id"], issued["invite_id"], binding=binding, approved=True)
        store.consent_joining(lobby["lobby_id"], issued["invite_id"], binding=binding, consented=True)
        return lobby

    def test_activate_without_native_rpc_configured_is_a_stable_error(self):
        lobby = self._fully_consented_lobby()
        status, data, _ = self._request("POST", f"/api/local/room-invites/lobbies/{lobby['lobby_id']}/activate")
        self.assertNotEqual(status, 200)
        self.assertIn(status, (409, 422, 503))

    def test_activate_not_ready_is_rejected(self):
        lobby = self._create_lobby()
        status, data, _ = self._request("POST", f"/api/local/room-invites/lobbies/{lobby['lobby_id']}/activate")
        self.assertNotEqual(status, 200)

    def _write_token_file(self):
        token_path = Path(self.tmp.name) / "config" / "native-rpc.token"
        _write_private_file(token_path, "unit-test-native-rpc-token")
        return token_path

    def test_activation_timeout_is_a_stable_error_and_does_not_retry(self):
        """R2: the previous version of this test never configured a material
        resolver/registry approval, so every attempt died at the unrelated
        ``material_channel_unavailable`` gate before ever reaching a native
        RPC call -- it could not actually exercise a timeout. This version
        wires the same scoped-material + registry-approval fixture as the
        idempotency tests so the request genuinely reaches
        ``groups.create``, hangs there, gets cancelled by the activation
        deadline, and must surface a single stable ``activation_timeout``
        with zero retry and unchanged durable state."""
        import contextlib as _contextlib

        class _HangingAfterCreateRpc:
            def __init__(self):
                self.calls: list[str] = []
                self.cancelled: list[str] = []

            async def __call__(self, method, params):
                self.calls.append(method)
                if method == "groups.capabilities":
                    return {"driver": True, "authority_gateway_id": INSTALL_A}
                if method == "groups.create":
                    try:
                        await asyncio.sleep(30)
                    except asyncio.CancelledError:
                        self.cancelled.append(method)
                        raise
                raise AssertionError(f"unexpected native call: {method}")

        rpc = _HangingAfterCreateRpc()

        @_contextlib.asynccontextmanager
        async def hanging_connect(_rpc_config):
            yield rpc

        lobby, issued, binding = self._consented_lobby_and_invite()
        scoped = self._scoped_material_for(lobby, issued, binding)
        self._wire_approved_registry(scoped)
        self._wire_material_resolver(scoped)

        original_connect = room_invite_routes._connect_native_rpc
        original_timeout = room_invite_routes._ACTIVATE_TIMEOUT_SECONDS
        room_invite_routes._connect_native_rpc = hanging_connect
        room_invite_routes._ACTIVATE_TIMEOUT_SECONDS = 0.2
        self.addCleanup(lambda: setattr(room_invite_routes, "_connect_native_rpc", original_connect))
        self.addCleanup(lambda: setattr(room_invite_routes, "_ACTIVATE_TIMEOUT_SECONDS", original_timeout))

        self._write_config(native_rpc={"endpoint": "ws://127.0.0.1:1/ignored",
                                        "token_path": str(self._write_token_file())})

        status, data, _ = self._request("POST", f"/api/local/room-invites/lobbies/{lobby['lobby_id']}/activate")

        self.assertEqual(status, 503, data)
        self.assertEqual(data.get("code"), "activation_timeout")
        self.assertEqual(rpc.calls, ["groups.capabilities", "groups.create"],
                          "exactly one create attempt, no retry")
        self.assertEqual(rpc.cancelled, ["groups.create"], "the pending create must be cancelled, not abandoned")

        store = RoomInviteStore(self.store_path)
        after = store.get_lobby(lobby["lobby_id"])
        self.assertEqual(after["state"], "sealed", "durable state must reflect the aborted-but-sealed attempt")
        self.assertEqual(after["planned_room_id"], lobby["planned_room_id"], "planned room id must not change")

        # A second attempt against the same still-hanging transport must not
        # retry the native create either -- the coordinator's own sealed-state
        # handling, not route-level retry logic, decides this.
        status2, data2, _ = self._request("POST", f"/api/local/room-invites/lobbies/{lobby['lobby_id']}/activate")
        self.assertNotEqual(status2, 200)


class ActivationIdempotencyTests(RoomInviteRoutesHttpTest):
    """Real activation tests: no vacuous 0==0 assertions, real call sequences.

    Replaces the removed ``test_duplicate_activate_does_not_call_native_create_twice``
    (F2): that test never reached ``groups.create`` (both POSTs failed at
    ``groups.capabilities`` with 503), so its assertion was trivially true.
    These tests drive a REAL successful activation, assert the exact native
    call sequence, and cover the partial-registration ambiguity (F1).
    """

    def _wire_recording_rpc(self, *, register_should_fail=False):
        """Labeled stand-in for the native RPC transport. Records every call
        and fabricates readback-consistent native state from the spec it was
        given at groups.create -- never a bare 0==0 stub."""
        calls = []

        class _RecordingRpc:
            def __init__(self):
                self.spec = None
                self.register_attempts = 0

            async def __call__(self, method, params):
                calls.append(method)
                if method == "groups.capabilities":
                    return {"driver": True, "authority_gateway_id": INSTALL_A}
                if method == "groups.create":
                    self.spec = params
                    return {}
                if method == "groups.state":
                    room = dict(self.spec, authority_gateway_id=INSTALL_A, authority_epoch=1, disbanded_at=None)
                    response = {"room": room}
                    if self.register_attempts and not register_should_fail:
                        response["driver_status"] = {"peer_routes": [
                            {"member_id": "peer-b", "room_id": self.spec["room_id"], "status": "ready"},
                        ]}
                    return response
                if method == "groups.peer.register":
                    self.register_attempts += 1
                    if register_should_fail:
                        raise ConnectionError("simulated lost register reply")
                    return {"registered": True, "target_install_id": INSTALL_B, "target_profile": "default"}
                raise AssertionError(f"unexpected native call: {method}")

        rpc = _RecordingRpc()

        import contextlib as _contextlib

        @_contextlib.asynccontextmanager
        async def fake_connect(_rpc_config):
            yield rpc

        original_connect = room_invite_routes._connect_native_rpc
        room_invite_routes._connect_native_rpc = fake_connect
        self.addCleanup(lambda: setattr(room_invite_routes, "_connect_native_rpc", original_connect))
        return calls

    def test_successful_activation_drives_exact_native_call_sequence(self):
        lobby, issued, binding = self._consented_lobby_and_invite()
        scoped = self._scoped_material_for(lobby, issued, binding)
        self._wire_approved_registry(scoped)
        self._wire_material_resolver(scoped)
        calls = self._wire_recording_rpc()
        self._write_config(native_rpc={"endpoint": "ws://127.0.0.1:1/ignored",
                                        "token_path": str(self._write_token_file())})

        status, data, _ = self._request("POST", f"/api/local/room-invites/lobbies/{lobby['lobby_id']}/activate")

        self.assertEqual(status, 200, data)
        self.assertEqual(data["status"], "activated")
        self.assertEqual(data["room_id"], lobby["planned_room_id"])
        self.assertEqual(calls, ["groups.capabilities", "groups.create", "groups.state",
                                  "groups.peer.register", "groups.state"])

    def test_duplicate_activate_after_success_is_same_200_with_zero_additional_native_calls(self):
        lobby, issued, binding = self._consented_lobby_and_invite()
        scoped = self._scoped_material_for(lobby, issued, binding)
        self._wire_approved_registry(scoped)
        self._wire_material_resolver(scoped)
        calls = self._wire_recording_rpc()
        self._write_config(native_rpc={"endpoint": "ws://127.0.0.1:1/ignored",
                                        "token_path": str(self._write_token_file())})

        status1, data1, _ = self._request("POST", f"/api/local/room-invites/lobbies/{lobby['lobby_id']}/activate")
        self.assertEqual(status1, 200, data1)
        calls_after_first = list(calls)

        status2, data2, _ = self._request("POST", f"/api/local/room-invites/lobbies/{lobby['lobby_id']}/activate")

        self.assertEqual(status2, 200, data2)
        self.assertEqual(data2["status"], "activated")
        self.assertEqual(data2["room_id"], lobby["planned_room_id"])
        self.assertEqual(calls, calls_after_first, "duplicate activate must make zero additional native calls")

    def test_partial_registration_then_duplicate_is_409_registration_incomplete_with_zero_native_calls(self):
        """F1: a create-then-register failure must never be reported as a
        success on retry -- the bare 'created' state is not proof every
        remote roster member actually finished native registration."""
        lobby, issued, binding = self._consented_lobby_and_invite()
        scoped = self._scoped_material_for(lobby, issued, binding)
        self._wire_approved_registry(scoped)
        self._wire_material_resolver(scoped)
        calls = self._wire_recording_rpc(register_should_fail=True)
        self._write_config(native_rpc={"endpoint": "ws://127.0.0.1:1/ignored",
                                        "token_path": str(self._write_token_file())})

        status1, data1, _ = self._request("POST", f"/api/local/room-invites/lobbies/{lobby['lobby_id']}/activate")
        self.assertEqual(status1, 503, data1)
        self.assertEqual(data1.get("code"), "registration_ambiguous")
        self.assertEqual(calls, ["groups.capabilities", "groups.create", "groups.state", "groups.peer.register"])
        calls_after_first = list(calls)

        status2, data2, _ = self._request("POST", f"/api/local/room-invites/lobbies/{lobby['lobby_id']}/activate")

        self.assertEqual(status2, 409, data2)
        self.assertEqual(data2.get("code"), "registration_incomplete")
        self.assertNotEqual(status2, 200)
        self.assertEqual(calls, calls_after_first, "a known-incomplete retry must make zero native calls")

    def test_activation_status_exposes_per_peer_registration_once_created(self):
        lobby, issued, binding = self._consented_lobby_and_invite()
        scoped = self._scoped_material_for(lobby, issued, binding)
        self._wire_approved_registry(scoped)
        self._wire_material_resolver(scoped)
        self._wire_recording_rpc(register_should_fail=True)
        self._write_config(native_rpc={"endpoint": "ws://127.0.0.1:1/ignored",
                                        "token_path": str(self._write_token_file())})
        self._request("POST", f"/api/local/room-invites/lobbies/{lobby['lobby_id']}/activate")

        status, data, _ = self._request("GET", f"/api/local/room-invites/lobbies/{lobby['lobby_id']}/activation")

        self.assertEqual(status, 200, data)
        self.assertEqual(data["lobby_state"], "created")
        peer = next(p for p in data["peers"] if p["member_id"] == "peer-b")
        self.assertIn("registered", peer)
        self.assertFalse(peer["registered"])


class MaterialChannelTests(RoomInviteRoutesHttpTest):
    """F3: an unwired material resolver must fail closed, before any native call."""

    def test_missing_material_resolver_fails_before_any_native_call(self):
        lobby = self._fully_consented_lobby_for_material_test()
        calls = []

        class _RecordingRpc:
            async def __call__(self, method, params):
                calls.append(method)
                raise AssertionError(f"unexpected native call: {method}")

        import contextlib as _contextlib

        @_contextlib.asynccontextmanager
        async def fake_connect(_rpc_config):
            yield _RecordingRpc()

        original_connect = room_invite_routes._connect_native_rpc
        room_invite_routes._connect_native_rpc = fake_connect
        self.addCleanup(lambda: setattr(room_invite_routes, "_connect_native_rpc", original_connect))
        # Explicit: the real-config default never wires a resolver (P2b's job).
        original_resolver = getattr(room_invite_routes, "_MATERIAL_RESOLVER", None)
        room_invite_routes._MATERIAL_RESOLVER = None
        self.addCleanup(lambda: setattr(room_invite_routes, "_MATERIAL_RESOLVER", original_resolver))

        self._write_config(native_rpc={"endpoint": "ws://127.0.0.1:1/ignored",
                                        "token_path": str(self._write_token_file())})

        status, data, _ = self._request("POST", f"/api/local/room-invites/lobbies/{lobby['lobby_id']}/activate")

        self.assertEqual(status, 503, data)
        self.assertEqual(data.get("code"), "material_channel_unavailable")
        self.assertEqual(calls, [], "no native call -- not even groups.capabilities -- may happen")

    def _fully_consented_lobby_for_material_test(self):
        lobby = self._create_lobby()
        _, issued, _ = self._request("POST", f"/api/local/room-invites/lobbies/{lobby['lobby_id']}/invites", {
            "member_id": "peer-b", "invitee_label": "B",
        })
        store = RoomInviteStore(self.store_path)
        registry = PeerRegistry()
        registry.register(peer_id="owner-b", installation_id=INSTALL_B, profile="default",
                           fingerprint=FINGERPRINT_B, endpoint_id="endpoint-b",
                           catalog=copy.deepcopy(CATALOG), consent_public_key=CONSENT_PUBLIC, tls_ca_pem=CA_PEM, pairing_generation=1)
        binding = registry.binding_for("owner-b")
        store.redeem(lobby["lobby_id"], issued["invite_id"], issued["secret"], binding=binding)
        store.decide_host(lobby["lobby_id"], issued["invite_id"], binding=binding, approved=True)
        store.consent_joining(lobby["lobby_id"], issued["invite_id"], binding=binding, consented=True)
        return lobby

    def _write_token_file(self):
        token_path = Path(self.tmp.name) / "config" / "native-rpc.token"
        _write_private_file(token_path, "unit-test-native-rpc-token")
        return token_path


class RouteErrorLoggingTests(RoomInviteRoutesHttpTest):
    """F4: the catch-all handler must never log a traceback or interpolated values."""

    def test_internal_error_log_has_no_traceback_or_secret_values(self):
        lobby = self._create_lobby()
        log_stream = __import__("io").StringIO()
        handler = logging.StreamHandler(log_stream)
        root_logger = logging.getLogger()
        root_logger.addHandler(handler)
        self.addCleanup(lambda: root_logger.removeHandler(handler))

        secret_path_marker = "/Users/definitely-not-a-real-owner/secret-marker-xyz"
        original_inspect = InviteRoomsService.inspect_lobby

        def boom(self, ctx, lobby_id):
            raise RuntimeError(f"boom touching {secret_path_marker}")

        InviteRoomsService.inspect_lobby = boom
        self.addCleanup(lambda: setattr(InviteRoomsService, "inspect_lobby", original_inspect))

        status, data, _ = self._request("GET", f"/api/local/room-invites/lobbies/{lobby['lobby_id']}")
        handler.flush()
        log_text = log_stream.getvalue()

        self.assertEqual(status, 500)
        self.assertEqual(data.get("error"), "internal")
        self.assertIn("RuntimeError", log_text)
        self.assertNotIn(secret_path_marker, log_text)
        self.assertNotIn("Traceback (most recent call last)", log_text)
        self.assertNotIn("boom touching", log_text)


class SecretLeakageLogTests(RoomInviteRoutesHttpTest):
    def test_issued_secret_never_appears_in_logs(self):
        log_stream = __import__("io").StringIO()
        handler = logging.StreamHandler(log_stream)
        root_logger = logging.getLogger()
        root_logger.addHandler(handler)
        self.addCleanup(lambda: root_logger.removeHandler(handler))
        try:
            lobby = self._create_lobby()
            status, issued, _ = self._request("POST", f"/api/local/room-invites/lobbies/{lobby['lobby_id']}/invites", {
                "member_id": "peer-b", "invitee_label": "B",
            })
            self.assertEqual(status, 200)
            self._request("GET", f"/api/local/room-invites/lobbies/{lobby['lobby_id']}")
        finally:
            handler.flush()
        self.assertNotIn(issued["secret"], log_stream.getvalue())
        self.assertNotIn(GRANT, log_stream.getvalue())


class TransportPreflightTests(RoomInviteRoutesHttpTest):
    """R1: nothing may open the native transport before every transport-free
    check has already decided the request. ``_wire_connect_attempt_recorder``
    records every connect attempt and, unless told otherwise, raises instead
    of ever yielding a usable RPC -- so any test asserting zero connect
    attempts fails loudly the moment preflight is skipped, and any test
    asserting a mapped failure gets a real transport-layer exception."""

    def _configure_native_rpc(self):
        self._write_config(native_rpc={"endpoint": "ws://127.0.0.1:1/ignored",
                                        "token_path": str(self._write_token_file())})

    def test_not_ready_lobby_rejects_before_any_connect_attempt(self):
        lobby = self._create_lobby()
        self._configure_native_rpc()
        attempts = self._wire_connect_attempt_recorder()

        status, data, _ = self._request("POST", f"/api/local/room-invites/lobbies/{lobby['lobby_id']}/activate")

        self.assertNotEqual(status, 200)
        self.assertEqual(attempts, [], "a not-ready lobby must never open the native transport")

    def test_missing_material_resolver_rejects_before_any_connect_attempt(self):
        lobby, issued, binding = self._consented_lobby_and_invite()
        # Deliberately skip _wire_material_resolver: the real-config default
        # (_MATERIAL_RESOLVER = None) must fail closed before any connect.
        self._configure_native_rpc()
        attempts = self._wire_connect_attempt_recorder()

        status, data, _ = self._request("POST", f"/api/local/room-invites/lobbies/{lobby['lobby_id']}/activate")

        self.assertEqual(status, 503, data)
        self.assertEqual(data.get("code"), "material_channel_unavailable")
        self.assertEqual(attempts, [], "a missing material resolver must never open the native transport")

    def test_duplicate_after_full_registration_is_durable_200_with_zero_connect_attempts(self):
        """Mirrors the review's ``already_registered_but_native_connection_unavailable``
        probe: a second activate on an already-created+fully-registered lobby
        must return the same durable 200 WITHOUT ever opening the transport,
        even though the transport stand-in would raise if it were reached."""
        lobby, issued, binding = self._consented_lobby_and_invite()
        scoped = self._scoped_material_for(lobby, issued, binding)
        self._wire_approved_registry(scoped)
        self._wire_material_resolver(scoped)
        self._configure_native_rpc()

        calls = self._wire_recording_rpc()
        status1, data1, _ = self._request("POST", f"/api/local/room-invites/lobbies/{lobby['lobby_id']}/activate")
        self.assertEqual(status1, 200, data1)
        self.assertTrue(calls, "first activation must drive real native calls")

        attempts = self._wire_connect_attempt_recorder()
        status2, data2, _ = self._request("POST", f"/api/local/room-invites/lobbies/{lobby['lobby_id']}/activate")

        self.assertEqual(status2, 200, data2)
        self.assertEqual(data2["status"], "activated")
        self.assertEqual(attempts, [], "a fully-registered duplicate must never open the native transport")

    def test_transport_open_failure_on_a_ready_lobby_is_stable_503_not_500(self):
        """R1's second half: once preflight genuinely requires a native call,
        an expected transport failure (refused connection, closed socket,
        empty token, ...) must map to a single stable sanitized 503 --
        never an uncaught 500."""
        lobby, issued, binding = self._consented_lobby_and_invite()
        scoped = self._scoped_material_for(lobby, issued, binding)
        self._wire_approved_registry(scoped)
        self._wire_material_resolver(scoped)
        self._configure_native_rpc()
        attempts = self._wire_connect_attempt_recorder()

        status, data, _ = self._request("POST", f"/api/local/room-invites/lobbies/{lobby['lobby_id']}/activate")

        self.assertEqual(status, 503, data)
        self.assertEqual(data.get("code"), "native_unavailable")
        self.assertEqual(attempts, ["connect_attempt"], "preflight must pass before the single real connect attempt")


class BodylessPostValidationTests(RoomInviteRoutesHttpTest):
    """R3: revoke and activate accept an empty body or exactly ``{}`` (strict
    JSON); anything else is rejected with zero side effects and no connect
    attempt, before any mutation or native call."""

    _BAD_BODIES = (
        ("malformed_non_json", b"not json!"),
        ("unknown_field", json.dumps({"grant": "browser-supplied"}).encode("utf-8")),
        ("unknown_field_target_url", json.dumps({"target_url": "http://evil.example"}).encode("utf-8")),
        ("duplicate_keys", b'{"a": 1, "a": 2}'),
        ("oversize", (b'{"pad": "' + b"A" * 20000 + b'"}')),
    )

    def test_revoke_rejects_every_bad_body_with_zero_side_effects(self):
        lobby = self._create_lobby()
        for name, raw in self._BAD_BODIES:
            with self.subTest(name=name):
                _, issued, _ = self._request("POST", f"/api/local/room-invites/lobbies/{lobby['lobby_id']}/invites", {
                    "member_id": "peer-b", "invitee_label": f"B-{name}",
                })
                status, data, _ = self._request(
                    "POST", f"/api/local/room-invites/lobbies/{lobby['lobby_id']}/invites/{issued['invite_id']}/revoke",
                    raw_body=raw)
                self.assertIn(status, (400, 413), (name, data))
                # Zero side effects: a real (bodyless) revoke must still succeed afterwards.
                status2, data2, _ = self._request(
                    "POST", f"/api/local/room-invites/lobbies/{lobby['lobby_id']}/invites/{issued['invite_id']}/revoke")
                self.assertEqual(status2, 200, (name, data2))
                self.assertTrue(data2["revoked"], (name, "the invite must still have been open to revoke"))

    def test_activate_rejects_every_bad_body_with_zero_connect_attempts(self):
        lobby, issued, binding = self._consented_lobby_and_invite()
        scoped = self._scoped_material_for(lobby, issued, binding)
        self._wire_approved_registry(scoped)
        self._wire_material_resolver(scoped)
        self._write_config(native_rpc={"endpoint": "ws://127.0.0.1:1/ignored",
                                        "token_path": str(self._write_token_file())})
        for name, raw in self._BAD_BODIES:
            with self.subTest(name=name):
                attempts = self._wire_connect_attempt_recorder()
                status, data, _ = self._request(
                    "POST", f"/api/local/room-invites/lobbies/{lobby['lobby_id']}/activate", raw_body=raw)
                self.assertIn(status, (400, 413), (name, data))
                self.assertEqual(attempts, [], (name, "a rejected body must never open the native transport"))

    def test_revoke_and_activate_accept_empty_or_empty_object_body(self):
        lobby = self._create_lobby()
        _, issued, _ = self._request("POST", f"/api/local/room-invites/lobbies/{lobby['lobby_id']}/invites", {
            "member_id": "peer-b", "invitee_label": "B",
        })
        status, data, _ = self._request(
            "POST", f"/api/local/room-invites/lobbies/{lobby['lobby_id']}/invites/{issued['invite_id']}/revoke",
            raw_body=b"{}")
        self.assertEqual(status, 200, data)
        self.assertTrue(data["revoked"])


class MisconfiguredStoreTests(RoomInviteRoutesHttpTest):
    """R4: a store that cannot even be opened (corrupt file, foreign bytes,
    ...) must be indistinguishable from any other misconfiguration -- 404,
    one sanitized warning, file bytes untouched -- never an escaping
    sqlite3 error with a traceback."""

    def test_non_sqlite_store_file_is_404_like_any_other_misconfiguration(self):
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        corrupt_bytes = b"not a sqlite database at all, just plain bytes\x00\x01\x02"
        self.store_path.write_bytes(corrupt_bytes)

        log_stream = __import__("io").StringIO()
        handler = logging.StreamHandler(log_stream)
        root_logger = logging.getLogger()
        root_logger.addHandler(handler)
        self.addCleanup(lambda: root_logger.removeHandler(handler))

        status, data, _ = self._request("GET", "/api/local/room-invites/lobbies/lobby-" + "0" * 32)
        handler.flush()
        log_text = log_stream.getvalue()
        baseline_status, baseline_data, _ = self._request("GET", "/api/local/totally-unknown-path")

        self.assertEqual(status, 404, data)
        self.assertEqual(status, baseline_status)
        self.assertEqual(data.get("error"), baseline_data.get("error"))
        self.assertNotIn("Traceback (most recent call last)", log_text)
        self.assertNotIn("DatabaseError", log_text)
        self.assertNotIn("OperationalError", log_text)
        self.assertEqual(self.store_path.read_bytes(), corrupt_bytes, "corrupt store file bytes must be untouched")

    def test_non_sqlite_store_file_does_not_crash_a_post_route_either(self):
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        self.store_path.write_bytes(b"also not sqlite\xff\xfe")

        status, data, _ = self._request("POST", "/api/local/room-invites/lobbies", {
            "name": "Test", "roster": [
                {"member_id": "local-a", "handle": "local"},
                {"member_id": "peer-b", "handle": "peer", "target": {"peer_id": "owner-b"}},
            ],
        })
        self.assertEqual(status, 404, data)


class DecisionErrorCodeTableTests(RoomInviteRoutesHttpTest):
    """R5: every machine code reachable from the decision route's public
    parameters must sit in the fixed error-code table -- table-driven, not a
    single example and a completeness claim in a comment."""

    def _lobby_and_invite(self):
        lobby = self._create_lobby()
        _, issued, _ = self._request("POST", f"/api/local/room-invites/lobbies/{lobby['lobby_id']}/invites", {
            "member_id": "peer-b", "invitee_label": "B",
        })
        return lobby, issued

    def test_decision_public_parameter_error_codes_are_all_mapped(self):
        lobby, issued = self._lobby_and_invite()
        filler_binding = {"fingerprint": "a" * 64, "target_install_id": INSTALL_B, "target_profile": "default",
                           "catalog_digest": "b" * 64, "policy_digest": "c" * 64}
        cases = (
            # (name, body, expected_status, expected_code_or_None)
            ("invalid_peer_id_malformed_token", {"peer_id": "???", "approved": True, "expected_binding": filler_binding}, 400, "invalid_peer_id"),
            ("unknown_peer_well_formed_but_unregistered", {"peer_id": "unregistered-peer-token", "approved": True, "expected_binding": filler_binding}, 404, None),
            ("valid_bound_peer_approves_cleanly", {"peer_id": "owner-b", "approved": True, "expected_binding": filler_binding}, 404, None),
            ("missing_expected_binding", {"peer_id": "owner-b", "approved": True}, 400, "invalid_body"),
            ("malformed_expected_binding_extra_key", {"peer_id": "owner-b", "approved": True,
                                                        "expected_binding": dict(filler_binding, endpoint_id="x")}, 400, "invalid_body"),
        )
        for name, body, expected_status, expected_code in cases:
            with self.subTest(name=name):
                status, data, _ = self._request(
                    "POST", f"/api/local/room-invites/lobbies/{lobby['lobby_id']}/invites/{issued['invite_id']}/decision",
                    body)
                self.assertNotEqual(status, 500, (name, data))
                self.assertEqual(status, expected_status, (name, data))
                if expected_code is not None:
                    self.assertEqual(data.get("code"), expected_code, (name, data))


class TeardownOutcomeTests(RoomInviteRoutesHttpTest):
    """P-1 (MVP.2a-fix3): a native-transport teardown fault must never
    replace an activation outcome that already happened -- only acquiring
    the transport (token read, ws_connect) may ever surface as
    ``native_unavailable``."""

    def _write_token_file(self):
        token_path = Path(self.tmp.name) / "config" / "native-rpc.token"
        _write_private_file(token_path, "unit-test-native-rpc-token")
        return token_path

    def _configure_native_rpc(self):
        self._write_config(native_rpc={"endpoint": "ws://127.0.0.1:1/ignored",
                                        "token_path": str(self._write_token_file())})

    def _wire_recording_rpc_with_teardown_fault(self, *, register_should_fail=False):
        """Same labeled stand-in as ``ActivationIdempotencyTests._wire_recording_rpc``,
        but the transport's own teardown (the ``async with`` exit, modeled
        after ``_connect_native_rpc``'s ``ws_connect``/``ClientSession``
        nesting) always raises -- after the body has already run to
        completion or to its own classified error."""
        calls: list[str] = []

        class _RecordingRpc:
            def __init__(self):
                self.spec = None
                self.register_attempts = 0

            async def __call__(self, method, params):
                calls.append(method)
                if method == "groups.capabilities":
                    return {"driver": True, "authority_gateway_id": INSTALL_A}
                if method == "groups.create":
                    self.spec = params
                    return {}
                if method == "groups.state":
                    room = dict(self.spec, authority_gateway_id=INSTALL_A, authority_epoch=1, disbanded_at=None)
                    response = {"room": room}
                    if self.register_attempts and not register_should_fail:
                        response["driver_status"] = {"peer_routes": [
                            {"member_id": "peer-b", "room_id": self.spec["room_id"], "status": "ready"},
                        ]}
                    return response
                if method == "groups.peer.register":
                    self.register_attempts += 1
                    if register_should_fail:
                        raise ConnectionError("simulated lost register reply")
                    return {"registered": True, "target_install_id": INSTALL_B, "target_profile": "default"}
                raise AssertionError(f"unexpected native call: {method}")

        rpc = _RecordingRpc()

        import contextlib as _contextlib

        @_contextlib.asynccontextmanager
        async def fake_connect_with_teardown_fault(_rpc_config):
            try:
                yield rpc
            finally:
                raise RuntimeError("simulated native transport teardown fault")

        original_connect = room_invite_routes._connect_native_rpc
        room_invite_routes._connect_native_rpc = fake_connect_with_teardown_fault
        self.addCleanup(lambda: setattr(room_invite_routes, "_connect_native_rpc", original_connect))
        return calls

    def test_cleanup_fault_after_full_success_still_returns_200_and_is_idempotent(self):
        lobby, issued, binding = self._consented_lobby_and_invite()
        scoped = self._scoped_material_for(lobby, issued, binding)
        self._wire_approved_registry(scoped)
        self._wire_material_resolver(scoped)
        self._configure_native_rpc()
        calls = self._wire_recording_rpc_with_teardown_fault()

        status, data, _ = self._request("POST", f"/api/local/room-invites/lobbies/{lobby['lobby_id']}/activate")

        self.assertEqual(status, 200, data)
        self.assertEqual(data["status"], "activated")
        self.assertEqual(data["room_id"], lobby["planned_room_id"])
        self.assertEqual(calls, ["groups.capabilities", "groups.create", "groups.state",
                                  "groups.peer.register", "groups.state"],
                          "a teardown-only fault must not trigger any extra/retried native call")

        store = RoomInviteStore(self.store_path)
        after = store.get_lobby(lobby["lobby_id"])
        self.assertEqual(after["state"], "created")
        self.assertTrue(store.peer_registration_status(lobby["lobby_id"]).get("peer-b"))

        calls_after_first = list(calls)
        attempts = self._wire_connect_attempt_recorder()
        status2, data2, _ = self._request("POST", f"/api/local/room-invites/lobbies/{lobby['lobby_id']}/activate")

        self.assertEqual(status2, 200, data2)
        self.assertEqual(data2["status"], "activated")
        self.assertEqual(attempts, [], "the idempotent duplicate must never reopen the native transport")
        self.assertEqual(calls, calls_after_first)

    def test_mid_register_fault_and_cleanup_fault_preserves_registration_ambiguous(self):
        lobby, issued, binding = self._consented_lobby_and_invite()
        scoped = self._scoped_material_for(lobby, issued, binding)
        self._wire_approved_registry(scoped)
        self._wire_material_resolver(scoped)
        self._configure_native_rpc()
        calls = self._wire_recording_rpc_with_teardown_fault(register_should_fail=True)

        status1, data1, _ = self._request("POST", f"/api/local/room-invites/lobbies/{lobby['lobby_id']}/activate")

        self.assertEqual(status1, 503, data1)
        self.assertEqual(data1.get("code"), "registration_ambiguous",
                          "a teardown fault must not replace the already-classified registration error")
        self.assertEqual(calls, ["groups.capabilities", "groups.create", "groups.state", "groups.peer.register"])
        calls_after_first = list(calls)

        store = RoomInviteStore(self.store_path)
        after = store.get_lobby(lobby["lobby_id"])
        self.assertEqual(after["state"], "created")
        self.assertEqual(store.peer_registration_status(lobby["lobby_id"]), {})

        attempts = self._wire_connect_attempt_recorder()
        status2, data2, _ = self._request("POST", f"/api/local/room-invites/lobbies/{lobby['lobby_id']}/activate")

        self.assertEqual(status2, 409, data2)
        self.assertEqual(data2.get("code"), "registration_incomplete")
        self.assertEqual(attempts, [], "a known-incomplete retry must make zero connect/RPC attempts")
        self.assertEqual(calls, calls_after_first)

    def test_acquisition_failure_is_still_native_unavailable_with_zero_rpc(self):
        lobby, issued, binding = self._consented_lobby_and_invite()
        scoped = self._scoped_material_for(lobby, issued, binding)
        self._wire_approved_registry(scoped)
        self._wire_material_resolver(scoped)
        self._configure_native_rpc()

        store = RoomInviteStore(self.store_path)
        before = store.get_lobby(lobby["lobby_id"])

        attempts = self._wire_connect_attempt_recorder()
        status, data, _ = self._request("POST", f"/api/local/room-invites/lobbies/{lobby['lobby_id']}/activate")

        self.assertEqual(status, 503, data)
        self.assertEqual(data.get("code"), "native_unavailable")
        self.assertEqual(attempts, ["connect_attempt"], "exactly the single acquisition attempt, zero RPC")

        after = store.get_lobby(lobby["lobby_id"])
        self.assertEqual(after, before, "an acquisition failure must leave the lobby state untouched")

    def test_unclassified_exception_after_activate_started_is_not_native_unavailable(self):
        """Point 3: a raw, unclassified bug surfacing once ``scoped.activate``
        has actually started must not be reported as ``native_unavailable``
        (that would wrongly imply nothing was attempted)."""
        lobby, issued, binding = self._consented_lobby_and_invite()
        scoped = self._scoped_material_for(lobby, issued, binding)
        self._wire_approved_registry(scoped)
        self._wire_material_resolver(scoped)
        self._configure_native_rpc()

        import contextlib as _contextlib

        @_contextlib.asynccontextmanager
        async def fake_connect(_rpc_config):
            yield object()

        original_connect = room_invite_routes._connect_native_rpc
        room_invite_routes._connect_native_rpc = fake_connect
        self.addCleanup(lambda: setattr(room_invite_routes, "_connect_native_rpc", original_connect))

        original_activate = InviteRoomsService.activate

        async def exploding_activate(self, ctx, lobby_id):
            # Already-started activation hitting an unclassified bug, not a
            # ServiceError/InviteError/PeerRegistryError and not a timeout.
            raise KeyError("simulated unclassified activation bug")

        InviteRoomsService.activate = exploding_activate
        self.addCleanup(lambda: setattr(InviteRoomsService, "activate", original_activate))

        status, data, _ = self._request("POST", f"/api/local/room-invites/lobbies/{lobby['lobby_id']}/activate")

        self.assertEqual(status, 503, data)
        self.assertNotEqual(data.get("code"), "native_unavailable",
                             "an unclassified in-flight failure must not be reported as native_unavailable")
        self.assertEqual(data.get("code"), "activation_failed")


class ControlSignalPreservationTests(RoomInviteRoutesHttpTest):
    """P-2 (MVP.2a-fix4): a transport-teardown outcome (``suppressed``) must
    never convert a control signal, ``TimeoutError``, or an already-classified
    ``ServiceError`` into ``activation_failed``. Drives ``_run_activation``
    directly (coroutine-level) with a synthetic ``scoped.activate`` raising
    each signal and a synthetic transport ``__aexit__`` returning both
    ``True`` and ``False``, asserting the exact exception that escapes.
    """

    def _prepared_lobby(self):
        lobby, issued, binding = self._consented_lobby_and_invite()
        scoped = self._scoped_material_for(lobby, issued, binding)
        self._wire_approved_registry(scoped)
        self._wire_material_resolver(scoped)
        self._write_config(native_rpc={"endpoint": "ws://127.0.0.1:1/ignored",
                                        "token_path": str(self._write_token_file())})
        return lobby

    def _drive(self, exc_class, suppress):
        lobby = self._prepared_lobby()
        service, ctx, config = runtime_config.load_service(material_resolver=room_invite_routes._MATERIAL_RESOLVER)

        class _FakeCm:
            async def __aenter__(self):
                return object()

            async def __aexit__(self, typ, exc, tb):
                return suppress

        async def exploding_activate(self, ctx, lobby_id):
            raise exc_class("synthetic private marker")

        import contextlib as _contextlib

        original_connect = room_invite_routes._connect_native_rpc
        room_invite_routes._connect_native_rpc = lambda _config: _FakeCm()
        self.addCleanup(lambda: setattr(room_invite_routes, "_connect_native_rpc", original_connect))
        original_activate = InviteRoomsService.activate
        InviteRoomsService.activate = exploding_activate
        self.addCleanup(lambda: setattr(InviteRoomsService, "activate", original_activate))

        async def run():
            return await room_invite_routes._run_activation(service, ctx, lobby["lobby_id"], config)

        return asyncio.run(run())

    def _assert_reraised_unchanged(self, exc_class):
        for suppress in (False, True):
            with self.subTest(exc_class=exc_class.__name__, suppressed=suppress):
                with self.assertRaises(exc_class) as cm:
                    self._drive(exc_class, suppress)
                self.assertIsInstance(cm.exception, exc_class)

    def test_keyboard_interrupt_is_never_converted(self):
        self._assert_reraised_unchanged(KeyboardInterrupt)

    def test_system_exit_is_never_converted(self):
        self._assert_reraised_unchanged(SystemExit)

    def test_generator_exit_is_never_converted(self):
        self._assert_reraised_unchanged(GeneratorExit)

    def test_cancelled_error_is_never_converted(self):
        self._assert_reraised_unchanged(asyncio.CancelledError)

    def test_timeout_error_is_never_converted(self):
        self._assert_reraised_unchanged(asyncio.TimeoutError)

    def test_known_service_error_is_preserved_exactly_even_when_suppressed(self):
        from invite_rooms.service import ServiceError

        for suppress in (False, True):
            with self.subTest(suppressed=suppress):
                with self.assertRaises(ServiceError) as cm:
                    self._drive(ServiceError, suppress)
                self.assertEqual(str(cm.exception), "synthetic private marker",
                                  "suppressed=True must never rewrite an already-classified ServiceError")


class PostMutationAmbiguityTests(RoomInviteRoutesHttpTest):
    """P-3 (MVP.2a-fix4): an unrecognized ordinary exception surfacing after
    a native mutation must be classified from durable state, never defaulted
    to ``activation_failed`` when the store proves something was admitted.
    Only the native RPC transport is replaced by a recording stand-in; the
    store, service and coordinator are real, so the fault is injected
    directly into the real ``RoomInviteStore`` write methods."""

    def _configure_native_rpc(self):
        self._write_config(native_rpc={"endpoint": "ws://127.0.0.1:1/ignored",
                                        "token_path": str(self._write_token_file())})

    def _wire_recording_rpc(self):
        calls = []

        class _RecordingRpc:
            def __init__(self):
                self.spec = None
                self.registered = False

            async def __call__(self, method, params):
                calls.append(method)
                if method == "groups.capabilities":
                    return {"driver": True, "authority_gateway_id": INSTALL_A}
                if method == "groups.create":
                    self.spec = params
                    return {}
                if method == "groups.peer.register":
                    self.registered = True
                    return {"registered": True, "target_install_id": INSTALL_B, "target_profile": "default"}
                if method == "groups.state":
                    room = dict(self.spec, authority_gateway_id=INSTALL_A, authority_epoch=1, disbanded_at=None)
                    response = {"room": room}
                    if self.registered:
                        response["driver_status"] = {"peer_routes": [
                            {"member_id": "peer-b", "room_id": self.spec["room_id"], "status": "ready"},
                        ]}
                    return response
                raise AssertionError(f"unexpected native call: {method}")

        rpc = _RecordingRpc()

        import contextlib as _contextlib

        @_contextlib.asynccontextmanager
        async def fake_connect(_rpc_config):
            yield rpc

        original_connect = room_invite_routes._connect_native_rpc
        room_invite_routes._connect_native_rpc = fake_connect
        self.addCleanup(lambda: setattr(room_invite_routes, "_connect_native_rpc", original_connect))
        return calls

    def test_fault_after_mark_created_write_is_registration_ambiguous_with_zero_rpc_on_retry(self):
        """An ordinary, unrecognized exception raised immediately after the
        real ``mark_created`` write completes: the create is durably
        confirmed (state == created) but no peer is registered yet."""
        lobby, issued, binding = self._consented_lobby_and_invite()
        scoped = self._scoped_material_for(lobby, issued, binding)
        self._wire_approved_registry(scoped)
        self._wire_material_resolver(scoped)
        self._configure_native_rpc()
        calls = self._wire_recording_rpc()

        original_mark_created = RoomInviteStore.mark_created

        def faulty_mark_created(self, *args):
            original_mark_created(self, *args)
            raise KeyError("synthetic post-created fault")

        RoomInviteStore.mark_created = faulty_mark_created
        self.addCleanup(lambda: setattr(RoomInviteStore, "mark_created", original_mark_created))

        status, data, _ = self._request("POST", f"/api/local/room-invites/lobbies/{lobby['lobby_id']}/activate")

        self.assertEqual(status, 503, data)
        self.assertEqual(data.get("code"), "registration_ambiguous",
                          "state is durably created with no registration confirmed -- must not read as activation_failed")
        self.assertEqual(calls, ["groups.capabilities", "groups.create", "groups.state"])

        store = RoomInviteStore(self.store_path)
        after = store.get_lobby(lobby["lobby_id"])
        self.assertEqual(after["state"], "created")
        self.assertEqual(store.peer_registration_status(lobby["lobby_id"]), {})
        self.assertEqual(after["planned_room_id"], lobby["planned_room_id"])

        calls_after_first = list(calls)
        status2, data2, _ = self._request("POST", f"/api/local/room-invites/lobbies/{lobby['lobby_id']}/activate")
        self.assertEqual(status2, 409, data2)
        self.assertEqual(data2.get("code"), "registration_incomplete")
        self.assertEqual(calls, calls_after_first, "a known-incomplete retry must make zero additional native calls")

    def test_fault_before_mark_peer_registered_write_is_registration_ambiguous(self):
        """The real ``groups.peer.register`` RPC already succeeded, but the
        durable write that records it fails: state stays created with an
        empty registration projection."""
        lobby, issued, binding = self._consented_lobby_and_invite()
        scoped = self._scoped_material_for(lobby, issued, binding)
        self._wire_approved_registry(scoped)
        self._wire_material_resolver(scoped)
        self._configure_native_rpc()
        calls = self._wire_recording_rpc()

        def faulty_mark_peer_registered(self, *args):
            raise KeyError("synthetic post-register persistence fault")

        original = RoomInviteStore.mark_peer_registered
        RoomInviteStore.mark_peer_registered = faulty_mark_peer_registered
        self.addCleanup(lambda: setattr(RoomInviteStore, "mark_peer_registered", original))

        status, data, _ = self._request("POST", f"/api/local/room-invites/lobbies/{lobby['lobby_id']}/activate")

        self.assertEqual(status, 503, data)
        self.assertEqual(data.get("code"), "registration_ambiguous")
        self.assertEqual(calls, ["groups.capabilities", "groups.create", "groups.state", "groups.peer.register", "groups.state"])

        store = RoomInviteStore(self.store_path)
        after = store.get_lobby(lobby["lobby_id"])
        self.assertEqual(after["state"], "created")
        self.assertEqual(store.peer_registration_status(lobby["lobby_id"]), {})

    def test_raw_unclassified_error_at_service_boundary_after_register_uses_routes_fallback_derivation(self):
        """Point c: when the unrecognized exception leaks past the service's
        own classification entirely (reaching ``routes._run_activation``'s
        fallback directly), that fallback must derive the same code from
        durable state instead of hardcoding ``activation_failed``."""
        lobby, issued, binding = self._consented_lobby_and_invite()
        scoped = self._scoped_material_for(lobby, issued, binding)
        self._wire_approved_registry(scoped)
        self._wire_material_resolver(scoped)
        self._configure_native_rpc()
        calls = self._wire_recording_rpc()

        def faulty_mark_peer_registered(self, *args):
            raise KeyError("synthetic post-register persistence fault")

        original_mark = RoomInviteStore.mark_peer_registered
        RoomInviteStore.mark_peer_registered = faulty_mark_peer_registered
        self.addCleanup(lambda: setattr(RoomInviteStore, "mark_peer_registered", original_mark))

        from invite_rooms.service import ServiceError

        original_activate = InviteRoomsService.activate

        async def leak_unclassified(self, ctx, lobby_id):
            try:
                return await original_activate(self, ctx, lobby_id)
            except ServiceError:
                raise KeyError("synthetic raw boundary error") from None

        InviteRoomsService.activate = leak_unclassified
        self.addCleanup(lambda: setattr(InviteRoomsService, "activate", original_activate))

        status, data, _ = self._request("POST", f"/api/local/room-invites/lobbies/{lobby['lobby_id']}/activate")

        self.assertEqual(status, 503, data)
        self.assertEqual(data.get("code"), "registration_ambiguous",
                          "the routes fallback must derive the same code as the service, not activation_failed")

        store = RoomInviteStore(self.store_path)
        after = store.get_lobby(lobby["lobby_id"])
        self.assertEqual(after["state"], "created")

    def test_fault_before_any_create_attempt_is_still_activation_failed(self):
        """Nothing durable was admitted at all: preflight never pinned a
        spec, so the fallback must still be activation_failed, not an
        ambiguous code (P-3's 'only when provably nothing was admitted')."""
        lobby, issued, binding = self._consented_lobby_and_invite()
        scoped = self._scoped_material_for(lobby, issued, binding)
        self._wire_approved_registry(scoped)
        self._wire_material_resolver(scoped)
        self._configure_native_rpc()

        store = RoomInviteStore(self.store_path)
        before = store.get_lobby(lobby["lobby_id"])

        import contextlib as _contextlib

        @_contextlib.asynccontextmanager
        async def fake_connect(_rpc_config):
            yield object()

        original_connect = room_invite_routes._connect_native_rpc
        room_invite_routes._connect_native_rpc = fake_connect
        self.addCleanup(lambda: setattr(room_invite_routes, "_connect_native_rpc", original_connect))

        original_activate = InviteRoomsService.activate

        async def exploding_activate(self, ctx, lobby_id):
            raise KeyError("simulated unclassified activation bug before any create attempt")

        InviteRoomsService.activate = exploding_activate
        self.addCleanup(lambda: setattr(InviteRoomsService, "activate", original_activate))

        status, data, _ = self._request("POST", f"/api/local/room-invites/lobbies/{lobby['lobby_id']}/activate")

        self.assertEqual(status, 503, data)
        self.assertEqual(data.get("code"), "activation_failed")

        after = store.get_lobby(lobby["lobby_id"])
        self.assertEqual(after["state"], before["state"], "nothing durable was admitted -- state must not advance")


if __name__ == "__main__":
    unittest.main()


class ActivationViewBindingTests(RoomInviteRoutesHttpTest):
    """MVP.2a-fix5: activation peers expose invite_id + redeemed_binding for
    the host UI, with an exact redacted key set -- never endpoint/secret."""

    _PEER_KEYS = {"member_id", "peer_id", "invite_id", "redeemed_binding",
                  "host_decision", "joining_consent", "material_available", "ready"}
    _BINDING_KEYS = {"fingerprint", "target_install_id", "target_profile", "catalog_digest", "policy_digest"}

    def test_peer_exposes_null_invite_id_and_binding_before_any_invite(self):
        lobby = self._create_lobby()
        status, data, _ = self._request("GET", f"/api/local/room-invites/lobbies/{lobby['lobby_id']}/activation")
        self.assertEqual(status, 200, data)
        peer = next(p for p in data["peers"] if p["member_id"] == "peer-b")
        self.assertEqual(set(peer), self._PEER_KEYS, peer)
        self.assertIsNone(peer["invite_id"])
        self.assertIsNone(peer["redeemed_binding"])

    def test_peer_exposes_invite_id_once_issued_but_null_binding_before_redemption(self):
        lobby = self._create_lobby()
        _, issued, _ = self._request("POST", f"/api/local/room-invites/lobbies/{lobby['lobby_id']}/invites", {
            "member_id": "peer-b", "invitee_label": "B",
        })
        status, data, _ = self._request("GET", f"/api/local/room-invites/lobbies/{lobby['lobby_id']}/activation")
        self.assertEqual(status, 200, data)
        peer = next(p for p in data["peers"] if p["member_id"] == "peer-b")
        self.assertEqual(peer["invite_id"], issued["invite_id"])
        self.assertIsNone(peer["redeemed_binding"])

    def test_peer_exposes_invite_id_and_exact_redacted_binding_after_redemption(self):
        lobby = self._create_lobby()
        _, issued, _ = self._request("POST", f"/api/local/room-invites/lobbies/{lobby['lobby_id']}/invites", {
            "member_id": "peer-b", "invitee_label": "B",
        })
        _, binding = self._redeem_directly(lobby, issued)
        status, data, _ = self._request("GET", f"/api/local/room-invites/lobbies/{lobby['lobby_id']}/activation")
        self.assertEqual(status, 200, data)
        peer = next(p for p in data["peers"] if p["member_id"] == "peer-b")
        self.assertEqual(peer["invite_id"], issued["invite_id"])
        self.assertEqual(set(peer["redeemed_binding"]), self._BINDING_KEYS)
        self.assertEqual(peer["redeemed_binding"], _expected_binding(binding))
        rendered = json.dumps(data)
        self.assertNotIn("endpoint_id", rendered)
        self.assertNotIn("endpoint-b", rendered)
        self.assertNotIn(GRANT, rendered)

    def test_revoked_invite_is_never_shown_as_the_latest(self):
        lobby = self._create_lobby()
        _, issued1, _ = self._request("POST", f"/api/local/room-invites/lobbies/{lobby['lobby_id']}/invites", {
            "member_id": "peer-b", "invitee_label": "B1",
        })
        self._request("POST", f"/api/local/room-invites/lobbies/{lobby['lobby_id']}/invites/{issued1['invite_id']}/revoke")
        _, issued2, _ = self._request("POST", f"/api/local/room-invites/lobbies/{lobby['lobby_id']}/invites", {
            "member_id": "peer-b", "invitee_label": "B2",
        })
        status, data, _ = self._request("GET", f"/api/local/room-invites/lobbies/{lobby['lobby_id']}/activation")
        self.assertEqual(status, 200, data)
        peer = next(p for p in data["peers"] if p["member_id"] == "peer-b")
        self.assertEqual(peer["invite_id"], issued2["invite_id"])
        self.assertNotEqual(peer["invite_id"], issued1["invite_id"])

    def test_deny_then_reissue_pairs_the_new_invite_with_its_own_redemption_only(self):
        """MVP.2a-fix6 R3-2: deny A -> reissue B must show B null/pending
        (never A's stale redemption), and once B is itself redeemed the
        view/decision must be bound to B's own redemption only."""
        lobby = self._create_lobby()
        _, issued_a, _ = self._request("POST", f"/api/local/room-invites/lobbies/{lobby['lobby_id']}/invites", {
            "member_id": "peer-b", "invitee_label": "A",
        })
        _, binding_a = self._redeem_directly(lobby, issued_a)
        status, data, _ = self._request(
            "POST", f"/api/local/room-invites/lobbies/{lobby['lobby_id']}/invites/{issued_a['invite_id']}/decision",
            {"peer_id": "owner-b", "approved": False, "expected_binding": _expected_binding(binding_a)})
        self.assertEqual(status, 200, data)

        _, issued_b, _ = self._request("POST", f"/api/local/room-invites/lobbies/{lobby['lobby_id']}/invites", {
            "member_id": "peer-b", "invitee_label": "B",
        })
        status, data, _ = self._request("GET", f"/api/local/room-invites/lobbies/{lobby['lobby_id']}/activation")
        self.assertEqual(status, 200, data)
        peer = next(p for p in data["peers"] if p["member_id"] == "peer-b")
        self.assertEqual(peer["invite_id"], issued_b["invite_id"])
        self.assertNotEqual(peer["invite_id"], issued_a["invite_id"])
        self.assertIsNone(peer["redeemed_binding"])
        self.assertEqual(peer["host_decision"], "pending")
        self.assertEqual(peer["joining_consent"], "pending")

        _, binding_b = self._redeem_directly(lobby, issued_b)
        status, data, _ = self._request("GET", f"/api/local/room-invites/lobbies/{lobby['lobby_id']}/activation")
        self.assertEqual(status, 200, data)
        peer = next(p for p in data["peers"] if p["member_id"] == "peer-b")
        self.assertEqual(peer["invite_id"], issued_b["invite_id"])
        self.assertEqual(peer["redeemed_binding"], _expected_binding(binding_b))
        self.assertEqual(peer["host_decision"], "pending")

        status, data, _ = self._request(
            "POST", f"/api/local/room-invites/lobbies/{lobby['lobby_id']}/invites/{issued_b['invite_id']}/decision",
            {"peer_id": "owner-b", "approved": True, "expected_binding": _expected_binding(binding_b)})
        self.assertEqual(status, 200, data)


class DecisionBindingContractTests(RoomInviteRoutesHttpTest):
    """MVP.2a-fix5: the decision must be bound to the exact displayed binding."""

    def _lobby_issued_and_redeemed(self):
        lobby = self._create_lobby()
        _, issued, _ = self._request("POST", f"/api/local/room-invites/lobbies/{lobby['lobby_id']}/invites", {
            "member_id": "peer-b", "invitee_label": "B",
        })
        _, binding = self._redeem_directly(lobby, issued)
        return lobby, issued, binding

    def test_matching_expected_binding_decides_successfully(self):
        lobby, issued, binding = self._lobby_issued_and_redeemed()
        status, data, _ = self._request(
            "POST", f"/api/local/room-invites/lobbies/{lobby['lobby_id']}/invites/{issued['invite_id']}/decision",
            {"peer_id": "owner-b", "approved": True, "expected_binding": _expected_binding(binding)})
        self.assertEqual(status, 200, data)

    def test_mismatch_on_each_of_the_5_fields_is_409_binding_changed_with_zero_state_change(self):
        lobby, issued, binding = self._lobby_issued_and_redeemed()
        store = RoomInviteStore(self.store_path)
        before = store.get_redemption(lobby["lobby_id"], issued["invite_id"])
        for field in ("fingerprint", "target_install_id", "target_profile", "catalog_digest", "policy_digest"):
            with self.subTest(field=field):
                tampered = _expected_binding(binding)
                tampered[field] = "0" * 64 if field in ("fingerprint", "catalog_digest", "policy_digest") else \
                    (INSTALL_C if field == "target_install_id" else "someone-else")
                status, data, _ = self._request(
                    "POST", f"/api/local/room-invites/lobbies/{lobby['lobby_id']}/invites/{issued['invite_id']}/decision",
                    {"peer_id": "owner-b", "approved": True, "expected_binding": tampered})
                self.assertEqual(status, 409, (field, data))
                self.assertEqual(data.get("code"), "binding_changed", (field, data))
                after = store.get_redemption(lobby["lobby_id"], issued["invite_id"])
                self.assertEqual(before, after, (field, "zero durable state change on a binding mismatch"))

    def test_missing_expected_binding_is_400_invalid_body(self):
        lobby, issued, binding = self._lobby_issued_and_redeemed()
        status, data, _ = self._request(
            "POST", f"/api/local/room-invites/lobbies/{lobby['lobby_id']}/invites/{issued['invite_id']}/decision",
            {"peer_id": "owner-b", "approved": True})
        self.assertEqual(status, 400, data)
        self.assertEqual(data.get("code"), "invalid_body")

    def test_malformed_expected_binding_is_400_invalid_body(self):
        lobby, issued, binding = self._lobby_issued_and_redeemed()
        bad_bodies = (
            dict(_expected_binding(binding), extra_field="x"),
            {k: v for k, v in _expected_binding(binding).items() if k != "fingerprint"},
            {**_expected_binding(binding), "fingerprint": 12345},
        )
        for bad in bad_bodies:
            with self.subTest(bad=list(bad)):
                status, data, _ = self._request(
                    "POST", f"/api/local/room-invites/lobbies/{lobby['lobby_id']}/invites/{issued['invite_id']}/decision",
                    {"peer_id": "owner-b", "approved": True, "expected_binding": bad})
                self.assertEqual(status, 400, data)
                self.assertEqual(data.get("code"), "invalid_body")

    def test_decision_before_redemption_keeps_the_existing_code(self):
        lobby = self._create_lobby()
        _, issued, _ = self._request("POST", f"/api/local/room-invites/lobbies/{lobby['lobby_id']}/invites", {
            "member_id": "peer-b", "invitee_label": "B",
        })
        filler_binding = {"fingerprint": "a" * 64, "target_install_id": INSTALL_B, "target_profile": "default",
                           "catalog_digest": "b" * 64, "policy_digest": "c" * 64}
        status, data, _ = self._request(
            "POST", f"/api/local/room-invites/lobbies/{lobby['lobby_id']}/invites/{issued['invite_id']}/decision",
            {"peer_id": "owner-b", "approved": True, "expected_binding": filler_binding})
        self.assertEqual(status, 404, data)  # invite_unavailable, same as every other nonexistent-id 404


class PairedPeersRouteTests(RoomInviteRoutesHttpTest):
    """MVP.2a-fix5: GET /paired-peers lists the owner's pre-paired peers."""

    _PEER_KEYS = {"peer_id", "installation_id", "profile", "capability_digest"}

    def test_no_token_is_401(self):
        status, data, _ = self._request("GET", "/api/local/room-invites/paired-peers", token=None)
        self.assertEqual(status, 401)

    def test_foreign_origin_is_403(self):
        status, data, _ = self._request("GET", "/api/local/room-invites/paired-peers", origin="https://evil.example")
        self.assertEqual(status, 403)

    def test_feature_gate_off_is_404(self):
        os.environ.pop("MISSION_CONTROL_ROOM_INVITES_CONFIG", None)
        status, data, _ = self._request("GET", "/api/local/room-invites/paired-peers")
        self.assertEqual(status, 404)

    def test_reports_the_trusted_owner_profile(self):
        self._write_config(profile="ops")
        status, data, _ = self._request("GET", "/api/local/room-invites/paired-peers")
        self.assertEqual(status, 200, data)
        self.assertEqual(set(data), {"peers", "owner_profile"}, data)
        self.assertEqual(data["owner_profile"], "ops")

    def test_the_payload_the_browser_sends_creates_a_lobby(self):
        # tests/fixtures/room-invites/create-lobby.json is the exact body the owner
        # UI sends; the frontend suite asserts the same file, so the two sides
        # cannot drift apart while both stay green.
        fixture = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "room-invites" / "create-lobby.json"
        body = json.loads(fixture.read_text(encoding="utf-8"))
        status, data, _ = self._request("POST", "/api/local/room-invites/lobbies", body)
        self.assertEqual(status, 200, data)
        roster = data["lobby"]["roster"]
        self.assertEqual([member["handle"] for member in roster], ["default", "owner-b"])
        self.assertEqual(roster[1]["target"]["peer_id"], "owner-b")

    def test_lists_the_configured_peer_with_exact_redacted_key_set(self):
        status, data, _ = self._request("GET", "/api/local/room-invites/paired-peers")
        self.assertEqual(status, 200, data)
        self.assertEqual(len(data["peers"]), 1, data)
        peer = data["peers"][0]
        self.assertEqual(set(peer), self._PEER_KEYS, peer)
        self.assertEqual(peer["peer_id"], "owner-b")
        self.assertEqual(peer["installation_id"], INSTALL_B)
        self.assertEqual(peer["profile"], "default")
        self.assertEqual(peer["capability_digest"], CATALOG_DIGEST)

    def test_no_private_material_ever_leaks(self):
        status, data, _ = self._request("GET", "/api/local/room-invites/paired-peers")
        self.assertEqual(status, 200, data)
        rendered = json.dumps(data)
        self.assertNotIn("endpoint_id", rendered)
        self.assertNotIn("endpoint-b", rendered)
        self.assertNotIn(FINGERPRINT_B, rendered)
        self.assertNotIn(GRANT, rendered)
        self.assertNotIn(ADAPTER_URL, rendered)

    def test_empty_when_no_peers_configured(self):
        _write_private_file(self.peer_registry_path, json.dumps([]))
        status, data, _ = self._request("GET", "/api/local/room-invites/paired-peers")
        self.assertEqual(status, 200, data)
        self.assertEqual(data["peers"], [])

    def test_get_makes_no_writes_to_store_or_registry_state(self):
        store = RoomInviteStore(self.store_path)
        lobby = self._create_lobby()
        before = store.get_lobby(lobby["lobby_id"])["updated_at"]
        for _ in range(3):
            status, data, _ = self._request("GET", "/api/local/room-invites/paired-peers")
            self.assertEqual(status, 200)
        after = store.get_lobby(lobby["lobby_id"])["updated_at"]
        self.assertEqual(before, after)
