"""Mission Control proxy for vault-scoped BDH synthesis activity."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

import session_synthesis_rejections


class SynthesisProxyError(RuntimeError):
    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


def _bdh_base_url() -> str:
    return os.environ.get("BDH_API_URL", "http://127.0.0.1:8643").rstrip("/")


def _request(path: str, *, method: str = "GET", payload: dict[str, Any] | None = None) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        f"{_bdh_base_url()}{path}",
        data=body,
        method=method,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            raw = response.read().decode("utf-8")
            status = int(response.status)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise SynthesisProxyError(f"BDH returned HTTP {exc.code}: {detail}", exc.code) from exc
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        raise SynthesisProxyError(f"BDH unavailable: {type(exc).__name__}: {exc}") from exc
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SynthesisProxyError("BDH returned invalid JSON") from exc
    if status < 200 or status >= 300 or not isinstance(value, dict):
        raise SynthesisProxyError(f"BDH returned invalid response ({status})", status)
    return value


def load_synthesis_activity(vault_id: str | None = None) -> dict[str, Any]:
    query = f"?vault_id={urllib.parse.quote(vault_id)}" if vault_id else ""
    return _request(f"/api/synthesis-activity{query}")


def _safe_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _safe_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_detail_map(raw: Any, *, allowed: set[str]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    return {key: raw[key] for key in allowed if key in raw and isinstance(raw[key], (str, int, float, bool, list, dict, type(None)))}


def _safe_candidate(raw: dict[str, Any]) -> dict[str, Any]:
    """Map a BDH candidate to the full review-safe MC shape.

    The modal may show the complete concept definition and provenance useful
    for human review, but never receives transcript hashes, raw transcript,
    prompts, or model request payloads.
    """
    provenance = raw.get("provenance") if isinstance(raw.get("provenance"), dict) else {}
    source_ref = _safe_str(provenance.get("source_ref"))
    if not source_ref:
        source_notes = provenance.get("source_notes")
        if isinstance(source_notes, list):
            titles: list[str] = []
            for note in source_notes:
                title = _safe_str(note)
                if title:
                    titles.append(title)
            source_ref = ", ".join(titles[:3]) or None
    provenance_detail = _safe_detail_map(provenance, allowed={
        "session_title", "source_ref", "source_notes", "source_node_ids",
        "activated_note_count", "extractor_model", "extractor_provider",
        "merge_similarity", "source", "would_conflict",
    })
    return {
        "source": _safe_str(raw.get("source")) or "session_synthesis",
        "candidate_id": _safe_str(raw.get("candidate_id")) or "",
        "synthesis_id": _safe_str(raw.get("synthesis_id")) or "",
        "session_id": _safe_str(raw.get("session_id")) or "",
        "vault_id": _safe_str(raw.get("vault_id")) or "",
        "title": _safe_str(raw.get("title")) or "",
        "definition": _safe_str(raw.get("definition")) or "",
        "confidence": _safe_str(raw.get("confidence")) or "",
        "status": _safe_str(raw.get("status")) or "pending_review",
        "created_at": _safe_str(raw.get("created_at")),
        "accepted_count": _safe_int(raw.get("accepted_count")),
        "context_only_count": _safe_int(raw.get("context_only_count")),
        "provenance": provenance_detail,
        "extra": _safe_detail_map(raw.get("extra"), allowed={"activated_from", "slug"}),
        "safe_provenance": {
            "session_title": _safe_str(provenance.get("session_title")),
            "created_at": _safe_str(raw.get("created_at")),
            "source_ref": source_ref,
            "concept_summary": _safe_str(raw.get("definition")),
        },
    }


def load_synthesis_candidates(
    vault_id: str | None = None,
    status: str | None = None,
    synthesis_id: str | None = None,
) -> dict[str, Any]:
    """List vault-scoped session_synthesis candidates for Curate review.

    Mirrors BDH ``GET /api/synthesis/candidates`` and projects each candidate
    through :func:`_safe_candidate` so no transcript/definition leakage reaches
    the frontend.
    """
    params: dict[str, str] = {}
    if vault_id:
        params["vault_id"] = vault_id
    if status:
        params["status"] = status
    if synthesis_id:
        params["synthesis_id"] = synthesis_id
    query = f"?{urllib.parse.urlencode(params)}" if params else ""
    raw = _request(f"/api/synthesis/candidates{query}")
    candidates = raw.get("candidates") if isinstance(raw.get("candidates"), list) else []
    rejected_ids = {
        str(entry.get("candidate_id"))
        for entry in (session_synthesis_rejections.list_rejections() or [])
        if isinstance(entry, dict)
        and (not vault_id or str(entry.get("vault_id") or "") == vault_id)
    }
    safe = [
        _safe_candidate(candidate)
        for candidate in candidates
        if isinstance(candidate, dict)
        and str(candidate.get("candidate_id") or "") not in rejected_ids
    ]
    return {
        "vault_id": raw.get("vault_id"),
        "count": len(safe),
        "candidates": safe,
    }


def get_synthesis_candidate(
    candidate_id: str,
    vault_id: str | None = None,
) -> dict[str, Any] | None:
    """Resolve one candidate by id within a vault, or None.

    Used by the apply endpoint to enforce vault isolation and to forward the
    BDH-owned correlation tuple (never the client-supplied one), so a
    cross-vault or tampered request cannot reach BDH.
    """
    snapshot = load_synthesis_candidates(vault_id=vault_id)
    for candidate in snapshot["candidates"]:
        if candidate.get("candidate_id") == candidate_id:
            return candidate
    return None


def approve_synthesis_candidate(
    candidate_id: str,
    synthesis_id: str,
    session_id: str,
    vault_id: str,
    source: str = "session_synthesis",
) -> dict[str, Any]:
    """Record human approval in BDH before the apply call."""
    return _request(
        "/api/synthesis/approve",
        method="POST",
        payload={
            "candidate_id": candidate_id,
            "synthesis_id": synthesis_id,
            "session_id": session_id,
            "vault_id": vault_id,
            "source": source,
        },
    )


def apply_synthesis_candidate(
    candidate_id: str,
    synthesis_id: str,
    session_id: str,
    vault_id: str,
    source: str = "session_synthesis",
) -> dict[str, Any]:
    """Apply an approved session_synthesis candidate via BDH.

    Mirrors BDH ``POST /api/synthesis/apply``. BDH owns extraction, dedupe,
    create/merge, Hebbian, audit, and graph refresh; MC only forwards the
    correlation tuple. The response carries ``status`` in
    {created, merged, noop, conflict, failed} plus optional ``operation_id`` /
    ``note_path`` — never transcript content.
    """
    return _request(
        "/api/synthesis/apply",
        method="POST",
        payload={
            "candidate_id": candidate_id,
            "synthesis_id": synthesis_id,
            "session_id": session_id,
            "vault_id": vault_id,
            "source": source,
        },
    )


def revert_synthesis(operation_id: str, vault_id: str | None = None) -> dict[str, Any]:
    result = _request(
        "/api/synthesis/revert",
        method="POST",
        payload={"operation_id": operation_id, **({"vault_id": vault_id} if vault_id else {})},
    )
    if result.get("refresh_required"):
        try:
            refresh = _request(
                "/api/refresh-graph",
                method="POST",
                payload={**({"vault_id": vault_id} if vault_id else {})},
            )
            result["graph_refresh"] = refresh
        except SynthesisProxyError as exc:
            result["graph_refresh"] = {"status": "failed", "error": str(exc)}
    return result
