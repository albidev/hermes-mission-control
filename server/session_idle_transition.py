"""Live -> idle transition detection and gateway forwarding for Mission Control.

Mission Control derives a session's displayed status from ``last_active_at``
and a 300-second live window (see ``mission_control_agents._is_live``). This
module adds a *separate* observer over the same liveness facts that emits one
authenticated idle signal per live -> idle transition for eligible, non-ended
sessions.

Design constraints (do not regress):

- The displayed status semantics are untouched: this detector never feeds back
  into ``_build_session_item`` or the status facets.
- Exactly one signal per transition: while a session stays idle, no further
  signal is emitted. The detector re-arms only after the session is observed
  live again.
- Ended sessions are excluded and their tracked state is forgotten.
- The forwarder targets an explicit gateway control endpoint/adapter contract
  (``MISSION_CONTROL_IDLE_SIGNAL_URL``). It never calls ``/new`` and never
  resets or closes a session.
- Gateway failures are returned as ``{ok: False, ...}`` and never raise, so
  normal session polling is unaffected.
- No transcript or raw provenance is included in the signal.
"""

from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.request
from typing import Any

from mission_control_agents import _collect_agent_sessions, _is_live

# Default gateway control endpoint. The core hook consumes this contract; the
# operator can override it via the environment.
_DEFAULT_IDLE_SIGNAL_URL = "http://127.0.0.1:9119/api/gateway/session-idle"
_IDLE_SIGNAL_TIMEOUT_SECONDS = 5.0


def _idle_signal_url() -> str:
    return os.environ.get("MISSION_CONTROL_IDLE_SIGNAL_URL", _DEFAULT_IDLE_SIGNAL_URL).rstrip("/")


def _idle_signal_token() -> str:
    return (os.environ.get("MISSION_CONTROL_TOKEN") or os.environ.get("API_SERVER_KEY") or "").strip()


def _collect_session_facts() -> list[dict[str, Any]]:
    """Project the liveness facts the detector needs from the shared session
    collector, without loading recent messages or touching status semantics."""
    items = _collect_agent_sessions(include_recent_messages=False)
    return [
        {
            "sessionId": item.get("sessionId"),
            "lastActiveAt": item.get("lastActiveAt"),
            "endedAt": item.get("endedAt"),
        }
        for item in items
        if item.get("sessionId")
    ]


class IdleTransitionDetector:
    """Stateful live -> idle edge detector over per-session liveness facts.

    Thread-safe: the watcher thread is the only caller, but the lock keeps the
    state consistent if a future caller observes from another thread.
    """

    def __init__(self, live_window_seconds: int = 300) -> None:
        self._live_window_seconds = live_window_seconds
        self._lock = threading.Lock()
        # session_id -> (was_live, transition_counter)
        self._state: dict[str, tuple[bool, int]] = {}

    def observe(self, facts: list[dict[str, Any]], now: float | None = None) -> list[dict[str, Any]]:
        """Observe one poll of session facts and return the idle signals for any
        live -> idle transitions detected this tick."""
        import time as _time

        now = _time.time() if now is None else now
        signals: list[dict[str, Any]] = []
        with self._lock:
            seen: set[str] = set()
            for fact in facts:
                session_id = str(fact.get("sessionId") or "").strip()
                if not session_id:
                    continue
                seen.add(session_id)
                last_active = fact.get("lastActiveAt")
                ended_at = fact.get("endedAt")
                is_live = _is_live(last_active, ended_at, self._live_window_seconds)
                previous = self._state.get(session_id)
                was_live = previous[0] if previous else False
                counter = previous[1] if previous else 0

                if ended_at is not None:
                    # Ended sessions are excluded and their state is forgotten.
                    self._state.pop(session_id, None)
                    continue

                if was_live and not is_live:
                    counter += 1
                    signals.append(
                        {
                            "event": "session_idle",
                            "session_id": session_id,
                            "transition_key": f"live_to_idle:{session_id}:{counter}",
                            "occurred_at": now,
                        }
                    )
                self._state[session_id] = (is_live, counter)

            # Forget sessions that disappeared from the poll entirely.
            for session_id in list(self._state.keys()):
                if session_id not in seen:
                    self._state.pop(session_id, None)
        return signals


def forward_idle_signal(signal: dict[str, Any], *, base_url: str | None = None, token: str | None = None) -> dict[str, Any]:
    """POST one idle signal to the gateway control endpoint.

    Returns ``{ok: True, status: <int>}`` on success or ``{ok: False, ...}`` on
    any failure. Never raises: a gateway outage must not break session polling.
    """
    url = (base_url or _idle_signal_url()).rstrip("/")
    auth_token = token if token is not None else _idle_signal_token()
    body = json.dumps(signal).encode("utf-8")
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"
    request = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=_IDLE_SIGNAL_TIMEOUT_SECONDS) as response:
            status = int(response.status)
            response.read()
    except urllib.error.HTTPError as exc:
        return {"ok": False, "status": exc.code, "error": f"gateway returned HTTP {exc.code}"}
    except (urllib.error.URLError, OSError) as exc:
        return {"ok": False, "error": f"gateway unavailable: {type(exc).__name__}: {exc}"}
    if status < 200 or status >= 300:
        return {"ok": False, "status": status, "error": f"gateway returned HTTP {status}"}
    return {"ok": True, "status": status}


def _idle_watcher_tick(detector: IdleTransitionDetector) -> list[dict[str, Any]]:
    """One watcher iteration: collect facts, detect transitions, forward each.

    Returns the forward results (one per emitted signal). A gateway failure is
    returned as ``{ok: False, ...}`` and never raised.
    """
    facts = _collect_session_facts()
    signals = detector.observe(facts)
    return [forward_idle_signal(signal) for signal in signals]


def _idle_watcher_loop(interval: float = 15.0) -> None:
    detector = IdleTransitionDetector()
    while True:
        try:
            _idle_watcher_tick(detector)
        except Exception:
            # The detector/forwarder must never take down the sidecar.
            pass
        import time as _time

        _time.sleep(interval)


def start_idle_watcher(interval: float = 15.0) -> None:
    """Start the background live -> idle transition watcher thread."""
    thread = threading.Thread(
        target=_idle_watcher_loop,
        args=(interval,),
        name="mc-idle-transition-watcher",
        daemon=True,
    )
    thread.start()
