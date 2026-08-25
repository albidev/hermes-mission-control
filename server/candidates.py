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
VAULTS_FILE = Path.home() / ".hermes" / "vault-brain" / "curate-vaults.yaml"


def _load_vaults() -> Dict[str, Dict[str, str]]:
    """Load the local per-vault candidate map (gitignored, outside the public
    MC repo). Returns {vault_id: {label, candidates_dir}}."""
    if not VAULTS_FILE.exists():
        return {}
    try:
        import yaml
        data = yaml.safe_load(VAULTS_FILE.read_text(encoding="utf-8")) or {}
        return {str(k): dict(v) for k, v in (data.get("vaults") or {}).items()}
    except Exception:
        return {}


def _candidates_dir(vault: Optional[str] = None) -> Path:
    """Resolve the candidate dir for a vault. With no vault (or 'core'), keeps
    the classic behaviour: VB_CANDIDATES env or the default dir."""
    if vault and vault != "core":
        mapping = _load_vaults().get(vault)
        if mapping and mapping.get("candidates_dir"):
            return Path(os.path.expanduser(str(mapping["candidates_dir"])))
    return Path(os.environ.get("VB_CANDIDATES", str(DEFAULT_CANDIDATES_DIR)))


def list_vaults() -> List[Dict[str, Any]]:
    """Return the vaults the Curate page can switch between. Always includes
    the default 'core' vault; the rest come from the local map."""
    mapping = _load_vaults()
    out = [{"id": "core", "label": "Core", "candidates_dir": str(_candidates_dir(None))}]
    for vid, m in mapping.items():
        if vid == "core":
            continue
        out.append({"id": vid, "label": m.get("label", vid),
                    "candidates_dir": str(_candidates_dir(vid))})
    return out


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


def _clean_body(body: str) -> str:
    """Strip leftover YAML/markdown separators and per-concept headers from a
    candidate body so the UI shows only readable content."""
    body = body.strip()
    # drop any ``` fence markers
    body = re.sub(r"^```\s*|```\s*$", "", body)
    # drop leading "# N. Title" headers
    lines = [ln for ln in body.splitlines() if not re.match(r"^\s*#\s+\d+\.", ln)]
    body = "\n".join(lines)
    # cut at the first separator that precedes another concept (mid or trailing)
    body = re.split(r"\n---(\n|$)", body)[0]
    return body.strip()


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
    # body = content after frontmatter, cleaned for display
    parts = text.split("---", 2)
    meta["body"] = _clean_body(parts[2]) if len(parts) > 2 else ""
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


def list_candidates(status: Optional[str] = None, vault: Optional[str] = None) -> List[Dict[str, Any]]:
    d = _candidates_dir(vault)
    if not d.exists():
        return []
    out = []
    for p in sorted(d.glob("*.md")):
        c = _read_candidate(p)
        if c and (status is None or c.get("status") == status):
            out.append(c)
    return out


def _find_by_id(cid: str, vault: Optional[str] = None, filename: Optional[str] = None) -> Optional[Path]:
    d = _candidates_dir(vault)
    if not d.exists():
        return None
    if filename:
        exact = d / Path(filename).name
        if exact.is_file():
            c = _read_candidate(exact)
            if c and c.get("id") == cid:
                return exact
    for p in d.glob("*.md"):
        c = _read_candidate(p)
        if c and c.get("id") == cid:
            return p
    return None


def _quarantine_delta(vault: Optional[str] = None) -> timedelta:
    """Quarantine window for a vault. Per-vault override (quarantine_hours in
    the local curate-vaults.yaml) wins; otherwise the global VB_QUARANTINE_DAYS
    (default 1 day)."""
    if vault and vault != "core":
        mapping = _load_vaults().get(vault) or {}
        qh = mapping.get("quarantine_hours")
        if qh is not None:
            return timedelta(hours=float(qh))
    days = float(os.environ.get("VB_QUARANTINE_DAYS", str(DEFAULT_QUARANTINE_DAYS)))
    return timedelta(days=days)


def approve(cid: str, vault: Optional[str] = None, filename: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Approve a candidate -> status approved, quarantine_until = now + delta."""
    p = _find_by_id(cid, vault, filename)
    if not p:
        return None
    c = _read_candidate(p)
    if not c:
        return None
    until = (datetime.now(timezone.utc) + _quarantine_delta(vault)).isoformat()
    c["status"] = "approved"
    c["approved_at"] = datetime.now(timezone.utc).isoformat()
    c["quarantine_until"] = until
    _write_candidate(p, c, c.get("body", ""))
    return _read_candidate(p)


def reject(cid: str, reason: str = "", vault: Optional[str] = None, filename: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Reject a candidate -> status rejected, rejection_reason = human feedback."""
    p = _find_by_id(cid, vault, filename)
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


def _all_candidate_dirs() -> List[Path]:
    """All candidate dirs to scan for promote/rejection-feedback: the default
    dir plus every per-vault dir from the local map."""
    dirs = [_candidates_dir(None)]
    for m in _load_vaults().values():
        if m.get("candidates_dir"):
            d = Path(os.path.expanduser(str(m["candidates_dir"])))
            if d not in dirs:
                dirs.append(d)
    return dirs


def _append_source_wikilinks(body: str) -> str:
    """Append a '## Sources' section of [[wikilinks]] derived from the body's
    `sources:` YAML entries, so the BDH graph creates edges from the promoted
    concept to the vault nodes that generated it.

    Only `vault:` sources map to vault notes (external: sources are repo/docs
    outside the vault and have no vault node to link). The wikilink target is
    the path after `vault:` with the `.md` stripped, e.g.
    `vault:wiki/entities/foo.md` -> `[[wiki/entities/foo]]`.
    """
    if not body or "[[wiki/" in body:
        return body  # already has wikilinks
    sources = re.findall(r"^\s*-\s*[\"']?vault:([^\s\"']+\.md)[\"']?\s*$", body, re.MULTILINE)
    if not sources:
        return body
    links = []
    for src in sources:
        target = src[:-3] if src.endswith(".md") else src  # strip .md
        links.append(f"- [[{target}]]")
    if not links:
        return body
    return body.rstrip() + "\n\n## Sources\n" + "\n".join(links) + "\n"


def promote_ready() -> List[Dict[str, Any]]:
    """Promote candidates whose quarantine has elapsed (status approved +
    quarantine_until <= now) to their vault's wiki/concepts. Scans every
    candidate dir (default + per-vault) and writes to the vault_dir for that
    vault. Returns promoted."""
    now = datetime.now(timezone.utc)
    promoted = []

    # vault -> (candidates_dir, vault_dir). Default "core" uses the global
    # VB_CANDIDATES / VB_VAULT. Per-vault dirs come from the local map.
    mapping = _load_vaults()

    def vault_target(vault_id: str) -> Path:
        m = mapping.get(vault_id) or {}
        if m.get("vault_dir"):
            return Path(os.path.expanduser(str(m["vault_dir"])))
        return Path(os.environ.get("VB_VAULT", str(Path.home() / "Documents" / "Hermes")))

    # build [(candidates_dir, vault_dir)]
    targets = [(str(_candidates_dir(None)), vault_target("core"))]
    for vid, m in mapping.items():
        if vid == "core":
            continue
        if m.get("candidates_dir"):
            targets.append((m["candidates_dir"], vault_target(vid)))

    for cand_dir, vault in targets:
        d = Path(os.path.expanduser(str(cand_dir)))
        if not d.exists():
            continue
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
                # move to this vault's wiki/concepts
                concepts_dir = vault / "wiki" / "concepts"
                concepts_dir.mkdir(parents=True, exist_ok=True)
                slug = re.sub(r"[^a-z0-9]+", "-", (c.get("title") or "concept").lower()).strip("-")
                dest = concepts_dir / f"{slug}.md"
                body = c.get("body", "")
                # Convert vault sources into wikilinks so the BDH graph creates
                # edges from this new concept to the nodes that generated it.
                # Without [[...]] links the promoted note is an isolated node.
                body = _append_source_wikilinks(body)
                # ensure frontmatter has type/tags/confidence from the concept block
                dest.write_text(body + "\n", encoding="utf-8")
                # mark promoted
                c["status"] = "promoted"
                c["promoted_at"] = now.isoformat()
                _write_candidate(p, c, body)
                promoted.append(c)
    return promoted


def vault_dirs_with_promotions() -> list:
    """Return the vault_dir of every vault whose candidate dir currently holds
    at least one promoted candidate. Used by the promote cron to know which
    vault repos need a commit+push."""
    promoted_dirs = set()
    mapping = _load_vaults()
    for vid, m in mapping.items():
        cand_dir = m.get("candidates_dir")
        if not cand_dir:
            continue
        d = Path(os.path.expanduser(str(cand_dir)))
        if not d.exists():
            continue
        has_promoted = any(
            (_read_candidate(p) or {}).get("status") == "promoted"
            for p in d.glob("*.md")
        )
        if has_promoted:
            promoted_dirs.add(vault_dir_for(vid))
    return sorted(promoted_dirs)


def vault_dir_for(vault_id: str) -> Path:
    """Resolve the vault_dir for a vault id (default core -> VB_VAULT)."""
    mapping = _load_vaults()
    m = mapping.get(vault_id) or {}
    if m.get("vault_dir"):
        return Path(os.path.expanduser(str(m["vault_dir"])))
    return Path(os.environ.get("VB_VAULT", str(Path.home() / "Documents" / "Hermes")))


def rejection_feedback() -> str:
    """Collect rejection_reason from rejected candidates as human feedback
    for the model's next run. Scans every candidate dir (default + per-vault)."""
    reasons = []
    for vault in _load_vaults():
        for c in list_candidates(status="rejected", vault=vault):
            r = c.get("rejection_reason", "").strip()
            if r:
                reasons.append(f"- {c.get('title', c.get('id'))}: {r}")
    for c in list_candidates(status="rejected"):
        r = c.get("rejection_reason", "").strip()
        if r:
            reasons.append(f"- {c.get('title', c.get('id'))}: {r}")
    return "\n".join(reasons)
