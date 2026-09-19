"""Mission Control's read/write bridge for Hermes Honcho configuration.

The sidecar never acts as a Honcho client and never accepts a runtime user ID
from the browser. It only provisions the explicit *local single-user* identity
used when Mission Control runs behind its installation bearer token. Gated
Dashboard sessions keep using the server-authenticated WebSocket identity that
Hermes already passes to memory providers.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

from hermes_paths import display_home_path, hermes_root


_PROFILE_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_PEER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_HOST_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_PROFILE_MARKERS = ("config.yaml", ".env", "SOUL.md", "profile.yaml", "auth.json", "state.db")


class HonchoBridgeError(ValueError):
    def __init__(self, status_code: int, message: str, *, error: str = "honcho_config_error") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.error = error


def _default_config_path(root: Path) -> Path:
    local = root / "honcho.json"
    global_config = Path.home() / ".honcho" / "config.json"
    if local.exists() or not global_config.exists():
        return local
    return global_config


def _profile_home(root: Path, profile: str) -> Path:
    return root if profile == "default" else root / "profiles" / profile


def _profile_config_path(root: Path, profile: str) -> Path:
    if profile != "default":
        local = _profile_home(root, profile) / "honcho.json"
        if local.exists():
            return local
    return _default_config_path(root)


def _read_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HonchoBridgeError(500, f"Could not read Honcho configuration: {exc}", error="invalid_honcho_config") from exc
    if not isinstance(payload, dict):
        raise HonchoBridgeError(500, "Honcho configuration must be a JSON object.", error="invalid_honcho_config")
    return payload


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}-{time.time_ns()}")
    data = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    fd: int | None = None
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            fd = None
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except BaseException:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def _profile_names(root: Path) -> list[str]:
    profiles_dir = root / "profiles"
    try:
        entries = list(profiles_dir.iterdir())
    except OSError:
        return []
    return sorted(
        entry.name
        for entry in entries
        if entry.is_dir()
        and _PROFILE_RE.fullmatch(entry.name)
        and any((entry / marker).exists() or (entry / marker).is_symlink() for marker in _PROFILE_MARKERS)
    )


def _dotenv_value(path: Path, key: str) -> str | None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return None
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        name, separator, value = line.partition("=")
        if separator and name.strip() == key:
            resolved = value.strip()
            if len(resolved) >= 2 and resolved[0] == resolved[-1] and resolved[0] in {"'", '"'}:
                resolved = resolved[1:-1]
            return resolved or None
    return None


def _canonical_host_key(profile: str) -> str:
    if profile == "default":
        return "hermes"
    safe = "".join(character if character.isalnum() or character in "_-" else "_" for character in profile).strip("_")
    return f"hermes_{safe or 'profile'}"


def _resolved_host_key(root: Path, profile: str, config: dict[str, Any]) -> str:
    explicit = _dotenv_value(_profile_home(root, profile) / ".env", "HERMES_HONCHO_HOST")
    if explicit and _HOST_RE.fullmatch(explicit):
        return explicit
    if profile == "default":
        default_host = str(config.get("defaultHost") or "").strip()
        if default_host and _HOST_RE.fullmatch(default_host):
            return default_host
    canonical = _canonical_host_key(profile)
    hosts = config.get("hosts")
    if isinstance(hosts, dict) and canonical not in hosts and profile != "default":
        legacy = f"hermes.{profile}"
        if isinstance(hosts.get(legacy), dict):
            return legacy
    return canonical


def _host_block(config: dict[str, Any], host_key: str) -> dict[str, Any]:
    hosts = config.get("hosts")
    if not isinstance(hosts, dict):
        return {}
    block = hosts.get(host_key)
    return block if isinstance(block, dict) else {}


def _present(block: dict[str, Any], config: dict[str, Any], key: str, default: Any = None) -> Any:
    return block[key] if key in block else config.get(key, default)


def _connection(config: dict[str, Any], block: dict[str, Any]) -> tuple[bool, str | None]:
    endpoint = config.get("endpoint")
    native_url = endpoint.get("baseUrl") if isinstance(endpoint, dict) else None
    base_url = (
        block.get("baseUrl")
        or block.get("base_url")
        or native_url
        or config.get("baseUrl")
        or config.get("base_url")
    )
    # Root credentials may be intentionally shared. A named profile never
    # inherits the *default host block's* key, matching Hermes isolation.
    api_key = block.get("apiKey") or config.get("apiKey")
    return bool(api_key or base_url), str(base_url).strip() if base_url else None


def _identity(config: dict[str, Any], block: dict[str, Any]) -> tuple[str | None, bool, bool]:
    peer = _present(block, config, "peerName")
    peer_name = str(peer).strip() if peer is not None else ""
    pinned = bool(_present(block, config, "pinUserPeer", _present(block, config, "pinPeerName", False)))
    prefixed = bool(_present(block, config, "sessionAiPeerPrefix", False))
    return peer_name or None, pinned, prefixed


def _profile_status(root: Path, profile: str) -> dict[str, Any]:
    config_path = _profile_config_path(root, profile)
    config = _read_config(config_path)
    host_key = _resolved_host_key(root, profile, config)
    block = _host_block(config, host_key)
    peer_name, pinned, prefixed = _identity(config, block)
    connection_configured, _base_url = _connection(config, block)
    enabled = bool(_present(block, config, "enabled", connection_configured))
    identity_ready = bool(peer_name)
    if not enabled:
        readiness = "disabled"
    elif not connection_configured:
        readiness = "credentials-required"
    elif not identity_ready:
        readiness = "identity-required"
    else:
        readiness = "ready"
    # Core parity: `look.pick("aiPeer") or host`, i.e. the host key is the fallback.
    default_ai_peer = host_key
    ai_peer = str(_present(block, config, "aiPeer", default_ai_peer) or default_ai_peer)
    workspace = str(_present(block, config, "workspace", host_key) or host_key)
    return {
        "profile": profile,
        "host": host_key,
        "managedBy": "shared-default" if config_path == _default_config_path(root) else "profile-local",
        "enabled": enabled,
        "connectionConfigured": connection_configured,
        "identityReady": identity_ready,
        "peerPinned": pinned,
        "sessionAiPeerPrefix": prefixed,
        "aiPeer": ai_peer,
        "workspace": workspace,
        "readiness": readiness,
        "configPath": str(config_path),
    }


def _memory_provider_installed(provider: str = "honcho") -> bool:
    """Whether the memory provider is actually installed on disk.

    Mission Control must not offer setup UI for a provider that does not exist:
    the panel is gated on this. Discovery is delegated to Hermes's own memory
    plugin layer when a checkout is importable, so a provider installed via
    entry point or an extra search directory is still found. ``MISSION_CONTROL_MEMORY_PROVIDERS_DIR``
    overrides discovery for tests and unusual deployments.
    """
    override = os.environ.get("MISSION_CONTROL_MEMORY_PROVIDERS_DIR", "").strip()
    if override:
        return (Path(os.path.expanduser(override)) / provider / "__init__.py").is_file()

    core = hermes_root() / "hermes-agent"
    try:
        import sys
        if str(core) not in sys.path:
            sys.path.insert(0, str(core))
        import plugins.memory as memory_plugins
        return memory_plugins.find_provider_dir(provider) is not None
    except Exception:
        # No importable checkout: fall back to the on-disk layout.
        return (core / "plugins" / "memory" / provider / "__init__.py").is_file()


def _memory_provider_active(root: Path) -> bool:
    config_path = root / "config.yaml"
    try:
        import yaml
        payload = yaml.safe_load(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    except (OSError, UnicodeError, ValueError):
        return False
    if not isinstance(payload, dict):
        return False
    memory = payload.get("memory")
    return isinstance(memory, dict) and str(memory.get("provider") or "").strip().lower() == "honcho"


def load_honcho_status() -> dict[str, Any]:
    root = hermes_root()
    profiles = ["default", *_profile_names(root)]
    profile_rows = [_profile_status(root, profile) for profile in profiles]
    default = profile_rows[0]
    default_config_path = _profile_config_path(root, "default")
    default_config = _read_config(default_config_path)
    default_block = _host_block(default_config, default["host"])
    peer_name, pinned, prefixed = _identity(default_config, default_block)
    if peer_name and pinned:
        identity_mode = "local-single-user"
    elif peer_name:
        identity_mode = "configured-peer"
    else:
        identity_mode = "unresolved"
    return {
        "available": default_config_path.exists(),
        "configured": default["connectionConfigured"],
        "enabled": default["enabled"],
        "providerInstalled": _memory_provider_installed(),
        "providerActive": _memory_provider_active(root),
        "identityMode": identity_mode,
        "identityReady": bool(peer_name),
        "authenticatedRuntimeSupported": True,
        "peerName": peer_name,
        "workspace": default["workspace"],
        "aiPeer": default["aiPeer"],
        "sessionAiPeerPrefix": prefixed,
        "configPath": display_home_path(default_config_path),
        "profileCount": len(profile_rows),
        "profiles": profile_rows,
    }


def _configure_profile(config: dict[str, Any], root: Path, profile: str, peer: str) -> None:
    hosts = config.get("hosts")
    if hosts is None:
        hosts = {}
        config["hosts"] = hosts
    if not isinstance(hosts, dict):
        raise HonchoBridgeError(500, "Honcho hosts configuration must be a JSON object.", error="invalid_honcho_config")

    host_key = _resolved_host_key(root, profile, config)
    existing = hosts.get(host_key)
    block = dict(existing) if isinstance(existing, dict) else {}
    workspace = str(block.get("workspace") or config.get("workspace") or host_key).strip() or host_key

    config["peerName"] = peer
    config["pinUserPeer"] = True
    config["sessionAiPeerPrefix"] = True
    block["peerName"] = peer
    block["pinUserPeer"] = True
    block["sessionAiPeerPrefix"] = True
    block.setdefault("workspace", workspace)
    # Core parity: the host key is the resolved default AI peer
    # (`look.pick("aiPeer") or host`), so write it explicitly instead of relying
    # on a root-level value that a later profile could inherit.
    block.setdefault("aiPeer", host_key)
    hosts[host_key] = block


def configure_local_identity(peer_name: str) -> dict[str, Any]:
    peer = str(peer_name or "").strip()
    if not _PEER_RE.fullmatch(peer):
        raise HonchoBridgeError(
            400,
            "Peer name must be 1-64 characters and use only letters, numbers, dot, underscore, or hyphen.",
            error="invalid_peer_name",
        )

    root = hermes_root()
    profiles = ["default", *_profile_names(root)]
    default_path = _profile_config_path(root, "default")
    managed_profiles = [profile for profile in profiles if _profile_config_path(root, profile) == default_path]
    skipped_profiles = [profile for profile in profiles if profile not in managed_profiles]
    config = _read_config(default_path)
    for profile in managed_profiles:
        _configure_profile(config, root, profile, peer)

    backup: Path | None = None
    if default_path.exists():
        backup = default_path.with_name(f"{default_path.name}.bak.{time.time_ns()}")
        try:
            backup.write_bytes(default_path.read_bytes())
            os.chmod(backup, 0o600)
        except OSError as exc:
            raise HonchoBridgeError(500, f"Could not back up Honcho configuration: {exc}", error="backup_failed") from exc

    try:
        _atomic_write_json(default_path, config)
    except OSError as exc:
        raise HonchoBridgeError(500, f"Could not write Honcho configuration: {exc}", error="write_failed") from exc

    status = load_honcho_status()
    return {
        "success": True,
        "backupPath": str(backup) if backup is not None else None,
        "backupPaths": [str(backup)] if backup is not None else [],
        "configuredProfiles": managed_profiles,
        "skippedProfiles": skipped_profiles,
        **status,
    }
