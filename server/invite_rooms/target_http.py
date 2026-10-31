"""Fixed-route loopback client and strict verifier for Core ``POST /v1/runs``."""
from __future__ import annotations

import hashlib
import json
import re
import asyncio
import math
import time
from types import MappingProxyType
from dataclasses import dataclass
from typing import Callable, Mapping, Optional

from invite_rooms.consent import canonical
from invite_rooms.runtime_config import TargetHttpConfig, RuntimeConfigError, _validate_target_values
from invite_rooms.target_material import TargetMaterialSource, ResolvedTargetMaterial
from invite_rooms.peer_registry import MaterialScope, admission_executor, LANE_MATERIAL, ADMISSION_DEADLINE
from invite_rooms.admission_receipts import AdmissionReceiptStore
from invite_rooms import target_http_io

_RUN_ID = re.compile(r"\Arun_[0-9a-f]{32}\Z")
# Derived from Core run assignments/persistence, not Bot Chat delivery states.
REPLAY_STATUSES = frozenset({"queued", "running", "waiting_for_approval", "stopping",
                             "completed", "failed", "cancelled", "interrupted"})
VERIFIER_VERSION = "mc-target-http-verifier-v1"
MAX_RESPONSE_BYTES = 64 * 1024


class TargetHttpError(ValueError):
    """Sanitized fail-closed target error."""


HttpResponse = target_http_io.HttpResponse
UNSET = object()
MAX_REQUEST_BYTES = 256 * 1024
_DISPATCH_FIELDS = frozenset({"room_id", "home_install_id", "authority_gateway_id", "authority_epoch", "member_id",
    "target_install_id", "target_profile", "execution_policy_digest", "protocol_version", "task_id",
    "execution_generation", "source_event_seq", "cancellation_scope_id", "trace_id", "prompt",
    "prompt_digest", "capability_digest"})
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,255}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


def request_fingerprint(body: Mapping, *, gateway_session_key: str = "") -> str:
    if not isinstance(body, dict) or not isinstance(gateway_session_key, str):
        raise TargetHttpError("invalid_request")
    return hashlib.sha256(canonical({"body": body, "gateway_session_key": gateway_session_key})).hexdigest()


def _route(config: TargetHttpConfig) -> str:
    return "/v1/runs" if config.profile == "default" else f"/p/{config.profile}/v1/runs"


def _no_duplicate_keys(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError
        value[key] = item
    return value

def _finite_int(value):
    return int(value)

def _parse_body(raw: bytes):
    if type(raw) is not bytes or len(raw) > MAX_REQUEST_BYTES:
        raise TargetHttpError("invalid_request")
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicate_keys,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
                          parse_int=_finite_int,
                          parse_float=lambda value: float(value) if math.isfinite(float(value)) else (_ for _ in ()).throw(ValueError()))
    except Exception:
        raise TargetHttpError("invalid_request") from None

def _same_json_type(a, b):
    if type(a) is not type(b): return False
    if type(a) is dict:
        return set(a) == set(b) and all(_same_json_type(a[k], b[k]) for k in a)
    if type(a) is list:
        return len(a) == len(b) and all(_same_json_type(x, y) for x, y in zip(a, b))
    return a == b

def _deadline(value):
    if type(value) not in (int, float) or isinstance(value, bool) or not math.isfinite(value) or time.monotonic() >= value:
        raise TargetHttpError("invalid_request")

def _validate_source_shape(source, resolved, forwarded_body, deadline, body, config):
    if type(source) is not TargetMaterialSource or type(resolved) is not ResolvedTargetMaterial:
        raise TargetHttpError("invalid_request")
    if type(resolved.scope) is not MaterialScope or type(resolved.credential_ref) is not str or not resolved.credential_ref:
        raise TargetHttpError("invalid_request")
    if type(forwarded_body) is not bytes or len(forwarded_body) > MAX_REQUEST_BYTES:
        raise TargetHttpError("invalid_request")
    _deadline(deadline)
    if type(body) is not dict: raise TargetHttpError("invalid_request")
    parsed = _parse_body(forwarded_body)
    if not _same_json_type(parsed, body) or json.dumps(parsed, sort_keys=True, separators=(",", ":")).encode() != json.dumps(body, sort_keys=True, separators=(",", ":")).encode():
        raise TargetHttpError("invalid_request")
    try:
        from invite_rooms.material_validators import validate_scope_attributes
        validate_scope_attributes(resolved.scope, generation=resolved.scope.pairing_generation, profile=config.profile)
    except Exception:
        raise TargetHttpError("invalid_request") from None
    if type(config.profile) is not str or resolved.scope.target_profile != config.profile:
        raise TargetHttpError("target_profile_mismatch")
    if set(parsed) != {"input", "hosted_room_dispatch"}:
        raise TargetHttpError("invalid_request")
    dispatch = parsed.get("hosted_room_dispatch")
    if type(dispatch) is not dict or set(dispatch) != _DISPATCH_FIELDS:
        raise TargetHttpError("invalid_request")
    if type(parsed.get("input")) is not str or not parsed["input"] or parsed["input"] != dispatch["prompt"]:
        raise TargetHttpError("invalid_request")
    positive = ("protocol_version", "authority_epoch", "execution_generation", "source_event_seq")
    if any(type(dispatch[k]) is not int or isinstance(dispatch[k], bool) or dispatch[k] < 1 for k in positive):
        raise TargetHttpError("invalid_request")
    identifiers = ("room_id", "home_install_id", "authority_gateway_id", "member_id", "target_install_id",
                   "target_profile", "task_id", "cancellation_scope_id", "trace_id")
    if any(type(dispatch[k]) is not str or _IDENTIFIER.fullmatch(dispatch[k]) is None for k in identifiers):
        raise TargetHttpError("invalid_request")
    if any(type(dispatch[k]) is not str or _DIGEST.fullmatch(dispatch[k]) is None
           for k in ("capability_digest", "execution_policy_digest", "prompt_digest")):
        raise TargetHttpError("invalid_request")
    if type(dispatch["prompt"]) is not str or not dispatch["prompt"].strip() \
            or len(dispatch["prompt"].encode("utf-8")) > 256 * 1024 \
            or hashlib.sha256(dispatch["prompt"].encode("utf-8")).hexdigest() != dispatch["prompt_digest"]:
        raise TargetHttpError("invalid_request")
    if dispatch["room_id"] != resolved.scope.planned_room_id or dispatch["home_install_id"] != resolved.scope.owner_installation_id or dispatch["member_id"] != resolved.scope.member_id or dispatch["target_install_id"] != resolved.scope.target_install_id or dispatch["target_profile"] != resolved.scope.target_profile or dispatch["capability_digest"] != resolved.scope.catalog_digest or dispatch["execution_policy_digest"] != resolved.scope.policy_digest:
        raise TargetHttpError("invalid_request")
    key = f"room:{dispatch['task_id']}:{dispatch['execution_generation']}"
    if not 1 <= len(key) <= 255 or any(ord(ch) < 33 or ord(ch) > 126 for ch in key):
        raise TargetHttpError("invalid_request")
    return parsed, key


def _material_identity(loaded: Mapping) -> tuple[str, bytes, bytes]:
    """Return a detached identity for one genuine Source.load result."""
    if type(loaded) is not dict:
        raise TargetHttpError("target_admission_ambiguous")
    try:
        grant = loaded["grant"]
        catalog = loaded["catalog"]
        binding = loaded["binding"]
        if type(grant) is not str:
            raise ValueError
        return (grant, canonical(json.loads(json.dumps(catalog, sort_keys=True))),
                canonical(json.loads(json.dumps(binding, sort_keys=True))))
    except Exception:
        raise TargetHttpError("target_admission_ambiguous") from None


def _require_same_material(initial: tuple[str, bytes, bytes], loaded: Mapping) -> None:
    if _material_identity(loaded) != initial:
        raise TargetHttpError("target_admission_ambiguous")


def _validate_response(response) -> tuple[int, dict]:
    try:
        status = response.status if hasattr(response, "status") else response[0]
        raw = response.body if hasattr(response, "body") else response[1]
        if type(status) is not int or not 100 <= status <= 599 or not isinstance(raw, (bytes, bytearray)):
            raise ValueError
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ValueError
        parsed = json.loads(bytes(raw).decode("utf-8"), object_pairs_hook=_no_duplicate_keys)
        if not isinstance(parsed, dict) or set(parsed) != {"run_id", "status", "replayed"}:
            raise ValueError
        if type(parsed["run_id"]) is not str or not _RUN_ID.fullmatch(parsed["run_id"]):
            raise ValueError
        if type(parsed["status"]) is not str or not parsed["status"]:
            raise ValueError
        if type(parsed["replayed"]) is not bool:
            raise ValueError
        return status, parsed
    except TargetHttpError:
        raise
    except Exception:
        raise TargetHttpError("invalid_target_response") from None


class TargetHttpClient:
    """POST only to the configured loopback origin and code-owned route."""

    def __init__(self, config: TargetHttpConfig, *, transport: Optional[Callable] = None):
        if not isinstance(config, TargetHttpConfig):
            raise TargetHttpError("invalid_target_config")
        try:
            _validate_target_values(config.base_url, config.profile)
        except (RuntimeConfigError, AttributeError, TypeError):
            raise TargetHttpError("invalid_target_config") from None
        self.config = config
        self._transport_supplied = transport is not None
        self._transport = transport or self._urlopen

    def _urlopen(self, url: str, headers: dict, payload: bytes, *, deadline=None):
        try:
            return target_http_io.post_loopback(url, headers, payload, deadline=deadline)
        except Exception:
            raise TargetHttpError("target_unavailable") from None

    def admit_run(self, *, body: dict, idempotency_key: str, admission_store,
                  scope=UNSET, material_store=UNSET, gateway_session_key: str = "",
                  forwarded_body=UNSET, source=UNSET, resolved=UNSET, deadline=UNSET) -> dict:
        source_lane = any(value is not UNSET for value in (forwarded_body, source, resolved, deadline))
        if source_lane:
            if (scope is not UNSET or material_store is not UNSET or forwarded_body is UNSET
                    or source is UNSET or resolved is UNSET or deadline is UNSET or self._transport_supplied):
                raise TargetHttpError("invalid_request")
            if type(source) is not TargetMaterialSource or type(resolved) is not ResolvedTargetMaterial \
                    or type(admission_store) is not AdmissionReceiptStore:
                raise TargetHttpError("invalid_request")
            try:
                loop = asyncio.get_running_loop()
                if loop.is_running(): raise TargetHttpError("invalid_request")
            except RuntimeError:
                pass
            _deadline(deadline)
            inherited = ADMISSION_DEADLINE.get()
            if inherited is not None:
                _deadline(inherited)
            effective = deadline if inherited is None else min(deadline, inherited)
            _deadline(effective)
            parsed, derived_key = _validate_source_shape(source, resolved, forwarded_body, effective, body, self.config)
            if type(idempotency_key) is not str or idempotency_key != derived_key:
                raise TargetHttpError("invalid_request")
            def worker(end):
                return TargetHttpClient._admit_source_worker(self, body=parsed, forwarded_body=forwarded_body,
                    idempotency_key=derived_key, admission_store=admission_store,
                    source=source, resolved=resolved, deadline=end,
                    gateway_session_key=gateway_session_key)
            try:
                result = asyncio.run(admission_executor().run(worker, effective, lane=LANE_MATERIAL, end_deadline=effective))
            except TargetHttpError:
                raise
            except Exception:
                raise TargetHttpError("target_admission_ambiguous") from None
            _deadline(effective)
            return result
        if any(value is None for value in (scope, material_store)):
            raise TargetHttpError("invalid_request")
        if (not isinstance(body, dict) or type(idempotency_key) is not str
                or not 1 <= len(idempotency_key) <= 255
                or any(ord(ch) < 33 or ord(ch) > 126 for ch in idempotency_key)):
            raise TargetHttpError("invalid_request")
        if not isinstance(gateway_session_key, str):
            raise TargetHttpError("invalid_request")
        try:
            try:
                _validate_target_values(self.config.base_url, self.config.profile)
            except (RuntimeConfigError, AttributeError, TypeError):
                raise TargetHttpError("invalid_target_config") from None
            material = material_store.load(scope)
            grant = material["grant"]
            if scope.target_profile != self.config.profile:
                raise TargetHttpError("target_profile_mismatch")
            fingerprint = request_fingerprint(body, gateway_session_key=gateway_session_key)
            prior = admission_store.find(scope=scope, generation=scope.pairing_generation,
                                         profile=self.config.profile, base_url=self.config.base_url,
                                         idempotency_key=idempotency_key, request_fingerprint=fingerprint)
            if prior is None:
                # Reservation is durable and process-safe, but is not an admission receipt.
                try:
                    admission_store.reserve(scope=scope, generation=scope.pairing_generation,
                                            profile=self.config.profile, base_url=self.config.base_url,
                                            idempotency_key=idempotency_key,
                                            request_fingerprint=fingerprint)
                except Exception as exc:
                    if getattr(exc, "args", [None])[0] == "idempotency_binding_mismatch":
                        raise TargetHttpError("idempotency_binding_mismatch") from None
                    raise
            payload = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
            headers = {"Content-Type": "application/json", "Accept": "application/json",
                       "Authorization": "HermesRoom " + grant, "Idempotency-Key": idempotency_key}
            if gateway_session_key:
                headers["X-Hermes-Session-Key"] = gateway_session_key
            response = self._transport(self.config.base_url + _route(self.config), headers, payload)
            status_code, result = _validate_response(response)
            if status_code != 202:
                raise TargetHttpError("target_rejected")
            if prior is None:
                if result["status"] != "started" or result["replayed"] is not False:
                    raise TargetHttpError("invalid_new_admission")
                admission_store.put(scope=scope, generation=scope.pairing_generation,
                                    profile=self.config.profile, base_url=self.config.base_url,
                                    idempotency_key=idempotency_key, request_fingerprint=fingerprint,
                                    run_id=result["run_id"], status=result["status"], replayed=False,
                                    verifier_version=VERIFIER_VERSION)
            else:
                if (result["replayed"] is not True or result["status"] not in REPLAY_STATUSES
                        or result["run_id"] != prior["run_id"]):
                    raise TargetHttpError("invalid_replay")
            return dict(result)
        except TargetHttpError:
            raise
        except Exception:
            raise TargetHttpError("target_admission_failed") from None

    def _admit_source_worker(self, *, body, forwarded_body, idempotency_key, admission_store,
                             source, resolved, deadline, gateway_session_key):
        _deadline(deadline)
        scope = resolved.scope
        fingerprint = request_fingerprint(body, gateway_session_key=gateway_session_key)
        # Authorize the exact existing material before any durable admission operation.
        initial = _material_identity(source.load(
            scope, credential_ref=resolved.credential_ref, deadline=deadline))
        _deadline(deadline)
        prior = admission_store.find(scope=scope, generation=scope.pairing_generation,
            profile=self.config.profile, base_url=self.config.base_url,
            idempotency_key=idempotency_key, request_fingerprint=fingerprint, deadline=deadline)
        if prior is None:
            prior = admission_store.reserve(scope=scope, generation=scope.pairing_generation,
                profile=self.config.profile, base_url=self.config.base_url,
                idempotency_key=idempotency_key, request_fingerprint=fingerprint, deadline=deadline)
        _deadline(deadline)
        loaded = source.load(scope, credential_ref=resolved.credential_ref, deadline=deadline)
        _require_same_material(initial, loaded)
        grant = loaded["grant"]
        headers = {"Content-Type": "application/json", "Accept": "application/json",
                   "Authorization": "HermesRoom " + grant, "Idempotency-Key": idempotency_key}
        if gateway_session_key: headers["X-Hermes-Session-Key"] = gateway_session_key
        _deadline(deadline)
        try:
            response = target_http_io.post_loopback(
                self.config.base_url + _route(self.config), headers, forwarded_body, deadline=deadline)
        except TimeoutError:
            # Once the bounded POST was entered, a timeout does not prove non-admission.
            raise TargetHttpError("target_admission_ambiguous") from None
        except Exception:
            raise TargetHttpError("target_unavailable") from None
        status_code, result = _validate_response(response)
        if status_code != 202: raise TargetHttpError("target_rejected")
        _deadline(deadline)
        if prior is None:
            if result["status"] != "started" or result["replayed"] is not False:
                raise TargetHttpError("invalid_new_admission")
            _require_same_material(initial, source.load(
                scope, credential_ref=resolved.credential_ref, deadline=deadline))
            _deadline(deadline)
            admission_store.put(scope=scope, generation=scope.pairing_generation,
                profile=self.config.profile, base_url=self.config.base_url,
                idempotency_key=idempotency_key, request_fingerprint=fingerprint,
                run_id=result["run_id"], status=result["status"], replayed=False,
                verifier_version=VERIFIER_VERSION, deadline=deadline)
        else:
            if result["replayed"] is not True or result["status"] not in REPLAY_STATUSES or result["run_id"] != prior["run_id"]:
                raise TargetHttpError("invalid_replay")
            _require_same_material(initial, source.load(
                scope, credential_ref=resolved.credential_ref, deadline=deadline))
        _deadline(deadline)
        # The receipt is durable, but success still requires a fresh authorization
        # immediately before the caller observes it.  A cutover is ambiguous.
        try:
            _require_same_material(initial, source.load(
                scope, credential_ref=resolved.credential_ref, deadline=deadline))
        except Exception:
            raise TargetHttpError("target_admission_ambiguous") from None
        return dict(result)


def admit_run(**kwargs) -> dict:
    client = kwargs.pop("client")
    return client.admit_run(**kwargs)
