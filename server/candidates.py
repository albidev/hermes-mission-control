#!/usr/bin/env python3
"""Candidate management for the nightly brain approval flow.

Candidates are YAML-frontmatter .md files written by vault-brain-v2.py into
~/.hermes/vault-brain/candidates/. Each has a status:
  pending      -> awaiting human review in Mission Control
  approved     -> human approved; enters quarantine (quarantine_until set)
  quarantined  -> approved + quarantine elapsed; ready to promote
  rejected     -> human rejected; rejection_reason is feedback for the model
  modified     -> human edited content, then approved

Quarantine is configurable (default 1 day) via VB_QUARANTINE_DAYS.
This module is standalone so it can later be extracted into a sidecar/plugin.
"""
from __future__ import annotations

import os
import re
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT_CANDIDATES_DIR = Path.home() / ".hermes" / "vault-brain" / "candidates"
DEFAULT_QUARANTINE_DAYS = float(os.environ.get("VB_QUARANTINE_DAYS", "1"))


def _candidates_dir() -> Path:
    return Path(os.environ.get("VB_CANDIDATES", str(DEFAULT_CANDIDATES_DIR)))


def _parse_frontmatter(text: str) -> Dict[str, Any]:
    """Parse YAML-ish frontmatter (simple key: value lines)."""
    meta: Dict[str, Any] = {}
    if not text.startswith("---"):
        return meta
    lines = text.splitlines()
    # skip opening ---
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip().strip('"').strip("'")
    return meta


def _read_candidate(path: Path) -> Optional[Dict[str, Any]]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    meta = _parse_frontmatter(text)
    if not meta:
        return None
    meta["_path"] = str(path)
    meta["_filename"] = path.name
    # body = content after frontmatter
    parts = text.split("---", 2)
    meta["body"] = parts[2].strip() if len(parts) > 2 else ""
    return meta


def _write_candidate(path: Path, meta: Dict[str, Any], body: str) -> None:
    lines = ["---"]
    for k, v in meta.items():
        if k.startswith("_"):
            continue
        if v is None:
            lines.append(f"{k}: null")
        else:
            lines.append(f'{k}: "{v}"')
    lines.append("---")
    lines.append("")
    lines.append(body)
    path.write_text("\n".join(lines), encoding="utf-8")


def list_candidates(status: Optional[str] = None) -> List[Dict[str, Any]]:
    d = _candidates_dir()
    if not d.exists():
        return []
    out = []
    for p in sorted(d.glob("*.md")):
        c = _read_candidate(p)
        if c and (status is None or c.get("status") == status):
            out.append(c)
    return out


def _find_by_id(cid: str) -> Optional[Path]:
    d = _candidates_dir()
    if not d.exists():
        return None
    for p in d.glob("*.md"):
        c = _read_candidate(p)
        if c and c.get("id") == cid:
            return p
    return None


def approve(cid: str) -> Optional[Dict[str, Any]]:
    """Approve a candidate -> status approved, quarantine_until = now + days."""
    p = _find_by_id(cid)
    if not p:
        return None
    c = _read_candidate(p)
    if not c:
        return None
    days = float(os.environ.get("VB_QUARANTINE_DAYS", str(DEFAULT_QUARANTINE_DAYS)))
    until = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
    c["status"] = "approved"
    c["approved_at"] = datetime.now(timezone.utc).isoformat()
    c["quarantine_until"] = until
    _write_candidate(p, c, c.get("body", ""))
    return _read_candidate(p)


def reject(cid: str, reason: str = "") -> Optional[Dict[str, Any]]:
    """Reject a candidate -> status rejected, rejection_reason = human feedback."""
    p = _find_by_id(cid)
    if not p:
        return None
    c = _read_candidate(p)
    if not c:
        return None
    c["status"] = "rejected"
    c["rejected_at"] = datetime.now(timezone.utc).isoformat()
    c["rejection_reason"] = reason
    _write_candidate(p, c, c.get("body", ""))
    return _read_candidate(p)


def promote_ready() -> List[Dict[str, Any]]:
    """Promote candidates whose quarantine has elapsed (status approved +
    quarantine_until <= now) to the vault wiki/concepts. Returns promoted."""
    d = _candidates_dir()
    if not d.exists():
        return []
    now = datetime.now(timezone.utc)
    promoted = []
    for p in d.glob("*.md"):
        c = _read_candidate(p)
        if not c or c.get("status") != "approved":
            continue
        q = c.get("quarantine_until")
        if not q:
            continue
        try:
            qdt = datetime.fromisoformat(q)
        except ValueError:
            continue
        if qdt <= now:
            # move to vault wiki/concepts
            vault = Path(os.environ.get("VB_VAULT", str(Path.home() / "Documents" / "Hermes")))
            concepts_dir = vault / "wiki" / "concepts"
            concepts_dir.mkdir(parents=True, exist_ok=True)
            slug = re.sub(r"[^a-z0-9]+", "-", (c.get("title") or "concept").lower()).strip("-")
            dest = concepts_dir / f"{slug}.md"
            body = c.get("body", "")
            # ensure frontmatter has type/tags/confidence from the concept block
            dest.write_text(body + "\n", encoding="utf-8")
            # mark promoted
            c["status"] = "promoted"
            c["promoted_at"] = now.isoformat()
            _write_candidate(p, c, body)
            promoted.append(c)
    return promoted


def rejection_feedback() -> str:
    """Collect rejection_reason from rejected candidates as human feedback
    for the model's next run."""
    reasons = []
    for c in list_candidates(status="rejected"):
        r = c.get("rejection_reason", "").strip()
        if r:
            reasons.append(f"- {c.get('title', c.get('id'))}: {r}")
    return "\n".join(reasons)
