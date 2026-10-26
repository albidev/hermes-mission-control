"""Owner-private trusted peer registry and scoped material resolution."""
from __future__ import annotations

import copy
import asyncio
import atexit
import contextvars
import fcntl
import hashlib
import json
import os
import re
import stat
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from room_invite_store import InviteError
from room_lobby_service import PeerMaterial, _validate_adapter
from invite_rooms.consent import canonical
from invite_rooms.material_receipts import ADMISSION_DEADLINE, valid_grant
from invite_rooms.peer_binding import PeerBindingError, validate_peer_binding


class PeerRegistryError(ValueError):
    """Sanitized machine code; never includes paths, input or secrets."""


_TOKEN = r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}"
_PROFILE = r"[a-z0-9][a-z0-9_-]{0,63}"
_INSTALL = r"install:[0-9a-f]{32}"
_FINGERPRINT = r"[0-9a-f]{64}"
_DIGEST = r"[0-9a-f]{64}"

# ADMISSION_DEADLINE (defined next to the receipt store, re-exported here) is propagated by run_admission
# to bound even legacy no-argument admission callbacks.

# Dedicated, bounded, named executor for admission/receipt IO so the loop's default executor is never
# occupied by (possibly lock-waiting) registry and receipt work.
ADMISSION_WORKERS = 4
ADMISSION_QUEUE_CAP = 8          # queued submissions beyond the workers; a full queue refuses (R3B-04)
ADMISSION_CALLBACK_RESERVED = 1  # workers a `material` read may never occupy, so callbacks never starve
LANE_MATERIAL = "material"       # scoped material reads: bounded, abandonable at the deadline
LANE_CALLBACK = "callback"       # receipt writes: reserved capacity, never abandoned mid-flight
MATERIAL_RESOLVE_TIMEOUT = 2.0   # outer cap for one scoped material read (R3B-04)


class AdmissionExecutor:
    """The process's admission thread pool: bounded submissions, a reserved lane, a deadline, an owner.

    Owner: this module.  Exactly ONE instance exists (``admission_executor``), created under
    ``_executor_lock`` and torn down deterministically by ``shutdown_admission_executor`` (also
    registered with :mod:`atexit`), so no admission worker outlives the process or a test that asked
    for the shutdown.  After a shutdown the next submission creates a fresh owner.

    Capacity (R3B-04): at most ``workers + queue_cap`` physical work items are retained and at most
    ``workers - reserved`` of them may be ``material`` reads, so a receipt-write callback always
    finds a worker instead of queueing behind material reads.  Capacity is released by the executor
    future's completion callback, not by the caller's awaiter: cancellation cannot abandon retained
    work or make the physical queue exceed the advertised bound.  Every refusal is a stable code
    (``admission_overloaded`` / ``admission_deadline_exceeded`` / ``admission_unavailable``).

    Deadline (R3B-04): the request deadline (``ADMISSION_DEADLINE`` or ``end_deadline``) is enforced
    BEFORE submission and again BEFORE the caller consumes the result.  A ``material`` read that is
    still queued at the deadline is abandoned (a read has no effect); a ``callback`` write is never
    abandoned mid-flight, but a write that finishes after its deadline is reported as
    ``admission_deadline_exceeded`` and must not be treated as a confirmed admit.
    """

    def __init__(self, *, workers: int = ADMISSION_WORKERS, queue_cap: int = ADMISSION_QUEUE_CAP,
                 reserved: int = ADMISSION_CALLBACK_RESERVED):
        self.workers = workers
        self.queue_cap = queue_cap
        self.reserved = max(0, min(reserved, workers - 1))  # at least one worker stays available to reads
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="invite-admission")
        self._slots = threading.BoundedSemaphore(workers + queue_cap)
        self._material = threading.BoundedSemaphore(workers - self.reserved)
        self._state = threading.Lock()
        self._closed = False

    # -- capacity ------------------------------------------------------------------------------
    def _reserve(self, lane: str) -> None:
        with self._state:
            if self._closed:
                raise PeerRegistryError("admission_unavailable")
        if lane == LANE_MATERIAL and not self._material.acquire(blocking=False):
            raise PeerRegistryError("admission_overloaded")  # reserved callback capacity is untouched
        if not self._slots.acquire(blocking=False):
            if lane == LANE_MATERIAL:
                self._material.release()
            raise PeerRegistryError("admission_overloaded")

    def _release(self, lane: str) -> None:
        self._slots.release()
        if lane == LANE_MATERIAL:
            self._material.release()

    # -- submission ----------------------------------------------------------------------------
    async def run(self, fn, *args, lane: str = LANE_MATERIAL, end_deadline: float | None = None, **kwargs):
        """Submit ``fn`` and return its value, or refuse it with a stable code."""
        if lane not in (LANE_MATERIAL, LANE_CALLBACK):
            raise PeerRegistryError("admission_lane_invalid")
        inherited = ADMISSION_DEADLINE.get()
        limit = inherited if end_deadline is None else (end_deadline if inherited is None else min(inherited, end_deadline))
        if limit is not None and time.monotonic() >= limit:
            raise PeerRegistryError("admission_deadline_exceeded")
        self._reserve(lane)
        context = contextvars.copy_context()
        try:
            # The reservation belongs to this physical Future.  Do not release it from the
            # coroutine: wait_for()/caller cancellation only stops waiting, never a running
            # ThreadPoolExecutor work item.
            with self._state:
                if self._closed:
                    raise RuntimeError("executor closed")
                future = self._pool.submit(context.run, fn, *args, **kwargs)
        except RuntimeError:
            self._release(lane)
            raise PeerRegistryError("admission_unavailable") from None  # owner shut down mid-reserve
        future.add_done_callback(lambda _future: self._release(lane))
        waiter = asyncio.wrap_future(future)

        def observe_waiter(done):
            # A timed-out/cancelled caller may never await the wrapper again.  Retrieve the
            # terminal exception so loop teardown does not turn an already-reported admission
            # deadline/cancellation into an unhandled-future warning; a live awaiter still receives
            # the same exception normally.
            if not done.cancelled():
                done.exception()

        waiter.add_done_callback(observe_waiter)
        if limit is None or lane == LANE_CALLBACK:
            result = await asyncio.shield(waiter)  # physical work survives caller cancellation
        else:
            try:
                # Shield is essential: wait_for otherwise cancels the asyncio wrapper and
                # propagates cancellation to the concurrent Future, while the worker remains
                # retained in ThreadPoolExecutor's queue.
                result = await asyncio.wait_for(
                    asyncio.shield(waiter), max(0.0, limit - time.monotonic()))
            except asyncio.TimeoutError:
                raise PeerRegistryError("admission_deadline_exceeded") from None
        if limit is not None and time.monotonic() >= limit:
            raise PeerRegistryError("admission_deadline_exceeded")  # BEFORE the result is consumed
        return result

    # -- owner lifecycle -----------------------------------------------------------------------
    def shutdown(self, *, wait: bool = True) -> None:
        """Refuse every new submission and join the workers (idempotent)."""
        with self._state:
            if self._closed:
                return
            self._closed = True
        self._pool.shutdown(wait=wait)


_executor: AdmissionExecutor | None = None
_executor_lock = threading.Lock()


def admission_executor() -> AdmissionExecutor:
    """The ONE process-wide admission executor (created on first use, re-created after a shutdown)."""
    global _executor
    with _executor_lock:
        if _executor is None:
            _executor = AdmissionExecutor()
        return _executor


def shutdown_admission_executor(*, wait: bool = True) -> None:
    """Deterministic owner shutdown: no further work, joined workers, and a fresh owner on demand."""
    global _executor
    with _executor_lock:
        executor, _executor = _executor, None
    if executor is not None:
        executor.shutdown(wait=wait)


atexit.register(shutdown_admission_executor)


async def run_admission(fn, *args, lane: str = LANE_MATERIAL, end_deadline: float | None = None, **kwargs):
    """Run ``fn`` on the admission executor with the caller's contextvars (incl. the request deadline)."""
    return await admission_executor().run(fn, *args, lane=lane, end_deadline=end_deadline, **kwargs)


def _check(value: str, pattern: str, label: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise PeerRegistryError(f"invalid_{label}")


def _native_catalog(value: dict):
    try:
        from gateway.hosted_room_peer import GatewayRoomCatalog
    except ImportError:
        raise PeerRegistryError("native_contract_unavailable") from None
    try:
        catalog = GatewayRoomCatalog.from_mapping(copy.deepcopy(value))
    except Exception:
        raise PeerRegistryError("invalid_catalog") from None
    policy = catalog.execution_policy
    if (not catalog.text or catalog.attachments
            or tuple(sorted(policy.enabled_toolsets)) != ("bot_room",)
            or policy.approval_mode != "manual"
            or not 1 <= policy.max_iterations <= 1000):
        raise PeerRegistryError("invalid_catalog")
    return catalog


def _validate_ca(value: str) -> None:
    if not isinstance(value, str) or len(value) > 131072 or value.count("BEGIN CERTIFICATE") != 1 or value.count("END CERTIFICATE") != 1:
        raise PeerRegistryError("invalid_tls_ca")
    try:
        from cryptography import x509
        from cryptography.hazmat.backends import default_backend
        x509.load_pem_x509_certificate(value.encode(), default_backend())
    except Exception:
        raise PeerRegistryError("invalid_tls_ca") from None


def adapter_id_for(scope: "MaterialScope") -> str:
    return "link-" + hashlib.sha256(b"mc-material-adapter-v1\0" + canonical(scope.as_dict()) + str(scope.pairing_generation).encode()).hexdigest()[:32]


@dataclass(frozen=True)
class MaterialScope:
    owner_installation_id: str
    owner_profile: str
    lobby_id: str
    planned_room_id: str
    member_id: str
    invite_id: str
    peer_id: str
    endpoint_id: str
    fingerprint: str
    target_install_id: str
    target_profile: str
    catalog_digest: str
    policy_digest: str
    pairing_generation: int = 1

    def __post_init__(self):
        for name in ("owner_installation_id", "target_install_id"):
            _check(getattr(self, name), _INSTALL, "material_scope")
        for name in ("owner_profile", "target_profile"):
            _check(getattr(self, name), _PROFILE, "material_scope")
        for name in ("lobby_id", "planned_room_id", "member_id", "invite_id", "peer_id", "endpoint_id"):
            _check(getattr(self, name), _TOKEN, "material_scope")
        for name in ("fingerprint", "catalog_digest", "policy_digest"):
            _check(getattr(self, name), _DIGEST, "material_scope")
        if isinstance(self.pairing_generation, bool) or not isinstance(self.pairing_generation, int) or self.pairing_generation < 1:
            raise PeerRegistryError("invalid_pairing_generation")
        try:
            validate_peer_binding(self.binding)
        except PeerBindingError:
            raise PeerRegistryError("invalid_material_scope") from None

    @property
    def binding(self):
        return {k: getattr(self, k) for k in ("endpoint_id", "fingerprint", "target_install_id", "target_profile", "catalog_digest", "policy_digest")}

    def as_dict(self):
        return {k: getattr(self, k) for k in ("owner_installation_id", "owner_profile", "lobby_id", "planned_room_id", "member_id", "invite_id", "peer_id", "endpoint_id", "fingerprint", "target_install_id", "target_profile", "catalog_digest", "policy_digest", "pairing_generation")}


@dataclass(frozen=True)
class ScopedPeerMaterial:
    scope: MaterialScope
    material: PeerMaterial


@dataclass(frozen=True)
class PeerRecord:
    peer_id: str
    installation_id: str
    profile: str
    fingerprint: str
    endpoint_id: str
    catalog_digest: str
    consent_public_key: bytes
    pairing_generation: int


class RegistrySource:
    """The authoritative on-disk ``peers.json`` plus the ONE process/file lock that serializes
    registry writes (generation cutover) with admission reads (R1-05).

    The lock is an ``flock`` on ``<registry dir>/.peers.json.lock`` (0600, no-follow, owner-checked):
    writers hold it exclusively while replacing the file, the admission check holds it shared while
    re-reading it.  Both are held only for the file operation, never across network I/O.
    """
    LOCK_NAME = ".peers.json.lock"
    LOCK_WAIT = 2.0  # sync callers/worker threads only; never poll this lock on the asyncio loop

    def __init__(self, path, loader: Callable[[Path], "PeerRegistry"]):
        self.path = Path(path)
        self._loader = loader

    @contextmanager
    def _dir(self):
        try:
            dfd = os.open(self.path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | os.O_CLOEXEC)
            st = os.fstat(dfd)
        except OSError:
            raise PeerRegistryError("registry_source_unavailable") from None
        try:
            if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) & 0o022:
                raise PeerRegistryError("registry_source_unsafe")
            yield dfd
        finally:
            os.close(dfd)

    @contextmanager
    def _lock(self, *, exclusive: bool, deadline=None):
        with self._dir() as dfd:
            try:
                fd = os.open(self.LOCK_NAME, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=dfd)
            except OSError:
                raise PeerRegistryError("registry_source_unsafe") from None
            try:
                st = os.fstat(fd)
                if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o600:
                    raise PeerRegistryError("registry_source_unsafe")
                mode = (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB
                deadline = min(time.monotonic() + self.LOCK_WAIT, deadline if deadline is not None else float('inf'))
                while True:
                    try:
                        fcntl.flock(fd, mode)
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise PeerRegistryError("registry_source_busy") from None
                        time.sleep(min(0.005, max(0, deadline - time.monotonic())))
                try:
                    yield dfd
                finally:
                    fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def write_lock(self):
        """Exclusive cutover lock; every registry writer must hold it while changing the file."""
        return self._lock(exclusive=True)

    def read_current(self) -> "PeerRegistry":
        """A fresh registry parsed from the file as it is NOW, under the shared lock."""
        with self.locked_current() as fresh:
            return fresh

    @contextmanager
    def locked_current(self, *, deadline=None):
        """Yield a fresh registry parsed from the file while HOLDING the shared lock (R3B-05).

        A generation cutover (``write_lock``, exclusive) cannot slip between this read and the
        caller's first effect, so a write may use the yielded registry to authorize itself.  The
        lock is held only for the file read plus the caller's bounded local write -- never across
        network IO.  ``deadline`` defaults to the propagated request deadline.
        """
        if deadline is None:
            deadline = ADMISSION_DEADLINE.get()
        if deadline is not None and time.monotonic() >= deadline:
            raise PeerRegistryError("registry_source_busy")
        with self._lock(exclusive=False, deadline=deadline):
            if deadline is not None and time.monotonic() >= deadline:
                raise PeerRegistryError("registry_source_busy")
            try:
                fresh = self._loader(self.path)
            except PeerRegistryError:
                raise
            except Exception:
                raise PeerRegistryError("registry_source_unavailable") from None
            # Yield OUTSIDE the try: an exception from the caller's own body must never be remapped.
            yield fresh

    def version(self):
        st = os.stat(self.path, follow_symlinks=False)
        if not stat.S_ISREG(st.st_mode):
            raise PeerRegistryError("registry_source_unsafe")
        return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns

    def replace(self, entries: list) -> None:
        """Atomic, durable, lock-holding replacement of the registry file (the only supported writer)."""
        data = json.dumps(entries, sort_keys=True).encode()
        with self._lock(exclusive=True) as dfd:
            staging = f".{self.path.name}.{os.getpid()}.{time.monotonic_ns()}.new"
            fd = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=dfd)
            try:
                try:
                    view = memoryview(data)
                    while view:
                        view = view[os.write(fd, view):]
                    os.fsync(fd)
                finally:
                    os.close(fd)
                os.replace(staging, self.path.name, src_dir_fd=dfd, dst_dir_fd=dfd)
            except BaseException:
                try:
                    os.unlink(staging, dir_fd=dfd)
                except FileNotFoundError:
                    pass
                raise
            os.fsync(dfd)


@dataclass
class _PeerEntry:
    peer_id: str
    installation_id: str
    profile: str
    fingerprint: str
    endpoint_id: str
    catalog: dict
    catalog_digest: str
    policy_digest: str
    consent_public_key: bytes
    tls_ca_pem: str
    pairing_generation: int


class PeerRegistry:
    def __init__(self, *, receipt_store=None, route_provider: Optional[Callable] = None,
                 source: Optional[RegistrySource] = None, authority=None):
        self._peers: dict[str, _PeerEntry] = {}
        self._receipt_store = receipt_store
        self._route_provider = route_provider
        self._source = source
        self._authority = authority

    def material_capability(self) -> bool:
        """Explicit capability seam for activation: a receipt store AND a route provider are installed."""
        return self._receipt_store is not None and callable(self._route_provider)

    def has_source(self) -> bool:
        return self._source is not None

    @staticmethod
    def _matches(fresh: "PeerRegistry", peer_id: str, generation, binding: dict) -> bool:
        """Does the freshly parsed authoritative registry still carry this exact generation/binding?"""
        try:
            record = fresh.lookup(peer_id)
            return record.pairing_generation == generation and fresh.binding_for(peer_id) == binding
        except Exception:
            return False

    def _authoritative(self, peer_id: str, generation, binding: dict):
        """``(version, fresh)`` only if the AUTHORITATIVE file still matches; any doubt fails closed.

        Never consults this instance's memory.  A busy/unreadable/replaced file is
        ``peer_binding_changed`` (the caller cannot prove its own currency); no installed source is
        ``admission_unavailable``.
        """
        source = self._source
        if source is None:
            raise PeerRegistryError("admission_unavailable")
        try:
            version = source.version()
            fresh = source.read_current()
            if source.version() != version:
                raise PeerRegistryError("peer_binding_changed")
        except Exception:
            # Same fail-closed mapping as before: busy, unreadable or already-replaced is one code.
            raise PeerRegistryError("peer_binding_changed") from None
        if not self._matches(fresh, peer_id, generation, binding):
            raise PeerRegistryError("peer_binding_changed")
        return version, fresh

    @staticmethod
    def _require_unchanged(source: "RegistrySource", version) -> None:
        """Re-stat the authoritative file immediately before the first effect (R3B-05)."""
        try:
            current = source.version()
        except OSError:
            raise PeerRegistryError("peer_binding_changed") from None
        if current != version:
            raise PeerRegistryError("peer_binding_changed")

    def check_admission(self, peer_id: str, generation, binding: dict) -> Callable[[], None]:
        """R1-05 admission check against the AUTHORITATIVE on-disk registry (never this instance's memory).

        Re-reads ``peers.json`` under the shared registry lock and requires the exact generation and
        six-field binding (which includes the endpoint) the request was built from.  Any doubt fails closed.
        Returns a non-waiting final validator for the transport to call AFTER its worker await.
        """
        if self._source is None:
            raise PeerRegistryError("admission_unavailable")
        source = self._source
        version, fresh = self._authoritative(peer_id, generation, binding)

        def final_check():
            # Called on-loop AFTER the worker await. No polling or parsing here: a busy writer fails closed.
            # The only thing that can change is the on-disk file, and every supported writer replaces it
            # (new inode/mtime/ctime/size) while holding the exclusive lock we take here. ``fresh`` is an
            # immutable parse of the version captured above and its generation/binding were verified at
            # check time, so re-comparing them (or ``self._source``, a constant closure value) could never
            # differ: the version compare under the shared lock is what proves nothing changed.
            with source._lock(exclusive=False, deadline=time.monotonic()):
                if source.version() != version:
                    raise PeerRegistryError("peer_binding_changed")
        return final_check

    def register(self, *, peer_id: str, installation_id: str, profile: str, fingerprint: str,
                 endpoint_id: str, catalog: dict, consent_public_key: bytes,
                 tls_ca_pem: str, pairing_generation: int) -> None:
        _check(peer_id, _TOKEN, "peer_id"); _check(installation_id, _INSTALL, "installation_id")
        _check(profile, _PROFILE, "profile"); _check(fingerprint, _FINGERPRINT, "fingerprint"); _check(endpoint_id, _TOKEN, "endpoint_id")
        if peer_id in self._peers: raise PeerRegistryError("peer_already_registered")
        if not isinstance(consent_public_key, bytes) or len(consent_public_key) != 32: raise PeerRegistryError("invalid_consent_public_key")
        if isinstance(pairing_generation, bool) or not isinstance(pairing_generation, int) or pairing_generation < 1: raise PeerRegistryError("invalid_pairing_generation")
        if not isinstance(catalog, dict): raise PeerRegistryError("invalid_catalog")
        validated = _native_catalog(catalog)
        if validated.installation_id != installation_id: raise PeerRegistryError("catalog_installation_mismatch")
        if validated.execution_policy.target_profile != profile: raise PeerRegistryError("catalog_profile_mismatch")
        _validate_ca(tls_ca_pem)
        self._peers[peer_id] = _PeerEntry(peer_id, installation_id, profile, fingerprint, endpoint_id,
            copy.deepcopy(catalog), validated.catalog_digest, validated.execution_policy.policy_digest,
            bytes(consent_public_key), tls_ca_pem, pairing_generation)

    def lookup(self, peer_id: str) -> PeerRecord:
        entry = self._entry(peer_id)
        return PeerRecord(entry.peer_id, entry.installation_id, entry.profile, entry.fingerprint, entry.endpoint_id,
                          entry.catalog_digest, entry.consent_public_key, entry.pairing_generation)

    def _entry(self, peer_id):
        _check(peer_id, _TOKEN, "peer_id")
        entry = self._peers.get(peer_id)
        if entry is None: raise PeerRegistryError("unknown_peer")
        return entry

    def list_peers(self) -> list:
        return [{"peer_id": e.peer_id, "installation_id": e.installation_id, "profile": e.profile, "capability_digest": e.catalog_digest}
                for e in sorted(self._peers.values(), key=lambda x: x.peer_id)]

    def binding_for(self, peer_id: str) -> dict:
        e = self._entry(peer_id)
        return {"endpoint_id": e.endpoint_id, "fingerprint": e.fingerprint, "target_install_id": e.installation_id,
                "target_profile": e.profile, "catalog_digest": e.catalog_digest, "policy_digest": e.policy_digest}

    def _validate_scope(self, scope: MaterialScope) -> _PeerEntry:
        if not isinstance(scope, MaterialScope): raise PeerRegistryError("invalid_material_scope")
        e = self._entry(scope.peer_id)
        if scope.pairing_generation != e.pairing_generation or scope.binding != self.binding_for(scope.peer_id):
            raise PeerRegistryError("binding_mismatch")
        try: validated = _native_catalog(e.catalog)
        except PeerRegistryError: raise PeerRegistryError("catalog_drift") from None
        if validated.catalog_digest != e.catalog_digest or validated.execution_policy.policy_digest != e.policy_digest:
            raise PeerRegistryError("catalog_drift")
        return e

    def _material_from_record(self, scope: MaterialScope, record) -> PeerMaterial:
        e = self._validate_scope(scope)
        if not isinstance(record, dict): raise PeerRegistryError("material_unavailable")
        expected_id = adapter_id_for(scope)
        adapter_url = record.get("adapter_url")
        if adapter_url is None:
            port = self._route_provider(scope) if callable(self._route_provider) else None
            adapter_url = f"http://127.0.0.1:{port}/links/{expected_id}" if isinstance(port, int) and 1 <= port <= 65535 else None
        if not isinstance(adapter_url, str): raise PeerRegistryError("material_channel_unavailable")
        try: _validate_adapter(adapter_url)
        except InviteError: raise PeerRegistryError("invalid_adapter") from None
        if record.get("binding") != scope.binding or record.get("catalog") != e.catalog or not valid_grant(record.get("grant")):
            raise PeerRegistryError("material_scope_mismatch")
        return PeerMaterial(binding=scope.binding, catalog=copy.deepcopy(e.catalog), grant=record["grant"], adapter_url=adapter_url)

    async def resolve_scoped_material(self, scope: MaterialScope, resolver=None) -> PeerMaterial:
        self._validate_scope(scope)
        if self._receipt_store is None: raise PeerRegistryError("material_scope_unapproved")
        limit = time.monotonic() + MATERIAL_RESOLVE_TIMEOUT
        inherited = ADMISSION_DEADLINE.get()
        if inherited is not None:
            limit = min(limit, inherited)
        try:
            record = await run_admission(self._receipt_store.load, scope, deadline=limit,
                                         end_deadline=limit, lane=LANE_MATERIAL)
        except PeerRegistryError as exc:
            # Capacity and deadline refusals (R3B-04) are never reported as a resolved material.
            code = str(exc)
            raise PeerRegistryError(code if code == "admission_overloaded" else "material_unavailable") from None
        except Exception as exc:
            code = str(exc)
            raise PeerRegistryError(code if code in {"material_scope_unapproved", "material_unavailable", "material_scope_conflict"} else "material_unavailable") from None
        if time.monotonic() >= limit:
            raise PeerRegistryError("material_unavailable")  # post-worker expiry, BEFORE the value is used
        return self._material_from_record(scope, record)

    def admit_signed_material(self, scope: MaterialScope, envelope: dict) -> None:
        """Host admission boundary: verify pinned peer consent, re-check the AUTHORITATIVE generation, persist.

        R3B-05: the in-memory registry is only a pre-check.  The write re-reads ``peers.json`` under the
        shared registry lock (so a generation cutover cannot interleave with the persist), requires the
        exact generation and binding the caller validated, and restates that requirement immediately
        before the first effect.  A stale registry therefore cannot persist a receipt -- it is refused
        with ``peer_binding_changed`` and nothing is written.

        Blocking (registry + receipt IO): callers on the event loop MUST use
        :meth:`admit_signed_material_async` (or ``run_admission`` with ``lane=LANE_CALLBACK``), which
        runs this on the admission executor with the capacity reserved for callbacks.
        """
        if self._receipt_store is None: raise PeerRegistryError("material_unavailable")
        self._validate_scope(scope)  # exact signature/scope pre-check; unchanged in-memory validation
        source = self._source
        if source is None:
            raise PeerRegistryError("admission_unavailable")  # no authoritative registry: cannot prove currency
        try:
            authority = self._authority
            if authority is None or not callable(getattr(authority, "receipt_admission", None)):
                raise PeerRegistryError("admission_unavailable")
            # The authority takes the journal lock first.  Never move source.locked_current outside
            # this context: unpair uses the same journal -> registry order.
            with authority.receipt_admission(source, endpoint_id=scope.binding["endpoint_id"],
                                             generation=scope.pairing_generation,
                                             binding=scope.binding,
                                             deadline=ADMISSION_DEADLINE.get()) as journal_peer:
                with source.locked_current() as fresh:
                    version = source.version()
                    journal_peer_id = getattr(journal_peer, "peer_id", None)
                    journal_key = getattr(journal_peer, "consent_public_key", None)
                    if journal_peer_id is not None:
                        if not self._matches(fresh, scope.peer_id, scope.pairing_generation, scope.binding):
                            raise PeerRegistryError("peer_binding_changed")
                        if scope.peer_id != journal_peer_id or not isinstance(journal_key, bytes):
                            raise PeerRegistryError("peer_binding_changed")
                        try:
                            entry = fresh.lookup(journal_peer_id)
                        except Exception:
                            raise PeerRegistryError("peer_binding_changed") from None
                        if entry.peer_id != journal_peer_id or entry.consent_public_key != journal_key:
                            raise PeerRegistryError("peer_binding_changed")
                        consent_key = journal_key
                    else:
                        if not self._matches(fresh, scope.peer_id, scope.pairing_generation, scope.binding):
                            raise PeerRegistryError("peer_binding_changed")
                        entry = fresh.lookup(scope.peer_id)
                        consent_key = entry.consent_public_key
                    self._receipt_store.admit_envelope(
                        scope=scope, envelope=envelope, consent_public_key=consent_key,
                        expected_signed={"scope": envelope["scope"], "grant": envelope["grant"],
                                         "catalog": envelope["catalog"], "purpose": "joining-owner-material-v1"},
                        map_scope=lambda received, expected: self._assert_envelope_scope(received, expected),
                        guard=lambda: self._require_unchanged(source, version),
                    )
        except Exception as exc:
            code = str(exc)
            if code == "unknown_peer":
                code = "peer_binding_changed"
            raise PeerRegistryError(code if code in {"invalid_consent", "invalid_catalog", "invalid_material", "material_scope_conflict", "material_unavailable", "material_scope_mismatch", "peer_binding_changed", "admission_unavailable", "journal_busy", "journal_corrupt", "journal_storage_unavailable", "registry_source_busy", "admission_deadline_exceeded"} else "material_unavailable") from None

    async def admit_signed_material_async(self, scope: MaterialScope, envelope: dict) -> None:
        """The off-loop receipt-write entry point: :meth:`admit_signed_material` on the callback lane.

        Runs on the dedicated admission executor (never the loop's default executor) with the capacity
        reserved for callbacks, so a write cannot starve behind material reads (R3B-04).  A write that
        finishes after its deadline is refused with ``admission_deadline_exceeded`` -- the caller must
        treat it as an unconfirmed admit, never as success (the write may still be idempotently retried).
        """
        await run_admission(self.admit_signed_material, scope, envelope, lane=LANE_CALLBACK)

    _WIRE_SCOPE_FIELDS = frozenset({"host_id", "room_id", "lobby_id", "member_id", "invite_id", "binding"})

    @classmethod
    def _assert_envelope_scope(cls, received, expected):
        """Map the TARGET-signed scope onto the trusted local MaterialScope (after signature verification).

        The target signs exactly {host_id, room_id, lobby_id, member_id, invite_id, binding}
        (joining.py / joining_material.py).  host_id is the trusted owner installation and room_id the
        planned room; the rest is compared verbatim, including the nested six-field binding.
        owner_profile, peer_id and pairing_generation are host-local and are NEVER taken from the wire."""
        if not isinstance(received, dict) or set(received) != cls._WIRE_SCOPE_FIELDS:
            raise PeerRegistryError("material_scope_mismatch")
        if (received["host_id"] != expected.owner_installation_id or received["room_id"] != expected.planned_room_id
                or received["lobby_id"] != expected.lobby_id or received["member_id"] != expected.member_id
                or received["invite_id"] != expected.invite_id or received["binding"] != expected.binding):
            raise PeerRegistryError("material_scope_mismatch")
