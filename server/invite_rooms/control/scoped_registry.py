"""Scope-aware registry for exact native routes on one caller-owned adapter."""
from __future__ import annotations

import inspect
import threading
from dataclasses import dataclass, replace
from typing import Any, Mapping
from urllib.parse import urlsplit

from .adapter import LoopbackAdapter
from .connector import LinkBinding
from invite_rooms.material_validators import validate_scope_attributes
from invite_rooms.peer_registry import MaterialScope, adapter_id_for


class ScopedRegistryError(ValueError):
    """A scoped route cannot be admitted or is not currently available."""


@dataclass(frozen=True)
class ScopedRoute:
    adapter_id: str
    endpoint_id: str
    pairing_generation: int
    host: str
    port: int
    url: str

    @property
    def is_ipv4_loopback(self) -> bool:
        return self.host == "127.0.0.1"


@dataclass(frozen=True)
class _Registration:
    scope: MaterialScope
    binding: LinkBinding
    identity: Any
    adapter_id: str
    expected_scope: dict
    backend_timeout_seconds: float
    verify_native: Any = None
    on_admitted: Any = None
    token: object | None = None


@dataclass
class _PendingReservation:
    """Identity of one in-flight reservation, including its duplicate wakeup."""

    event: threading.Event
    token: object

    def wait(self, timeout=None):
        return self.event.wait(timeout)

    def set(self):
        self.event.set()


class ScopedAdapterRegistry:
    """Map exact native scopes to routes owned by one concrete LoopbackAdapter.

    The adapter and journal authority remain caller-owned.  Each registration gets
    a private token before native insertion; the token is required for both the
    descriptor read and conditional removal, so a route-id replacement is never
    mistaken for this registry's route.
    """

    def __init__(self, adapter, authority) -> None:
        if not isinstance(adapter, LoopbackAdapter):
            raise TypeError("concrete loopback adapter required")
        if not callable(getattr(authority, "current", None)):
            raise TypeError("journal link authority required")
        self._adapter = adapter
        self._authority = authority
        self._lock = threading.RLock()
        self._changed = threading.Condition(self._lock)
        self._entries: dict[str, _Registration] = {}
        self._retiring: dict[str, list[_Registration]] = {}
        self._pending: dict[str, _PendingReservation] = {}
        self._active = 0
        self._closed = False
        self._closing = False

    @property
    def adapter(self):
        return self._adapter

    def _validate(self, scope: MaterialScope, binding, expected_scope: Mapping[str, Any],
                  identity, verify_native, on_admitted, backend_timeout_seconds) -> tuple[dict, str]:
        try:
            generation = scope.pairing_generation
            profile = scope.target_profile
            canonical = validate_scope_attributes(scope, generation=generation, profile=profile)
        except (AttributeError, ValueError) as exc:
            raise ScopedRegistryError("invalid_material_scope") from exc
        if not isinstance(scope, MaterialScope):
            raise ScopedRegistryError("invalid_material_scope")
        if not isinstance(expected_scope, dict):
            raise ScopedRegistryError("invalid_expected_scope")
        expected = {
            "room_id": canonical["planned_room_id"],
            "home_install_id": canonical["owner_installation_id"],
            "member_id": canonical["member_id"],
            "target_install_id": canonical["target_install_id"],
            "target_profile": canonical["target_profile"],
        }
        required = {"room_id", "home_install_id", "authority_gateway_id", "authority_epoch",
                    "member_id", "target_install_id", "target_profile"}
        if set(expected_scope) != required or any(
                type(expected_scope[k]) is not type(v) or expected_scope[k] != v
                for k, v in expected.items()):
            raise ScopedRegistryError("material_scope_mismatch")
        if (type(expected_scope["authority_gateway_id"]) is not str
                or not expected_scope["authority_gateway_id"]
                or type(expected_scope["authority_epoch"]) is not int
                or expected_scope["authority_epoch"] < 1):
            raise ScopedRegistryError("invalid_expected_scope")
        if not isinstance(binding, LinkBinding):
            raise ScopedRegistryError("invalid_link_binding")
        if (binding.endpoint_id != canonical["endpoint_id"]
                or binding.generation != canonical["pairing_generation"]
                or binding.peer_fingerprint != canonical["fingerprint"]
                or dict(binding.expected_binding) != scope.binding):
            raise ScopedRegistryError("material_scope_mismatch")
        if not callable(verify_native):
            raise ScopedRegistryError("invalid_native_verifier")
        if on_admitted is not None and not callable(on_admitted):
            raise ScopedRegistryError("invalid_admission_callback")
        if type(backend_timeout_seconds) not in (int, float) or isinstance(backend_timeout_seconds, bool):
            raise ScopedRegistryError("invalid_backend_timeout")
        if not 0 < backend_timeout_seconds <= 30:
            raise ScopedRegistryError("invalid_backend_timeout")
        return dict(expected_scope), adapter_id_for(scope)

    def _authority_ready(self, registration: _Registration) -> bool:
        try:
            state = self._authority.current(registration.scope.endpoint_id)
            return (state is not None
                    and type(getattr(state, "generation", None)) is int
                    and state.generation == registration.scope.pairing_generation
                    and getattr(state, "protocol_state", None) == "acknowledged"
                    and dict(getattr(state, "binding", {})) == registration.scope.binding)
        except Exception:
            return False

    def _route(self, registration: _Registration) -> ScopedRoute | None:
        if not self._adapter.is_running() or registration.token is None:
            return None
        if not self._authority_ready(registration):
            return None
        descriptor = self._adapter.owned_route_descriptor(
            registration.adapter_id, endpoint_id=registration.scope.endpoint_id,
            generation=registration.scope.pairing_generation, ownership_token=registration.token)
        if descriptor is None:
            return None
        host, port, url = descriptor
        if type(host) is not str or type(port) is not int or not (1 <= port <= 65535):
            return None
        parsed = urlsplit(url)
        if parsed.path != "/links/" + registration.adapter_id or parsed.query or parsed.fragment:
            return None
        return ScopedRoute(registration.adapter_id, registration.scope.endpoint_id,
                           registration.scope.pairing_generation, host, port, url)

    @staticmethod
    def _same_registration(a: _Registration, b: _Registration) -> bool:
        # Callback identity is intentionally excluded: first registration wins.
        # The callables are nevertheless shape-validated on every invocation.
        return (a.scope == b.scope and a.binding == b.binding and a.identity == b.identity
                and a.expected_scope == b.expected_scope
                and a.backend_timeout_seconds == b.backend_timeout_seconds)

    def _remove(self, registration: _Registration) -> None:
        self._adapter.deregister_route(
            registration.adapter_id, endpoint_id=registration.scope.endpoint_id,
            generation=registration.scope.pairing_generation, ownership_token=registration.token)

    def _native_route_present(self, registration: _Registration) -> bool:
        """Fail closed when deciding whether stale bookkeeping may be repaired.

        This is only an observation for the pre-existing externally-deregistered
        route repair path.  It never authorizes cleanup; all cleanup still uses
        the registration's exact ownership token through ``_remove``.
        """
        try:
            return self._adapter.route_descriptor(registration.adapter_id) is not None
        except Exception:
            return True

    def _retain_retiring(self, registration: _Registration) -> None:
        with self._lock:
            self._retiring.setdefault(registration.adapter_id, []).append(registration)
            self._changed.notify_all()

    def _has_retiring(self, route_id: str) -> bool:
        return bool(self._retiring.get(route_id))

    def _discard_retiring(self, registration: _Registration) -> None:
        retiring = self._retiring.get(registration.adapter_id)
        if retiring is None:
            return
        try:
            retiring.remove(registration)
        except ValueError:
            return
        if not retiring:
            self._retiring.pop(registration.adapter_id, None)

    def register(self, *, scope: MaterialScope, binding, identity, expected_scope: Mapping[str, Any],
                 verify_native, backend_timeout_seconds: float = 30.0, on_admitted=None) -> ScopedRoute:
        expected, route_id = self._validate(scope, binding, expected_scope, identity,
                                            verify_native, on_admitted, backend_timeout_seconds)
        token = object()
        candidate = _Registration(scope, binding, identity, route_id, expected,
                                  backend_timeout_seconds, verify_native, on_admitted, token)
        while True:
            waiter = None
            repair_snapshot = None
            with self._lock:
                if self._closed:
                    raise ScopedRegistryError("registry_closed")
                existing = self._entries.get(route_id)
                if existing is not None:
                    if not self._same_registration(existing, candidate):
                        raise ScopedRegistryError("scoped_route_conflict")
                    waiter = self._pending.get(route_id)
                    snapshot = None if waiter is not None else existing
                else:
                    if self._has_retiring(route_id):
                        raise ScopedRegistryError("route_cleanup_pending")
                    waiter = self._pending.get(route_id)
                    if waiter is not None:
                        snapshot = None
                    else:
                        self._pending[route_id] = _PendingReservation(threading.Event(), token)
                        self._active += 1
                        break
            if snapshot is not None:
                route = self._route(snapshot)
                native_present = (self._native_route_present(snapshot)
                                  if route is None else None)
                with self._lock:
                    current = self._entries.get(route_id)
                    pending = self._pending.get(route_id)
                    if pending is not None:
                        # The external absence observation is stale as soon as
                        # any repair reservation exists. Do not replace that
                        # reservation while the committed logical snapshot is
                        # still current.
                        waiter = pending
                        repair_snapshot = None
                        continue
                    if current is snapshot and route is not None:
                        return route
                    if current is snapshot and route is None:
                        # A failed readiness/descriptor read is not evidence
                        # that this registry stopped owning the native route.
                        # Retain the OLD token whenever the route is still
                        # present, so close/revoke can perform exact cleanup.
                        # Only the established external-deregistration repair
                        # path may discard stale bookkeeping and retry.
                        if native_present:
                            raise ScopedRegistryError("route_unavailable")
                        # Native ownership may be repaired after an external
                        # deregistration, but the logical registration config
                        # remains first-success immutable.  Keep the fresh
                        # token and validated scope while reusing the original
                        # callbacks for the new native insertion.
                        candidate = replace(
                            candidate,
                            verify_native=snapshot.verify_native,
                            on_admitted=snapshot.on_admitted,
                        )
                        repair_snapshot = snapshot
                        self._pending[route_id] = _PendingReservation(threading.Event(), token)
                        self._active += 1
                        break
                continue
            assert waiter is not None
            waiter.wait()

        original_error = None
        cleanup_error = None
        try:
            if not self._adapter.is_running():
                raise ScopedRegistryError("adapter_not_running")
            if not self._authority_ready(candidate):
                raise ScopedRegistryError("journal_link_unavailable")
            self._adapter.register_link(
                binding.endpoint_id, binding.generation, expected_scope=expected,
                verify_native=candidate.verify_native, binding=binding, identity=identity,
                authority=self._authority, material_scope=scope,
                backend_timeout_seconds=candidate.backend_timeout_seconds,
                on_admitted=candidate.on_admitted,
                ownership_token=token)
            route = self._route(candidate)
            if route is None:
                raise ScopedRegistryError("route_unavailable")
            with self._lock:
                if self._closed or (
                        repair_snapshot is not None
                        and self._entries.get(route_id) is not repair_snapshot):
                    raise ScopedRegistryError("registry_closed")
                self._entries[route_id] = candidate
                # Wake duplicate callers once the exact owned entry exists;
                # they still revalidate the native descriptor themselves.
                pending = self._pending.get(route_id)
                if pending is not None and pending.token is token:
                    pending.set()
            return route
        except Exception as exc:
            original_error = exc
            try:
                self._remove(candidate)
            except Exception as cleanup_exc:
                cleanup_error = cleanup_exc
                self._retain_retiring(candidate)
            with self._lock:
                if self._entries.get(route_id) is candidate:
                    if repair_snapshot is not None and not self._closed:
                        self._entries[route_id] = repair_snapshot
                    else:
                        self._entries.pop(route_id, None)
            if cleanup_error is not None:
                raise ExceptionGroup("registration and rollback failed", [original_error, cleanup_error]) from original_error
            raise
        finally:
            with self._lock:
                pending = self._pending.get(route_id)
                if pending is not None and pending.token is token:
                    self._pending.pop(route_id, None)
                    pending.set()
                self._active -= 1
                self._changed.notify_all()

    def lookup(self, scope: MaterialScope) -> ScopedRoute | None:
        try:
            generation = scope.pairing_generation
            profile = scope.target_profile
            validate_scope_attributes(scope, generation=generation, profile=profile)
        except (ValueError, AttributeError) as exc:
            raise ScopedRegistryError("invalid_material_scope") from exc
        if not isinstance(scope, MaterialScope):
            raise ScopedRegistryError("invalid_material_scope")
        route_id = adapter_id_for(scope)
        with self._lock:
            registration = self._entries.get(route_id)
            if registration is None or registration.scope != scope:
                return None
        route = self._route(registration)
        with self._lock:
            if self._entries.get(route_id) is not registration:
                return None
        return route

    def port_for(self, scope: MaterialScope) -> int | None:
        route = self.lookup(scope)
        return route.port if route is not None and route.is_ipv4_loopback else None

    def revoke(self, scope: MaterialScope) -> bool:
        try:
            canonical = validate_scope_attributes(scope, generation=scope.pairing_generation, profile=scope.target_profile)
        except (ValueError, AttributeError) as exc:
            raise ScopedRegistryError("invalid_material_scope") from exc
        route_id = adapter_id_for(scope)
        with self._lock:
            registration = self._entries.get(route_id)
            if registration is None or registration.scope != scope:
                return False
            self._entries.pop(route_id, None)
            self._retiring.setdefault(route_id, []).append(registration)
            self._active += 1
        try:
            self._remove(registration)
        except Exception as exc:
            with self._lock:
                self._active -= 1
                self._changed.notify_all()
            raise exc
        else:
            with self._lock:
                self._discard_retiring(registration)
                self._active -= 1
                self._changed.notify_all()
            return True
        finally:
            with self._lock:
                self._changed.notify_all()

    def close(self) -> None:
        with self._lock:
            while self._closing:
                self._changed.wait()
            if self._closed and not self._entries and not self._retiring and self._active == 0:
                return
            self._closed = True
            self._closing = True
            for route_id, registration in tuple(self._entries.items()):
                self._entries.pop(route_id, None)
                self._retiring.setdefault(route_id, []).append(registration)
            for pending in self._pending.values():
                pending.set()
            while self._active:
                self._changed.wait()

        failures = []
        try:
            while True:
                with self._lock:
                    batch = tuple(registration
                                  for registrations in self._retiring.values()
                                  for registration in registrations)
                if not batch:
                    break
                for registration in batch:
                    try:
                        self._remove(registration)
                    except Exception as exc:
                        failures.append(exc)
                    else:
                        with self._lock:
                            self._discard_retiring(registration)
                with self._lock:
                    if self._active:
                        while self._active:
                            self._changed.wait()
                    if not self._retiring:
                        break
                    if failures:
                        break
        finally:
            with self._lock:
                self._closing = False
                self._changed.notify_all()
        if failures:
            raise ExceptionGroup("route cleanup failed", failures)

    register_scope = register
    lookup_scope = lookup
    revoke_scope = revoke
