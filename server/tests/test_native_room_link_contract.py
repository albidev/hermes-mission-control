"""N1: canonical transport authentication is not a RoomLink capability grant.

The response below is the disabled payload witnessed on two real native owners
at Core eb30bd6e48a86bcbcbbc0f343e7c0f0aadfe2dc4. Only its public installation
identity is replaced by the disposable fixture identity. The connector is a
labelled response fixture, NOT native E2E or a rich owner catalog. HTTP, route,
PairingService, journal and identity behavior remain real candidate code.
"""
from __future__ import annotations

import copy
import json
import os
from contextlib import asynccontextmanager
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_room_invite_pairing_routes as support
from invite_rooms import routes
from invite_rooms.service import ServiceError


CANONICAL_DISABLED = {
    "protocol_version": 2,
    "driver": True,
    "persistent_process": True,
    "authority_gateway_id": support.ps.INSTALL_A,
    "room_link": {"enabled": False, "reason": "canonical_driver_required"},
    "features": ["room_identity", "monotonic_log", "replayable_disband"],
    "methods": [
        "groups.capabilities", "groups.list", "groups.state", "groups.log",
        "groups.create", "groups.rename", "groups.disband", "groups.send",
        "groups.attachment.upload", "groups.attachment.download", "groups.stop",
        "groups.retry", "groups.discard", "groups.approve",
    ],
    "max_log_limit": 500,
}


def connector(payload, calls):
    @asynccontextmanager
    async def connect(config):
        async def rpc(method, params):
            calls.append((method, params))
            return copy.deepcopy(payload)
        yield rpc
    return connect


def setUpModule():
    support.setUpModule()


def tearDownModule():
    support.tearDownModule()


class NativeRoomLinkCatalogTests(unittest.TestCase):
    def test_running_driver_without_room_link_catalog_is_not_pairing_capability(self):
        calls = []
        config = SimpleNamespace(native_rpc={"profile_home": "/disposable/native"}, profile="default")
        with patch.object(routes, "_PAIRING_CATALOG_SOURCE", None), patch.object(
            routes, "_connect_native_rpc", connector(CANONICAL_DISABLED, calls)
        ):
            with self.assertRaisesRegex(ServiceError, "native_contract_unavailable"):
                routes._pairing_catalog(config, None)
        self.assertEqual(calls, [("groups.capabilities", {"profile": "default"})])

    def test_driver_flag_cannot_substitute_for_enabled_true_and_catalog(self):
        config = SimpleNamespace(native_rpc={}, profile="lab-target")
        for link in (None, {"enabled": True}, {"enabled": 1, "catalog": {}},
                     {"enabled": False, "catalog": {}}):
            with self.subTest(link=link):
                payload = dict(CANONICAL_DISABLED, room_link=link)
                calls = []
                with patch.object(routes, "_PAIRING_CATALOG_SOURCE", None), patch.object(
                    routes, "_connect_native_rpc", connector(payload, calls)
                ):
                    with self.assertRaisesRegex(ServiceError, "native_contract_unavailable"):
                        routes._pairing_catalog(config, None)
                self.assertEqual(calls, [("groups.capabilities", {"profile": "lab-target"})])


class NativeRoomLinkHTTPTests(support.PairingRoutesBase):
    def write_config(self, pairing="default"):
        super().write_config(pairing)
        home = self.tmp / "native"
        home.mkdir(mode=0o700, exist_ok=True)
        identity = home / "install_id"
        identity.write_text(support.ps.INSTALL_A.removeprefix("install:") + "\n")
        os.chmod(identity, 0o600)
        payload = json.loads(self.config_path.read_text())
        payload["native_rpc"] = {"profile_home": str(home)}
        self.config_path.write_text(json.dumps(payload))

    def setUp(self):
        super().setUp()
        os.environ["MISSION_CONTROL_ALLOWED_ORIGIN"] = "http://%s:%s" % self.httpd.server_address
        # Deliberately bypass the old rich-catalog test hook. Only the native
        # transport boundary is replaced, with the actual disabled wire shape.
        routes._PAIRING_CATALOG_SOURCE = None
        self.calls = []
        self.connector_patch = patch.object(
            routes, "_connect_native_rpc", connector(CANONICAL_DISABLED, self.calls)
        )
        self.connector_patch.start()
        self.addCleanup(self.connector_patch.stop)

    def test_owner_offer_is_503_and_no_pairing_or_peer_is_created(self):
        origin = "http://%s:%s" % self.httpd.server_address
        before_peers = self.A.peers_path.read_bytes()
        status, body, headers = self.req("POST", "/pairing/offers", {}, origin=origin)
        self.assertEqual((status, body), (503, {
            "error": "unavailable", "code": "pairing_native_unavailable"}))
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        self.assertEqual(self.calls, [("groups.capabilities", {"profile": "default"})])
        self.assertEqual(self.A.peers_path.read_bytes(), before_peers)
        status, body, _ = self.req("GET", "/pairing", origin=origin)
        self.assertEqual((status, body), (200, {"available": True, "pairings": []}))

    def test_auth_and_origin_refuse_before_native_capability_lookup(self):
        origin = "http://%s:%s" % self.httpd.server_address
        self.assertEqual(self.req("POST", "/pairing/offers", {}, token="", origin=origin)[0], 401)
        self.assertEqual(self.req("POST", "/pairing/offers", {}, origin="https://evil.example")[0], 403)
        self.assertEqual(self.calls, [])
