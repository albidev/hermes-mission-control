#!/usr/bin/env python3
from __future__ import annotations

import hmac
import json
import os
import platform
import socket
import sys
import threading
import time
import urllib.parse
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from statistics import median
from typing import Any, Deque, Dict, Optional

import psutil

SERVER_DIR = Path(__file__).resolve().parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from mission_control_agents import (
    load_agent_trace_snapshot,
    load_agents_sessions_snapshot,
    load_agents_snapshot,
)


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

    if disk.percent >= 92 or (load_per_core is not None and load_per_core >= 2.0):
        health = "degraded"
    else:
        health = "healthy"

    summary = f"Load {load_per_core:.2f}/core, CPU {cpu_percent:.1f}%, RAM {vm.percent:.1f}%, Disk {disk.percent:.1f}%, RSS {process_memory_mb:.1f} MB" if load_per_core is not None else f"CPU {cpu_percent:.1f}%, RAM {vm.percent:.1f}%, Disk {disk.percent:.1f}%, RSS {process_memory_mb:.1f} MB"

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
            "usedPercent": round(float(vm.percent), 1),
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
            "usedPercent": round(float(disk.percent), 1),
            "freeGb": gb(disk.free),
            "totalGb": gb(disk.total),
        },
        "processMemoryMb": process_memory_mb,
        "summary": summary,
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
    if not expected:
        return False
    candidate = _extract_bearer_token(handler.headers.get("Authorization"))
    if not candidate and allow_query_token:
        candidate = _extract_query_token(handler)
    if not candidate:
        return False
    return hmac.compare_digest(candidate, expected)


_KNOWLEDGE_CORE_FILES = {"SOUL.md", "USER.md", "AGENTS.md"}
_KNOWLEDGE_EXCLUDED_FILENAMES = {"IDENTITY.md"}
_KNOWLEDGE_SKIPPED_DIRS = {".git", ".obsidian", ".agents", "node_modules", "dist", "build", ".trash", "__pycache__"}


def _knowledge_vault_root() -> Path:
    override = os.environ.get("HERMES_OBSIDIAN_VAULT") or os.environ.get("MISSION_CONTROL_VAULT_PATH")
    if override:
        return Path(os.path.expanduser(override)).resolve()
    return (Path.home() / "Documents" / "Hermes").resolve()


def _knowledge_core_root() -> Path:
    return (Path.home() / ".hermes").resolve()


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _display_knowledge_path(path: Path) -> str:
    resolved = path.resolve()
    home_root = Path.home().resolve()
    vault_root = _knowledge_vault_root().resolve()

    if resolved == vault_root:
        return "~/Documents/Hermes"
    if _path_is_within(resolved, vault_root):
        relative = resolved.relative_to(vault_root).as_posix()
        return f"~/Documents/Hermes/{relative}" if relative else "~/Documents/Hermes"
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
    if path.name in _KNOWLEDGE_EXCLUDED_FILENAMES or path.suffix.lower() != ".md":
        return False
    try:
        relative = path.resolve().relative_to(vault_root.resolve())
    except ValueError:
        return False
    parents = relative.parts[:-1]
    if any(part.startswith(".") or part in _KNOWLEDGE_SKIPPED_DIRS for part in parents):
        return False
    return True


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

    core_candidates = [
        ("soul", "~/.hermes/SOUL.md", Path.home() / ".hermes" / "SOUL.md"),
        ("user", "~/.hermes/USER.md", Path.home() / ".hermes" / "USER.md"),
        ("agents", "~/.hermes/AGENTS.md", Path.home() / ".hermes" / "AGENTS.md"),
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

        vault_candidates = [
            path
            for path in sorted(vault_root.rglob("*.md"), key=_sort_key)
            if path.is_file() and _is_allowed_vault_note(path, vault_root)
        ]
        max_items = int(os.environ.get("MISSION_CONTROL_KNOWLEDGE_MAX_FILES", "80"))
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
            "sourcePath": "~/Documents/Hermes/Knowledge Sharing.md",
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
    if raw == "~/Documents/Hermes":
        candidate = vault_root
    elif raw.startswith("~/Documents/Hermes/"):
        candidate = vault_root / raw[len("~/Documents/Hermes/"):]
    elif raw == "~/.hermes":
        candidate = core_root
    elif raw.startswith("~/.hermes/"):
        candidate = core_root / raw[len("~/.hermes/"):]
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


class Handler(BaseHTTPRequestHandler):
    def _cors_headers(self) -> Dict[str, str]:
        configured_origin = (os.getenv("MISSION_CONTROL_ALLOWED_ORIGIN") or "").strip()
        request_origin = (self.headers.get("Origin") or "").strip()
        if configured_origin and request_origin and request_origin == configured_origin:
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
        if parsed.path == "/health":
            self._json(200, {"ok": True, "service": "mission-control-local-telemetry", "source": "local-psutil"})
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
            self._json(200, collect_knowledge_snapshot())
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
            self._json(200, load_agents_sessions_snapshot(limit=limit))
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
        self._json(404, {"error": "not_found", "path": self.path})

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        return


def main() -> None:
    host = os.getenv("MISSION_CONTROL_LOCAL_TELEMETRY_HOST", "127.0.0.1")
    port = int(os.getenv("MISSION_CONTROL_LOCAL_TELEMETRY_PORT", "8765"))

    sampler = threading.Thread(target=_cpu_sampler, name="mc-cpu-sampler", daemon=True)
    sampler.start()

    server = ThreadingHTTPServer((host, port), Handler)
    print(f"[mission-control-local-telemetry] listening on http://{host}:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
