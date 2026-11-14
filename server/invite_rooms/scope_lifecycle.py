"""Bundle-owned, fixed-topology target resources; no HTTP/material orchestration.

Reservation locks protect metadata only. Preparing (including catalog reads),
checking, binding, starting, closing and joining always run outside the lock.
Closing admission never revokes grants, stops native runs or deletes receipts.
Reconciliation is metadata-only, not full material recovery/packet B proof.
"""
from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field

from invite_rooms.consent import canonical
from invite_rooms.material_validators import validate_scope_attributes
from invite_rooms.peer_registry import adapter_id_for


class ScopeLifecycleError(RuntimeError):
    """Sanitized lifecycle refusal; incomplete cleanup remains owned for retry."""


@dataclass(eq=False)
class _Attempt:
    key: tuple
    authority: bytes | None
    token: object = field(default_factory=object)
    done: threading.Event = field(default_factory=threading.Event)
    prepared: object = None
    error: BaseException | None = None
    active: bool = True
    retiring: bool = False
    releasing: bool = False


class ScopeLifecycle:
    """One attempt per locally selected scope coordinates; construction is inert.

    prepare is the bundle's target_connector_factory (no request-selected bind).
    check re-reads owner material/journal metadata after prepare/start. Only a
    literal True permits publication; a read failure never implies absence.
    """
    def __init__(self, *, prepare, check):
        if not callable(prepare) or not callable(check):
            raise TypeError('concrete prepare and check required')
        self._prepare, self._check = prepare, check
        self._lock = threading.Lock()
        self._changed = threading.Condition(self._lock)
        self._attempts = {}
        self._started = False
        self._closed = False

    def start(self):
        with self._lock:
            if self._closed or self._started:
                raise ScopeLifecycleError('scope_lifecycle_already_used')
            self._started = True

    @staticmethod
    def _deadline(deadline):
        if type(deadline) not in (int,float) or not math.isfinite(deadline) or time.monotonic() >= deadline:
            raise ScopeLifecycleError('scope_deadline_exceeded')

    def _guard(self, attempt, deadline):
        self._deadline(deadline)
        with self._lock:
            if self._closed or attempt.retiring or self._attempts.get(attempt.key) is not attempt:
                raise ScopeLifecycleError('scope_lifecycle_closed')

    def ensure_target(self, *, endpoint_id, generation, lobby_id, invite_id, deadline, expected_scope=None):
        self._deadline(deadline)
        if (any(type(v) is not str or not v or len(v)>128 for v in (endpoint_id,lobby_id,invite_id))
                or type(generation) is not int or generation < 1):
            raise ScopeLifecycleError('invalid_scope_coordinates')
        key = (endpoint_id,generation,lobby_id,invite_id)
        authority = canonical(dict(expected_scope)) if expected_scope is not None else None
        with self._lock:
            if not self._started or self._closed:
                raise ScopeLifecycleError('scope_lifecycle_closed')
            attempt = self._attempts.get(key)
            first = attempt is None
            if first:
                if len(self._attempts) >= 4096:
                    raise ScopeLifecycleError('scope_capacity')
                attempt = _Attempt(key, authority)
                self._attempts[key] = attempt
            elif attempt.authority != authority or attempt.retiring:
                raise ScopeLifecycleError('scope_conflict')
        if not first:
            if not attempt.done.wait(max(0,deadline-time.monotonic())):
                raise ScopeLifecycleError('scope_deadline_exceeded')
            self._guard(attempt,deadline)
            if attempt.error is not None:
                raise attempt.error
            prepared = attempt.prepared
            if self._check(prepared,deadline) is not True:
                raise ScopeLifecycleError('scope_unavailable')
            self._guard(attempt,deadline)
            return prepared
        try:
            self._guard(attempt,deadline)
            prepared = self._prepare(endpoint_id=endpoint_id,generation=generation,lobby_id=lobby_id,
                                     invite_id=invite_id,deadline=deadline)
            # Retain ownership BEFORE validation or any start/IO can fail.
            with self._lock:
                attempt.prepared = prepared
            scope = prepared.scope
            validate_scope_attributes(scope,generation=generation,profile=scope.target_profile)
            if ((scope.endpoint_id,scope.pairing_generation,scope.lobby_id,scope.invite_id) != key
                    or prepared.adapter_id != adapter_id_for(scope)
                    or (authority is not None and canonical(dict(prepared.expected_scope)) != authority)):
                raise ScopeLifecycleError('scope_conflict')
            host, port = prepared.bind
            if type(port) is not int or not 0 < port <= 65535 or host not in ('127.0.0.1','::1'):
                raise ScopeLifecycleError('target_listener_unconfigured')
            self._guard(attempt,deadline)
            if self._check(prepared,deadline) is not True:
                raise ScopeLifecycleError('scope_unavailable')
            self._guard(attempt,deadline)
            prepared.connector.start(host=host,port=port)
            if self._check(prepared,deadline) is not True:
                raise ScopeLifecycleError('scope_unavailable')
            self._guard(attempt,deadline)
            with self._lock:
                if self._closed or attempt.retiring:
                    raise ScopeLifecycleError('scope_lifecycle_closed')
                attempt.active = False
                attempt.done.set()
                self._changed.notify_all()
            return prepared
        except BaseException as exc:
            with self._lock:
                attempt.error = exc
                attempt.retiring = True
            try:
                self._release(attempt)
            except BaseException:
                pass  # Keep primary AND retained resource. close() retries cleanup.
            raise
        finally:
            with self._lock:
                attempt.active = False
                attempt.done.set()
                self._changed.notify_all()

    def _release(self, attempt):
        with self._lock:
            if attempt.releasing:
                raise ScopeLifecycleError('scope_cleanup_incomplete')
            attempt.releasing = True
            prepared = attempt.prepared
        try:
            if prepared is not None:
                prepared.connector.close()
        except BaseException:
            raise
        else:
            with self._lock:
                if self._attempts.get(attempt.key) is attempt:
                    self._attempts.pop(attempt.key)
        finally:
            with self._lock:
                attempt.releasing = False
                self._changed.notify_all()

    def retire(self, scope):
        key = (scope.endpoint_id,scope.pairing_generation,scope.lobby_id,scope.invite_id)
        with self._lock:
            attempt = self._attempts.get(key)
            if attempt is None:
                return False
            if attempt.prepared is not None and attempt.prepared.scope != scope:
                raise ScopeLifecycleError('scope_conflict')
            attempt.retiring = True
            if attempt.active:
                raise ScopeLifecycleError('scope_cleanup_incomplete')
        self._release(attempt)
        return True

    def snapshot(self):
        """Metadata only; no grants, private paths or raw connector references."""
        with self._lock:
            return tuple((a.key, 'retiring' if a.retiring else 'preparing' if a.active else 'running')
                         for a in self._attempts.values())

    def reconcile(self, *, deadline):
        """Bounded observation of already-owned targets only, never mint/restart.

        False means confirmed invalidation; exceptions are unreadable metadata,
        preserve ownership and surface failure rather than infer deletion.
        """
        self._deadline(deadline)
        with self._lock:
            batch = tuple(a for a in self._attempts.values() if not a.active and not a.retiring)
        failures = []
        for attempt in batch:
            try:
                self._deadline(deadline)
                if self._check(attempt.prepared,deadline) is not True:
                    self.retire(attempt.prepared.scope)
            except Exception as exc:
                failures.append(exc)
        if failures:
            raise failures[0]

    def close(self, *, timeout=5):
        if type(timeout) not in (int,float) or not math.isfinite(timeout) or timeout < 0:
            raise ValueError('invalid cleanup timeout')
        end = time.monotonic()+timeout
        with self._lock:
            self._closed = True
            for attempt in self._attempts.values():
                attempt.retiring = True
            while any(a.active for a in self._attempts.values()):
                remaining = end-time.monotonic()
                if remaining <= 0:
                    break
                self._changed.wait(remaining)
            batch = tuple(a for a in self._attempts.values() if not a.active)
        failures = []
        for attempt in batch:
            try:
                self._release(attempt)
            except BaseException as exc:
                failures.append(exc)
        with self._lock:
            incomplete = bool(self._attempts)
        if failures:
            raise failures[0]
        if incomplete:
            raise ScopeLifecycleError('scope_cleanup_incomplete')
