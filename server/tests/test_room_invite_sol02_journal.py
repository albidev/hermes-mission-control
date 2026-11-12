"""SOL-02 regression: durable journal unpair cutover beats a stale registry projection."""
from __future__ import annotations

import base64
import asyncio
import json
import threading
import time
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from invite_rooms import material_receipts as mr
from invite_rooms import peer_registry as pr
from invite_rooms import runtime_config, runtime_joining as rj
from invite_rooms.pairing_journal import _verify

import test_room_invite_joining_routes as J
import test_room_invite_material_receipts as M


class JournalCutoverReceiptAdmissionTests(unittest.TestCase):
    def _loaded_host(self, fixture, room_id):
        fixture.cfg.update({
            "store_path": str(fixture.A.root / "invites" / "host.sqlite"),
            "installation_id": J.ps.INSTALL_A,
            "peer_registry_path": str(fixture.A.peers_path),
            "pairing": {"identity_dir": str(fixture.A.identity_dir),
                         "journal_dir": str(fixture.A.journal_dir),
                         "peers_path": str(fixture.A.peers_path)},
        })
        fixture.save_config()
        loaded = runtime_config.load_service()
        self.assertIsNotNone(loaded)
        service, _ctx, _config = loaded
        reg = service.registry
        peer = "peer-" + "b" * 32
        binding = reg.binding_for(peer)
        scope = pr.MaterialScope(owner_installation_id=J.ps.INSTALL_A, owner_profile="default",
            lobby_id=J.LOBBY, planned_room_id=room_id, member_id="joining-b",
            invite_id=J.INVITE, peer_id=peer, pairing_generation=1, **binding)
        bundle = rj.identity_module.load_identity(fixture.B.identity_dir,
                                                   installation_id=J.ps.INSTALL_B, profile="default")
        key = rj._IdentityConsentKey(bundle)
        wire_scope = {"host_id": J.ps.INSTALL_A, "room_id": scope.planned_room_id,
                      "lobby_id": scope.lobby_id, "member_id": scope.member_id,
                      "invite_id": scope.invite_id, "binding": binding}
        signed = {"scope": wire_scope, "grant": M.GRANT_A, "catalog": reg._entry(peer).catalog,
                  "purpose": "joining-owner-material-v1"}
        envelope = {k: v for k, v in signed.items() if k != "purpose"}
        envelope["signature"] = key.sign(signed)
        return service, reg, peer, binding, scope, envelope, signed

    def _replace_projection(self, fixture, mutate):
        entries = json.loads(fixture.A.peers_path.read_text())
        mutate(entries[0])
        pr.RegistrySource(fixture.A.peers_path, runtime_config.build_peer_registry).replace(entries)

    def test_new_receipt_after_unpair_fsync_is_refused_before_projection(self):
        J.setUpModule()
        fixture = J.JoiningHttpTest("runTest")
        try:
            fixture.setUp()
            loaded = runtime_config.load_service()
            if loaded is None:
                self.fail("configured production load_service must construct pairing authority")
            reg = runtime_config.build_peer_registry(fixture.A.peers_path)
            reg._source = pr.RegistrySource(fixture.A.peers_path, runtime_config.build_peer_registry)
            reg._authority = fixture.a.authority
            store = mr.MaterialReceiptStore(fixture.tmp / "receipt-probe")
            try:
                reg._receipt_store = store
                peer = "peer-" + "b" * 32
                binding = reg.binding_for(peer)
                scope = pr.MaterialScope(owner_installation_id=J.ps.INSTALL_A, owner_profile="default",
                    lobby_id=J.LOBBY, planned_room_id="invite-room-probe", member_id="joining-b",
                    invite_id=J.INVITE, peer_id=peer, pairing_generation=1, **binding)
                bundle = rj.identity_module.load_identity(fixture.B.identity_dir,
                                                          installation_id=J.ps.INSTALL_B, profile="default")
                key = rj._IdentityConsentKey(bundle)
                wire_scope = {"host_id": J.ps.INSTALL_A, "room_id": scope.planned_room_id,
                              "lobby_id": scope.lobby_id, "member_id": scope.member_id,
                              "invite_id": scope.invite_id, "binding": binding}
                catalog = reg._entry(peer).catalog
                signed = {"scope": wire_scope, "grant": M.GRANT_A, "catalog": catalog,
                          "purpose": "joining-owner-material-v1"}
                envelope = {k: v for k, v in signed.items() if k != "purpose"}
                envelope["signature"] = key.sign(signed)
                writer_errors = []
                projection_entered = threading.Event()

                def unpair():
                    try:
                        fixture.a.unpair(peer)
                    except Exception as exc:
                        writer_errors.append(str(exc))

                writer = threading.Thread(target=unpair)
                real_replace = fixture.a.projector._source.replace

                def observe_projection(entries):
                    projection_entered.set()
                    return real_replace(entries)

                with reg._source.locked_current(), patch.object(fixture.a.projector._source, "replace", observe_projection):
                    writer.start()
                    self.assertTrue(projection_entered.wait(1), "post-fsync projection was not reached")
                    deadline = time.monotonic() + 1
                    cutover = False
                    while time.monotonic() < deadline:
                        state = _verify((fixture.A.journal_dir / "pairing.journal").read_bytes())
                        if state.links[fixture.ep]["protocol_state"] == "revoked":
                            cutover = True
                            break
                        time.sleep(0.002)
                    self.assertTrue(cutover, "journal cutover did not occur")
                    with self.assertRaises(pr.PeerRegistryError) as caught:
                        reg.admit_signed_material(scope, envelope)
                    self.assertEqual(str(caught.exception), "peer_binding_changed")

                writer.join(3)
                self.assertFalse(store.root.joinpath(store.receipt_name(scope)).exists())
                self.assertEqual(writer_errors, [])
                self.assertEqual(fixture.a.journal.snapshot().links[fixture.ep]["protocol_state"], "revoked")
            finally:
                store.close()
        finally:
            if fixture._cleanups:
                fixture.doCleanups()
            J.tearDownModule()

    def test_loaded_service_accepts_first_receipt_in_sibling_layout(self):
        J.setUpModule()
        fixture = J.JoiningHttpTest("runTest")
        try:
            fixture.setUp()
            # The host side has the acknowledged peer projection; place its store in the
            # documented sibling invites/ directory while retaining journal/ beside it.
            fixture.cfg.update({
                "store_path": str(fixture.A.root / "invites" / "host.sqlite"),
                "installation_id": J.ps.INSTALL_A,
                "peer_registry_path": str(fixture.A.peers_path),
                "pairing": {"identity_dir": str(fixture.A.identity_dir),
                             "journal_dir": str(fixture.A.journal_dir),
                             "peers_path": str(fixture.A.peers_path)},
            })
            fixture.save_config()
            loaded = runtime_config.load_service()
            self.assertIsNotNone(loaded)
            service, _ctx, config = loaded
            self.assertEqual(config.store_path.parent, fixture.A.root / "invites")
            self.assertEqual(config.pairing.journal_anchor, fixture.A.root)
            reg = service.registry
            peer = "peer-" + "b" * 32
            binding = reg.binding_for(peer)
            scope = pr.MaterialScope(owner_installation_id=J.ps.INSTALL_A, owner_profile="default",
                lobby_id=J.LOBBY, planned_room_id="invite-room-sibling", member_id="joining-b",
                invite_id=J.INVITE, peer_id=peer, pairing_generation=1, **binding)
            bundle = rj.identity_module.load_identity(fixture.B.identity_dir,
                                                       installation_id=J.ps.INSTALL_B, profile="default")
            key = rj._IdentityConsentKey(bundle)
            wire_scope = {"host_id": J.ps.INSTALL_A, "room_id": scope.planned_room_id,
                          "lobby_id": scope.lobby_id, "member_id": scope.member_id,
                          "invite_id": scope.invite_id, "binding": binding}
            catalog = reg._entry(peer).catalog
            signed = {"scope": wire_scope, "grant": M.GRANT_A, "catalog": catalog,
                      "purpose": "joining-owner-material-v1"}
            envelope = {k: v for k, v in signed.items() if k != "purpose"}
            envelope["signature"] = key.sign(signed)
            reg.admit_signed_material(scope, envelope)
            receipt_path = service.registry._receipt_store.root / service.registry._receipt_store.receipt_name(scope)
            first_bytes, first_inode = receipt_path.read_bytes(), receipt_path.stat().st_ino
            reg.admit_signed_material(scope, envelope)
            self.assertEqual(receipt_path.read_bytes(), first_bytes)
            self.assertEqual(receipt_path.stat().st_ino, first_inode)
            wrong = dict(envelope)
            wrong["signature"] = ("A" if wrong["signature"][0] != "A" else "B") + wrong["signature"][1:]
            with self.assertRaises(pr.PeerRegistryError) as caught:
                reg.admit_signed_material(scope, wrong)
            self.assertEqual(str(caught.exception), "invalid_consent")
        finally:
            if fixture._cleanups:
                fixture.doCleanups()
            J.tearDownModule()

    def test_journal_lock_obeys_propagated_admission_deadline(self):
        J.setUpModule()
        fixture = J.JoiningHttpTest("runTest")
        try:
            fixture.setUp()
            service, reg, _peer, _binding, scope, envelope, _signed = self._loaded_host(
                fixture, "invite-room-journal-deadline")
            entered, release = threading.Event(), threading.Event()

            def hold_journal():
                with fixture.a.journal.transaction():
                    entered.set()
                    release.wait(2)

            holder = threading.Thread(target=hold_journal)
            holder.start()
            self.assertTrue(entered.wait(1))
            receipt = service.registry._receipt_store.root / service.registry._receipt_store.receipt_name(scope)

            async def attempt():
                token = pr.ADMISSION_DEADLINE.set(time.monotonic() + 0.04)
                try:
                    await reg.admit_signed_material_async(scope, envelope)
                finally:
                    pr.ADMISSION_DEADLINE.reset(token)

            started = time.monotonic()
            with self.assertRaises(pr.PeerRegistryError) as caught:
                asyncio.run(attempt())
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 0.2, "journal admission waited past the request deadline")
            self.assertEqual(str(caught.exception), "admission_deadline_exceeded")
            self.assertFalse(receipt.exists())
            release.set()
            holder.join(2)
            self.assertFalse(holder.is_alive())

            # Positive control: the same valid envelope succeeds once contention is gone.
            asyncio.run(reg.admit_signed_material_async(scope, envelope))
            self.assertTrue(receipt.exists())
        finally:
            if fixture._cleanups:
                fixture.doCleanups()
            J.tearDownModule()

    def test_expired_admission_refuses_before_journal_submit(self):
        J.setUpModule()
        fixture = J.JoiningHttpTest("runTest")
        try:
            fixture.setUp()
            service, reg, _peer, _binding, scope, envelope, _signed = self._loaded_host(
                fixture, "invite-room-expired-before-submit")
            receipt = service.registry._receipt_store.root / service.registry._receipt_store.receipt_name(scope)

            async def attempt():
                token = pr.ADMISSION_DEADLINE.set(time.monotonic() - 0.001)
                try:
                    await reg.admit_signed_material_async(scope, envelope)
                finally:
                    pr.ADMISSION_DEADLINE.reset(token)

            with self.assertRaises(pr.PeerRegistryError) as caught:
                asyncio.run(attempt())
            self.assertEqual(str(caught.exception), "admission_deadline_exceeded")
            self.assertFalse(receipt.exists())
        finally:
            if fixture._cleanups:
                fixture.doCleanups()
            J.tearDownModule()

    def test_deadline_around_uncontended_journal_acquire_allows_valid_admission(self):
        J.setUpModule()
        fixture = J.JoiningHttpTest("runTest")
        try:
            fixture.setUp()
            service, reg, _peer, _binding, scope, envelope, _signed = self._loaded_host(
                fixture, "invite-room-deadline-positive")
            receipt = service.registry._receipt_store.root / service.registry._receipt_store.receipt_name(scope)

            async def attempt():
                token = pr.ADMISSION_DEADLINE.set(time.monotonic() + 1.0)
                try:
                    await reg.admit_signed_material_async(scope, envelope)
                finally:
                    pr.ADMISSION_DEADLINE.reset(token)

            asyncio.run(attempt())
            self.assertTrue(receipt.exists())
        finally:
            if fixture._cleanups:
                fixture.doCleanups()
            J.tearDownModule()

    def test_projection_consent_key_drift_refuses_with_stable_mismatch_code(self):
        J.setUpModule()
        fixture = J.JoiningHttpTest("runTest")
        try:
            fixture.setUp()
            service, reg, _peer, _binding, scope, envelope, signed = self._loaded_host(
                fixture, "invite-room-key-drift")
            unrelated = Ed25519PrivateKey.generate()
            forged = dict(envelope)
            forged["signature"] = base64.b64encode(unrelated.sign(M.canonical(signed))).decode("ascii")
            with self.assertRaises(pr.PeerRegistryError) as before:
                reg.admit_signed_material(scope, forged)
            self.assertEqual(str(before.exception), "invalid_consent")
            self._replace_projection(fixture, lambda entry: entry.update(
                consent_public_key_b64=base64.b64encode(unrelated.public_key().public_bytes_raw()).decode("ascii")))
            with self.assertRaises(pr.PeerRegistryError) as caught:
                reg.admit_signed_material(scope, forged)
            self.assertEqual(str(caught.exception), "peer_binding_changed")
            receipt = service.registry._receipt_store.root / service.registry._receipt_store.receipt_name(scope)
            self.assertFalse(receipt.exists())
        finally:
            if fixture._cleanups:
                fixture.doCleanups()
            J.tearDownModule()

    def test_projection_peer_alias_refuses_before_receipt_write(self):
        J.setUpModule()
        fixture = J.JoiningHttpTest("runTest")
        try:
            fixture.setUp()
            service, reg, _peer, binding, scope, envelope, _signed = self._loaded_host(
                fixture, "invite-room-peer-drift")
            alias = "peer-alias-" + "a" * 16
            self._replace_projection(fixture, lambda entry: entry.update(peer_id=alias))
            with self.assertRaises(pr.PeerRegistryError) as caught:
                reg.admit_signed_material(scope, envelope)
            self.assertEqual(str(caught.exception), "peer_binding_changed")
            receipt = service.registry._receipt_store.root / service.registry._receipt_store.receipt_name(scope)
            self.assertFalse(receipt.exists())
        finally:
            if fixture._cleanups:
                fixture.doCleanups()
            J.tearDownModule()

    def test_reloaded_projection_alias_refuses_with_journal_paired_key(self):
        """A fresh service must not admit a local alias with a valid journal signature."""
        J.setUpModule()
        fixture = J.JoiningHttpTest("runTest")
        try:
            fixture.setUp()
            alias = "peer-alias-" + "c" * 16
            self._replace_projection(fixture, lambda entry: entry.update(peer_id=alias))
            fixture.cfg.update({
                "store_path": str(fixture.A.root / "invites" / "host.sqlite"),
                "installation_id": J.ps.INSTALL_A,
                "peer_registry_path": str(fixture.A.peers_path),
                "pairing": {"identity_dir": str(fixture.A.identity_dir),
                             "journal_dir": str(fixture.A.journal_dir),
                             "peers_path": str(fixture.A.peers_path)},
            })
            fixture.save_config()
            loaded = runtime_config.load_service()
            self.assertIsNotNone(loaded)
            service, _ctx, _config = loaded
            reg = service.registry
            binding = reg.binding_for(alias)
            scope = pr.MaterialScope(owner_installation_id=J.ps.INSTALL_A, owner_profile="default",
                lobby_id=J.LOBBY, planned_room_id="invite-room-reloaded-alias", member_id="joining-c",
                invite_id=J.INVITE, peer_id=alias, pairing_generation=1, **binding)
            bundle = rj.identity_module.load_identity(fixture.B.identity_dir,
                                                       installation_id=J.ps.INSTALL_B, profile="default")
            key = rj._IdentityConsentKey(bundle)
            wire_scope = {"host_id": J.ps.INSTALL_A, "room_id": scope.planned_room_id,
                          "lobby_id": scope.lobby_id, "member_id": scope.member_id,
                          "invite_id": scope.invite_id, "binding": binding}
            signed = {"scope": wire_scope, "grant": M.GRANT_A, "catalog": reg._entry(alias).catalog,
                      "purpose": "joining-owner-material-v1"}
            envelope = {k: v for k, v in signed.items() if k != "purpose"}
            envelope["signature"] = key.sign(signed)
            with self.assertRaises(pr.PeerRegistryError) as caught:
                reg.admit_signed_material(scope, envelope)
            self.assertEqual(str(caught.exception), "peer_binding_changed")
            receipt = reg._receipt_store.root / reg._receipt_store.receipt_name(scope)
            self.assertFalse(receipt.exists())
        finally:
            if fixture._cleanups:
                fixture.doCleanups()
            J.tearDownModule()

    def test_fresh_projection_binding_drift_refuses_before_receipt_write(self):
        """Each projection-only binding mutation is refused on a fresh configured service."""
        mutations = (
            ("fingerprint", lambda entry: entry.update(fingerprint="f" * 64)),
            ("endpoint_id", lambda entry: entry.update(endpoint_id="endpoint-foreign")),
            ("pairing_generation", lambda entry: entry.update(pairing_generation=2)),
        )
        for index, (label, mutate) in enumerate(mutations):
            J.setUpModule()
            fixture = J.JoiningHttpTest("runTest")
            try:
                fixture.setUp()
                service, reg, _peer, _binding, scope, envelope, _signed = self._loaded_host(
                    fixture, "invite-room-binding-drift-" + label)
                # Use a distinct member and receipt scope for each independent mutation.
                scope = pr.MaterialScope(owner_installation_id=scope.owner_installation_id,
                    owner_profile=scope.owner_profile, lobby_id=scope.lobby_id,
                    planned_room_id=scope.planned_room_id, member_id="joining-drift-" + str(index),
                    invite_id=scope.invite_id, peer_id=scope.peer_id,
                    pairing_generation=scope.pairing_generation, **scope.binding)
                bundle = rj.identity_module.load_identity(fixture.B.identity_dir,
                                                           installation_id=J.ps.INSTALL_B, profile="default")
                key = rj._IdentityConsentKey(bundle)
                wire_scope = {"host_id": J.ps.INSTALL_A, "room_id": scope.planned_room_id,
                              "lobby_id": scope.lobby_id, "member_id": scope.member_id,
                              "invite_id": scope.invite_id, "binding": scope.binding}
                signed = {"scope": wire_scope, "grant": M.GRANT_A,
                          "catalog": reg._entry(scope.peer_id).catalog,
                          "purpose": "joining-owner-material-v1"}
                envelope = {k: v for k, v in signed.items() if k != "purpose"}
                envelope["signature"] = key.sign(signed)
                self._replace_projection(fixture, mutate)
                with self.assertRaises(pr.PeerRegistryError) as caught:
                    reg.admit_signed_material(scope, envelope)
                self.assertEqual(str(caught.exception), "peer_binding_changed")
                receipt = service.registry._receipt_store.root / service.registry._receipt_store.receipt_name(scope)
                self.assertFalse(receipt.exists())
            finally:
                if fixture._cleanups:
                    fixture.doCleanups()
                J.tearDownModule()

    def test_pairing_config_rejects_symlinked_journal_anchor(self):
        J.setUpModule()
        fixture = J.JoiningHttpTest("runTest")
        try:
            fixture.setUp()
            real = fixture.tmp / "real-owner"
            real.mkdir(mode=0o700)
            alias = fixture.tmp / "owner-alias"
            alias.symlink_to(real, target_is_directory=True)
            pairing = {"identity_dir": str(fixture.A.identity_dir),
                       "journal_dir": str(alias / "journal"),
                       "peers_path": str(fixture.A.peers_path)}
            self.assertIsNone(runtime_config._parse_pairing(pairing, fixture.A.peers_path))
            self.assertFalse((real / "journal").exists())
        finally:
            if fixture._cleanups:
                fixture.doCleanups()
            J.tearDownModule()

    def test_private_owner_root_below_shared_parent_preserves_valid_first_receipt(self):
        J.setUpModule()
        fixture = J.JoiningHttpTest("runTest")
        try:
            fixture.setUp()
            # Only the scratch parent is shared. The selected owner root and every private
            # descendant remain owner-only; validation must stop at that selected root.
            fixture.tmp.chmod(0o777)
            fixture.cfg.update({
                "store_path": str(fixture.B.root / "host.sqlite"),
                "installation_id": J.ps.INSTALL_A,
                "peer_registry_path": str(fixture.A.peers_path),
                "pairing": {"identity_dir": str(fixture.A.identity_dir),
                             "journal_dir": str(fixture.A.journal_dir),
                             "peers_path": str(fixture.A.peers_path)},
            })
            fixture.save_config()
            loaded = runtime_config.load_service()
            self.assertIsNotNone(loaded)
            service, _ctx, config = loaded
            self.assertEqual(config.pairing.journal_anchor, fixture.A.root)
            reg = service.registry
            peer = "peer-" + "b" * 32
            binding = reg.binding_for(peer)
            scope = pr.MaterialScope(owner_installation_id=J.ps.INSTALL_A, owner_profile="default",
                lobby_id=J.LOBBY, planned_room_id="invite-room-shared-parent", member_id="joining-b",
                invite_id=J.INVITE, peer_id=peer, pairing_generation=1, **binding)
            bundle = rj.identity_module.load_identity(fixture.B.identity_dir,
                                                       installation_id=J.ps.INSTALL_B, profile="default")
            key = rj._IdentityConsentKey(bundle)
            wire_scope = {"host_id": J.ps.INSTALL_A, "room_id": scope.planned_room_id,
                          "lobby_id": scope.lobby_id, "member_id": scope.member_id,
                          "invite_id": scope.invite_id, "binding": binding}
            signed = {"scope": wire_scope, "grant": M.GRANT_A, "catalog": reg._entry(peer).catalog,
                      "purpose": "joining-owner-material-v1"}
            envelope = {k: v for k, v in signed.items() if k != "purpose"}
            envelope["signature"] = key.sign(signed)
            reg.admit_signed_material(scope, envelope)
            receipt_path = service.registry._receipt_store.root / service.registry._receipt_store.receipt_name(scope)
            self.assertEqual(service.registry._receipt_store.load(scope)["grant"], M.GRANT_A)
            first_bytes, first_inode = receipt_path.read_bytes(), receipt_path.stat().st_ino
            reg.admit_signed_material(scope, envelope)
            self.assertEqual(receipt_path.read_bytes(), first_bytes)
            self.assertEqual(receipt_path.stat().st_ino, first_inode)
        finally:
            fixture.tmp.chmod(0o700)
            if fixture._cleanups:
                fixture.doCleanups()
            J.tearDownModule()

    def test_pairing_config_rejects_nonprivate_selected_journal_anchor(self):
        J.setUpModule()
        fixture = J.JoiningHttpTest("runTest")
        try:
            fixture.setUp()
            fixture.A.root.chmod(0o755)
            pairing = {"identity_dir": str(fixture.A.identity_dir),
                       "journal_dir": str(fixture.A.journal_dir),
                       "peers_path": str(fixture.A.peers_path)}
            self.assertIsNone(runtime_config._parse_pairing(pairing, fixture.A.peers_path))
            self.assertFalse((fixture.A.root / "new-descendant").exists())
        finally:
            if fixture._cleanups:
                fixture.doCleanups()
            J.tearDownModule()


if __name__ == "__main__":
    unittest.main(verbosity=2)
