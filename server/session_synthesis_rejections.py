"""Local rejection log for session_synthesis candidates.

BDH owns the session_synthesis candidate lifecycle and exposes only
stage/candidates/apply — there is no BDH reject endpoint. When a human rejects
a candidate in Curate, Mission Control records the reason locally so it is not
lost and can feed the model's next run, but it never calls BDH.

The log is a profile-aware JSONL file under the vault-brain directory
(``<hermes-home>/vault-brain/session-synthesis-rejections.jsonl``), one JSON
object per rejection. It stores only safe metadata — candidate_id, vault_id,
synthesis_id, session_id, title, reason, and timestamp — never transcript or
candidate definition content.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from hermes_paths import hermes_vault_brain_dir

_LOCK = threading.Lock()


def _rejections_path() -> Path:
    return hermes_vault_brain_dir() / "session-synthesis-rejections.jsonl"


def record_rejection(
    *,
    candidate_id: str,
    vault_id: str,
    synthesis_id: str = "",
    session_id: str = "",
    title: str = "",
    reason: str = "",
) -> Dict[str, Any]:
    """Append one rejection record and return it.

    Safe fields only; the raw transcript and candidate definition are never
    written. Idempotent per candidate: a second rejection for the same
    candidate_id overwrites the previous reason rather than duplicating rows.
    """
    record = {
        "candidate_id": candidate_id,
        "vault_id": vault_id,
        "synthesis_id": synthesis_id,
        "session_id": session_id,
        "title": title,
        "reason": reason,
        "rejected_at": datetime.now(timezone.utc).isoformat(),
    }
    with _LOCK:
        path = _rejections_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = _read_all(path)
        kept = [entry for entry in existing if entry.get("candidate_id") != candidate_id]
        kept.append(record)
        tmp = path.with_suffix(".jsonl.tmp")
        tmp.write_text(
            "".join(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n" for entry in kept),
            encoding="utf-8",
        )
        tmp.replace(path)
    return record


def list_rejections() -> List[Dict[str, Any]]:
    """Return all rejection records, newest last (append order)."""
    with _LOCK:
        return _read_all(_rejections_path())


def _read_all(path: Path) -> List[Dict[str, Any]]:
    if not path.is_file():
        return []
    entries: List[Dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except ValueError:
                continue
            if isinstance(data, dict):
                entries.append(data)
    except OSError:
        return []
    return entries
