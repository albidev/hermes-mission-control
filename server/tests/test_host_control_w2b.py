from __future__ import annotations

import unittest
from dataclasses import dataclass

from invite_rooms.consent import ConsentError
from invite_rooms.control.dispatcher import ControlError
from invite_rooms.host_control import build_host_handlers
from invite_rooms.service import OwnerContext
from room_invite_store import InviteError


@dataclass
class Context:
    peer_id: str = "peer-b"
    consent_public_key: bytes = b"k" * 32
    binding: dict = None
    admitted: int = 0

    def __post_init__(self):
        if self.binding is None:
            self.binding = {"endpoint_id": "ep", "fingerprint": "fp", "target_install_id": "install:" + "b" * 32,
                            "target_profile": "default", "catalog_digest": "c" * 64, "policy_digest": "d" * 64}

    def binding_dict(self):
        return dict(self.binding)

    def readmit(self):
        self.admitted += 1
        return True


class Store:
    def __init__(self):
        self.calls = []
        self.status = {"status": "consent_required", "scope": {"host_id": "install:" + "a" * 32,
            "room_id": "planned-room", "member_id": "peer-b", "lobby_id": "lobby-1", "invite_id": "invite-1",
            "binding": Context().binding}}

    def peer_preview(self, *args, **kwargs):
        self.calls.append(("preview", args, kwargs))
        return {"name": "Owners", "expires_at": 123.0}

    def redeem(self, *args, **kwargs):
        self.calls.append(("redeem", args, kwargs))
        return {"status": "awaiting_host_approval"}

    def peer_scope_status(self, *args, **kwargs):
        self.calls.append(("status", args, kwargs))
        return self.status

    def consent_joining(self, *args, **kwargs):
        self.calls.append(("consent_joining", args, kwargs))
        return {"status": "ready"}

    def is_owner_scope_bound(self, installation_id, profile):
        return (installation_id, profile) == ("install:" + "a" * 32, "default")


class HostControlTest(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.context = Context()
        self.handlers = build_host_handlers(self.store, OwnerContext("install:" + "a" * 32, "default"))

    def call(self, op, **fields):
        request = {"op": op, "lobby_id": "lobby-1", "invite_id": "invite-1", **fields}
        label = "joining_consent" if op == "consent" else op
        return self.handlers[label](self.context, request)

    def test_default_factory_has_exact_contextual_operations(self):
        self.assertEqual(set(self.handlers), {"preview", "redeem", "status", "joining_consent"})

    def test_preview_uses_context_identity_binding_and_readmit(self):
        self.assertEqual(self.call("preview"), {"name": "Owners", "expires_at": 123.0})
        name, args, kw = self.store.calls[-1]
        self.assertEqual(name, "preview")
        self.assertEqual(args, ("lobby-1", "invite-1"))
        self.assertEqual(kw, {"peer_id": "peer-b", "binding": self.context.binding,
                              "before_return": self.context.readmit})

    def test_redeem_uses_context_and_never_returns_secret(self):
        self.assertEqual(self.call("redeem", secret="s" * 43), {"status": "awaiting_host_approval"})
        name, args, kw = self.store.calls[-1]
        self.assertEqual(name, "redeem")
        self.assertEqual(args, ("lobby-1", "invite-1", "s" * 43))
        self.assertEqual(kw, {"peer_id": "peer-b", "binding": self.context.binding,
                              "before_effect": self.context.readmit})

    def test_status_exactly_passes_context_and_returns_read_model(self):
        self.assertEqual(self.call("status"), self.store.status)
        self.assertEqual(self.store.calls[-1][2], {"peer_id": "peer-b", "binding": self.context.binding,
                                                    "before_return": self.context.readmit})

    def test_consent_verifies_pinned_key_and_exact_scope_then_mutates(self):
        import invite_rooms.consent as consent
        observed = []
        original = consent.verify
        consent.verify = lambda key, receipt, signature, expected: observed.append((key, receipt, signature, expected))
        try:
            scope = self.store.status["scope"]
            receipt = dict(scope, purpose="joining-owner-consent-v1")
            self.assertEqual(self.call("consent", receipt=receipt, signature="x" * 88), {"status": "ready"})
        finally:
            consent.verify = original
        self.assertEqual(observed, [(self.context.consent_public_key, receipt, "x" * 88, receipt)])
        self.assertEqual(self.store.calls[-1][0], "consent_joining")
        self.assertEqual(self.store.calls[-1][2], {"binding": self.context.binding, "consented": True,
            "peer_id": "peer-b", "before_effect": self.context.readmit})

    def test_all_handler_failures_are_generic_control_error(self):
        def fail(*args, **kwargs):
            raise InviteError("secret leaked")
        self.store.peer_preview = fail
        with self.assertRaises(ControlError) as error:
            self.call("preview")
        self.assertEqual(str(error.exception), "control_unavailable")

    def test_malformed_consent_is_rejected_before_store_effect(self):
        for receipt, signature in (({}, "x" * 88), (None, "x" * 88), ({"x": 1}, 1)):
            with self.assertRaises(ControlError) as error:
                self.call("consent", receipt=receipt, signature=signature)
            self.assertEqual(str(error.exception), "control_unavailable")
        self.assertFalse(any(name == "consent_joining" for name, _, _ in self.store.calls))


if __name__ == "__main__":
    unittest.main()
