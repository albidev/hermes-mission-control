#!/usr/bin/env python3
from __future__ import annotations

import hmac
import hashlib
import json
import os
import signal
import platform
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from statistics import median
from collections.abc import Callable
from typing import Any, Deque, Dict, Optional

import psutil

SERVER_DIR = Path(__file__).resolve().parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from hermes_paths import (
    display_home_path,
    get_hermes_home as resolve_hermes_home,
    hermes_cache_dir,
    hermes_core_dir,
    hermes_logs_dir,
    hermes_skills_dir,
    hermes_root,
    hermes_vault_dir,
)

_CLIENT_DIAGNOSTICS_LOCK = threading.Lock()


def _client_diagnostics_log() -> Path:
    return hermes_logs_dir() / "mission-control-client.log"

import candidates as candidates_mod
from nous_portal_usage import collect_nous_portal_usage
from provider_usage_config import apply_provider_display_config, visible_usage_providers
from provider_usage_contract import normalize_cached_entry, normalize_codexbar_entry

from mission_control_agents import (
    load_agent_trace_snapshot,
    load_agents_sessions_snapshot,
    load_agents_snapshot,
    load_sessions_usage,
)

from push_server import (
    add_subscription,
    list_subscriptions,
    load_vapid_public_key,
    push_status,
    remove_subscription,
    send_push,
    start_gateway_watcher,
)

from last_chat_store import get_last_chat, set_last_chat
from chat_sync_relay import chat_sync_relay, core_event_dedupe_key, system_message_dedupe_key, user_message_dedupe_key
import kanban_bridge as kanban_bridge_mod
from kanban_bridge import KanbanError as KanbanBridgeError
import cron_bridge as cron_bridge_mod
from cron_bridge import CronBridgeError
from whiteboard_store import acknowledge_commands, enqueue_command, get_whiteboard, load_state, save_snapshot, save_state, get_agent_mode
from terminal_server import issue_ticket, shutdown_terminal_sessions, start_terminal_server

CANVAS_DISPATCH: dict[str, Callable[[str, dict], dict]] = {}


def register_canvas_handler(addon_id: str):
    """Register a handler for a canvas addon protocol."""
    def decorator(fn: Callable[[str, dict], dict]):
        CANVAS_DISPATCH[addon_id] = fn
        return fn
    return decorator


@register_canvas_handler('tldraw')
def handle_tldraw(session_id: str, data: dict) -> dict:
    """Protocol handler for tldraw addon (whiteboard-v2)."""
    action = data.get('action')
    if action == 'get':
        fallback = str(data.get('sessionKey') or '').strip()
        return get_whiteboard(session_id, fallback)
    if action == 'enqueue':
        command = data.get('command')
        if not isinstance(command, dict) or not str(command.get('type') or '').strip():
            raise ValueError('Missing command')
        mode = str(data.get('mode') or '').strip()
        if mode:
            command = {**command, 'mode': mode}
        return {'command': enqueue_command(session_id, command)}
    if action == 'mode':
        mode = str(data.get('mode') or '').strip()
        if mode not in ('draw', 'review', 'arrange', 'explain', ''):
            raise ValueError(f'Unknown mode {mode}')
        state = load_state()
        state.setdefault(session_id, {})['agentMode'] = mode
        state[session_id]['updatedAt'] = int(time.time() * 1000)
        save_state(state)
        return {'success': True, 'mode': mode}
    if action == 'ack':
        ids = data.get('commandIds')
        if not isinstance(ids, list):
            raise ValueError('Missing commandIds')
        acknowledge_commands(session_id, [str(item) for item in ids])
        return {'success': True}
    return save_snapshot(session_id, data.get('snapshot'))


def gb(value: float) -> float:
    return round(float(value) / (1024**3), 1)


_cpu_samples: Deque[float] = deque(maxlen=12)
_cpu_lock = threading.Lock()
_cpu_ready = threading.Event()


def _cpu_sampler() -> None:
    # Prime psutil baseline once, then keep sampling in background.
    psutil.cpu_percent(interval=None)
    while True:
        try:
            value = float(psutil.cpu_percent(interval=0.5))
            with _cpu_lock:
                _cpu_samples.append(value)
            _cpu_ready.set()
        except Exception:
            # Keep sampler alive even if psutil glitches.
            continue


def _read_smoothed_cpu_percent() -> float:
    if _cpu_ready.is_set():
        with _cpu_lock:
            if _cpu_samples:
                # Median is more stable than mean for short transient spikes/drops.
                return round(float(median(_cpu_samples)), 1)
    # Early fallback before first sampler tick.
    return round(float(psutil.cpu_percent(interval=None)), 1)


def collect_system_snapshot() -> Dict[str, Any]:
    vm = psutil.virtual_memory()
    disk = psutil.disk_usage(os.path.expanduser("~"))
    load = None
    try:
        load = os.getloadavg()
    except Exception:
        load = (None, None, None)

    cpu_cores = os.cpu_count() or 1
    cpu_percent = _read_smoothed_cpu_percent()

    load_one = round(float(load[0]), 2) if load[0] is not None else None
    load_five = round(float(load[1]), 2) if load[1] is not None else None
    load_fifteen = round(float(load[2]), 2) if load[2] is not None else None
    load_per_core = round(float(load[0]) / cpu_cores, 3) if load[0] is not None and cpu_cores else None

    # Guard against occasional 0.0 snapshots when the machine clearly has load.
    if load_per_core is not None:
        inferred_cpu_from_load = round(max(0.0, min(100.0, load_per_core * 100.0)), 1)
        if cpu_percent < 0.5 and inferred_cpu_from_load >= 1.0:
            cpu_percent = inferred_cpu_from_load

    process_memory_mb = round(float(psutil.Process().memory_info().rss) / (1024**2), 1)

    ram_used_percent = round((float(vm.used) / float(vm.total)) * 100.0, 1) if vm.total else 0.0
    disk_used_percent = (
        round(((float(disk.total) - float(disk.free)) / float(disk.total)) * 100.0, 1)
        if disk.total else 0.0
    )
    thermal = collect_thermal_snapshot()

    if disk.percent >= 92 or (load_per_core is not None and load_per_core >= 2.0):
        health = "degraded"
    else:
        health = "healthy"

    summary_parts = [f"Load {load_per_core:.2f}/core" if load_per_core is not None else "",
                     f"CPU {cpu_percent:.1f}%", f"RAM {ram_used_percent:.1f}%", f"Disk {disk_used_percent:.1f}%"]
    if thermal.get("fanRpm") is not None:
        summary_parts.append(f"Fan {thermal['fanRpm']:.0f} rpm")
    if thermal.get("thermalLevel") is not None:
        summary_parts.append(f"Thermal {thermal['thermalLevel'].capitalize()}")
    summary_parts.append(f"RSS {process_memory_mb:.1f} MB")
    summary = ", ".join(p for p in summary_parts)

    return {
        "source": "local-psutil",
        "collectedAt": datetime.now(timezone.utc).isoformat(),
        "health": health,
        "host": socket.gethostname() or "unknown",
        "platform": f"{platform.system()} {platform.release()}".strip(),
        "platformVersion": platform.version() or "",
        "cpuCores": cpu_cores,
        "cpuUsagePercent": cpu_percent,
        "ramUsage": {
            "usedPercent": ram_used_percent,
            "usedGb": gb(vm.used),
            "availableGb": gb(vm.available),
            "totalGb": gb(vm.total),
        },
        "loadAverage": {
            "one": load_one,
            "five": load_five,
            "fifteen": load_fifteen,
            "perCore": load_per_core,
        },
        "diskUsage": {
            "path": "~",
            "usedPercent": disk_used_percent,
            "freeGb": gb(disk.free),
            "totalGb": gb(disk.total),
        },
        "processMemoryMb": process_memory_mb,
        "thermal": thermal,
        "summary": summary,
    }


_THERMAL_LEVEL_INDEX = {
    "nominal": 0.0,
    "low": 25.0,
    "moderate": 50.0,
    "heavy": 75.0,
    "extreme": 100.0,
}


def _thermal_unavailable(error: Optional[str]) -> Dict[str, Any]:
    """Structured 'no usable thermal sensor' payload.

    ``unavailable`` is distinct from ``None`` so the frontend can tell
    "sensor missing" from "value is zero/nominal". The API still exposes
    ``error`` for diagnostics, but a missing sensor is a normal state on
    hosts without thermal hardware (containers, VMs, some desktops), not
    a backend failure.
    """
    return {
        "fanRpm": None,
        "fanCount": None,
        "thermalPressure": None,
        "thermalLevel": None,
        "levelSource": None,
        "source": "unavailable",
        "error": error,
    }


def _collect_linux_thermal_snapshot() -> Dict[str, Any]:
    """Collect thermal state from sysfs and, optionally, lm-sensors.

    Fallback order:

    1. ``/sys/class/thermal`` — kernel thermal zones. Purely passive
       reads, no privileges needed. Works for CPU packages/cores on most
       x86 and many ARM Linux hosts.
    2. ``sensors`` (lm-sensors) — a user-space fallback when sysfs has no
       ``thermal_zone`` entries but lm-sensors is installed. Executed
       without sudo; if it is missing or fails, the result stays
       ``unavailable``.
    3. Unavailable — structured ``unavailable`` state, never an error.
    """
    thermal_zones_dir = Path("/sys/class/thermal")

    if thermal_zones_dir.is_dir():
        zones = sorted(thermal_zones_dir.glob("thermal_zone*"))
        if zones:
            temps: list[float] = []
            for zone in zones:
                temp_path = zone / "temp"
                if not temp_path.is_file():
                    continue
                try:
                    temp_milli = float(temp_path.read_text(encoding="utf-8", errors="replace").strip())
                except (OSError, ValueError):
                    continue
                if temp_milli <= 0:
                    continue
                temps.append(temp_milli / 1000.0)
            if temps:
                return {
                    "fanRpm": None,
                    "fanCount": None,
                    "thermalPressure": round(max(temps), 1),
                    "thermalLevel": None,
                    "levelSource": None,
                    "source": "sysfs-thermal",
                    "error": None,
                }

    # lm-sensors fallback: `sensors` parses every chip the kernel knows
    # about. No sudo, no config: missing binary == unavailable.
    try:
        proc = subprocess.run(
            ["sensors", "-u"],
            capture_output=True,
            text=True,
            timeout=8,
        )
    except FileNotFoundError:
        return _thermal_unavailable("no thermal sensor available (sysfs empty, lm-sensors not installed)")
    except subprocess.TimeoutExpired:
        return _thermal_unavailable("sensors timed out")
    except Exception as exc:  # noqa: BLE001 - telemetry must never crash on a bad read
        return _thermal_unavailable(str(exc))

    if proc.returncode != 0:
        return _thermal_unavailable((proc.stderr or "").strip() or f"sensors exited {proc.returncode}")

    temps: list[float] = []
    for line in (proc.stdout or "").splitlines():
        # `sensors -u` emits "tempN_input: 52.000" per sensor.
        m = re.match(r"\s*temp\d+_input:\s*([+-]?\d+(?:\.\d+)?)", line)
        if m:
            temps.append(float(m.group(1)))
    if not temps:
        return _thermal_unavailable("sensors reported no temperature inputs")

    return {
        "fanRpm": None,
        "fanCount": None,
        "thermalPressure": round(max(temps), 1),
        "thermalLevel": None,
        "levelSource": None,
        "source": "lm-sensors",
        "error": None,
    }


def _collect_macos_thermal_snapshot() -> Dict[str, Any]:
    """Collect macOS thermal pressure through powermetrics.

    macOS 26 on Apple Silicon exposes a textual thermal pressure level but
    does not expose fan RPM or temperatures through the ``smc``/``fan``
    samplers. The numeric ``thermalPressure`` value is therefore only a
    normalised level index for the UI meter, never a temperature.
    """
    thermal_pressure: Optional[float] = None
    thermal_level: Optional[str] = None
    level_source: Optional[str] = None
    fan_rpm: Optional[float] = None
    fan_count: Optional[int] = None
    error: Optional[str] = None

    try:
        proc = subprocess.run(
            ["sudo", "-n", "/usr/bin/powermetrics", "--samplers", "thermal", "-n", "1", "-i", "1000", "-f", "text"],
            capture_output=True,
            text=True,
            timeout=8,
        )
        if proc.returncode != 0:
            stderr = (proc.stderr or "").strip()
            error = stderr or f"powermetrics exited {proc.returncode}"
        else:
            text = proc.stdout or ""
            # macOS releases have emitted both labels over time.
            m = re.search(r"(?:Current|Thermal)\s+pressure\s+level:\s*([A-Za-z]+)", text, re.IGNORECASE)
            if m:
                level = m.group(1).strip().lower()
                thermal_level = level
                level_source = "powermetrics"
                if level in _THERMAL_LEVEL_INDEX:
                    thermal_pressure = _THERMAL_LEVEL_INDEX[level]
                else:
                    error = f"unknown thermal level: {level}"
            else:
                error = "powermetrics returned no thermal pressure level"
    except subprocess.TimeoutExpired:
        error = "powermetrics timed out"
    except FileNotFoundError:
        error = "powermetrics not found"
    except Exception as exc:  # noqa: BLE001 - telemetry must never crash on a bad read
        error = str(exc)

    if error is not None and thermal_level is None:
        return _thermal_unavailable(error)

    return {
        "fanRpm": fan_rpm,
        "fanCount": fan_count,
        "thermalPressure": thermal_pressure,
        "thermalLevel": thermal_level,
        "levelSource": level_source,
        "source": "powermetrics",
        "error": error,
    }


def collect_thermal_snapshot() -> Dict[str, Any]:
    """Read thermal telemetry using the backend for the current OS."""
    current_platform = platform.system()
    if current_platform == "Linux":
        return _collect_linux_thermal_snapshot()
    if current_platform == "Darwin":
        return _collect_macos_thermal_snapshot()
    return _thermal_unavailable(f"unsupported platform: {current_platform or 'unknown'}")


_USAGE_PROVIDERS = ("codex", "ollama", "openrouter")


def _sanitize_usage_window(value: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(value, dict):
        return None
    result: Dict[str, Any] = {}
    for key in ("usedPercent", "resetsAt", "windowMinutes"):
        if key in value and value[key] is not None:
            result[key] = value[key]
    return result or None


def _sanitize_provider_usage(provider: str, payload: Any) -> Dict[str, Any]:
    """Compatibility wrapper for the provider-agnostic contract."""
    return normalize_codexbar_entry(provider, payload)


def collect_provider_usage() -> Dict[str, Any]:
    visible = set(visible_usage_providers())
    cache_path = hermes_cache_dir() / "mission-control-provider-usage.json"
    cached: Optional[Dict[str, Any]] = None
    try:
        candidate = json.loads(cache_path.read_text(encoding="utf-8"))
        if isinstance(candidate, dict) and isinstance(candidate.get("providers"), list):
            cached = candidate
    except (OSError, json.JSONDecodeError):
        pass

    if cached is not None:
        providers = []
        for provider in cached["providers"]:
            normalized = normalize_cached_entry(provider)
            if normalized is None or normalized.get("provider") not in visible or normalized.get("provider") == "nous":
                continue
            providers.append(apply_provider_display_config(normalized))
    else:
        executable = shutil.which("codexbar") or "/opt/homebrew/bin/codexbar"
        providers = []
        for provider in _USAGE_PROVIDERS:
            if provider not in visible:
                continue
            try:
                # Ollama's API path exposes no usage data; must read the web dashboard (Chrome cookies).
                src_flag = ["--source", "web"] if provider == "ollama" else []
                completed = subprocess.run(
                    [executable, "usage", "--provider", provider, *src_flag, "--json", "--no-color"],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
                stdout, stderr, returncode = completed.stdout, completed.stderr, completed.returncode
                try:
                    payload = json.loads(stdout)
                except json.JSONDecodeError:
                    raw_output = stdout.strip()
                    try:
                        decoded = json.loads(raw_output)
                        payload = json.loads(decoded) if isinstance(decoded, str) else decoded
                    except (json.JSONDecodeError, TypeError):
                        start = raw_output.find("[")
                        end = raw_output.rfind("]")
                        try:
                            payload = json.loads(raw_output[start:end + 1]) if start >= 0 and end > start else None
                        except json.JSONDecodeError:
                            payload = None
                result = apply_provider_display_config(_sanitize_provider_usage(provider, payload))
                if returncode != 0 and result.get("available"):
                    result["available"] = False
                    result["error"] = "CodexBar returned a provider error."
                providers.append(result)
            except (OSError, subprocess.TimeoutExpired) as exc:
                providers.append({
                    "provider": provider,
                    "available": False,
                    "source": "cli",
                    "error": "CodexBar unavailable." if isinstance(exc, OSError) else "CodexBar timed out.",
                })

    if "nous" in visible:
        # Nous is deliberately not sent through CodexBar. The sidecar uses the
        # access token already persisted by Hermes and never rotates a refresh token.
        providers.append(apply_provider_display_config(collect_nous_portal_usage()))
    return {
        "schemaVersion": 1,
        "success": any(provider.get("available") for provider in providers),
        "available": True,
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "providers": providers,
    }


def _resolve_access_token() -> Optional[str]:
    token = (os.getenv("MISSION_CONTROL_TOKEN") or "").strip()
    if token:
        return token
    fallback = (os.getenv("API_SERVER_KEY") or "").strip()
    return fallback or None


def _extract_bearer_token(header_value: Optional[str]) -> Optional[str]:
    if not header_value:
        return None
    scheme, _, token = header_value.partition(" ")
    if scheme.lower() != "bearer":
        return None
    token = token.strip()
    return token or None


def _extract_query_token(handler: BaseHTTPRequestHandler) -> Optional[str]:
    parsed = urllib.parse.urlparse(handler.path)
    params = urllib.parse.parse_qs(parsed.query)
    token = (params.get("access_token") or [""])[0].strip()
    return token or None


def _is_authorized(handler: BaseHTTPRequestHandler, *, allow_query_token: bool = False) -> bool:
    expected = _resolve_access_token()
    candidate = _extract_bearer_token(handler.headers.get("Authorization"))
    if not candidate and allow_query_token:
        candidate = _extract_query_token(handler)

    # Protected telemetry endpoints require the bearer token regardless of source
    # address. `/health` remains the unauthenticated liveness probe.
    if not expected or not candidate:
        return False
    return hmac.compare_digest(candidate, expected)


def _session_filter_params(params: dict[str, list[str]]) -> Optional[dict[str, str]]:
    filters = {}
    for key in ("query", "status", "category", "origin", "model", "tab"):
        value = (params.get(key) or [""])[0].strip()
        if value and value.lower() != "all":
            filters[key] = value
    return filters or None


def _append_client_diagnostic(payload: Dict[str, Any]) -> None:
    """Persist browser reload breadcrumbs without ever recording its auth token."""
    safe_payload = {key: value for key, value in payload.items() if key != "_accessToken"}
    line = json.dumps(safe_payload, ensure_ascii=False, separators=(",", ":"))
    diagnostics_log = _client_diagnostics_log()
    with _CLIENT_DIAGNOSTICS_LOCK:
        diagnostics_log.parent.mkdir(parents=True, exist_ok=True)
        with diagnostics_log.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def _read_only_mode() -> bool:
    """Disable every mutating HTTP method for contained deployments."""
    return (os.getenv("MISSION_CONTROL_READ_ONLY") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


_KNOWLEDGE_CORE_FILES = {"SOUL.md", "USER.md", "AGENTS.md"}
_KNOWLEDGE_EXCLUDED_FILENAMES = {"IDENTITY.md"}
# Vaults often contain checked-out projects. Never descend into generated or
# transient trees: they are both irrelevant to knowledge browsing and can
# change underneath us while a build is running (the source of the old 500s).
_KNOWLEDGE_SKIPPED_DIRS = {
    ".git", ".obsidian", ".agents", ".cache", ".pytest_cache", ".trash", ".turbo",
    "node_modules", "dist", "build", "target", "__pycache__", "venv", ".venv",
}


def _knowledge_vault_root() -> Path:
    """Canonical vault-path resolver.

    Resolution order:
    1. ``MISSION_CONTROL_VAULT_PATH`` (canonical).
    2. ``HERMES_OBSIDIAN_VAULT`` (legacy alias, kept for compatibility).
    3. Platform default: ``~/Documents/Hermes`` on macOS, ``~/wiki`` on Linux.

    The same resolved path is used for scanning, display, fallback payloads,
    and file reads.
    """
    return hermes_vault_dir()


def _knowledge_core_root() -> Path:
    return resolve_hermes_home().resolve()


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _home_relative(path: Path) -> str:
    """Render a path relative to the user's home directory (``~/...``)."""
    try:
        relative = path.resolve().relative_to(Path.home().resolve()).as_posix()
    except ValueError:
        return str(path.resolve())
    return f"~/{relative}" if relative else "~"


def _display_knowledge_path(path: Path) -> str:
    """Home-relative display path for API responses (no absolute usernames).

    The vault root is rendered from its real location (``~/Documents/Hermes``
    on macOS, ``~/wiki`` on Linux, or wherever ``MISSION_CONTROL_VAULT_PATH``
    points), so a configured vault never shows a fabricated macOS path.
    """
    resolved = path.resolve()
    home_root = Path.home().resolve()
    vault_root = _knowledge_vault_root().resolve()
    core_root = _knowledge_core_root().resolve()

    if _path_is_within(resolved, vault_root):
        relative = resolved.relative_to(vault_root).as_posix()
        vault_display = _home_relative(vault_root)
        if not relative or relative == ".":
            return vault_display
        return f"{vault_display}/{relative}"
    if _path_is_within(resolved, core_root):
        return display_home_path(resolved)
    if _path_is_within(resolved, home_root):
        relative = resolved.relative_to(home_root).as_posix()
        return f"~/{relative}" if relative else "~"
    return str(resolved)


def _isoformat_mtime(path: Path) -> Optional[str]:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()
    except OSError:
        return None


def _read_text_file(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _is_allowed_vault_note(path: Path, vault_root: Path) -> bool:
    try:
        if path.name in _KNOWLEDGE_EXCLUDED_FILENAMES or path.suffix.lower() != ".md":
            return False
        relative = path.resolve().relative_to(vault_root.resolve())
    except (OSError, ValueError):
        # Files can disappear while Obsidian/build tooling is changing the
        # vault. A vanished entry is not a server failure.
        return False
    parents = relative.parts[:-1]
    return not any(part.startswith(".") or part in _KNOWLEDGE_SKIPPED_DIRS for part in parents)


def _find_knowledge_markdown_files(vault_root: Path) -> list[Path]:
    """Walk the vault defensively, without ``Path.rglob``.

    ``rglob`` eagerly descends into every directory and lets a transient
    FileNotFoundError/InterruptedError escape when a project build changes the
    tree. The telemetry request must instead skip unreadable/vanishing
    entries and return the stable notes it was able to collect.
    """
    results: list[Path] = []
    pending = [vault_root]
    while pending:
        current = pending.pop()
        try:
            with os.scandir(current) as entries:
                child_dirs: list[Path] = []
                for entry in entries:
                    try:
                        entry_path = Path(entry.path)
                        if entry.is_dir(follow_symlinks=False):
                            if not entry.name.startswith(".") and entry.name not in _KNOWLEDGE_SKIPPED_DIRS:
                                child_dirs.append(entry_path)
                        elif entry.is_file(follow_symlinks=False) and entry.name.lower().endswith(".md"):
                            if _is_allowed_vault_note(entry_path, vault_root):
                                results.append(entry_path)
                    except (OSError, UnicodeError):
                        continue
                pending.extend(child_dirs)
        except (OSError, UnicodeError):
            continue
    return results


def _is_allowed_knowledge_file(path: Path) -> bool:
    resolved = path.resolve()
    vault_root = _knowledge_vault_root().resolve()
    core_root = _knowledge_core_root().resolve()
    if _path_is_within(resolved, vault_root):
        return _is_allowed_vault_note(resolved, vault_root)
    if _path_is_within(resolved, core_root):
        return resolved.name in _KNOWLEDGE_CORE_FILES and resolved.parent == core_root and resolved.suffix.lower() == ".md"
    return False


def _extract_markdown_title(content: str, fallback: str) -> str:
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            title = stripped.lstrip("#").strip()
            if title:
                return title
    for line in content.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:120]
    return fallback


def _extract_markdown_excerpt(content: str) -> str:
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith(("- ", "* ")):
            stripped = stripped[2:].strip()
        return stripped[:240]
    return ""


def _extract_markdown_highlights(content: str, limit: int = 4) -> list[str]:
    highlights: list[str] = []
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(("- ", "* ")):
            bullet = stripped[2:].strip()
            if bullet:
                highlights.append(bullet[:160])
        elif stripped.startswith("##"):
            heading = stripped.lstrip("#").strip()
            if heading:
                highlights.append(heading[:160])
        if len(highlights) >= limit:
            break
    return highlights


def _build_knowledge_item(
    path: Path,
    *,
    section_id: str,
    root: Optional[Path] = None,
    full_preview: bool = False,
) -> Dict[str, Any]:
    content = _read_text_file(path)
    title = _extract_markdown_title(content, path.stem)
    excerpt = _extract_markdown_excerpt(content)
    relative_path = path.name
    if root is not None and _path_is_within(path, root):
        relative_path = path.relative_to(root).as_posix()
    preview = content if full_preview else (content[:800] if content else excerpt)
    return {
        "id": f"{section_id}:{relative_path}",
        "title": title,
        "path": relative_path,
        "sourcePath": _display_knowledge_path(path),
        "updatedAt": _isoformat_mtime(path),
        "excerpt": excerpt,
        "highlights": _extract_markdown_highlights(content),
        "contentPreview": preview,
    }


def collect_knowledge_snapshot() -> Dict[str, Any]:
    vault_root = _knowledge_vault_root().resolve()
    sections: list[Dict[str, Any]] = []
    all_items: list[Dict[str, Any]] = []

    core_root = _knowledge_core_root()
    core_candidates = [
        ("soul", display_home_path(core_root / "SOUL.md"), core_root / "SOUL.md"),
        ("user", display_home_path(core_root / "USER.md"), core_root / "USER.md"),
        ("agents", display_home_path(core_root / "AGENTS.md"), core_root / "AGENTS.md"),
    ]
    for section_id, title, file_path in core_candidates:
        items: list[Dict[str, Any]] = []
        if file_path.exists() and file_path.is_file():
            try:
                items.append(_build_knowledge_item(file_path, section_id=section_id, full_preview=True))
            except OSError:
                pass
        sections.append({"id": section_id, "title": title, "items": items})
        all_items.extend(items)

    vault_items: list[Dict[str, Any]] = []
    if vault_root.exists() and vault_root.is_dir():
        def _sort_key(path: Path) -> tuple[int, float, str]:
            priority = 0 if path.name == "Knowledge Sharing.md" else 1
            try:
                mtime = -path.stat().st_mtime
            except OSError:
                mtime = 0.0
            return (priority, mtime, path.as_posix())

        vault_candidates = _find_knowledge_markdown_files(vault_root)
        vault_candidates.sort(key=_sort_key)
        try:
            max_items = max(1, min(int(os.environ.get("MISSION_CONTROL_KNOWLEDGE_MAX_FILES", "80")), 500))
        except (TypeError, ValueError):
            max_items = 80
        selected_candidates = vault_candidates[:max_items]
        knowledge_sharing = next((path for path in vault_candidates if path.name == "Knowledge Sharing.md"), None)
        if knowledge_sharing is not None and knowledge_sharing not in selected_candidates:
            selected_candidates = [knowledge_sharing, *selected_candidates[:-1]] if selected_candidates else [knowledge_sharing]

        for path in selected_candidates:
            try:
                vault_items.append(_build_knowledge_item(path, section_id="vault-notes", root=vault_root))
            except OSError:
                continue

    sections.append({"id": "vault-notes", "title": "Vault knowledge", "items": vault_items})
    all_items.extend(vault_items)

    primary = next((item for item in vault_items if item["path"] == "Knowledge Sharing.md"), None)
    if primary is None and all_items:
        primary = all_items[0]
    if primary is None:
        primary = {
            "id": "knowledge-sharing",
            "title": "Knowledge Sharing",
            "path": "Knowledge Sharing.md",
            "sourcePath": _display_knowledge_path(vault_root / "Knowledge Sharing.md"),
            "updatedAt": None,
            "excerpt": "Create shared vault notes to surface them here.",
            "highlights": [],
            "contentPreview": "",
        }

    return {
        "available": bool(all_items),
        "vaultPath": _display_knowledge_path(vault_root),
        "title": primary["title"],
        "path": primary["path"],
        "updatedAt": primary.get("updatedAt"),
        "excerpt": primary.get("excerpt", ""),
        "highlights": primary.get("highlights", []),
        "primary": primary,
        "items": all_items,
        "sections": sections,
    }


def _resolve_knowledge_request_path(requested_path: str) -> Path:
    raw = (requested_path or "").strip()
    if not raw:
        raise FileNotFoundError("missing path")

    vault_root = _knowledge_vault_root().resolve()
    core_root = _knowledge_core_root().resolve()
    vault_prefix = _display_knowledge_path(vault_root)
    core_home = resolve_hermes_home()
    core_display = display_home_path(core_home)
    root_display = display_home_path(hermes_root())
    if raw == vault_prefix:
        candidate = vault_root
    elif raw.startswith(vault_prefix + "/"):
        candidate = vault_root / raw[len(vault_prefix) + 1:]
    elif raw == core_display:
        candidate = core_root
    elif raw.startswith(core_display + "/"):
        candidate = core_home / raw[len(core_display) + 1:]
    elif raw == root_display:
        candidate = hermes_root()
    elif raw.startswith(root_display + "/"):
        candidate = hermes_root() / raw[len(root_display) + 1:]
    elif raw.startswith("~/"):
        candidate = Path.home() / raw[2:]
    else:
        candidate = Path(os.path.expanduser(raw))

    resolved = candidate.resolve(strict=True)
    if not _path_is_within(resolved, vault_root) and not _path_is_within(resolved, core_root):
        raise PermissionError(raw)
    if not resolved.is_file():
        raise FileNotFoundError(raw)
    if not _is_allowed_knowledge_file(resolved):
        raise PermissionError(raw)
    return resolved


def collect_knowledge_file_payload(requested_path: str) -> Dict[str, Any]:
    file_path = _resolve_knowledge_request_path(requested_path)
    content = _read_text_file(file_path)
    vault_root = _knowledge_vault_root().resolve()
    relative_path = file_path.relative_to(vault_root).as_posix() if _path_is_within(file_path, vault_root) else file_path.name
    return {
        "success": True,
        "title": _extract_markdown_title(content, file_path.stem),
        "path": relative_path,
        "sourcePath": _display_knowledge_path(file_path),
        "updatedAt": _isoformat_mtime(file_path),
        "excerpt": _extract_markdown_excerpt(content),
        "highlights": _extract_markdown_highlights(content),
        "content": content,
        "contentLength": len(content),
    }


def _parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _parse_int(value: str | None, default: int, minimum: int | None = None, maximum: int | None = None) -> int:
    try:
        parsed = int(value) if value is not None else default
    except Exception:
        parsed = default
    if minimum is not None:
        parsed = max(minimum, parsed)
    if maximum is not None:
        parsed = min(maximum, parsed)
    return parsed


def _parse_float(value: str | None, default: float, minimum: float | None = None, maximum: float | None = None) -> float:
    try:
        parsed = float(value) if value is not None else default
    except Exception:
        parsed = default
    if minimum is not None:
        parsed = max(minimum, parsed)
    if maximum is not None:
        parsed = min(maximum, parsed)
    return parsed


def _get_hermes_home() -> Path:
    return resolve_hermes_home()


def _read_runtime_status() -> Optional[Dict[str, Any]]:
    path = _get_hermes_home() / 'runtime_status.json'
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def _read_version() -> str:
    try:
        init_file = _get_hermes_home() / 'hermes-agent' / 'hermes_cli' / '__init__.py'
        if not init_file.exists():
            return '0.0.0'
        text = init_file.read_text()
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith('__version__'):
                parts = stripped.split('=', 1)
                if len(parts) == 2:
                    return parts[1].strip().strip(chr(34) + chr(39))
    except Exception:
        pass
    return '0.0.0'


def _candidates_enabled() -> bool:
    """Curate (BDH candidate curation) is an OPTIONAL feature. It is compiled into
    MC but only exposed when MC_ENABLE_BDH_CURATOR is truthy. Default OFF so the
    public Mission Control repo ships clean without the private nightly-brain
    dependency. The real logic lives in the bdh-nightly-brain sidecar."""
    return (os.getenv("MC_ENABLE_BDH_CURATOR") or "").strip().lower() in ("1", "true", "yes")


_STATUS_CACHE_LOCK = threading.Lock()
_STATUS_CACHE: tuple[float, Dict[str, Any]] | None = None
_STATUS_CACHE_TTL_SECONDS = 2.0


def _collect_status_payload_uncached() -> Dict[str, Any]:
    version = _read_version()
    runtime = _read_runtime_status()
    gateway_pid = None
    gateway_running = False
    gateway_state = None
    gateway_platforms: Dict[str, Any] = {}
    gateway_exit_reason = None
    gateway_updated_at = None

    try:
        pid_file = _get_hermes_home() / 'gateway.pid'
        if pid_file.exists():
            raw = pid_file.read_text().strip()
            if raw:
                try:
                    data = json.loads(raw)
                    candidate = data.get('pid') if isinstance(data, dict) else int(raw)
                except (json.JSONDecodeError, ValueError):
                    candidate = int(raw)
                if candidate and psutil.pid_exists(candidate):
                    gateway_pid = candidate
                    gateway_running = True
    except Exception:
        pass

    if runtime:
        gateway_state = runtime.get('gateway_state')
        gateway_platforms = runtime.get('platforms') or {}
        gateway_exit_reason = runtime.get('exit_reason')
        gateway_updated_at = runtime.get('updated_at')
        if not gateway_running:
            gateway_state = gateway_state if gateway_state in ('stopped', 'startup_failed') else 'stopped'
            gateway_platforms = {}

    active_sessions = 0
    try:
        sessions_dir = _get_hermes_home() / 'sessions'
        if sessions_dir.exists():
            now = time.time()
            for entry in os.scandir(sessions_dir):
                if entry.is_file() and entry.name.endswith('.json'):
                    try:
                        data = json.loads(open(entry.path).read())
                        ended = data.get('ended_at')
                        last_active = max(
                            data.get('last_active', 0),
                            data.get('started_at', 0),
                        )
                        if ended is None and (now - last_active) < 300:
                            active_sessions += 1
                    except Exception:
                        pass
    except Exception:
        pass

    return {
        'version': version,
        'gateway_running': gateway_running,
        'gateway_pid': gateway_pid,
        'gateway_state': gateway_state,
        'gateway_platforms': gateway_platforms,
        'gateway_exit_reason': gateway_exit_reason,
        'gateway_updated_at': gateway_updated_at,
        'active_sessions': active_sessions,
        'candidates_enabled': _candidates_enabled(),
    }


def _collect_status_payload() -> Dict[str, Any]:
    """Return a short-lived status snapshot without duplicate filesystem scans."""
    global _STATUS_CACHE
    now = time.monotonic()
    with _STATUS_CACHE_LOCK:
        if _STATUS_CACHE and now - _STATUS_CACHE[0] < _STATUS_CACHE_TTL_SECONDS:
            return _STATUS_CACHE[1]
        payload = _collect_status_payload_uncached()
        _STATUS_CACHE = (time.monotonic(), payload)
        return payload


def _collect_model_info() -> Dict[str, Any]:
    config_path = _get_hermes_home() / 'config.yaml'
    default = {
        'provider': 'unknown',
        'model': 'unknown',
        'base_url': None,
        'has_api_key': False,
        'context_length': None,
    }
    if not config_path.exists():
        return default
    try:
        import yaml
        text = config_path.read_text()
        config = yaml.safe_load(text) or {}
        model = config.get('model') or {}
        env_path = _get_hermes_home() / '.env'
        has_key = False
        if env_path.exists():
            env_text = env_path.read_text()
            for kw in ['API_KEY', 'HF_TOKEN', 'TOKEN']:
                if kw in env_text:
                    has_key = True
                    break
        return {
            'provider': model.get('provider', 'unknown'),
            'model': model.get('default', 'unknown'),
            'base_url': model.get('base_url'),
            'has_api_key': has_key,
            'context_length': model.get('context_length'),
        }
    except Exception:
        return default


def _collect_cron_jobs() -> list[Dict[str, Any]]:
    """Return the lightweight core cron inventory for frequent polling."""
    try:
        return cron_bridge_mod.list_jobs(include_disabled=True, include_output=False)
    except CronBridgeError:
        # Keep the dashboard readable if the core is temporarily unavailable.
        # The raw file is still useful for diagnostics and does not mutate state.
        jobs_path = _get_hermes_home() / 'cron' / 'jobs.json'
        if not jobs_path.exists():
            return []
        try:
            data = json.loads(jobs_path.read_text(encoding='utf-8'))
            jobs = data.get('jobs', []) if isinstance(data, dict) else []
            return jobs if isinstance(jobs, list) else []
        except (OSError, TypeError, ValueError):
            return []


def _read_config_snapshot() -> Dict[str, Any]:
    config_path = _get_hermes_home() / 'config.yaml'
    if not config_path.exists():
        return {'hash': None, 'size': 0, 'content': '', 'config': {}, 'path': str(config_path), 'updated_at': None}
    text = config_path.read_text()
    h = hashlib.sha256(text.encode('utf-8')).hexdigest()
    parsed_config: Dict[str, Any] = {}
    try:
        import yaml
        parsed = yaml.safe_load(text)
        if isinstance(parsed, dict):
            parsed_config = parsed
    except Exception:
        pass
    return {
        'hash': h,
        'size': len(text),
        'content': text,
        'config': parsed_config,
        'path': str(config_path),
        'updated_at': _isoformat_mtime(config_path),
    }


def _atomic_write_text(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + '.tmp')
    fd = None
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if tmp.exists():
            try:
                os.unlink(tmp)
            except OSError:
                pass
        raise





def _collect_tools() -> Dict[str, Any]:
    """Build a tools snapshot from what we can discover locally."""
    toolsets_list: list[Dict[str, Any]] = []
    tool_catalog: list[Dict[str, Any]] = []
    resolved: list[str] = []

    project_root = hermes_core_dir()
    tc_path = project_root / "hermes_cli" / "tools_config.py"

    try:
        if tc_path.exists():
            tc_text = tc_path.read_text()
            m = re.search(r"CONFIGURABLE_TOOLSETS\s*=\s*\[", tc_text)
            if m:
                brace_start = tc_text.find("[", m.start())
                brace_end = brace_start + 1
                depth = 1
                while brace_end < len(tc_text) and depth > 0:
                    if tc_text[brace_end] == "[": depth += 1
                    elif tc_text[brace_end] == "]": depth -= 1
                    brace_end += 1
                raw = tc_text[brace_start + 1:brace_end - 1]
                # Parse entries with simple regex
                entries = re.findall(r'\(\s*"([^"]+)"\s*,\s*"([^"]+)"\s*,\s*"([^"]+)"[^)]*\)', raw)
                for name, label, desc in entries:
                    direct = [name] if name != "skills" else ["list", "view", "manage"]
                    toolsets_list.append({
                        "name": name,
                        "description": label or desc,
                        "directTools": direct,
                        "includes": [],
                        "resolvedTools": direct,
                        "toolCount": len(direct),
                        "isComposite": False,
                        "available": True,
                        "requirements": [],
                    })
                    for d in direct:
                        tool_catalog.append({"name": d, "toolset": name, "available": True})
                        resolved.append(d)
    except Exception:
        pass

    if not toolsets_list:
        toolsets_list = [
            {"name": "terminal", "description": "Terminal", "directTools": ["terminal"], "includes": [], "resolvedTools": ["terminal"], "toolCount": 1, "isComposite": False, "available": True, "requirements": []},
            {"name": "file", "description": "File", "directTools": ["file"], "includes": [], "resolvedTools": ["file"], "toolCount": 1, "isComposite": False, "available": True, "requirements": []},
        ]
        tool_catalog = [{"name": "terminal", "toolset": "terminal", "available": True}, {"name": "file", "toolset": "file", "available": True}]
        resolved = ["terminal", "file"]

    return {
        "available": True,
        "count": len(toolsets_list),
        "toolCount": len(tool_catalog),
        "toolsets": toolsets_list,
        "availableToolsets": toolsets_list,
        "toolCatalog": tool_catalog,
        "resolvedTools": list(dict.fromkeys(resolved)),
    }

def _parse_skill_yaml_frontmatter(text: str) -> Dict[str, Any]:
    """Extract YAML frontmatter between --- and --- from a SKILL.md file."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    end_idx = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end_idx = i
            break
    if end_idx is None:
        return {}
    yaml_text = "\n".join(lines[1:end_idx])
    try:
        import yaml
        data = yaml.safe_load(yaml_text)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


_SKILLS_EXCLUDED_DIRS = {".archive", ".hub", ".curator_backups", ".git"}


def _find_skill_md_files(skills_dir: Path) -> list[Path]:
    """Recursively find all SKILL.md files, excluding special directories."""
    results = []
    try:
        for entry in skills_dir.rglob("SKILL.md"):
            # Skip if any parent directory is excluded
            rel = entry.relative_to(skills_dir)
            parts = rel.parts[:-1]  # all parts except the filename
            if any(p.startswith(".") or p in _SKILLS_EXCLUDED_DIRS for p in parts):
                continue
            results.append(entry)
    except Exception:
        pass
    return results


_SKILLS_CACHE_LOCK = threading.Lock()
_SKILLS_CACHE: tuple[float, Dict[str, Any]] | None = None
_SKILLS_CACHE_TTL_SECONDS = 5.0
_SKILL_INSTALL_LOCK = threading.Lock()


def _collect_skills_uncached() -> Dict[str, Any]:
    """Scan ~/.hermes/skills/ to build an installed skills snapshot.

    Recursively discovers all SKILL.md files, parses their YAML frontmatter
    to extract name, description, tags, and computes category from the
    first-level subdirectory.

    Reads skills.disabled (and skills.platform_disabled for the local
    platform) from config.yaml to determine each skill's enabled state.
    """
    skills_dir = hermes_skills_dir()

    # Read disabled skill names from config.yaml
    disabled_names: set[str] = set()
    config_path = _get_hermes_home() / "config.yaml"
    if config_path.exists():
        try:
            import yaml
            text = config_path.read_text()
            cfg = yaml.safe_load(text) or {}
            skills_cfg = cfg.get("skills", {})
            disabled_names = set(skills_cfg.get("disabled", []))
            # Also include platform_disabled for 'local'
            platform_cfg = skills_cfg.get("platform_disabled", {})
            if isinstance(platform_cfg, dict):
                local_disabled = platform_cfg.get("local")
                if local_disabled and isinstance(local_disabled, list):
                    disabled_names |= set(local_disabled)
        except Exception:
            pass

    skills_list: list[Dict[str, Any]] = []
    categories_map: Dict[str, list[str]] = {}

    if skills_dir.exists():
        skill_md_files = _find_skill_md_files(skills_dir)

        for md_path in skill_md_files:
            rel = md_path.relative_to(skills_dir)
            parts = rel.parts[:-1]  # everything except "SKILL.md"

            # Compute category: first subdirectory level, or "general"
            category = parts[0] if parts else "general"

            # Parse YAML frontmatter
            try:
                text = md_path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                text = ""
            frontmatter = _parse_skill_yaml_frontmatter(text) if text else {}

            # Extract name from YAML, fallback to the leaf directory name
            skill_name = str(frontmatter.get("name", "") or "")
            if not skill_name:
                skill_name = parts[-1] if parts else "unknown"

            # Extract description from YAML, fallback to empty
            desc = str(frontmatter.get("description", "") or "")

            # Extract tags from YAML metadata.hermes.tags
            tags: list[str] = []
            metadata = frontmatter.get("metadata")
            if isinstance(metadata, dict):
                hermes_meta = metadata.get("hermes")
                if isinstance(hermes_meta, dict):
                    raw_tags = hermes_meta.get("tags", [])
                    if isinstance(raw_tags, list):
                        tags = [str(t) for t in raw_tags]

            # Build the display path (relative to skills dir without filename)
            rel_dir = "/".join(parts) if parts else category

            skills_list.append({
                "id": skill_name,
                "name": skill_name,
                "description": desc or skill_name,
                "enabled": skill_name not in disabled_names,
                "model": "",
                "tags": tags,
                "category": category,
                "filePath": str(md_path.parent),
            })

            # Track in categories_map
            if category not in categories_map:
                categories_map[category] = []
            categories_map[category].append(skill_name)

    categories = []
    for name, skill_names in sorted(categories_map.items()):
        categories.append({
            "name": name,
            "description": f"Skills in {name}",
            "count": len(skill_names),
            "skills": skill_names,
        })

    return {
        "available": True,
        "count": len(skills_list),
        "hint": None,
        "skills": skills_list,
        "categories": categories,
    }


def _collect_skills() -> Dict[str, Any]:
    """Return a short-lived skills snapshot without duplicate YAML scans."""
    global _SKILLS_CACHE
    now = time.monotonic()
    with _SKILLS_CACHE_LOCK:
        if _SKILLS_CACHE and now - _SKILLS_CACHE[0] < _SKILLS_CACHE_TTL_SECONDS:
            return _SKILLS_CACHE[1]
        payload = _collect_skills_uncached()
        _SKILLS_CACHE = (time.monotonic(), payload)
        return payload


def _collect_skills_catalog(query: str = "", source: str = "all", limit: int = 500) -> Dict[str, Any]:
    """Collect available skills from the published Hermes Skills Hub index."""
    installed_snapshot = _collect_skills()
    installed_names = {str(skill.get("name") or skill.get("id") or "").lower() for skill in installed_snapshot.get("skills", [])}

    try:
        with urllib.request.urlopen("https://hermes-agent.nousresearch.com/docs/api/skills-index.json", timeout=12) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        return {
            "available": False,
            "count": 0,
            "hint": f"Skills catalog unavailable: {exc}",
            "skills": [],
            "sources": {},
            "timedOut": [],
        }

    raw_items = payload.get("skills", []) if isinstance(payload, dict) else []
    normalized_query = (query or "").strip().lower()
    source_filter = (source or "all").strip().lower()
    requested_limit = max(1, min(int(limit), 5000))
    source_counts: Dict[str, int] = {}
    items = []

    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        item_source = str(raw.get("source") or "unknown")
        source_counts[item_source] = source_counts.get(item_source, 0) + 1
        if source_filter != "all" and item_source.lower() != source_filter:
            continue

        name = str(raw.get("name") or "")
        identifier = str(raw.get("identifier") or name)
        tags = raw.get("tags") if isinstance(raw.get("tags"), list) else []
        haystack = " ".join([
            name,
            str(raw.get("description") or ""),
            item_source,
            identifier,
            str(raw.get("repo") or ""),
            str(raw.get("path") or ""),
            *[str(tag) for tag in tags],
        ]).lower()
        if normalized_query and normalized_query not in haystack:
            continue

        items.append({
            "id": identifier,
            "name": name,
            "description": str(raw.get("description") or ""),
            "source": item_source,
            "identifier": identifier,
            "trustLevel": str(raw.get("trust_level") or raw.get("trustLevel") or "community"),
            "repo": raw.get("repo") or None,
            "path": raw.get("path") or None,
            "tags": [str(tag) for tag in tags],
            "installed": name.lower() in installed_names,
        })

    trust_rank = {"builtin": 0, "trusted": 1, "community": 2}
    items.sort(key=lambda item: (trust_rank.get(str(item.get("trustLevel")), 3), str(item.get("source")) != "official", str(item.get("name", "")).lower()))
    items = items[:requested_limit]

    return {
        "available": True,
        "count": len(items),
        "hint": "Available skills from the centralized Hermes Skills Hub index. Installed skills come from /api/local/skills.",
        "skills": items,
        "sources": source_counts,
        "timedOut": [],
    }


def _resolve_hermes_cli() -> Optional[Path]:
    """Resolve Hermes CLI independently of the LaunchAgent PATH.

    LaunchAgents intentionally start with a minimal PATH. The CLI installed by
    Hermes may still be present in ``~/.local/bin`` or the active core venv.
    """
    discovered = shutil.which("hermes")
    candidates = [Path(discovered)] if discovered else []
    home = _get_hermes_home()
    candidates.extend([
        home / "hermes-agent" / "venv" / "bin" / "hermes",
        Path.home() / ".local" / "bin" / "hermes",
        home / "bin" / "hermes",
        Path.home() / ".hermes" / "bin" / "hermes",
        Path("/opt/homebrew/bin/hermes"),
    ])
    for candidate in candidates:
        try:
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return candidate.resolve()
        except OSError:
            continue
    return None


def _install_catalog_skill(identifier: str) -> tuple[int, Dict[str, Any]]:
    """Install one exact entry from the published Skills Hub catalog.

    The identifier is never passed to a shell. Before invoking Hermes, it must
    match an entry returned by the same catalog source used by the UI. This
    keeps the sidecar from becoming an arbitrary command runner while still
    delegating installation semantics to the Hermes CLI.
    """
    requested = identifier.strip()
    if not requested or len(requested) > 500:
        return 400, {"success": False, "error": "invalid_identifier", "detail": "A valid skill identifier is required."}

    catalog = _collect_skills_catalog(query=requested, limit=5000)
    if not catalog.get("available"):
        return 503, {"success": False, "error": "catalog_unavailable", "detail": "The Skills Hub catalog is unavailable."}

    item = next(
        (
            candidate
            for candidate in catalog.get("skills", [])
            if isinstance(candidate, dict) and str(candidate.get("identifier") or "") == requested
        ),
        None,
    )
    if item is None:
        return 404, {"success": False, "error": "skill_not_in_catalog", "detail": "That skill is not present in the published catalog."}

    skill_name = str(item.get("name") or requested).strip()
    if bool(item.get("installed")):
        return 409, {"success": False, "error": "already_installed", "skillName": skill_name, "detail": "This skill is already installed."}

    if not _SKILL_INSTALL_LOCK.acquire(blocking=False):
        return 409, {"success": False, "error": "install_in_progress", "detail": "Another skill installation is already in progress."}

    try:
        hermes_bin = _resolve_hermes_cli()
        if not hermes_bin:
            return 503, {"success": False, "error": "hermes_cli_not_found", "detail": "The Hermes CLI is not available to the telemetry sidecar."}

        env = os.environ.copy()
        hermes_home = _get_hermes_home()
        env["HERMES_HOME"] = str(hermes_home)
        # Preserve the LaunchAgent environment but make the resolved CLI and
        # its venv helpers available to the child process as well.
        cli_dirs = [str(hermes_bin.parent), str(hermes_home / "hermes-agent" / "venv" / "bin"), str(Path.home() / ".local" / "bin")]
        env["PATH"] = ":".join(dict.fromkeys([*cli_dirs, env.get("PATH", "")]))
        try:
            completed = subprocess.run(
                [str(hermes_bin), "skills", "install", requested, "--yes"],
                cwd=str(hermes_home),
                env=env,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return 504, {"success": False, "error": "install_timeout", "skillName": skill_name, "detail": "Skill installation timed out."}
        except OSError as exc:
            return 503, {"success": False, "error": "install_start_failed", "skillName": skill_name, "detail": str(exc)[:240]}

        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "Skill installation failed.").strip()
            detail = detail.replace(str(_get_hermes_home()), display_home_path(_get_hermes_home()))[:500]
            return 422, {"success": False, "error": "install_failed", "skillName": skill_name, "detail": detail}

        global _SKILLS_CACHE
        with _SKILLS_CACHE_LOCK:
            _SKILLS_CACHE = None
        installed_snapshot = _collect_skills_uncached()
        installed_names = {
            str(skill.get("name") or skill.get("id") or "").strip().lower()
            for skill in installed_snapshot.get("skills", [])
            if isinstance(skill, dict)
        }
        if skill_name.lower() not in installed_names:
            return 500, {"success": False, "error": "install_unverified", "skillName": skill_name, "detail": "Hermes completed without a verifiable installed skill."}
        return 200, {"success": True, "skillName": skill_name, "identifier": requested, "installed": True, "verified": True}
    finally:
        _SKILL_INSTALL_LOCK.release()


_SKILL_FILE_READ_LIMIT = 200_000  # 200 KB max for a single file read


def _is_within_skills_dir(path: Path) -> bool:
    """Check that a resolved path is inside the active Hermes skills directory."""
    skills_root = hermes_skills_dir().resolve()
    try:
        path.resolve().relative_to(skills_root)
        return True
    except ValueError:
        return False


def _collect_skill_detail(skill_name: str) -> Dict[str, Any]:
    """Return the SKILL.md content and a file listing for a named skill.

    Searches ~/.hermes/skills/ for a SKILL.md whose frontmatter ``name``
    matches *skill_name* (case-insensitive).  Falls back to matching the
    leaf directory name.
    """
    skills_dir = hermes_skills_dir()
    if not skills_dir.exists():
        return {"success": False, "error": "skills_directory_not_found"}

    target = skill_name.strip().lower()
    matched_dir: Optional[Path] = None

    for md_path in _find_skill_md_files(skills_dir):
        try:
            text = md_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        fm = _parse_skill_yaml_frontmatter(text)
        fm_name = str(fm.get("name", "") or "").strip().lower()
        dir_name = md_path.parent.name.lower()
        if fm_name == target or dir_name == target:
            matched_dir = md_path.parent
            break

    if matched_dir is None:
        return {"success": False, "error": "skill_not_found", "detail": f"No skill named '{skill_name}'"}

    # Read SKILL.md content
    skill_md = matched_dir / "SKILL.md"
    skill_md_content = ""
    if skill_md.exists():
        try:
            raw = skill_md.read_text(encoding="utf-8", errors="replace")
            skill_md_content = raw[:_SKILL_FILE_READ_LIMIT]
        except Exception:
            skill_md_content = ""

    # List all files in the skill directory (non-recursive, skip hidden)
    files: list[Dict[str, Any]] = []
    try:
        for entry in sorted(matched_dir.iterdir(), key=lambda p: (not p.is_file(), p.name.lower())):
            if entry.name.startswith("."):
                continue
            file_info: Dict[str, Any] = {
                "name": entry.name,
                "isDir": entry.is_dir(),
            }
            if entry.is_file():
                try:
                    file_info["size"] = entry.stat().st_size
                except OSError:
                    file_info["size"] = 0
            files.append(file_info)
    except Exception:
        pass

    return {
        "success": True,
        "name": skill_name,
        "dirPath": str(matched_dir),
        "skillMdContent": skill_md_content,
        "files": files,
    }


def _read_skill_file(file_path_str: str) -> Dict[str, Any]:
    """Read a single file from inside ~/.hermes/skills/.

    *file_path_str* must be an absolute path or a path relative to the
    skills directory.  Directory-traversal is blocked.
    """
    candidate = Path(file_path_str)
    if not candidate.is_absolute():
        candidate = hermes_skills_dir() / candidate

    resolved = candidate.resolve()
    if not _is_within_skills_dir(resolved):
        return {"success": False, "error": "forbidden", "detail": "Path is outside skills directory."}
    if not resolved.exists():
        return {"success": False, "error": "not_found", "detail": f"File not found: {file_path_str}"}
    if resolved.is_dir():
        return {"success": False, "error": "is_directory", "detail": "Use the list endpoint for directories."}

    try:
        stat = resolved.stat()
        content = resolved.read_text(encoding="utf-8", errors="replace")[:_SKILL_FILE_READ_LIMIT]
        return {
            "success": True,
            "path": str(resolved),
            "name": resolved.name,
            "content": content,
            "size": stat.st_size,
        }
    except Exception as exc:
        return {"success": False, "error": "read_failed", "detail": str(exc)}


def _collect_skill_files_recursive(skill_name: str) -> Dict[str, Any]:
    """Return all files with their contents for a named skill (recursive)."""
    skills_dir = hermes_skills_dir()
    if not skills_dir.exists():
        raise FileNotFoundError("Skills directory not found")

    target = skill_name.strip().lower()
    matched_dir: Optional[Path] = None

    for md_path in _find_skill_md_files(skills_dir):
        try:
            text = md_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        fm = _parse_skill_yaml_frontmatter(text)
        fm_name = str(fm.get("name", "") or "").strip().lower()
        dir_name = md_path.parent.name.lower()
        if fm_name == target or dir_name == target:
            matched_dir = md_path.parent
            break

    if matched_dir is None:
        raise FileNotFoundError(f"Skill '{skill_name}' not found")

    base = matched_dir.resolve()
    files: list[Dict[str, Any]] = []

    for p in sorted(base.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(base)
        parts = rel.parts
        if any(part.startswith(".") or part == "node_modules" for part in parts):
            continue
        try:
            content = p.read_text(encoding="utf-8", errors="replace")[:_SKILL_FILE_READ_LIMIT]
        except Exception:
            content = ""
        try:
            size = p.stat().st_size
        except OSError:
            size = 0
        files.append({
            "name": str(rel),
            "path": str(rel),
            "size": size,
            "content": content,
        })

    return {
        "skill": skill_name,
        "path": str(base),
        "files": files,
    }


def _collect_logs(max_files: int = 10, max_lines: int = 160) -> Dict[str, Any]:
    """Read latest log files from the active Hermes logs directory."""
    logs_dir = hermes_logs_dir()
    files_list: list[Dict[str, Any]] = []
    total_entries = 0

    if logs_dir.exists():
        all_files = sorted(
            [f for f in logs_dir.iterdir() if f.is_file() and not f.name.startswith(".")],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        # Prioritize gateway and agent logs by boosting them when present.
        priority_names = {"gateway.log", "gateway.error.log", "agent.log", "dashboard-api.error.log", "mission-control-telemetry.error.log", "mission-control.error.log", "mission-control-client.log", "tui_gateway_crash.log"}
        priority_files = [f for f in all_files if f.name in priority_names]
        other_files = [f for f in all_files if f.name not in priority_names]
        merged = priority_files + other_files
        for log_file in merged[:max_files]:
            try:
                stat = log_file.stat()
                size_bytes = stat.st_size
                # Read last max_lines lines efficiently
                entries: list[Dict[str, Any]] = []
                try:
                    with open(log_file, "r", encoding="utf-8", errors="replace") as fh:
                        lines = fh.readlines()
                except Exception:
                    lines = []
                trimmed = lines[-max_lines:] if len(lines) > max_lines else lines
                for idx, raw_line in enumerate(trimmed, start=max(1, len(lines) - len(trimmed) + 1)):
                    line = raw_line.rstrip("\n\r")
                    level: str = "info"
                    lower = line.lower()
                    if "error" in lower or "exception" in lower or "traceback" in lower or "failed" in lower:
                        level = "error"
                    elif "warn" in lower or "warning" in lower or "deprecated" in lower:
                        level = "warn"
                    entries.append({"lineNumber": idx, "level": level, "text": line})
                files_list.append({
                    "name": log_file.name,
                    "path": str(log_file),
                    "updatedAt": _isoformat_mtime(log_file),
                    "sizeBytes": size_bytes,
                    "entryCount": len(entries),
                    "entries": entries,
                })
                total_entries += len(entries)
            except Exception:
                continue

    return {
        "available": True,
        "path": str(logs_dir),
        "fileCount": len(files_list),
        "totalEntries": total_entries,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "files": files_list,
    }


class Handler(BaseHTTPRequestHandler):
    def setup(self) -> None:  # noqa: D401
        """Set a finite socket deadline so abandoned clients cannot pin a thread."""
        super().setup()
        self.connection.settimeout(15.0)

    def handle(self) -> None:  # noqa: D401
        """Treat disconnected/idle clients as normal request termination."""
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            return

    def _cors_headers(self) -> Dict[str, str]:
        configured_origin = (os.getenv("MISSION_CONTROL_ALLOWED_ORIGIN") or "").strip()
        request_origin = (self.headers.get("Origin") or "").strip()
        if configured_origin:
            # Explicit allow-list mode: only the exact configured origin is
            # accepted. Any other Origin gets no CORS headers, so browsers
            # block the cross-origin response. Requests without an Origin
            # header (curl, same-origin fetches, non-browser clients) are not
            # subject to CORS and pass through untouched.
            if request_origin and request_origin == configured_origin:
                return {
                    "Access-Control-Allow-Origin": request_origin,
                    "Vary": "Origin",
                }
            return {}
        # Dev mode (no explicit configuration): mirror the incoming origin so
        # browser sidecars work across Tailscale/LAN without extra config.
        if request_origin:
            return {
                "Access-Control-Allow-Origin": request_origin,
                "Vary": "Origin",
            }
        return {}

    def _json(self, status: int, payload: Dict[str, Any], extra_headers: Optional[Dict[str, str]] = None) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        for key, value in self._cors_headers().items():
            self.send_header(key, value)
        if extra_headers:
            for key, value in extra_headers.items():
                self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _unauthorized(self) -> None:
        self._json(
            401,
            {"error": "invalid_api_key", "detail": "Mission Control local telemetry requires a bearer token."},
            extra_headers={"WWW-Authenticate": 'Bearer realm="Mission Control"'},
        )

    def _reject_mutation_in_read_only_mode(self) -> bool:
        if not _read_only_mode():
            return False
        self._json(
            403,
            {
                "error": "read_only_mode",
                "detail": "Mission Control mutations are disabled for this deployment.",
            },
        )
        return True

    def _read_json_body(self) -> Dict[str, Any] | None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8", errors="replace") if length else "{}"
        try:
            payload = json.loads(raw or "{}")
        except (ValueError, json.JSONDecodeError) as exc:
            self._json(400, {"error": "bad_request", "detail": f"invalid JSON: {exc}"})
            return None
        if not isinstance(payload, dict):
            self._json(400, {"error": "bad_request", "detail": "JSON payload must be an object."})
            return None
        return payload

    def _cron_error(self, exc: Exception) -> None:
        if isinstance(exc, CronBridgeError):
            self._json(exc.status_code, {"error": "cron_error", "detail": exc.message})
        else:
            self._json(500, {"error": "cron_failed", "detail": str(exc)[:240]})

    def _cron_post(self, parsed, payload: Dict[str, Any]) -> None:
        sub = parsed.path[len("/api/local/cron/"):].strip("/")
        try:
            if sub == "jobs":
                self._json(200, cron_bridge_mod.create_job(payload))
                return
            if sub.startswith("jobs/"):
                parts = sub[len("jobs/"):].split("/")
                if len(parts) == 2 and parts[1] in {"pause", "resume", "run"}:
                    job_id = urllib.parse.unquote(parts[0])
                    action = parts[1]
                    if action == "pause":
                        result = cron_bridge_mod.pause_job(job_id, reason=payload.get("reason"))
                    elif action == "resume":
                        result = cron_bridge_mod.resume_job(job_id)
                    else:
                        result = cron_bridge_mod.run_job(job_id)
                    self._json(200, result)
                    return
            self._json(404, {"error": "not_found", "path": parsed.path})
        except Exception as exc:
            self._cron_error(exc)

    def _cron_patch(self, parsed, payload: Dict[str, Any]) -> None:
        sub = parsed.path[len("/api/local/cron/"):].strip("/")
        if not sub.startswith("jobs/") or sub.count("/") != 1:
            self._json(404, {"error": "not_found", "path": parsed.path})
            return
        job_id = urllib.parse.unquote(sub[len("jobs/"):])
        try:
            self._json(200, cron_bridge_mod.update_job(job_id, payload))
        except Exception as exc:
            self._cron_error(exc)

    def _cron_delete(self, parsed) -> None:
        sub = parsed.path[len("/api/local/cron/"):].strip("/")
        if not sub.startswith("jobs/") or sub.count("/") != 1:
            self._json(404, {"error": "not_found", "path": parsed.path})
            return
        job_id = urllib.parse.unquote(sub[len("jobs/"):])
        try:
            self._json(200, cron_bridge_mod.delete_job(job_id))
        except Exception as exc:
            self._cron_error(exc)

    def _stream_chat_sync(self, session_id: str, client_id: str, since: Optional[int]) -> None:
        queue, replay, latest = chat_sync_relay.subscribe(session_id, client_id, since=since)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-store")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        for key, value in self._cors_headers().items():
            self.send_header(key, value)
        self.end_headers()

        def send_event(event_name: str, payload: Dict[str, Any]) -> None:
            frame = (
                f"event: {event_name}\n"
                f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
            ).encode("utf-8")
            self.wfile.write(frame)
            self.wfile.flush()

        try:
            send_event("chat-sync-ready", {
                "session_id": session_id,
                "client_id": client_id,
                "latest_seq": latest,
                "replayed": len(replay),
            })
            for item in replay:
                send_event("chat-sync", item)
            while True:
                item = chat_sync_relay.wait(queue)
                if item is None:
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
                else:
                    send_event("chat-sync", item)
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            return
        finally:
            chat_sync_relay.unsubscribe(session_id, client_id)

    def _chat_sync_post(self, payload: Dict[str, Any]) -> None:
        session_id = str(payload.get("session_id") or "").strip()
        client_id = str(payload.get("client_id") or "").strip()
        kind = str(payload.get("kind") or "").strip()
        if not session_id or not client_id or not kind:
            self._json(400, {"error": "bad_request", "detail": "session_id, client_id and kind are required."})
            return
        if kind == "gateway_event":
            event = payload.get("event")
            if not isinstance(event, dict):
                self._json(400, {"error": "bad_request", "detail": "gateway_event requires an event object."})
                return
            event = dict(event)
            event.setdefault("session_id", session_id)
            event_id = str(payload.get("event_id") or "").strip()
            dedupe_key = core_event_dedupe_key(session_id, event, event_id)
            relay_payload = event
        elif kind in {"user_message", "system_message"}:
            message = payload.get("message")
            expected_role = "user" if kind == "user_message" else "system"
            if not isinstance(message, dict) or not str(message.get("id") or "").strip() or message.get("role") != expected_role:
                self._json(400, {"error": "bad_request", "detail": f"{kind} requires a {expected_role} message with an id."})
                return
            message = dict(message)
            dedupe_key = (
                user_message_dedupe_key(session_id, str(message["id"]))
                if kind == "user_message"
                else system_message_dedupe_key(session_id, str(message["id"]))
            )
            relay_payload = message
        else:
            self._json(400, {"error": "bad_request", "detail": f"Unsupported sync kind: {kind}."})
            return
        result = chat_sync_relay.publish(session_id, client_id, kind, relay_payload, dedupe_key)
        self._json(200, {"success": True, "relay": result})

    def _stream_trace(self, session_id: str | None, limit: int, compact: bool, interval: float) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache, no-store")
        self.send_header("Connection", "keep-alive")
        for key, value in self._cors_headers().items():
            self.send_header(key, value)
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        while True:
            payload = load_agent_trace_snapshot(session_id=session_id, limit=limit, compact=compact)
            frame = f"event: trace\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")
            try:
                self.wfile.write(frame)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                break
            time.sleep(interval)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        if parsed.path.startswith('/api/local/chat/canvas/'):
            # Generic canvas addon GET dispatcher
            # Path format: /api/local/chat/canvas/:addonId?sessionId=...&sessionKey=...
            if not _is_authorized(self):
                self._unauthorized()
                return
            addon_id = parsed.path.split('/api/local/chat/canvas/', 1)[1].strip('/')
            if not addon_id:
                self._json(400, {'error': 'bad_request', 'detail': 'Missing addon ID in path.'})
                return
            handler = CANVAS_DISPATCH.get(addon_id)
            if handler is None:
                self._json(400, {'error': 'bad_request', 'detail': f'Unknown canvas addon: {addon_id}'})
                return
            session_id = (params.get('sessionId') or [''])[0].strip()
            if not session_id:
                self._json(400, {'error': 'bad_request', 'detail': 'Missing sessionId.'})
                return
            try:
                response = handler(session_id, {
                    'action': 'get',
                    'sessionKey': (params.get('sessionKey') or [''])[0].strip(),
                })
                self._json(200, response)
            except ValueError as exc:
                self._json(400, {'error': 'bad_request', 'detail': str(exc)})
            except Exception:
                import logging
                logging.exception('Canvas addon GET handler error for %s', addon_id)
                self._json(500, {'error': 'internal_error', 'detail': 'Internal server error'})
            return
        if parsed.path == "/health":
            self._json(200, {
                "ok": True,
                "service": "mission-control-local-telemetry",
                "source": "local-psutil",
                "push": push_status(),
            })
            return
        if parsed.path == "/api/local/health":
            self._json(200, {
                "ok": True,
                "service": "mission-control-local-telemetry",
                "source": "local-psutil",
                "push": push_status(),
            })
            return
        if parsed.path == "/api/local/chat/sync/stream":
            if not _is_authorized(self, allow_query_token=True):
                self._unauthorized()
                return
            session_id = str((params.get("session_id") or [""])[0]).strip()
            client_id = str((params.get("client_id") or [""])[0]).strip()
            if not session_id or not client_id:
                self._json(400, {"error": "bad_request", "detail": "session_id and client_id are required."})
                return
            since_raw = (params.get("since") or [None])[0]
            since = None if since_raw in (None, "") else _parse_int(since_raw, default=0, minimum=0, maximum=2_000_000_000)
            self._stream_chat_sync(session_id, client_id, since)
            return
        if parsed.path == "/api/local/chat/sync/stats":
            if not _is_authorized(self):
                self._unauthorized()
                return
            self._json(200, chat_sync_relay.stats())
            return
        if parsed.path == "/api/local/system":
            if not _is_authorized(self):
                self._unauthorized()
                return
            self._json(200, collect_system_snapshot())
            return
        if parsed.path == "/api/local/knowledge":
            if not _is_authorized(self):
                self._unauthorized()
                return
            try:
                payload = collect_knowledge_snapshot()
            except (OSError, UnicodeError, ValueError) as exc:
                # The vault is user-editable and can change during the scan.
                # Return a valid response instead of killing the request thread.
                self._json(503, {
                    "error": "knowledge_temporarily_unavailable",
                    "detail": f"Knowledge vault changed or could not be read ({type(exc).__name__}).",
                })
                return
            self._json(200, payload)
            return
        if parsed.path == "/api/local/knowledge/file":
            if not _is_authorized(self):
                self._unauthorized()
                return
            requested_path = (params.get("path") or [""])[0]
            try:
                payload = collect_knowledge_file_payload(requested_path)
            except PermissionError:
                self._json(403, {"error": "forbidden", "detail": "Knowledge path is outside allowed roots."})
                return
            except FileNotFoundError:
                self._json(404, {"error": "not_found", "detail": "Knowledge file not found."})
                return
            except OSError as exc:
                self._json(503, {"error": "knowledge_file_temporarily_unavailable", "detail": f"Knowledge file could not be read ({type(exc).__name__})."})
                return
            self._json(200, payload)
            return
        if parsed.path == "/api/local/mission-control/agents":
            if not _is_authorized(self):
                self._unauthorized()
                return
            self._json(200, load_agents_snapshot())
            return
        if parsed.path == "/api/local/mission-control/sessions":
            if not _is_authorized(self):
                self._unauthorized()
                return
            limit = _parse_int((params.get("limit") or [None])[0], default=100, minimum=1, maximum=500)
            offset = _parse_int((params.get("offset") or [None])[0], default=0, minimum=0, maximum=100000)
            session_id = (params.get("session_id") or [None])[0] or None
            self._json(200, load_agents_sessions_snapshot(limit=limit, offset=offset, session_id=session_id, filters=_session_filter_params(params)))
            return
        if parsed.path == "/api/local/sessions":
            if not _is_authorized(self):
                self._unauthorized()
                return
            limit = _parse_int((params.get("limit") or [None])[0], default=100, minimum=1, maximum=500)
            offset = _parse_int((params.get("offset") or [None])[0], default=0, minimum=0, maximum=100000)
            self._json(200, load_agents_sessions_snapshot(limit=limit, offset=offset, filters=_session_filter_params(params)))
            return
        if parsed.path == "/api/local/sessions/usage":
            if not _is_authorized(self):
                self._unauthorized()
                return
            self._json(200, load_sessions_usage())
            return
        if parsed.path == "/api/local/provider-usage":
            if not _is_authorized(self):
                self._unauthorized()
                return
            self._json(200, collect_provider_usage())
            return
        if parsed.path.startswith("/api/local/kanban/"):
            if not _is_authorized(self):
                self._unauthorized()
                return
            self._kanban_get(parsed)
            return
        if parsed.path == "/api/local/mission-control/agents/trace":
            if not _is_authorized(self):
                self._unauthorized()
                return
            session_id = (params.get("session_id") or [None])[0] or None
            limit = _parse_int((params.get("limit") or [None])[0], default=300, minimum=0, maximum=1000)
            compact = _parse_bool((params.get("compact") or [None])[0], default=False)
            self._json(200, load_agent_trace_snapshot(session_id=session_id, limit=limit, compact=compact))
            return
        if parsed.path == "/api/local/mission-control/agents/trace/stream":
            if not _is_authorized(self, allow_query_token=True):
                self._unauthorized()
                return
            session_id = (params.get("session_id") or [None])[0] or None
            limit = _parse_int((params.get("limit") or [None])[0], default=300, minimum=0, maximum=1000)
            compact = _parse_bool((params.get("compact") or [None])[0], default=True)
            interval = _parse_float((params.get("interval") or [None])[0], default=2.0, minimum=0.5, maximum=30.0)
            self._stream_trace(session_id=session_id, limit=limit, compact=compact, interval=interval)
            return
        if parsed.path == '/api/local/status':
            if not _is_authorized(self):
                self._unauthorized()
                return
            self._json(200, _collect_status_payload())
            return
        if parsed.path == '/api/local/model/info':
            if not _is_authorized(self):
                self._unauthorized()
                return
            self._json(200, _collect_model_info())
            return
        if parsed.path == '/api/local/cron/jobs':
            if not _is_authorized(self):
                self._unauthorized()
                return
            jobs = _collect_cron_jobs()
            self._json(200, {'jobs': jobs, 'count': len(jobs)})
            return
        if parsed.path.startswith('/api/local/cron/jobs/'):
            if not _is_authorized(self):
                self._unauthorized()
                return
            job_id = urllib.parse.unquote(parsed.path[len('/api/local/cron/jobs/'):].strip('/'))
            try:
                self._json(200, cron_bridge_mod.get_job(job_id))
            except CronBridgeError as exc:
                self._json(exc.status_code, {'error': 'cron_error', 'detail': exc.message})
            except Exception as exc:
                self._json(500, {'error': 'cron_failed', 'detail': str(exc)[:240]})
            return
        if parsed.path == '/api/local/config':
            if not _is_authorized(self):
                self._unauthorized()
                return
            self._json(200, _read_config_snapshot())
            return
        if parsed.path == '/api/local/tools':
            if not _is_authorized(self):
                self._unauthorized()
                return
            self._json(200, _collect_tools())
            return
        if parsed.path == '/api/local/skills':
            if not _is_authorized(self):
                self._unauthorized()
                return
            self._json(200, _collect_skills())
            return
        if parsed.path == '/api/local/skills/catalog':
            if not _is_authorized(self):
                self._unauthorized()
                return
            query = urllib.parse.parse_qs(parsed.query)
            search_query = query.get('query', [''])[0]
            source = query.get('source', ['all'])[0] or 'all'
            try:
                limit = int(query.get('limit', ['5000'])[0])
            except (ValueError, IndexError):
                limit = 500
            self._json(200, _collect_skills_catalog(query=search_query, source=source, limit=limit))
            return
        if parsed.path == '/api/local/skills/files':
            if not _is_authorized(self):
                self._unauthorized()
                return
            skill_name = (params.get("skill") or [""])[0].strip()
            if not skill_name:
                self._json(400, {"error": "missing_skill", "detail": "Query parameter 'skill' is required."})
                return
            try:
                payload = _collect_skill_files_recursive(skill_name)
            except FileNotFoundError:
                self._json(404, {"error": "not_found", "detail": f"Skill '{skill_name}' not found."})
                return
            self._json(200, payload)
            return
        if parsed.path == '/api/local/logs':
            if not _is_authorized(self):
                self._unauthorized()
                return
            # Parse query params for optional limits
            query = urllib.parse.parse_qs(parsed.query)
            try:
                max_files = int(query.get('maxFiles', ['10'])[0])
            except (ValueError, IndexError):
                max_files = 10
            try:
                max_lines = int(query.get('maxLines', ['160'])[0])
            except (ValueError, IndexError):
                max_lines = 160
            self._json(200, _collect_logs(max_files=max_files, max_lines=max_lines))
            return
        if parsed.path == '/api/local/push/vapid-public-key':
            if not _is_authorized(self):
                self._unauthorized()
                return
            public_key = load_vapid_public_key()
            if not public_key:
                self._json(200, {'publicKey': None, 'enabled': False, 'status': push_status()})
                return
            self._json(200, {'publicKey': public_key, 'enabled': True, 'status': push_status()})
            return
        if parsed.path == '/api/local/push/subscriptions':
            if not _is_authorized(self):
                self._unauthorized()
                return
            self._json(200, {'subscriptions': list_subscriptions()})
            return
        if parsed.path == '/api/local/chat/last':
            if not _is_authorized(self):
                self._unauthorized()
                return
            self._json(200, {'lastChat': get_last_chat()})
            return
        if parsed.path == '/api/local/chat/whiteboard':
            if not _is_authorized(self):
                self._unauthorized()
                return
            session_id = (params.get('sessionId') or params.get('sessionKey') or [''])[0].strip()
            fallback_session_id = (params.get('sessionKey') or [''])[0].strip()
            if not session_id:
                self._json(400, {'error': 'bad_request', 'detail': 'Missing sessionId.'})
                return
            try:
                self._json(200, handle_tldraw(session_id, {
                    'action': 'get',
                    'sessionKey': fallback_session_id,
                }))
            except Exception:
                import logging
                logging.exception('Legacy whiteboard GET handler error for session %s', session_id)
                self._json(500, {'error': 'internal_error', 'detail': 'Internal server error'})
            return
        if parsed.path == '/api/local/candidates':
            if not _is_authorized(self):
                self._unauthorized()
                return
            if not _candidates_enabled():
                self._json(404, {'error': 'feature_disabled',
                                 'detail': 'BDH curator is disabled. Set MC_ENABLE_BDH_CURATOR=1 to enable.'})
                return
            status = (params.get("status") or [None])[0] or None
            vault = (params.get("vault") or [None])[0] or None
            cands = candidates_mod.list_candidates(status=status, vault=vault)
            self._json(200, {"candidates": cands, "count": len(cands), "vault": vault})
            return
        if parsed.path == '/api/local/candidates/vaults':
            if not _is_authorized(self):
                self._unauthorized()
                return
            if not _candidates_enabled():
                self._json(404, {'error': 'feature_disabled',
                                 'detail': 'BDH curator is disabled. Set MC_ENABLE_BDH_CURATOR=1 to enable.'})
                return
            self._json(200, {"vaults": candidates_mod.list_vaults()})
            return
        self._json(404, {"error": "not_found", "path": self.path})

    def do_PUT(self) -> None:  # noqa: N802
        if self._reject_mutation_in_read_only_mode():
            return
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == '/api/local/config':
            if not _is_authorized(self):
                self._unauthorized()
                return
            length = int(self.headers.get('Content-Length', 0))
            if length == 0:
                self._json(400, {'error': 'bad_request', 'detail': 'Empty body.'})
                return
            body = self.rfile.read(length).decode('utf-8')
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                self._json(400, {'error': 'bad_request', 'detail': 'Invalid JSON body.'})
                return
            content = data.get('content', '')
            expected_hash = data.get('hash', '')
            config_path = _get_hermes_home() / 'config.yaml'
            if config_path.exists():
                current_text = config_path.read_text()
                current_hash = hashlib.sha256(current_text.encode('utf-8')).hexdigest()
                if expected_hash and expected_hash != current_hash:
                    self._json(409, {'error': 'hash_mismatch', 'detail': 'Config changed since last read.', 'currentHash': current_hash})
                    return
            try:
                import yaml
                yaml.safe_load(content)
            except Exception as exc:
                self._json(400, {'error': 'invalid_yaml', 'detail': str(exc)})
                return
            if config_path.exists():
                backup = config_path.with_suffix(f'.yaml.bak.{int(time.time())}')
                try:
                    backup.write_text(config_path.read_text(), encoding='utf-8')
                except OSError:
                    pass
            try:
                _atomic_write_text(config_path, content)
            except OSError as exc:
                self._json(500, {'error': 'write_failed', 'detail': str(exc)})
                return
            new_hash = hashlib.sha256(content.encode('utf-8')).hexdigest()
            self._json(200, {'success': True, 'hash': new_hash, 'size': len(content), 'path': str(config_path)})
            return
        self._json(404, {'error': 'not_found', 'path': self.path})

    # ------------------------------------------------------------------
    # Kanban (delegates to server/kanban_bridge.py → hermes_cli.kanban_db)
    # ------------------------------------------------------------------

    def _kanban_get(self, parsed) -> None:
        params = urllib.parse.parse_qs(parsed.query)
        board = (params.get("board") or [None])[0] or None
        sub = parsed.path[len("/api/local/kanban/"):].strip("/")
        try:
            if sub == "board":
                self._json(200, kanban_bridge_mod.get_board(board=board))
                return
            if sub == "boards":
                include_archived = (params.get("include_archived") or ["false"])[0].lower() == "true"
                self._json(200, kanban_bridge_mod.list_boards(include_archived=include_archived))
                return
            if sub == "events":
                since_raw = (params.get("since") or ["0"])[0]
                try:
                    since = int(since_raw)
                except ValueError:
                    since = 0
                self._json(200, kanban_bridge_mod.get_events(since=since, board=board))
                return
            if sub.startswith("tasks/") and sub.endswith("/log"):
                task_id = sub[len("tasks/"):-len("/log")]
                try:
                    tail = int((params.get("tail") or ["100000"])[0])
                except ValueError:
                    tail = 100000
                self._json(200, kanban_bridge_mod.get_task_log(task_id, board=board, tail=max(1, min(tail, 2_000_000))))
                return
            if sub.startswith("tasks/"):
                task_id = sub[len("tasks/"):]
                if not task_id:
                    self._json(404, {"error": "not_found", "path": parsed.path})
                    return
                self._json(200, kanban_bridge_mod.get_task_detail(task_id, board=board))
                return
            self._json(404, {"error": "not_found", "path": parsed.path})
        except KanbanBridgeError as exc:
            self._json(exc.status_code, {"error": "kanban_error", "detail": exc.message})
        except Exception as exc:  # defensive: never kill the sidecar worker
            self._json(500, {"error": "kanban_failed", "detail": str(exc)[:240]})

    def _kanban_post(self, parsed, payload: Dict[str, Any]) -> None:
        params = urllib.parse.parse_qs(parsed.query)
        board = (params.get("board") or [None])[0] or None
        sub = parsed.path[len("/api/local/kanban/"):].strip("/")
        try:
            if sub == "tasks":
                author = (payload.get("author") or "mission-control")
                self._json(200, kanban_bridge_mod.create_task(payload, board=board, author=author))
                return
            if sub == "boards":
                self._json(200, kanban_bridge_mod.create_board(payload, switch=bool(payload.get("switch"))))
                return
            if sub.startswith("tasks/") and sub.endswith("/links"):
                task_id = sub[len("tasks/"):-len("/links")]
                parent_id = str(payload.get("parent_id") or "").strip()
                if not parent_id:
                    raise KanbanBridgeError(400, "parent_id is required")
                self._json(200, kanban_bridge_mod.link_task(parent_id, task_id, board=board, remove=bool(payload.get("remove"))))
                return
            if sub.startswith("tasks/") and sub.endswith("/comments"):
                task_id = sub[len("tasks/"):-len("/comments")]
                author = (payload.get("author") or "mission-control")
                self._json(200, kanban_bridge_mod.add_comment(task_id, payload, board=board, author=author))
                return
            if sub.startswith("tasks/") and sub.endswith("/archive"):
                task_id = sub[len("tasks/"):-len("/archive")]
                self._json(200, kanban_bridge_mod.delete_task(task_id, board=board))
                return
            if sub.startswith("tasks/"):
                task_id = sub[len("tasks/"):]
                self._json(200, kanban_bridge_mod.update_task(task_id, payload, board=board))
                return
            if sub.startswith("boards/") and sub.endswith("/switch"):
                slug = sub[len("boards/"):-len("/switch")]
                self._json(200, kanban_bridge_mod.switch_board(slug))
                return
            if sub.startswith("boards/") and sub.endswith("/delete"):
                slug = sub[len("boards/"):-len("/delete")]
                self._json(200, kanban_bridge_mod.delete_board(slug, hard=bool(payload.get("hard"))))
                return
            self._json(404, {"error": "not_found", "path": parsed.path})
        except KanbanBridgeError as exc:
            self._json(exc.status_code, {"error": "kanban_error", "detail": exc.message})
        except Exception as exc:  # defensive: never kill the sidecar worker
            self._json(500, {"error": "kanban_failed", "detail": str(exc)[:240]})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/local/terminal/ticket":
            if not _is_authorized(self):
                self._unauthorized()
                return
            token = _extract_bearer_token(self.headers.get("Authorization")) or ""
            ticket = issue_ticket(token)
            if not ticket:
                self._json(401, {"error": "unauthorized", "detail": "Invalid terminal credentials."})
                return
            self._json(200, {"ticket": ticket})
            return
        if parsed.path == "/api/local/client-diagnostics":
            payload = self._read_json_body()
            if payload is None:
                return
            supplied_token = str(payload.pop("_accessToken", "") or "").strip()
            expected_token = _resolve_access_token()
            body_authorized = bool(
                expected_token
                and supplied_token
                and hmac.compare_digest(supplied_token, expected_token)
            )
            if not _is_authorized(self) and not body_authorized:
                self._unauthorized()
                return
            _append_client_diagnostic(payload)
            self._json(200, {"success": True})
            return
        if self._reject_mutation_in_read_only_mode():
            return
        if parsed.path.startswith("/api/local/cron/"):
            if not _is_authorized(self):
                self._unauthorized()
                return
            payload = self._read_json_body()
            if payload is None:
                return
            self._cron_post(parsed, payload)
            return
        if parsed.path.startswith("/api/local/kanban/"):
            if not _is_authorized(self):
                self._unauthorized()
                return
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length).decode("utf-8", errors="replace") if length else "{}"
            try:
                payload = json.loads(raw or "{}")
                if not isinstance(payload, dict):
                    raise ValueError("payload must be a JSON object")
            except (ValueError, json.JSONDecodeError) as exc:
                self._json(400, {"error": "bad_request", "detail": f"invalid JSON: {exc}"})
                return
            self._kanban_post(parsed, payload)
            return
        if parsed.path == '/api/local/chat/sync/publish':
            if not _is_authorized(self):
                self._unauthorized()
                return
            payload = self._read_json_body()
            if payload is None:
                return
            try:
                self._chat_sync_post(payload)
            except ValueError as exc:
                self._json(400, {'error': 'bad_request', 'detail': str(exc)})
            except Exception as exc:  # defensive: relay must not kill the sidecar worker
                self._json(500, {'error': 'chat_sync_failed', 'detail': str(exc)[:240]})
            return
        if parsed.path == '/api/local/gateway/restart':
            if not _is_authorized(self):
                self._unauthorized()
                return
            # Attempt restart; don't crash if hermes CLI isn't available.
            try:
                subprocess.Popen(
                    ['hermes', 'gateway', 'restart'],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            except FileNotFoundError:
                self._json(200, {'success': False, 'detail': 'hermes CLI not found on PATH.', 'manual': True})
                return
            except Exception as exc:
                self._json(200, {'success': False, 'detail': str(exc), 'manual': True})
                return
            self._json(202, {'success': True, 'detail': 'Gateway restart initiated.'})
            return
        if parsed.path == '/api/local/chat/last':
            if not _is_authorized(self):
                self._unauthorized()
                return
            length = int(self.headers.get('Content-Length', 0))
            if length == 0:
                self._json(400, {'error': 'bad_request', 'detail': 'Empty body.'})
                return
            body = self.rfile.read(length).decode('utf-8')
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                self._json(400, {'error': 'bad_request', 'detail': 'Invalid JSON body.'})
                return
            if not str(data.get('sessionId', '')).strip():
                self._json(400, {'error': 'bad_request', 'detail': 'Missing sessionId.'})
                return
            saved = set_last_chat(data)
            self._json(200, {'success': True, 'lastChat': saved})
            return
        if parsed.path == '/api/local/chat/whiteboard':
            if not _is_authorized(self):
                self._unauthorized()
                return
            length = int(self.headers.get('Content-Length', 0))
            try:
                data = json.loads(self.rfile.read(length).decode('utf-8')) if length else {}
            except json.JSONDecodeError:
                self._json(400, {'error': 'bad_request', 'detail': 'Invalid JSON body.'})
                return
            session_id = str(data.get('sessionId') or data.get('sessionKey') or '').strip()
            fallback_session_id = str(data.get('sessionKey') or '').strip()
            if not session_id:
                self._json(400, {'error': 'bad_request', 'detail': 'Missing sessionId.'})
                return
            try:
                response = handle_tldraw(session_id, {
                    **data,
                    'sessionKey': fallback_session_id,
                })
                self._json(200, response)
            except ValueError as exc:
                self._json(400, {'error': 'bad_request', 'detail': str(exc)})
            except Exception:
                import logging
                logging.exception('Legacy whiteboard POST handler error for session %s', session_id)
                self._json(500, {'error': 'internal_error', 'detail': 'Internal server error'})
            return
        if parsed.path.startswith('/api/local/chat/canvas/'):
            # Generic canvas addon dispatcher
            # Path format: /api/local/chat/canvas/:addonId
            if not _is_authorized(self):
                self._unauthorized()
                return
            addon_id = parsed.path.split('/api/local/chat/canvas/', 1)[1].strip('/')
            if not addon_id:
                self._json(400, {'error': 'bad_request', 'detail': 'Missing addon ID in path.'})
                return
            handler = CANVAS_DISPATCH.get(addon_id)
            if handler is None:
                self._json(400, {'error': 'bad_request', 'detail': f'Unknown canvas addon: {addon_id}'})
                return
            length = int(self.headers.get('Content-Length', 0))
            if length > 8 * 1024 * 1024:
                self._json(413, {'error': 'payload_too_large', 'detail': 'Request body too large.'})
                return
            try:
                data = json.loads(self.rfile.read(length).decode('utf-8')) if length else {}
            except json.JSONDecodeError:
                self._json(400, {'error': 'bad_request', 'detail': 'Invalid JSON body.'})
                return
            session_id = str(data.get('sessionId') or data.get('sessionKey') or '').strip()
            if not session_id:
                self._json(400, {'error': 'bad_request', 'detail': 'Missing sessionId.'})
                return
            try:
                response = handler(session_id, data)
                self._json(200, response)
            except ValueError as exc:
                self._json(400, {'error': 'bad_request', 'detail': str(exc)})
            except Exception:
                import logging
                logging.exception('Canvas addon handler error for %s', addon_id)
                self._json(500, {'error': 'internal_error', 'detail': 'Internal server error'})
            return
        if parsed.path == '/api/local/skills/install':
            if not _is_authorized(self):
                self._unauthorized()
                return
            payload = self._read_json_body()
            if payload is None:
                return
            identifier = payload.get('identifier')
            if not isinstance(identifier, str):
                self._json(400, {'error': 'bad_request', 'detail': 'Missing skill identifier.'})
                return
            status, result = _install_catalog_skill(identifier)
            self._json(status, result)
            return
        if parsed.path == '/api/local/skills/toggle':
            if not _is_authorized(self):
                self._unauthorized()
                return
            length = int(self.headers.get('Content-Length', 0))
            if length == 0:
                self._json(400, {'error': 'bad_request', 'detail': 'Empty body.'})
                return
            body = self.rfile.read(length).decode('utf-8')
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                self._json(400, {'error': 'bad_request', 'detail': 'Invalid JSON body.'})
                return
            skill_name = data.get('skillName', '').strip()
            if not skill_name:
                self._json(400, {'error': 'bad_request', 'detail': 'Missing skillName.'})
                return
            desired_enabled = bool(data.get('enabled', True))
            config_path = _get_hermes_home() / 'config.yaml'
            try:
                import yaml
                current_text = config_path.read_text()
                cfg = yaml.safe_load(current_text) or {}
            except Exception as exc:
                self._json(500, {'error': 'read_failed', 'detail': str(exc)})
                return
            cfg.setdefault('skills', {})
            disabled: list = list(cfg['skills'].get('disabled', []))
            if desired_enabled:
                # Remove from disabled list
                disabled = [d for d in disabled if d != skill_name]
            else:
                # Add to disabled list if not already present
                if skill_name not in disabled:
                    disabled.append(skill_name)
            cfg['skills']['disabled'] = sorted(disabled)
            new_text = yaml.safe_dump(cfg, default_flow_style=False, allow_unicode=True, sort_keys=False)
            try:
                backup = config_path.with_suffix(f'.yaml.bak.{int(time.time())}')
                backup.write_text(current_text, encoding='utf-8')
            except OSError:
                pass
            try:
                _atomic_write_text(config_path, new_text)
            except OSError as exc:
                self._json(500, {'error': 'write_failed', 'detail': str(exc)})
                return
            self._json(200, {
                'success': True,
                'skillName': skill_name,
                'enabled': desired_enabled,
                'detail': f"Skill '{skill_name}' {'enabled' if desired_enabled else 'disabled'}.",
            })
            return
        if parsed.path == '/api/local/push/subscriptions':
            if not _is_authorized(self):
                self._unauthorized()
                return
            length = int(self.headers.get('Content-Length', 0))
            if length == 0:
                self._json(400, {'error': 'bad_request', 'detail': 'Empty body.'})
                return
            body = self.rfile.read(length).decode('utf-8')
            try:
                subscription = json.loads(body)
            except json.JSONDecodeError:
                self._json(400, {'error': 'bad_request', 'detail': 'Invalid JSON body.'})
                return
            if not isinstance(subscription, dict) or not subscription.get('endpoint'):
                self._json(400, {'error': 'bad_request', 'detail': 'Missing subscription endpoint.'})
                return
            add_subscription(subscription)
            self._json(200, {'success': True, 'count': len(list_subscriptions())})
            return
        if parsed.path == '/api/local/push/send':
            if not _is_authorized(self):
                self._unauthorized()
                return
            length = int(self.headers.get('Content-Length', 0))
            if length == 0:
                self._json(400, {'error': 'bad_request', 'detail': 'Empty body.'})
                return
            body = self.rfile.read(length).decode('utf-8')
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                self._json(400, {'error': 'bad_request', 'detail': 'Invalid JSON body.'})
                return
            title = data.get('title', 'Hermes Mission Control')
            text = data.get('body', '')
            result = send_push(str(title), str(text))
            if result.get('disabled'):
                self._json(200, {'success': False, 'disabled': True, **result})
                return
            self._json(200, {'success': True, **result})
            return
        if parsed.path == '/api/local/candidates/approve':
            if not _is_authorized(self):
                self._unauthorized()
                return
            if not _candidates_enabled():
                self._json(404, {'error': 'feature_disabled',
                                 'detail': 'BDH curator is disabled. Set MC_ENABLE_BDH_CURATOR=1 to enable.'})
                return
            length = int(self.headers.get('Content-Length', 0))
            if length == 0:
                self._json(400, {'error': 'bad_request', 'detail': 'Empty body.'})
                return
            body = self.rfile.read(length).decode('utf-8')
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                self._json(400, {'error': 'bad_request', 'detail': 'Invalid JSON body.'})
                return
            cid = data.get('id', '')
            filename = data.get('filename') or None
            vault = data.get('vault') or None
            if not cid:
                self._json(400, {'error': 'bad_request', 'detail': 'Missing id.'})
                return
            if not candidates_mod.can_curate(vault):
                self._json(403, {'error': 'vault_not_curable',
                                 'detail': 'Candidate mutations are disabled for this vault.'})
                return
            cand = candidates_mod.approve(cid, vault, filename)
            if not cand:
                self._json(404, {'error': 'not_found', 'detail': f'Candidate {cid} not found.'})
                return
            self._json(200, {'success': True, 'candidate': cand})
            return
        if parsed.path == '/api/local/candidates/reject':
            if not _is_authorized(self):
                self._unauthorized()
                return
            if not _candidates_enabled():
                self._json(404, {'error': 'feature_disabled',
                                 'detail': 'BDH curator is disabled. Set MC_ENABLE_BDH_CURATOR=1 to enable.'})
                return
            length = int(self.headers.get('Content-Length', 0))
            if length == 0:
                self._json(400, {'error': 'bad_request', 'detail': 'Empty body.'})
                return
            body = self.rfile.read(length).decode('utf-8')
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                self._json(400, {'error': 'bad_request', 'detail': 'Invalid JSON body.'})
                return
            cid = data.get('id', '')
            filename = data.get('filename') or None
            reason = data.get('reason', '')
            vault = data.get('vault') or None
            if not cid:
                self._json(400, {'error': 'bad_request', 'detail': 'Missing id.'})
                return
            if not candidates_mod.can_curate(vault):
                self._json(403, {'error': 'vault_not_curable',
                                 'detail': 'Candidate mutations are disabled for this vault.'})
                return
            cand = candidates_mod.reject(cid, reason, vault, filename)
            if not cand:
                self._json(404, {'error': 'not_found', 'detail': f'Candidate {cid} not found.'})
                return
            self._json(200, {'success': True, 'candidate': cand})
            return
        self._json(404, {'error': 'not_found', 'path': self.path})

    def do_PATCH(self) -> None:  # noqa: N802
        if self._reject_mutation_in_read_only_mode():
            return
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path.startswith("/api/local/cron/"):
            if not _is_authorized(self):
                self._unauthorized()
                return
            payload = self._read_json_body()
            if payload is None:
                return
            self._cron_patch(parsed, payload)
            return
        self._json(404, {'error': 'not_found', 'path': self.path})

    def do_DELETE(self) -> None:  # noqa: N802
        if self._reject_mutation_in_read_only_mode():
            return
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path.startswith('/api/local/cron/jobs/'):
            if not _is_authorized(self):
                self._unauthorized()
                return
            self._cron_delete(parsed)
            return
        if parsed.path == '/api/local/push/subscriptions':
            if not _is_authorized(self):
                self._unauthorized()
                return
            length = int(self.headers.get('Content-Length', 0))
            if length == 0:
                self._json(400, {'error': 'bad_request', 'detail': 'Empty body.'})
                return
            body = self.rfile.read(length).decode('utf-8')
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                self._json(400, {'error': 'bad_request', 'detail': 'Invalid JSON body.'})
                return
            endpoint = data.get('endpoint', '')
            if not endpoint:
                self._json(400, {'error': 'bad_request', 'detail': 'Missing endpoint.'})
                return
            remove_subscription(endpoint)
            self._json(200, {'success': True})
            return
        self._json(404, {'error': 'not_found', 'path': self.path})

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        for key, value in self._cors_headers().items():
            self.send_header(key, value)
        self.send_header('Access-Control-Allow-Methods', 'GET, PUT, PATCH, POST, DELETE, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Authorization, Content-Type, X-API-Key')
        self.send_header('Access-Control-Allow-Credentials', 'true')
        self.send_header('Access-Control-Max-Age', '0')
        self.end_headers()

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        return


def _resolve_telemetry_bind() -> tuple[str, int]:
    """Resolve the telemetry bind address and port from environment variables.

    Canonical names: MISSION_CONTROL_LOCAL_TELEMETRY_HOST / _PORT.
    Legacy aliases (documented in .env.example): TELEMETRY_BIND_HOST / _PORT.
    Canonical names win over the legacy aliases when both are set.
    An invalid port aborts startup with a clear error instead of silently
    falling back to the default.

    The default bind is loopback (``127.0.0.1``): Mission Control is a
    local operator dashboard and must not listen on all interfaces unless
    the operator explicitly opts in (Tailscale/LAN exposure) by setting
    ``MISSION_CONTROL_LOCAL_TELEMETRY_HOST=0.0.0.0`` (or the legacy alias).
    """
    host = os.getenv("MISSION_CONTROL_LOCAL_TELEMETRY_HOST") or os.getenv("TELEMETRY_BIND_HOST") or "127.0.0.1"
    port_raw = os.getenv("MISSION_CONTROL_LOCAL_TELEMETRY_PORT") or os.getenv("TELEMETRY_BIND_PORT") or "8765"
    try:
        port = int(port_raw)
    except (TypeError, ValueError):
        raise SystemExit(
            f"[mission-control-local-telemetry] invalid telemetry port {port_raw!r}: "
            "expected an integer (MISSION_CONTROL_LOCAL_TELEMETRY_PORT / TELEMETRY_BIND_PORT)"
        )
    if not 0 < port < 65536:
        raise SystemExit(
            f"[mission-control-local-telemetry] invalid telemetry port {port}: "
            "must be between 1 and 65535"
        )
    return host, port


def main() -> None:
    host, port = _resolve_telemetry_bind()

    sampler = threading.Thread(target=_cpu_sampler, name="mc-cpu-sampler", daemon=True)
    sampler.start()

    start_gateway_watcher()
    terminal_thread = start_terminal_server()

    server = ThreadingHTTPServer((host, port), Handler)
    # Client handlers must not keep the process alive after the listener is
    # restarted while a browser/proxy still owns an abandoned connection.
    server.daemon_threads = True
    server.block_on_close = False
    def shutdown(signum: int, _frame: Any) -> None:
        print(f"[mission-control-local-telemetry] stopping (signal {signum})", flush=True)
        shutdown_terminal_sessions()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    print(f"[mission-control-local-telemetry] listening on http://{host}:{port}", flush=True)
    try:
        server.serve_forever()
    finally:
        shutdown_terminal_sessions()
        terminal_thread.stop()  # type: ignore[attr-defined]
        terminal_thread.join(timeout=2)


if __name__ == "__main__":
    main()
