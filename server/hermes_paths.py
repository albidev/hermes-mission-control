"""Centralized Hermes home / active-profile resolution for Mission Control.

All Mission Control code that touches Hermes state (state DB, sessions,
logs, skills, config, cache, vault-brain candidates, core checkout) must
resolve paths through this module — never through hardcoded ``~/.hermes``
(issue #12).

The resolution mirrors the running Hermes installation:

* ``hermes_constants.get_hermes_home()`` — ``HERMES_HOME`` env var, falling
  back to the platform default (``~/.hermes``).
* ``hermes_cli.profiles.get_active_profile_name()`` — a non-default sticky
  profile (``<root>/active_profile``) redirects the home to
  ``<root>/profiles/<name>``, exactly like the CLI entry point
  (``hermes_cli/main.py``) does before any module import.

Order of precedence (same as the core launcher):

1. ``HERMES_HOME`` set and already profile-shaped (parent dir is
   ``profiles``) → used verbatim.
2. Sticky active profile (``<root>/active_profile`` contains a name other
   than ``default``) → ``<root>/profiles/<name>``.
3. ``HERMES_HOME`` set (non profile-shaped) → used verbatim.
4. Platform default (``~/.hermes`` on POSIX).

``HERMES_PROFILE`` is deliberately NOT consulted for path resolution: the
Hermes core does not use it to relocate the home at boot (it is only read
by subprocess spawners / kanban tooling), so following it here would drift
from the running installation.
"""

from __future__ import annotations

import os
import platform
from pathlib import Path

_HERMES_DIR_NAME = ".hermes"
_PROFILES_DIR_NAME = "profiles"
_ACTIVE_PROFILE_FILENAME = "active_profile"
_CORE_CHECKOUT_DIRNAME = "hermes-agent"

_DEFAULT_PROFILE = "default"


def _platform_default_home() -> Path:
    """Return the platform-native default Hermes home (``~/.hermes``)."""
    return _user_home() / _HERMES_DIR_NAME


def _user_home() -> Path:
    return Path.home()


def _env_home() -> Path | None:
    val = os.environ.get("HERMES_HOME", "").strip()
    return Path(val) if val else None


def is_profile_path(home: str | Path) -> bool:
    """True when *home* lives under a ``profiles/<name>`` layout."""
    return Path(home).expanduser().parent.name == _PROFILES_DIR_NAME


def hermes_root() -> Path:
    """Return the root Hermes directory (mirrors ``get_default_hermes_root``).

    Standard installs: ``~/.hermes``. Docker/custom deployments where
    ``HERMES_HOME`` points outside the native home: ``HERMES_HOME`` itself.
    Profile mode (``HERMES_HOME`` = ``<root>/profiles/<name>``): ``<root>``.
    """
    native = _platform_default_home()
    env = _env_home()
    if env is None:
        return native
    try:
        env.resolve().relative_to(native.resolve())
        # HERMES_HOME is under ~/.hermes (normal or profile mode)
        return native
    except ValueError:
        if env.parent.name == _PROFILES_DIR_NAME:
            return env.parent.parent
        return env


def _read_active_profile() -> str:
    """Read the sticky active profile name (``default`` when absent/empty)."""
    path = hermes_root() / _ACTIVE_PROFILE_FILENAME
    try:
        name = path.read_text(encoding="utf-8").strip()
        return name if name else _DEFAULT_PROFILE
    except (OSError, UnicodeDecodeError):
        return _DEFAULT_PROFILE


def get_hermes_home() -> Path:
    """Return the profile-aware Hermes home used by the running installation.

    See the module docstring for the precedence order.
    """
    env = _env_home()
    if env is not None and is_profile_path(env):
        return env
    active = _read_active_profile()
    if active != _DEFAULT_PROFILE:
        return hermes_root() / _PROFILES_DIR_NAME / active
    if env is not None:
        return env
    return _platform_default_home()


def get_active_profile() -> str:
    """Infer the current profile name (mirrors ``profiles.get_active_profile_name``).

    ``default`` when the home is the root, the profile name when the home
    lives under ``<root>/profiles/<name>``, ``custom`` otherwise.
    """
    home = get_hermes_home().resolve()
    root = hermes_root().resolve()
    if home == root:
        return _DEFAULT_PROFILE
    profiles_root = (root / _PROFILES_DIR_NAME).resolve()
    try:
        rel = home.relative_to(profiles_root)
        if len(rel.parts) == 1:
            return rel.parts[0]
    except ValueError:
        pass
    return "custom"


def display_home_path(path: str | Path) -> str:
    """Render *path* with a ``~/`` prefix when under the user home, else absolute."""
    resolved = Path(path).resolve()
    home = _user_home().resolve()
    try:
        rel = resolved.relative_to(home)
        return "~/" + rel.as_posix() if rel.parts else "~"
    except ValueError:
        return str(resolved)


# ---------------------------------------------------------------------------
# Hermes state convenience paths (all profile-aware)
# ---------------------------------------------------------------------------

def hermes_core_dir() -> Path:
    """The Hermes core checkout directory (``<home>/hermes-agent``)."""
    return get_hermes_home() / _CORE_CHECKOUT_DIRNAME


def hermes_state_db() -> Path:
    """SessionDB path (``<home>/state.db``)."""
    return get_hermes_home() / "state.db"


def hermes_sessions_dir() -> Path:
    """JSONL transcript + gateway index directory (``<home>/sessions``)."""
    return get_hermes_home() / "sessions"


def hermes_logs_dir() -> Path:
    """Runtime logs directory (``<home>/logs``)."""
    return get_hermes_home() / "logs"


def hermes_skills_dir() -> Path:
    """Installed skills directory (``<home>/skills``)."""
    return get_hermes_home() / "skills"


def hermes_cache_dir() -> Path:
    """Hermes-managed cache directory (``<home>/cache``)."""
    return get_hermes_home() / "cache"


def hermes_config_path() -> Path:
    """Hermes configuration file (``<home>/config.yaml``)."""
    return get_hermes_home() / "config.yaml"


def hermes_vault_brain_dir() -> Path:
    """Nightly-brain candidate store (``<home>/vault-brain``)."""
    return get_hermes_home() / "vault-brain"


def hermes_vault_dir() -> Path:
    """Return the configured Obsidian vault directory.

    ``MISSION_CONTROL_VAULT_PATH`` is canonical; the older
    ``HERMES_OBSIDIAN_VAULT`` name remains supported. Without an override,
    use the platform-native default used by Mission Control knowledge views.
    """
    override = os.environ.get("MISSION_CONTROL_VAULT_PATH") or os.environ.get("HERMES_OBSIDIAN_VAULT")
    if override:
        return Path(os.path.expanduser(override)).resolve()
    if platform.system().lower() == "darwin":
        return (_user_home() / "Documents" / "Hermes").resolve()
    return (_user_home() / "wiki").resolve()
