"""Inert, owner-bound assembly of the sidecar data plane (W2d Card2-A2b3-1b).

``build_sidecar_data_plane`` turns the canonical ``runtime_config.load_service`` result into ONE per-process bundle of
concrete, UNSTARTED components: the local HTTP ``LoopbackAdapter`` (+ ``ScopedAdapterRegistry``), the ``ControlListener``
with the real frozen host/material handler maps, the journal-authoritative data endpoint resolver, the accepted target
dispatch backend factory (real ``TargetMaterialSource`` + a dedicated ``AdmissionReceiptStore``) and the request-scoped
``MaterialControl`` resolvers.

Construction opens no socket, starts no thread, contacts no Core and registers no route/scope. The bundle owns the
configured target lifecycle and receipt-backed verifiers. HTTP/material orchestration, ``PeerRegistry.route_provider``
and registration after a validated durable receipt remain a separate packet. Every failure to
assemble (absent/incomplete/invalid owner config, unopenable store, aliasing topology, ...) is a sanitized ``None``:
there is never a partial bundle.  No module state: every cache/resource lives in the returned bundle or a closure.
"""
from __future__ import annotations

import asyncio
import contextlib
import math
import os
import re
import threading
import time
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping, Optional

from invite_rooms import identity as identity_module
from invite_rooms import runtime_config
from invite_rooms.admission_receipts import AdmissionReceiptStore
from invite_rooms.control.adapter import LoopbackAdapter
from invite_rooms.control.connector import LinkBinding, LinkIdentity, TargetConnector, make_link_connector
from invite_rooms.control.dispatcher import ControlError
from invite_rooms.control.listener import ControlListener, build_pair_ack_listener
from invite_rooms.control.scoped_registry import ScopedAdapterRegistry
from invite_rooms.host_control import build_host_handlers
from invite_rooms.history_host import OwnerHistoryHandlers, _Signer
from invite_rooms.joining_material import TargetMaterialMint
from invite_rooms.joining_store import JoiningStore
from invite_rooms.material_control import HostConnectorMaterialTransport, MaterialControl, MaterialControlChannel
from invite_rooms.material_receipts import MaterialReceiptStore
from invite_rooms.native_rpc import NativeRpcDenied
from invite_rooms.pairing_journal import Journal, JournalLinkAuthority, MaterialPeerContext, PeerTrust
from invite_rooms.peer_registry import MaterialScope, PeerRegistry, RegistrySource, adapter_id_for
from invite_rooms.runtime_config import RuntimeConfig
from invite_rooms.runtime_joining import _IdentityConsentKey, _config_connector_factory, _joining_settings
from invite_rooms.run_bindings import HostAckBindingStore, TargetReceiptVerifier
from invite_rooms.scope_lifecycle import ScopeLifecycle
from invite_rooms.service import InviteRoomsService, OwnerContext
from invite_rooms.target_dispatch_backend import make_target_dispatch_backend_factory
from invite_rooms.target_material import TargetMaterialSource, _claim_payload

__all__ = ("DataPlaneError", "PreparedTargetConnector", "SidecarDataPlane", "build_from_env",
           "build_sidecar_data_plane")

_ADAPTER_ID = re.compile(r"link-[0-9a-f]{32}\Z")
_ACKNOWLEDGED = ("acknowledged",)
_BINDING_KEYS = frozenset({"endpoint_id", "fingerprint", "target_install_id", "target_profile",
                           "catalog_digest", "policy_digest"})
_SCOPE_KEYS = frozenset({"host_id", "room_id", "member_id", "lobby_id", "invite_id", "binding"})
_NATIVE_CATALOG_TIMEOUT = 10.0
_JOIN_TIMEOUT = 5.0


class DataPlaneError(ValueError):
    """Machine code only: never interpolates paths, peer text, tokens or exception detail."""


def _refuse(code: str = "data_plane_unavailable"):
    raise DataPlaneError(code)


def _control_refused() -> ControlError:
    return ControlError("control_unavailable")


def _future(deadline, code: str = "deadline_invalid") -> float:
    if (type(deadline) not in (int, float) or not math.isfinite(deadline) or time.monotonic() >= deadline):
        _refuse(code)
    return float(deadline)


def _ca_text(ca_pem) -> str:
    return ca_pem.decode("ascii") if isinstance(ca_pem, bytes) else ca_pem


@dataclass(frozen=True)
class PreparedTargetConnector:
    """One UNSTARTED per-scope ``TargetConnector`` and the exact owner bind it must later be started on."""
    connector: Any
    bind: tuple
    adapter_id: str
    scope: MaterialScope
    expected_scope: Mapping


class _LazyNativeRpc:
    """Native Core RPC that only connects inside a request-bound call.

    It preserves ``bind_request(invite_admission=..., deadline=...)`` exactly as ``BoundedNativeRpc`` defines it (same
    validation, same ``_request_bound_native_rpc`` marker) and offers NO unbound call: a direct ``__call__`` is denied, so
    the lab/legacy path of ``TargetMaterialMint`` can never reach Core through this object.  Construction opens nothing.
    """
    __slots__ = ("_config",)

    def __init__(self, rpc_config: Mapping):
        self._config = dict(rpc_config)

    def bind_request(self, *, invite_admission, deadline):
        if (not callable(invite_admission) or type(deadline) not in (int, float) or isinstance(deadline, bool)
                or not math.isfinite(deadline)):
            raise NativeRpcDenied()
        config = self._config

        async def request(method, params):
            from invite_rooms.routes import _connect_native_rpc   # lazy: first real request only
            async with _connect_native_rpc(config) as rpc:
                return await rpc(method, params, invite_admission=invite_admission, deadline=deadline)

        request._request_bound_native_rpc = True
        return request

    async def __call__(self, *args, **kwargs):
        raise NativeRpcDenied()


class _Lifecycle:
    """Mutable per-bundle lifecycle state (the bundle itself is a frozen dataclass)."""
    __slots__ = ("lock", "started", "starting", "closing", "closed")

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.started = False
        self.starting = False
        self.closing = False
        self.closed = False


def _config_key(value):
    """Detached full config comparison; token PATH only, never token contents."""
    if is_dataclass(value):
        return (type(value).__name__, tuple((f.name, _config_key(getattr(value, f.name))) for f in fields(value)))
    if isinstance(value, Mapping):
        return tuple(sorted((k, _config_key(v)) for k, v in value.items()))
    if isinstance(value, (tuple, list)):
        return tuple(_config_key(v) for v in value)
    return value


class MaterialCoordinator:
    """One bundle's owned material work, never a module-global resolver.

    Each ingress executes on a retained worker with its own asyncio.run. That
    loop drains all blocking executor work on cancellation before the worker
    releases ownership. Metadata locks never surround IO or thread joins.
    """
    def __init__(self, config, owner):
        self._config = _config_key(config)
        self._owner = owner
        self._plane: Any = None
        self._lock = threading.Lock()
        self._threads = set()
        self._closing = False
        self._started = False

    def matches(self, config, owner, registry):
        return (owner == self._owner and _config_key(config) == self._config
                and self._plane is not None and registry is self._plane.scoped_registry)

    def start(self):
        with self._lock:
            if self._closing or self._started:
                _refuse()
            self._started = True

    def busy(self):
        with self._lock:
            return any(t.is_alive() for t in self._threads)

    def close(self, *, timeout=_JOIN_TIMEOUT):
        with self._lock:
            self._closing = True
            threads = tuple(self._threads)
        end = time.monotonic() + timeout
        for thread in threads:
            thread.join(max(0, end - time.monotonic()))
        with self._lock:
            self._threads.difference_update(t for t in threads if not t.is_alive())
            if self._threads:
                _refuse("material_cleanup_incomplete")

    async def _work(self, operation, *, deadline):
        _future(deadline)
        loop = asyncio.get_running_loop()
        result = loop.create_future()
        cancelled = threading.Event()

        def admitted():
            with self._lock:
                return not self._closing and not cancelled.is_set() and time.monotonic() < deadline

        def finish(value, error):
            if not result.done():
                if error is not None:
                    result.set_exception(error)
                else:
                    result.set_result(value)

        def run():
            value, error = None, None
            try:
                if not admitted():
                    _refuse()
                value = asyncio.run(operation(admitted))
                if not admitted():
                    _refuse()
            except BaseException as exc:
                error = exc
            try:
                loop.call_soon_threadsafe(finish, value, error)
            except RuntimeError:
                pass  # caller loop closed; ownership still retained until this thread exits

        thread = threading.Thread(target=run, name="material-owned")
        # Starting a thread while holding only the reservation lock makes the
        # ownership visible before close can snapshot it. No worker IO is locked.
        with self._lock:
            if self._closing or not self._started:
                _refuse()
            self._threads = {t for t in self._threads if t.is_alive()}
            self._threads.add(thread)
            try:
                thread.start()
            except BaseException:
                self._threads.remove(thread)
                raise
        try:
            return await asyncio.wait_for(result, max(0, deadline - time.monotonic()))
        finally:
            cancelled.set()

    def handler(self, callback):
        async def handle(context, request):
            if type(context) is not MaterialPeerContext:
                raise _control_refused()
            async def operation(admitted):
                bound = MaterialPeerContext._issue(
                    lambda: admitted() and context.readmit(),
                    **{f.name: getattr(context, f.name) for f in fields(context) if f.init})
                return await callback(bound, request)
            return await self._work(operation, deadline=context.deadline)
        return handle

    @contextlib.contextmanager
    def fresh_registry(self):
        registry = self.new_registry()
        try:
            yield registry
        finally:
            registry._receipt_store.close()

    def new_registry(self):
        plane = self._plane
        config = plane.config
        registry = runtime_config.build_peer_registry(config.peer_registry_path)
        receipts = MaterialReceiptStore(config.store_path.parent / "material-receipts", anchor=config.store_path.parent)
        registry._receipt_store = receipts
        registry._source = RegistrySource(config.peer_registry_path, runtime_config.build_peer_registry)
        registry._authority = plane.authority
        registry._route_provider = plane.scoped_registry.lookup
        registry._original_frontdoor = plane.original_frontdoor
        return registry

    def original_registry(self):
        registry = self.new_registry()
        registry._frontdoor_release = registry._receipt_store.close
        return registry

    def original_scope_ready(self, scope):
        self._host_trust(scope)
        status = self._plane.store.peer_scope_status(scope.lobby_id, scope.invite_id,
            peer_id=scope.peer_id, binding=scope.binding,
            before_return=lambda: self._host_trust(scope) is not None)
        if status.get('status') != 'ready':
            _refuse('original_scope_unavailable')

    def _host_trust(self, scope):
        trust = self._plane.authority.peer_trust_strict(scope.endpoint_id, scope.pairing_generation,
                                                       role="host", states=_ACKNOWLEDGED)
        if trust is None or trust.peer_id != scope.peer_id or trust.binding_dict() != scope.binding:
            _refuse()
        return trust

    def _identity(self):
        plane = self._plane
        bundle = identity_module.load_identity(plane.config.pairing.identity_dir,
            installation_id=plane.owner.installation_id, profile=plane.owner.profile)
        return LinkIdentity(cert_file=str(bundle.leaf_cert_file), key_file=str(bundle.leaf_key_file))

    async def ensure_material(self, scope, *, deadline, service):
        plane = self._plane
        if (type(scope) is not MaterialScope or service._trusted_owner != plane.owner
                or scope.owner_installation_id != plane.owner.installation_id or scope.owner_profile != plane.owner.profile
                or not service.registry.material_capability()):
            _refuse()
        async def operation(admitted):
            # Request-local readiness/source, not the plane's startup snapshot.
            _, ready, peers = service._readiness(plane.owner, scope.lobby_id)
            if not ready or not any(p['member_id'] == scope.member_id and p['invite_id'] == scope.invite_id for p in peers):
                _refuse()
            trust = self._host_trust(scope)
            destination = plane.config.pairing.control_endpoints.get(trust.peer_id) if plane.config.pairing.control_endpoints else None
            if destination is None or not admitted():
                _refuse()
            binding = LinkBinding(endpoint_id=trust.endpoint_id, generation=trust.generation, address=tuple(destination),
                peer_ca_file=None, peer_ca_data=_ca_text(trust.ca_pem), peer_fingerprint=trust.fingerprint,
                expected_binding=trust.binding_dict(), timeout=deadline-time.monotonic())
            connector = make_link_connector(binding, self._identity(), plane.authority)
            control = MaterialControl(lobby_id=scope.lobby_id, invite_id=scope.invite_id, journal=plane.journal,
                connector=HostConnectorMaterialTransport(connector, owned=True))
            # ALWAYS obtain target-success outer ready, including durable-receipt
            # retries. The journal returns the original operation or refuses expiry.
            await control.request(deadline=deadline, admit=admitted)
            self._host_trust(scope)
            if not admitted() or plane.scoped_registry.lookup(scope) is None:
                _refuse()
        await self._work(operation, deadline=deadline)

    async def post_receipt(self, *, scope, context, deadline):
        plane = self._plane
        _future(deadline)
        if not context.readmit():
            _refuse()
        with self.fresh_registry() as registry:
            material = await asyncio.to_thread(registry._receipt_store.load, scope, deadline=deadline)
            registry._validate_scope(scope)
            claims = _claim_payload(material['grant'])
            keys = ('room_id', 'home_install_id', 'authority_gateway_id', 'authority_epoch',
                    'member_id', 'target_install_id', 'target_profile')
            expected = {k: claims[k] for k in keys}
            trust = self._host_trust(scope)
            binding = LinkBinding(endpoint_id=trust.endpoint_id, generation=trust.generation,
                address=plane.data_endpoint_for_scope(scope), peer_ca_file=None, peer_ca_data=_ca_text(trust.ca_pem),
                peer_fingerprint=trust.fingerprint, expected_binding=trust.binding_dict())
            verifier = plane.host_ack_store.verifier(scope=scope, expected_scope=expected)
            _future(deadline)
            if not context.readmit():
                _refuse()
            await asyncio.to_thread(plane.scoped_registry.register, scope=scope, binding=binding, identity=self._identity(),
                expected_scope=expected, verify_native=verifier, on_admitted=verifier.record_ack)
            if time.monotonic() >= deadline or not context.readmit():
                await asyncio.to_thread(plane.scoped_registry.revoke, scope)
                _refuse()

    async def post_target(self, *, context, request, deadline):
        _future(deadline)
        if not context.readmit():
            _refuse()
        # Factory re-derives scope only from the confirmed local import/READY
        # envelope. Request IDs are lookup coordinates, never grant authority.
        prepared = await asyncio.to_thread(self._plane.scope_lifecycle.ensure_target,
            endpoint_id=context.endpoint_id, generation=context.generation, lobby_id=request['lobby_id'],
            invite_id=request['invite_id'], deadline=deadline)
        if time.monotonic() >= deadline or not context.readmit():
            await asyncio.to_thread(self._plane.scope_lifecycle.retire, prepared.scope)
            _refuse()


@dataclass(frozen=True)
class SidecarDataPlane:
    """Per-process, per-owner bundle of components, unstarted until ``start()``.

    ``start()`` binds the data adapter at exactly ``config.data_listen`` and then the control listener; ``close()``
    releases everything the bundle owns (registry, adapter, control listener, the stores the builder opened) and never
    touches the loader-owned ``store``/``registry``. The scope manager owns configured targets, but HTTP/material
    activation orchestration and host route registration remain a separate packet.
    """
    config: RuntimeConfig
    owner: OwnerContext
    owner_service: InviteRoomsService
    store: Any                      # caller-owned owner-bound RoomInviteStore (from ``load_service``)
    registry: PeerRegistry          # caller-owned receipt registry (from ``load_service``); route_provider untouched
    journal: Journal                # the journal the origin gate / material control / adapter authority share
    authority: JournalLinkAuthority
    joining_store: JoiningStore
    admission_store: AdmissionReceiptStore
    adapter: LoopbackAdapter
    adapter_bind: tuple
    scoped_registry: ScopedAdapterRegistry
    control_listener: ControlListener
    host_handlers: Mapping
    material_control: MaterialControl
    material_handlers: Mapping
    target_dispatch_backend_factory: Callable
    data_endpoint_resolver: Callable
    data_endpoint_for_scope: Callable
    target_connector_factory: Callable
    scope_lifecycle: ScopeLifecycle
    host_ack_store: HostAckBindingStore
    material_coordinator: Any = None
    original_frontdoor: Any = None
    _owned: tuple = ()
    _state: Any = field(default_factory=_Lifecycle, repr=False, compare=False)

    # -- lifecycle ---------------------------------------------------------------------------------------
    def start(self, *, attach: Optional[Callable[[ScopedAdapterRegistry], None]] = None) -> None:
        """Start the complete bundle once: data adapter, then ``attach(scoped_registry)``, then the control listener.

        The adapter binds the literal ``config.data_listen`` (never a substituted host, port 0 or request value).
        ``attach`` makes the already-built scope registry visible to the HTTP layer; it runs after the data bind and
        strictly before the control listener starts.  On ANY failure what actually started is unwound in reverse order,
        the bundle-owned stores are released and the ORIGINAL exception is re-raised unchanged (rollback failures never
        replace it).  A bundle is single-use.
        """
        state = self._state
        with state.lock:
            if state.started or state.closing or state.closed:
                _refuse("data_plane_already_used")
            state.started = True
            state.starting = True
        started: list = []
        try:
            started.append("scopes")
            self.scope_lifecycle.start()
            if self.material_coordinator is not None:
                self.material_coordinator.start()
            host, port = self.adapter_bind
            if type(host) is not str or type(port) is not int or not 0 < port <= 65535:
                _refuse("data_bind_invalid")
            # Each step is recorded BEFORE it is attempted: a partially started adapter/listener (bound socket, started
            # loop thread, failed self-clean) is only reachable for cleanup if the rollback knows it was attempted.
            started.append("adapter")
            self.adapter.start(host=host, port=port)
            self._check_starting()
            if attach is not None:
                attach(self.scoped_registry)
            self._check_starting()
            started.append("control")
            self.control_listener.start()
            self._check_starting()
        except BaseException:
            self._rollback(started)
            raise
        finally:
            with state.lock:
                state.starting = False

    def _check_starting(self) -> None:
        with self._state.lock:
            if self._state.closing or self._state.closed:
                _refuse("data_plane_closing")

    def _rollback(self, started: list) -> None:
        """Best-effort reverse unwind of a partial start; never raises (the caller re-raises the original error)."""
        steps: list = ([self.material_coordinator.close] if self.material_coordinator is not None else [])
        steps.append(self.scope_lifecycle.close)
        if self.original_frontdoor is not None:
            steps.insert(0, self.original_frontdoor.close)
        if "control" in started:
            steps.append(self._close_control)
        if "adapter" in started:
            steps.extend((self.scoped_registry.close, self._close_adapter))
        failed = False
        for step in steps:
            try:
                step()
            except Exception:
                failed = True
        # A late prepare/start still owns its source stores. Never close them underneath it.
        if not self.scope_lifecycle.snapshot() and not (self.material_coordinator and self.material_coordinator.busy()):
            for step in self._close_stores():
                try:
                    step()
                except Exception:
                    failed = True
        else:
            failed = True
        if not failed:
            with self._state.lock:
                self._state.closed = True

    def _close_adapter(self) -> None:
        thread = getattr(self.adapter, "_thread", None)
        self.adapter.close()
        if thread is not None and thread.is_alive():
            raise DataPlaneError("adapter_thread_alive")

    def _close_control(self) -> None:
        failure = None
        try:
            self.control_listener.close()
        except Exception as exc:
            failure = exc
        try:
            self.control_listener.join(timeout=_JOIN_TIMEOUT)
        except Exception as exc:
            failure = failure or exc
        if failure is not None:
            raise failure

    def _close_stores(self) -> tuple:
        return (self.host_ack_store.close, self.admission_store.close, self.joining_store.close)

    def close(self) -> None:
        """Release everything this bundle owns: scoped registry, data adapter, control listener (close + join), then the
        stores the builder opened.  Every step is attempted; the first failure is raised only AFTER all of them were
        tried (never swallowed, never reported as success).  Idempotent: a clean close is final, a failed close may be
        retried.  Loader-owned ``store``/``registry`` are never closed here."""
        state = self._state
        with state.lock:
            if state.closed:
                return
            state.closing = True
        failures: list = []
        steps = ([self.material_coordinator.close] if self.material_coordinator is not None else [])
        steps.extend((self.scope_lifecycle.close, self.scoped_registry.close, self._close_adapter, self._close_control))
        if self.original_frontdoor is not None:
            steps.insert(0, self.original_frontdoor.close)
        for step in steps:
            try:
                step()
            except Exception as exc:
                failures.append(exc)
        with state.lock:
            starting = state.starting
        if not starting and not self.scope_lifecycle.snapshot() and not (self.material_coordinator and self.material_coordinator.busy()):
            for step in self._close_stores():
                try:
                    step()
                except Exception as exc:
                    failures.append(exc)
        elif not failures:
            failures.append(DataPlaneError("data_plane_cleanup_incomplete"))
        if failures:
            raise failures[0]
        with state.lock:
            state.closed = True


def _warn(key: str, message: str) -> None:
    runtime_config._warn_once("data-plane-" + key, message)


def _unpack(loaded):
    if type(loaded) is not tuple or len(loaded) != 3:
        return None
    service, ctx, config = loaded
    if (not isinstance(service, InviteRoomsService) or not isinstance(ctx, OwnerContext)
            or not isinstance(config, RuntimeConfig)):
        return None
    if ctx.installation_id != config.installation_id or ctx.profile != config.profile:
        return None
    return service, ctx, config


def _complete(config: RuntimeConfig) -> bool:
    pairing = config.pairing
    return (pairing is not None and pairing.control_listen is not None and pairing.target_listeners is not None
            and pairing.data_endpoints is not None and config.data_listen is not None
            and config.joining_store_dir is not None and config.target_http is not None
            and config.native_rpc is not None and config.joining_owner is not None)


def _local_binds(config: RuntimeConfig) -> tuple:
    pairing = config.pairing
    return (tuple(pairing.control_listen), tuple(config.data_listen),
            *(tuple(bind) for bind in pairing.target_listeners.values()))


def _validate_topology(config: RuntimeConfig) -> None:
    """Re-prove the accepted A2b3-1a topology on the object actually handed to the builder (fail closed)."""
    pairing = config.pairing
    joining = config.joining_owner
    if config.original_frontdoor_url is not None:
        from invite_rooms.original_frontdoor import validate_frontdoor
        if validate_frontdoor(config.original_frontdoor_url) != tuple(config.data_listen):
            _refuse('frontdoor_bind_mismatch')
    joining_control = None
    if isinstance(joining, Mapping) and "control_endpoints" in joining:
        joining_control = runtime_config.parse_control_endpoints(joining["control_endpoints"])
    runtime_config._check_data_topology(pairing.control_listen, config.data_listen, pairing.target_listeners,
                                        pairing.control_endpoints, pairing.data_endpoints, joining_control)
    destinations = [tuple(dest) for scopes in pairing.data_endpoints.values() for dest in scopes.values()]
    binds = _local_binds(config)
    if (len(set(destinations)) != len(destinations) or len(set(binds)) != len(binds)
            or set(destinations) & set(binds)):
        raise runtime_config.RuntimeConfigError("invalid_data_topology")
    for adapter_ids in (pairing.target_listeners, *pairing.data_endpoints.values()):
        if not all(_ADAPTER_ID.fullmatch(key) for key in adapter_ids):
            raise runtime_config.RuntimeConfigError("invalid_data_topology")


def build_from_env(env: Optional[dict] = None) -> Optional[SidecarDataPlane]:
    """Canonical loader + builder: ``None`` for feature-off, misconfigured or incomplete owner config."""
    return build_sidecar_data_plane(runtime_config.load_service(env))


def build_sidecar_data_plane(loaded) -> Optional[SidecarDataPlane]:
    """Assemble the unstarted bundle from ``runtime_config.load_service``'s ``(service, ctx, config)``, else ``None``.

    The loader-owned service/store/registry are only borrowed (never rebound or closed).  Stores opened here are tracked
    by the bundle and released by ``close()``; if assembly fails after something was opened it is closed before ``None``
    is returned.
    """
    parts = _unpack(loaded)
    if parts is None:
        return None
    service, ctx, config = parts
    if not _complete(config):
        _warn("incomplete", "data plane configuration is incomplete")
        return None
    opened: list = []
    try:
        _validate_topology(config)
        _joining_settings(config)       # same joining block validation the pinned control factory relies on
        store_parent = Path(os.path.abspath(config.store_path.parent))
        if not os.path.isdir(store_parent):
            raise DataPlaneError("store_unavailable")
        return _assemble(service, ctx, config, store_parent, opened)
    except Exception:
        for close in reversed(opened):
            with contextlib.suppress(Exception):
                close()
        _warn("assembly", "data plane assembly failed")
        return None


def _assemble(service, ctx, config, store_parent, opened) -> SidecarDataPlane:
    pairing = config.pairing
    journal = Journal(pairing.journal_dir, anchor=pairing.journal_anchor)
    authority = JournalLinkAuthority(journal)
    joining_store = JoiningStore(config.joining_store_dir)
    opened.append(joining_store.close)
    # Dedicated sibling of the invite store: distinct from MaterialReceiptStore ("material-receipts") and from the
    # JoiningStore.  ``AdmissionReceiptStore`` anchors at the nearest existing parent, which is ``store_parent`` (it exists).
    admission_store = AdmissionReceiptStore(store_parent / "admission-receipts")
    opened.append(admission_store.close)
    host_ack_store = HostAckBindingStore(store_parent / "host-ack-bindings", anchor=store_parent)
    opened.append(host_ack_store.close)

    def identity_bundle():
        return identity_module.load_identity(pairing.identity_dir, installation_id=ctx.installation_id,
                                             profile=ctx.profile)

    def identity_reader(*, deadline):
        _future(deadline)
        return identity_bundle()

    def catalog_reader(*, deadline):
        """The owner's own native catalog (``groups.capabilities``); first used inside a real target request."""
        remaining = _future(deadline) - time.monotonic()

        async def fetch():
            from invite_rooms.routes import _connect_native_rpc   # lazy
            async with _connect_native_rpc(config.native_rpc) as rpc:
                result = await rpc("groups.capabilities", {"profile": config.profile})
            link = result.get("room_link") if isinstance(result, dict) else None
            if not isinstance(link, dict) or link.get("enabled") is not True or not isinstance(link.get("catalog"), dict):
                _refuse("catalog_unavailable")
            return link["catalog"]

        return asyncio.run(asyncio.wait_for(fetch(), timeout=min(remaining, _NATIVE_CATALOG_TIMEOUT)))

    dispatch_factory = make_target_dispatch_backend_factory(
        joining_store, authority, identity_reader, catalog_reader, config.target_http, admission_store)

    adapter = LoopbackAdapter(authority=authority.current)
    scoped_registry = ScopedAdapterRegistry(adapter, authority)
    coordinator = MaterialCoordinator(config, ctx)
    async def history_native_rpc(method, params, *, effect_admission=None, deadline=None):
        from invite_rooms.routes import _connect_native_rpc
        async with _connect_native_rpc(config.native_rpc) as rpc:
            return await rpc(method, params, effect_admission=effect_admission, deadline=deadline)

    # The local owner identity is the only issuer of the owner-history receipt.
    # The guest key is supplied only by the journal-authenticated context at
    # handler execution; it is never taken from config or request data.
    owner_bundle = identity_bundle()
    history = OwnerHistoryHandlers(
        store=service.store, owner_installation_id=ctx.installation_id,
        owner_key=_Signer(owner_bundle.consent_key()),
        consent_db=store_parent / "history-consent.sqlite",
        native_rpc=history_native_rpc)
    host_handlers = build_host_handlers(service.store, ctx)
    if getattr(config, "history_enabled", False):
        host_handlers.update({
            "history_read_scope": history.history_read_scope,
            "history_read_acceptance": history.history_read_acceptance,
            "room_log": history.room_log,
        })
    material_control = _material_control(service, ctx, config, journal, authority, joining_store, coordinator)
    material_handlers = {name: coordinator.handler(callback) for name, callback in material_control.handlers().items()}
    control_listener = build_pair_ack_listener(
        config, installation_id=ctx.installation_id, profile=ctx.profile,
        host_handlers=host_handlers, material_handlers=material_handlers)

    resolve_endpoint, endpoint_for_scope = _endpoint_resolvers(config, authority)
    connector_factory = _target_connector_factory(config, ctx, authority, joining_store, identity_reader,
                                                  catalog_reader, dispatch_factory, identity_bundle, admission_store)

    def check_target(prepared, deadline):
        _future(deadline)
        scope = prepared.scope
        # Confirmed cutover/leave retires transport only. Unreadable metadata raises, not inferred absence.
        trust = authority.peer_trust_strict(scope.endpoint_id, scope.pairing_generation,
                                            role="joining", states=_ACKNOWLEDGED)
        if trust is None:
            return False
        record = joining_store.find_import(scope.lobby_id, scope.invite_id, deadline=deadline)
        if record is not None and record.get("state") == "left":
            return False
        source = TargetMaterialSource(joining_store=joining_store, authority=authority,
            identity_reader=identity_reader, catalog_reader=catalog_reader, target_http=config.target_http,
            endpoint_id=scope.endpoint_id, generation=scope.pairing_generation)
        resolved = source.resolve(lobby_id=scope.lobby_id, invite_id=scope.invite_id, deadline=deadline)
        material = source.load(scope, credential_ref=resolved.credential_ref, deadline=deadline)
        claims = _claim_payload(material["grant"])
        if (resolved.scope != scope or any(type(claims.get(k)) is not type(v) or claims.get(k) != v
                                          for k, v in prepared.expected_scope.items())
                or tuple(pairing.target_listeners.get(prepared.adapter_id, ())) != prepared.bind):
            raise DataPlaneError("target_scope_changed")
        _future(deadline)
        # This bool gate also refuses busy/corrupt/unavailable journals. Unlike
        # the strict invalidation reads above, False is not proof of retirement.
        if authority.admit(scope.endpoint_id, scope.pairing_generation, "data_native") is not True:
            _refuse()
        return True

    scope_lifecycle = ScopeLifecycle(prepare=connector_factory, check=check_target)
    original_frontdoor = None
    if config.original_frontdoor_url is not None:
        from invite_rooms.original_frontdoor import OriginalFrontdoor
        original_frontdoor = OriginalFrontdoor(adapter, scoped_registry, config.original_frontdoor_url,
            coordinator.original_registry, coordinator.original_scope_ready)
        opened.append(original_frontdoor.close)
    owned = (scope_lifecycle.close, scoped_registry.close, control_listener.close, adapter.close, *reversed(opened))
    plane = SidecarDataPlane(
        config=config, owner=ctx, owner_service=service, store=service.store, registry=service.registry,
        journal=journal, authority=authority, joining_store=joining_store, admission_store=admission_store,
        adapter=adapter, adapter_bind=tuple(config.data_listen), scoped_registry=scoped_registry,
        control_listener=control_listener, host_handlers=MappingProxyType(dict(host_handlers)),
        material_control=material_control, material_handlers=MappingProxyType(dict(material_handlers)),
        target_dispatch_backend_factory=dispatch_factory, data_endpoint_resolver=resolve_endpoint,
        data_endpoint_for_scope=endpoint_for_scope, target_connector_factory=connector_factory,
        scope_lifecycle=scope_lifecycle, host_ack_store=host_ack_store, material_coordinator=coordinator,
        original_frontdoor=original_frontdoor, _owned=owned)
    coordinator._plane = plane
    return plane


# ---------------------------------------------------------------------------------------------------------------
# Journal-authoritative data endpoint resolution
# ---------------------------------------------------------------------------------------------------------------
def _endpoint_resolvers(config: RuntimeConfig, authority: JournalLinkAuthority):
    pairing = config.pairing
    data_endpoints = pairing.data_endpoints
    binds = frozenset(_local_binds(config))
    joining_control = (runtime_config.parse_control_endpoints(config.joining_owner["control_endpoints"])
                       if "control_endpoints" in config.joining_owner else {})
    pairing_control = pairing.control_endpoints or {}

    def resolve(trust, adapter_id) -> tuple:
        """``(host, port)`` for ``(fresh journal trust, canonical adapter id)``; anything else fails closed."""
        if type(trust) is not PeerTrust or type(adapter_id) is not str or not _ADAPTER_ID.fullmatch(adapter_id):
            _refuse("data_endpoint_unavailable")
        fresh = authority.peer_trust(trust.endpoint_id, trust.generation, role=trust.role, states=_ACKNOWLEDGED)
        if fresh is None or fresh != trust or fresh.role != "host":
            _refuse("data_endpoint_unavailable")
        scopes = data_endpoints.get(fresh.peer_id)
        destination = scopes.get(adapter_id) if scopes is not None else None
        if destination is None:
            _refuse("data_endpoint_unavailable")
        destination = tuple(destination)
        aliases = {tuple(control[fresh.peer_id]) for control in (pairing_control, joining_control)
                   if fresh.peer_id in control}
        if destination in binds or destination in aliases:
            _refuse("data_endpoint_ambiguous")
        return destination

    def for_scope(scope) -> tuple:
        if type(scope) is not MaterialScope:
            _refuse("data_endpoint_unavailable")
        trust = authority.peer_trust(scope.endpoint_id, scope.pairing_generation, role="host", states=_ACKNOWLEDGED)
        if (trust is None or trust.peer_id != scope.peer_id or trust.binding_dict() != scope.binding):
            _refuse("data_endpoint_unavailable")
        return resolve(trust, adapter_id_for(scope))

    return resolve, for_scope


# ---------------------------------------------------------------------------------------------------------------
# Request-scoped material control resolvers
# ---------------------------------------------------------------------------------------------------------------
def _material_control(service, ctx, config, journal, authority, joining_store, coordinator) -> MaterialControl:
    store = service.store
    connector_factory = _config_connector_factory(config)

    def checked_context(context, role: str, operation: str):
        """The journal-issued capability, re-proved against a FRESH journal read; never request-selected."""
        if (type(context) is not MaterialPeerContext or context.role != role or context.operation != operation
                or context.protocol_state != "acknowledged"):
            raise _control_refused()
        fresh = authority.peer_trust(context.endpoint_id, context.generation, role=role, states=_ACKNOWLEDGED)
        if (fresh is None or fresh.peer_id != context.peer_id or fresh.binding != context.binding
                or fresh.fingerprint != context.fingerprint or fresh.ca_pem != context.ca_pem
                or not context.readmit()):
            raise _control_refused()
        return context

    def request_ids(request):
        if (not isinstance(request, dict) or type(request.get("lobby_id")) is not str or not request["lobby_id"]
                or type(request.get("invite_id")) is not str or not request["invite_id"]):
            raise _control_refused()
        return request["lobby_id"], request["invite_id"]

    def live(deadline) -> float:
        try:
            return _future(deadline)
        except DataPlaneError:
            raise _control_refused() from None

    def read_status(context, lobby_id, invite_id):
        status = store.peer_scope_status(lobby_id, invite_id, peer_id=context.peer_id,
                                         binding=context.binding_dict(), before_return=context.readmit)
        scope = status.get("scope") if isinstance(status, dict) else None
        if (not isinstance(status, dict) or set(status) - {"status", "scope", "grant_ttl_hint_seconds"}
                or not {"status", "scope"} <= set(status) or ("grant_ttl_hint_seconds" in status and
                (type(status["grant_ttl_hint_seconds"]) is not int or not 60 <= status["grant_ttl_hint_seconds"] <= 86400))
                or not isinstance(scope, dict) or set(scope) != _SCOPE_KEYS or scope["host_id"] != ctx.installation_id
                or scope["lobby_id"] != lobby_id or scope["invite_id"] != invite_id
                or scope["binding"] != context.binding_dict() or scope["binding"].get("endpoint_id") != context.endpoint_id
                or set(scope["binding"]) != _BINDING_KEYS):
            raise _control_refused()
        return status

    def host_scope(context, lobby_id, invite_id) -> MaterialScope:
        scope = read_status(context, lobby_id, invite_id)["scope"]
        return MaterialScope(owner_installation_id=ctx.installation_id, owner_profile=ctx.profile,
                             lobby_id=lobby_id, planned_room_id=scope["room_id"], member_id=scope["member_id"],
                             invite_id=invite_id, peer_id=context.peer_id, pairing_generation=context.generation,
                             **scope["binding"])

    async def receipt_resolver(*, context, request, deadline):
        try:
            live(deadline)
            context = checked_context(context, "host", "material_receipt")
            lobby_id, invite_id = request_ids(request)
            scope = await asyncio.to_thread(host_scope, context, lobby_id, invite_id)
            live(deadline)
            # Source and consent pins are loaded NOW, never from startup's
            # plane.registry. Retain this receipt handle through admission.
            registry = coordinator.new_registry()
            return registry, scope
        except ControlError:
            raise
        except Exception:
            raise _control_refused() from None

    async def scope_status_resolver(*, context, request, scope, deadline):
        try:
            live(deadline)
            context = checked_context(context, "host", "material_receipt")
            lobby_id, invite_id = request_ids(request)
            status = await asyncio.to_thread(read_status, context, lobby_id, invite_id)
            live(deadline)
            return status
        except ControlError:
            raise
        except Exception:
            raise _control_refused() from None

    async def target_resolver(*, context, request, deadline):
        try:
            end = live(deadline)
            context = checked_context(context, "joining", "material_request")
            lobby_id, invite_id = request_ids(request)
            link = {"endpoint_id": context.endpoint_id, "generation": context.generation}
            connector = await asyncio.to_thread(connector_factory, link, end - time.monotonic())
            bundle = await asyncio.to_thread(
                identity_module.load_identity, config.pairing.identity_dir,
                installation_id=ctx.installation_id, profile=ctx.profile)
            channel = MaterialControlChannel(HostConnectorMaterialTransport(connector, owned=True), lobby_id=lobby_id,
                                             invite_id=invite_id, deadline=end, admit=context.readmit)
            return TargetMaterialMint(joining_store, consent_key=_IdentityConsentKey(bundle), channel=channel,
                                      rpc=_LazyNativeRpc(config.native_rpc),
                                      host_peer_id=f"{context.endpoint_id}:{context.generation}",
                                      grant_ttl_seconds=config.joining_grant_ttl_seconds,
                                      grant_ttl_max_seconds=config.joining_grant_ttl_max_seconds,
                                      grant_status_ttl_seconds=config.joining_grant_status_ttl_seconds)
        except ControlError:
            raise
        except Exception:
            raise _control_refused() from None

    return MaterialControl(journal=journal, target_resolver=target_resolver, receipt_resolver=receipt_resolver,
                           scope_status_resolver=scope_status_resolver,
                           post_durable_receipt=coordinator.post_receipt, post_success_target=coordinator.post_target,
                           receipt_registry_release=lambda registry: registry._receipt_store.close())


# ---------------------------------------------------------------------------------------------------------------
# Per-scope TargetConnector construction (unstarted; coordinates only from the original signed grant)
# ---------------------------------------------------------------------------------------------------------------
def _target_connector_factory(config, ctx, authority, joining_store, identity_reader, catalog_reader,
                              dispatch_factory, identity_bundle, admission_store):
    pairing = config.pairing
    receipt_backed = object()

    def build(*, endpoint_id, generation, lobby_id, invite_id, deadline,
              verify_native=receipt_backed) -> PreparedTargetConnector:
        """Return an UNSTARTED connector for ONE already-READY local scope; the caller must ``start()`` it on ``bind``.

        ``endpoint_id/generation/lobby_id/invite_id`` are local lifecycle facts, re-proved here by the existing
        ``TargetMaterialSource`` (journal trust + confirmed+consented import + READY encrypted material); they are never
        taken from a remote request.  ``authority_gateway_id``/``authority_epoch`` are read verbatim from the original,
        locally persisted signed grant's claims (no derivation, default or request value). Omitting ``verify_native``
        constructs the concrete receipt-backed verifier; explicit non-callables are refused, never a permissive default.
        """
        if verify_native is not receipt_backed and not callable(verify_native):
            _refuse("verify_native_required")
        end = _future(deadline)
        try:
            source = TargetMaterialSource(
                joining_store=joining_store, authority=authority, identity_reader=identity_reader,
                catalog_reader=catalog_reader, target_http=config.target_http,
                endpoint_id=endpoint_id, generation=generation)
            resolved = source.resolve(lobby_id=lobby_id, invite_id=invite_id, deadline=end)
            material = source.load(resolved.scope, credential_ref=resolved.credential_ref, deadline=end)
            scope = resolved.scope
            claims = _claim_payload(material["grant"])
            gateway, epoch = claims.get("authority_gateway_id"), claims.get("authority_epoch")
            if (type(gateway) is not str or not gateway or type(epoch) is not int or epoch < 1
                    or claims.get("room_id") != scope.planned_room_id
                    or claims.get("home_install_id") != scope.owner_installation_id
                    or claims.get("member_id") != scope.member_id
                    or claims.get("target_install_id") != scope.target_install_id
                    or claims.get("target_profile") != scope.target_profile):
                _refuse("grant_scope_mismatch")
            expected_scope = {"room_id": scope.planned_room_id, "home_install_id": scope.owner_installation_id,
                              "authority_gateway_id": gateway, "authority_epoch": epoch,
                              "member_id": scope.member_id, "target_install_id": scope.target_install_id,
                              "target_profile": scope.target_profile}
            verifier = (TargetReceiptVerifier(store=admission_store, source=source, resolved=resolved,
                                               expected_scope=expected_scope)
                        if verify_native is receipt_backed else verify_native)
            adapter_id = adapter_id_for(scope)
            bind = pairing.target_listeners.get(adapter_id)
            if bind is None:
                _refuse("target_listener_unconfigured")
            trust = authority.peer_trust_strict(endpoint_id, generation, role="joining", states=_ACKNOWLEDGED)
            if trust is None:
                _refuse("link_unavailable")
            binding = LinkBinding(endpoint_id=endpoint_id, generation=generation, address=tuple(bind),
                                  peer_ca_file=None, peer_ca_data=_ca_text(trust.ca_pem),
                                  peer_fingerprint=trust.fingerprint, expected_binding=trust.binding_dict())
            bundle = identity_bundle()
            identity = LinkIdentity(cert_file=str(bundle.leaf_cert_file), key_file=str(bundle.leaf_key_file))
            connector = TargetConnector(binding=binding, identity=identity, authority=authority,
                                        expected_scope=expected_scope, verify_native=verifier,
                                        native_backend_factory=dispatch_factory)
            return PreparedTargetConnector(connector=connector, bind=tuple(bind), adapter_id=adapter_id,
                                           scope=scope, expected_scope=MappingProxyType(dict(expected_scope)))
        except DataPlaneError:
            raise
        except Exception:
            raise DataPlaneError("target_connector_unavailable") from None

    return build
