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

import hashlib
import json
import math
import re
from datetime import datetime
from typing import Any, Dict, Optional

_SAFE_SOURCES = {"api", "oauth", "web", "cli", "local", "openai-web", "oauth+web", "codex-cli", "claude"}
_SAFE_UNITS = {"%", "USD", "credits", "tokens", "requests", "messages", "count", "days", "hours"}
_FIELD_ROLES = {
    "quota", "spend_limit", "limit_remaining", "account_balance", "spendable_balance",
    "balance_component", "credits", "spend_today", "spend_month", "spend",
    "paid_access", "reset_credits", "diagnostic",
}
_COMMON_FIELD_ROLES = {
    "windows": {"cost_budget": "spend_limit"},
    "balances": {"balance": "account_balance", "total_spendable": "spendable_balance",
                 "subscription_remaining": "balance_component", "topup_remaining": "balance_component",
                 "credits_remaining": "credits"},
    "metrics": {"cost_used": "spend", "cost_personal_used": "spend", "cost_next_regen": "diagnostic",
                "paid_access": "paid_access", "reset_credits_available": "reset_credits"},
}
_EMAIL = re.compile(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b")
_SECRET_TEXT = re.compile(
    r"(?i)\b(?:bearer\s+\S+|(?:api[_ -]?key|token|secret|password)\s*[:=]\s*\S+"
    r"|sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}"
    r"|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)"
)
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_FIELD_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}")
_PROVIDER_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_WARNING_CODES = {
    "invalid_field", "truncated", "synthetic_placeholder", "unknown_currency",
    "balance_unavailable", "unsupported_extension", "clock_skew", "no_data",
}
_COMMON_USAGE_KEYS = {
    "primary", "secondary", "tertiary", "extraRateWindows", "providerCost", "details",
    "subscriptionRenewsAt", "subscriptionExpiresAt", "updatedAt", "dataConfidence",
    "codexResetCredits", "rateWindowLabels", "planObservation",
}
_PRIVATE_USAGE_KEYS = {"identity", "accountEmail", "accountOrganization", "loginMethod", "pace"}


def _safe_id(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and _FIELD_ID.fullmatch(value) and _safe_label(value) else None


def _unique_id(value: str, seen: set[str]) -> str:
    # Prefixes and duplicate suffixes must fit the same contract as source IDs.
    if len(value) > 160:
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:20]
        value = f"{value[:135]}:{digest}"
    candidate = value
    occurrence = 2
    while candidate in seen:
        suffix = f":{occurrence}"
        base = value
        if len(base) + len(suffix) > 160:
            digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:20]
            base = f"{value[:135]}:{digest}"
        candidate = f"{base}{suffix}"
        occurrence += 1
    seen.add(candidate)
    return candidate


def _safe_label(value: Any) -> Optional[str]:
    return _safe_text(value, limit=80)


def _safe_text(value: Any, *, limit: int = 120) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = _CONTROL.sub(" ", value).strip()
    if _SECRET_TEXT.search(text) or _EMAIL.search(text):
        return None
    return text[:limit].strip() or None


def _timestamp(value: Any) -> Optional[str]:
    if not isinstance(value, str) or len(value) > 64:
        return None
    text = value.strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})?", text):
        return None
    try:
        datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError:
        return None
    return text


def _finite_number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return value if math.isfinite(float(value)) else None
        except OverflowError:
            return None
    return None


def _currency(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and re.fullmatch(r"[A-Z]{3}", value) else None


def _window_unit(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and (value in _SAFE_UNITS or _currency(value)) else None


def _infer_field_role(collection: str, field: Dict[str, Any]) -> Optional[str]:
    field_id = field["id"]
    role = _COMMON_FIELD_ROLES[collection].get(field_id)
    if role:
        return role
    if collection == "windows" and (field_id in ("primary", "secondary", "tertiary") or field_id.startswith("extra:")):
        return "quota"
    if collection == "metrics":
        if field.get("kind") == "chart":
            return "diagnostic"
        vocabulary = {
            "API key": {"API key limit": "spend_limit", "API key remaining": "limit_remaining",
                        "API key used": "diagnostic", "Today": "spend_today",
                        "This month": "spend_month", "This week": "diagnostic"},
            "Credits": {"Remaining": "account_balance", "Used": "diagnostic", "Total added": "diagnostic"},
        }
        return vocabulary.get(field.get("sectionLabel"), {}).get(field.get("label"))
    return None


def _window(value: Any, window_id: str, label: str, warnings: set[str]) -> Optional[Dict[str, Any]]:
    if not isinstance(value, dict) or value.get("isSyntheticPlaceholder") is True:
        return None
    result: Dict[str, Any] = {"id": window_id, "label": label}
    for key, normalize in (
        ("usedPercent", _finite_number), ("resetsAt", _timestamp),
        ("windowMinutes", _finite_number), ("remaining", _finite_number),
        ("total", _finite_number), ("unit", _window_unit),
        ("resetDescription", _safe_label), ("nextRegenPercent", _finite_number),
    ):
        if key not in value or value[key] is None:
            continue
        normalized = normalize(value[key])
        if normalized is not None:
            result[key] = normalized
        else:
            warnings.add("invalid_field")
    if isinstance(value.get("usageKnown"), bool):
        result["usageKnown"] = value["usageKnown"]
    if result.get("usageKnown") is False:
        for key in ("usedPercent", "remaining", "total"):
            result.pop(key, None)
    return result if len(result) > 2 else None


def _normalize_windows(item: Dict[str, Any], usage: Dict[str, Any], warnings: set[str]) -> list[Dict[str, Any]]:
    windows = []
    labels = item.get("rateWindowLabels")
    if not isinstance(labels, dict):
        labels = usage.get("rateWindowLabels")
    for window_id, fallback in (("primary", "Primary quota"), ("secondary", "Secondary quota"), ("tertiary", "Tertiary quota")):
        raw = usage.get(window_id)
        label = _safe_label(labels.get(window_id)) if isinstance(labels, dict) else None
        if isinstance(raw, dict) and raw.get("isSyntheticPlaceholder") is True:
            warnings.add("synthetic_placeholder")
        window = _window(raw, window_id, label or fallback, warnings)
        if window:
            windows.append(window)
    extra = usage.get("extraRateWindows")
    if isinstance(extra, list):
        if len(extra) > 64:
            warnings.add("truncated")
        seen: set[str] = set()
        for raw in extra[:64]:
            field_id = _safe_id(raw.get("id")) if isinstance(raw, dict) else None
            if not field_id or not isinstance(raw.get("window"), dict):
                warnings.add("invalid_field")
                continue
            value = dict(raw["window"])
            if value.get("isSyntheticPlaceholder") is True:
                warnings.add("synthetic_placeholder")
            if isinstance(raw.get("usageKnown"), bool):
                value["usageKnown"] = raw["usageKnown"]
            window = _window(value, _unique_id(f"extra:{field_id}", seen), _safe_label(raw.get("title")) or "Extra quota", warnings)
            if window:
                windows.append(window)
    return windows


def _normalize_cost(raw: Any, warnings: set[str]) -> tuple[list, list, list]:
    windows, balances, metrics = [], [], []
    if not isinstance(raw, dict):
        return windows, balances, metrics
    currency = _currency(raw.get("currencyCode"))
    if not currency:
        warnings.add("unknown_currency")
    money = {"currency": currency} if currency else {}
    period = _safe_label(raw.get("period"))
    observed = _timestamp(raw.get("updatedAt"))
    metadata = {"updatedAt": observed} if observed else {}
    for key, field_id, label in (
        ("used", "cost_used", "Spend"),
        ("personalUsed", "cost_personal_used", "Personal spend"),
        ("nextRegenAmount", "cost_next_regen", "Next regeneration"),
    ):
        value = _finite_number(raw.get(key))
        if value is not None:
            metrics.append({"id": field_id, "label": label, "value": value, **money, **metadata,
                            **({"sectionLabel": period} if period else {})})
    used, limit = _finite_number(raw.get("used")), _finite_number(raw.get("limit"))
    if used is not None and limit is not None and limit > 0:
        percent, remaining = _finite_number(used / limit * 100), _finite_number(limit - used)
        if percent is not None and remaining is not None:
            window = {"id": "cost_budget", "label": f"{period} spend limit" if period else "Spend limit",
                      "usedPercent": percent, "remaining": remaining, "total": limit, **metadata}
            if currency:
                window["unit"] = currency
            reset = _timestamp(raw.get("resetsAt"))
            if reset:
                window["resetsAt"] = reset
            windows.append(window)
    balance = _finite_number(raw.get("balance"))
    if raw.get("balanceIsUnavailable") is True:
        warnings.add("balance_unavailable")
    elif balance is not None:
        item = {"id": "balance", "label": "Balance", "value": balance, **money}
        observed = _timestamp(raw.get("balanceUpdatedAt"))
        if observed:
            item["updatedAt"] = observed
        if isinstance(raw.get("balanceIsWorkspace"), bool):
            item["scope"] = "workspace" if raw["balanceIsWorkspace"] else "account"
        balances.append(item)
    return windows, balances, metrics


def _progress(value: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(value, dict):
        return None
    used, total = _finite_number(value.get("used")), _finite_number(value.get("total"))
    return {"used": used, "total": total} if used is not None and total is not None and total > 0 else None


def _bounded_list(raw: list, limit: int, warnings: set[str]) -> list:
    if len(raw) > limit:
        warnings.add("truncated")
    return raw[:limit]


def _chart(raw: Any, warnings: set[str]) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict) or raw.get("kind") not in ("bars", "line") or not isinstance(raw.get("points"), list):
        return None
    points = []
    for point in _bounded_list(raw["points"], 120, warnings):
        label = _safe_text(point.get("label")) if isinstance(point, dict) else None
        value = _finite_number(point.get("value")) if isinstance(point, dict) else None
        if label and value is not None:
            points.append({"label": label, "value": value})
        else:
            warnings.add("invalid_field")
    if not points:
        return None
    result = {"kind": raw["kind"], "points": points}
    for key in ("title", "unit"):
        text = _safe_text(raw.get(key))
        if text:
            result[key] = text
    return result


def _detail_id(section: Optional[str], label: str) -> str:
    identity = json.dumps([section, label], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]


def _normalize_details(raw: Any, warnings: set[str]) -> list[Dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    metrics = []
    seen: set[str] = set()
    for section_index, section in enumerate(_bounded_list(raw, 8, warnings)):
        if not isinstance(section, dict):
            warnings.add("invalid_field")
            continue
        title = _safe_text(section.get("title"))
        rows = section.get("rows")
        if isinstance(rows, list):
            for row_index, row in enumerate(_bounded_list(rows, 24, warnings)):
                if not isinstance(row, dict):
                    warnings.add("invalid_field")
                    continue
                label = _safe_text(row.get("label"))
                value = row.get("value")
                if isinstance(value, str):
                    value = _safe_text(value)
                elif not isinstance(value, bool):
                    value = _finite_number(value)
                if not label or value is None:
                    warnings.add("invalid_field")
                    continue
                field_id = _safe_id(row.get("id")) or _detail_id(title, label)
                metric = {"id": _unique_id(f"detail:{field_id}", seen), "label": label, "value": value,
                          "legacyIds": [f"detail-{section_index}-{row_index}"]}
                if title:
                    metric["sectionLabel"] = title
                secondary = _safe_text(row.get("secondaryValue"))
                if secondary:
                    metric["secondaryValue"] = secondary
                elif row.get("secondaryValue") is not None:
                    warnings.add("invalid_field")
                progress = _progress(row.get("progress"))
                if progress:
                    metric["progress"] = progress
                elif row.get("progress") is not None:
                    warnings.add("invalid_field")
                number = _finite_number(row.get("usageValue"))
                if number is not None:
                    metric["usageValue"] = number
                elif row.get("usageValue") is not None:
                    warnings.add("invalid_field")
                metrics.append(metric)
        chart = _chart(section.get("chart"), warnings)
        if chart:
            label = chart.get("title") or title or "Usage chart"
            metric = {"id": _unique_id(f"detail:chart:{_detail_id(title, label)}", seen),
                      "label": label, "kind": "chart", "chart": chart}
            if title:
                metric["sectionLabel"] = title
            metrics.append(metric)
    return metrics


def _base_entry(provider: str, *, available: bool, source: str, error: Optional[str] = None) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "provider": provider,
        "available": available,
        "dataState": "ready" if available else "error",
        "source": source,
        "updatedAt": None,
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

    matching = [entry for entry in payload if isinstance(entry, dict) and entry.get("provider") == provider]
    if len(matching) > 1:
        return unavailable_provider(provider, "cli", "Multiple CodexBar accounts returned for provider.")
    item = matching[0] if matching else None
    if not isinstance(item, dict):
        return unavailable_provider(provider, "cli", "Provider not returned by CodexBar.")

    source_value = item.get("source")
    source = source_value if isinstance(source_value, str) and source_value in _SAFE_SOURCES else "cli"
    error = item.get("error")
    if error:
        return unavailable_provider(provider, source, "CodexBar returned a provider error.")

    raw_usage = item.get("usage")
    if raw_usage is None and isinstance(item.get("credits"), dict):
        raw_usage = {}
    if not isinstance(raw_usage, dict):
        return unavailable_provider(provider, source, "Invalid CodexBar usage.")
    usage = raw_usage
    result = _base_entry(provider, available=True, source=source)
    result["updatedAt"] = _timestamp(usage.get("updatedAt")) or result["updatedAt"]

    warnings: set[str] = set()
    if any(key not in _COMMON_USAGE_KEYS | _PRIVATE_USAGE_KEYS for key in usage):
        warnings.add("unsupported_extension")
    plan_observation = usage.get("planObservation")
    if isinstance(plan_observation, dict) and "rawPlan" in plan_observation:
        plan = _safe_text(plan_observation.get("rawPlan"))
        if plan:
            result["plan"] = plan
        else:
            warnings.add("invalid_field")
    elif plan_observation is not None:
        warnings.add("invalid_field")
    result["windows"] = _normalize_windows(item, usage, warnings)

    cost_windows, balances, metrics = _normalize_cost(usage.get("providerCost"), warnings)
    result["windows"].extend(cost_windows)
    result["balances"].extend(balances)
    result["metrics"].extend(metrics)

    credits = item.get("credits")
    remaining = _finite_number(credits.get("remaining")) if isinstance(credits, dict) else None
    if isinstance(credits, dict) and credits.get("balanceReadSucceeded") is False:
        warnings.add("balance_unavailable")
    elif remaining is not None:
        balance = {"id": "credits_remaining", "label": "Credits remaining", "value": remaining, "unit": "credits"}
        observed = _timestamp(credits.get("updatedAt"))
        if observed:
            balance["updatedAt"] = observed
        if isinstance(credits.get("balanceIsWorkspace"), bool):
            balance["scope"] = "workspace" if credits["balanceIsWorkspace"] else "account"
        result["balances"].append(balance)
    reset_credits = usage.get("codexResetCredits")
    if isinstance(reset_credits, dict):
        count = _finite_number(reset_credits.get("availableCount"))
        if count is None and isinstance(reset_credits.get("credits"), list):
            count = sum(1 for entry in reset_credits["credits"] if isinstance(entry, dict) and entry.get("status") == "available")
        if count is not None:
            result["metrics"].append({
                "id": "reset_credits_available", "label": "Reset credits available", "value": count, "unit": "count",
            })
    renewal = _timestamp(usage.get("subscriptionRenewsAt"))
    if renewal:
        result["renewsAt"] = renewal
    expiry = _timestamp(usage.get("subscriptionExpiresAt"))
    if expiry:
        result["metrics"].append({
            "id": "subscription_expires", "label": "Subscription expires", "value": expiry, "kind": "timestamp",
        })
    confidence = usage.get("dataConfidence")
    if isinstance(confidence, str) and confidence in ("exact", "estimated", "percentOnly", "unknown"):
        result["dataConfidence"] = confidence

    result["metrics"].extend(_normalize_details(usage.get("details"), warnings))
    for collection in ("windows", "balances", "metrics"):
        for field in result[collection]:
            role = _infer_field_role(collection, field)
            if role:
                field["role"] = role

    result["available"] = any(result[key] for key in ("windows", "balances", "metrics"))
    result["dataState"] = "ready" if result["available"] else "no_data"
    if warnings:
        result["warnings"] = sorted(warnings)
    return result


def _field_metadata(raw: Dict[str, Any], result: Dict[str, Any], warnings: set[str]) -> None:
    if "role" in raw:
        role = raw["role"]
        if isinstance(role, str) and role in _FIELD_ROLES:
            result["role"] = role
        else:
            warnings.add("invalid_field")
    if isinstance(raw.get("featured"), bool):
        result["featured"] = raw["featured"]
    if raw.get("updatedAt") is None and "updatedAt" in raw:
        result["updatedAt"] = None
    elif timestamp := _timestamp(raw.get("updatedAt")):
        result["updatedAt"] = timestamp
    elif "updatedAt" in raw:
        warnings.add("invalid_field")
    if raw.get("scope") in ("account", "workspace"):
        result["scope"] = raw["scope"]
    elif raw.get("scope") is not None:
        warnings.add("invalid_field")
    aliases = raw.get("legacyIds")
    if isinstance(aliases, list):
        valid = list(dict.fromkeys(alias for alias in aliases[:16] if _safe_id(alias)))
        if valid:
            result["legacyIds"] = valid
        if len(valid) != len(aliases):
            warnings.add("invalid_field")
    elif aliases is not None:
        warnings.add("invalid_field")


def _cached_field(raw: Any, collection: str, warnings: set[str]) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    field_id, label = _safe_id(raw.get("id")), _safe_text(raw.get("label"))
    if not field_id or not label:
        return None
    if collection == "windows":
        result = _window(raw, field_id, label, warnings)
        if not result:
            return None
    else:
        result = {"id": field_id, "label": label}
        value = raw.get("value")
        if collection == "metrics" and isinstance(value, str):
            value = _timestamp(value) if raw.get("kind") == "timestamp" else _safe_text(value)
        elif not (collection == "metrics" and isinstance(value, bool)):
            value = _finite_number(value)
        if value is not None:
            result["value"] = value
        if collection == "metrics":
            for key in ("secondaryValue", "sectionLabel"):
                text = _safe_text(raw.get(key))
                if text:
                    result[key] = text
            for key, normalize in (("progress", _progress), ("usageValue", _finite_number)):
                normalized = normalize(raw.get(key))
                if normalized is not None:
                    result[key] = normalized
            kind = raw.get("kind")
            if kind in ("value", "timestamp"):
                result["kind"] = kind
            chart = _chart(raw.get("chart"), warnings)
            if kind == "chart" and chart:
                result.update(kind="chart", chart=chart)
        if "value" not in result and "chart" not in result:
            return None
        currency = _currency(raw.get("currency"))
        if currency:
            result["currency"] = currency
        unit = _safe_text(raw.get("unit"))
        if unit:
            result["unit"] = unit
    _field_metadata(raw, result, warnings)
    return result


def normalize_cached_entry(entry: Any) -> Optional[Dict[str, Any]]:
    """Reconstruct approved fields; a malformed field cannot erase its siblings."""
    if not isinstance(entry, dict) or not isinstance(entry.get("provider"), str) or not _PROVIDER_ID.fullmatch(entry["provider"]):
        return None
    raw = entry if any(key in entry for key in ("windows", "balances", "metrics")) else _legacy_cached_entry(entry)
    source = raw.get("source")
    source = source if isinstance(source, str) and source in _SAFE_SOURCES | {"portal-account"} else "cli"
    result = _base_entry(raw["provider"], available=raw.get("available") is True, source=source)
    warnings = {code for code in raw.get("warnings", []) if isinstance(code, str) and code in _WARNING_CODES} if isinstance(raw.get("warnings"), list) else set()
    for key in ("updatedAt", "lastAttemptAt", "renewsAt", "freshUntil", "nextRetryAt", "refreshStartedAt", "refreshDeadlineAt"):
        if key in raw:
            result[key] = _timestamp(raw[key])
            if raw[key] is not None and result[key] is None:
                warnings.add("invalid_field")
    for key, allowed in (("refreshState", ("idle", "running", "cooldown", "failed")),
                         ("dataConfidence", ("exact", "estimated", "percentOnly", "unknown"))):
        if key in raw:
            if isinstance(raw[key], str) and raw[key] in allowed:
                result[key] = raw[key]
            else:
                warnings.add("invalid_field")
    if "staleAfterSeconds" in raw:
        seconds = _finite_number(raw["staleAfterSeconds"])
        if seconds is not None and seconds > 0:
            result["staleAfterSeconds"] = seconds
        else:
            warnings.add("invalid_field")
    result["stale"] = raw.get("stale") is True
    plan = _safe_text(raw.get("plan"))
    if plan:
        result["plan"] = plan
    error = _safe_text(raw.get("error"))
    if error:
        result["error"] = error
    elif raw.get("error"):
        result["error"] = "Provider refresh failed."
    for collection, limit in (("windows", 68), ("balances", 64), ("metrics", 256)):
        values = raw.get(collection)
        if not isinstance(values, list):
            warnings.add("invalid_field")
            continue
        seen: set[str] = set()
        for value in _bounded_list(values, limit, warnings):
            field = _cached_field(value, collection, warnings)
            if field:
                field["id"] = _unique_id(field["id"], seen)
                result[collection].append(field)
            else:
                warnings.add("invalid_field")
    has_data = any(result[key] for key in ("windows", "balances", "metrics"))
    if not has_data and "invalid_field" in warnings:
        result["available"] = False
    state = raw.get("dataState")
    if state is not None and state not in ("ready", "no_data", "error"):
        warnings.add("invalid_field")
    result["dataState"] = "ready" if result["available"] else "no_data" if state == "no_data" and "invalid_field" not in warnings else "error"
    if warnings:
        result["warnings"] = sorted(warnings)
    return result


def _legacy_cached_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Translate historical MC fields, then validate through the common reader."""
    provider = entry["provider"]
    result = _base_entry(
        provider,
        available=bool(entry.get("available")),
        source=str(entry.get("source") or "cli"),
        error=entry.get("error") if isinstance(entry.get("error"), str) else None,
    )
    for key in ("updatedAt", "lastAttemptAt", "stale", "plan", "renewsAt", "pace", "dataState", "dataConfidence", "refreshState", "staleAfterSeconds", "freshUntil", "nextRetryAt", "refreshStartedAt", "refreshDeadlineAt", "warnings"):
        if key in entry:
            result[key] = entry[key]

    labels = {"primary": "Session", "secondary": "Weekly", "tertiary": "Tertiary"}
    warnings: set[str] = set()
    for window_id, label in labels.items():
        window = _window(entry.get(window_id), window_id, label, warnings)
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
    if warnings:
        existing = result.get("warnings")
        result["warnings"] = sorted(warnings | {code for code in existing if isinstance(code, str) and code in _WARNING_CODES}) if isinstance(existing, list) else sorted(warnings)
    return result
