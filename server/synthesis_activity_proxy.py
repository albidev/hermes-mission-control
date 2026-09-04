"""Mission Control proxy for vault-scoped BDH synthesis activity."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


class SynthesisProxyError(RuntimeError):
    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


def _bdh_base_url() -> str:
    return os.environ.get("BDH_API_URL", "http://127.0.0.1:8643").rstrip("/")


def _request(path: str, *, method: str = "GET", payload: dict[str, Any] | None = None) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        f"{_bdh_base_url()}{path}",
        data=body,
        method=method,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            raw = response.read().decode("utf-8")
            status = int(response.status)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise SynthesisProxyError(f"BDH returned HTTP {exc.code}: {detail}", exc.code) from exc
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        raise SynthesisProxyError(f"BDH unavailable: {type(exc).__name__}: {exc}") from exc
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SynthesisProxyError("BDH returned invalid JSON") from exc
    if status < 200 or status >= 300 or not isinstance(value, dict):
        raise SynthesisProxyError(f"BDH returned invalid response ({status})", status)
    return value


def load_synthesis_activity(vault_id: str | None = None) -> dict[str, Any]:
    query = f"?vault_id={urllib.parse.quote(vault_id)}" if vault_id else ""
    return _request(f"/api/synthesis-activity{query}")


def revert_synthesis(operation_id: str, vault_id: str | None = None) -> dict[str, Any]:
    result = _request(
        "/api/synthesis/revert",
        method="POST",
        payload={"operation_id": operation_id, **({"vault_id": vault_id} if vault_id else {})},
    )
    if result.get("refresh_required"):
        try:
            refresh = _request(
                "/api/refresh-graph",
                method="POST",
                payload={**({"vault_id": vault_id} if vault_id else {})},
            )
            result["graph_refresh"] = refresh
        except SynthesisProxyError as exc:
            result["graph_refresh"] = {"status": "failed", "error": str(exc)}
    return result
