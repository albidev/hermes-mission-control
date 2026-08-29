#!/usr/bin/env python3
"""Refresh the provider usage cache using the MC provider contract."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
SERVER_DIR = SCRIPT_DIR.parent / "server"
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from hermes_paths import hermes_cache_dir  # noqa: E402
from provider_usage_config import visible_usage_providers  # noqa: E402
from provider_usage_contract import normalize_codexbar_entry, unavailable_provider  # noqa: E402

CODEXBAR_PROVIDERS = ("codex", "ollama", "openrouter")


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


def collect_codexbar_usage() -> list[dict[str, Any]]:
    executable = shutil.which("codexbar") or "/opt/homebrew/bin/codexbar"
    visible = set(visible_usage_providers())
    providers: list[dict[str, Any]] = []
    for provider in CODEXBAR_PROVIDERS:
        if provider not in visible:
            continue
        source_args = ["--source", "web"] if provider == "ollama" else []
        try:
            completed = subprocess.run(
                [executable, "usage", "--provider", provider, *source_args, "--json", "--no-color"],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if not completed.stdout.strip():
                result = unavailable_provider(provider, "cli", "CodexBar returned no data.")
            else:
                result = normalize_codexbar_entry(provider, _decode_payload(completed.stdout))
            if completed.returncode != 0 and result.get("available"):
                result["available"] = False
                result["error"] = "CodexBar returned a provider error."
            providers.append(result)
        except subprocess.TimeoutExpired:
            providers.append(unavailable_provider(provider, "cli", "CodexBar timed out."))
        except OSError:
            providers.append(unavailable_provider(provider, "cli", "CodexBar unavailable."))
    return providers


def main() -> int:
    output_dir = Path(os.environ.get("MISSION_CONTROL_CACHE_DIR", "")).expanduser() if os.environ.get("MISSION_CONTROL_CACHE_DIR") else hermes_cache_dir()
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / "mission-control-provider-usage.json"
    temporary = output.with_name(f"{output.name}.tmp.{os.getpid()}")
    providers = collect_codexbar_usage()
    payload = {
        "schemaVersion": 1,
        "success": any(provider.get("available") for provider in providers),
        "available": True,
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "providers": providers,
    }
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    temporary.replace(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
