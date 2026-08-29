"""Provider-agnostic usage contract shared by Mission Control adapters.

Every provider entry uses the same small vocabulary:
- ``windows`` for quota or period usage;
- ``balances`` for money/credit balances;
- ``metrics`` for provider counters.

Provider-specific payloads are normalized at the telemetry boundary. The UI does
not need to know whether a value came from CodexBar, Nous Portal, or another
adapter.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Dict, Optional


def _finite_number(value: Any) -> Optional[float | int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value if math.isfinite(float(value)) else None
    return None


def _window(value: Any, window_id: str, label: str) -> Optional[Dict[str, Any]]:
    if not isinstance(value, dict):
        return None
    result: Dict[str, Any] = {"id": window_id, "label": label}
    for key in ("usedPercent", "resetsAt", "windowMinutes", "remaining", "total", "unit"):
        if key not in value or value[key] is None:
            continue
        if key in ("usedPercent", "windowMinutes", "remaining", "total"):
            number = _finite_number(value[key])
            if number is not None:
                result[key] = number
        elif isinstance(value[key], str):
            result[key] = value[key]
    return result if len(result) > 2 else None


def _base_entry(provider: str, *, available: bool, source: str, error: Optional[str] = None) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "provider": provider,
        "available": available,
        "source": source,
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "stale": False,
        "windows": [],
        "balances": [],
        "metrics": [],
    }
    if error:
        result["error"] = error[:240]
    return result


def unavailable_provider(provider: str, source: str, error: str) -> Dict[str, Any]:
    return _base_entry(provider, available=False, source=source, error=error)


def normalize_codexbar_entry(provider: str, payload: Any) -> Dict[str, Any]:
    """Normalize one CodexBar provider response to the MC contract."""
    if not isinstance(payload, list):
        return unavailable_provider(provider, "cli", "Invalid CodexBar response.")

    item = next((entry for entry in payload if isinstance(entry, dict) and entry.get("provider") == provider), None)
    if not isinstance(item, dict):
        return unavailable_provider(provider, "cli", "Provider not returned by CodexBar.")

    source = str(item.get("source") or "cli")
    error = item.get("error")
    if isinstance(error, dict):
        return unavailable_provider(provider, source, str(error.get("message") or "Provider unavailable."))

    raw_usage = item.get("usage")
    usage: Dict[str, Any] = raw_usage if isinstance(raw_usage, dict) else {}
    result = _base_entry(provider, available=True, source=source)
    result["updatedAt"] = usage.get("updatedAt") or result["updatedAt"]

    labels = {
        "primary": "Session",
        "secondary": "Weekly",
        "tertiary": "Tertiary",
    }
    for window_id, label in labels.items():
        window = _window(usage.get(window_id), window_id, label)
        if window is not None:
            result["windows"].append(window)

    if provider == "openrouter":
        openrouter = usage.get("openRouterUsage")
        if isinstance(openrouter, dict):
            balance = _finite_number(openrouter.get("balance"))
            if balance is not None:
                result["balances"].append({
                    "id": "balance",
                    "label": "Balance",
                    "value": balance,
                    "currency": "USD",
                })
            for key, label in (
                ("totalCredits", "Total credits"),
                ("totalUsage", "Total usage"),
                ("keyUsageDaily", "Daily usage"),
                ("keyUsageWeekly", "Weekly usage"),
                ("keyUsageMonthly", "Monthly usage"),
            ):
                value = _finite_number(openrouter.get(key))
                if value is not None:
                    result["metrics"].append({"id": key, "label": label, "value": value, "unit": "USD"})

    if provider == "codex":
        credits = item.get("credits")
        remaining = _finite_number(credits.get("remaining")) if isinstance(credits, dict) else None
        if remaining is not None:
            result["balances"].append({
                "id": "credits_remaining",
                "label": "Credits remaining",
                "value": remaining,
                "unit": "credits",
            })
        codex_credits = usage.get("codexResetCredits")
        if isinstance(codex_credits, dict):
            count = _finite_number(codex_credits.get("availableCount"))
            if count is None and isinstance(codex_credits.get("credits"), list):
                count = sum(1 for entry in codex_credits["credits"] if isinstance(entry, dict) and entry.get("status") == "available")
            if count is not None:
                result["metrics"].append({
                    "id": "reset_credits_available",
                    "label": "Reset credits available",
                    "value": count,
                    "unit": "count",
                })

    pace = item.get("pace")
    if isinstance(pace, dict):
        result["pace"] = pace
    return result


def normalize_cached_entry(entry: Any) -> Optional[Dict[str, Any]]:
    """Accept both the pre-contract cache and the current contract."""
    if not isinstance(entry, dict) or not isinstance(entry.get("provider"), str):
        return None
    if all(isinstance(entry.get(key), list) for key in ("windows", "balances", "metrics")):
        return dict(entry)

    provider = entry["provider"]
    result = _base_entry(
        provider,
        available=bool(entry.get("available")),
        source=str(entry.get("source") or "cli"),
        error=entry.get("error") if isinstance(entry.get("error"), str) else None,
    )
    for key in ("updatedAt", "stale", "plan", "renewsAt", "pace"):
        if key in entry:
            result[key] = entry[key]

    labels = {"primary": "Session", "secondary": "Weekly", "tertiary": "Tertiary"}
    for window_id, label in labels.items():
        window = _window(entry.get(window_id), window_id, label)
        if window is not None:
            result["windows"].append(window)

    openrouter = entry.get("openRouter")
    if isinstance(openrouter, dict):
        balance = _finite_number(openrouter.get("balance"))
        if balance is not None:
            result["balances"].append({"id": "balance", "label": "Balance", "value": balance, "currency": "USD"})

    credits = _finite_number(entry.get("creditsRemaining"))
    if credits is not None:
        result["balances"].append({"id": "credits_remaining", "label": "Credits remaining", "value": credits, "unit": "credits"})

    reset_count = _finite_number(entry.get("resetCreditsAvailable"))
    if reset_count is not None:
        result["metrics"].append({"id": "reset_credits_available", "label": "Reset credits available", "value": reset_count, "unit": "count"})
    return result
