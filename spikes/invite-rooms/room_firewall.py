"""Fail-closed room-only HTTP forwarding policy for the network spike.

This is deliberately transport-agnostic: connectors turn an authenticated
inner request into ``Request`` and pass it here before calling the owner-fixed
native loopback backend.  It never performs proxy discovery or accepts a target
URL from the request.
"""
from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import unquote


class FirewallDenied(Exception):
    """The request or native response is outside the room-only contract."""


class UpstreamAmbiguous(FirewallDenied):
    """The request was already forwarded; its native outcome is unknown.

    Must surface as 5xx: the native peer client treats any 4xx on ``POST /v1/runs``
    as proof of non-admission, which would be a false claim after forwarding.
    """


@dataclass(frozen=True)
class Request:
    method: str
    target: str
    headers: tuple[tuple[str, str], ...] = ()
    body: bytes = b""


@dataclass(frozen=True)
class UpstreamResponse:
    status: int
    headers: tuple[tuple[str, str], ...] = ()
    body: bytes = b""


@dataclass(frozen=True)
class ForwardedRequest:
    method: str
    path: str
    headers: tuple[tuple[str, str], ...]
    body: bytes


_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_SAFE_SCOPE = frozenset({"room_id", "home_install_id", "authority_gateway_id", "authority_epoch", "member_id", "target_install_id", "target_profile"})
_NATIVE_DISPATCH_FIELDS = frozenset({
    "protocol_version", "room_id", "home_install_id", "authority_gateway_id", "authority_epoch",
    "member_id", "target_install_id", "target_profile", "task_id", "execution_generation",
    "source_event_seq", "cancellation_scope_id", "prompt", "prompt_digest", "capability_digest",
    "execution_policy_digest", "trace_id"})
_ALLOWED_METHODS = frozenset({"GET", "POST"})
_MAX_REFRESH_TTL = 24 * 60 * 60  # gateway.hosted_room_peer.MAX_DISPATCH_GRANT_TTL_SECONDS
_ALLOWED_HEADERS = frozenset({"accept", "authorization", "content-type", "idempotency-key"})
_HOP_BY_HOP = frozenset({"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer", "transfer-encoding", "upgrade", "host", "cookie", "forwarded", "via", "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto"})
_RESPONSE_HEADERS = frozenset({"content-type", "content-length"})
_MAX_RESPONSE_HEADERS = 8
_MAX_RESPONSE_VALUE = 64 * 1024
_MAX_RESPONSE_DEPTH = 4
_MAX_RESPONSE_FIELDS = 32
_PRIVATE_RESPONSE_FIELDS = frozenset({"approval", "tool", "tool_args", "tool_arguments", "trace", "event", "session", "config", "history"})


def _deny(message: str) -> None:
    raise FirewallDenied(message)


def _headers(request: Request) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw_name, value in request.headers:
        name = raw_name.lower()
        if name in result:
            _deny("duplicate header")
        if not name or name in _HOP_BY_HOP or name not in _ALLOWED_HEADERS:
            _deny("header is not allowlisted")
        if not isinstance(value, str) or "\r" in value or "\n" in value or len(value) > 4096:
            _deny("invalid header value")
        result[name] = value
    return result


def _path(target: str) -> str:
    if not isinstance(target, str) or not target or len(target) > 2048:
        _deny("invalid request target")
    if "?" in target or "#" in target or "\\" in target or "//" in target:
        _deny("ambiguous request target")
    if unquote(target) != target or "%" in target:
        _deny("encoded request target is forbidden")
    if not target.startswith("/") or ".." in target.split("/"):
        _deny("path traversal or non-origin target")
    return target


# Machine codes the native host client branches on (needs_reauthorization, terminal revoke,
# not_admitted drift). Only the code crosses the firewall; native messages never do.
_FORWARDED_ERROR_CODES = frozenset({
    "invalid_room_grant", "room_reauthorization_required", "room_execution_policy_changed",
    "room_capability_catalog_changed", "invalid_room_dispatch", "idempotency_key_conflict",
    "run_not_found", "run_not_active", "invalid_room_grant_refresh", "invalid_room_grant_revoke"})


def _error_envelope(body: bytes) -> bytes:
    code = None
    try:
        error = json.loads(body[:4096].decode("utf-8")).get("error")
        if isinstance(error, dict) and error.get("code") in _FORWARDED_ERROR_CODES:
            code = error["code"]
    except (UnicodeDecodeError, ValueError, AttributeError):
        pass
    return json.dumps({"error": {"message": "native request rejected", "type": "room_error", "code": code}},
                      separators=(",", ":")).encode()


# Route-specific response fields the native peer client reads
# (tui_gateway/hosted_room_peer_http.py refresh_grant / revoke_grant). The replacement
# grant goes back only to the legitimate holder, inside the inner mTLS stream.
_ROUTE_RESPONSE_FIELDS = {
    "/v1/room-members/grants/refresh": frozenset({"object", "grant", "expires_at", "status_expires_at"}),
    "/v1/room-members/grants/revoke": frozenset({"object", "revoked"}),
}


def _empty_json_object(body: bytes) -> bool:
    """No body, or exactly the JSON object ``{}`` (whitespace-free, as the native client sends)."""
    return body in (b"", b"{}")


def _json(body: bytes, maximum: int = 256 * 1024) -> Mapping[str, Any]:
    if len(body) > maximum:
        _deny("body exceeds limit")
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FirewallDenied("body must be UTF-8 JSON") from exc
    if not isinstance(value, dict):
        _deny("body must be an object")
    return value


def _validated_scope(scope: Mapping[str, Any]) -> dict[str, Any]:
    """Return a complete, bounded owner binding or fail closed.

    Every dispatch coordinate is security-sensitive.  A partial binding would
    make the omitted coordinate request-controlled, so completeness is part of
    the constructor contract rather than something checked per request.
    """
    if not isinstance(scope, Mapping) or set(scope) != _SAFE_SCOPE:
        raise ValueError("expected scope must contain every fixed coordinate")
    clean: dict[str, Any] = {}
    for key in _SAFE_SCOPE:
        value = scope[key]
        if key == "authority_epoch":
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError("authority_epoch must be a positive integer")
        else:
            if not isinstance(value, str) or not value or len(value) > 256:
                raise ValueError("expected scope values must be bounded non-empty strings")
            if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
                raise ValueError("expected scope values must not contain control characters")
        clean[key] = value
    return clean


def _scope_matches(scope: Mapping[str, Any], expected: Mapping[str, str]) -> bool:
    return all(scope.get(key) == value for key, value in expected.items())


def _sanitize_response(response: UpstreamResponse, extra_fields: frozenset[str] = frozenset()) -> UpstreamResponse:
    if not isinstance(response.status, int) or isinstance(response.status, bool):
        _deny("invalid response status")
    response_headers: dict[str, str] = {}
    if not isinstance(response.headers, (tuple, list)) or len(response.headers) > _MAX_RESPONSE_HEADERS:
        _deny("response headers exceed limit")
    for raw_name, value in response.headers:
        name = raw_name.lower() if isinstance(raw_name, str) else ""
        if name in response_headers or name not in _RESPONSE_HEADERS:
            _deny("response header is not allowlisted")
        if not isinstance(value, str) or "\r" in value or "\n" in value or len(value) > 512:
            _deny("invalid response header value")
        response_headers[name] = value
    if response.status < 200 or response.status >= 300:
        # Never pass through redirect locations or arbitrary error payloads.
        if 300 <= response.status < 400:
            _deny("redirect response")
        return UpstreamResponse(response.status, (("content-type", "application/json"),), _error_envelope(response.body))
    if len(response.body) > 16 * 1024 * 1024:
        _deny("response exceeds limit")
    try:
        payload = json.loads(response.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FirewallDenied("native response is not JSON") from exc
    if not isinstance(payload, dict):
        _deny("native response must be an object")
    def check_shape(value: Any, depth: int = 0) -> None:
        if depth > _MAX_RESPONSE_DEPTH:
            _deny("native response is too deeply nested")
        if isinstance(value, str):
            if len(value.encode("utf-8")) > _MAX_RESPONSE_VALUE:
                _deny("native response field exceeds limit")
        elif isinstance(value, dict):
            if len(value) > _MAX_RESPONSE_FIELDS:
                _deny("native response has too many fields")
            for key, child in value.items():
                if not isinstance(key, str) or len(key) > 128:
                    _deny("invalid native response field name")
                if key in _PRIVATE_RESPONSE_FIELDS:
                    _deny("private native payload present")
                check_shape(child, depth + 1)
        elif isinstance(value, list):
            if len(value) > _MAX_RESPONSE_FIELDS:
                _deny("native response list exceeds limit")
            for child in value:
                check_shape(child, depth + 1)
        elif not isinstance(value, (int, float, bool)) and value is not None:
            _deny("invalid native response field type")
    check_shape(payload)
    allowed = {
        "run_id", "task_id", "execution_generation", "status", "output", "error", "acknowledged", "replayed",
        "installation_id", "protocol_versions", "link_modes", "persistent_process", "text",
        "attachments", "execution_policy", "catalog_digest", "endpoint", "catalog",
        "room_id", "home_install_id", "authority_gateway_id", "authority_epoch", "member_id", "target_profile",
    } | extra_fields
    for key, value in payload.items():
        if key in {"run_id", "task_id", "status"} and (not isinstance(value, str) or len(value) > 128):
            _deny("native response identity/status is invalid")
        if key == "execution_generation" and value is not None and (
                not isinstance(value, int) or isinstance(value, bool) or value < 0):
            _deny("native execution generation is invalid")
        if key == "acknowledged" and not isinstance(value, bool):
            _deny("native acknowledgement is invalid")
    clean = {key: payload[key] for key in payload if key in allowed}
    # Tool/approval/private payloads are never forwarded, even nested in a permitted field.
    for forbidden in _PRIVATE_RESPONSE_FIELDS:
        if forbidden in payload:
            _deny("private native payload present")
    encoded = json.dumps(clean, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return UpstreamResponse(response.status, (("content-type", "application/json"), ("content-length", str(len(encoded)))), encoded)


class RoomFirewall:
    """Validate and forward only the fixed room-member native contract."""

    def __init__(self, *, expected_scope: Mapping[str, str], backend: Callable[[ForwardedRequest], UpstreamResponse], verify_native: Callable[[str, Mapping[str, Any]], bool], backend_timeout_seconds: float = 30.0):
        self._scope = _validated_scope(expected_scope)
        self._backend = backend
        self._verify_native = verify_native
        if not 0 < backend_timeout_seconds <= 30:
            raise ValueError("backend timeout must be between 0 and 30 seconds")
        self._backend_timeout_seconds = backend_timeout_seconds

    def _call_backend(self, request: ForwardedRequest) -> UpstreamResponse:
        """Apply a deadline; a runaway backend is not forcibly cancelled.

        The owner-configured loopback backend must also implement its own
        cancellation/HTTP timeout.  The daemon worker prevents the connector
        from waiting forever, but cannot undo arbitrary Python side effects.
        """
        result: list[UpstreamResponse] = []
        error: list[BaseException] = []

        def invoke() -> None:
            try:
                result.append(self._backend(request))
            except BaseException as exc:  # surface backend failures as denials
                error.append(exc)

        started = time.monotonic()
        worker = threading.Thread(target=invoke, daemon=True)
        worker.start()
        worker.join(self._backend_timeout_seconds)
        if worker.is_alive() or time.monotonic() - started > self._backend_timeout_seconds:
            raise UpstreamAmbiguous("native backend deadline exceeded")
        if error:
            raise UpstreamAmbiguous("native backend failed") from error[0]
        if len(result) != 1 or not isinstance(result[0], UpstreamResponse):
            _deny("native backend returned an invalid response")
        return result[0]

    def forward(self, request: Request) -> UpstreamResponse:
        method = request.method.upper() if isinstance(request.method, str) else ""
        if method not in _ALLOWED_METHODS:
            _deny("method is not allowlisted")
        path = _path(request.target)
        headers = _headers(request)
        auth = headers.get("authorization", "")
        if not auth.startswith("HermesRoom ") or not auth[11:].strip() or "Bearer " in auth:
            _deny("only HermesRoom authorization is accepted")
        if "cookie" in headers:
            _deny("cookie authentication is forbidden")
        if method == "GET" and path == "/v1/room-members/capabilities":
            if request.body:
                _deny("capabilities body is forbidden")
        elif method == "POST" and path == "/v1/runs":
            if headers.get("content-type") != "application/json" or "idempotency-key" not in headers:
                _deny("dispatch requires JSON and idempotency key")
            payload = _json(request.body)
            if set(payload) != {"input", "hosted_room_dispatch"}:
                _deny("dispatch body fields are not exact")
            dispatch = payload["hosted_room_dispatch"]
            if not isinstance(dispatch, dict) or set(dispatch) != _NATIVE_DISPATCH_FIELDS:
                _deny("dispatch coordinates are not exact")
            if not _scope_matches(dispatch, self._scope):
                _deny("dispatch scope mismatch")
            for field in ("protocol_version", "authority_epoch", "execution_generation", "source_event_seq"):
                if not isinstance(dispatch[field], int) or isinstance(dispatch[field], bool) or dispatch[field] < 1:
                    _deny("native dispatch integer is invalid")
            if not isinstance(payload["input"], str) or not payload["input"] or len(payload["input"].encode()) > 256 * 1024:
                _deny("input is not bounded text")
        elif method == "GET" and (match := re.fullmatch(r"/v1/runs/([^/]+)", path)):
            if not _RUN_ID.fullmatch(match.group(1)) or request.body:
                _deny("invalid run status request")
            if not self._verify_native(match.group(1), self._scope):
                _deny("native run verifier rejected status")
        elif method == "POST" and (match := re.fullmatch(r"/v1/runs/([^/]+)/stop", path)):
            # The native peer client posts a literal ``{}`` (hosted_room_peer_http._post_run_action).
            if not _RUN_ID.fullmatch(match.group(1)) or not _empty_json_object(request.body):
                _deny("invalid stop request")
            if not self._verify_native(match.group(1), self._scope):
                _deny("native run verifier rejected stop")
        elif method == "POST" and path == "/v1/room-members/grants/revoke":
            # Host-side disband/leave must reach the target or the native Room stays undisbandable.
            if not _empty_json_object(request.body):
                _deny("grant revoke accepts no fields")
        elif method == "POST" and path == "/v1/room-members/grants/refresh":
            # Renewal is bounded natively by the target-owner's hard horizon (status_expires_at).
            payload = _json(request.body)
            ttl = payload.get("ttl_seconds")
            if set(payload) != {"ttl_seconds"} or isinstance(ttl, bool) or not isinstance(ttl, (int, float)) \
                    or not 0 < ttl <= _MAX_REFRESH_TTL:
                _deny("grant refresh accepts only a bounded ttl_seconds")
        else:
            _deny("route is not allowlisted")
        forwarded_headers = (
            ("authorization", auth),
            ("accept", headers.get("accept", "application/json")),
            ("content-type", "application/json"),
        )
        if "idempotency-key" in headers:
            forwarded_headers += (("idempotency-key", headers["idempotency-key"]),)
        forwarded = ForwardedRequest(method, path, forwarded_headers, request.body)
        response = self._call_backend(forwarded)
        try:
            return _sanitize_response(response, _ROUTE_RESPONSE_FIELDS.get(path, frozenset()))
        except UpstreamAmbiguous:
            raise
        except FirewallDenied as exc:  # withheld response of a request the target already saw
            raise UpstreamAmbiguous(str(exc)) from exc
