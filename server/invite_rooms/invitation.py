"""Local, network-free invitation parsing and the joining owner's approved-host
registry.

`parse_invitation` never performs navigation/fetch: it only inspects the text
against a pinned, owner-configured origin (constructor-supplied, never the
lab's hardcoded ``https://rooms.lab``). It never consumes the invitation and
never returns the secret to a caller that only wants structural validation —
the secret is returned once, to the one caller (joining_store.create_import)
that persists it for a subsequent explicit redeem().

`ApprovedHostRegistry` holds only the identity/policy binding of hosts the
joining owner has pre-approved out-of-band. An invitation can never supply an
endpoint, fingerprint, policy or grant: those values only ever come from this
registry, keyed by a peer_id the integration selects — never from parsed
invitation text.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from invite_rooms.peer_binding import PeerBindingError, validate_peer_binding

_LOBBY_ID = re.compile(r"lobby-[a-zA-Z0-9-]{1,64}")
_INVITE_ID = re.compile(r"invite-[a-zA-Z0-9-]{1,64}")
_SECRET = re.compile(r"[A-Za-z0-9_-]{43}")
_PATH = re.compile(r"/invite/(lobby-[a-zA-Z0-9-]{1,64})/(invite-[a-zA-Z0-9-]{1,64})")

_TOKEN = r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}"
_PROFILE = r"[a-z0-9][a-z0-9_-]{0,63}"
_INSTALL = r"install:[0-9a-f]{32}"
_FINGERPRINT = r"[0-9a-f]{64}"
_DIGEST = r"[0-9a-f]{64}"

MAX_INVITATION_LENGTH = 512


class InvitationError(ValueError):
    """Machine code only: never interpolates remote values or secrets."""


@dataclass(frozen=True)
class ParsedInvitation:
    """Result of local, non-consuming parsing. The secret is present exactly
    once here so the caller can hand it to joining_store.create_import; it is
    never echoed by __repr__/__str__ and callers must not log this object."""
    lobby_id: str
    invite_id: str
    secret: str = field(repr=False)


def parse_invitation(text: str, *, origin: str) -> ParsedInvitation:
    """Parse WITHOUT any network/navigation/fetch; never consumes the invitation.

    `origin` is the owner-configured pinned scheme+netloc (e.g.
    "https://peer-a.example"); it is never derived from the invitation text
    itself. Exact match only: no query string, no userinfo, no control chars,
    no alternate casing tricks via percent-encoding left unresolved (urlsplit
    leaves percent-encoding intact so the literal comparison below cannot be
    bypassed by encoding '/' or '@').
    """
    if not isinstance(text, str) or not 0 < len(text) <= MAX_INVITATION_LENGTH:
        raise InvitationError("invalid_invitation")
    if any(ord(ch) < 33 or ord(ch) == 127 for ch in text):
        raise InvitationError("invalid_invitation")
    if not isinstance(origin, str) or not origin:
        raise InvitationError("invalid_invitation_origin")
    try:
        pinned = urlsplit(origin)
        if (pinned.scheme != "https" or not pinned.netloc or pinned.username or pinned.password
                or pinned.path or pinned.query or pinned.fragment):
            raise InvitationError("invalid_invitation_origin")
    except ValueError:
        raise InvitationError("invalid_invitation_origin") from None
    try:
        parsed = urlsplit(text)
    except ValueError:
        raise InvitationError("invalid_invitation") from None
    match = _PATH.fullmatch(parsed.path or "")
    valid = (
        parsed.scheme == pinned.scheme
        and parsed.netloc == pinned.netloc
        and not parsed.username
        and not parsed.password
        and not parsed.query
        and match is not None
        and _SECRET.fullmatch(parsed.fragment or "") is not None
    )
    if not valid:
        raise InvitationError("invalid_invitation")
    assert match is not None
    return ParsedInvitation(lobby_id=match[1], invite_id=match[2], secret=parsed.fragment)


def _check(value, pattern, label) -> None:
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise InvitationError(f"invalid_{label}")


@dataclass(frozen=True)
class HostBinding:
    """Public, redacted identity of one pre-approved host peer."""
    peer_id: str
    installation_id: str
    profile: str
    fingerprint: str
    policy_digest: str
    catalog_digest: str

    @property
    def binding(self) -> dict:
        return validate_peer_binding({
            "endpoint_id": self.peer_id,
            "fingerprint": self.fingerprint,
            "target_install_id": self.installation_id,
            "target_profile": self.profile,
            "catalog_digest": self.catalog_digest,
            "policy_digest": self.policy_digest,
        })


class ApprovedHostRegistry:
    """Installation-local, in-memory set of hosts the joining owner has
    pre-approved out-of-band. Never trusts a browser/invitation-supplied
    endpoint, fingerprint, policy or grant: those always come from here."""

    def __init__(self):
        self._hosts: dict[str, HostBinding] = {}

    def register(self, *, peer_id: str, installation_id: str, profile: str,
                 fingerprint: str, policy_digest: str, catalog_digest: str) -> None:
        _check(peer_id, _TOKEN, "peer_id")
        try:
            validate_peer_binding({"endpoint_id": peer_id, "fingerprint": fingerprint,
                                   "target_install_id": installation_id,
                                   "target_profile": profile, "catalog_digest": catalog_digest,
                                   "policy_digest": policy_digest})
        except PeerBindingError:
            raise InvitationError("invalid_host_binding") from None
        if peer_id in self._hosts:
            raise InvitationError("host_already_registered")
        self._hosts[peer_id] = HostBinding(
            peer_id=peer_id, installation_id=installation_id, profile=profile,
            fingerprint=fingerprint, policy_digest=policy_digest, catalog_digest=catalog_digest,
        )

    def binding_for(self, peer_id: str) -> HostBinding:
        _check(peer_id, _TOKEN, "peer_id")
        entry = self._hosts.get(peer_id)
        if entry is None:
            raise InvitationError("unknown_host")
        return entry
