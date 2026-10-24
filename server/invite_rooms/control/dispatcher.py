"""Authenticated link dispatcher for control RPCs.

The dispatcher deliberately knows no database schema and never forwards native
Room parameters.  ``LinkAuthority`` is the seam that P3b.1a-2 will replace
with the journal-backed implementation.
"""
from __future__ import annotations

import asyncio
import inspect
import math
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol


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
            "material_request", "material_receipt"}
# No native method, URL, or arbitrary parameter can cross this boundary.
_FIELDS = {
    "pair_ack": {"op", "ack"},
    "preview": {"op", "lobby_id", "invite_id"},
    "redeem": {"op", "lobby_id", "invite_id", "secret"},
    "consent": {"op", "lobby_id", "invite_id", "receipt", "signature"},
    "status": {"op", "lobby_id", "invite_id"},
    "material_request": {"op", "lobby_id", "invite_id"},
    "material_receipt": {"op", "lobby_id", "invite_id", "receipt"},
}


class ControlDispatcher:
    def __init__(self, authority: LinkAuthority, *, handlers: Mapping[str, Callable],
                 admit: Callable[[str, int, str], bool] | None = None,
                 owner_verify: Callable[..., bool] | None = None,
                 direction: str = "A_to_B", max_workers: int = 8,
                 completion_workers: int = 2, queue_size: int = 32,
                 timeout: float = 10.0, op_timeouts: Mapping[str, float] | None = None,
                 registry: TransportRegistry | None = None) -> None:
        if max_workers < 1 or completion_workers < 1 or queue_size < 1:
            raise ValueError("invalid dispatcher capacity")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("invalid dispatcher timeout")
        self.authority = authority
        self.handlers = dict(handlers)
        self.admit = admit or (lambda endpoint, generation, op: True)
        self.owner_verify = owner_verify
        self.direction = direction
        self.timeout = timeout
        self.op_timeouts = dict(op_timeouts or {})
        self._slots = asyncio.BoundedSemaphore(queue_size)
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="invite-control")
        # Completion callbacks are never queued behind user handlers.
        self._completion_executor = ThreadPoolExecutor(max_workers=completion_workers,
                                                        thread_name_prefix="invite-control-complete")
        self.registry = registry or TransportRegistry()

    async def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
        self._completion_executor.shutdown(wait=False, cancel_futures=True)

    def _state(self, endpoint_id: str, generation: int) -> LinkState:
        if not isinstance(endpoint_id, str) or type(generation) is not int:
            raise ControlError()
        state = self.authority.current(endpoint_id)
        if state is None or state.generation != generation:
            raise ControlError()
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
        if op not in _ALLOWED or set(value) != _FIELDS[op]:
            raise ControlError()
        if op in {"preview", "redeem", "consent", "status", "material_request", "material_receipt"}:
            if not all(isinstance(value.get(k), str) and value[k] for k in ("lobby_id", "invite_id")):
                raise ControlError()
        return op, dict(value)

    def _allowed(self, state: LinkState, op: str) -> None:
        if state.protocol_state not in {"confirmed", "acknowledged"}:
            raise ControlError()
        if state.protocol_state == "confirmed" and op != "pair_ack":
            raise ControlError()
        if op == "material_request" and self.direction != "A_to_B":
            raise ControlError()
        if op == "material_receipt" and self.direction != "B_to_A":
            raise ControlError()

    def _owner_check(self, endpoint_id: str, request: dict) -> None:
        if request["op"] in {"pair_ack"}:
            return
        if self.owner_verify is None:
            raise ControlError()
        try:
            result = self.owner_verify(endpoint_id, request)
        except TypeError:
            result = self.owner_verify(request)
        if result is not True:
            raise ControlError()

    async def dispatch(self, endpoint_id: str, generation: int, request: Any) -> Any:
        state = self._state(endpoint_id, generation)
        op, request = self._request(request)
        self._allowed(state, op)
        self._owner_check(endpoint_id, request)
        timeout = self.op_timeouts.get(op, self.timeout)
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise ControlError()
        try:
            await asyncio.wait_for(self._slots.acquire(), timeout=timeout)
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
            handler = self.handlers.get("joining_consent" if op == "consent" else op)
            if handler is None:
                raise ControlError()
            effective_request = request
            if op == "consent":
                effective_request = dict(request)
                effective_request["op"] = "joining_consent"
            loop = asyncio.get_running_loop()
            if inspect.iscoroutinefunction(handler):
                result = handler(effective_request)
                return await asyncio.wait_for(result, timeout=timeout)
            # Submit without awaiting: the admission decision and this submission
            # are adjacent, while blocking handlers cannot starve the event loop.
            future = loop.run_in_executor(self._executor, handler, effective_request)
            return await asyncio.wait_for(future, timeout=timeout)
        except ControlError:
            raise
        except (TimeoutError, asyncio.TimeoutError):
            raise ControlError("control_unavailable") from None
        except Exception:
            raise ControlError() from None
        finally:
            self._slots.release()
