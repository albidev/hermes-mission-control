"""Canonical six-field peer binding shared by host and joining registries."""
from __future__ import annotations

import re
from dataclasses import dataclass

_FIELDS = ("endpoint_id", "fingerprint", "target_install_id", "target_profile", "catalog_digest", "policy_digest")
_PATTERNS = {
    "endpoint_id": r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}",
    "fingerprint": r"[0-9a-f]{64}",
    "target_install_id": r"install:[0-9a-f]{32}",
    "target_profile": r"[a-z0-9][a-z0-9_-]{0,63}",
    "catalog_digest": r"[0-9a-f]{64}",
    "policy_digest": r"[0-9a-f]{64}",
}


class PeerBindingError(ValueError):
    """Stable validation failure without echoing untrusted values."""


@dataclass(frozen=True)
class PeerBinding:
    endpoint_id: str
    fingerprint: str
    target_install_id: str
    target_profile: str
    catalog_digest: str
    policy_digest: str

    def as_dict(self) -> dict[str, str]:
        return {name: getattr(self, name) for name in _FIELDS}


def validate_peer_binding(value: dict) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != set(_FIELDS):
        raise PeerBindingError("invalid_peer_binding")
    for name in _FIELDS:
        if not isinstance(value[name], str) or not re.fullmatch(_PATTERNS[name], value[name]):
            raise PeerBindingError("invalid_peer_binding")
    return {name: value[name] for name in _FIELDS}


def peer_binding(**kwargs) -> PeerBinding:
    return PeerBinding(**validate_peer_binding(kwargs))
