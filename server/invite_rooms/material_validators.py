"""Authoritative validation for persisted material scopes.

This module deliberately has no dependency on the registry or receipt store so both
boundaries use exactly the same grammar.
"""
from __future__ import annotations

import re
from typing import Mapping

SCOPE_KEYS = frozenset({
    "owner_installation_id", "owner_profile", "lobby_id", "planned_room_id",
    "member_id", "invite_id", "peer_id", "endpoint_id", "fingerprint",
    "target_install_id", "target_profile", "catalog_digest", "policy_digest",
    "pairing_generation",
})
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_PROFILE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
_INSTALL = re.compile(r"install:[0-9a-f]{32}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")

class MaterialScopeValidationError(ValueError):
    """A scope does not satisfy the native material grammar."""


def validate_scope_dict(value: Mapping[str, object]) -> dict:
    """Validate and return a detached canonical scope dictionary.

    Values must be an exact built-in dict with canonical built-in field types;
    subclasses are rejected rather than normalized before validation.
    """
    if type(value) is not dict or set(value) != SCOPE_KEYS:
        raise MaterialScopeValidationError("invalid_material_scope")
    result = dict(value)
    for field in ("owner_installation_id", "target_install_id"):
        if type(result[field]) is not str or _INSTALL.fullmatch(result[field]) is None:
            raise MaterialScopeValidationError("invalid_material_scope")
    for field in ("owner_profile", "target_profile"):
        if type(result[field]) is not str or _PROFILE.fullmatch(result[field]) is None:
            raise MaterialScopeValidationError("invalid_material_scope")
    for field in ("lobby_id", "planned_room_id", "member_id", "invite_id", "peer_id", "endpoint_id"):
        if type(result[field]) is not str or _TOKEN.fullmatch(result[field]) is None:
            raise MaterialScopeValidationError("invalid_material_scope")
    for field in ("fingerprint", "catalog_digest", "policy_digest"):
        if type(result[field]) is not str or _DIGEST.fullmatch(result[field]) is None:
            raise MaterialScopeValidationError("invalid_material_scope")
    generation = result["pairing_generation"]
    if type(generation) is not int or generation < 1:
        raise MaterialScopeValidationError("invalid_material_scope")
    return result


def validate_scope_attributes(scope: object, *, generation: int, profile: str) -> dict:
    """Validate native scope serialization and bind all canonical attributes."""
    try:
        value = scope.as_dict()
    except AttributeError:
        raise MaterialScopeValidationError("invalid_material_scope") from None
    result = validate_scope_dict(value)
    if type(generation) is not int or generation < 1 or type(profile) is not str:
        raise MaterialScopeValidationError("invalid_material_scope")
    if (type(result["pairing_generation"]) is not type(generation)
            or result["pairing_generation"] != generation
            or type(result["target_profile"]) is not type(profile)
            or result["target_profile"] != profile):
        raise MaterialScopeValidationError("invalid_material_scope")
    for field in SCOPE_KEYS:
        attribute = getattr(scope, field, object())
        if type(attribute) is not type(result[field]) or attribute != result[field]:
            raise MaterialScopeValidationError("invalid_material_scope")
    return result
