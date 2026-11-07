"""Inert target-side dispatch backend for one bound ``TargetConnector``.

Only the exact ``POST /v1/runs`` reaches ``TargetHttpClient`` (source lane); every
other already-validated ``ForwardedRequest`` goes to the unchanged native loopback
passthrough with identical arguments.  The unique local import is discovered per
request from the connector-owned binding and immutable ``expected_scope`` only;
the request is never selector material, and ``TargetMaterialSource.resolve``
remains the authority.  No module state, cache, socket, thread or listener.

Denial classes: anything the backend refuses *before* entering
``TargetHttpClient.admit_run`` returns the generic 403; once ``admit_run`` is
entered every exception propagates, so the firewall reports an ambiguous 502.
"""
from __future__ import annotations

import json
import math
import time
from typing import Callable, Mapping

from invite_rooms.control.connector import native_http_backend
from invite_rooms.control.firewall import ForwardedRequest, UpstreamResponse
from invite_rooms.target_http import TargetHttpClient, _parse_body
from invite_rooms.target_material import TargetMaterialSource

_DENIED = UpstreamResponse(403, (("content-type", "application/json"),),
                           b'{"error":"room request denied"}')
_CONFIRMED_KEYS = frozenset({"host_id", "room_id", "lobby_id", "member_id", "binding"})


class _Refused(Exception):
    """Backend-owned pre-effect refusal; never carries detail."""


def _confirmed_matches(record: Mapping, confirmed, *, endpoint_id: str, expected_scope: Mapping) -> bool:
    if type(confirmed) is not dict or set(confirmed) != _CONFIRMED_KEYS:
        return False
    binding = confirmed["binding"]
    return (confirmed["host_id"] == expected_scope["home_install_id"]
            and confirmed["room_id"] == expected_scope["room_id"]
            and confirmed["member_id"] == expected_scope["member_id"]
            and confirmed["lobby_id"] == record.get("lobby_id")
            and type(binding) is dict
            and binding.get("endpoint_id") == endpoint_id
            and binding.get("target_install_id") == expected_scope["target_install_id"]
            and binding.get("target_profile") == expected_scope["target_profile"])


def select_target_import(joining_store, endpoint_id, generation, expected_scope, deadline) -> tuple[str, str]:
    """Return the local ``(lobby_id, invite_id)`` of the ONE matching ``consent_given`` import.

    Inputs are connector-owned only.  Zero or several matches raise ``_Refused``; never first-match.
    """
    peer = f"{endpoint_id}:{generation}"
    matches = []
    for record in joining_store.list_imports(deadline=deadline):
        if type(record) is not dict or record.get("host_peer_id") != peer or record.get("state") != "consent_given":
            continue
        import_id = record.get("import_id")
        if type(import_id) is not str:
            continue
        confirmed = joining_store.confirmed_scope(import_id, deadline=deadline)
        if _confirmed_matches(record, confirmed, endpoint_id=endpoint_id, expected_scope=expected_scope):
            matches.append(record)
    if len(matches) != 1:
        raise _Refused()
    lobby_id, invite_id = matches[0].get("lobby_id"), matches[0].get("invite_id")
    if type(lobby_id) is not str or type(invite_id) is not str:
        raise _Refused()
    return lobby_id, invite_id


def _header(request: ForwardedRequest, name: str) -> str:
    values = [value for key, value in request.headers if key.lower() == name]
    if len(values) != 1 or type(values[0]) is not str:
        raise _Refused()
    return values[0]


def _finite_future(deadline) -> None:
    if (type(deadline) not in (int, float) or isinstance(deadline, bool)
            or not math.isfinite(deadline) or time.monotonic() >= deadline):
        raise _Refused()


def make_target_dispatch_backend_factory(joining_store, authority, identity_reader, catalog_reader,
                                         target_http, admission_store) -> Callable:
    """Capture caller-owned dependencies; return ``factory(binding, expected_scope) -> native``."""

    def factory(binding, expected_scope: Mapping) -> Callable:
        scope = dict(expected_scope)
        endpoint_id, generation = binding.endpoint_id, binding.generation
        source = TargetMaterialSource(
            joining_store=joining_store, authority=authority, identity_reader=identity_reader,
            catalog_reader=catalog_reader, target_http=target_http,
            endpoint_id=endpoint_id, generation=generation)
        client = TargetHttpClient(target_http)
        passthrough = native_http_backend(target_http.base_url, timeout=binding.timeout)

        def native(request: ForwardedRequest, *, deadline) -> UpstreamResponse:
            if not (request.method == "POST" and request.path == "/v1/runs"):
                if scope.get("target_profile") != target_http.profile:
                    return _DENIED
                if target_http.profile != "default":
                    request = ForwardedRequest(request.method, "/p/" + target_http.profile + request.path,
                                               request.headers, request.body)
                return passthrough(request, deadline=deadline)
            try:
                _finite_future(deadline)
                if scope["target_profile"] != target_http.profile:
                    raise _Refused()
                key = _header(request, "idempotency-key")
                body = _parse_body(request.body)
                if type(body) is not dict:
                    raise _Refused()
                lobby_id, invite_id = select_target_import(joining_store, endpoint_id, generation, scope, deadline)
                resolved = source.resolve(lobby_id=lobby_id, invite_id=invite_id, deadline=deadline)
            except Exception:
                return _DENIED
            # admit_run entered: nothing below may become a 4xx; every exception is ambiguous.
            result = client.admit_run(body=body, idempotency_key=key, admission_store=admission_store,
                                      forwarded_body=request.body, source=source, resolved=resolved,
                                      deadline=deadline)
            payload = {name: result[name] for name in ("run_id", "status", "replayed")}
            return UpstreamResponse(202, (("content-type", "application/json"),),
                                    json.dumps(payload, separators=(",", ":")).encode())

        return native

    return factory
