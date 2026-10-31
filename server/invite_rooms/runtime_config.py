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
import ipaddress
import json
import logging
import os
import re
import sqlite3
import stat
import threading
from urllib.parse import urlsplit
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Optional

from invite_rooms.guest import GuestRoomService
from invite_rooms.joining_store import JoiningStore
from invite_rooms import private_fs
from invite_rooms.peer_registry import PeerRegistry, PeerRegistryError, RegistrySource
from invite_rooms.material_receipts import MaterialReceiptStore, MaterialReceiptError
from invite_rooms.pairing_journal import Journal, JournalLinkAuthority
from invite_rooms.service import InviteRoomsService, OwnerContext, ServiceError, _INSTALL_PATTERN, _PROFILE_PATTERN
from invite_rooms.consent import ConsentKey
from room_invite_store import InviteError, RoomInviteStore

_CONFIG_ENV = "MISSION_CONTROL_ROOM_INVITES_CONFIG"
_TOP_LEVEL_FIELDS = {"store_path", "installation_id", "profile", "peer_registry_path", "native_rpc", "joining", "pairing", "target_http"}
_TARGET_HTTP_FIELDS = {"base_url", "profile"}
_JOINING_FIELDS = {"store_dir", "host_label", "own_bot_id", "identity_dir", "hosts_path", "origins", "control_endpoints"}
_CONTROL_ENDPOINT_FIELDS = {"host", "port"}
_CONTROL_ENDPOINT_HOSTS = frozenset({"127.0.0.1", "::1"})
_PAIRING_FIELDS = {"identity_dir", "journal_dir", "peers_path"}
_PAIRING_OPTIONAL_FIELDS = {"control_listen", "control_endpoints"}
_PEER_ID_KEY = re.compile(r"peer-[0-9a-f]{32}\Z")
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


@dataclass(frozen=True)
class TargetHttpConfig:
    """Validated, owner-private destination for the MC run client."""
    base_url: str
    profile: str

    def __post_init__(self) -> None:
        _validate_target_values(self.base_url, self.profile)


def _validate_target_values(base_url: str, profile: str) -> str:
    if not isinstance(base_url, str) or not isinstance(profile, str) or not _PROFILE_PATTERN.fullmatch(profile):
        raise RuntimeConfigError("invalid_target_http")
    if any(ord(char) < 0x20 or ord(char) == 0x7f for char in base_url) or "?" in base_url or "#" in base_url:
        raise RuntimeConfigError("invalid_target_http")
    try:
        parsed = urlsplit(base_url)
        if parsed.scheme != "http" or parsed.username is not None or parsed.password is not None:
            raise ValueError
        if parsed.path or parsed.query or parsed.fragment or not parsed.netloc:
            raise ValueError
        if parsed.hostname not in {"127.0.0.1", "::1"}:
            raise ValueError
        if parsed.port is None or type(parsed.port) is not int or not 1 <= parsed.port <= 65535:
            raise ValueError
        expected = f"http://127.0.0.1:{parsed.port}" if parsed.hostname == "127.0.0.1" else f"http://[::1]:{parsed.port}"
        if base_url != expected:
            raise ValueError
    except (ValueError, TypeError):
        raise RuntimeConfigError("invalid_target_http") from None
    return base_url


def parse_target_http(raw) -> Optional[TargetHttpConfig]:
    """Parse the exact target block; invalid input raises a sanitized error.

    Only explicit-port HTTP loopback authorities are accepted.  The returned URL
    is canonicalized from the already-validated authority and can never carry a
    request-selected path, query, fragment, userinfo or alternate host spelling.
    """
    if not isinstance(raw, dict) or set(raw) != _TARGET_HTTP_FIELDS:
        raise RuntimeConfigError("invalid_target_http")
    base_url, profile = raw["base_url"], raw["profile"]
    return TargetHttpConfig(base_url=_validate_target_values(base_url, profile), profile=profile)


def parse_control_endpoints(raw) -> dict:
    """Validate ``joining.control_endpoints``: ``{peer_id: {"host": literal loopback IP, "port": 1..65535}}``.

    Owner-private DESTINATION only.  It never carries identity, fingerprint, generation, CA or authorization, and it
    is deliberately independent of ``joining.origins`` (the HTTPS invitation-origin pin).  Raises
    ``RuntimeConfigError`` with a machine code on any deviation; returns ``{peer_id: (host, port)}``.
    """
    if not isinstance(raw, dict) or not raw:
        raise RuntimeConfigError("invalid_control_endpoints")
    result = {}
    for peer_id, entry in raw.items():
        if not isinstance(peer_id, str) or not peer_id or not isinstance(entry, dict) or set(entry) != _CONTROL_ENDPOINT_FIELDS:
            raise RuntimeConfigError("invalid_control_endpoints")
        host, port = entry["host"], entry["port"]
        if not isinstance(host, str) or type(port) is not int or not 0 < port <= 65535:
            raise RuntimeConfigError("invalid_control_endpoints")
        # Exact string membership BEFORE any address normalisation: only the two approved loopback literals (no
        # other 127/8 address, scope id, alternate/padded/mapped spelling, hostname or non-loopback address).
        if host not in _CONTROL_ENDPOINT_HOSTS:
            raise RuntimeConfigError("invalid_control_endpoints")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            raise RuntimeConfigError("invalid_control_endpoints") from None
        if not address.is_loopback or str(address) != host:
            raise RuntimeConfigError("invalid_control_endpoints")
        result[peer_id] = (host, port)
    return result


def parse_control_listen(raw) -> tuple:
    """Validate ``pairing.control_listen``: ``{"host": "127.0.0.1" | "::1", "port": 1..65535}`` -> ``(host, port)``.

    Owner-private LOCAL BIND only (distinct from the remote client map ``joining.control_endpoints``).  Same exact
    literal-loopback rules as the client map: no hostname, wildcard, other 127/8 spelling, bool/float/string/zero port.
    """
    if not isinstance(raw, dict):
        raise RuntimeConfigError("invalid_control_listen")
    try:
        return parse_control_endpoints({"listen": raw})["listen"]
    except RuntimeConfigError:
        raise RuntimeConfigError("invalid_control_listen") from None


def parse_pairing_control_endpoints(raw) -> Mapping:
    """Validate ``pairing.control_endpoints``: ``{peer_id: {"host": literal loopback, "port": 1..65535}}``.

    Owner-private OUTBOUND destination map of THIS process (where it dials each journal peer's control listener).  It
    is never a bind (``pairing.control_listen``), never carries identity/credential/trust fields, and its keys must be
    journal-shaped peer ids.  Returns a read-only ``{peer_id: (host, port)}``.
    """
    try:
        parsed = parse_control_endpoints(raw)
    except RuntimeConfigError:
        raise RuntimeConfigError("invalid_pairing_control_endpoints") from None
    if not all(_PEER_ID_KEY.fullmatch(peer_id) for peer_id in parsed):
        raise RuntimeConfigError("invalid_pairing_control_endpoints")
    return MappingProxyType(dict(parsed))


def _no_duplicate_keys(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate")
        out[key] = value
    return out


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
    target_http: Optional[TargetHttpConfig] = None
    joining_store_dir: Optional[Path] = None
    joining_host_label: str = "host"
    joining_own_bot_id: Optional[str] = None
    pairing: Optional["PairingConfig"] = None
    joining_owner: Optional[object] = None


@dataclass(frozen=True)
class PairingConfig:
    identity_dir: Path
    journal_dir: Path
    journal_anchor: Path
    peers_path: Path
    control_listen: Optional[tuple] = None  # (host, port) owner-private bind for the W2 control listener, or None
    control_endpoints: Optional[Mapping] = None  # {peer_id: (host, port)} owner-private OUTBOUND destinations, or None


def _parse_pairing(raw, peer_registry_path: Path) -> Optional[PairingConfig]:
    """Validate the optional ``pairing`` block.  Any problem -> ``None`` (pairing routes 404), never a partial feature.

    Directories that already exist must be owner-private (0700, owner UID, no symlink component below the nearest
    existing ancestor); ones that do not exist yet are created 0700 on first use.  ``peers_path`` must be the very
    registry file the sidecar reads (``peer_registry_path``): the journal is its only writer.
    """
    if not isinstance(raw, dict) or not _PAIRING_FIELDS <= set(raw) or set(raw) - _PAIRING_FIELDS - _PAIRING_OPTIONAL_FIELDS:
        return None
    values = []
    for key in ("identity_dir", "journal_dir", "peers_path"):
        value = raw[key]
        if not isinstance(value, str) or not value or "\x00" in value or not os.path.isabs(value):
            return None
        values.append(Path(os.path.abspath(value)))
    identity_dir, journal_dir, peers_path = values
    control_listen = parse_control_listen(raw["control_listen"]) if "control_listen" in raw else None
    control_endpoints = (parse_pairing_control_endpoints(raw["control_endpoints"])
                         if "control_endpoints" in raw else None)
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
    # The journal and invite store are deliberately allowed to be siblings.  Validate the
    # journal's nearest existing owner-private parent here, then retain that exact anchor for
    # runtime construction.  Passing the store parent would reject the documented sibling layout;
    # falling back to a broad ancestor would weaken private_fs confinement.
    journal_anchor = private_fs.nearest_existing_parent(journal_dir)
    try:
        # The selected anchor is the trust boundary.  Validate that directory itself without
        # implicitly validating its parent (which may be a shared, non-private directory).
        os.close(private_fs.open_private_dir(journal_anchor, journal_anchor, create=False))
    except private_fs.PrivateStorageError:
        return None
    return PairingConfig(identity_dir=identity_dir, journal_dir=journal_dir,
                         journal_anchor=journal_anchor, peers_path=peers_path, control_listen=control_listen,
                         control_endpoints=control_endpoints)


def _load_config_file(raw_path: str) -> Optional[RuntimeConfig]:
    path = Path(raw_path)
    if not _is_private_file(path):
        _warn_once("perm", "config path missing or not mode 0600 with a 0700 parent directory")
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_no_duplicate_keys)
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
        target_http = parse_target_http(data["target_http"]) if "target_http" in data else None
        joining_raw = data.get("joining")
        joining_store_dir = None
        joining_host_label = "host"
        joining_own_bot_id = None
        if joining_raw is not None:
            if not isinstance(joining_raw, dict) or not {"store_dir"} <= set(joining_raw) or set(joining_raw) - _JOINING_FIELDS:
                raise RuntimeConfigError("invalid_joining")
            if not isinstance(joining_raw["store_dir"], str) or not joining_raw["store_dir"]:
                raise RuntimeConfigError("invalid_joining")
            joining_store_dir = Path(joining_raw["store_dir"])
            if "host_label" in joining_raw and not isinstance(joining_raw["host_label"], str):
                raise RuntimeConfigError("invalid_joining")
            if "own_bot_id" in joining_raw and not isinstance(joining_raw["own_bot_id"], str):
                raise RuntimeConfigError("invalid_joining")
            joining_host_label = joining_raw.get("host_label", "host")
            joining_own_bot_id = joining_raw.get("own_bot_id")
            if "control_endpoints" in joining_raw:
                parse_control_endpoints(joining_raw["control_endpoints"])

        if not _is_private_file(peer_registry_path):
            _warn_once("peer-registry-perm", "peer registry path missing or not mode 0600 with a 0700 parent directory")
            return None
        pairing = None
        if isinstance(data.get("pairing"), dict) and ("control_listen" in data["pairing"]
                                                      or "control_endpoints" in data["pairing"]):
            # Bind / destination field present: strict. Bad value or duplicate keys anywhere reject the WHOLE
            # config (never a half-enabled listener or sender).
            if "control_listen" in data["pairing"]:
                parse_control_listen(data["pairing"]["control_listen"])
            if "control_endpoints" in data["pairing"]:
                parse_pairing_control_endpoints(data["pairing"]["control_endpoints"])

        if "pairing" in data:
            pairing = _parse_pairing(data["pairing"], peer_registry_path)
            if pairing is None:
                _warn_once("pairing", "pairing block is invalid; pairing routes are disabled")
        joining_owner_config = data.get("joining")
        return RuntimeConfig(store_path=store_path, installation_id=installation_id, profile=profile,
                              peer_registry_path=peer_registry_path, native_rpc=native_rpc, target_http=target_http,
                              joining_store_dir=joining_store_dir, joining_host_label=joining_host_label,
                              joining_own_bot_id=joining_own_bot_id, pairing=pairing, joining_owner=joining_owner_config)
    except RuntimeConfigError:
        _warn_once("values", "config file contains invalid owner identity, store, peer registry or native RPC values")
        return None


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


def _scoped_route_provider(scoped_adapter_registry):
    """Return the IPv4 route lookup for one caller-owned A1 registry.

    The registry is deliberately validated by concrete type here, before any
    method on the supplied object can run.  The loader never constructs,
    starts, joins, or closes this caller-owned resource.
    """
    from invite_rooms.control.scoped_registry import ScopedAdapterRegistry

    if type(scoped_adapter_registry) is not ScopedAdapterRegistry:
        raise RuntimeConfigError("invalid_scoped_adapter_registry")

    def route_provider(scope):
        return scoped_adapter_registry.port_for(scope)

    return route_provider


def load_service(env: Optional[dict] = None, *, material_resolver=None, scoped_adapter_registry=None):
    """Resolve the full owner-private configuration into a ready InviteRoomsService.

    Returns ``(service, ctx, config)`` or ``None`` if the feature is disabled or
    misconfigured in any way. The caller must treat ``None`` as "route does not
    exist" (same HTTP response as an unknown path) -- never as a distinguishable
    error. ``service`` is always built with ``rpc=None``; the (optional) native
    RPC connection is established per-activation by the routes layer so a
    construction-time singleton never leaks across concurrent requests.

    ``scoped_adapter_registry`` is an optional explicit injection of one
    already-running, concrete A1 registry owned by the caller.  It is used
    only as the PeerRegistry route provider; this loader never constructs,
    starts, joins, or closes it.  Invalid injected objects fail closed before
    any provider method is invoked.

    ``material_resolver`` is the explicit seam for the joining-owner material
    channel (P2b): this module never invents a config-file format for it and
    never wires one itself -- by default it stays ``None``, which makes any
    activation that actually needs remote peer material fail closed with
    ``ServiceError("material_channel_unavailable")`` before any native RPC
    call. The caller (routes layer today, P2b's integration tomorrow) is the
    only place allowed to pass a real resolver callable through.
    """
    import os

    route_provider = None
    if scoped_adapter_registry is not None:
        try:
            route_provider = _scoped_route_provider(scoped_adapter_registry)
        except RuntimeConfigError:
            _warn_once("scoped-registry", "scoped adapter registry injection is invalid")
            return None

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
        # An absent injection must preserve the registry's construction-time
        # provider.  The explicit A1 injection is the only authority allowed
        # to replace it; ``None`` means keep the default fail-closed state (or
        # a caller-installed builder provider), not erase it.
        if route_provider is not None:
            registry._route_provider = route_provider
        # Receipt admission is enabled only when the owner has supplied the validated pairing
        # configuration.  The journal is authoritative; a JSON-only registry is never a production
        # fallback because it cannot serialize a post-fsync unpair cutover.
        if config.pairing is not None:
            journal = Journal(config.pairing.journal_dir, anchor=config.pairing.journal_anchor)
            registry._authority = JournalLinkAuthority(journal)
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


def load_guest_service(env: Optional[dict] = None, *, channel_factory=None, rpc_factory=None,
                       approval_factory=None):
    """Build per request from validated config, without stale transport globals.

    The sidecar supplies the factories. No host connector yet means an offline
    host, not a disabled local guest view. Native sockets are acquired lazily by
    the sidecar's RPC callable, never in construction or for read-only listing.
    """
    loaded = load_service(env)
    if loaded is None:
        return None
    _service, _ctx, config = loaded
    if config.joining_store_dir is None:
        return None
    try:
        joining = JoiningStore(config.joining_store_dir)
        channel = channel_factory(config) if channel_factory is not None else None
        rpc = rpc_factory(config) if rpc_factory is not None else None
        approvals = approval_factory(config) if approval_factory is not None else None
        def peer_check(peer_id, host_id):
            # Reload the authoritative private registry after every native await.
            from dataclasses import asdict
            peer = build_peer_registry(config.peer_registry_path).lookup(peer_id)
            if peer.installation_id != host_id:
                raise ValueError('peer_binding_mismatch')
            value = asdict(peer)
            value['consent_public_key'] = peer.consent_public_key.hex()
            return value
        return GuestRoomService(joining, channel=channel, rpc=rpc, approvals=approvals,
                                peer_check=peer_check, local_installation_id=config.installation_id,
                                host_label=config.joining_host_label, own_bot_id=config.joining_own_bot_id)
    except Exception:
        _warn_once("joining", "guest joining configuration is unavailable")
        return None
