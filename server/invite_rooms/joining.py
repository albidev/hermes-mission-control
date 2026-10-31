"""Joining-owner local operations: transport-agnostic, callable later by both
HTTP routes (P2b-http) and a headless adapter.

No network/navigation/fetch happens anywhere except through the single
injected `channel.call(request) -> dict` control-channel interface and the
single injected `rpc(method, params)` bounded native RPC coroutine (the exact
contract already defined in invite_rooms/native_rpc.py). This module never
imports a transport implementation (tls_stream/connector) and never imports
spike code; it only defines interfaces those live behind.

Preserves the accepted joining-owner contract: same consent
receipt/scope shape, same material envelope/signature purpose strings, same
canonical encoding (invite_rooms/consent.py). A real-lab parity test drives
the real control adapter against this implementation
without this module ever importing them.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from invite_rooms.consent import ConsentKey, canonical, verify as verify_consent
from invite_rooms.invitation import ApprovedHostRegistry, InvitationError, parse_invitation
from invite_rooms.joining_store import JoiningStore, JoiningStoreError
from invite_rooms.peer_binding import PeerBindingError, validate_peer_binding

_TOKEN = r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}"
_ROOM_ID = r"invite-room-[A-Za-z0-9-]{1,64}"
# The product service requires the full six-field self-binding (with pinned
# catalog_digest/policy_digest): no four-field legacy fallback here. The
# four-field shape remains only as the real control lab's OWN historical
# contract, exercised solely by the real-lab parity test fixture, never by
# this module (R2).
_SIX_FIELD = {"endpoint_id", "fingerprint", "target_install_id", "target_profile",
              "catalog_digest", "policy_digest"}
_BINDING_PATTERNS = {
    "endpoint_id": r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}",
    "fingerprint": r"[0-9a-f]{64}",
    "target_install_id": r"install:[0-9a-f]{32}",
    "target_profile": r"[a-z0-9][a-z0-9_-]{0,63}",
    "catalog_digest": r"[0-9a-f]{64}",
    "policy_digest": r"[0-9a-f]{64}",
}


class JoiningError(ValueError):
    """Stable machine code; never includes remote values or secrets."""


def _validate_self_binding(binding: dict) -> dict:
    try:
        return validate_peer_binding(binding)
    except PeerBindingError:
        raise JoiningError("invalid_self_binding") from None


class JoiningOwnerService:
    """The joining owner's client-side operations: preview, redeem, consent.

    Bound at construction to exactly ONE pre-approved host peer and ONE
    pinned invitation origin (MVP scope: one host, one joining
    installation/profile). `self_binding` is owner-provided fixed local
    configuration describing this installation's own identity as the host
    will see it — never learned from the invitation or from the host.
    """

    def __init__(self, store: JoiningStore, host_registry: ApprovedHostRegistry, *,
                 host_peer_id: str, origin: str, consent_key: ConsentKey, channel,
                 self_binding: dict):
        if not isinstance(store, JoiningStore) or not isinstance(host_registry, ApprovedHostRegistry):
            raise JoiningError("invalid_service_dependencies")
        if not isinstance(consent_key, ConsentKey):
            raise JoiningError("invalid_service_dependencies")
        if channel is None or not callable(getattr(channel, "call", None)):
            raise JoiningError("invalid_control_channel")
        # Fail closed immediately if the pinned host peer is unknown: an
        # invitation can never cause a new host to become approved.
        self._host_binding = host_registry.binding_for(host_peer_id)
        self._store = store
        self._host_registry = host_registry
        self._host_peer_id = host_peer_id
        self._origin = origin
        self._consent_key = consent_key
        self._channel = channel
        self._self_binding = _validate_self_binding(self_binding)

    def _owned_import(self, import_id: str) -> dict:
        record = self._store.get_import(import_id)
        if record["host_peer_id"] != self._host_peer_id:
            raise JoiningError("peer_binding_mismatch")
        return record

    def _call(self, request: dict) -> dict:
        try:
            response = self._channel.call(request)
        except Exception:
            raise JoiningError("control_unavailable") from None
        if not isinstance(response, dict):
            raise JoiningError("control_unavailable")
        return response

    def preview(self, invitation_text: str) -> dict:
        """Parse and persist LOCALLY ONLY: no network/navigation/fetch
        happens anywhere in this method. Resolves the referenced host
        against the pre-approved registry and stores the secret once,
        encrypted; it is never returned. Non-secret invite metadata
        (name/expires_at) is NOT fetched here — that is the separate,
        explicit, non-consuming `refresh_metadata()` owner action (R8)."""
        parsed = parse_invitation(invitation_text, origin=self._origin)
        host = self._host_binding
        import_id = self._store.create_import(
            lobby_id=parsed.lobby_id, invite_id=parsed.invite_id,
            secret=parsed.secret, host_peer_id=self._host_peer_id,
        )
        return {
            "import_id": import_id,
            "lobby_id": parsed.lobby_id,
            "invite_id": parsed.invite_id,
            "host": {
                "peer_id": host.peer_id,
                "installation_id": host.installation_id,
                "profile": host.profile,
                "fingerprint": host.fingerprint,
                "policy_digest": host.policy_digest,
                "catalog_digest": host.catalog_digest,
            },
        }

    def refresh_metadata(self, import_id: str) -> dict:
        """Explicit owner action: ask the ALREADY-PINNED control channel for
        non-secret, non-consuming invite metadata (name/expires_at). Never
        touches import state and never sends the invitation secret."""
        record = self._owned_import(import_id)
        response = self._call({"op": "preview", "invite_id": record["invite_id"]})
        if (set(response) != {"name", "expires_at"} or not isinstance(response["name"], str)
                or type(response["expires_at"]) not in (int, float)):
            raise JoiningError("control_unavailable")
        return {"import_id": import_id, "name": response["name"], "expires_at": response["expires_at"]}

    def redeem(self, import_id: str) -> dict:
        """Explicit owner action. Idempotent: a second call never re-sends the
        secret and never produces a second native effect by itself. If the
        host's reply is lost after the secret has actually been sent, the
        import becomes visibly 'redeem_ambiguous' rather than silently
        reverting to 'previewed' (which would otherwise resend the secret on
        the next call) — see resolve_redeem_ambiguous() for the only
        (explicit, owner-directed) recovery path (R4)."""
        record = self._owned_import(import_id)
        if record["state"] == "redeemed":
            return {"import_id": import_id, "status": "already_redeemed"}
        if record["state"] == "redeem_ambiguous":
            raise JoiningError("redeem_ambiguous")
        if record["state"] == "redeeming":
            raise JoiningError("redeem_ambiguous")
        if record["state"] not in ("previewed",):
            raise JoiningError("import_state_conflict")
        self._store.begin_redeem(import_id)  # durable intent BEFORE the send
        try:
            secret = self._store.load_secret(import_id)
        except Exception:
            self._store.mark_redeem_ambiguous(import_id)
            raise JoiningError("redeem_ambiguous") from None
        try:
            response = self._call({"op": "redeem", "invite_id": record["invite_id"], "secret": secret})
        except JoiningError:
            self._store.mark_redeem_ambiguous(import_id)
            raise
        if set(response) != {"status"} or not isinstance(response["status"], str):
            self._store.mark_redeem_ambiguous(import_id)
            raise JoiningError("control_unavailable")
        self._store.mark_redeemed(import_id)
        return {"import_id": import_id, "status": response["status"]}

    def resolve_redeem_ambiguous(self, import_id: str, *, confirmed_redeemed: bool) -> dict:
        """The ONLY recovery path out of 'redeem_ambiguous'. This is an
        explicit owner action, never automatic: the owner must have
        authenticated, out-of-band, whether the host actually redeemed this
        exact invite (e.g. via an authenticated readback of the exact
        binding) before calling this. It never re-sends the secret."""
        self._owned_import(import_id)
        try:
            self._store.resolve_redeem_ambiguous(import_id, redeemed=confirmed_redeemed)
        except JoiningStoreError as exc:
            raise JoiningError(str(exc)) from None
        return {"import_id": import_id, "status": "redeemed" if confirmed_redeemed else "previewed"}

    def confirm_scope(self, import_id: str, *, room_id: str, member_id: str) -> None:
        """Owner-private manual review: the exact binding this consent will
        cover must be confirmed BEFORE a signature can ever be produced for
        it. Re-confirming a different room/member for the same import is a
        conflict, never a silent overwrite."""
        record = self._owned_import(import_id)
        if not re.fullmatch(_ROOM_ID, room_id) or not re.fullmatch(_TOKEN, member_id):
            raise JoiningError("invalid_owner_scope")
        scope = {
            "room_id": room_id, "member_id": member_id, "lobby_id": record["lobby_id"],
            "host_id": self._host_binding.installation_id, "binding": self._self_binding,
        }
        try:
            self._store.confirm_scope(import_id, scope)
        except JoiningStoreError as exc:
            raise JoiningError(str(exc)) from None

    def consent(self, import_id: str, *, approve: bool) -> dict:
        """Only after host approval is observed for the EXACT confirmed
        binding does this sign and send a durable Ed25519 consent receipt,
        matching the accepted lab contract byte-for-byte (same canonical
        encoding and purpose string), so a real host can verify it without
        knowing this is a reimplementation.

        The receipt+signature are persisted ATOMICALLY BEFORE being sent
        (state 'consent_pending_delivery', distinct from the acknowledged
        'consent_given'): a lost ack never loses the receipt, a retry or a
        restart reuses the exact same bytes rather than re-signing, and once
        persisted here a later refusal attempt is rejected (R3)."""
        if type(approve) is not bool:
            raise JoiningError("invalid_decision")
        record = self._owned_import(import_id)
        if not approve:
            # No remote assertion exists for refusal in the accepted contract:
            # a refusal is a local decision only. It never signs anything.
            # Once a consent receipt has been persisted (pending_delivery or
            # given) the state is no longer 'redeemed'/'refused', so this is
            # always rejected — a transmitted (or even just durably signed)
            # receipt can never become a local "refused" (R3).
            if record["state"] not in ("redeemed", "refused"):
                raise JoiningError("import_state_conflict")
            self._store.mark_refused(import_id)
            return {"import_id": import_id, "status": "refused"}
        if record["state"] not in ("redeemed", "consent_pending_delivery", "consent_given"):
            raise JoiningError("redeem_required")
        confirmed = self._store.confirmed_scope(import_id)
        if confirmed is None:
            raise JoiningError("owner_scope_required")
        status = self._call({"op": "status", "invite_id": record["invite_id"]})
        if set(status) != {"status", "scope"}:
            raise JoiningError("control_unavailable")
        if status["status"] not in ("consent_required", "ready"):
            raise JoiningError("host_approval_required")
        expected_scope = dict(confirmed, invite_id=record["invite_id"])
        if status["scope"] != expected_scope:
            raise JoiningError("consent_scope_conflict")
        receipt = dict(expected_scope, purpose="joining-owner-consent-v1")
        existing = self._store.get_consent(import_id)
        if existing is not None and existing["receipt"] != receipt:
            raise JoiningError("consent_scope_conflict")
        signature = existing["signature"] if existing is not None else self._consent_key.sign(receipt)
        # Durable persistence BEFORE the send, every time (idempotent: a
        # retry with the exact same receipt/signature is a no-op here).
        try:
            self._store.begin_consent(import_id, receipt=receipt, signature=signature)
        except JoiningStoreError as exc:
            raise JoiningError(str(exc)) from None
        response = self._call({
            "op": "joining_consent", "invite_id": record["invite_id"],
            "receipt": receipt, "signature": signature,
        })
        if response.get("status") != "ready":
            raise JoiningError("control_unavailable")
        self._store.mark_consent_delivered(import_id)
        return {"import_id": import_id, "status": "ready"}
