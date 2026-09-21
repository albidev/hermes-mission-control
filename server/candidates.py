#!/usr/bin/env python3
"""Candidate management for the nightly brain approval flow.

Candidates are YAML-frontmatter .md files written by vault-brain-v2.py into
<hermes-home>/vault-brain/candidates/ (default ~/.hermes/vault-brain/candidates/,
profile-aware via server/hermes_paths.py). Each has a status:
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
import json
import sys
import re
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


SERVER_DIR = Path(__file__).resolve().parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from hermes_paths import get_hermes_home, hermes_vault_dir


def _vault_brain_dir() -> Path:
    from hermes_paths import hermes_vault_brain_dir

    return hermes_vault_brain_dir()


def _default_candidates_dir() -> Path:
    return _vault_brain_dir() / "candidates"


def _vaults_file() -> Path:
    return _vault_brain_dir() / "curate-vaults.yaml"


def _routing_file() -> Path:
    return get_hermes_home() / "vault-routing.yaml"


DEFAULT_CANDIDATES_DIR = _default_candidates_dir()
DEFAULT_QUARANTINE_DAYS = float(os.environ.get("VB_QUARANTINE_DAYS", "1"))
MIN_MERGE_CHARS = 120


def _load_vaults() -> Dict[str, Dict[str, Any]]:
    """Load the local candidate map used by Curate."""
    path = _vaults_file()
    if not path.exists():
        return {}
    try:
        import yaml
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return {str(k): dict(v) for k, v in (data.get("vaults") or {}).items()}
    except Exception:
        return {}


def _load_routing_vaults() -> Dict[str, Dict[str, Any]]:
    """Load the broader evidence-routing registry, if configured."""
    path = _routing_file()
    if not path.exists():
        return {}
    try:
        import yaml
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return {str(k): dict(v) for k, v in (data.get("vaults") or {}).items()}
    except Exception:
        return {}


def _candidates_dir(vault: Optional[str] = None) -> Optional[Path]:
    """Resolve a candidate directory without falling back to Core for known
    non-candidate vaults."""
    if vault and vault != "core":
        mapping = _load_vaults().get(vault)
        if mapping and mapping.get("candidates_dir"):
            return Path(os.path.expanduser(str(mapping["candidates_dir"])))
        return None
    return Path(os.environ.get("VB_CANDIDATES", str(DEFAULT_CANDIDATES_DIR)))


def _as_bool(value: Any, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() not in {"0", "false", "no", "off"}


def _display_vault_label(vault_id: str) -> str:
    return vault_id.replace("-", " ").title()


def list_vaults() -> List[Dict[str, Any]]:
    """Return every configured routing vault plus candidate capabilities."""
    candidate_map = _load_vaults()
    routing_map = _load_routing_vaults()
    vault_ids = list(dict.fromkeys(["core", *routing_map.keys(), *candidate_map.keys()]))
    out: List[Dict[str, Any]] = []
    for vid in vault_ids:
        candidate_config = candidate_map.get(vid) or {}
        routing_config = routing_map.get(vid) or {}
        routes = routing_config.get("routes")
        routes = routes if isinstance(routes, dict) else {}
        candidate_enabled = vid == "core" or bool(candidate_config.get("candidates_dir"))
        writable = _as_bool(routing_config.get("writable"), default=True)
        candidate_dir = _candidates_dir(vid) if candidate_enabled else None
        candidates = list_candidates(vault=vid) if candidate_enabled else []
        review_enabled = "review_inbox" in routes
        if candidate_enabled:
            mode = "candidates"
        elif not writable:
            mode = "read_only"
        elif review_enabled:
            mode = "review_only"
        else:
            mode = "storage_only"
        out.append({
            "id": vid,
            "label": candidate_config.get("label") or routing_config.get("name") or _display_vault_label(vid),
            "candidates_dir": str(candidate_dir) if candidate_dir else "",
            "candidate_enabled": candidate_enabled,
            "review_enabled": review_enabled,
            "writable": writable,
            "read_only": not writable,
            "mode": mode,
            "candidate_count": len(candidates),
            "pending_count": sum(1 for c in candidates if c.get("status") == "pending"),
            "reviewed_count": sum(1 for c in candidates if c.get("status") != "pending"),
        })
    return out


def can_curate(vault: Optional[str] = None) -> bool:
    """Return whether approve/reject mutations are allowed for a vault."""
    vault_id = vault or "core"
    if _candidates_dir(vault_id) is None:
        return False
    routing_config = _load_routing_vaults().get(vault_id) or {}
    return _as_bool(routing_config.get("writable"), default=True)


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
    if d is None or not d.exists():
        return []
    out = []
    for p in sorted(d.glob("*.md")):
        c = _read_candidate(p)
        if c and (status is None or c.get("status") == status):
            out.append(c)
    return out


def _find_by_id(cid: str, vault: Optional[str] = None, filename: Optional[str] = None) -> Optional[Path]:
    d = _candidates_dir(vault)
    if d is None or not d.exists():
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
    """All configured candidate dirs to scan for promote/rejection-feedback."""
    default = _candidates_dir(None)
    dirs = [default] if default is not None else []
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


def _jev_benchmark_dir() -> Path:
    """Resolve the Jev provider without assuming a developer's checkout path."""
    candidates: List[Path] = []
    explicit_dir = os.environ.get("JEV_BENCHMARK_DIR", "").strip()
    if explicit_dir:
        candidates.append(Path(os.path.expanduser(explicit_dir)))
    harness_root = os.environ.get("BDH_GRAPH_HARNESS_DIR", "").strip()
    if harness_root:
        candidates.append(Path(os.path.expanduser(harness_root)) / "benchmarks")
    # Temporary vendor: keep MC runnable while the provider is later extracted
    # into a proper shared runtime package.
    candidates.append(SERVER_DIR)
    # Backward-compatible local default; deployments should configure one of the
    # variables above instead of relying on this machine-specific path.
    candidates.append(Path(os.path.expanduser(
        "~/Projects/bdh-graph-harness/benchmarks")))
    seen = set()
    for directory in candidates:
        directory = directory.resolve()
        if directory in seen:
            continue
        seen.add(directory)
        if (directory / "jev_benchmark.py").is_file():
            return directory
    searched = ", ".join(str(path) for path in candidates)
    raise ImportError(
        "jev_benchmark.py not found; set JEV_BENCHMARK_DIR or "
        f"BDH_GRAPH_HARNESS_DIR (searched: {searched})"
    )


def _merge_check(dest: Path, body: str) -> Dict[str, Any]:
    """Pre-write collision check for promote_ready().

    Returns {action: write|skip|needs_review, existing_excerpt, similarity}.
    Mechanical slug match first; a Jev complementary/conflicting judgment is
    applied only when both bodies are substantive (>= MIN_MERGE_CHARS).
    Degradation: on any error, action=needs_review (never overwrite blind).
    """
    if not dest.exists():
        return {"action": "write", "similarity": None}
    existing = dest.read_text(encoding="utf-8")
    if existing.strip() == body.strip():
        return {"action": "skip", "similarity": 1.0}
    # both bodies too thin to merge automatically -> human review
    if len(existing.strip()) < MIN_MERGE_CHARS or len(body.strip()) < MIN_MERGE_CHARS:
        return {"action": "needs_review", "similarity": None,
                "existing_excerpt": existing.strip()[:300]}

    # Jev judgment: identical / complementary / conflicting
    try:
        sys.path.insert(0, str(_jev_benchmark_dir()))
        from jev_benchmark import CloudTypeSafeProvider  # type: ignore
        provider = CloudTypeSafeProvider()
        state = (
            "Obsidian vault note merge check. A candidate note is about to be "
            f"promoted to '{dest.name}' which already exists.\n"
            f"EXISTING note body:\n{existing[:1200]}\n\n"
            f"NEW candidate body:\n{body[:1200]}\n"
        )
        questions = {
            "mrg": {
                "type": "choice",
                "instructions": (
                    "Decide how the new candidate relates to the existing note."
                ),
                "criteria": {
                    "identical": (
                        "the new body adds nothing the existing note lacks; "
                        "safe to skip promotion"
                    ),
                    "complementary": (
                        "the two bodies each contain distinct information; "
                        "a merge (union of content) is appropriate"
                    ),
                    "conflicting": (
                        "the two contradict each other or the merge would "
                        "be misleading; a human must decide"
                    ),
                },
            }
        }
        resp = provider.evaluate(state, questions)
        answer = (resp.get("answers") or {}).get("mrg") or {}
        choice = answer.get("choice")
        conf = float(answer.get("confidence") or 0.0)
        if choice == "identical" and conf >= 0.8:
            return {"action": "skip", "similarity": conf}
        if choice == "complementary" and conf >= 0.8:
            return {"action": "merge", "similarity": conf}
        return {"action": "needs_review", "similarity": conf,
                "jev_choice": choice}
    except Exception as exc:  # noqa: BLE001 — fail-closed
        print(f"[merge_check] Jev unavailable: {exc}", file=sys.stderr)
        return {"action": "needs_review", "similarity": None,
                "error": str(exc)}


def _merge_bodies(existing: str, incoming: str, cand: Dict[str, Any]) -> str:
    """Union-merge an incoming candidate into an existing note.

    Existing content remains primary; tags and sources are unioned and a
    provenance section records the incoming candidate. Conflicting notes never
    reach this function because _merge_check returns needs_review instead.
    """
    def _field(text: str, key: str) -> List[str]:
        match = re.search(rf"^{key}:\s*\[([^\]]*)\]", text, re.MULTILINE)
        if not match:
            return []
        return [item.strip().strip('"') for item in match.group(1).split(",") if item.strip()]

    ex_tags = _field(existing, "tags")
    ex_sources = _field(existing, "sources")
    in_tags = _field(incoming, "tags")
    in_sources = _field(incoming, "sources")
    merged_tags = sorted(set(ex_tags) | set(in_tags))
    merged_sources = sorted(set(ex_sources) | set(in_sources))

    def _union_line(text: str, key: str, values: List[str]) -> str:
        pattern = re.compile(rf"^{key}:.*$", re.MULTILINE)
        joined = ", ".join(values)
        if pattern.search(text):
            return pattern.sub(f"{key}: [{joined}]", text, count=1)
        return text

    out = _union_line(existing, "tags", merged_tags)
    out = _union_line(out, "sources", merged_sources)
    out = re.sub(
        r"^updated:.*$",
        f"updated: {datetime.now(timezone.utc).date().isoformat()}",
        out,
        count=1,
        flags=re.MULTILINE,
    )

    incoming_description = re.search(
        r"^description:\s*>\s*$\n\s*([^\n]+)", incoming, re.MULTILINE
    )
    same_prefix = incoming[:400].strip() == existing[:400].strip()
    if incoming_description and not same_prefix:
        out += (
            f"\n\n## Merged variant ({datetime.now(timezone.utc).date().isoformat()})\n\n"
            f"{incoming_description.group(1).strip()}\n"
            f"\nmerged_from candidate: {cand.get('id', '?')} "
            f"(promote_ready merge-check, conf {cand.get('jev_confidence', 'n/a')})"
        )
    return out


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
        return Path(os.environ.get("VB_VAULT", str(hermes_vault_dir())))

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
                # move to this vault's wiki/concepts — unless it's a cron-brief
                # report artifact, which is a dated delivery log, not a concept:
                # it goes to projects/<prefix>/reports/<run-id>.md (Plan B,
                # Albi 2026-09-21: "per me sono importanti come report").
                is_report = (c.get("type") or "") == "cron-brief" or str(c.get("id") or p.stem).startswith("cron-brief-")
                if is_report:
                    profile = c.get("profile") or "crossnection-delivery"
                    prefix = str(profile).split("-")[0] or "crossnection"
                    reports_dir = vault / "projects" / prefix / "reports"
                    reports_dir.mkdir(parents=True, exist_ok=True)
                    dest = reports_dir / f"{p.stem}.md"
                    body = c.get("body", "")
                    dest.write_text(body + "\n", encoding="utf-8")
                    c["status"] = "promoted"
                    c["promoted_at"] = now.isoformat()
                    c["promote_note"] = "archived to projects reports (delivery log)"
                    _write_candidate(p, c, body)
                    promoted.append(c)
                    continue
                concepts_dir = vault / "wiki" / "concepts"
                concepts_dir.mkdir(parents=True, exist_ok=True)
                slug = re.sub(r"[^a-z0-9]+", "-", (c.get("title") or "concept").lower()).strip("-")
                dest = concepts_dir / f"{slug}.md"
                body = c.get("body", "")
                # Convert vault sources into wikilinks so the BDH graph creates
                # edges from this new concept to the nodes that generated it.
                # Without [[...]] links the promoted note is an isolated node.
                body = _append_source_wikilinks(body)
                # Pre-write merge check (parity with bdh-nightly-consolidation
                # curate/candidates.py 535c185): never overwrite blind.
                # Repairs the Synaptic Pruning Rule slug-collision data loss.
                check = _merge_check(dest, body)
                if check["action"] == "needs_review":
                    c["status"] = "needs_review"
                    c["merge_check"] = json.dumps(check)[:200]
                    _write_candidate(p, c, body)
                    continue
                if check["action"] == "skip":
                    c["status"] = "promoted"
                    c["promoted_at"] = now.isoformat()
                    c["promote_note"] = "promoted-duplicate: existing note kept"
                    _write_candidate(p, c, body)
                    promoted.append(c)
                    continue
                if check["action"] == "merge":
                    existing = dest.read_text(encoding="utf-8")
                    merged = _merge_bodies(existing, body, c)
                    dest.write_text(merged, encoding="utf-8")
                    c["status"] = "promoted"
                    c["promoted_at"] = now.isoformat()
                    c["promote_note"] = "merged into existing note"
                    _write_candidate(p, c, body)
                    promoted.append(c)
                    continue
                # action == write
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
    return Path(os.environ.get("VB_VAULT", str(hermes_vault_dir())))


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
