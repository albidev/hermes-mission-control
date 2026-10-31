"""Injected material-control wiring for the acknowledged pinned control lane.

This module deliberately contains no service registration or global factories.  The
caller supplies the local stores, signer, registry and pinned connector.
"""
from __future__ import annotations

import asyncio
import functools
import inspect
import math
import threading
import time
import json
import secrets
import base64
from typing import Any, Callable
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from .consent import canonical

from .control.dispatcher import (ControlError, ClockProbeError, EXPIRY_LOSS_MS,
                                 local_deadline_from_utc, new_material_request_id, utc_now_ms,
                                 validate_clock_probe,
                                 validate_material_v2_receipt)
from .joining_material import MaterialError, TargetMaterialMint, validate_material
from .peer_registry import MaterialScope, PeerRegistryError
from .pairing_journal import MaterialPeerContext
from .material_receipts import ADMISSION_DEADLINE

_READY = {"status": "material_ready"}


class HostConnectorMaterialTransport:
    """Audited request-bound adapter for the real pinned HostConnector.

    This is deliberately concrete rather than a signature-based protocol: the
    adapter owns the final conversion to ``HostConnector.forward`` and therefore
    cannot silently discard the inherited deadline or admission predicate.
    """
    def __init__(self, connector):
        from .control.connector import HostConnector
        if not isinstance(connector, HostConnector):
            raise TypeError("pinned HostConnector required")
        self._connector = connector

    def send(self, wire: dict, *, deadline: float, admit):
        from .control.connector import ForwardedRequest
        if wire.get("op") == "status":
            result = self._connector.forward(
                ForwardedRequest("POST", "/control/joining", (),
                                 json.dumps(wire, separators=(",", ":")).encode()),
                admit=admit, deadline=deadline)
            return json.loads(result.body)
        request_id = wire.get("request_id")
        if not isinstance(request_id, str):
            raise MaterialError("control_unavailable")
        nonce = secrets.token_hex(16)
        probe_request = {"op": "material_probe", "v": 1, "operation": "material_request",
                         "request_id": request_id, "nonce": nonce, "t1_utc_ms": utc_now_ms()}
        probe_result = self._connector.forward(
            ForwardedRequest("POST", "/control/joining", (),
                             json.dumps(probe_request, separators=(",", ":")).encode()),
            admit=admit, deadline=deadline)
        probe = json.loads(probe_result.body)
        if (not isinstance(probe, dict) or set(probe) != {"op", "v", "operation", "endpoint_id", "generation",
                "request_id", "nonce", "t1_utc_ms", "t2_utc_ms", "t3_utc_ms"}
                or probe.get("request_id") != request_id or probe.get("nonce") != nonce
                or probe.get("endpoint_id") != self._connector.binding.endpoint_id
                or probe.get("generation") != self._connector.binding.generation):
            raise MaterialError("control_unavailable")
        probe["t4_utc_ms"] = utc_now_ms()
        wire = dict(wire, probe=probe)
        result = self._connector.forward(
            ForwardedRequest("POST", "/control/joining", (),
                             json.dumps(wire, separators=(",", ":")).encode()),
            admit=admit, deadline=deadline)
        return json.loads(result.body)


def _deadline(value: float | None) -> float:
    if value is None:
        return time.monotonic() + 10.0
    if type(value) not in (int, float) or not math.isfinite(value) or time.monotonic() >= value:
        raise MaterialError("control_unavailable")
    return float(value)


async def _await_deadline(awaitable, deadline: float):
    """Await a connector result without blocking the event loop.

    A synchronous connector cannot be force-stopped by asyncio.  Run it in the
    executor, race the Future against an explicit timer, and discard late
    results.  The caller never treats cancellation as cancellation of the
    underlying blocking operation, so a late result cannot create an effect.
    """
    future = asyncio.ensure_future(awaitable) if inspect.isawaitable(awaitable) else awaitable
    if not asyncio.isfuture(future) and not isinstance(future, asyncio.Future):
        raise TypeError("connector did not return an awaitable")
    done, _ = await asyncio.wait({future}, timeout=max(0.0, deadline - time.monotonic()))
    if future not in done:
        future.cancel()
        raise MaterialError("control_unavailable")
    return future.result()


async def _call_connector(connector, wire: dict, *, deadline: float, admit, allow_legacy=False):
    if not isinstance(connector, HostConnectorMaterialTransport) and not allow_legacy:
        raise MaterialError("control_unavailable")
    loop = asyncio.get_running_loop()
    if isinstance(connector, HostConnectorMaterialTransport):
        fn = functools.partial(connector.send, wire, deadline=deadline, admit=admit)
    else:
        fn = functools.partial(connector, wire, deadline=deadline, admit=admit)
    worker = loop.create_future()
    def invoke() -> None:
        try:
            value = fn()
        except BaseException as exc:
            try:
                loop.call_soon_threadsafe(_set_future_exception, worker, exc)
            except RuntimeError:
                # The request deadline may close the short-lived test/worker
                # loop before a deliberately late connector returns.
                pass
        else:
            try:
                loop.call_soon_threadsafe(_set_future_result, worker, value)
            except RuntimeError:
                pass
    threading.Thread(target=invoke, name="material-connector", daemon=True).start()
    try:
        result = await _await_deadline(worker, deadline)
        if inspect.isawaitable(result):
            result = await _await_deadline(result, deadline)
        return result
    except asyncio.CancelledError:
        worker.cancel()
        raise


def _set_future_result(future, value) -> None:
    if not future.done():
        future.set_result(value)


def _set_future_exception(future, exc) -> None:
    if not future.done():
        future.set_exception(exc)


class MaterialControlChannel:
    """Adapter from TargetMaterialMint's internal command vocabulary to wire RPC."""
    def __init__(self, connector: Callable[[dict], Any], *, lobby_id: str, invite_id: str,
                 deadline: float | None = None, admit: Callable[[], bool] | None = None,
                 lab: bool = False):
        if not isinstance(lobby_id, str) or not lobby_id or not isinstance(invite_id, str) or not invite_id:
            raise ValueError("material identifiers required")
        if not isinstance(lab, bool):
            raise ValueError("invalid material transport mode")
        if lab and (deadline is not None or admit is not None):
            raise ValueError("lab material transport cannot be request-bound")
        # The lab lane is an explicit fixture-only capability.  Connector type
        # is not authority: an arbitrary callable must never become product
        # compatible merely because request metadata was omitted.
        self._connector = connector
        self._lobby_id, self._invite_id = lobby_id, invite_id
        self._legacy_lab = lab
        self._deadline = deadline
        self._admit = admit or (lambda: True)

    def call(self, request: dict):
        if (not isinstance(self._connector, HostConnectorMaterialTransport)
                and not self._legacy_lab):
            raise MaterialError("control_unavailable")
        if not isinstance(request, dict) or request.get("invite_id") != self._invite_id:
            raise MaterialError("control_unavailable")
        op = request.get("op")
        if op == "material" and self._legacy_lab and "request_id" in request:
            # A named callable is only a non-request lab transport.  It must
            # never be reclassified as an authenticated v2 sender.
            raise MaterialError("control_unavailable")
        if op == "status":
            wire = {"op": "status", "lobby_id": self._lobby_id, "invite_id": self._invite_id}
        elif op == "material":
            if self._legacy_lab and set(request) == {"op", "invite_id", "envelope"} and isinstance(request["envelope"], dict):
                wire = {"op": "material_receipt", "lobby_id": self._lobby_id,
                        "invite_id": self._invite_id, "receipt": request["envelope"]}
            elif (set(request) != {"op", "invite_id", "envelope", "request_id", "expires_at_utc_ms", "probe",
                                   "operation", "operation_signature"}
                    or not isinstance(request["envelope"], dict)):
                raise MaterialError("control_unavailable")
            else:
                wire = {"op": "material_receipt", "v": 2, "lobby_id": self._lobby_id,
                        "invite_id": self._invite_id, "request_id": request["request_id"],
                        "expires_at_utc_ms": request["expires_at_utc_ms"], "probe": request["probe"],
                        "operation": request["operation"], "operation_signature": request["operation_signature"],
                        "receipt": request["envelope"]}
        else:
            raise MaterialError("control_unavailable")
        if self._deadline is not None and time.monotonic() >= self._deadline:
            raise MaterialError("control_unavailable")
        try:
            result = self._connector(wire)
            if inspect.isawaitable(result):
                # Do not leave a coroutine un-awaited when a legacy synchronous
                # caller accidentally hands us the real async transport.
                close = getattr(result, "close", None)
                if callable(close):
                    close()
                raise MaterialError("control_unavailable")
        except MaterialError:
            raise
        except Exception:
            raise MaterialError("control_unavailable") from None
        if not isinstance(result, dict) or (op != "status" and result != _READY):
            raise MaterialError("control_unavailable")
        return result

    async def call_async(self, request: dict, *, deadline: float | None = None,
                         admit: Callable[[], bool] | None = None):
        # Async invocation carries a request deadline/admission capability.  The
        # explicit lab lane is synchronous-only; never let it reach a product
        # request by selecting the legacy branch here.
        if (not isinstance(self._connector, HostConnectorMaterialTransport)
                and self._legacy_lab):
            raise MaterialError("control_unavailable")
        if not isinstance(request, dict) or request.get("invite_id") != self._invite_id:
            raise MaterialError("control_unavailable")
        op = request.get("op")
        if op == "material" and self._legacy_lab and "request_id" in request:
            # A named callable is only a non-request lab transport.  It must
            # never be reclassified as an authenticated v2 sender.
            raise MaterialError("control_unavailable")
        if op == "status":
            wire = {"op": "status", "lobby_id": self._lobby_id, "invite_id": self._invite_id}
        elif op == "material" and self._legacy_lab and set(request) == {"op", "invite_id", "envelope"} and isinstance(request["envelope"], dict):
            wire = {"op": "material_receipt", "lobby_id": self._lobby_id,
                    "invite_id": self._invite_id, "receipt": request["envelope"]}
        elif (op == "material" and set(request) == {"op", "invite_id", "envelope", "request_id",
                "expires_at_utc_ms", "probe", "operation", "operation_signature"}
                and isinstance(request["envelope"], dict)):
            wire = {"op": "material_receipt", "v": 2, "lobby_id": self._lobby_id,
                    "invite_id": self._invite_id, "request_id": request["request_id"],
                    "expires_at_utc_ms": request["expires_at_utc_ms"], "probe": request["probe"],
                    "operation": request["operation"], "operation_signature": request["operation_signature"],
                    "receipt": request["envelope"]}
        else:
            raise MaterialError("control_unavailable")
        end = deadline if deadline is not None else self._deadline
        end = _deadline(end)
        admission = admit or self._admit
        if time.monotonic() >= end or admission() is not True:
            raise MaterialError("control_unavailable")
        try:
            result = await _call_connector(self._connector, wire, deadline=end, admit=admission,
                                           allow_legacy=self._legacy_lab)
            if result is None:
                raise MaterialError("control_unavailable")
        except MaterialError:
            raise
        except Exception:
            raise MaterialError("control_unavailable") from None
        if (time.monotonic() >= end
                or admission() is not True
                or not isinstance(result, dict) or (op != "status" and result != _READY)):
            raise MaterialError("control_unavailable")
        return result


class MaterialControl:
    """Per-instance target/host handlers and the A-side request helper."""
    _pending_lock = threading.Lock()
    _pending_requests: dict[tuple[str, str, str], dict] = {}

    def __init__(self, *, lobby_id: str | None = None, invite_id: str | None = None, mint: TargetMaterialMint | None = None,
                 mint_factory: Callable[..., TargetMaterialMint] | None = None,
                 registry=None, scope_factory: Callable[..., MaterialScope] | None = None,
                 connector: Any = None, deadline: float | None = None,
                 request_bound: bool = True,
                 journal=None,
                 target_resolver: Callable[..., TargetMaterialMint] | None = None,
                 receipt_resolver: Callable[..., tuple[Any, MaterialScope]] | None = None,
                 scope_status_resolver: Callable[..., dict] | None = None):
        self.lobby_id, self.invite_id = lobby_id, invite_id
        self._mint, self._mint_factory = mint, mint_factory
        self._registry, self._scope_factory = registry, scope_factory
        self._connector, self._deadline = connector, deadline
        self._request_bound = request_bound
        self._journal = journal
        self._pending_request = None
        self._pending_generation = None
        self._target_resolver, self._receipt_resolver = target_resolver, receipt_resolver
        self._scope_status_resolver = scope_status_resolver

    async def request(self, *, deadline: float | None = None) -> dict:
        if self._connector is None:
            raise MaterialError("control_unavailable")
        end = _deadline(deadline if deadline is not None else self._deadline)
        binding = getattr(getattr(self._connector, "_connector", None), "binding", None)
        endpoint_id = getattr(binding, "endpoint_id", "lab")
        generation = getattr(binding, "generation", 0)
        if self._pending_request is None:
            if self._request_bound:
                candidate = {
                    "op": "material_request", "v": 2, "lobby_id": self.lobby_id, "invite_id": self.invite_id,
                    "request_id": new_material_request_id(),
                    "expires_at_utc_ms": utc_now_ms() + max(0, math.ceil((end - time.monotonic()) * 1000)),
                }
                if self._journal is not None:
                    # This is the durable origin gate: it must complete before
                    # the first send, and its returned record wins after a
                    # process restart or a lost response.
                    try:
                        origin = await asyncio.to_thread(
                            self._journal.record_material_origin,
                            endpoint_id=endpoint_id, generation=generation,
                            lobby_id=self.lobby_id, invite_id=self.invite_id,
                            request_id=candidate["request_id"], expires_at_utc_ms=candidate["expires_at_utc_ms"],
                            deadline=end)
                    except Exception:
                        raise MaterialError("control_unavailable") from None
                    if time.monotonic() >= end:
                        raise MaterialError("control_unavailable")
                    self._pending_request = dict(candidate, request_id=origin["request_id"],
                                                 expires_at_utc_ms=origin["expires_at_utc_ms"])
                    pending_generation = origin["generation"]
                else:
                    self._pending_request = candidate
                    pending_generation = generation
            else:
                self._pending_request = {"op": "material_request", "lobby_id": self.lobby_id, "invite_id": self.invite_id}
                pending_generation = generation
        pending_generation = self._pending_generation if self._pending_generation is not None else generation
        self._pending_generation = pending_generation
        request_wire = dict(self._pending_request)
        pending_key = (self.lobby_id, self.invite_id, endpoint_id)
        with self._pending_lock:
            self._pending_requests[pending_key] = dict(request_wire, endpoint_id=endpoint_id, generation=pending_generation)
        try:
            result = await _call_connector(
                self._connector,
                request_wire,
                deadline=end,
                admit=lambda: True,
                allow_legacy=not self._request_bound,
            )
        except Exception as exc:
            if isinstance(exc, MaterialError):
                raise
            raise MaterialError("control_unavailable") from None
        if time.monotonic() >= end or result != _READY:
            raise MaterialError("control_unavailable")
        return _READY.copy()

    async def target_handler(self, context, request: dict) -> dict:
        if not isinstance(request, dict):
            raise ControlError()
        is_product = isinstance(context, MaterialPeerContext) and getattr(context, "operation", None) == "material_request"
        if not is_product:
            if (set(request) != {"op", "lobby_id", "invite_id"}
                    or request.get("op") != "material_request"):
                raise ControlError()
        else:
            try:
                from .control.dispatcher import validate_material_v2_request
                validate_material_v2_request(request)
            except Exception:
                raise ControlError() from None
        if ((self.lobby_id is not None and request["lobby_id"] != self.lobby_id)
                or (self.invite_id is not None and request["invite_id"] != self.invite_id)
                or not context.readmit()):
            raise ControlError()
        mint = self._mint
        try:
            if self._target_resolver is not None:
                mint = self._target_resolver(context=context, request=dict(request), deadline=context.deadline)
                if inspect.isawaitable(mint):
                    mint = await asyncio.wait_for(mint, max(0.0, context.deadline - time.monotonic()))
            elif mint is None and self._mint_factory is not None:
                mint = self._mint_factory(context=context, request=dict(request), deadline=context.deadline)
        except TypeError:
            # Keep the narrow W2c factory seam source-compatible while allowing
            # the R01 resolver to receive the authenticated request.
            if self._target_resolver is not None:
                mint = self._target_resolver(context=context, request=dict(request))
            else:
                mint = self._mint_factory(context=context, deadline=context.deadline)
        if mint is None:
            raise ControlError()
        try:
            mint_request = {"lobby_id": request["lobby_id"], "invite_id": request["invite_id"]}
            try:
                result = mint.mint(mint_request, deadline=context.deadline,
                                   request_meta=request,
                                   final_admission=context.readmit, peer_context=context)
            except TypeError as exc:
                # Keep the narrow lab seam source-compatible; product mints
                # accept peer_context and cannot bypass import binding.
                if "peer_context" not in str(exc):
                    raise
                result = mint.mint(mint_request, deadline=context.deadline,
                                   request_meta=request,
                                   final_admission=context.readmit)
            if inspect.isawaitable(result):
                result = await asyncio.wait_for(result, max(0.0, context.deadline - time.monotonic()))
            if time.monotonic() >= context.deadline or not context.readmit() or result != _READY:
                raise MaterialError("control_unavailable")
            return _READY.copy()
        except Exception:
            raise ControlError() from None

    async def receipt_handler(self, context, request: dict) -> dict:
        v2 = isinstance(request, dict) and request.get("v") == 2
        is_product = isinstance(context, MaterialPeerContext) and getattr(context, "operation", None) == "material_receipt"
        if is_product and not v2:
            raise ControlError()
        if v2:
            try:
                validate_material_v2_receipt(request)
            except Exception:
                raise ControlError() from None
        elif not isinstance(request, dict) or set(request) != {"op", "lobby_id", "invite_id", "receipt"}:
            raise ControlError()
        if ((self.lobby_id is not None and request["lobby_id"] != self.lobby_id)
                or (self.invite_id is not None and request["invite_id"] != self.invite_id)):
            raise ControlError()
        if not context.readmit() or not isinstance(request["receipt"], dict):
            raise ControlError()
        effective_deadline = context.deadline
        if v2:
            receipt = request["receipt"]
            operation = request.get("operation")
            receipt = request["receipt"]
            if (receipt.get("v") != 2 or receipt.get("operation") != operation):
                raise ControlError()
            pending_key = (request["lobby_id"], request["invite_id"], context.endpoint_id)
            if self._journal is not None:
                pending = await asyncio.to_thread(self._journal.material_origin,
                                                  context.endpoint_id, request["lobby_id"], request["invite_id"],
                                                  deadline=effective_deadline)
            else:
                with self._pending_lock:
                    pending = self._pending_requests.get(pending_key)
            if pending is None or any(request.get(k) != pending.get(k)
                                      for k in ("request_id", "expires_at_utc_ms")):
                raise ControlError()
            if (pending.get("generation") != context.generation
                    or request["lobby_id"] != pending["lobby_id"]
                    or request["invite_id"] != pending["invite_id"]):
                raise ControlError()
            expected_operation = {
                "endpoint_id": pending["endpoint_id"], "generation": pending["generation"],
                "lobby_id": pending["lobby_id"], "invite_id": pending["invite_id"],
                "request_id": pending["request_id"], "expires_at_utc_ms": pending["expires_at_utc_ms"],
            }
            if operation != expected_operation:
                raise ControlError()
            try:
                signature = base64.b64decode(request["operation_signature"], validate=True)
                Ed25519PublicKey.from_public_bytes(context.consent_public_key).verify(
                    signature, canonical({"purpose": "joining-owner-material-v2", "operation": operation}))
            except Exception:
                raise ControlError() from None
            try:
                # The probe is an authenticated context capability, never a
                # request-selected clock claim.  Missing/stale/out-of-bound is
                # indistinguishable from an unavailable control path.
                validate_clock_probe(context.probe, endpoint_id=context.endpoint_id,
                                     generation=context.generation, request_id=request["request_id"])
                effective_deadline = min(
                    effective_deadline,
                    local_deadline_from_utc(request["expires_at_utc_ms"]),
                )
            except ClockProbeError:
                raise ControlError() from None
            if time.monotonic() >= effective_deadline:
                raise ControlError()
        try:
            if self._receipt_resolver is not None:
                resolved = self._receipt_resolver(context=context, request=dict(request), deadline=effective_deadline)
                if inspect.isawaitable(resolved):
                    resolved = await asyncio.wait_for(resolved, max(0.0, effective_deadline - time.monotonic()))
                registry, scope = resolved
            else:
                registry = self._registry
                scope = self._scope_factory(context=context, lobby_id=request["lobby_id"],
                                            invite_id=request["invite_id"]) if self._scope_factory else None
            if scope is None or registry is None:
                raise ValueError()
            # The H3 owner/store status is a separate durable authority from the
            # receipt registry.  Check it immediately before admission so a
            # pending/denied scope cannot create a receipt merely because its
            # envelope happens to be well signed.  The resolver is injected by
            # the owner-bound host wiring; it may be synchronous or async, but
            # it must return the exact ready scope represented by the envelope.
            if self._scope_status_resolver is not None:
                status = self._scope_status_resolver(context=context, request=dict(request),
                                                      scope=scope, deadline=effective_deadline)
                if inspect.isawaitable(status):
                    status = await asyncio.wait_for(status, max(0.0, effective_deadline - time.monotonic()))
                expected_scope = {
                    "host_id": scope.owner_installation_id, "room_id": scope.planned_room_id,
                    "member_id": scope.member_id, "lobby_id": scope.lobby_id,
                    "invite_id": scope.invite_id, "binding": scope.binding,
                }
                if (not isinstance(status, dict) or status.get("status") != "ready"
                        or status.get("scope") != request["receipt"].get("scope")
                        or status.get("scope") != expected_scope):
                    raise ValueError()
            token = ADMISSION_DEADLINE.set(effective_deadline)
            try:
                result = registry.admit_signed_material_async(scope, request["receipt"])
                if inspect.isawaitable(result):
                    await asyncio.wait_for(result, max(0.0, effective_deadline - time.monotonic()))
            finally:
                ADMISSION_DEADLINE.reset(token)
            if not context.readmit():
                raise ValueError()
            if v2 and self._journal is not None:
                consumed = await asyncio.to_thread(
                    self._journal.consume_material_origin,
                    endpoint_id=context.endpoint_id, generation=pending["generation"],
                    lobby_id=pending["lobby_id"], invite_id=pending["invite_id"],
                    request_id=pending["request_id"], expires_at_utc_ms=pending["expires_at_utc_ms"],
                    deadline=effective_deadline)
                if not consumed:
                    raise ValueError()
            return _READY.copy()
        except ControlError:
            raise
        except Exception:
            raise ControlError() from None

    def handlers(self):
        return {"material_request": self.target_handler, "material_receipt": self.receipt_handler}
