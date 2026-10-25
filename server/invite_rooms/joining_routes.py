"""Owner-authenticated imports HTTP adapter. Never mounted outside routes.handle_request."""
from __future__ import annotations

import json
import os
import re

from invite_rooms import runtime_config, runtime_joining
from invite_rooms.invitation import InvitationError
from invite_rooms.joining import JoiningError
from invite_rooms.joining_store import JoiningStoreError

_PATH = re.compile(r"^/imports/(?:(preview)|(import-[0-9a-f]{32})(?:/(metadata|redeem|scope|consent|resolve))?)$")
_FIELDS = {"preview": {"code", "host_id"}, "metadata": set(), "redeem": set(),
           "scope": {"room_id", "member_id"}, "consent": {"approve"}, "resolve": set()}
#: Actions that carry NO field at all: a bodiless POST (no Content-Type, no
#: Content-Length) is the same request as ``{}`` for these.  Any body that IS
#: sent is still parsed and validated against the (empty) field set below.
_FIELDLESS = frozenset({"metadata", "redeem", "resolve"})
_NOT_FOUND = {"import_unavailable", "invalid_import_id", "peer_binding_mismatch", "unknown_host"}
_BAD = {"invalid_invitation", "invalid_invitation_origin", "invalid_owner_scope", "invalid_decision"}
_CONFLICT = {"redeem_ambiguous", "import_state_conflict", "redeem_required", "owner_scope_required",
             "host_approval_required", "consent_scope_conflict", "manual_reconciliation_required", "host_link_changed"}


def _body(handler, fields):
    from invite_rooms.routes import _MAX_ROUTE_BODY_BYTES, _reject_duplicate_keys
    lengths = handler.headers.get_all("Content-Length", [])
    try:
        if len(lengths) != 1 or not re.fullmatch(r"[0-9]+", lengths[0]):
            raise ValueError()
        size = int(lengths[0])
        if size > _MAX_ROUTE_BODY_BYTES:
            handler._json(413, {"error": "payload_too_large"})
            return None
        raw = handler.rfile.read(size)
        if len(raw) != size:
            raise ValueError()
        def constant(_):
            raise ValueError()
        body = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys, parse_constant=constant)
        if not isinstance(body, dict) or set(body) - fields:
            raise ValueError()
        # All browser fields are scalar; reject arbitrary nested JSON before use.
        if any(isinstance(v, (dict, list)) for v in body.values()):
            raise ValueError()
    except (ValueError, UnicodeDecodeError, RecursionError):
        handler._json(400, {"error": "bad_request", "code": "invalid_body"})
        return None
    return body


def handle(handler, method, path, suffix, params):
    # Imported late to avoid a circular import and reuse the existing HTTP envelope.
    from invite_rooms.routes import _content_type_is_json
    config_loaded = runtime_config.load_service()
    owner = None
    if config_loaded is not None:
        owner = runtime_joining.load_owner(config_loaded[2])
    if owner is None:
        handler._json(404, {"error": "not_found", "path": path})
        return True
    try:
        origins = handler.headers.get_all("Origin", [])
        if origins and (len(origins) != 1 or origins[0] != os.getenv("MISSION_CONTROL_ALLOWED_ORIGIN") or not origins[0]):
            handler._json(403, {"error": "forbidden_origin"})
            return True
        match = _PATH.fullmatch(suffix)
        if match is None:
            handler._json(404, {"error": "not_found", "path": path})
            return True
        preview, import_id, action = match.groups()
        action = "preview" if preview else action or "get"
        if method != ("GET" if action == "get" else "POST"):
            handler._json(404, {"error": "not_found", "path": path})
            return True
        if params or handler.headers.get("Transfer-Encoding") or (action == "get" and handler.headers.get("Content-Length") not in (None, "0")):
            handler._json(400, {"error": "bad_request", "code": "invalid_body"})
            return True
        body = {}
        if action != "get":
            bodiless = (action in _FIELDLESS and handler.headers.get("Content-Length") in (None, "0")
                        and not (handler.headers.get("Content-Type") or "").strip())
            if not bodiless:
                if not _content_type_is_json(handler):
                    handler._json(400, {"error": "bad_request", "code": "invalid_content_type"})
                    return True
                body = _body(handler, _FIELDS[action])
                if body is None:
                    return True
            valid = True
            if action == "preview":
                valid = isinstance(body.get("code"), str) and ("host_id" not in body or isinstance(body["host_id"], str))
            elif action == "scope":
                valid = set(body) == _FIELDS[action] and all(isinstance(v, str) for v in body.values())
            elif action == "consent":
                valid = set(body) == {"approve"} and type(body["approve"]) is bool
            if not valid:
                handler._json(400, {"error": "bad_request", "code": "invalid_body"})
                return True
        try:
            result = owner.execute(action, import_id, body)
        except (JoiningError, JoiningStoreError, InvitationError) as exc:
            code = str(exc)
            if code in _NOT_FOUND:
                handler._json(404, {"error": "not_found"})
            elif code in _BAD:
                handler._json(400, {"error": "invalid_request", "code": code})
            elif code in _CONFLICT:
                handler._json(409, {"error": "conflict", "code": code})
            else:
                handler._json(503, {"error": "unavailable", "code": "control_unavailable"})
            return True
        except Exception:
            # Sanitized at the routes layer (type name only, no values, no traceback).
            raise
        handler._json(200, {"import": result})
        return True
    finally:
        owner.close()
