"""Owner-private trusted peer registry and scoped material resolution."""
from __future__ import annotations

import copy
import contextvars
import fcntl
import hashlib
import json
import os
import re
import stat
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from room_invite_store import InviteError
from room_lobby_service import PeerMaterial, _validate_adapter
from invite_rooms.consent import canonical
from invite_rooms.material_receipts import valid_grant
from invite_rooms.peer_binding import PeerBindingError, validate_peer_binding


class PeerRegistryError(ValueError):
    """Sanitized machine code; never includes paths, input or secrets."""


_TOKEN = r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}"
_PROFILE = r"[a-z0-9][a-z0-9_-]{0,63}"
_INSTALL = r"install:[0-9a-f]{32}"
_FINGERPRINT = r"[0-9a-f]{64}"
_DIGEST = r"[0-9a-f]{64}"

# Propagated by asyncio.to_thread to bound even legacy no-argument admission callbacks.
ADMISSION_DEADLINE: contextvars.ContextVar[float | None] = contextvars.ContextVar("registry_admission_deadline", default=None)


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
        deadline = ADMISSION_DEADLINE.get()
        if deadline is not None and time.monotonic() >= deadline:
            raise PeerRegistryError("registry_source_busy")
        with self._lock(exclusive=False, deadline=deadline):
            if deadline is not None and time.monotonic() >= deadline:
                raise PeerRegistryError("registry_source_busy")
            return self._loader(self.path)

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
                 source: Optional[RegistrySource] = None):
        self._peers: dict[str, _PeerEntry] = {}
        self._receipt_store = receipt_store
        self._route_provider = route_provider
        self._source = source

    def material_capability(self) -> bool:
        """Explicit capability seam for activation: a receipt store AND a route provider are installed."""
        return self._receipt_store is not None and callable(self._route_provider)

    def has_source(self) -> bool:
        return self._source is not None

    def check_admission(self, peer_id: str, generation, binding: dict) -> Callable[[], None]:
        """R1-05 admission check against the AUTHORITATIVE on-disk registry (never this instance's memory).

        Re-reads ``peers.json`` under the shared registry lock and requires the exact generation and
        six-field binding (which includes the endpoint) the request was built from.  Any doubt fails closed.
        Returns a non-waiting final validator for the transport to call AFTER its worker await.
        """
        if self._source is None:
            raise PeerRegistryError("admission_unavailable")
        try:
            source = self._source
            version = source.version()
            fresh = source.read_current()
            if source.version() != version:
                raise PeerRegistryError("peer_binding_changed")
            current = fresh.lookup(peer_id)
            ok = current.pairing_generation == generation and fresh.binding_for(peer_id) == binding
        except Exception:
            raise PeerRegistryError("peer_binding_changed") from None
        if not ok:
            raise PeerRegistryError("peer_binding_changed")

        def final_check():
            # Called on-loop AFTER the worker await. No polling or parsing here: a busy
            # writer fails closed. Supported writers cannot change the version under this lock.
            with source._lock(exclusive=False, deadline=time.monotonic()):
                if source.version() != version or self._source is not source:
                    raise PeerRegistryError("peer_binding_changed")
                if fresh.lookup(peer_id).pairing_generation != generation or fresh.binding_for(peer_id) != binding:
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
        try: record = self._receipt_store.load(scope)
        except Exception as exc:
            code = str(exc)
            raise PeerRegistryError(code if code in {"material_scope_unapproved", "material_unavailable", "material_scope_conflict"} else "material_unavailable") from None
        return self._material_from_record(scope, record)

    def admit_signed_material(self, scope: MaterialScope, envelope: dict) -> None:
        """Host admission boundary: verify pinned peer consent, then persist once."""
        if self._receipt_store is None: raise PeerRegistryError("material_unavailable")
        entry = self._validate_scope(scope)
        try:
            self._receipt_store.admit_envelope(
                scope=scope, envelope=envelope, consent_public_key=entry.consent_public_key,
                expected_signed={"scope": envelope["scope"], "grant": envelope["grant"],
                                 "catalog": envelope["catalog"], "purpose": "joining-owner-material-v1"},
                map_scope=lambda received, expected: self._assert_envelope_scope(received, expected),
            )
        except Exception as exc:
            code = str(exc)
            raise PeerRegistryError(code if code in {"invalid_consent", "invalid_catalog", "invalid_material", "material_scope_conflict", "material_unavailable", "material_scope_mismatch"} else "material_unavailable") from None

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
