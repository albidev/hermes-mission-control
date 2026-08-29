"""Local visibility and presentation configuration for provider usage."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from hermes_paths import get_hermes_home, hermes_root

BUILTIN_USAGE_PROVIDERS = ("codex", "ollama", "openrouter", "nous")
_USAGE_CONFIG_FILENAME = "mission-control-usage.json"


def visible_usage_providers() -> tuple[str, ...]:
    """Return the locally configured provider allowlist in stable order.

    ``MISSION_CONTROL_USAGE_PROVIDERS`` is intentionally local configuration,
    loaded by the telemetry launcher from the external environment file.
    An unset or blank value keeps every built-in provider visible.
    """
    raw = os.environ.get("MISSION_CONTROL_USAGE_PROVIDERS", "").strip()
    if not raw:
        return BUILTIN_USAGE_PROVIDERS

    configured = {item.strip().lower() for item in raw.split(",") if item.strip()}
    return tuple(provider for provider in BUILTIN_USAGE_PROVIDERS if provider in configured)


def is_usage_provider_visible(provider: str) -> bool:
    return provider.strip().lower() in visible_usage_providers()


def _config_paths() -> list[Path]:
    override = os.environ.get("MISSION_CONTROL_USAGE_CONFIG_FILE", "").strip()
    if override:
        return [Path(override).expanduser()]

    paths = [
        get_hermes_home() / _USAGE_CONFIG_FILENAME,
        hermes_root() / _USAGE_CONFIG_FILENAME,
    ]
    unique: list[Path] = []
    for path in paths:
        resolved = path.resolve(strict=False)
        if resolved not in unique:
            unique.append(resolved)
    return unique


def _load_config() -> dict[str, Any]:
    for path in _config_paths():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            return payload
    return {}


def _configured_ids(section: Any, key: str) -> set[str]:
    if not isinstance(section, dict):
        return set()
    values = section.get(key)
    if not isinstance(values, list):
        return set()
    return {value.strip() for value in values if isinstance(value, str) and value.strip()}


def apply_provider_display_config(entry: dict[str, Any]) -> dict[str, Any]:
    """Apply local hidden/featured rules without changing provider semantics."""
    provider = entry.get("provider")
    if not isinstance(provider, str):
        return dict(entry)

    providers = _load_config().get("providers")
    provider_config = providers.get(provider) if isinstance(providers, dict) else None
    if not isinstance(provider_config, dict):
        return dict(entry)

    hidden = provider_config.get("hidden")
    featured = provider_config.get("featured")
    result = dict(entry)
    for collection_name in ("windows", "balances", "metrics"):
        items = entry.get(collection_name)
        if not isinstance(items, list):
            continue
        hidden_ids = _configured_ids(hidden, collection_name)
        featured_ids = _configured_ids(featured, collection_name)
        normalized: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            item_id = item.get("id")
            if isinstance(item_id, str) and item_id in hidden_ids:
                continue
            clone = dict(item)
            clone.pop("featured", None)
            if isinstance(item_id, str) and item_id in featured_ids:
                clone["featured"] = True
            normalized.append(clone)
        result[collection_name] = normalized
    return result
