from __future__ import annotations

import io
import asyncio
import json
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from invite_rooms import routes

from invite_rooms.host_control import _status
from invite_rooms.service import InviteRoomsService, OwnerContext
from room_invite_store import RoomInviteStore
from invite_rooms import sidecar_data_plane
from invite_rooms.pairing_journal import MaterialPeerContext


INSTALL_A = "install:" + "a" * 32
INSTALL_B = "install:" + "b" * 32
ROSTER = [
    {"member_id": "local-a", "profile": "default", "handle": "local"},
    {"member_id": "peer-b", "profile": "default", "handle": "peer",
     "target": {"kind": "peer", "peer_id": "owner-b", "installation_id": INSTALL_B, "profile": "default"}},
]


class StoreHintTests(unittest.TestCase):
    def test_store_round_trip_and_empty_has_no_row(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "room.sqlite"
            store = RoomInviteStore(path)
            plain = store.create_lobby(name="Plain", roster=ROSTER)
            try:
                hinted = store.create_lobby(name="Hinted", roster=ROSTER, grant_ttl_hint_seconds=3600)
            except sqlite3.OperationalError as exc:
                self.fail(f"atomicity property failed with lock/crash: {exc}")
            self.assertIsNone(plain["grant_ttl_hint_seconds"])
            self.assertEqual(hinted["grant_ttl_hint_seconds"], 3600)
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM lobby_grant_ttl WHERE lobby_id=?", (plain["lobby_id"],)).fetchone()[0], 0)
                self.assertEqual(db.execute("SELECT hint_seconds FROM lobby_grant_ttl WHERE lobby_id=?", (hinted["lobby_id"],)).fetchone()[0], 3600)

    def test_real_peer_scope_status_without_hint_has_exact_wire_keys(self):
        with tempfile.TemporaryDirectory() as td:
            store = RoomInviteStore(Path(td) / "room.sqlite")
            store._peer_snapshot = lambda *a, **k: {"redemption": {"host_decision": "approved", "joining_consent": "given",
                "endpoint_id": "e", "fingerprint": "f", "target_install_id": "i", "target_profile": "p",
                "catalog_digest": "c", "policy_digest": "d"}, "binding": {"endpoint_id": "e", "fingerprint": "f",
                "target_install_id": "i", "target_profile": "p", "catalog_digest": "c", "policy_digest": "d"},
                "owner": {"installation_id": "owner"}, "lobby": {"planned_room_id": "room"},
                "invite": {"member_id": "member"}}
            result = store.peer_scope_status("lobby", "invite", peer_id="peer", binding={"endpoint_id":"e","fingerprint":"f","target_install_id":"i","target_profile":"p","catalog_digest":"c","policy_digest":"d"}, before_return=lambda: True)
            self.assertEqual(set(result), {"status", "scope"})
    def test_real_peer_scope_status_uses_each_lobby_hint(self):
        store = RoomInviteStore(Path(tempfile.mkdtemp()) / "room.sqlite")
        binding = {"endpoint_id": "e", "fingerprint": "f", "target_install_id": "t",
                   "target_profile": "p", "catalog_digest": "c", "policy_digest": "d"}
        snap = {"owner": {"installation_id": "owner"}, "lobby": {"planned_room_id": "room"},
                "invite": {"member_id": "member"},
                "redemption": {**binding, "host_decision": "approved", "joining_consent": "given"},
                "binding": binding}
        store._peer_snapshot = mock.Mock(return_value=snap)
        store.lobby_grant_ttl_hint = mock.Mock(side_effect=lambda lobby: {"A": 600, "B": 3600}.get(lobby))
        try:
            a = store.peer_scope_status("A", "invite", peer_id="peer", binding=binding, before_return=lambda: True)
            b = store.peer_scope_status("B", "invite", peer_id="peer", binding=binding, before_return=lambda: True)
            self.assertEqual(a["grant_ttl_hint_seconds"], 600)
            self.assertEqual(b["grant_ttl_hint_seconds"], 3600)
            self.assertEqual(store.lobby_grant_ttl_hint.call_args_list, [mock.call("A"), mock.call("B")])
        finally:
            pass


class ServiceHintTests(unittest.TestCase):
    def test_service_accepts_hint_and_policy_rejects_extended_without_flag(self):
        with tempfile.TemporaryDirectory() as td:
            store = RoomInviteStore(Path(td) / "room.sqlite")
            from invite_rooms.peer_registry import PeerRegistry
            registry = PeerRegistry()
            owner = OwnerContext(INSTALL_A, "default")
            service = InviteRoomsService(store, registry, trusted_owner=owner)
            with self.assertRaisesRegex(Exception, "invalid_lobby"):
                service.create_lobby(owner, name="Bad", roster=ROSTER, grant_ttl_hint_seconds=3600)

    def test_service_dependency_flag_is_exact_bool(self):
        from invite_rooms.peer_registry import PeerRegistry
        from invite_rooms.service import ServiceError
        for value in (1, "true", None, 0):
            with tempfile.TemporaryDirectory() as td:
                with self.assertRaisesRegex(ServiceError, "invalid_service_dependencies"):
                    InviteRoomsService(RoomInviteStore(Path(td) / "room.sqlite"), PeerRegistry(),
                                       trusted_owner=OwnerContext(INSTALL_A, "default"),
                                       lobby_grant_ttl_extended_presets=value)
        with tempfile.TemporaryDirectory() as td:
            self.assertIsInstance(InviteRoomsService(RoomInviteStore(Path(td) / "room.sqlite"), PeerRegistry(),
                                                     trusted_owner=OwnerContext(INSTALL_A, "default"),
                                                     lobby_grant_ttl_extended_presets=True), InviteRoomsService)


class HostStatusHintTests(unittest.TestCase):
    def test_status_adds_unsigned_hint_only_when_present(self):
        class Context:
            peer_id = "owner-b"
            readmit = staticmethod(lambda: True)
            def binding_dict(self):
                return {"endpoint_id": "endpoint-b", "fingerprint": "b" * 64,
                        "target_install_id": INSTALL_B, "target_profile": "default",
                        "catalog_digest": "c" * 64, "policy_digest": "d" * 64}
        scope = {"host_id": INSTALL_A, "room_id": "room", "member_id": "peer-b", "lobby_id": "lobby",
                 "invite_id": "invite", "binding": Context().binding_dict()}
        class Store:
            def peer_scope_status(self, *args, **kwargs):
                return {"status": "ready", "scope": scope, "grant_ttl_hint_seconds": 3600}
        result = _status(Store(), Context(), {"op": "status", "lobby_id": "lobby", "invite_id": "invite"})
        self.assertEqual(result["grant_ttl_hint_seconds"], 3600)
        self.assertNotIn("grant_ttl_hint_seconds", result["scope"])

    def test_status_without_hint_is_byte_identical(self):
        class Context:
            peer_id = "owner-b"
            readmit = staticmethod(lambda: True)
            def binding_dict(self):
                return {"endpoint_id": "endpoint-b", "fingerprint": "b" * 64,
                        "target_install_id": INSTALL_B, "target_profile": "default",
                        "catalog_digest": "c" * 64, "policy_digest": "d" * 64}
        scope = {"host_id": INSTALL_A, "room_id": "room", "member_id": "peer-b", "lobby_id": "lobby",
                 "invite_id": "invite", "binding": Context().binding_dict()}
        class Store:
            def peer_scope_status(self, *args, **kwargs):
                return {"status": "ready", "scope": scope}
        self.assertEqual(_status(Store(), Context(), {"op": "status", "lobby_id": "lobby", "invite_id": "invite"}),
                         {"status": "ready", "scope": scope})


class RouteAndStoreHardeningTests(unittest.TestCase):
    def test_real_sidecar_status_validator_rejects_bad_hint_types_ranges_and_shape(self):
        # Exercise the actual MaterialControl resolver, not a copied predicate.
        binding = {"endpoint_id": "endpoint-b", "fingerprint": "b" * 64,
                   "target_install_id": INSTALL_B, "target_profile": "default",
                   "catalog_digest": "c" * 64, "policy_digest": "d" * 64}
        context = MaterialPeerContext._issue(lambda: True, endpoint_id="endpoint-b", generation=1,
            role="host", protocol_state="acknowledged", peer_id="peer-b", binding=tuple(binding.items()),
            ca_pem="CA", fingerprint=binding["fingerprint"], consent_public_key=b"key",
            operation="material_receipt", deadline=9999999999)
        scope = {"host_id": INSTALL_A, "room_id": "room", "member_id": "peer-b",
                 "lobby_id": "lobby", "invite_id": "invite", "binding": binding}
        base = {"status": "ready", "scope": scope}
        class Store:
            def peer_scope_status(self, *args, **kwargs): return dict(base)
        class Authority:
            def peer_trust(self, *args, **kwargs):
                return type("Trust", (), {"peer_id": "peer-b", "binding": tuple(binding.items()),
                    "fingerprint": binding["fingerprint"], "ca_pem": "CA"})()
        class Service: store = Store()
        class Ctx: installation_id = INSTALL_A; profile = "default"
        class Coordinator:
            post_receipt = staticmethod(lambda *a, **k: None)
            post_target = staticmethod(lambda *a, **k: None)
        with mock.patch.object(sidecar_data_plane, "_config_connector_factory", return_value=lambda *a: None):
            control = sidecar_data_plane._material_control(Service(), Ctx(), object(), object(),
                Authority(), object(), Coordinator())
        request = {"lobby_id": "lobby", "invite_id": "invite"}
        for value in (True, False, 59, 86401, "600", 600.0, None, -1):
            with self.subTest(value=value):
                status = dict(base, grant_ttl_hint_seconds=value)
                with mock.patch.object(Service.store, "peer_scope_status", return_value=status):
                    with self.assertRaises(Exception, msg=f"validator accepted invalid hint {value!r}"):
                        asyncio.run(control._scope_status_resolver(context=context, request=request, scope=scope, deadline=9999999999))
        for value in (60, 600, 86400):
            with self.subTest(value=value):
                status = dict(base, grant_ttl_hint_seconds=value)
                with mock.patch.object(Service.store, "peer_scope_status", return_value=status):
                    self.assertEqual(asyncio.run(control._scope_status_resolver(
                        context=context, request=request, scope=scope, deadline=9999999999))["grant_ttl_hint_seconds"], value)
        malformed = (
            {"scope": base["scope"], "grant_ttl_hint_seconds": 600},
            {"status": base["status"], "grant_ttl_hint_seconds": 600},
            {"grant_ttl_hint_seconds": 600}, {},
            {"scope": base["scope"]}, {"status": base["status"]},
            {"status": base["status"], "scope": base["scope"], "unknown": 1},
        )
        for status in malformed:
            with mock.patch.object(Service.store, "peer_scope_status", return_value=status):
                with self.assertRaises(Exception, msg=f"validator accepted malformed status {status!r}"):
                    asyncio.run(control._scope_status_resolver(context=context, request=request, scope=scope, deadline=9999999999))
        for status in ({"status": base["status"], "scope": base["scope"]},
                       {"status": base["status"], "scope": base["scope"], "grant_ttl_hint_seconds": 600}):
            with mock.patch.object(Service.store, "peer_scope_status", return_value=status):
                self.assertEqual(asyncio.run(control._scope_status_resolver(
                    context=context, request=request, scope=scope, deadline=9999999999)), status)

    def test_real_create_lobby_handler_has_closed_integer_matrix_and_never_500(self):
        class Handler:
            headers = {"Content-Type": "application/json", "Content-Length": "0"}
            def __init__(self, body):
                self.body = body
                self.responses = []
                self.headers["Content-Length"] = str(len(body))
                self.rfile = io.BytesIO(body)
            def _json(self, status, payload):
                self.responses.append((status, payload))

        class Service:
            def __init__(self, extended): self.extended = extended
            def create_lobby(self, _ctx, **kwargs):
                hint = kwargs.get("grant_ttl_hint_seconds")
                allowed = hint is None or hint == 600 or (self.extended and hint in (3600, 28800, 86400))
                if not allowed:
                    from invite_rooms.service import ServiceError
                    raise ServiceError("invalid_lobby")
                return {"lobby_id": "lobby-test"}
        values = [None, 59, 60, 600, 601, 3600, 28800, 86400, 86401, 0, -1,
                  True, False, 600.0, "600", [], {}, 10**100]
        for flag in (False, True):
            for value in values:
                with self.subTest(flag=flag, value=value):
                    payload = {"name": "Matrix", "roster": ROSTER}
                    if value is not None:
                        payload["grant_ttl_hint_seconds"] = value
                    handler = Handler(json.dumps(payload).encode())
                    with mock.patch.object(routes, "_expand_roster", return_value=ROSTER):
                        routes._h_create_lobby(handler, Service(flag), object(), mock.Mock(), (), {})
                    self.assertEqual(len(handler.responses), 1)
                    status, _ = handler.responses[0]
                    expected = 200 if (value is None or (type(value) is int and (value == 600 or (flag and value in (3600, 28800, 86400))))) else 400
                    self.assertEqual(status, expected, (flag, value, handler.responses))

    def test_route_itself_rejects_bool_before_service(self):
        class Handler:
            headers = {"Content-Type": "application/json"}
            def __init__(self):
                body = json.dumps({"name": "Matrix", "roster": ROSTER, "grant_ttl_hint_seconds": True}).encode()
                self.rfile, self.headers, self.responses = io.BytesIO(body), {"Content-Type": "application/json", "Content-Length": str(len(body))}, []
            def _json(self, status, payload): self.responses.append((status, payload))
        service = mock.Mock()
        service.create_lobby.return_value = {"lobby_id": "ok"}
        handler = Handler()
        with mock.patch.object(routes, "_expand_roster", return_value=ROSTER):
            routes._h_create_lobby(handler, service, object(), mock.Mock(), (), {})
        self.assertEqual(handler.responses[0][0], 400)
        service.create_lobby.assert_not_called()

        class Handler:
            headers = {"Content-Type": "application/json"}
            def __init__(self, body):
                self.rfile = io.BytesIO(body)
                self.headers["Content-Length"] = str(len(body))
                self.responses = []
            def _json(self, status, payload): self.responses.append((status, payload))
        service = mock.Mock()
        for extra in ({"unknown": 1}, {"grant_ttl_seconds": 600}):
            body = json.dumps({"name": "Matrix", "roster": ROSTER, **extra}).encode()
            handler = Handler(body)
            routes._h_create_lobby(handler, service, object(), mock.Mock(), (), {})
            self.assertEqual(handler.responses[0][0], 400)
            service.create_lobby.assert_not_called()

    def test_hint_is_immutable_and_lobby_plus_sidecar_is_atomic(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "room.sqlite"
            store = RoomInviteStore(path)
            try:
                lobby = store.create_lobby(name="Hinted", roster=ROSTER, grant_ttl_hint_seconds=3600)
            except sqlite3.OperationalError as exc:
                self.fail(f"atomicity property failed with lock/crash: {exc}")
            self.assertEqual(store.lobby_grant_ttl_hint(lobby["lobby_id"]), 3600)
            self.assertFalse(hasattr(store, "update_lobby_grant_ttl"))
            @contextmanager
            def failing_write():
                conn = sqlite3.connect(broken.path, isolation_level=None)
                try:
                    conn.execute("PRAGMA foreign_keys=ON")
                    conn.execute("BEGIN IMMEDIATE")
                    class ConnProxy:
                        def __init__(self, c): self.c, self.lobby_seen = c, False
                        def execute(self, sql, params=()):
                            result = self.c.execute(sql, params)
                            if "INSERT INTO lobbies" in sql:
                                self.lobby_seen = True
                            if self.lobby_seen and "INSERT INTO lobby_grant_ttl" in sql:
                                raise RuntimeError("after lobby insert")
                            return result
                        def commit(self): return self.c.commit()
                        def rollback(self): return self.c.rollback()
                    yield ConnProxy(conn)
                    conn.commit()
                except BaseException:
                    conn.rollback()
                    raise
                finally:
                    conn.close()
            broken = RoomInviteStore(Path(td) / "broken.sqlite")
            with mock.patch.object(broken, "_write", failing_write):
                try:
                    broken.create_lobby(name="Atomic", roster=ROSTER, grant_ttl_hint_seconds=3600)
                except RuntimeError:
                    pass
                except sqlite3.OperationalError as exc:
                    self.fail(f"atomicity property failed with lock/crash: {exc}")
                except Exception as exc:
                    self.fail(f"atomicity property failed unexpectedly: {exc}")
                else:
                    self.fail("atomicity property was not exercised")
            with sqlite3.connect(broken.path) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM lobbies").fetchone()[0], 0)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM lobby_grant_ttl").fetchone()[0], 0)
            retry = broken.create_lobby(name="Atomic retry", roster=ROSTER, grant_ttl_hint_seconds=3600)
            with sqlite3.connect(broken.path) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM lobbies WHERE lobby_id=?", (retry["lobby_id"],)).fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT hint_seconds FROM lobby_grant_ttl WHERE lobby_id=?", (retry["lobby_id"],)).fetchone()[0], 3600)

            # The database owns the invariant on both the fresh schema and the
            # migration-created side table, independently of store validation.
            with sqlite3.connect(path) as db:
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute("INSERT INTO lobby_grant_ttl(lobby_id,hint_seconds) VALUES(?,?)", (lobby["lobby_id"], 59))
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute("UPDATE lobby_grant_ttl SET hint_seconds=86401 WHERE lobby_id=?", (lobby["lobby_id"],))
            migrated = Path(td) / "migrated.sqlite"
            RoomInviteStore(migrated).create_lobby(name="Seed", roster=ROSTER)
            with sqlite3.connect(migrated) as db:
                db.execute("DROP TABLE lobby_grant_ttl")
            migrated_store = RoomInviteStore(migrated)
            migrated_lobby = migrated_store.create_lobby(name="Migrated", roster=ROSTER, grant_ttl_hint_seconds=3600)
            with sqlite3.connect(migrated) as db:
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute("INSERT INTO lobby_grant_ttl(lobby_id,hint_seconds) VALUES(?,?)", (migrated_lobby["lobby_id"], 59))
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute("UPDATE lobby_grant_ttl SET hint_seconds=86401 WHERE lobby_id=?", (migrated_lobby["lobby_id"],))

    def test_future_schema_without_sidecar_is_readable_and_other_operational_error_propagates(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "legacy.sqlite"
            with sqlite3.connect(path) as db:
                db.executescript("CREATE TABLE schema_version(version INTEGER NOT NULL); INSERT INTO schema_version VALUES(4); CREATE TABLE lobbies(lobby_id TEXT PRIMARY KEY, room_id TEXT, planned_room_id TEXT, name TEXT, state TEXT, roster_json TEXT, native_spec_json TEXT, created_at REAL, updated_at REAL); INSERT INTO lobbies VALUES('lobby-x',NULL,'room-x','X','open','[]',NULL,1,1);")
            before = path.read_bytes()
            store = RoomInviteStore(path)
            self.assertTrue(store.read_only)
            self.assertIsNone(store.get_lobby("lobby-x")["grant_ttl_hint_seconds"])
            self.assertIsNone(store.get_history_policy("lobby-x"))
            self.assertEqual(path.read_bytes(), before, 'opening a future schema must not migrate it')
            class BrokenConnection:
                def __enter__(self): return self
                def __exit__(self, *args): return False
                def execute(self, *_args): raise sqlite3.OperationalError("disk I/O error")
            with mock.patch.object(store, "_connection", return_value=BrokenConnection()):
                with self.assertRaisesRegex(sqlite3.OperationalError, "disk I/O"):
                    store.lobby_grant_ttl_hint("lobby-x")


if __name__ == "__main__":
    unittest.main()
