"""Mission Control bridge for the Hermes cron scheduler.

The bridge deliberately delegates job persistence and scheduling semantics to the
core ``cron.jobs`` module. Mission Control only validates the HTTP-facing
payload, enriches read results with output, and exposes a small action API.
"""

from __future__ import annotations

import importlib
import re
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, Optional

from hermes_paths import hermes_root, get_active_profile


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


_PROFILE_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}$")


def _profile_home(profile: str) -> Path:
    name = str(profile or '').strip().lower()
    if not name or name == 'default':
        return hermes_root()
    if not _PROFILE_RE.fullmatch(name):
        raise CronBridgeError(400, 'Invalid Hermes profile name')
    home = hermes_root() / 'profiles' / name
    if not home.is_dir():
        raise CronBridgeError(404, f"Profile '{name}' not found")
    return home


def _profile_names() -> list[str]:
    root = hermes_root()
    names = ['default']
    profiles = root / 'profiles'
    if profiles.is_dir():
        names.extend(sorted(path.name for path in profiles.iterdir() if path.is_dir() and _PROFILE_RE.fullmatch(path.name)))
    return names


@contextmanager
def _cron_context(profile: str) -> Iterator[Any]:
    core = _load_core()
    use_cron_store = getattr(core, 'use_cron_store', None)
    if not callable(use_cron_store):
        # Compatibility with older embedded cores and test doubles. The active
        # profile remains the only available store in that mode.
        yield core
        return
    with use_cron_store(_profile_home(profile)):
        yield core


def _resolve_profile_for_job(job_id: str, profile: Optional[str] = None) -> str:
    if profile:
        return str(profile).strip().lower() or get_active_profile()
    wanted = str(job_id or '').strip()
    for candidate in _profile_names():
        with _cron_context(candidate) as core:
            try:
                jobs = core.list_jobs(include_disabled=True)
            except Exception:
                continue
            if any(isinstance(job, dict) and str(job.get('id') or '') == wanted for job in jobs):
                return candidate
    return get_active_profile()


def _profile_from_payload(payload: Dict[str, Any]) -> str:
    return str(payload.get('profile') or get_active_profile()).strip().lower() or 'default'


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


def _enrich_job(core: Any, job: Dict[str, Any], *, profile: str, include_output: bool = True) -> Dict[str, Any]:
    enriched = dict(job)
    enriched["profile"] = profile
    enriched["latest_execution"] = _latest_execution(enriched)
    if include_output:
        enriched["last_output"] = _read_latest_output(core, str(enriched.get("id", "")))
    else:
        enriched.pop("last_output", None)
    return enriched


def list_jobs(include_disabled: bool = True, *, include_output: bool = True, profile: Optional[str] = None) -> list[Dict[str, Any]]:
    core = _load_core()
    supports_profile_store = callable(getattr(core, 'use_cron_store', None))
    profiles = [_profile_from_payload({"profile": profile})] if profile else (_profile_names() if supports_profile_store else [get_active_profile()])
    result: list[Dict[str, Any]] = []
    for profile_name in profiles:
        try:
            with _cron_context(profile_name) as core:
                jobs = core.list_jobs(include_disabled=include_disabled)
                result.extend(
                    _enrich_job(core, job, profile=profile_name, include_output=include_output)
                    for job in jobs if isinstance(job, dict)
                )
        except CronBridgeError:
            if profile:
                raise
        except Exception as exc:
            if profile:
                raise CronBridgeError(500, f"Could not list cron jobs: {exc}") from exc
    return result


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


def _job_response(job: Optional[Dict[str, Any]], core: Any, profile: str) -> Dict[str, Any]:
    if not isinstance(job, dict):
        raise CronBridgeError(404, "Cron job not found")
    return {"success": True, "job": _enrich_job(core, job, profile=profile)}


def create_job(payload: Dict[str, Any]) -> Dict[str, Any]:
    profile = _profile_from_payload(payload)
    values = _filtered_payload(payload, _CREATE_FIELDS)
    if not str(values.get("schedule") or "").strip():
        raise CronBridgeError(400, "schedule is required")
    try:
        with _cron_context(profile) as core:
            job = core.create_job(**values)
            return _job_response(job, core, profile)
    except (ValueError, TypeError) as exc:
        raise CronBridgeError(400, str(exc)) from exc
    except CronBridgeError:
        raise
    except Exception as exc:
        raise CronBridgeError(500, f"Could not create cron job: {exc}") from exc


def update_job(job_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    wanted = str(job_id or "").strip()
    if not wanted:
        raise CronBridgeError(400, "Cron job id is required")
    profile = _resolve_profile_for_job(wanted, payload.get('profile') if isinstance(payload, dict) else None)
    values = _filtered_payload(payload, _UPDATE_FIELDS)
    if not values:
        raise CronBridgeError(400, "No mutable cron job fields provided")
    try:
        with _cron_context(profile) as core:
            job = core.update_job(wanted, values)
            return _job_response(job, core, profile)
    except (ValueError, TypeError) as exc:
        raise CronBridgeError(400, str(exc)) from exc
    except CronBridgeError:
        raise
    except Exception as exc:
        raise CronBridgeError(500, f"Could not update cron job: {exc}") from exc


def pause_job(job_id: str, reason: Optional[str] = None, profile: Optional[str] = None) -> Dict[str, Any]:
    profile_name = _resolve_profile_for_job(job_id, profile)
    try:
        with _cron_context(profile_name) as core:
            job = core.pause_job(str(job_id), reason=reason)
            return _job_response(job, core, profile_name)
    except (ValueError, TypeError) as exc:
        raise CronBridgeError(400, str(exc)) from exc
    except CronBridgeError:
        raise
    except Exception as exc:
        raise CronBridgeError(500, f"Could not pause cron job: {exc}") from exc


def resume_job(job_id: str, profile: Optional[str] = None) -> Dict[str, Any]:
    profile_name = _resolve_profile_for_job(job_id, profile)
    try:
        with _cron_context(profile_name) as core:
            job = core.resume_job(str(job_id))
            return _job_response(job, core, profile_name)
    except (ValueError, TypeError) as exc:
        raise CronBridgeError(400, str(exc)) from exc
    except CronBridgeError:
        raise
    except Exception as exc:
        raise CronBridgeError(500, f"Could not resume cron job: {exc}") from exc


def run_job(job_id: str, profile: Optional[str] = None) -> Dict[str, Any]:
    profile_name = _resolve_profile_for_job(job_id, profile)
    try:
        with _cron_context(profile_name) as core:
            job = core.trigger_job(str(job_id))
            return _job_response(job, core, profile_name)
    except (ValueError, TypeError) as exc:
        raise CronBridgeError(400, str(exc)) from exc
    except CronBridgeError:
        raise
    except Exception as exc:
        raise CronBridgeError(500, f"Could not run cron job: {exc}") from exc


def delete_job(job_id: str, profile: Optional[str] = None) -> Dict[str, Any]:
    wanted = str(job_id or "").strip()
    if not wanted:
        raise CronBridgeError(400, "Cron job id is required")
    profile_name = _resolve_profile_for_job(wanted, profile)
    try:
        with _cron_context(profile_name) as core:
            removed = bool(core.remove_job(wanted))
    except CronBridgeError:
        raise
    except Exception as exc:
        raise CronBridgeError(500, f"Could not delete cron job: {exc}") from exc
    if not removed:
        raise CronBridgeError(404, f"Cron job '{wanted}' not found")
    return {"success": True, "job_id": wanted, "profile": profile_name}
