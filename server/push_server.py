#!/usr/bin/env python3
"""Web Push support for Mission Control.

Provides:
- VAPID public key lookup for the client's pushManager.subscribe() call.
- Subscription persistence (store/delete) so the backend can deliver pushes.
- Push delivery (send a notification to every stored subscription).
- A gateway watcher that fires a push whenever Hermes completes a response,
  so notifications arrive while the app is in the background or closed
  (the page's own JS is suspended then and cannot poll).

Optional runtime dependencies (declared in server/requirements-push.txt, not
in the base server/requirements.txt):
- pywebpush   — Web Push Protocol delivery.
- websockets  — the real-time interaction observer (approvals, clarities...).

Push is enabled only when ALL of the following hold:
- MISSION_CONTROL_VAPID_PUBLIC_KEY and MISSION_CONTROL_VAPID_PRIVATE_KEY are
  set (VAPID keypair);
- MISSION_CONTROL_VAPID_CONTACT is set (required sender identity);
- pywebpush and websockets are importable.

Otherwise push is explicitly disabled and push_status() reports a stable
`reason` so an operator can distinguish an intentional disablement from an
incomplete installation:
- vapid_not_configured  — keys/contact missing (see missingConfig);
- missing_dependency    — optional package(s) absent (see missingDependencies).
"""
from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import os
import re
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

SERVER_DIR = Path(__file__).resolve().parent

# --- VAPID ----------------------------------------------------------------
# VAPID keys are secrets. They are loaded from environment variables; the
# plist injects them, and the repo keeps only an example. If unset, push is
# disabled.

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def load_vapid_private_key() -> Optional[str]:
    """Return the VAPID private key in a form pywebpush's Vapid.from_string()
    accepts, or None if unset.

    from_string() auto-detects RAW (32-byte) vs DER. We store the RAW
    base64url form (32 bytes) which it parses directly.
    """
    raw = os.environ.get("MISSION_CONTROL_VAPID_PRIVATE_KEY", "").strip()
    if not raw:
        return None
    return raw or None


def load_vapid_public_key() -> Optional[str]:
    """Return the base64url VAPID public key the client subscribes with."""
    raw = os.environ.get("MISSION_CONTROL_VAPID_PUBLIC_KEY", "").strip()
    return raw or None


def vapid_contact() -> Optional[str]:
    return os.environ.get("MISSION_CONTROL_VAPID_CONTACT", "").strip() or None


# --- Optional dependency detection ------------------------------------------
#
# pywebpush and websockets are declared in server/requirements-push.txt.
# Importability is checked with importlib.util.find_spec so the module works
# (and reports a precise reason) when the packages are absent.

_PUSH_DEPENDENCIES = ("pywebpush", "websockets")


def _missing_push_dependencies() -> List[str]:
    return [name for name in _PUSH_DEPENDENCIES if importlib.util.find_spec(name) is None]


def _dependencies_available() -> bool:
    return not _missing_push_dependencies()


def push_status() -> Dict[str, Any]:
    """Report whether push delivery is available and, if not, why.

    Reasons:
    - "vapid_not_configured": VAPID keys or MISSION_CONTROL_VAPID_CONTACT are
      missing (intentional disablement). missingConfig lists what is unset.
    - "missing_dependency": the optional packages are not installed in the
      interpreter running the telemetry server. missingDependencies lists
      them; install with `python3 -m pip install -r server/requirements-push.txt`.
    - "ok": everything is configured and importable; delivery may still fail
      at runtime per subscription (surfaced via send_push's failed count).
    """
    missing_config: List[str] = []
    if not load_vapid_private_key():
        missing_config.append("MISSION_CONTROL_VAPID_PRIVATE_KEY")
    if not load_vapid_public_key():
        missing_config.append("MISSION_CONTROL_VAPID_PUBLIC_KEY")
    if not vapid_contact():
        missing_config.append("MISSION_CONTROL_VAPID_CONTACT")
    if missing_config:
        return {
            "enabled": False,
            "reason": "vapid_not_configured",
            "missingConfig": missing_config,
        }
    missing_deps = _missing_push_dependencies()
    if missing_deps:
        return {
            "enabled": False,
            "reason": "missing_dependency",
            "missingDependencies": missing_deps,
        }
    return {"enabled": True, "reason": "ok"}


# --- Subscription storage -------------------------------------------------

_SUBSCRIPTION_FILE = SERVER_DIR / "push_subscriptions.json"
_SUBSCRIPTION_LOCK = threading.Lock()


def _load_subscriptions() -> List[Dict[str, Any]]:
    try:
        if _SUBSCRIPTION_FILE.exists():
            data = json.loads(_SUBSCRIPTION_FILE.read_text())
            if isinstance(data, list):
                return data
    except Exception:
        pass
    return []


def _save_subscriptions(subscriptions: List[Dict[str, Any]]) -> None:
    tmp = _SUBSCRIPTION_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(subscriptions, indent=2))
    tmp.replace(_SUBSCRIPTION_FILE)


def list_subscriptions() -> List[Dict[str, Any]]:
    with _SUBSCRIPTION_LOCK:
        return _load_subscriptions()


def add_subscription(subscription: Dict[str, Any]) -> bool:
    with _SUBSCRIPTION_LOCK:
        subs = _load_subscriptions()
        endpoint = str(subscription.get("endpoint", ""))
        # Replace an existing subscription with the same endpoint.
        subs = [s for s in subs if s.get("endpoint") != endpoint]
        subs.append(subscription)
        _save_subscriptions(subs)
    return True


def remove_subscription(endpoint: str) -> bool:
    with _SUBSCRIPTION_LOCK:
        subs = _load_subscriptions()
        before = len(subs)
        subs = [s for s in subs if s.get("endpoint") != endpoint]
        if len(subs) != before:
            _save_subscriptions(subs)
            return True
    return False


# --- Push delivery --------------------------------------------------------

def send_push(title: str, body: str, *, tag: str = "mission-control", data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Send a push to every stored subscription.

    Returns a summary {sent, failed, disabled}. Failures are isolated per
    subscription so one dead device doesn't block the rest.
    """
    private_key = load_vapid_private_key()
    contact = vapid_contact()
    status = push_status()
    if status.get("reason") != "ok":
        return {"sent": 0, "failed": 0, "disabled": True, "reason": status.get("reason")}
    # push_status() == ok guarantees a non-empty contact; keep the type narrow.
    assert contact is not None and private_key is not None

    # VAPID configuration is complete; a missing package here means the
    # dependency disappeared between status checks — still report it.
    if not _dependencies_available():
        return {
            "sent": 0,
            "failed": 0,
            "disabled": True,
            "reason": "missing_dependency",
            "missingDependencies": _missing_push_dependencies(),
        }

    from pywebpush import webpush, WebPushException

    payload = json.dumps({"title": title, "body": body, "tag": tag, "data": data or {}})

    sent = 0
    failed = 0
    for subscription in list_subscriptions():
        try:
            webpush(
                subscription_info=subscription,
                data=payload,
                vapid_private_key=private_key,
                vapid_claims={"sub": contact},
                timeout=10,
            )
            sent += 1
        except WebPushException as exc:
            # 404/410 means the subscription is gone — drop it.
            status_code = getattr(exc.response, "status_code", None)
            if status_code in (404, 410):
                remove_subscription(str(subscription.get("endpoint", "")))
            failed += 1
        except Exception:
            failed += 1

    return {"sent": sent, "failed": failed, "disabled": False}


# --- Gateway watcher (server-side trigger) --------------------------------
#
# Trigger model: the app's own WebSocket dies in the background (Safari
# suspends the page), so a server-side process must fire pushes. The source
# of truth is state.db (messages table): a new assistant message in a
# mission-control session is a completed Hermes response → push. This is
# precise and only fires on final answers, not on intermediate reasoning or
# tool noise (which are tool/role rows we ignore).

def _state_db() -> Optional[Any]:
    """Open Hermes's session store read-only for the watcher.

    Mirrors mission_control_agents._try_get_session_db (read_only=True) so we
    never compete with the gateway's writer or run schema init.
    """
    try:
        from hermes_state import SessionDB
        return SessionDB(read_only=True)
    except Exception:
        return None


def _last_message_id(conn: Any) -> int:
    try:
        row = conn.execute("SELECT COALESCE(MAX(id), 0) FROM messages").fetchone()
        return int(row[0]) if row else 0
    except Exception:
        return 0


def _new_final_answers(conn: Any, after_id: int) -> List[Dict[str, str]]:
    """Return assistant responses added to mission-control sessions after
    after_id: {session, text}. Empty content and hidden/system rows are
    skipped so we only notify on real final answers.
    """
    rows = conn.execute(
        """
        SELECT m.id, s.title, m.content
        FROM messages m
        JOIN sessions s ON s.id = m.session_id
        WHERE m.id > ? AND s.source = 'mission-control'
          AND m.role = 'assistant'
          AND (m.display_kind IS NULL OR m.display_kind NOT IN ('hidden'))
          AND m.content IS NOT NULL AND length(trim(m.content)) > 0
        ORDER BY m.id ASC
        """,
        (after_id,),
    ).fetchall()
    return [
        {
            "id": str(row[0]),
            "session": row[1] or "Mission Control",
            "text": str(row[2])[:160],
        }
        for row in rows
    ]


def _watch_gateway_loop(interval: float = 5.0) -> None:
    """Poll state.db for new final answers in mission-control sessions and
    push each one. Tracks the last-seen message id so each response fires
    exactly once.
    """
    db = _state_db()
    if db is None:
        return
    try:
        conn = getattr(db, "_conn", None)
        if conn is None:
            return
        last_id = _last_message_id(conn)
    except Exception:
        return

    while True:
        try:
            conn = getattr(db, "_conn", None)
            if conn is None:
                db = _state_db()
                if db is None:
                    time.sleep(interval)
                    continue
                conn = getattr(db, "_conn", None)
                if conn is None:
                    time.sleep(interval)
                    continue
                last_id = _last_message_id(conn)

            answers = _new_final_answers(conn, last_id)
            for answer in answers:
                send_push(
                    "Hermes Mission Control",
                    f"{answer['session']}: {answer['text']}",
                    tag=f"answer-{answer['id']}",
                    data={"url": "/"},
                )
                if int(answer["id"]) > last_id:
                    last_id = int(answer["id"])
        except Exception:
            pass
        time.sleep(interval)


def start_gateway_watcher(interval: float = 6.0) -> None:
    thread = threading.Thread(
        target=_watch_gateway_loop,
        args=(interval,),
        name="mc-push-watcher",
        daemon=True,
    )
    thread.start()
    _start_interaction_observer()


# --- Interaction observer (real-time, WebSocket) ---------------------------
#
# Interaction requests (approval / clarify / secret / sudo / terminal read)
# are NOT persisted to state.db — they are live WebSocket events. To push on
# them while the app is in the background we dial the same dashboard gateway
# WebSocket the client uses, with the injected session token, and push on
# interaction.request events. Final answers are already covered by the
# state.db poller above, so we only handle interactions here.

_INTERACTION_EVENTS = {
    "approval.request",
    "clarify.request",
    "secret.request",
    "sudo.request",
    "terminal.read.request",
}

_INTERACTION_LABELS = {
    "approval.request": "Hermes needs permission",
    "clarify.request": "Hermes needs your answer",
    "secret.request": "Hermes needs a secret",
    "sudo.request": "Hermes needs elevated access",
    "terminal.read.request": "Hermes needs terminal output",
}

_WS_BASE = os.environ.get(
    "MISSION_CONTROL_GATEWAY_WS_URL",
    "ws://127.0.0.1:5174/api/ws",
)
_GATEWAY_ROOT_URL = os.environ.get(
    "MISSION_CONTROL_GATEWAY_ROOT_URL",
    "http://127.0.0.1:5174/api/gateway-root",
)
_RECONNECT_DELAY = float(os.environ.get("MISSION_CONTROL_WS_RECONNECT_DELAY", "5"))


def _fetch_session_token() -> Optional[str]:
    """Pull the dashboard session token injected into the gateway-root page.

    Mirrors what the browser client reads on startup so the observer can
    authenticate to the same WebSocket endpoint.
    """
    try:
        req = urllib.request.Request(_GATEWAY_ROOT_URL, headers={"User-Agent": "hermes-mc-push"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            html = resp.read().decode("utf-8", errors="replace")
        match = re.search(r'__HERMES_SESSION_TOKEN__\s*=\s*"([^"]+)"', html)
        return match.group(1) if match else None
    except Exception:
        return None


def _interaction_body(event_type: str, payload: Dict[str, Any]) -> str:
    label = _INTERACTION_LABELS.get(event_type, "Hermes needs your input")
    # Try to surface a human-readable detail for the push body.
    for key in ("message", "prompt", "description", "question", "text", "command"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            detail = value.strip().replace("\n", " ")[:140]
            return f"{label}: {detail}"
    return label


async def _interaction_observer_loop() -> None:
    while True:
        token = _fetch_session_token()
        if not token:
            await asyncio.sleep(_RECONNECT_DELAY)
            continue
        url = f"{_WS_BASE}?token={token}"
        try:
            import websockets
            async with websockets.connect(url, open_timeout=10) as ws:
                while True:
                    raw = await asyncio.wait_for(ws.recv(), timeout=120)
                    try:
                        frame = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if frame.get("method") != "event":
                        continue
                    params = frame.get("params") or {}
                    etype = params.get("type", "")
                    if etype in _INTERACTION_EVENTS:
                        payload = params.get("payload") or {}
                        send_push(
                            _INTERACTION_LABELS.get(etype, "Hermes"),
                            _interaction_body(etype, payload),
                            tag=f"interaction-{etype}-{int(time.time()*1000)}",
                            data={"url": "/"},
                        )
        except asyncio.TimeoutError:
            # No event within the heartbeat window — still connected, loop.
            continue
        except Exception:
            pass
        await asyncio.sleep(_RECONNECT_DELAY)


def _start_interaction_observer() -> None:
    def runner() -> None:
        try:
            asyncio.run(_interaction_observer_loop())
        except Exception:
            pass

    thread = threading.Thread(
        target=runner,
        name="mc-push-interaction-observer",
        daemon=True,
    )
    thread.start()
