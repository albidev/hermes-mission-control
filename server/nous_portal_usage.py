"""Read-only Nous Portal account usage for Mission Control.

This adapter deliberately lives in the telemetry sidecar instead of Hermes Core.
It consumes the current access token already persisted by Hermes. When that token
is expired, it delegates refresh to the existing ``hermes portal info`` command;
it never implements or calls the refresh-token endpoint itself. Refresh tokens
are single-use; a monitoring process must not rotate or persist them
independently of Hermes.
"""

from __future__ import annotations

import json
import math
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from hermes_paths import get_hermes_home, hermes_root
from provider_usage_contract import unavailable_provider

_DEFAULT_PORTAL_BASE_URL = "https://portal.nousresearch.com"
_ALLOWED_PORTAL_HOSTS = {"portal.nousresearch.com"}
_REQUEST_TIMEOUT_SECONDS = 8
_CACHE_TTL_SECONDS = 60.0

_CACHE_LOCK = threading.Lock()
_CACHE: tuple[float, dict[str, Any]] | None = None


def reset_nous_portal_usage_cache() -> None:
    """Clear the process-local snapshot; intended for tests and diagnostics."""
    global _CACHE
    with _CACHE_LOCK:
        _CACHE = None


def _auth_paths() -> list[Path]:
    """Return profile-local auth first, then the shared root auth store."""
    paths = [get_hermes_home() / "auth.json", hermes_root() / "auth.json"]
    unique: list[Path] = []
    for path in paths:
        resolved = path.resolve(strict=False)
        if resolved not in unique:
            unique.append(resolved)
    return unique


def _read_nous_state() -> Optional[dict[str, Any]]:
    """Read only the Nous provider state from the active/shared auth stores."""
    for path in _auth_paths():
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(document, dict):
            continue
        providers = document.get("providers")
        state = providers.get("nous") if isinstance(providers, dict) else None
        if isinstance(state, dict):
            return state
    return None


def _finite_float(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _string(value: Any) -> Optional[str]:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _portal_url(state: dict[str, Any]) -> str:
    candidate = _string(state.get("portal_base_url")) or _DEFAULT_PORTAL_BASE_URL
    parsed = urllib.parse.urlsplit(candidate)
    if parsed.scheme != "https" or parsed.hostname not in _ALLOWED_PORTAL_HOSTS:
        return _DEFAULT_PORTAL_BASE_URL
    return candidate.rstrip("/")


def _unavailable(error: str) -> dict[str, Any]:
    return unavailable_provider("nous", "portal-account", error)


def _token_is_expiring(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        expires_at = datetime.fromisoformat(text)
    except ValueError:
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at <= datetime.now(timezone.utc)


def _hermes_cli_path() -> Optional[str]:
    candidates = [
        shutil.which("hermes"),
        str(get_hermes_home() / "hermes-agent" / "venv" / "bin" / "hermes"),
        str(hermes_root() / "hermes-agent" / "venv" / "bin" / "hermes"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    return None


def _refresh_via_hermes_cli() -> bool:
    """Ask Hermes to refresh its own OAuth state without touching refresh tokens."""
    executable = _hermes_cli_path()
    if not executable:
        return False
    try:
        completed = subprocess.run(
            [executable, "portal", "info"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        return completed.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False

def _subscription_usage(monthly: Optional[float], remaining: Optional[float]) -> Optional[dict[str, Any]]:
    if monthly is None or monthly <= 0 or remaining is None or remaining > monthly:
        return None
    remaining_clamped = max(0.0, remaining)
    used_percent = round(max(0.0, min(100.0, (monthly - remaining_clamped) / monthly * 100.0)))
    return {
        "id": "subscription",
        "label": "Subscription",
        "usedPercent": used_percent,
        "remaining": remaining_clamped,
        "total": monthly,
        "unit": "USD",
    }


def normalize_account_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Convert the Portal account payload into a UI-safe billing snapshot."""
    subscription = payload.get("subscription")
    subscription = subscription if isinstance(subscription, dict) else {}
    access = payload.get("paid_service_access")
    access = access if isinstance(access, dict) else {}

    monthly = _finite_float(subscription.get("monthly_credits"))
    subscription_remaining = _finite_float(access.get("subscription_credits_remaining"))
    if subscription_remaining is None:
        subscription_remaining = _finite_float(subscription.get("credits_remaining"))
    topup_remaining = _finite_float(access.get("purchased_credits_remaining"))
    total_spendable = _finite_float(access.get("total_usable_credits"))
    if total_spendable is None:
        parts = [value for value in (subscription_remaining, topup_remaining) if value is not None]
        total_spendable = sum(parts) if parts else None

    paid_access = access.get("allowed")
    if paid_access is None:
        paid_access = access.get("paid_access")
    status = "healthy"
    if paid_access is False:
        status = "depleted"
    elif total_spendable is None:
        status = "unknown"

    windows: list[dict[str, Any]] = []
    subscription_usage = _subscription_usage(monthly, subscription_remaining)
    if subscription_usage is not None:
        windows.append(subscription_usage)

    balances = []
    if subscription_remaining is not None:
        balances.append({
            "id": "subscription_remaining",
            "label": "Subscription remaining",
            "value": subscription_remaining,
            "currency": "USD",
        })
    if topup_remaining is not None:
        balances.append({
            "id": "topup_remaining",
            "label": "Top-up remaining",
            "value": topup_remaining,
            "currency": "USD",
        })
    if total_spendable is not None:
        balances.append({
            "id": "total_spendable",
            "label": "Total spendable",
            "value": total_spendable,
            "currency": "USD",
        })

    metrics = []
    if paid_access is not None:
        metrics.append({
            "id": "paid_access",
            "label": "Paid access",
            "value": paid_access,
            "unit": "boolean",
        })

    return {
        "provider": "nous",
        "available": True,
        "source": "portal-account",
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "plan": _string(subscription.get("plan")),
        "status": status,
        "renewsAt": _string(subscription.get("current_period_end")),
        "stale": False,
        "windows": windows,
        "balances": balances,
        "metrics": metrics,
    }


def fetch_nous_portal_usage() -> dict[str, Any]:
    """Fetch Nous billing data using the currently persisted access token.

    This function never implements the refresh-token exchange. If the access token
    is expired, refresh is delegated to Hermes's existing CLI command.
    """
    state = _read_nous_state()
    access_token = _string(state.get("access_token")) if state else None
    if state and access_token and _token_is_expiring(state.get("expires_at")):
        # Hermes remains the only component allowed to rotate the refresh token.
        # The CLI command uses the existing authenticated session and persists
        # any token rotation back to auth.json; telemetry then re-reads it.
        if _refresh_via_hermes_cli():
            state = _read_nous_state()
            access_token = _string(state.get("access_token")) if state else None
    if not access_token:
        return _unavailable("Nous Portal is not authenticated.")

    request = urllib.request.Request(
        f"{_portal_url(state or {})}/api/oauth/account",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=_REQUEST_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            return _unavailable("Nous Portal access token expired; refresh the Hermes Portal session.")
        return _unavailable(f"Nous Portal returned HTTP {exc.code}.")
    except (urllib.error.URLError, TimeoutError):
        return _unavailable("Nous Portal request timed out or failed.")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError):
        return _unavailable("Invalid Nous Portal account response.")

    if not isinstance(payload, dict):
        return _unavailable("Invalid Nous Portal account response.")
    return normalize_account_payload(payload)


def collect_nous_portal_usage(*, force: bool = False) -> dict[str, Any]:
    """Return a 60-second process-local snapshot, preserving last good data."""
    global _CACHE
    now = time.monotonic()
    with _CACHE_LOCK:
        if not force and _CACHE is not None and now - _CACHE[0] < _CACHE_TTL_SECONDS:
            return dict(_CACHE[1])

    result = fetch_nous_portal_usage()
    if result.get("available"):
        with _CACHE_LOCK:
            _CACHE = (time.monotonic(), result)
        return dict(result)

    with _CACHE_LOCK:
        if _CACHE is not None and _CACHE[1].get("available"):
            stale = dict(_CACHE[1])
            stale["stale"] = True
            stale["error"] = result.get("error")
            return stale
    return result
