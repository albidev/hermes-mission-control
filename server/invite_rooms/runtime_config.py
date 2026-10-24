"""Owner-private runtime configuration for the invite-rooms HTTP routes (MVP P2a).

The feature is OFF by default. It is enabled only by pointing
``MISSION_CONTROL_ROOM_INVITES_CONFIG`` at one owner-private JSON file that
names: the local invite store path, the trusted owner installation/profile,
an owner-private peer-registry source file, and (optionally) a native RPC
endpoint + token file reference. Every referenced secret-bearing file must be
mode 0600 with a 0700 parent directory, or the whole feature is treated as
misconfigured.

Misconfiguration (missing env var, missing/world-readable file, invalid JSON,
unknown peer data, a store that can't be opened, ...) is deliberately
indistinguishable from "feature not present" to any HTTP caller: this module
only ever returns ``None`` on failure, after logging a SANITIZED warning once
per distinct cause. No path, secret, or raw config value is ever logged.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import sqlite3
import stat
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from invite_rooms import private_fs
from invite_rooms.peer_registry import PeerRegistry, PeerRegistryError, RegistrySource
from invite_rooms.material_receipts import MaterialReceiptStore, MaterialReceiptError
from invite_rooms.service import InviteRoomsService, OwnerContext, ServiceError, _INSTALL_PATTERN, _PROFILE_PATTERN
from room_invite_store import InviteError, RoomInviteStore

_CONFIG_ENV = "MISSION_CONTROL_ROOM_INVITES_CONFIG"
_TOP_LEVEL_FIELDS = {"store_path", "installation_id", "profile", "peer_registry_path", "native_rpc", "pairing"}
_PAIRING_FIELDS = {"identity_dir", "journal_dir", "peers_path"}
_PEER_FIELDS = {"peer_id", "installation_id", "profile", "fingerprint", "endpoint_id", "catalog", "consent_public_key_b64", "tls_ca_pem", "pairing_generation"}
_NATIVE_RPC_FIELDS = {"endpoint", "token_path"}

_LOG = logging.getLogger("invite_rooms.runtime_config")
_warn_lock = threading.Lock()
_warned_once: set[str] = set()


def _warn_once(key: str, message: str) -> None:
    """Log a sanitized, machine-code-only warning at most once per cause."""
    with _warn_lock:
        if key in _warned_once:
            return
        _warned_once.add(key)
    _LOG.warning("room-invites feature disabled: %s", message)


class RuntimeConfigError(ValueError):
    """Machine code only: never interpolates remote values, paths or secrets."""


def _is_private_file(path: Path) -> bool:
    """True iff ``path`` is a regular file, mode 0600, with a 0700 parent dir."""
    try:
        file_stat = path.stat()
        parent_stat = path.parent.stat()
    except OSError:
        return False
    if not stat.S_ISREG(file_stat.st_mode) or stat.S_IMODE(file_stat.st_mode) != 0o600:
        return False
    if not stat.S_ISDIR(parent_stat.st_mode) or stat.S_IMODE(parent_stat.st_mode) != 0o700:
        return False
    return True


@dataclass(frozen=True)
class RuntimeConfig:
    store_path: Path
    installation_id: str
    profile: str
    peer_registry_path: Path
    native_rpc: Optional[dict]  # {"endpoint": str, "token_path": Path} or None
    pairing: Optional["PairingConfig"] = None  # P3b.1a-2: OFF unless a valid ``pairing`` block is present


@dataclass(frozen=True)
class PairingConfig:
    identity_dir: Path
    journal_dir: Path
    peers_path: Path


def _parse_pairing(raw, peer_registry_path: Path) -> Optional[PairingConfig]:
    """Validate the optional ``pairing`` block.  Any problem -> ``None`` (pairing routes 404), never a partial feature.

    Directories that already exist must be owner-private (0700, owner UID, no symlink component below the nearest
    existing ancestor); ones that do not exist yet are created 0700 on first use.  ``peers_path`` must be the very
    registry file the sidecar reads (``peer_registry_path``): the journal is its only writer.
    """
    if not isinstance(raw, dict) or set(raw) != _PAIRING_FIELDS:
        return None
    values = []
    for key in ("identity_dir", "journal_dir", "peers_path"):
        value = raw[key]
        if not isinstance(value, str) or not value or "\x00" in value or not os.path.isabs(value):
            return None
        values.append(Path(os.path.abspath(value)))
    identity_dir, journal_dir, peers_path = values
    if peers_path != Path(os.path.abspath(peer_registry_path)):
        return None
    if identity_dir == journal_dir or peers_path.parent in (identity_dir, journal_dir):
        return None
    for directory in (identity_dir, journal_dir):
        if os.path.lexists(directory):
            try:
                os.close(private_fs.open_private_dir(directory, None, create=False))
            except private_fs.PrivateStorageError:
                return None
    return PairingConfig(identity_dir=identity_dir, journal_dir=journal_dir, peers_path=peers_path)


def _load_config_file(raw_path: str) -> Optional[RuntimeConfig]:
    path = Path(raw_path)
    if not _is_private_file(path):
        _warn_once("perm", "config path missing or not mode 0600 with a 0700 parent directory")
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        _warn_once("parse", "config file is unreadable or not valid JSON")
        return None
    if not isinstance(data, dict) or set(data) - _TOP_LEVEL_FIELDS or not {"store_path", "installation_id", "profile", "peer_registry_path"} <= set(data):
        _warn_once("schema", "config file has an invalid or incomplete shape")
        return None
    try:
        installation_id = data["installation_id"]
        profile = data["profile"]
        if not isinstance(installation_id, str) or not _INSTALL_PATTERN.fullmatch(installation_id):
            raise RuntimeConfigError("invalid_installation_id")
        if not isinstance(profile, str) or not _PROFILE_PATTERN.fullmatch(profile):
            raise RuntimeConfigError("invalid_profile")
        if not isinstance(data["store_path"], str) or not data["store_path"]:
            raise RuntimeConfigError("invalid_store_path")
        if not isinstance(data["peer_registry_path"], str) or not data["peer_registry_path"]:
            raise RuntimeConfigError("invalid_peer_registry_path")
        store_path = Path(data["store_path"])
        peer_registry_path = Path(data["peer_registry_path"])
        native_rpc_raw = data.get("native_rpc")
        native_rpc: Optional[dict] = None
        if native_rpc_raw is not None:
            if not isinstance(native_rpc_raw, dict) or set(native_rpc_raw) != _NATIVE_RPC_FIELDS:
                raise RuntimeConfigError("invalid_native_rpc")
            endpoint, token_path_raw = native_rpc_raw["endpoint"], native_rpc_raw["token_path"]
            if not isinstance(endpoint, str) or not (endpoint.startswith("ws://127.0.0.1:") or endpoint.startswith("ws://[::1]:")):
                raise RuntimeConfigError("invalid_native_rpc_endpoint")
            if not isinstance(token_path_raw, str) or not token_path_raw:
                raise RuntimeConfigError("invalid_native_rpc_token_path")
            token_path = Path(token_path_raw)
            if not _is_private_file(token_path):
                raise RuntimeConfigError("invalid_native_rpc_token_permissions")
            native_rpc = {"endpoint": endpoint, "token_path": token_path}
    except RuntimeConfigError:
        _warn_once("values", "config file contains invalid owner identity, store, peer registry or native RPC values")
        return None
    if not _is_private_file(peer_registry_path):
        _warn_once("peer-registry-perm", "peer registry path missing or not mode 0600 with a 0700 parent directory")
        return None
    pairing = None
    if "pairing" in data:
        pairing = _parse_pairing(data["pairing"], peer_registry_path)
        if pairing is None:
            _warn_once("pairing", "pairing block is invalid; pairing routes are disabled")
    return RuntimeConfig(store_path=store_path, installation_id=installation_id, profile=profile,
                          peer_registry_path=peer_registry_path, native_rpc=native_rpc, pairing=pairing)


def build_peer_registry(peer_registry_path: Path) -> PeerRegistry:
    """Build a PeerRegistry from an owner-private JSON file. Raises RuntimeConfigError."""
    try:
        entries = json.loads(peer_registry_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        raise RuntimeConfigError("peer_registry_unreadable") from None
    if not isinstance(entries, list):
        raise RuntimeConfigError("peer_registry_invalid") from None
    registry = PeerRegistry()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != _PEER_FIELDS:
            raise RuntimeConfigError("peer_registry_invalid") from None
        try:
            consent_b64 = entry["consent_public_key_b64"]
            if not isinstance(consent_b64, str): raise ValueError()
            consent_key = base64.b64decode(consent_b64, validate=True)
            if len(consent_key) != 32: raise ValueError()
        except (ValueError, TypeError):
            raise RuntimeConfigError("peer_registry_invalid") from None
        kwargs = {key: entry[key] for key in _PEER_FIELDS if key != "consent_public_key_b64"}
        try:
            registry.register(consent_public_key=consent_key, **kwargs)
        except (PeerRegistryError, TypeError, KeyError):
            raise RuntimeConfigError("peer_registry_invalid") from None
    return registry


def load_service(env: Optional[dict] = None, *, material_resolver=None):
    """Resolve the full owner-private configuration into a ready InviteRoomsService.

    Returns ``(service, ctx, config)`` or ``None`` if the feature is disabled or
    misconfigured in any way. The caller must treat ``None`` as "route does not
    exist" (same HTTP response as an unknown path) -- never as a distinguishable
    error. ``service`` is always built with ``rpc=None``; the (optional) native
    RPC connection is established per-activation by the routes layer so a
    construction-time singleton never leaks across concurrent requests.

    ``material_resolver`` is the explicit seam for the joining-owner material
    channel (P2b): this module never invents a config-file format for it and
    never wires one itself -- by default it stays ``None``, which makes any
    activation that actually needs remote peer material fail closed with
    ``ServiceError("material_channel_unavailable")`` before any native RPC
    call. The caller (routes layer today, P2b's integration tomorrow) is the
    only place allowed to pass a real resolver callable through.
    """
    import os

    source = env if env is not None else os.environ
    raw_path = (source.get(_CONFIG_ENV) or "").strip()
    if not raw_path:
        return None
    config = _load_config_file(raw_path)
    if config is None:
        return None
    try:
        registry = build_peer_registry(config.peer_registry_path)
        store = RoomInviteStore(config.store_path)
        # Trusted anchor = the owner-private parent of store_path (~/.config/mc-room-invites).
        receipts = MaterialReceiptStore(config.store_path.parent / "material-receipts", anchor=config.store_path.parent)
        registry._receipt_store = receipts
        # Authoritative source for the R1-05 admission seam; the loader is resolved at call time.
        registry._source = RegistrySource(config.peer_registry_path, lambda path: build_peer_registry(path))
        ctx = OwnerContext(installation_id=config.installation_id, profile=config.profile)
        service = InviteRoomsService(store, registry, trusted_owner=ctx, material_resolver=material_resolver)
    except (RuntimeConfigError, InviteError, ServiceError, PeerRegistryError, MaterialReceiptError, sqlite3.Error, OSError):
        # A store that can't be opened (corrupt file, unreadable schema row,
        # locked/foreign file bytes, ...) must behave exactly like any other
        # misconfiguration: feature off / 404, one sanitized warning, and the
        # file's bytes are never touched (RoomInviteStore raises before any
        # write is attempted against an unrecognized file).
        _warn_once("build", "service construction from config failed")
        return None
    return service, ctx, config
