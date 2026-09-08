#!/usr/bin/env python3
"""Curate plugin endpoints — HTTP request/response translation layer.

This module exports handler functions that match the names in manifest.json.
Each handler receives:
  - body: parsed JSON body (dict) or {}
  - params: urllib.parse.parse_qs result (dict of lists)
  - auth: auth context dict (unused but reserved)

Each handler returns a dict that gets JSON-serialized to the HTTP response.

The plugin loader dispatches to these functions based on the manifest.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from . import handlers


# ---------------------------------------------------------------------------
# Feature gate — Curate is only exposed when MC_ENABLE_BDH_CURATOR is truthy.
# Mirrors _candidates_enabled() in the old local_telemetry_server.py.
# ---------------------------------------------------------------------------

def _candidates_enabled() -> bool:
    import os
    return os.environ.get("MC_ENABLE_BDH_CURATOR", "").strip().lower() in {"1", "true", "yes", "on"}


class PluginError(Exception):
    """Raised for user-visible errors; maps to HTTP 4xx."""

    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# GET handlers
# ---------------------------------------------------------------------------

def listCandidates(body: Dict[str, Any], params: Dict[str, List[str]], auth: Any = None) -> Dict[str, Any]:
    """GET /api/local/candidates?status=&="""
    if not _candidates_enabled():
        raise PluginError(404, "feature_disabled", "BDH curator is disabled. Set MC_ENABLE_BDH_CURATOR=1 to enable.")
    status = (params.get("status") or [None])[0] or None
    vault = (params.get("vault") or [None])[0] or None
    cands = handlers.list_candidates(status=status, vault=vault)
    return {"candidates": cands, "count": len(cands), "vault": vault}


def listVaults(body: Dict[str, Any], params: Dict[str, List[str]], auth: Any = None) -> Dict[str, Any]:
    """GET /api/local/candidates/vaults"""
    if not _candidates_enabled():
        raise PluginError(404, "feature_disabled", "BDH curator is disabled. Set MC_ENABLE_BDH_CURATOR=1 to enable.")
    return {"vaults": handlers.list_vaults()}


# ---------------------------------------------------------------------------
# POST handlers
# ---------------------------------------------------------------------------

def approveCandidate(body: Dict[str, Any], params: Dict[str, List[str]], auth: Any = None) -> Dict[str, Any]:
    """POST /api/local/candidates/approve"""
    if not _candidates_enabled():
        raise PluginError(404, "feature_disabled", "BDH curator is disabled. Set MC_ENABLE_BDH_CURATOR=1 to enable.")
    cid = body.get("id", "")
    if not cid:
        raise PluginError(400, "bad_request", "Missing id.")
    filename = body.get("filename") or None
    vault = body.get("vault") or None
    if not handlers.can_curate(vault):
        raise PluginError(403, "vault_not_curable", "Candidate mutations are disabled for this vault.")
    cand = handlers.approve(cid, vault, filename)
    if not cand:
        raise PluginError(404, "not_found", f"Candidate {cid} not found.")
    return {"success": True, "candidate": cand}


def rejectCandidate(body: Dict[str, Any], params: Dict[str, List[str]], auth: Any = None) -> Dict[str, Any]:
    """POST /api/local/candidates/reject"""
    if not _candidates_enabled():
        raise PluginError(404, "feature_disabled", "BDH curator is disabled. Set MC_ENABLE_BDH_CURATOR=1 to enable.")
    cid = body.get("id", "")
    if not cid:
        raise PluginError(400, "bad_request", "Missing id.")
    filename = body.get("filename") or None
    reason = body.get("reason", "")
    vault = body.get("vault") or None
    if not handlers.can_curate(vault):
        raise PluginError(403, "vault_not_curable", "Candidate mutations are disabled for this vault.")
    cand = handlers.reject(cid, reason, vault, filename)
    if not cand:
        raise PluginError(404, "not_found", f"Candidate {cid} not found.")
    return {"success": True, "candidate": cand}
