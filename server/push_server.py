#!/usr/bin/env python3
"""Web Push support for Mission Control.

Provides:
- VAPID public key lookup for the client's pushManager.subscribe() call.
- Subscription persistence (store/delete) so the backend can deliver pushes.
- Push delivery (send a notification to every stored subscription).
- A gateway watcher that fires a push whenever Hermes completes a response,
  so notifications arrive while the app is in the background or closed
  (the page's own JS is suspended then and cannot poll).

Requires pywebpush (installed in the same interpreter the telemetry server
runs under) and a VAPID keypair. If either is missing, push is disabled
gracefully and every call returns a "disabled" state rather than crashing.
"""
from __future__ import annotations

import base64
import json
import os
import threading
import time
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
    public_key = load_vapid_public_key()
    contact = vapid_contact()
    if not private_key or not public_key:
        return {"sent": 0, "failed": 0, "disabled": True}

    try:
        from pywebpush import webpush, WebPushException
    except ImportError:
        return {"sent": 0, "failed": 0, "disabled": True}

    payload = json.dumps({"title": title, "body": body, "tag": tag, "data": data or {}})

    sent = 0
    failed = 0
    for subscription in list_subscriptions():
        try:
            webpush(
                subscription_info=subscription,
                data=payload,
                vapid_private_key=private_key,
                vapid_claims={"sub": contact or "mailto:albi@mac-mini-01.taild7292a.ts.net"},
                timeout=10,
            )
            sent += 1
        except WebPushException as exc:
            # 404/410 means the subscription is gone — drop it.
            status = getattr(exc.response, "status_code", None)
            if status in (404, 410):
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
