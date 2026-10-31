"""Authenticated link dispatcher for control RPCs.

The dispatcher deliberately knows no database schema and never forwards native
Room parameters.  ``LinkAuthority`` is the seam that P3b.1a-2 will replace
with the journal-backed implementation.
"""
from __future__ import annotations

import asyncio
import inspect
import math
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol


# Cross-host material expiry policy.  These are deliberately constants rather
# than configuration: relaxing them changes the safety proof for the wire
# operation.  UTC values cross the wire; monotonic values never do.
CLOCK_MAX_SKEW_MS = 50
CLOCK_MAX_UNCERTAINTY_MS = 50
EXPIRY_MARGIN_MS = 100
CLOCK_FRESHNESS_MS = 1000
EXPIRY_LOSS_MS = CLOCK_MAX_SKEW_MS + CLOCK_MAX_UNCERTAINTY_MS + EXPIRY_MARGIN_MS


class ClockProbeError(ValueError):
    """A probe cannot prove the pairwise UTC bound; callers must fail closed."""


def new_material_request_id() -> str:
    """Return the protocol's fresh 128-bit request identifier."""
    return secrets.token_hex(16)


def utc_now_ms(clock=time.time) -> int:
    """Read UTC as an integer millisecond value, rejecting unusable clocks."""
    value = clock()
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ClockProbeError("utc_unavailable")
    result = int(value * 1000)
    if result < 0:
        raise ClockProbeError("utc_unavailable")
    return result


def local_deadline_from_utc(expires_at_utc_ms: int, *, utc_ms: int | None = None,
                            monotonic: float | None = None, clock=time.time,
                            mono_clock=time.monotonic) -> float:
    """Convert an authenticated UTC expiry to a local monotonic cutoff.

    The conversion is intentionally local and one-shot.  The resulting
    monotonic instant must not be placed in a request, receipt, log, or probe.
    """
    if type(expires_at_utc_ms) is not int or expires_at_utc_ms < 0:
        raise ClockProbeError("invalid_expiry")
    now_utc = utc_now_ms(clock) if utc_ms is None else utc_ms
    now_mono = mono_clock() if monotonic is None else monotonic
    if type(now_utc) is not int or type(now_mono) not in (int, float) or not math.isfinite(now_mono):
        raise ClockProbeError("utc_unavailable")
    remaining = expires_at_utc_ms - now_utc
    if remaining <= EXPIRY_LOSS_MS:
        raise ClockProbeError("expiry_too_close")
    return float(now_mono) + (remaining - EXPIRY_LOSS_MS) / 1000.0


def validate_clock_probe(probe: Mapping[str, Any], *, endpoint_id: str, generation: int,
                         request_id: str, now_utc_ms: int | None = None) -> tuple[int, int]:
    """Validate the four-timestamp probe under arbitrary path asymmetry.

    The conservative offset interval is ``[t2-t1-rtt, t2-t1]``.  Its width is
    the measured RTT; midpoint +/- RTT/2 is deliberately not used.  The
    returned tuple is ``(lower_offset_ms, upper_offset_ms)``.
    """
    if not isinstance(probe, Mapping) or set(probe) != {
            "op", "v", "operation", "nonce", "endpoint_id", "generation", "request_id", "t1_utc_ms",
            "t2_utc_ms", "t3_utc_ms", "t4_utc_ms"}:
        raise ClockProbeError("invalid_probe")
    if (probe["op"] != "material_probe" or probe["operation"] != "material_request"
            or type(probe["v"]) is not int or probe["v"] != 1
            or not isinstance(probe["nonce"], str) or len(probe["nonce"]) != 32):
        raise ClockProbeError("invalid_probe")
    if probe["endpoint_id"] != endpoint_id or type(probe["generation"]) is not int or probe["generation"] != generation:
        raise ClockProbeError("probe_binding_mismatch")
    if probe["request_id"] != request_id:
        raise ClockProbeError("probe_binding_mismatch")
    values = [probe[key] for key in ("t1_utc_ms", "t2_utc_ms", "t3_utc_ms", "t4_utc_ms")]
    if any(type(value) is not int or value < 0 for value in values):
        raise ClockProbeError("invalid_probe")
    t1, t2, t3, t4 = values
    if t3 < t2 or t4 < t1:
        raise ClockProbeError("invalid_probe")
    rtt = t4 - t1
    processing = t3 - t2
    if rtt < processing or rtt > CLOCK_MAX_UNCERTAINTY_MS:
        raise ClockProbeError("probe_uncertain")
    now = utc_now_ms() if now_utc_ms is None else now_utc_ms
    if type(now) is not int or now - t4 > CLOCK_FRESHNESS_MS or t4 - now > CLOCK_FRESHNESS_MS:
        raise ClockProbeError("probe_stale")
    lower, upper = t2 - t1 - rtt, t2 - t1
    if upper - lower > CLOCK_MAX_UNCERTAINTY_MS or max(abs(lower), abs(upper)) > CLOCK_MAX_SKEW_MS:
        raise ClockProbeError("probe_out_of_bounds")
    return lower, upper


_MATERIAL_V2_REQUEST_FIELDS = frozenset({
    "op", "v", "lobby_id", "invite_id", "request_id", "expires_at_utc_ms", "probe",
})
_MATERIAL_V2_RECEIPT_FIELDS = frozenset({
    "op", "v", "lobby_id", "invite_id", "request_id", "expires_at_utc_ms", "probe",
    "operation", "operation_signature", "receipt",
})


def _valid_request_id(value: Any) -> bool:
    try:
        return isinstance(value, str) and len(value) == 32 and all(c in "0123456789abcdef" for c in value)
    except Exception:
        return False


def validate_material_v2_request(value: Mapping[str, Any]) -> dict:
    """Strictly validate the v2 request envelope; no v1 coercion or fallback."""
    if not isinstance(value, Mapping) or set(value) != _MATERIAL_V2_REQUEST_FIELDS:
        raise ControlError()
    if value.get("op") != "material_request" or type(value.get("v")) is not int or value.get("v") != 2:
        raise ControlError()
    if any(not isinstance(value.get(key), str) or not value[key] for key in ("lobby_id", "invite_id")):
        raise ControlError()
    if not _valid_request_id(value.get("request_id")):
        raise ControlError()
    if type(value.get("expires_at_utc_ms")) is not int or value["expires_at_utc_ms"] < 0:
        raise ControlError()
    if not isinstance(value.get("probe"), Mapping):
        raise ControlError()
    return dict(value)


def validate_material_v2_receipt(value: Mapping[str, Any]) -> dict:
    """Strictly validate the v2 receipt envelope before any registry/store work."""
    if not isinstance(value, Mapping) or set(value) != _MATERIAL_V2_RECEIPT_FIELDS:
        raise ControlError()
    if value.get("op") != "material_receipt" or type(value.get("v")) is not int or value.get("v") != 2:
        raise ControlError()
    if any(not isinstance(value.get(key), str) or not value[key] for key in ("lobby_id", "invite_id")):
        raise ControlError()
    if not _valid_request_id(value.get("request_id")):
        raise ControlError()
    if type(value.get("expires_at_utc_ms")) is not int or value["expires_at_utc_ms"] < 0:
        raise ControlError()
    if not isinstance(value.get("receipt"), Mapping) or not isinstance(value.get("probe"), Mapping):
        raise ControlError()
    operation = value.get("operation")
    if (not isinstance(operation, Mapping)
            or set(operation) != {"endpoint_id", "generation", "lobby_id", "invite_id", "request_id", "expires_at_utc_ms"}
            or not isinstance(value.get("operation_signature"), str)
            or len(value["operation_signature"]) > 4096):
        raise ControlError()
    return dict(value)


class ControlError(ValueError):
    def __init__(self, code: str = "control_unavailable"):
        self.code = code
        super().__init__(code)


class BusyError(ControlError):
    def __init__(self):
        super().__init__("busy")


@dataclass(frozen=True)
class LinkState:
    binding: Mapping[str, str]
    generation: int
    protocol_state: str


# Wire ops (handler labels) that may run in the contextual host lane: the invitation operations only.  Material,
# room-log and membership operations are deliberately absent (later work packages), as is ``pair_ack``.
CONTEXT_OPERATIONS = frozenset({"preview", "redeem", "joining_consent", "status"})


@dataclass(frozen=True)
class AuthenticatedPeerContext:
    """Server-internal, immutable identity of ONE exact acknowledged host link, read from the pairing journal.

    Created only by ``JournalLinkAuthority.authenticate_peer`` after a fresh nonblocking journal read that matched the
    TLS-resolved ``PeerTrust``.  Never serialised, never put on the wire, never taken from request fields.
    ``deadline`` is an absolute ``time.monotonic()`` instant; ``readmit()`` is the nonblocking re-admission a later
    store/effect boundary calls (it holds no journal lock after returning and never waits).
    """
    endpoint_id: str
    generation: int
    role: str
    protocol_state: str
    peer_id: str
    binding: tuple
    ca_pem: Any = field(repr=False)
    fingerprint: str
    consent_public_key: bytes = field(repr=False)
    operation: str
    deadline: float
    # v2 material metadata is issuer-authenticated and process-local after
    # admission.  Defaults preserve the v1 contextual constructor contract.
    request_id: str | None = None
    expires_at_utc_ms: int | None = None
    probe: Mapping[str, Any] | None = field(default=None, repr=False)
    # The issuer-bound readmit capability.  NOT an ``__init__`` argument and not a comparable/printable field: it is
    # attached only by ``_issue`` (the journal authority), so a publicly constructed or ``dataclasses.replace``d value
    # carries no authority and ``readmit()`` is ``False``.  It closes over the ORIGINAL TLS ``PeerTrust``, operation,
    # absolute deadline and consent key; it never consults the (copyable) fields of the context presenting it.
    _readmit: Any = field(init=False, repr=False, compare=False, default=None)

    @classmethod
    def _issue(cls, readmit: Callable[[], bool], **fields: Any) -> "AuthenticatedPeerContext":
        context = cls(**fields)
        object.__setattr__(context, "_readmit", readmit)
        return context

    def binding_dict(self) -> dict:
        return dict(self.binding)

    def readmit(self) -> bool:
        """``True`` only while the exact link this context was ISSUED for is still current and its deadline is open."""
        try:
            capability = self._readmit
            return capability is not None and capability() is True
        except Exception:
            return False

    def __reduce__(self):
        raise TypeError("AuthenticatedPeerContext is not serialisable")

    def __copy__(self):
        raise TypeError("AuthenticatedPeerContext is not copyable")

    def __deepcopy__(self, memo):
        raise TypeError("AuthenticatedPeerContext is not copyable")


class LinkAuthority(Protocol):
    def current(self, endpoint_id: str) -> LinkState | None: ...


class TransportRegistry:
    """Tracks live transports and closes them at a generation cutover."""
    def __init__(self) -> None:
        self._connections: dict[tuple[str, int], set[Callable[[], Any]]] = {}
        self._generation: dict[str, int] = {}

    def register(self, endpoint_id: str, generation: int, close: Callable[[], Any]) -> None:
        self._connections.setdefault((endpoint_id, generation), set()).add(close)
        self._generation[endpoint_id] = generation

    def unregister(self, endpoint_id: str, generation: int, close: Callable[[], Any]) -> None:
        self._connections.get((endpoint_id, generation), set()).discard(close)

    def observe_generation(self, endpoint_id: str, generation: int) -> None:
        old = self._generation.get(endpoint_id)
        self._generation[endpoint_id] = generation
        if old is None or old == generation:
            return
        stale = [key for key in self._connections if key[0] == endpoint_id and key[1] != generation]
        for key in stale:
            callbacks = self._connections.pop(key, set())
            for close in callbacks:
                try:
                    close()
                except Exception:
                    pass

    def close_link(self, endpoint_id: str, generation: int | None = None) -> None:
        keys = [key for key in self._connections if key[0] == endpoint_id and
                (generation is None or key[1] == generation)]
        for key in keys:
            for close in self._connections.pop(key, set()):
                try:
                    close()
                except Exception:
                    pass


_ALLOWED = {"pair_ack", "preview", "redeem", "consent", "status",
            "material_request", "material_receipt", "room_log", "member_left"}
# No native method, URL, or arbitrary parameter can cross this boundary.
_FIELDS = {
    "pair_ack": {"op", "ack"},
    "preview": {"op", "lobby_id", "invite_id"},
    "redeem": {"op", "lobby_id", "invite_id", "secret"},
    "consent": {"op", "lobby_id", "invite_id", "receipt", "signature"},
    "status": {"op", "lobby_id", "invite_id"},
    "material_request": {"op", "lobby_id", "invite_id"},
    "material_receipt": {"op", "lobby_id", "invite_id", "receipt"},
    "room_log": {"op", "room_id", "since_seq", "limit"},
    "member_left": {"op", "room_id"},
}


class ControlDispatcher:
    def __init__(self, authority: LinkAuthority, *, handlers: Mapping[str, Callable],
                 admit: Callable[[str, int, str], bool] | None = None,
                 owner_verify: Callable[..., bool] | None = None,
                 direction: str = "A_to_B", max_workers: int = 8,
                 completion_workers: int = 2, queue_size: int = 32,
                 timeout: float = 10.0, op_timeouts: Mapping[str, float] | None = None,
                 registry: TransportRegistry | None = None,
                 contextual_handlers: Mapping[str, Callable] | None = None,
                 material_handlers: Mapping[str, Callable] | None = None,
                 allow_legacy_material: bool = False) -> None:
        if max_workers < 1 or completion_workers < 1 or queue_size < 1:
            raise ValueError("invalid dispatcher capacity")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("invalid dispatcher timeout")
        self.authority = authority
        self.handlers = dict(handlers)
        self.contextual_handlers = dict(contextual_handlers or {})
        self.material_handlers = dict(material_handlers or {})
        self.allow_legacy_material = bool(allow_legacy_material)
        if self.contextual_handlers:
            # Additive lane ``handler(context, request)``: host direction only, invitation ops only, never shadowing a
            # legacy ``handler(request)`` and only with an authority that can mint journal-authenticated contexts.
            if (direction != "B_to_A" or not set(self.contextual_handlers) <= CONTEXT_OPERATIONS
                    or set(self.contextual_handlers) & set(self.handlers)
                    or not callable(getattr(authority, "authenticate_peer", None))
                    or not all(callable(h) for h in self.contextual_handlers.values())):
                raise ValueError("invalid contextual handlers")
        if self.material_handlers:
            if (set(self.material_handlers) not in ({"material_request"}, {"material_receipt"})
                    or not callable(getattr(authority, "authenticate_material_peer", None))
                    or not all(callable(h) for h in self.material_handlers.values())):
                raise ValueError("invalid material handlers")
        self.admit = admit or (lambda endpoint, generation, op: True)
        self.owner_verify = owner_verify
        self.direction = direction
        self.timeout = timeout
        self.op_timeouts = dict(op_timeouts or {})
        self._slots = asyncio.BoundedSemaphore(queue_size)
        self._completion_slots = asyncio.BoundedSemaphore(completion_workers)
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="invite-control")
        # Completion callbacks are never queued behind user handlers.
        self._completion_executor = ThreadPoolExecutor(max_workers=completion_workers,
                                                        thread_name_prefix="invite-control-complete")
        self.registry = registry or TransportRegistry()

    async def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
        self._completion_executor.shutdown(wait=False, cancel_futures=True)

    def threads(self) -> tuple:
        """Every executor worker thread this dispatcher ever started (alive or not)."""
        out = []
        for executor in (self._executor, self._completion_executor):
            out.extend(getattr(executor, "_threads", ()))
        return tuple(out)

    def drain(self, timeout: float) -> bool:
        """Cancel queued work and wait AT MOST ``timeout`` seconds for running handler/completion threads.

        Thread-safe, callable off the event loop and repeatable.  ``True`` only when no owned worker thread is
        alive; an already-admitted handler may finish (R1-05) but is never abandoned silently: ``False`` means the
        caller still owns live threads and must retry/escalate.
        """
        self._executor.shutdown(wait=False, cancel_futures=True)
        self._completion_executor.shutdown(wait=False, cancel_futures=True)
        end = time.monotonic() + max(0.0, timeout)
        for thread in self.threads():
            if thread is not threading.current_thread():
                thread.join(max(0.0, end - time.monotonic()))
        return not any(thread.is_alive() for thread in self.threads())

    def _state(self, endpoint_id: str, generation: int) -> LinkState:
        if not isinstance(endpoint_id, str) or type(generation) is not int:
            raise ControlError()
        state = self.authority.current(endpoint_id)
        if state is None or state.generation != generation:
            raise ControlError("forbidden")
        binding = state.binding
        bound_endpoint = (binding.get("endpoint_id") if isinstance(binding, Mapping)
                          else getattr(binding, "endpoint_id", None))
        if bound_endpoint != endpoint_id:
            raise ControlError()
        self.registry.observe_generation(endpoint_id, generation)
        return state

    @staticmethod
    def _request(value: Any) -> tuple[str, dict]:
        if not isinstance(value, dict) or type(value.get("op")) is not str:
            raise ControlError()
        op = value["op"]
        if op == "material_request" and value.get("v") == 2:
            return op, validate_material_v2_request(value)
        if op == "material_receipt" and value.get("v") == 2:
            return op, validate_material_v2_receipt(value)
        if op not in _ALLOWED or set(value) != _FIELDS[op]:
            raise ControlError()
        if op in {"preview", "redeem", "consent", "status", "material_request", "material_receipt"}:
            if not all(isinstance(value.get(k), str) and value[k] for k in ("lobby_id", "invite_id")):
                raise ControlError()
        if op == "room_log":
            if (not isinstance(value.get("room_id"), str) or not value["room_id"]
                    or type(value.get("since_seq")) is not int or value["since_seq"] < 0
                    or type(value.get("limit")) is not int or not 1 <= value["limit"] <= 200):
                raise ControlError()
        if op == "member_left" and (not isinstance(value.get("room_id"), str) or not value["room_id"]):
            raise ControlError()
        return op, dict(value)

    def _allowed(self, state: LinkState, op: str) -> None:
        if state.protocol_state not in {"confirmed", "acknowledged"}:
            raise ControlError()
        if state.protocol_state == "confirmed" and op != "pair_ack":
            raise ControlError()
        if op == "material_request" and self.direction != "A_to_B":
            raise ControlError("forbidden")
        if op == "material_receipt" and self.direction != "B_to_A":
            raise ControlError("forbidden")
        if op == "member_left" and self.direction != "B_to_A":
            raise ControlError("forbidden")

    def _owner_check(self, endpoint_id: str, request: dict, *, contextual: bool = False) -> None:
        if request["op"] in {"pair_ack"}:
            return
        if self.owner_verify is None:
            if contextual:
                return        # preflight only: the journal-authenticated context is the authorization, not this hook
            raise ControlError()
        try:
            result = self.owner_verify(endpoint_id, request)
        except TypeError:
            result = self.owner_verify(request)
        if result is not True:
            raise ControlError()

    async def dispatch(self, endpoint_id: str, generation: int, request: Any, *, expected_trust: Any = None,
                       deadline: float | None = None) -> Any:
        """``expected_trust`` and ``deadline`` are INTERNAL ONLY (the listener's TLS-resolved ``PeerTrust`` and its
        absolute ``time.monotonic()`` request deadline); neither is ever read from the request/envelope and both are
        only consulted by the contextual lane.  The contextual lane runs under ONE finite absolute deadline, no later
        than the listener deadline and than dispatch-entry + operation timeout; waits consume it, never reset it."""
        entry = time.monotonic()
        state = self._state(endpoint_id, generation)
        if (isinstance(request, dict) and request.get("op") in {"material_request", "material_receipt"}
                and request.get("v") != 2):
            # Product material handlers are always v2-only.  The only
            # compatibility lane is the old one-argument generic handler,
            # which is deliberately not installed by ControlListener.
            legacy_op = request.get("op")
            if not isinstance(legacy_op, str):
                raise ControlError()
            self._allowed(state, legacy_op)
            if ("v" in request
                    or (not self.allow_legacy_material and legacy_op in self.material_handlers)
                    or (not self.allow_legacy_material and legacy_op not in self.handlers)):
                raise ControlError()
            op, request = self._request(request)
        else:
            op, request = self._request(request)
        self._allowed(state, op)
        contextual_label = "joining_consent" if op == "consent" else op
        contextual = contextual_label in self.contextual_handlers
        material = op in self.material_handlers
        self._owner_check(endpoint_id, request, contextual=(contextual or material))
        timeout = self.op_timeouts.get(op, self.timeout)
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise ControlError()
        absolute = None
        if contextual or material:
            absolute = entry + timeout
            if deadline is not None:
                if type(deadline) not in (int, float) or not math.isfinite(deadline):
                    raise ControlError()
                absolute = min(absolute, float(deadline))
            if not time.monotonic() < absolute:
                raise ControlError("control_unavailable")
        slots = self._completion_slots if op == "material_receipt" else self._slots
        executor = self._completion_executor if op == "material_receipt" else self._executor
        try:
            await asyncio.wait_for(slots.acquire(), timeout=timeout if absolute is None else absolute - time.monotonic())
        except (TimeoutError, asyncio.TimeoutError):
            raise BusyError() from None
        try:
            # This is the R1-05 admission seam: no await between this synchronous
            # decision and invoking the handler effect below.
            try:
                if self.admit(endpoint_id, generation, "joining_consent" if op == "consent" else op) is not True:
                    raise ControlError()
            except ControlError:
                raise
            except Exception:
                raise ControlError() from None
            label = "joining_consent" if op == "consent" else op
            handler = None if material else self.handlers.get(label)
            context_handler = self.material_handlers.get(label) if material else self.contextual_handlers.get(label)
            if handler is None and context_handler is None:
                raise ControlError()
            effective_request = request
            if op == "consent":
                effective_request = dict(request)
                effective_request["op"] = "joining_consent"
            loop = asyncio.get_running_loop()
            if context_handler is not None:
                # No fallback to a legacy handler: either a fresh journal-authenticated context is created at the
                # point of execution (worker thread / the scheduled coroutine body itself) or nothing runs.
                if expected_trust is None:
                    raise ControlError()

                def authenticate() -> AuthenticatedPeerContext:
                    issuer = "authenticate_material_peer" if material else "authenticate_peer"
                    kwargs = {"deadline": absolute}
                    if material and request.get("v") == 2:
                        kwargs.update(request_id=request["request_id"],
                                      expires_at_utc_ms=request["expires_at_utc_ms"],
                                      probe=request["probe"])
                    context = getattr(self.authority, issuer)(expected_trust, label, **kwargs)
                    expected_type = __import__("invite_rooms.pairing_journal", fromlist=["MaterialPeerContext", "AuthenticatedPeerContext"])
                    expected_type = expected_type.MaterialPeerContext if material else AuthenticatedPeerContext
                    if (not isinstance(context, expected_type) or context.endpoint_id != endpoint_id
                            or context.generation != generation or context.operation != label):
                        raise ControlError()
                    return context
                remaining = absolute - time.monotonic()      # the slot wait already counted; every wait gets the rest
                if remaining <= 0:
                    raise ControlError("control_unavailable")
                if inspect.iscoroutinefunction(context_handler):
                    async def run_async() -> Any:
                        # Admission happens HERE, in the scheduled body: no await between authenticate() and the
                        # first step of the user coroutine (awaiting a fresh coroutine runs it immediately).
                        context = authenticate()
                        return await context_handler(context, effective_request)
                    result = await asyncio.wait_for(run_async(), timeout=remaining)
                else:
                    def run() -> Any:
                        return context_handler(authenticate(), effective_request)
                    future = loop.run_in_executor(executor, run)
                    # A timeout cancels a still-queued work item; a handler already running cannot be cancelled.
                    result = await asyncio.wait_for(future, timeout=remaining)
            elif inspect.iscoroutinefunction(handler):
                result = handler(effective_request)
                result = await asyncio.wait_for(result, timeout=timeout)
            else:
                # Submit without awaiting: the admission decision and this submission
                # are adjacent, while blocking handlers cannot starve the event loop.
                future = loop.run_in_executor(executor, handler, effective_request)
                result = await asyncio.wait_for(future, timeout=timeout)
            if op == "room_log":
                # A revocation or generation cutover during groups.log must
                # invalidate the page; never return a stale canonical transcript.
                try:
                    if self.admit(endpoint_id, generation, "room_log") is not True:
                        raise ControlError("forbidden")
                except ControlError:
                    raise
                except Exception:
                    raise ControlError("forbidden") from None
            return result
        except ControlError:
            raise
        except (TimeoutError, asyncio.TimeoutError):
            raise ControlError("control_unavailable") from None
        except Exception:
            raise ControlError() from None
        finally:
            slots.release()
