import json
import multiprocessing
import os
import socket
import threading
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from invite_rooms import admission_receipts
from invite_rooms.admission_receipts import AdmissionReceiptError, AdmissionReceiptStore
from invite_rooms.runtime_config import RuntimeConfigError, TargetHttpConfig, parse_target_http
from invite_rooms.target_http import HttpResponse, TargetHttpClient, TargetHttpError


class Scope:
    pairing_generation = 7
    target_profile = "default"

    def as_dict(self):
        return {
            "owner_installation_id": "install:" + "a" * 32, "owner_profile": "default",
            "lobby_id": "lobby-" + "a" * 16, "planned_room_id": "room-" + "b" * 16,
            "member_id": "member-" + "c" * 16, "invite_id": "invite-" + "d" * 16,
            "peer_id": "peer-" + "e" * 32, "endpoint_id": "endpoint-" + "f" * 16,
            "fingerprint": "1" * 64, "target_install_id": "install:" + "b" * 32,
            "target_profile": "default", "catalog_digest": "2" * 64,
            "policy_digest": "3" * 64, "pairing_generation": self.pairing_generation,
        }

    def __getattr__(self, name):
        if name in {"owner_installation_id", "owner_profile", "lobby_id", "planned_room_id", "member_id",
                    "invite_id", "peer_id", "endpoint_id", "fingerprint", "target_install_id",
                    "target_profile", "catalog_digest", "policy_digest"}:
            return self.as_dict()[name]
        raise AttributeError(name)


class Material:
    def load(self, scope):
        return {"binding": {}, "catalog": {}, "grant": "opaque-grant"}


class TargetHttpCard1Tests(unittest.TestCase):
    def _capacity_expected(self, size):
        return [{"version": "mc-admission-receipt-v1", "scope": Scope().as_dict(),
                 "generation": 7, "profile": "default", "base_url": "http://127.0.0.1:8765",
                 "idempotency_key": f"k-{index}", "request_fingerprint": f"{index:064x}",
                 "run_id": "run_" + f"{index:032x}", "status": "started", "replayed": False}
                for index in range(size)]

    def _capacity_put_args(self, record):
        return {**{key: value for key, value in record.items() if key not in {"version", "scope"}},
                "scope": Scope(), "verifier_version": record["version"]}

    def _capacity_error(self, operation, reason):
        with self.assertRaises(AdmissionReceiptError) as caught:
            operation()
        self.assertIs(type(caught.exception), AdmissionReceiptError, "capacity exception type")
        self.assertEqual(caught.exception.args, (reason,), "capacity exception args")

    def _capacity_phase(self, phase):
        print("capacity-phase " + self._testMethodName + " " + phase, flush=True)

    def _capacity_overflow(self, store):
        self._capacity_phase("overflow-before")
        self._capacity_error(lambda: store.put(
            scope=Scope(), generation=7, profile="default", base_url="http://127.0.0.1:8765",
            idempotency_key="overflow", request_fingerprint="f" * 64,
            run_id="run_" + "f" * 32, status="started", replayed=False), "admission_capacity")
        self._capacity_phase("overflow-after")

    def _capacity_samples(self, store, expected, indices):
        for index in indices:
            record = expected[index]
            self.assertTrue(store.find_idempotency_key(record["idempotency_key"]) == record,
                            "public idempotency sample equality")
            lookup = {key: value for key, value in self._capacity_put_args(record).items()
                      if key not in {"verifier_version", "run_id", "status", "replayed"}}
            self.assertTrue(store.find(**lookup) == record, "public find sample equality")
        self.assertIsNone(store.find_idempotency_key("overflow"))
        self.assertIsNone(store.find(scope=Scope(), generation=7, profile="default",
                                     base_url="http://127.0.0.1:8765", idempotency_key="overflow",
                                     request_fingerprint="f" * 64))

    def test_target_config_is_exact_loopback_explicit_port(self):
        self.assertEqual(parse_target_http({"base_url": "http://127.0.0.1:8765", "profile": "default"}).base_url,
                         "http://127.0.0.1:8765")
        for value in (
            {"base_url": "http://localhost:8765", "profile": "default"},
            {"base_url": "http://127.0.0.1", "profile": "default"},
            {"base_url": "http://127.0.0.1:0", "profile": "default"},
            {"base_url": "https://127.0.0.1:8765", "profile": "default"},
            {"base_url": "http://x@127.0.0.1:8765", "profile": "default"},
            {"base_url": "http://127.0.0.1:8765/path", "profile": "default"},
            {"base_url": "http://127.0.0.1:8765", "profile": "bad profile"},
            {"base_url": "http://127.0.0.1:8765", "profile": "default", "extra": 1},
            {"base_url": "http://127.0.0.1:8765?", "profile": "default"},
            {"base_url": "http://127.0.0.1:8765#", "profile": "default"},
            {"base_url": "http://127.0.0.1:8765\n", "profile": "default"},
        ):
            with self.assertRaises(RuntimeConfigError):
                parse_target_http(value)
        with self.assertRaises(RuntimeConfigError):
            TargetHttpConfig("http://127.0.0.1:8765?", "default")

    def test_new_response_receipt_and_strict_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            store = AdmissionReceiptStore(Path(directory) / "admissions")
            seen = []
            responses = [HttpResponse(202, json.dumps({
                "run_id": "run_" + "a" * 32, "status": "started", "replayed": False}).encode()),
                         HttpResponse(202, json.dumps({
                "run_id": "run_" + "a" * 32, "status": "running", "replayed": True}).encode())]
            def transport(url, headers, body):
                seen.append((url, headers, body))
                return responses.pop(0)
            client = TargetHttpClient(TargetHttpConfig("http://127.0.0.1:8765", "default"), transport=transport)
            result = client.admit_run(body={"input": "hello"}, idempotency_key="k-1", scope=Scope(),
                                      material_store=Material(), admission_store=store)
            self.assertFalse(result["replayed"])
            self.assertEqual(seen[0][0], "http://127.0.0.1:8765/v1/runs")
            self.assertEqual(seen[0][1]["Authorization"], "HermesRoom opaque-grant")
            self.assertNotIn("API_SERVER_KEY", seen[0][1])
            reopened = AdmissionReceiptStore(Path(directory) / "admissions")
            result = client.admit_run(body={"input": "hello"}, idempotency_key="k-1", scope=Scope(),
                                      material_store=Material(), admission_store=reopened)
            self.assertTrue(result["replayed"])
            self.assertEqual(reopened.count(), 1)

    def test_key_reuse_with_changed_payload_is_refused_before_transport(self):
        with tempfile.TemporaryDirectory() as directory:
            store = AdmissionReceiptStore(Path(directory) / "admissions")
            responses = [HttpResponse(202, json.dumps({
                "run_id": "run_" + "b" * 32, "status": "started", "replayed": False}).encode())]
            calls = []
            client = TargetHttpClient(TargetHttpConfig("http://127.0.0.1:8765", "default"),
                                      transport=lambda *args: (calls.append(args), responses.pop(0))[1])
            client.admit_run(body={"input": "one"}, idempotency_key="same", scope=Scope(),
                             material_store=Material(), admission_store=store)
            with self.assertRaises(TargetHttpError):
                client.admit_run(body={"input": "two"}, idempotency_key="same", scope=Scope(),
                                 material_store=Material(), admission_store=store)
            self.assertEqual(len(calls), 1)

    def test_bad_new_response_does_not_write(self):
        with tempfile.TemporaryDirectory() as directory:
            store = AdmissionReceiptStore(Path(directory) / "admissions")
            client = TargetHttpClient(TargetHttpConfig("http://127.0.0.1:8765", "default"),
                                      transport=lambda *args: HttpResponse(202, b'{"run_id":"bad","status":"started","replayed":false}'))
            with self.assertRaises(TargetHttpError):
                client.admit_run(body={"input": "hello"}, idempotency_key="k-1", scope=Scope(),
                                 material_store=Material(), admission_store=store)
            self.assertEqual(store.count(), 0)

    def test_redirect_is_not_followed_and_session_key_is_transmitted(self):
        with tempfile.TemporaryDirectory() as directory:
            store = AdmissionReceiptStore(Path(directory) / "admissions")
            seen = []
            client = TargetHttpClient(TargetHttpConfig("http://127.0.0.1:8765", "default"),
                                      transport=lambda url, headers, body: (seen.append((url, headers, body)),
                                      HttpResponse(202, json.dumps({"run_id": "run_" + "c" * 32,
                                      "status": "started", "replayed": False}).encode()))[1])
            client.admit_run(body={"input": "hello"}, idempotency_key="k-2", scope=Scope(),
                             material_store=Material(), admission_store=store,
                             gateway_session_key="session-secret")
            self.assertEqual(seen[0][1]["X-Hermes-Session-Key"], "session-secret")

    def test_replay_statuses_are_source_derived(self):
        from invite_rooms.target_http import REPLAY_STATUSES
        self.assertIn("waiting_for_approval", REPLAY_STATUSES)
        self.assertIn("stopping", REPLAY_STATUSES)
        self.assertNotIn("claimed", REPLAY_STATUSES)

    def test_duplicate_config_keys_are_rejected_unconditionally(self):
        from invite_rooms.runtime_config import _load_config_file
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "private"
            root.mkdir(mode=0o700)
            config = root / "config.json"
            config.write_text('{"store_path":"/tmp/a","store_path":"/tmp/b",'
                              '"installation_id":"' + 'i' * 32 + '","profile":"default",'
                              '"peer_registry_path":"/tmp/p"}')
            config.chmod(0o600)
            self.assertIsNone(_load_config_file(str(config)))

    def test_r02_missing_key_with_pending_refuses_without_regeneration(self):
        from invite_rooms.admission_receipts import AdmissionReceiptError
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "admissions"
            store = AdmissionReceiptStore(root)
            store.reserve(scope=Scope(), generation=7, profile="default", base_url="http://127.0.0.1:8765",
                          idempotency_key="pending", request_fingerprint="f" * 64)
            (root / "admission_receipts.key").unlink()
            with self.assertRaises(AdmissionReceiptError):
                AdmissionReceiptStore(root)
            self.assertFalse((root / "admission_receipts.key").exists())

    def test_r02_oversized_record_rejected_before_write(self):
        from invite_rooms.admission_receipts import AdmissionReceiptError
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "admissions"
            store = AdmissionReceiptStore(root)
            before = (root / "admission_receipts.json.enc").read_bytes() if (root / "admission_receipts.json.enc").exists() else None
            with self.assertRaises(AdmissionReceiptError):
                store.put(scope=Scope(), generation=7, profile="default", base_url="http://127.0.0.1:8765",
                          idempotency_key="oversize", request_fingerprint="x" * 70000,
                          run_id="run_" + "a" * 32, status="started", replayed=False)
            after = (root / "admission_receipts.json.enc").read_bytes() if (root / "admission_receipts.json.enc").exists() else None
            self.assertEqual(before, after)
            self.assertEqual(store.count(), 0)

    def test_r02_client_revalidates_bypassed_typed_config(self):
        config = object.__new__(TargetHttpConfig)
        object.__setattr__(config, "base_url", "http://example.invalid:12345")
        object.__setattr__(config, "profile", "default")
        with self.assertRaises(TargetHttpError):
            TargetHttpClient(config, transport=lambda *args: self.fail("transport contacted"))

    def test_capacity_rejection_preserves_readable_store(self):
        self.assertEqual(admission_receipts._MAX_RECORDS, 4096, "literal real capacity")
        self._capacity_phase("build-before")
        expected = self._capacity_expected(4096)
        self.assertEqual(len({r["idempotency_key"] for r in expected}), 4096)
        self.assertEqual(len({AdmissionReceiptStore._key(r) for r in expected}), 4096)
        schema = {"version", "scope", "generation", "profile", "base_url", "idempotency_key",
                  "request_fingerprint", "run_id", "status", "replayed"}
        self.assertEqual(admission_receipts._MAX_RECORD_BYTES, 65536)
        self.assertEqual(admission_receipts._MAX_BLOB_BYTES, 8388608)
        for record in expected:
            self.assertIs(type(record), dict)
            self.assertEqual(set(record), schema)
            self.assertIs(type(record["scope"]), dict)
            AdmissionReceiptStore._validate_record(record, pending=False)
            self.assertLessEqual(len(json.dumps(record, sort_keys=True, separators=(",", ":")).encode()),
                                 65536)
        self.assertLessEqual(len(json.dumps(expected, sort_keys=True, separators=(",", ":")).encode()) + 28,
                             8388608)
        self._capacity_phase("build-after")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "admissions"
            store = reopened = None
            try:
                store = AdmissionReceiptStore(root)
                self._capacity_phase("seed-before")
                with store._transaction():
                    self.assertEqual(store._read(), [])
                    self.assertEqual(store._read_pending(), [])
                    store._write(expected[:4095])
                    self.assertTrue(store._read() == expected[:4095], "seed entire-list equality")
                self._capacity_phase("seed-after")
                self._capacity_phase("count-before")
                self.assertEqual(store.count(), 4095, "pre-last literal count")
                self._capacity_phase("count-after")
                self._capacity_phase("last-put-before")
                self.assertTrue(store.put(**self._capacity_put_args(expected[4095])) == expected[4095],
                                "last-slot public put equality")
                self._capacity_phase("last-put-after")
                self.assertEqual(store.count(), 4096, "last-slot literal count")
                before = store.path.read_bytes()
                self._capacity_overflow(store)
                self.assertTrue(store.path.read_bytes() == before, "overflow ciphertext equality")
                self._capacity_phase("count-before")
                self.assertEqual(store.count(), 4096, "overflow literal count")
                self._capacity_phase("count-after")
                self._capacity_phase("readback-before")
                with store._transaction():
                    self.assertTrue(store._read() == expected, "open entire-list equality")
                self._capacity_phase("readback-after")
                self._capacity_phase("close-before")
                store.close()
                self._capacity_phase("close-after")
                self._capacity_phase("reopen-before")
                reopened = AdmissionReceiptStore(root)
                self._capacity_phase("reopen-after")
                self.assertTrue(reopened.path.read_bytes() == before, "reopen ciphertext equality")
                self.assertEqual(reopened.count(), 4096, "reopen literal count")
                self._capacity_phase("readback-before")
                with reopened._transaction():
                    self.assertTrue(reopened._read() == expected, "reopen entire-list equality")
                self._capacity_samples(reopened, expected, (0, 2048, 4095))
                self._capacity_phase("readback-after")
                self.assertTrue(reopened.put(**self._capacity_put_args(expected[4095])) == expected[4095],
                                "full-capacity duplicate equality")
                self.assertTrue(reopened.path.read_bytes() == before, "duplicate ciphertext equality")
                self.assertEqual(reopened.count(), 4096)
                conflict = {**self._capacity_put_args(expected[4095]), "run_id": "run_" + "e" * 32}
                self._capacity_error(lambda: reopened.put(**conflict), "admission_receipt_conflict")
                self.assertTrue(reopened.path.read_bytes() == before, "conflict ciphertext equality")
                self.assertEqual(reopened.count(), 4096)
            finally:
                for instance in (reopened, store):
                    if instance is not None:
                        instance.close()

    def test_capacity_scaled_public_put_rejection_preserves_reopen(self):
        self.assertEqual(admission_receipts._MAX_RECORDS, 4096)
        with patch.object(admission_receipts, "_MAX_RECORDS", 4):
            self._capacity_phase("build-before")
            expected = self._capacity_expected(4)
            self._capacity_phase("build-after")
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / "admissions"
                store = reopened = None
                try:
                    store = AdmissionReceiptStore(root)
                    self._capacity_phase("seed-before")
                    for record in expected:
                        self.assertTrue(store.put(**self._capacity_put_args(record)) == record,
                                        "scaled public put equality")
                    self._capacity_phase("seed-after")
                    self._capacity_phase("count-before")
                    self.assertEqual(store.count(), 4)
                    self._capacity_phase("count-after")
                    before = store.path.read_bytes()
                    self._capacity_overflow(store)
                    self.assertTrue(store.path.read_bytes() == before, "scaled overflow ciphertext equality")
                    self.assertEqual(store.count(), 4)
                    with store._transaction():
                        self.assertTrue(store._read() == expected, "scaled open entire-list equality")
                    self._capacity_phase("close-before")
                    store.close()
                    self._capacity_phase("close-after")
                    self._capacity_phase("reopen-before")
                    reopened = AdmissionReceiptStore(root)
                    self._capacity_phase("reopen-after")
                    self.assertTrue(reopened.path.read_bytes() == before, "scaled reopen ciphertext equality")
                    self.assertEqual(reopened.count(), 4)
                    self._capacity_phase("readback-before")
                    with reopened._transaction():
                        self.assertTrue(reopened._read() == expected, "scaled reopen entire-list equality")
                    self._capacity_samples(reopened, expected, (0, 1, 2, 3))
                    self._capacity_phase("readback-after")
                finally:
                    for instance in (reopened, store):
                        if instance is not None:
                            instance.close()
        self.assertEqual(admission_receipts._MAX_RECORDS, 4096)

    def test_ancestor_symlink_is_refused_before_any_private_files(self):
        from invite_rooms.admission_receipts import AdmissionReceiptError
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            real = base / "real" / "nested"
            real.mkdir(parents=True)
            alias = base / "alias"
            alias.symlink_to(base / "real", target_is_directory=True)
            root = alias / "nested" / "store"
            with self.assertRaises(AdmissionReceiptError):
                AdmissionReceiptStore(root)
            self.assertFalse((real / "nested" / "store").exists())
            self.assertEqual(list(real.iterdir()), [])

    def test_failed_link_preserves_committed_receipt_and_reopen(self):
        from invite_rooms.admission_receipts import AdmissionReceiptError
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "admissions"
            store = AdmissionReceiptStore(root)
            kwargs = dict(scope=Scope(), generation=7, profile="default", base_url="http://127.0.0.1:8765",
                          idempotency_key="old", request_fingerprint="a" * 64,
                          run_id="run_" + "a" * 32, status="started", replayed=False)
            store.put(**kwargs)
            before = (root / "admission_receipts.json.enc").read_bytes()
            with patch("invite_rooms.admission_receipts.os.link", side_effect=OSError("injected-link")):
                with self.assertRaises(AdmissionReceiptError):
                    store.put(**{**kwargs, "idempotency_key": "new", "request_fingerprint": "b" * 64,
                                 "run_id": "run_" + "b" * 32})
            self.assertEqual(before, (root / "admission_receipts.json.enc").read_bytes())
            reopened = AdmissionReceiptStore(root)
            self.assertEqual(reopened.count(), 1)
            self.assertEqual(reopened.find_idempotency_key("old")["run_id"], "run_" + "a" * 32)

    def test_receipt_schema_rejects_bad_types_and_values_before_mutation(self):
        from invite_rooms.admission_receipts import AdmissionReceiptError
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "admissions"
            store = AdmissionReceiptStore(root)
            good = dict(scope=Scope(), generation=7, profile="default", base_url="http://127.0.0.1:8765",
                        idempotency_key="good", request_fingerprint="a" * 64,
                        run_id="run_" + "a" * 32, status="started", replayed=False)
            for field, value in (("run_id", None), ("profile", 3), ("status", []),
                                 ("request_fingerprint", None), ("verifier_version", "arbitrary")):
                with self.assertRaises(AdmissionReceiptError):
                    store.put(**{**good, field: value})
            for field, value in (("generation", True), ("idempotency_key", None), ("request_fingerprint", 7)):
                with self.assertRaises(AdmissionReceiptError):
                    store.reserve(scope=Scope(), generation=value if field == "generation" else 7,
                                  profile="default", base_url="http://127.0.0.1:8765",
                                  idempotency_key=value if field == "idempotency_key" else "reserve",
                                  request_fingerprint=value if field == "request_fingerprint" else "b" * 64)
            self.assertFalse((root / "admission_receipts.json.enc").exists())
            self.assertEqual(store.count(), 0)
            self.assertEqual(AdmissionReceiptStore(root).count(), 0)

    def test_anchor_open_race_refuses_without_external_files(self):
        from invite_rooms.admission_receipts import AdmissionReceiptError
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            parent = base / "parent"
            nested = parent / "nested"
            parent.mkdir(mode=0o700)
            nested.mkdir(mode=0o700)
            outside = base / "outside"
            outside.mkdir(mode=0o700)
            moved = base / "parent-moved"
            real_open = os.open
            hits = []

            def swap_anchor(name, *args, **kwargs):
                # Swap the component at the actual descriptor-relative open seam.
                # The retained parent fd must prevent this from redirecting writes.
                if name == "nested" and "dir_fd" in kwargs:
                    hits.append(1)
                    nested.rename(moved)
                    nested.symlink_to(outside, target_is_directory=True)
                return real_open(name, *args, **kwargs)

            with patch("invite_rooms.admission_receipts.os.open", side_effect=swap_anchor):
                with self.assertRaises(AdmissionReceiptError):
                    AdmissionReceiptStore(nested / "store")
            self.assertEqual(hits, [1])
            self.assertEqual(list(outside.iterdir()), [])

    def test_stage_and_commit_substitution_refuse_and_preserve_old_bytes(self):
        from invite_rooms.admission_receipts import AdmissionReceiptError
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "admissions"
            store = AdmissionReceiptStore(root)
            old = dict(scope=Scope(), generation=7, profile="default", base_url="http://127.0.0.1:8765",
                       idempotency_key="old", request_fingerprint="a" * 64,
                       run_id="run_" + "a" * 32, status="started", replayed=False)
            store.put(**old)
            before = store.path.read_bytes()
            original_link = os.link

            def replace_stage(src, dst, *args, **kwargs):
                if str(src).startswith("." + "admission_receipts.json.enc"):
                    os.unlink(src, dir_fd=kwargs["src_dir_fd"])
                    fd = os.open(src, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600,
                                 dir_fd=kwargs["src_dir_fd"])
                    os.write(fd, b"attacker-stage")
                    os.close(fd)
                return original_link(src, dst, *args, **kwargs)

            new = {**old, "idempotency_key": "new", "request_fingerprint": "b" * 64,
                   "run_id": "run_" + "b" * 32}
            with patch("invite_rooms.admission_receipts.os.link", side_effect=replace_stage):
                with self.assertRaises(AdmissionReceiptError):
                    store.put(**new)
            self.assertEqual(store.path.read_bytes(), before)
            self.assertEqual(AdmissionReceiptStore(root).find_idempotency_key("old")["run_id"], old["run_id"])

            original_replace = os.replace

            def replace_commit(src, dst, *args, **kwargs):
                if dst == "admission_receipts.json.enc":
                    os.unlink(src, dir_fd=kwargs["src_dir_fd"])
                    fd = os.open(src, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600,
                                 dir_fd=kwargs["src_dir_fd"])
                    os.write(fd, b"attacker-commit")
                    os.close(fd)
                return original_replace(src, dst, *args, **kwargs)

            with patch("invite_rooms.admission_receipts.os.replace", side_effect=replace_commit):
                with self.assertRaises(AdmissionReceiptError):
                    store.put(**new)
            self.assertEqual(store.path.read_bytes(), before)
            self.assertEqual(AdmissionReceiptStore(root).find_idempotency_key("old")["run_id"], old["run_id"])

    def test_scope_and_authenticated_read_schema_are_strict(self):
        from invite_rooms.admission_receipts import AdmissionReceiptError
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "admissions"
            store = AdmissionReceiptStore(root)
            good = dict(scope=Scope(), generation=7, profile="default", base_url="http://127.0.0.1:8765",
                        idempotency_key="good", request_fingerprint="a" * 64,
                        run_id="run_" + "a" * 32, status="started", replayed=False)
            for url in ("http://127.0.0.1:0", "http://127.0.0.1:8765?", "http://127.0.0.1:8765#"):
                with self.assertRaises(AdmissionReceiptError):
                    store.put(**{**good, "base_url": url})
            with self.assertRaises(AdmissionReceiptError):
                store.reserve(scope=Scope(), generation=8, profile="default", base_url=good["base_url"],
                              idempotency_key="pending", request_fingerprint="b" * 64)
            record = store.put(**good)
            record["run_id"] = None
            store._write([record])
            with self.assertRaises(AdmissionReceiptError):
                AdmissionReceiptStore(root)


if __name__ == "__main__":
    unittest.main()
