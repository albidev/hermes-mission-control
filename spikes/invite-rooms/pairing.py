"""Owner-approved binding between mTLS certificate pins and native identities."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping

from tls_stream import TLSIdentityError, certificate_fingerprint


@dataclass(frozen=True)
class PeerBinding:
    """A local approval; display labels are deliberately absent."""

    link_id: str
    peer_fingerprint: str
    native_installation_id: str
    native_profile: str

    def __post_init__(self) -> None:
        if not self.link_id or "/" in self.link_id:
            raise TLSIdentityError("invalid link binding")
        if len(self.peer_fingerprint) != 64 or any(c not in "0123456789abcdef" for c in self.peer_fingerprint.lower()):
            raise TLSIdentityError("invalid pinned fingerprint")
        if not self.native_installation_id or not self.native_installation_id.startswith("install:"):
            raise TLSIdentityError("invalid native installation identity")
        if not self.native_profile or "/" in self.native_profile:
            raise TLSIdentityError("invalid native profile")


def approve_binding(*, link_id: str, peer_certificate_der: bytes, native_identity: Mapping[str, str], profile: str) -> PeerBinding:
    """Create a binding only from the actual certificate and native probe output."""
    installation_id = native_identity.get("install_id")
    if not isinstance(installation_id, str):
        raise TLSIdentityError("native identity probe did not return installation id")
    return PeerBinding(link_id, certificate_fingerprint(peer_certificate_der), installation_id, profile)


def verify_binding(
    binding: PeerBinding,
    *,
    peer_certificate_der: bytes,
    native_identity_probe: Callable[[], Mapping[str, str]],
    profile: str,
) -> None:
    """Fail closed unless both the TLS pin and independently probed identity match."""
    actual_fingerprint = certificate_fingerprint(peer_certificate_der)
    if actual_fingerprint != binding.peer_fingerprint:
        raise TLSIdentityError("peer certificate rotation is not approved")
    identity = native_identity_probe()
    if identity.get("install_id") != binding.native_installation_id:
        raise TLSIdentityError("certificate is bound to a different native installation")
    if profile != binding.native_profile:
        raise TLSIdentityError("peer profile does not match approved binding")


def verify_peer_or_raise(binding: PeerBinding, *, peer_certificate_der: bytes, native_identity: Mapping[str, str], profile: str) -> None:
    """Convenience for callers that already performed an independent native probe."""
    verify_binding(
        binding,
        peer_certificate_der=peer_certificate_der,
        native_identity_probe=lambda: native_identity,
        profile=profile,
    )
