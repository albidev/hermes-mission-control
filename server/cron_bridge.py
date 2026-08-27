"""Mission Control bridge for the Hermes cron scheduler.

The bridge deliberately delegates job persistence and scheduling semantics to the
core ``cron.jobs`` module. Mission Control only validates the HTTP-facing
payload, enriches read results with output, and exposes a small action API.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, Optional


_core_module = None


class CronBridgeError(Exception):
    """An expected cron API error that can be returned to the frontend."""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


_CREATE_FIELDS = (
    "prompt",
    "schedule",
    "name",
    "repeat",
    "deliver",
    "origin",
    "skill",
    "skills",
    "model",
    "provider",
    "base_url",
    "script",
    "context_from",
    "enabled_toolsets",
    "workdir",
    "no_agent",
    "attach_to_session",
    "monitor_script",
    "monitor_url",
    "reasoning_effort",
)

_UPDATE_FIELDS = (
    "prompt",
    "schedule",
    "schedule_display",
    "name",
    "repeat",
    "deliver",
    "skill",
    "skills",
    "model",
    "provider",
    "base_url",
    "script",
    "context_from",
    "enabled_toolsets",
    "workdir",
    "no_agent",
    "attach_to_session",
    "monitor_script",
    "monitor_url",
    "reasoning_effort",
    "enabled",
    "state",
)


def _load_core():
    global _core_module
    if _core_module is not None:
        return _core_module

    from hermes_paths import hermes_core_dir, get_hermes_home

    hermes_home = get_hermes_home()
    core_root = hermes_core_dir()
    if str(core_root) not in sys.path:
        sys.path.insert(0, str(core_root))
    try:
        _core_module = importlib.import_module("cron.jobs")
    except Exception as exc:  # pragma: no cover - exercised by runtime fallback
        raise CronBridgeError(503, f"Hermes cron core is unavailable: {exc}") from exc
    return _core_module


def _read_latest_output(core: Any, job_id: str, limit: int = 100_000) -> Optional[str]:
    try:
        output_dir = Path(core.get_cron_output_dir())
        if not output_dir.is_dir():
            return None
        candidates = [
            path
            for path in output_dir.iterdir()
            if path.is_file() and (path.name == job_id or path.name.startswith(f"{job_id}_"))
        ]
        job_output_dir = output_dir / job_id
        if job_output_dir.is_dir():
            candidates.extend(path for path in job_output_dir.rglob("*") if path.is_file())
        if not candidates:
            return None
        latest = max(candidates, key=lambda path: path.stat().st_mtime_ns)
        return latest.read_text(encoding="utf-8", errors="replace")[-limit:]
    except (OSError, ValueError, TypeError):
        return None


def _latest_execution(job: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    execution = job.get("latest_execution")
    if isinstance(execution, dict):
        return execution
    status = job.get("last_status")
    run_at = job.get("last_run_at")
    error = job.get("last_error")
    if not status and not run_at and not error:
        return None
    return {
        "status": status or ("error" if error else "unknown"),
        "started_at": run_at,
        "finished_at": run_at,
        "error": error,
    }


def _enrich_job(core: Any, job: Dict[str, Any], *, include_output: bool = True) -> Dict[str, Any]:
    enriched = dict(job)
    enriched["latest_execution"] = _latest_execution(enriched)
    if include_output:
        enriched["last_output"] = _read_latest_output(core, str(enriched.get("id", "")))
    else:
        # The list endpoint is polled frequently and the UI only needs output
        # after opening a job detail. Avoid shipping up to 100 KB per job on
        # every 15/30 second Mission Control refresh.
        enriched.pop("last_output", None)
    return enriched


def list_jobs(include_disabled: bool = True, *, include_output: bool = True) -> list[Dict[str, Any]]:
    core = _load_core()
    try:
        jobs = core.list_jobs(include_disabled=include_disabled)
    except Exception as exc:
        raise CronBridgeError(500, f"Could not list cron jobs: {exc}") from exc
    return [_enrich_job(core, job, include_output=include_output) for job in jobs if isinstance(job, dict)]


def get_job(job_id: str) -> Dict[str, Any]:
    wanted = str(job_id or "").strip()
    if not wanted:
        raise CronBridgeError(400, "Cron job id is required")
    for job in list_jobs(include_disabled=True, include_output=True):
        if str(job.get("id", "")) == wanted or str(job.get("name", "")) == wanted:
            return job
    raise CronBridgeError(404, f"Cron job '{wanted}' not found")


def _filtered_payload(payload: Dict[str, Any], fields: Iterable[str]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise CronBridgeError(400, "Payload must be a JSON object")
    return {key: payload[key] for key in fields if key in payload}


def _job_response(job: Optional[Dict[str, Any]], core: Any) -> Dict[str, Any]:
    if not isinstance(job, dict):
        raise CronBridgeError(404, "Cron job not found")
    return {"success": True, "job": _enrich_job(core, job)}


def create_job(payload: Dict[str, Any]) -> Dict[str, Any]:
    values = _filtered_payload(payload, _CREATE_FIELDS)
    if not str(values.get("schedule") or "").strip():
        raise CronBridgeError(400, "schedule is required")
    core = _load_core()
    try:
        job = core.create_job(**values)
    except (ValueError, TypeError) as exc:
        raise CronBridgeError(400, str(exc)) from exc
    except Exception as exc:
        raise CronBridgeError(500, f"Could not create cron job: {exc}") from exc
    return _job_response(job, core)


def update_job(job_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    wanted = str(job_id or "").strip()
    if not wanted:
        raise CronBridgeError(400, "Cron job id is required")
    values = _filtered_payload(payload, _UPDATE_FIELDS)
    if not values:
        raise CronBridgeError(400, "No mutable cron job fields provided")
    core = _load_core()
    try:
        job = core.update_job(wanted, values)
    except (ValueError, TypeError) as exc:
        raise CronBridgeError(400, str(exc)) from exc
    except Exception as exc:
        raise CronBridgeError(500, f"Could not update cron job: {exc}") from exc
    return _job_response(job, core)


def pause_job(job_id: str, reason: Optional[str] = None) -> Dict[str, Any]:
    core = _load_core()
    try:
        job = core.pause_job(str(job_id), reason=reason)
    except (ValueError, TypeError) as exc:
        raise CronBridgeError(400, str(exc)) from exc
    except Exception as exc:
        raise CronBridgeError(500, f"Could not pause cron job: {exc}") from exc
    return _job_response(job, core)


def resume_job(job_id: str) -> Dict[str, Any]:
    core = _load_core()
    try:
        job = core.resume_job(str(job_id))
    except (ValueError, TypeError) as exc:
        raise CronBridgeError(400, str(exc)) from exc
    except Exception as exc:
        raise CronBridgeError(500, f"Could not resume cron job: {exc}") from exc
    return _job_response(job, core)


def run_job(job_id: str) -> Dict[str, Any]:
    core = _load_core()
    try:
        job = core.trigger_job(str(job_id))
    except (ValueError, TypeError) as exc:
        raise CronBridgeError(400, str(exc)) from exc
    except Exception as exc:
        raise CronBridgeError(500, f"Could not run cron job: {exc}") from exc
    return _job_response(job, core)


def delete_job(job_id: str) -> Dict[str, Any]:
    wanted = str(job_id or "").strip()
    if not wanted:
        raise CronBridgeError(400, "Cron job id is required")
    core = _load_core()
    try:
        removed = bool(core.remove_job(wanted))
    except Exception as exc:
        raise CronBridgeError(500, f"Could not delete cron job: {exc}") from exc
    if not removed:
        raise CronBridgeError(404, f"Cron job '{wanted}' not found")
    return {"success": True, "job_id": wanted}
