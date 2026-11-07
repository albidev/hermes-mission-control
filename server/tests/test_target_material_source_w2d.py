"""Behavioural tests for the read-only target material source.

The positive fixture deliberately crosses the accepted W2c material path.  It
is kept here rather than importing a parent scratch module so this test module
is a durable witness of the real producer, not an API-only probe.
"""
from __future__ import annotations

import base64
import copy
import inspect
import json
import os
import re
import secrets
import select
import subprocess
import sys
import time
import unittest
from contextlib import contextmanager
from dataclasses import FrozenInstanceError, replace
from unittest import mock

from invite_rooms.runtime_config import TargetHttpConfig
from invite_rooms.target_material import TargetMaterialSource, TargetMaterialSourceError


class _Captured(Exception):
    pass


@contextmanager
def genuine_source_fixture():
    """Build one material row through real journals, mTLS and MaterialControl."""
    import test_material_control_mtls_w2c as w2c
    import test_room_invite_joining as joining_tests
    from gateway.hosted_room_peer import GatewayRoomCatalog, decode_room_grant, issue_room_grant
    from invite_rooms.consent import canonical
    from invite_rooms.identity import load_identity

    class Flow(w2c.RealTwoListenerMaterialRoundtrip):
        def assertEqual(self, first, second, msg=None):
            super().assertEqual(first, second, msg)
            frame = inspect.currentframe().f_back
            if (first == {"status": "material_ready"} and second == first
                    and frame.f_code.co_name == "test_material_request_and_receipt_cross_two_real_listeners"):
                self.producer = dict(frame.f_locals)
                raise _Captured()

    flow = Flow("test_material_request_and_receipt_cross_two_real_listeners")
    native_secret = secrets.token_bytes(32)
    issued = []
    flow.setUp()
    original_rpc = w2c.BoundedNativeRpc.__call__

    async def native_with_real_grant(rpc, method, params, **kwargs):
        response = await original_rpc(rpc, method, params, **kwargs)
        if method == "groups.peer.invite":
            catalog = GatewayRoomCatalog.from_mapping(response["catalog"])
            token = issue_room_grant(
                native_secret, grant_id=params["grant_id"], room_id=params["room_id"],
                home_install_id=params["home_install_id"],
                authority_gateway_id=params["authority_gateway_id"],
                authority_epoch=params["authority_epoch"], member_id=params["member_id"],
                target_install_id=catalog.installation_id, target_profile=params["profile"],
                execution_policy_digest=catalog.execution_policy.policy_digest,
                issued_at=time.time(), ttl_seconds=params["ttl_seconds"],
            )
            response = dict(response, grant=token)
            issued.append(token)
        return response

    try:
        def paired_consent_key(root):
            import os
            from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat
            from invite_rooms.consent import ConsentKey
            root.mkdir(mode=0o700, parents=True)
            raw = flow.b_bundle.consent_key().private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
            fd = os.open(root / "consent.key", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(raw)
            return ConsentKey(root)

        with mock.patch.object(joining_tests, "ConsentKey", side_effect=paired_consent_key), \
                mock.patch.object(w2c.BoundedNativeRpc, "__call__", native_with_real_grant):
            try:
                flow.test_material_request_and_receipt_cross_two_real_listeners()
            except _Captured:
                pass
            else:
                raise AssertionError("producer capture boundary never reached")
        local = flow.producer
        fx, scope, wire_scope = local["fx"], local["scope"], local["scope_wire"]
        assert len(issued) == len(local["native_calls"]) == 1
        key = canonical(wire_scope).decode()
        reference = fx["joining_store"].mint_grant_id(key)
        claims = decode_room_grant(native_secret, issued[0], permission="dispatch")
        assert claims["grant_id"] == reference
        assert fx["joining_store"].mint_state(key) == "ready"
        source = TargetMaterialSource(
            joining_store=fx["joining_store"], authority=local["authority_b"],
            identity_reader=lambda *, deadline: load_identity(
                flow.b_side.identity_dir, installation_id=w2c.INSTALL_B, profile="default"),
            catalog_reader=lambda *, deadline: copy.deepcopy(flow.b_side.catalog),
            target_http=TargetHttpConfig("http://127.0.0.1:19001", "default"),
            endpoint_id=scope.endpoint_id, generation=scope.pairing_generation,
        )
        yield {"flow": flow, "producer": local, "fx": fx, "scope": scope,
               "source": source, "credential_ref": reference, "grant": issued[0],
               "scope_key": key, "mint_count": len(issued), "native_secret": native_secret}
    finally:
        flow.close_listeners()
        if hasattr(flow, "producer"):
            for name in ("joining_store", "store"):
                owned = flow.producer["fx"][name]
                close = getattr(owned, "close", None)
                if close is not None:
                    close()
        flow.doCleanups()


class TargetMaterialSourceContractTests(unittest.TestCase):
    def _rows(self, store):
        with store._read() as connection:
            return tuple(tuple(row) for row in connection.execute(
                "SELECT * FROM material ORDER BY scope_key"))

    def _controls(self, fx):
        """Capture durable rows and the real native boundary counter."""
        return self._rows(fx["fx"]["joining_store"]), len(fx["producer"]["native_calls"])

    def _assert_controls(self, fx, controls):
        self.assertEqual(self._controls(fx), controls)

    def _signed(self, fx, mutate):
        store, key = fx["fx"]["joining_store"], fx["scope_key"]
        envelope = json.loads(store.mint_load(key).decode("utf-8"))
        mutate(envelope)
        unsigned = dict(envelope)
        unsigned.pop("signature")
        from invite_rooms.consent import canonical
        envelope["signature"] = base64.b64encode(fx["flow"].b_bundle.consent_key().sign(
            canonical(dict(unsigned, purpose="joining-owner-material-v2")))).decode()
        return canonical(envelope)

    def _positive(self, fx, source=None, scope=None):
        source = source or fx["source"]
        scope = scope or fx["scope"]
        before = self._controls(fx)
        value = source.load(scope, credential_ref=fx["credential_ref"], deadline=time.monotonic() + 3)
        from gateway.hosted_room_peer import decode_room_grant
        self.assertEqual(
            decode_room_grant(fx["native_secret"], fx["grant"], permission="dispatch")["grant_id"],
            fx["credential_ref"],
        )
        self.assertEqual(value["grant"], fx["grant"])
        self.assertEqual(value["catalog"], source._catalog_reader(deadline=time.monotonic() + 3))
        self.assertEqual(value["binding"], scope.binding)
        self.assertEqual(fx["producer"]["native_calls"][-1]["method"], "groups.peer.invite")
        self._assert_controls(fx, before)

    def _deny(self, callback):
        with self.assertRaises(TargetMaterialSourceError):
            callback()

    def test_requires_finite_future_absolute_deadline(self):
        with genuine_source_fixture() as fx:
            for value in (None, True, float("nan"), float("inf"), time.monotonic() - 1):
                with self.subTest(value=value):
                    with self.assertRaises(TargetMaterialSourceError):
                        fx["source"].resolve(lobby_id=fx["scope"].lobby_id,
                                              invite_id=fx["scope"].invite_id,
                                              deadline=value)  # type: ignore[arg-type]

    def test_source_rejects_invalid_link_and_keeps_config_immutable(self):
        with self.assertRaises(TargetMaterialSourceError):
            TargetMaterialSource(
                joining_store=object(), authority=object(), identity_reader=lambda **kwargs: None,
                catalog_reader=lambda **kwargs: None,
                target_http=TargetHttpConfig("http://127.0.0.1:1", "default"),
                endpoint_id="endpoint-a", generation=True,
            )
        with genuine_source_fixture() as fx:
            config = fx["source"]._target_http
            self.assertEqual(config, TargetHttpConfig("http://127.0.0.1:19001", "default"))
            with self.assertRaises(FrozenInstanceError):
                setattr(config, "base_url", "http://127.0.0.1:2")

    def test_genuine_producer_positive_is_detached_and_reusable(self):
        with genuine_source_fixture() as fx:
            scope, source = fx["scope"], fx["source"]
            resolved = source.resolve(lobby_id=scope.lobby_id, invite_id=scope.invite_id,
                                      deadline=time.monotonic() + 3.0)
            self.assertEqual(resolved.scope, scope)
            self.assertEqual(resolved.credential_ref, fx["credential_ref"])
            loaded = source.load(scope, credential_ref=fx["credential_ref"],
                                 deadline=time.monotonic() + 3.0)
            self.assertEqual(set(loaded), {"grant", "catalog", "binding"})
            self.assertEqual(loaded["grant"], fx["grant"])
            loaded["catalog"].clear()
            loaded["binding"]["target_profile"] = "client_mutation"
            again = source.load(scope, credential_ref=fx["credential_ref"],
                                deadline=time.monotonic() + 3.0)
            self.assertTrue(again["catalog"])
            self.assertEqual(again["binding"], scope.binding)
            self.assertEqual(len(fx["producer"]["native_calls"]), 1)

    def test_genuine_positive_rejects_every_scope_and_attribute_mismatch(self):
        with genuine_source_fixture() as fx:
            scope, source = fx["scope"], fx["source"]
            original = scope.as_dict()

            def different(field, value):
                if field == "pairing_generation":
                    return value + 1
                if field in {"owner_installation_id", "target_install_id"}:
                    return "install:" + ("f" if value != "install:" + "f" * 32 else "e") * 32
                if field in {"fingerprint", "catalog_digest", "policy_digest"}:
                    return ("f" if value != "f" * 64 else "e") * 64
                return "other" if value != "other" else "different"

            for field, old in original.items():
                with self.subTest(field=field):
                    changed = replace(scope, **{field: different(field, old)})
                    with self.assertRaises(TargetMaterialSourceError):
                        source.load(changed, credential_ref=fx["credential_ref"],
                                    deadline=time.monotonic() + 3.0)
                    object.__setattr__(changed, "as_dict", lambda: dict(original))
                    with self.assertRaises(TargetMaterialSourceError):
                        source.load(changed, credential_ref=fx["credential_ref"],
                                    deadline=time.monotonic() + 3.0)

    def test_valid_producer_fault_matrix_reaches_material_and_consent_guards(self):
        with genuine_source_fixture() as fx:
            source, scope, store = fx["source"], fx["scope"], fx["fx"]["joining_store"]
            key, ref = fx["scope_key"], fx["credential_ref"]
            original_raw = store.mint_load(key)

            seen_deadlines = []

            def rejected(label, loader):
                with self.subTest(label=label):
                    def callback(key, *, deadline):
                        seen_deadlines.append(deadline)
                        return loader(key, deadline=deadline)
                    with mock.patch.object(store, "mint_load", side_effect=callback):
                        with self.assertRaises(TargetMaterialSourceError):
                            source.load(scope, credential_ref=ref, deadline=time.monotonic() + 3.0)

            def altered(mutator):
                envelope = json.loads(original_raw.decode("utf-8"))
                mutator(envelope)
                return json.dumps(envelope, separators=(",", ":")).encode("utf-8")

            rejected("v1", lambda _key, *, deadline: altered(lambda value: value.__setitem__("v", 1)))
            rejected("wrong-operation-endpoint", lambda _key, *, deadline: altered(
                lambda value: value["operation"].__setitem__("endpoint_id", "endpoint-other")))
            rejected("wrong-operation-generation", lambda _key, *, deadline: altered(
                lambda value: value["operation"].__setitem__("generation", scope.pairing_generation + 1)))
            rejected("wrong-grant-claim", lambda _key, *, deadline: altered(
                lambda value: value.__setitem__("grant", "not.a.grant")))
            rejected("catalog-drift", lambda _key, *, deadline: altered(
                lambda value: value["catalog"].__setitem__("installation_id", "install:" + "f" * 32)))
            self.assertEqual(len(seen_deadlines), 5)
            self.assertTrue(all(type(value) is float for value in seen_deadlines))

            import_id = store.find_import(scope.lobby_id, scope.invite_id)["import_id"]
            consent = store.get_consent(import_id)
            with mock.patch.object(store, "get_consent", return_value=None):
                with self.assertRaises(TargetMaterialSourceError):
                    source.resolve(lobby_id=scope.lobby_id, invite_id=scope.invite_id,
                                   deadline=time.monotonic() + 3.0)
            bad_consent = copy.deepcopy(consent)
            bad_consent["signature"] = "A" * 88
            with mock.patch.object(store, "get_consent", return_value=bad_consent):
                with self.assertRaises(TargetMaterialSourceError):
                    source.resolve(lobby_id=scope.lobby_id, invite_id=scope.invite_id,
                                   deadline=time.monotonic() + 3.0)

    def test_local_leave_during_read_denies_resolve_and_load(self):
        with genuine_source_fixture() as fx:
            scope, source = fx["scope"], fx["source"]
            self.assertEqual(source.resolve(lobby_id=scope.lobby_id, invite_id=scope.invite_id,
                                             deadline=time.monotonic() + 3.0).scope, scope)
            self.assertEqual(source.load(scope, credential_ref=fx["credential_ref"],
                                         deadline=time.monotonic() + 3.0)["grant"], fx["grant"])

        with genuine_source_fixture() as fx:
            scope, source, store = fx["scope"], fx["source"], fx["fx"]["joining_store"]
            self.assertEqual(source.load(scope, credential_ref=fx["credential_ref"],
                                         deadline=time.monotonic() + 3.0)["grant"], fx["grant"])
            record = store.find_import(scope.lobby_id, scope.invite_id)
            original = source._catalog_reader

            def leave_after_catalog(*, deadline):
                value = original(deadline=deadline)
                store.mark_left(record["import_id"])
                return value

            source._catalog_reader = leave_after_catalog
            with self.assertRaises(TargetMaterialSourceError):
                source.resolve(lobby_id=scope.lobby_id, invite_id=scope.invite_id,
                               deadline=time.monotonic() + 3.0)

        with genuine_source_fixture() as fx:
            scope, source, store = fx["scope"], fx["source"], fx["fx"]["joining_store"]
            self.assertEqual(source.load(scope, credential_ref=fx["credential_ref"],
                                         deadline=time.monotonic() + 3.0)["grant"], fx["grant"])
            record = store.find_import(scope.lobby_id, scope.invite_id)
            original = source._catalog_reader

            def leave_before_material(*, deadline):
                value = original(deadline=deadline)
                store.mark_left(record["import_id"])
                return value

            source._catalog_reader = leave_before_material
            with self.assertRaises(TargetMaterialSourceError):
                source.load(scope, credential_ref=fx["credential_ref"],
                            deadline=time.monotonic() + 3.0)

    def test_load_denies_after_detach_deadline(self):
        with genuine_source_fixture() as fx:
            scope, source = fx["scope"], fx["source"]
            self.assertEqual(source.load(scope, credential_ref=fx["credential_ref"],
                                         deadline=time.monotonic() + 3.0)["grant"], fx["grant"])
            original = __import__("invite_rooms.target_material", fromlist=["_detached"])._detached
            limit = time.monotonic() + 0.2

            def delayed_detach(value):
                time.sleep(max(0.0, limit - time.monotonic()) + 0.02)
                return original(value)

            with mock.patch("invite_rooms.target_material._detached", side_effect=delayed_detach):
                with self.assertRaises(TargetMaterialSourceError):
                    source.load(scope, credential_ref=fx["credential_ref"], deadline=limit)

    def _raw(self, fx, mutate, *, resign=False):
        """Return one altered encrypted-row plaintext without touching production."""
        store, key = fx["fx"]["joining_store"], fx["scope_key"]
        envelope = json.loads(store.mint_load(key).decode("utf-8"))
        mutate(envelope)
        if resign:
            unsigned = dict(envelope)
            unsigned.pop("signature")
            from invite_rooms.consent import canonical
            envelope["signature"] = base64.b64encode(fx["flow"].b_bundle.consent_key().sign(
                canonical(dict(unsigned, purpose="joining-owner-material-v2")))).decode()
        from invite_rooms.consent import canonical
        return canonical(envelope)

    def test_reopen_uses_same_ready_material_and_one_native_mint(self):
        with genuine_source_fixture() as fx:
            store, source, scope = fx["fx"]["joining_store"], fx["source"], fx["scope"]
            original = store.mint_load(fx["scope_key"])
            baseline_rows = self._rows(store)
            root = store.root
            authority = fx["flow"].producer["authority_b"]
            identity = source._identity_reader
            catalog = source._catalog_reader
            store.close()
            from invite_rooms.joining_store import JoiningStore
            reopened = JoiningStore(root)
            try:
                fresh = TargetMaterialSource(
                    joining_store=reopened, authority=authority, identity_reader=identity,
                    catalog_reader=catalog, target_http=TargetHttpConfig("http://127.0.0.1:19001", "default"),
                    endpoint_id=scope.endpoint_id, generation=scope.pairing_generation)
                self.assertEqual(fresh.resolve(lobby_id=scope.lobby_id, invite_id=scope.invite_id,
                                                deadline=time.monotonic() + 3).credential_ref, fx["credential_ref"])
                resolved = fresh.resolve(lobby_id=scope.lobby_id, invite_id=scope.invite_id,
                                         deadline=time.monotonic() + 3)
                self.assertEqual(resolved.scope, scope)
                loaded = fresh.load(scope, credential_ref=fx["credential_ref"], deadline=time.monotonic() + 3)
                self.assertEqual(set(loaded), {"grant", "catalog", "binding"})
                self.assertEqual(loaded["grant"], fx["grant"])
                self.assertEqual(reopened.mint_load(fx["scope_key"]), original)
                self.assertEqual(self._rows(reopened), baseline_rows)
                self.assertEqual(len(fx["producer"]["native_calls"]), 1)
            finally:
                reopened.close()

    def test_expired_material_operation_does_not_expire_valid_grant(self):
        with genuine_source_fixture() as fx:
            self._positive(fx)
            controls = self._controls(fx)
            raw = self._raw(fx, lambda e: e["operation"].__setitem__("expires_at_utc_ms", 1), resign=True)
            store = fx["fx"]["joining_store"]
            with mock.patch.object(store, "mint_load", return_value=raw):
                self.assertEqual(fx["source"].load(fx["scope"], credential_ref=fx["credential_ref"],
                                                     deadline=time.monotonic() + 3)["grant"], fx["grant"])
            self._assert_controls(fx, controls)
            self._positive(fx)

    def test_expired_signed_native_grant_is_denied(self):
        with genuine_source_fixture() as fx:
            self._positive(fx)
            controls = self._controls(fx)
            from gateway.hosted_room_peer import decode_room_grant, issue_room_grant
            claims = decode_room_grant(fx["native_secret"], fx["grant"], permission="dispatch")
            self.assertIsNotNone(claims)
            expired = issue_room_grant(fx["native_secret"],
                grant_id=claims["grant_id"], room_id=claims["room_id"],
                home_install_id=claims["home_install_id"], authority_gateway_id=claims["authority_gateway_id"],
                authority_epoch=claims["authority_epoch"], member_id=claims["member_id"],
                target_install_id=claims["target_install_id"], target_profile=claims["target_profile"],
                execution_policy_digest=claims["execution_policy_digest"], issued_at=time.time() - 10,
                ttl_seconds=1)
            with self.assertRaises(Exception):
                decode_room_grant(fx["native_secret"], expired, permission="dispatch")
            raw = self._raw(fx, lambda e: e.__setitem__("grant", expired), resign=True)
            with mock.patch.object(fx["fx"]["joining_store"], "mint_load", return_value=raw):
                with self.assertRaises(TargetMaterialSourceError):
                    fx["source"].load(fx["scope"], credential_ref=fx["credential_ref"], deadline=time.monotonic() + 3)
            self._assert_controls(fx, controls)
            self._positive(fx)

    def test_unpair_and_actual_repair_reject_old_material(self):
        with genuine_source_fixture() as fx:
            flow = fx["flow"]
            peer_id = flow.b.list_peers()[0]["peer_id"]
            old = fx["source"].resolve(lobby_id=fx["scope"].lobby_id, invite_id=fx["scope"].invite_id,
                                         deadline=time.monotonic() + 3)
            self._positive(fx)
            controls = self._controls(fx)
            old_raw = fx["fx"]["joining_store"].mint_load(fx["scope_key"])
            flow.b.unpair(peer_id)
            self.assertEqual(fx["flow"].producer["authority_b"].current(fx["scope"].endpoint_id).protocol_state, "revoked")
            import pairing_support as ps
            pair = ps.drive_pairing(flow.a, flow.b)
            ack = flow.a.ack_request(pair["a_id"])
            flow.a.complete_ack(pair["a_id"], flow.b.handle_pair_ack(ack.request))
            repaired = flow.b.journal.snapshot().active_links()[0]
            self.assertEqual(repaired["protocol_state"], "acknowledged")
            self.assertNotEqual(repaired["endpoint_id"], fx["scope"].endpoint_id)
            self.assertGreater(repaired["generation"], fx["scope"].pairing_generation)
            fresh = TargetMaterialSource(
                joining_store=fx["fx"]["joining_store"], authority=fx["source"]._authority,
                identity_reader=fx["source"]._identity_reader, catalog_reader=fx["source"]._catalog_reader,
                target_http=fx["source"]._target_http, endpoint_id=repaired["endpoint_id"],
                generation=repaired["generation"])
            self._assert_controls(fx, controls)
            old_scope = old.scope
            with self.assertRaises(TargetMaterialSourceError):
                fx["source"].load(old_scope, credential_ref=old.credential_ref, deadline=time.monotonic() + 3)
            with self.assertRaises(TargetMaterialSourceError):
                fresh.resolve(lobby_id=old_scope.lobby_id, invite_id=old_scope.invite_id,
                              deadline=time.monotonic() + 3)
            self._assert_controls(fx, controls)
            self.assertEqual(fx["fx"]["joining_store"].mint_load(fx["scope_key"]), old_raw)
            self.assertEqual(len(fx["producer"]["native_calls"]), 1)

    def test_signed_operation_identity_and_types_are_bound(self):
        with genuine_source_fixture() as fx:
            store, scope, source = fx["fx"]["joining_store"], fx["scope"], fx["source"]
            self._positive(fx)
            controls = self._controls(fx)
            rows = [("endpoint", lambda e: e["operation"].__setitem__("endpoint_id", "endpoint-other")),
                    ("generation", lambda e: e["operation"].__setitem__("generation", scope.pairing_generation + 1)),
                    ("lobby", lambda e: e["operation"].__setitem__("lobby_id", "lobby-other")),
                    ("invite", lambda e: e["operation"].__setitem__("invite_id", "invite-other")),
                    ("float-version", lambda e: e.__setitem__("v", 2.0)),
                    ("string-version", lambda e: e.__setitem__("v", "2")),
                    ("bool-version", lambda e: e.__setitem__("v", True)),
                    ("missing-envelope-field", lambda e: e.pop("catalog")),
                    ("extra-envelope-field", lambda e: e.__setitem__("extra", 1)),
                    ("bool-generation", lambda e: e["operation"].__setitem__("generation", True)),
                    ("string-request", lambda e: e["operation"].__setitem__("request_id", 3))]
            for label, mutate in rows:
                with self.subTest(label=label):
                    raw = self._raw(fx, mutate, resign=True)
                    with mock.patch.object(store, "mint_load", return_value=raw):
                        with self.assertRaises(TargetMaterialSourceError):
                            source.load(scope, credential_ref=fx["credential_ref"], deadline=time.monotonic() + 3)
                    self._assert_controls(fx, controls)
                    self._positive(fx)
            original = store.mint_load(fx["scope_key"])
            self.assertTrue(original.startswith(b'{'))
            duplicate = b'{"v":2,' + original[1:]
            self.assertNotEqual(duplicate, original)
            pairs = json.loads(duplicate, object_pairs_hook=lambda items: items)
            self.assertEqual(sum(key == 'v' for key, value in pairs), 2)
            with mock.patch.object(store, "mint_load", return_value=duplicate):
                with self.assertRaises(TargetMaterialSourceError):
                    source.load(scope, credential_ref=fx["credential_ref"], deadline=time.monotonic() + 3)
            self._assert_controls(fx, controls)
            self._positive(fx)

    def test_envelope_signature_and_grant_reference_are_bound(self):
        with genuine_source_fixture() as fx:
            store, source, scope = fx["fx"]["joining_store"], fx["source"], fx["scope"]
            self._positive(fx)
            controls = self._controls(fx)
            badsig = self._raw(fx, lambda e: e.__setitem__("signature", "A" * 88))
            from gateway.hosted_room_peer import decode_room_grant, issue_room_grant
            claims = decode_room_grant(fx["native_secret"], fx["grant"], permission="dispatch")
            other = issue_room_grant(
                fx["native_secret"], **{key: claims[key] for key in (
                    "room_id", "home_install_id", "authority_gateway_id", "authority_epoch",
                    "member_id", "target_install_id", "target_profile", "execution_policy_digest")},
                grant_id="grant-" + "d" * 32, issued_at=claims["issued_at"],
                ttl_seconds=claims["expires_at"] - claims["issued_at"],
                status_expires_at=claims["status_expires_at"])
            other_raw = self._raw(fx, lambda e: e.__setitem__("grant", other), resign=True)
            self.assertNotEqual(decode_room_grant(fx["native_secret"], other, permission="dispatch")["grant_id"],
                                fx["credential_ref"])
            original_claims = decode_room_grant(fx["native_secret"], fx["grant"], permission="dispatch")
            different_claims = decode_room_grant(fx["native_secret"], other, permission="dispatch")
            self.assertNotEqual(original_claims["grant_id"], different_claims["grant_id"])
            self.assertEqual(
                {key: value for key, value in original_claims.items() if key != "grant_id"},
                {key: value for key, value in different_claims.items() if key != "grant_id"},
            )
            for label, raw in (("signature", badsig), ("grant-reference", other_raw)):
                with self.subTest(label=label), mock.patch.object(store, "mint_load", return_value=raw):
                    with self.assertRaises(TargetMaterialSourceError):
                        source.load(scope, credential_ref=fx["credential_ref"], deadline=time.monotonic() + 3)
                    self._assert_controls(fx, controls)
                self._positive(fx)

    def test_ready_absent_intent_and_decrypt_failure_are_read_only(self):
        with genuine_source_fixture() as fx:
            store, source, scope = fx["fx"]["joining_store"], fx["source"], fx["scope"]
            baseline = self._rows(store)
            original_open = store._open
            for state in ("absent", "intent", "corrupt"):
                with self.subTest(state=state):
                    controls = self._controls(fx)
                    with store._write() as connection:
                        if state == "absent":
                            connection.execute("DELETE FROM material WHERE scope_key=?", (fx["scope_key"],))
                        elif state == "intent":
                            connection.execute("UPDATE material SET state='intent' WHERE scope_key=?", (fx["scope_key"],))
                        else:
                            row = baseline[0]
                            ciphertext = row[-1]
                            connection.execute("UPDATE material SET ciphertext=? WHERE scope_key=?",
                                               (bytes([ciphertext[0] ^ 1]) + ciphertext[1:], fx["scope_key"]))
                    faulty = self._rows(store)
                    entered = []
                    def observed_open(*args):
                        entered.append(True)
                        return original_open(*args)
                    with mock.patch.object(store, "_open", side_effect=observed_open):
                        self._deny(lambda: source.load(scope, credential_ref=fx["credential_ref"],
                                                       deadline=time.monotonic() + 3))
                    self.assertEqual(self._rows(store), faulty)
                    self.assertEqual(bool(entered), state == "corrupt")
                    self.assertEqual(len(fx["producer"]["native_calls"]), controls[1])
                    with store._write() as connection:
                        connection.execute("DELETE FROM material WHERE scope_key=?", (fx["scope_key"],))
                        connection.execute("INSERT INTO material VALUES(?,?,?,?,?)", baseline[0])
                    self._positive(fx)
                    self._assert_controls(fx, controls)
            self.assertEqual(len(fx["producer"]["native_calls"]), 1)

    def test_identity_catalog_and_profile_drift_refuses(self):
        with genuine_source_fixture() as fx:
            source, scope = fx["source"], fx["scope"]
            original_identity, original_catalog = source._identity_reader, source._catalog_reader
            for label in ("identity", "catalog", "profile"):
                with self.subTest(label=label):
                    controls = self._controls(fx)
                    if label == "identity":
                        from invite_rooms.identity import initialize_identity, load_identity
                        path = fx["flow"].tmp / "independent-identity"
                        initialize_identity(path, installation_id="install:" + "f" * 32, profile="default")
                        identity = load_identity(path, installation_id="install:" + "f" * 32, profile="default")
                        source._identity_reader = lambda **kw: identity
                    elif label == "catalog":
                        import pairing_support as ps
                        from gateway.hosted_room_peer import GatewayRoomCatalog
                        with mock.patch.object(ps, "_POLICY_UNSIGNED",
                                                dict(ps._POLICY_UNSIGNED, max_iterations=11)):
                            changed = ps.make_catalog(scope.target_install_id)
                        GatewayRoomCatalog.from_mapping(changed)
                        source._catalog_reader = lambda **kw: copy.deepcopy(changed)
                    else:
                        source._target_http = TargetHttpConfig("http://127.0.0.1:19001", "other")
                    self._deny(lambda: source.resolve(lobby_id=scope.lobby_id,
                                                       invite_id=scope.invite_id, deadline=time.monotonic() + 3))
                    self._assert_controls(fx, controls)
                    source._identity_reader, source._catalog_reader = original_identity, original_catalog
                    source._target_http = TargetHttpConfig("http://127.0.0.1:19001", "default")
                    self._positive(fx)
            self.assertEqual(len(fx["producer"]["native_calls"]), 1)

    def test_concrete_authority_store_and_scope_reject_spoofs_without_calls(self):
        with genuine_source_fixture() as fx:
            source, scope = fx["source"], fx["scope"]
            self.assertEqual(source.resolve(lobby_id=scope.lobby_id, invite_id=scope.invite_id, deadline=time.monotonic() + 3).scope, scope)
            from invite_rooms.joining_store import JoiningStore
            from invite_rooms.pairing_journal import JournalLinkAuthority
            from invite_rooms.peer_registry import MaterialScope
            calls = []
            class Duck:
                def __getattr__(self, name):
                    calls.append(name)
                    raise AssertionError("spoof invoked")
            class Instrumented:
                marker = "owned"
                def __init__(self):
                    self.accesses = []
                def __getattribute__(self, name):
                    if name not in {"accesses", "__dict__", "__class__", "__getattribute__"}:
                        object.__getattribute__(self, "accesses").append(name)
                    return object.__getattribute__(self, name)
            def impostor(cls):
                return type("ClassSpoof", (Duck,), {"__class__": property(lambda self: cls)})()
            store, authority = fx["fx"]["joining_store"], fx["source"]._authority
            for field, cls in (("joining_store", JoiningStore), ("authority", JournalLinkAuthority)):
                subclass = type("ConcreteSubclass", (cls,), {})
                for bad in (Duck(), impostor(cls), object.__new__(subclass)):
                    kwargs = dict(joining_store=store, authority=authority,
                                  identity_reader=fx["source"]._identity_reader,
                                  catalog_reader=fx["source"]._catalog_reader,
                                  target_http=fx["source"]._target_http,
                                  endpoint_id=scope.endpoint_id, generation=scope.pairing_generation)
                    kwargs[field] = bad
                    self._deny(lambda kwargs=kwargs: TargetMaterialSource(**kwargs))
                    self.assertFalse(calls)
                instrumented = type("InstrumentedSubclass", (cls, Instrumented), {})
                owned = object.__new__(instrumented)
                object.__setattr__(owned, "accesses", [])
                self.assertEqual(owned.marker, "owned")
                self.assertEqual(owned.accesses, ["marker"])
                owned.accesses.clear()
                kwargs = dict(joining_store=store, authority=authority,
                              identity_reader=fx["source"]._identity_reader,
                              catalog_reader=fx["source"]._catalog_reader,
                              target_http=fx["source"]._target_http,
                              endpoint_id=scope.endpoint_id, generation=scope.pairing_generation)
                kwargs[field] = owned
                self._deny(lambda kwargs=kwargs: TargetMaterialSource(**kwargs))
                self.assertEqual(owned.accesses, [])
            subclass = type("ScopeSubclass", (MaterialScope,), {})
            for bad in (Duck(), impostor(MaterialScope), object.__new__(subclass)):
                self._deny(lambda bad=bad: source.load(bad, credential_ref=fx["credential_ref"],
                                                       deadline=time.monotonic() + 3))
                self.assertFalse(calls)
            instrumented_scope = type("InstrumentedScopeSubclass", (MaterialScope, Instrumented), {})
            owned_scope = object.__new__(instrumented_scope)
            object.__setattr__(owned_scope, "accesses", [])
            self.assertEqual(owned_scope.marker, "owned")
            self.assertEqual(owned_scope.accesses, ["marker"])
            owned_scope.accesses.clear()
            self._deny(lambda: source.load(owned_scope, credential_ref=fx["credential_ref"],
                                           deadline=time.monotonic() + 3))
            self.assertEqual(owned_scope.accesses, [])
            self._positive(fx)

    def test_journal_busy_and_corrupt_are_fail_closed(self):
        with genuine_source_fixture() as fx:
            source, scope = fx["source"], fx["scope"]
            self._positive(fx)
            controls = self._controls(fx)
            from invite_rooms.pairing_journal import JOURNAL_FILE, LOCK_FILE, JournalError
            authority = source._authority
            holder_code = ("import json,sys,time\n"
                            "from pathlib import Path\n"
                            "p=Path(json.loads(sys.stdin.readline())[0]); f=open(p,'a+')\n"
                            "import fcntl; fcntl.flock(f, fcntl.LOCK_EX); print('held',flush=True)\n"
                            "sys.stdin.readline(); fcntl.flock(f, fcntl.LOCK_UN); f.close()")
            child = subprocess.Popen([sys.executable, "-B", "-W", "error::ResourceWarning", "-c", holder_code],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     text=True, env=dict(os.environ))
            try:
                child.stdin.write(json.dumps([str(authority.journal.directory / LOCK_FILE)]) + "\n")
                child.stdin.flush()
                self.assertTrue(select.select([child.stdout], [], [], 5)[0])
                self.assertEqual(child.stdout.readline().strip(), "held")
                with self.assertRaises(JournalError) as caught:
                    authority.peer_trust_strict(scope.endpoint_id, scope.pairing_generation,
                                                role="joining", states=("acknowledged",))
                self.assertEqual(caught.exception.code, "journal_busy")
                self._deny(lambda: source.resolve(lobby_id=scope.lobby_id, invite_id=scope.invite_id,
                                                   deadline=time.monotonic() + .3))
            finally:
                child.stdin.write("release\n"); child.stdin.flush(); child.wait(timeout=5)
                child.stdin.close(); child.stdout.close(); child.stderr.close()
                self.assertEqual(child.returncode, 0)
            self._assert_controls(fx, controls)
            self._positive(fx)
            path = authority.journal.directory / JOURNAL_FILE
            original = path.read_bytes()
            try:
                path.write_bytes(original + b"not-json\n")
                corrupt = path.read_bytes()
                with self.assertRaises(JournalError) as caught:
                    authority.peer_trust_strict(scope.endpoint_id, scope.pairing_generation,
                                                role="joining", states=("acknowledged",))
                self.assertEqual(caught.exception.code, "journal_corrupt")
                self._deny(lambda: source.resolve(lobby_id=scope.lobby_id, invite_id=scope.invite_id,
                                                   deadline=time.monotonic() + .3))
                self.assertEqual(path.read_bytes(), corrupt)
                self._assert_controls(fx, controls)
            finally:
                path.write_bytes(original)
            self._positive(fx)

    def test_store_contention_obeys_inherited_deadline(self):
        with genuine_source_fixture() as fx:
            self._positive(fx)
            controls = self._controls(fx)
            holder_code = ("import json,sys,sqlite3\n"
                           "path=json.loads(sys.stdin.readline())[0]; c=sqlite3.connect(path,timeout=0)\n"
                           "c.execute('BEGIN IMMEDIATE'); print('held',flush=True); sys.stdin.readline(); c.rollback(); c.close()")
            child = subprocess.Popen([sys.executable, "-B", "-W", "error::ResourceWarning", "-c", holder_code],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     text=True, env=dict(os.environ))
            try:
                child.stdin.write(json.dumps([str(fx["fx"]["joining_store"].path)]) + "\n"); child.stdin.flush()
                self.assertTrue(select.select([child.stdout], [], [], 5)[0])
                self.assertEqual(child.stdout.readline().strip(), "held")
                deadline = time.monotonic() + .08
                seen_deadlines = []
                original_find = fx["fx"]["joining_store"].find_import
                def observed_find(*args, **kwargs):
                    seen_deadlines.append(kwargs["deadline"])
                    return original_find(*args, **kwargs)
                with mock.patch.object(fx["fx"]["joining_store"], "find_import", side_effect=observed_find):
                    self._deny(lambda: fx["source"].resolve(lobby_id=fx["scope"].lobby_id,
                                                             invite_id=fx["scope"].invite_id, deadline=deadline))
                self.assertEqual(seen_deadlines, [deadline])
                self.assertLess(time.monotonic(), deadline + .25)
            finally:
                child.stdin.write("release\n"); child.stdin.flush(); child.wait(timeout=5)
                child.stdin.close(); child.stdout.close(); child.stderr.close()
                self.assertEqual(child.returncode, 0)
            self._assert_controls(fx, controls)
            self._positive(fx)

    def test_midread_unpair_and_delayed_reader_return_no_credential(self):
        with genuine_source_fixture() as fx:
            source, scope, flow = fx["source"], fx["scope"], fx["flow"]
            self._positive(fx)
            for kind in ("identity", "catalog"):
                controls = self._controls(fx)
                original = getattr(source, "_" + kind + "_reader")
                deadline = time.monotonic() + .3
                seen = []
                def delayed(*, deadline, original=original, kind=kind):
                    seen.append(deadline)
                    value = original(deadline=deadline)
                    time.sleep(max(0.0, deadline - time.monotonic()) + .02)
                    return value
                setattr(source, "_" + kind + "_reader", delayed)
                try:
                    self._deny(lambda: source.load(scope, credential_ref=fx["credential_ref"], deadline=deadline))
                    self.assertEqual(seen, [deadline])
                finally:
                    setattr(source, "_" + kind + "_reader", original)
                self._assert_controls(fx, controls)
                self._positive(fx)
            original = source._catalog_reader
            controls = self._controls(fx)
            def unpair_after_read(*, deadline):
                value = original(deadline=deadline)
                flow.b.unpair(flow.b.list_peers()[0]["peer_id"])
                return value
            source._catalog_reader = unpair_after_read
            try:
                self._deny(lambda: source.load(scope, credential_ref=fx["credential_ref"], deadline=time.monotonic() + 3))
            finally:
                source._catalog_reader = original
            self.assertEqual(source._authority.current(scope.endpoint_id).protocol_state, "revoked")
            self._assert_controls(fx, controls)


if __name__ == "__main__":
    unittest.main()