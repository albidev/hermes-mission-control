"""Local visibility configuration for Mission Control provider usage."""

from __future__ import annotations

import os

BUILTIN_USAGE_PROVIDERS = ("codex", "ollama", "openrouter", "nous")


def visible_usage_providers() -> tuple[str, ...]:
    """Return the locally configured provider allowlist in stable order.

    ``MISSION_CONTROL_USAGE_PROVIDERS`` is intentionally local configuration,
    loaded by the telemetry launcher from ``~/.hermes/mission-control.env``.
    An unset or blank value keeps every built-in provider visible.
    """
    raw = os.environ.get("MISSION_CONTROL_USAGE_PROVIDERS", "").strip()
    if not raw:
        return BUILTIN_USAGE_PROVIDERS

    configured = {item.strip().lower() for item in raw.split(",") if item.strip()}
    return tuple(provider for provider in BUILTIN_USAGE_PROVIDERS if provider in configured)


def is_usage_provider_visible(provider: str) -> bool:
    return provider.strip().lower() in visible_usage_providers()
