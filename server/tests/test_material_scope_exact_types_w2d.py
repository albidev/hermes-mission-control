import copy
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, cast
from unittest import mock

from invite_rooms.admission_receipts import AdmissionReceiptError, AdmissionReceiptStore
from invite_rooms.material_validators import MaterialScopeValidationError, validate_scope_attributes, validate_scope_dict
from invite_rooms.peer_registry import MaterialScope, PeerRegistryError


class ScopeStr(str):
    pass


class ScopeDict(dict):
    pass


class ScopeInt(int):
    pass


class EqualWrongType:
    calls = []

    def __init__(self, value):
        self.value = value

    def __eq__(self, other):
        self.calls.append((type(self).__name__, type(other).__name__))
        return other == self.value

    def __ne__(self, other):
        return not self == other


SCOPE = {
    "owner_installation_id": "install:" + "a" * 32,
    "owner_profile": "default",
    "lobby_id": "lobby-" + "a" * 16,
    "planned_room_id": "room-" + "b" * 16,
    "member_id": "member-" + "c" * 16,
    "invite_id": "invite-" + "d" * 16,
    "peer_id": "peer-" + "e" * 32,
    "endpoint_id": "endpoint-" + "f" * 16,
    "fingerprint": "1" * 64,
    "target_install_id": "install:" + "b" * 32,
    "target_profile": "default",
    "catalog_digest": "2" * 64,
    "policy_digest": "3" * 64,
    "pairing_generation": 7,
}


class ScopeView:
    def __init__(self, values, *, mapping_type=dict):
        self._values = values
        self._mapping_type = mapping_type
        for key, value in values.items():
            setattr(self, key, value)

    def as_dict(self):
        return self._mapping_type(self._values)


def native_scope(values=None):
    return MaterialScope(**(values or SCOPE))


def receipt_kwargs(scope, key="key-a"):
    return {
        "scope": scope,
        "generation": 7,
        "profile": "default",
        "base_url": "http://127.0.0.1:8765",
        "idempotency_key": key,
        "request_fingerprint": "a" * 64,
    }


def put_kwargs(scope, key="key-a"):
    return {
        **receipt_kwargs(scope, key),
        "run_id": "run_" + "a" * 32,
        "status": "started",
        "replayed": False,
    }


def file_bytes(root):
    return {path.name: path.read_bytes() for path in root.iterdir() if path.is_file()}


class ExactTypeScopeValidationTests(unittest.TestCase):
    def test_native_constructor_rejects_every_string_subclass_and_dict_subclass(self):
        for field, value in SCOPE.items():
            if field == "pairing_generation":
                continue
            with self.subTest(field=field):
                altered = dict(SCOPE, **{field: ScopeStr(value)})
                with self.assertRaises(PeerRegistryError) as error:
                    native_scope(altered)
                self.assertEqual(str(error.exception), "invalid_material_scope")

        # **kwargs erases the incoming mapping type; this checks the shared API.
        with self.assertRaises(MaterialScopeValidationError):
            validate_scope_dict(ScopeDict(SCOPE))

        for generation in (True, 7.0, ScopeInt(7)):
            with self.subTest(native_generation_type=type(generation).__name__):
                with self.assertRaises(PeerRegistryError) as error:
                    native_scope(dict(SCOPE, pairing_generation=generation))
                self.assertEqual(str(error.exception), "invalid_material_scope")

    def test_shared_validator_rejects_wrong_types_before_custom_equality(self):
        EqualWrongType.calls.clear()
        for field, value in SCOPE.items():
            with self.subTest(field=field):
                scope = ScopeView(dict(SCOPE))
                setattr(scope, field, EqualWrongType(value))
                self.assertEqual(scope.as_dict(), SCOPE)
                with self.assertRaises(MaterialScopeValidationError):
                    validate_scope_attributes(scope, generation=7, profile="default")
                self.assertEqual(EqualWrongType.calls, [])

        for generation in (True, 7.0, ScopeInt(7), EqualWrongType(7)):
            with self.subTest(generation=repr(generation)):
                scope = ScopeView(dict(SCOPE, pairing_generation=7))
                with self.assertRaises(MaterialScopeValidationError):
                    validate_scope_attributes(scope, generation=cast(Any, generation), profile="default")

        for profile in (ScopeStr("default"), EqualWrongType("default")):
            with self.subTest(profile=repr(profile)):
                scope = ScopeView(dict(SCOPE))
                with self.assertRaises(MaterialScopeValidationError):
                    validate_scope_attributes(scope, generation=7, profile=cast(Any, profile))

    def test_real_store_put_reserve_find_reject_exact_type_cases_without_mutation(self):
        invalid_cases = []
        for field, value in SCOPE.items():
            if field != "pairing_generation":
                invalid_cases.append((field + ":serialized-subclass",
                                      ScopeView(dict(SCOPE, **{field: ScopeStr(value)}))))
            altered = ScopeView(dict(SCOPE))
            setattr(altered, field, EqualWrongType(value))
            invalid_cases.append((field + ":attribute-only", altered))
        invalid_cases.append(("dict-subclass", ScopeView(dict(SCOPE), mapping_type=ScopeDict)))
        for generation in (True, 7.0, ScopeInt(7)):
            invalid_cases.append(("generation:" + type(generation).__name__,
                                  ScopeView(dict(SCOPE, pairing_generation=generation))))

        for label, scope in invalid_cases:
            with self.subTest(case=label):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory) / "admissions"
                    store = AdmissionReceiptStore(root)
                    seed_args = receipt_kwargs(native_scope(), key="seed")
                    seed = store.put(**put_kwargs(native_scope(), key="seed"))
                    before = file_bytes(root)
                    EqualWrongType.calls.clear()
                    for method in ("put", "reserve", "find"):
                        args = put_kwargs(scope, key=method + "-bad") if method == "put" else receipt_kwargs(scope, key=method + "-bad")
                        with mock.patch("invite_rooms.admission_receipts.validate_scope_attributes",
                                        wraps=validate_scope_attributes) as validator:
                            with self.assertRaises(AdmissionReceiptError) as error:
                                getattr(store, method)(**args)
                            validator.assert_called_once_with(scope, generation=7, profile="default")
                        self.assertEqual(str(error.exception), "invalid_material_scope")
                        self.assertEqual(EqualWrongType.calls, [])
                        self.assertEqual(file_bytes(root), before)
                        reopened = AdmissionReceiptStore(root)
                        try:
                            self.assertEqual(reopened.find(**seed_args), seed)
                        finally:
                            reopened.close()
                        self.assertEqual(file_bytes(root), before)
                    store.close()

    def test_malformed_json_scope_records_refuse_on_reopen(self):
        for pending in (False, True):
            with self.subTest(record="pending" if pending else "receipt"):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory) / "admissions"
                    store = AdmissionReceiptStore(root)
                    if pending:
                        record = receipt_kwargs(native_scope(), key="pending")
                        record["scope"] = dict(SCOPE, pairing_generation=7.0)
                        store._write_blob("admission_pending.json.enc", [record])
                    else:
                        record = put_kwargs(native_scope(), key="receipt")
                        store._write([{
                            **record,
                            "scope": dict(SCOPE, pairing_generation=7.0),
                        }])
                    store.close()
                    with self.assertRaises(AdmissionReceiptError) as error:
                        AdmissionReceiptStore(root)
                    self.assertEqual(str(error.exception), "admission_storage_corrupt")

    def test_native_scope_put_find_and_reopen_remain_valid(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "admissions"
            scope = native_scope()
            store = AdmissionReceiptStore(root)
            stored = store.put(**put_kwargs(scope))
            self.assertEqual(stored["scope"], SCOPE)
            self.assertEqual(store.find(**receipt_kwargs(scope)), stored)
            self.assertIsNone(store.reserve(**receipt_kwargs(scope, key="pending")))
            store.close()

            reopened = AdmissionReceiptStore(root)
            self.assertEqual(reopened.find(**receipt_kwargs(scope)), stored)
            self.assertEqual(reopened.count(), 1)
            reopened.close()


if __name__ == "__main__":
    unittest.main()
