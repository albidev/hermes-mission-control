"""Shared, allowlisted CodexBar provider collection for the sidecar and cache writer."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import nous_portal_usage
from provider_usage_contract import normalize_codexbar_entry, unavailable_provider

_CODEXBAR_FALLBACK = "/opt/homebrew/bin/codexbar"
_PROVIDER_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_MAX_CONCURRENT_PROVIDER_REFRESHES = 5


def _decode_payload(stdout: str) -> Any:
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        raw = stdout.strip()
        try:
            decoded = json.loads(raw)
            return json.loads(decoded) if isinstance(decoded, str) else decoded
        except (json.JSONDecodeError, TypeError):
            start, end = raw.find("["), raw.rfind("]")
            if start >= 0 and end > start:
                try:
                    return json.loads(raw[start : end + 1])
                except json.JSONDecodeError:
                    pass
    return None


def _codexbar_provider_ids(catalog: list[dict[str, Any]]) -> set[str]:
    return {
        item["provider"]
        for item in catalog
        if isinstance(item, dict)
        and item.get("source") == "codexbar"
        and isinstance(item.get("provider"), str)
        and _PROVIDER_ID.fullmatch(item["provider"])
        and item["provider"] != "nous"
    }


def collect_codexbar_provider(provider: str, catalog_ids: set[str]) -> dict[str, Any]:
    """Collect exactly one provider, only after discovery has allowlisted its ID."""
    if provider not in catalog_ids or not _PROVIDER_ID.fullmatch(provider) or provider == "nous":
        return unavailable_provider(provider, "cli", "Unknown provider.")

    executable = shutil.which("codexbar") or _CODEXBAR_FALLBACK
    try:
        completed = subprocess.run(
            [executable, "usage", "--provider", provider, "--json", "--no-color"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return unavailable_provider(provider, "cli", "CodexBar timed out.")
    except OSError:
        return unavailable_provider(provider, "cli", "CodexBar unavailable.")

    if not completed.stdout.strip():
        return unavailable_provider(provider, "cli", "CodexBar returned no data.")
    result = normalize_codexbar_entry(provider, _decode_payload(completed.stdout))
    if completed.returncode != 0 and result.get("available"):
        return unavailable_provider(provider, result.get("source", "cli"), "CodexBar returned a provider error.")
    return result


def collect_codexbar_usage(
    providers: tuple[str, ...] | list[str],
    catalog: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Collect selected CodexBar providers independently; Nous is native to MC."""
    catalog_ids = _codexbar_provider_ids(catalog)
    selected = tuple(provider for provider in providers if provider != "nous")
    if not selected:
        return []

    def collect_one(provider: str) -> dict[str, Any]:
        try:
            return collect_codexbar_provider(provider, catalog_ids)
        except Exception:
            return unavailable_provider(provider, "cli", "CodexBar provider refresh failed.")

    with ThreadPoolExecutor(max_workers=min(_MAX_CONCURRENT_PROVIDER_REFRESHES, len(selected))) as executor:
        return list(executor.map(collect_one, selected))


def collect_selected_usage(
    providers: tuple[str, ...] | list[str], catalog: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Use one bounded pool for native Nous and allowlisted CLI providers."""
    catalog_ids = _codexbar_provider_ids(catalog)
    selected = tuple(dict.fromkeys(providers))
    if not selected:
        return []

    def collect_one(provider: str) -> dict[str, Any]:
        try:
            if provider == "nous":
                return nous_portal_usage.fetch_nous_portal_usage()
            return collect_codexbar_provider(provider, catalog_ids)
        except Exception:
            return unavailable_provider(provider, "portal-account" if provider == "nous" else "cli",
                                        "Provider refresh failed.")

    with ThreadPoolExecutor(max_workers=min(_MAX_CONCURRENT_PROVIDER_REFRESHES, len(selected))) as executor:
        return list(executor.map(collect_one, selected))
