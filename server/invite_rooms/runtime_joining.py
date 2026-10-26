"""Runtime wiring for the joining-owner ``/imports`` surface (P3b.2).

This module is the ONLY place where the transport-agnostic
:class:`invite_rooms.joining.JoiningOwnerService` meets the real product stores
for the *joining* side of an invite-rooms pairing.  It does four things and
nothing else:

1. **One verified host.**  :func:`project_hosts` re-reads the hash-chained
   pairing journal under its lock and derives the single host this installation
   may import invitations from -- from the CONFIRMED links in the journal, never
   from an invitation, a browser field or the peer registry file.  The result is
   published once to ``joining.hosts_path`` (owner-private 0600 in a 0700 parent,
   atomic replace, no-follow reader) and afterwards only VERIFIED: a projection
   that disagrees with the journal (manual edit, chmod, symlink, stale
   generation, superseded pairing) fails closed and is never repaired.
2. **One approved registry.**  The :class:`~invite_rooms.invitation.ApprovedHostRegistry`
   handed to the product service is built exclusively from that verified link:
   no invitation and no HTTP caller can install a host, endpoint, fingerprint,
   digest or generation.
3. **One control channel.**  :class:`PinnedControlChannel` speaks the joining
   control protocol over the existing bounded connector
   (``invite_rooms.control.connector.HostConnector``) with a per-call absolute
   deadline.  No new transport, no ``requests``, no unbounded sockets, no
   request-selected destination.  It is installed at PROCESS wiring time via
   :func:`configure_joining_control`; the default stays ``None`` so that remote
   operations fail closed with ``control_unavailable`` until P3b.3c supplies the
   pinned transport (:func:`configure_joining_transport`).
4. **The HTTP action map.**  :meth:`JoiningOwner.execute` is a thin, exact map
   from the seven owner actions to the accepted product service calls plus the
   read model the sidecar renders.  No action ever invents a host, session,
   grant, scope or redemption: ``resolve`` is *always*
   ``409 manual_reconciliation_required`` because this surface carries no
   operator evidence.

The local identity is read with the existing safe reader -- never initialized,
repaired or re-keyed here -- and the local catalog/leaf/consent key must equal
the joining identity recorded in the pairing's own accept transcript, so the
six-field ``self_binding`` (the shape ``validate_peer_binding`` accepts) can
never come from request input.
"""
from __future__ import annotations

import asyncio
import base64
import copy
import inspect
import json
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional
from urllib.parse import urlsplit

from invite_rooms import identity as identity_module
from invite_rooms import pairing_codec, private_fs
from invite_rooms.consent import ConsentKey, Encoding, PublicFormat
from invite_rooms import runtime_config
from invite_rooms.control.connector import LinkBinding, LinkIdentity, make_link_connector
from invite_rooms.control.dispatcher import ControlDispatcher, ControlError
from invite_rooms.control.firewall import FirewallDenied, ForwardedRequest, UpstreamAmbiguous, UpstreamResponse
from invite_rooms.invitation import ApprovedHostRegistry, InvitationError
from invite_rooms.joining import JoiningError, JoiningOwnerService
from invite_rooms.joining_store import JoiningStore, JoiningStoreError
from invite_rooms.pairing_journal import Journal, JournalError, JournalLinkAuthority
from invite_rooms.pairing_journal import binding_for as link_binding
from invite_rooms.peer_binding import PeerBindingError, validate_peer_binding
from invite_rooms.peer_registry import PeerRegistryError, _native_catalog

#: Absolute per-operation budget for ONE control call (seconds).  Test seam:
#: the P3b.2 spec patches this module attribute to prove that a reply produced
#: after the budget is never emitted.
TIMEOUT = 5.0
#: Whole-request budget used when the caller supplies no absolute deadline.
REQUEST_BUDGET = 30.0

PROJECTION_VERSION = 1
PROJECTION_MAX = 256 * 1024
CONTROL_PATH = "/control/joining"        # fixed control path, never request-derived
CONTROL_RESPONSE_MAX = 16 * 1024
CONTROL_REQUEST_MAX = 16 * 1024

_ROOM_ID = re.compile(r"invite-room-[A-Za-z0-9-]{1,64}")
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_LOBBY_ID = re.compile(r"lobby-[a-zA-Z0-9-]{1,64}")
_SCOPE_FIELDS = {"room_id", "member_id", "lobby_id", "invite_id", "host_id", "binding"}
#: Host statuses the adapter accepts from the existing store vocabulary.
_REMOTE_STATUS = frozenset({"awaiting_host_approval", "consent_required", "ready", "active",
                            "denied", "refused", "expired", "cancelled"})
#: Local import states for which the host's authenticated status is authoritative.
_REMOTE_LOCAL_STATES = frozenset({"redeemed", "consent_pending_delivery", "consent_given"})
_ACTIVE_STATES = frozenset({"confirmed", "acknowledged"})
_PROJECTION_KEYS = {"version", "revision", "hosts"}
#: The product service speaks ``joining_consent``; the control wire's allowed op
#: set spells it ``consent`` (the transport maps it back to the same handler).
_WIRE_OP = {"joining_consent": "consent"}

_CONTROL_FACTORY: Optional[Callable] = None
_TRANSPORT_FACTORY: Optional[Callable] = None
_FACTORY_LOCK = threading.Lock()


def configure_joining_control(factory=None) -> None:
    """Install (or clear) the joining control-channel factory -- PROCESS wiring only.

    ``factory(config, verified_link) -> channel`` where ``channel`` exposes
    ``call(request, *, deadline, admit)`` (sync or async).  It must not perform
    IO during construction.  Absent factory == every remote action returns
    ``503 control_unavailable`` (local preview still works).
    """
    global _CONTROL_FACTORY
    if factory is not None and not callable(factory):
        raise ValueError("invalid_joining_control_factory")
    with _FACTORY_LOCK:
        _CONTROL_FACTORY = factory


def configure_joining_transport(factory=None) -> None:
    """Install (or clear) the pinned-transport resolver used by the real channel.

    ``factory(verified_link, deadline) -> connector`` where ``connector`` is a
    ``invite_rooms.control.connector.HostConnector`` (built by
    ``make_link_connector``) for exactly that endpoint/generation.  P3b.3c owns
    the address/CA materialization; without it the real channel refuses.
    """
    global _TRANSPORT_FACTORY
    if factory is not None and not callable(factory):
        raise ValueError("invalid_joining_transport_factory")
    with _FACTORY_LOCK:
        _TRANSPORT_FACTORY = factory


# ---------------------------------------------------------------------------------------------------------------
# Host projection
# ---------------------------------------------------------------------------------------------------------------
def binding_for(link: Mapping) -> dict:
    """The six-field binding of the HOST link as the joining side sees it."""
    return validate_peer_binding(link_binding(link))


@dataclass(frozen=True)
class HostProjection:
    """The verified result of :func:`project_hosts`: one host, one registry."""

    registry: ApprovedHostRegistry
    link: Mapping
    state: Any
    peer_id: str
    origin: str
    generation: int
    host_peer_id: str          # internal stable key "<endpoint_id>:<generation>"
    binding: Mapping           # the host's six-field binding
    revision: int

    @property
    def endpoint_id(self) -> str:
        return self.link["endpoint_id"]


def _pinned_origin(value: Any) -> bool:
    """Mirror of ``parse_invitation``'s pinned-origin rule (scheme+netloc only)."""
    if not isinstance(value, str) or not value:
        return False
    try:
        parts = urlsplit(value)
    except ValueError:
        return False
    return (parts.scheme == "https" and bool(parts.netloc) and not parts.username
            and not parts.password and not parts.path and not parts.query and not parts.fragment)


def _internal_host_id(link: Mapping) -> str:
    return f"{link['endpoint_id']}:{link['generation']}"


def _select_link(state, origins: Mapping) -> Mapping:
    """Exactly one ACTIVE joining-role link, with an owner-pinned origin.

    Counting includes *confirmed but not yet acknowledged* links, so a second
    host is refused even when the browser selects one.  A link that supersedes a
    revoked one (generation > 1) must have completed the ``pair_ack`` handshake
    before it can carry imports: an unpair is an owner decision, and a silent
    re-pair must not resurrect the import surface on a link whose generation
    cutover the two sides have not agreed on yet.
    """
    candidates = [link for link in state.active_links() if link.get("role") == "joining"]
    if len(candidates) != 1:
        raise JoiningError("unknown_host")
    link = candidates[0]
    if link["protocol_state"] not in _ACTIVE_STATES:
        raise JoiningError("unknown_host")
    generation = link["generation"]
    if type(generation) is not int or generation < 1:
        raise JoiningError("unknown_host")
    if generation != 1 and link["protocol_state"] != "acknowledged":
        raise JoiningError("unknown_host")
    origin = origins.get(link["peer"]["peer_id"]) if isinstance(origins, Mapping) else None
    if not _pinned_origin(origin):
        raise JoiningError("unknown_host")
    return link


def _publish_projection(dirfd: int, name: str, document: Mapping) -> None:
    """Atomic, durable, 0600 replacement of the projection (no-follow, single link)."""
    data = json.dumps(document, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("ascii")
    staging = f".{name}.{os.getpid()}.{time.monotonic_ns()}.new"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(staging, flags, 0o600, dir_fd=dirfd)
    except OSError:
        raise JoiningError("unknown_host") from None
    try:
        try:
            os.fchmod(fd, 0o600)
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            private_fs.fsync(fd)
        finally:
            os.close(fd)
        os.replace(staging, name, src_dir_fd=dirfd, dst_dir_fd=dirfd)
    except BaseException:
        try:
            os.unlink(staging, dir_fd=dirfd)
        except FileNotFoundError:
            pass
        raise
    private_fs.fsync(dirfd)


def _verify_or_publish_projection(path: Path, document: Mapping) -> None:
    """Publish the projection once; afterwards only verify it, never repair it."""
    try:
        dirfd = private_fs.open_private_dir(path.parent, None, create=False)
    except (private_fs.PrivateStorageError, OSError):
        raise JoiningError("unknown_host") from None
    try:
        try:
            os.stat(path.name, dir_fd=dirfd, follow_symlinks=False)
        except FileNotFoundError:
            _publish_projection(dirfd, path.name, document)
            return
        except OSError:
            raise JoiningError("unknown_host") from None
        try:
            fd = private_fs.open_regular(dirfd, path.name)
        except (private_fs.PrivateStorageError, OSError):
            raise JoiningError("unknown_host") from None
        try:
            raw = private_fs.read_all(fd, PROJECTION_MAX)
        except (private_fs.PrivateStorageError, OSError):
            raise JoiningError("unknown_host") from None
        finally:
            os.close(fd)
        try:
            current = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise JoiningError("unknown_host") from None
        if (not isinstance(current, dict) or set(current) != _PROJECTION_KEYS
                or current["version"] != PROJECTION_VERSION
                or type(current["revision"]) is not int
                or not 0 <= current["revision"] <= document["revision"]
                or current["hosts"] != document["hosts"]):
            # Manual edit, chmod, stale generation or superseded pairing: never trusted, never overwritten.
            raise JoiningError("unknown_host")
    finally:
        os.close(dirfd)


def project_hosts(journal: Journal, hosts_path, origins: Mapping) -> HostProjection:
    """Project the ONE confirmed+approved host from the journal; publish/verify ``hosts_path``.

    Never derives a host from the invitation, the browser or the peer registry
    file: the journal is the only authority and ``origins`` is the only origin
    source (a peer absent from it is not usable).
    """
    state = journal.snapshot()
    link = _select_link(state, origins)
    peer = link["peer"]
    peer_id = peer["peer_id"]
    try:
        binding = binding_for(link)
        consent_public_key = base64.b64decode(peer["consent_public_key_b64"], validate=True)
        if len(consent_public_key) != 32:
            raise ValueError()
    except (PeerBindingError, ValueError, TypeError, KeyError):
        raise JoiningError("unknown_host") from None
    generation = link["generation"]
    endpoint_id = link["endpoint_id"]
    host_peer_id = _internal_host_id(link)
    document = {
        "version": PROJECTION_VERSION,
        "revision": state.seq,
        "hosts": [{
            "peer_id": peer_id,
            "endpoint_id": endpoint_id,
            "origin": origins[peer_id],
            "generation": generation,
            "binding": binding,
        }],
    }
    _verify_or_publish_projection(Path(hosts_path), document)
    registry = ApprovedHostRegistry()
    try:
        registry.register(peer_id=host_peer_id, endpoint_id=endpoint_id,
                          installation_id=binding["target_install_id"],
                          profile=binding["target_profile"], fingerprint=binding["fingerprint"],
                          policy_digest=binding["policy_digest"], catalog_digest=binding["catalog_digest"],
                          consent_public_key=consent_public_key, pairing_generation=generation)
    except InvitationError:
        raise JoiningError("unknown_host") from None
    return HostProjection(registry=registry, link=link, state=state, peer_id=peer_id,
                          origin=origins[peer_id], generation=generation, host_peer_id=host_peer_id,
                          binding=binding, revision=state.seq)


def self_binding_for(state, link: Mapping, bundle, installation_id: str, profile: str):
    """The LOCAL six-field binding, from the local identity + this pairing's own accept.

    The accept transcript in the journal is what the host validated as us, so it
    is the only honest source for the local catalog/policy digests.  Any
    disagreement with the loaded identity bundle fails closed: a re-keyed or
    swapped identity must never be presented as the paired one.
    """
    session = state.sessions.get(link["session_id"]) if isinstance(link.get("session_id"), str) else None
    if session is None or session.get("role") != "joining" or not session.get("accept"):
        raise JoiningError("unknown_host")
    try:
        offer = pairing_codec.parse_offer(session["offer"], now=None)
        accept = pairing_codec.parse_accept(session["accept"], offer=offer, now=None)
    except pairing_codec.PairingCodecError:
        raise JoiningError("unknown_host") from None
    who = accept.identity
    if (who.installation_id != installation_id or who.profile != profile
            or who.leaf_fingerprint != bundle.leaf_fingerprint
            or who.consent_public_key != bundle.consent_public_key):
        raise JoiningError("unknown_host")
    try:
        catalog = json.loads(json.dumps(dict(who.catalog)))
        validated = _native_catalog(copy.deepcopy(catalog))
    except (PeerRegistryError, TypeError, ValueError):
        raise JoiningError("unknown_host") from None
    if validated.installation_id != installation_id or validated.execution_policy.target_profile != profile:
        raise JoiningError("unknown_host")
    try:
        binding = validate_peer_binding({
            "endpoint_id": link["endpoint_id"],
            "fingerprint": bundle.leaf_fingerprint,
            "target_install_id": installation_id,
            "target_profile": profile,
            "catalog_digest": validated.catalog_digest,
            "policy_digest": validated.execution_policy.policy_digest,
        })
    except PeerBindingError:
        raise JoiningError("unknown_host") from None
    return binding, catalog, validated


# ---------------------------------------------------------------------------------------------------------------
# Consent key bound to the paired identity
# ---------------------------------------------------------------------------------------------------------------
class _IdentityConsentKey(ConsentKey):
    """The consent signer is the PAIRED identity's key, not a second credential.

    ``ConsentKey.__init__`` creates a fresh ``consent.key`` file, which a joining
    owner must never do: the host only knows the public key carried by the
    pairing accept.  So the constructor is deliberately not called; only the
    canonical ``sign`` behaviour is reused (and is what the spec patches to prove
    a retry reuses the persisted signature).
    """

    def __init__(self, bundle):
        self._key = bundle.consent_key()
        self.public_bytes = self._key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)

    def sign(self, receipt: dict) -> str:
        return ConsentKey.sign(self, receipt)


# ---------------------------------------------------------------------------------------------------------------
# Real control channel over the existing bounded connector
# ---------------------------------------------------------------------------------------------------------------
class PinnedControlChannel:
    """The real joining control channel: one verified endpoint, one bounded send.

    The request is schema-checked with the dispatcher's own request validator
    (the same allowlist the transport enforces), encoded as one forwarded POST on
    a FIXED path, and handed to the existing pinned ``HostConnector`` for exactly
    the verified endpoint/generation.  A connector is built per call so the
    remaining absolute deadline also bounds TLS setup, and it is closed before
    returning.  ``admit()`` is invoked by the connector immediately before its
    first application frame -- the durable R1-05 boundary.
    """

    def __init__(self, link: Mapping, *, connector_factory: Optional[Callable] = None,
                 path: str = CONTROL_PATH, timeout: float = TIMEOUT):
        if not isinstance(link, Mapping):
            raise ControlError("control_unavailable")
        self.endpoint_id = link.get("endpoint_id")
        self.generation = link.get("generation")
        if not isinstance(self.endpoint_id, str) or type(self.generation) is not int:
            raise ControlError("control_unavailable")
        self._link = dict(link)
        self._connector_factory = connector_factory
        self.path = path
        self.timeout = timeout

    # -- protocol ---------------------------------------------------------------------------------------------
    async def call(self, request, *, deadline, admit):
        self._check_deadline(deadline)
        op, payload = ControlDispatcher._request(copy.deepcopy(request))
        body = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        if len(body) > CONTROL_REQUEST_MAX:
            raise ControlError("control_unavailable")
        connector = self._connector(deadline)
        try:
            response = await asyncio.to_thread(
                connector.forward, ForwardedRequest("POST", self.path, (), body), admit)
        except (FirewallDenied, UpstreamAmbiguous):
            raise ControlError("control_unavailable") from None
        except ControlError:
            raise
        except Exception:
            raise ControlError("control_unavailable") from None
        finally:
            self._close_connector(connector)
        self._check_deadline(deadline)
        return self._decode(response)

    async def close(self) -> None:
        """No persistent state: every call owns and closes its own connector."""
        return None

    # -- helpers ----------------------------------------------------------------------------------------------
    @staticmethod
    def _check_deadline(deadline) -> None:
        if type(deadline) not in (int, float):
            raise ControlError("control_unavailable")
        if deadline <= 0 or time.monotonic() >= deadline:
            raise ControlError("control_unavailable")

    def _connector(self, deadline):
        factory = self._connector_factory
        if factory is None:
            with _FACTORY_LOCK:
                factory = _TRANSPORT_FACTORY
        if factory is None:
            raise ControlError("control_unavailable")
        remaining = deadline - time.monotonic()
        if not remaining > 0:
            raise ControlError("control_unavailable")
        try:
            connector = factory(dict(self._link), min(self.timeout, remaining))
        except ControlError:
            raise
        except Exception:
            connector = None
        if connector is None or not callable(getattr(connector, "forward", None)):
            raise ControlError("control_unavailable")
        return connector

    @staticmethod
    def _close_connector(connector) -> None:
        closer = getattr(connector, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:
                pass

    @staticmethod
    def _decode(response) -> dict:
        if not isinstance(response, UpstreamResponse) or response.status != 200:
            raise ControlError("control_unavailable")
        body = response.body
        if not isinstance(body, bytes) or len(body) > CONTROL_RESPONSE_MAX:
            raise ControlError("control_unavailable")
        try:
            value = json.loads(body.decode("utf-8"), object_pairs_hook=_unique, parse_constant=_refuse_constant)
        except (ValueError, TypeError, UnicodeDecodeError, RecursionError):
            raise ControlError("control_unavailable") from None
        if not isinstance(value, dict):
            raise ControlError("control_unavailable")
        return value


def _unique(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate")
        out[key] = value
    return out


def _refuse_constant(_name):
    raise ValueError("constant")


def _config_connector_factory(config) -> Callable:
    """Per-config pinned transport: ``factory(link, remaining) -> HostConnector`` (loopback mTLS, in-memory CA).

    Everything that decides trust is re-read at CALL time from authoritative state, never from the caller's link
    snapshot, the peer-registry projection or request data:

    * the peer record (leaf pin, CA PEM, binding, generation, state) comes from the pairing journal;
    * the local certificate/key come from the verified local identity bundle;
    * the destination comes ONLY from the owner-private ``joining.control_endpoints`` entry for that journal peer.

    The peer CA PEM is handed to ``ssl`` as in-memory ``cadata``; no CA file is ever created or read.  Any failure
    raises (the channel maps it to ``control_unavailable``) before a socket is opened.
    """
    def factory(link: Mapping, remaining: float):
        settings = _joining_settings(config)
        endpoint_id, generation = link["endpoint_id"], link["generation"]
        if not isinstance(endpoint_id, str) or type(generation) is not int:
            raise ControlError("control_unavailable")
        journal = Journal(config.pairing.journal_dir, anchor=config.pairing.journal_anchor)
        authority = JournalLinkAuthority(journal)
        # Runs on the event-loop thread: exactly ONE non-waiting shared-lock read of the verified journal (busy /
        # corrupt / missing / revoked / wrong role / changed generation => immediate refusal, no socket, no effect).
        # The lock is released before the identity-file, TLS and socket work below.
        trust = authority.peer_trust(endpoint_id, generation, role="joining", states=tuple(sorted(_ACTIVE_STATES)))
        if trust is None:
            raise ControlError("control_unavailable")
        peer_id, ca_pem = trust.peer_id, trust.ca_pem
        link_peer = link.get("peer")
        if isinstance(link_peer, Mapping) and link_peer.get("peer_id") not in (None, peer_id):
            raise ControlError("control_unavailable")
        try:
            endpoints = runtime_config.parse_control_endpoints(settings["control_endpoints"])
            host, port = endpoints[peer_id]
        except (runtime_config.RuntimeConfigError, KeyError, TypeError):
            raise ControlError("control_unavailable") from None
        bundle = identity_module.load_identity(settings["identity_dir"], installation_id=config.installation_id,
                                               profile=config.profile)
        expected = trust.binding_dict()
        binding = LinkBinding(endpoint_id=endpoint_id, generation=generation, address=(host, port),
                              peer_ca_file=None, peer_ca_data=ca_pem, peer_fingerprint=trust.fingerprint,
                              expected_binding=expected, timeout=float(remaining))
        identity = LinkIdentity(cert_file=str(bundle.leaf_cert_file), key_file=str(bundle.leaf_key_file))
        return make_link_connector(binding, identity, authority)
    return factory


def default_joining_control(config, link) -> PinnedControlChannel:
    """Process-wiring factory for the REAL channel.

    Install with ``configure_joining_control(default_joining_control)`` (see :func:`install_joining_wiring`).  It
    performs no IO: the pinned connector is built per call from THIS config, so nothing leaks between configs, and
    without an owner endpoint / current journal link every call fails closed.
    """
    if config is None:
        return PinnedControlChannel(link)
    return PinnedControlChannel(link, connector_factory=_config_connector_factory(config))


def install_joining_wiring() -> None:
    """Sidecar lifecycle: install the config-scoped joining channel factory (idempotent).

    The process-wide transport hook is deliberately left cleared: the pinned transport is bound to each config by
    :func:`default_joining_control`, so no ambient transport can be shared across configs/profiles.
    """
    configure_joining_control(default_joining_control)
    configure_joining_transport()


def clear_joining_wiring() -> None:
    configure_joining_control()
    configure_joining_transport()


# ---------------------------------------------------------------------------------------------------------------
# The owner
# ---------------------------------------------------------------------------------------------------------------
class _ServiceChannel:
    """Sync bridge the product service calls: adds the STORED lobby id, never a client one."""

    def __init__(self, owner: "JoiningOwner", lobby_id: Optional[str]):
        self._owner, self._lobby_id = owner, lobby_id
        self.closed = False

    def call(self, request: dict) -> dict:
        return self._owner.control(request, lobby_id=self._lobby_id)


class JoiningOwner:
    """One request-scoped joining owner over the real product stores."""

    def __init__(self, config, deadline: Optional[float] = None):
        settings = _joining_settings(config)
        self.config = config
        self.deadline = float(time.monotonic() + REQUEST_BUDGET) if deadline is None else float(deadline)
        self._call_deadline: Optional[float] = None
        self._closed = False
        self._close_lock = threading.Lock()
        self.hosts_path = settings["hosts_path"]
        self.origins = settings["origins"]
        self.journal = Journal(config.pairing.journal_dir)
        try:
            self.bundle = identity_module.load_identity(
                settings["identity_dir"], installation_id=config.installation_id, profile=config.profile)
        except identity_module.IdentityError:
            raise JoiningError("unknown_host") from None
        self.store = JoiningStore(settings["store_dir"])
        try:
            projection = project_hosts(self.journal, self.hosts_path, self.origins)
            self.projection = projection
            self.link = projection.link
            self.registry = projection.registry
            self.peer_id = projection.peer_id
            self.origin = projection.origin
            self.generation = projection.generation
            self.endpoint_id = projection.endpoint_id
            self.host_peer_id = projection.host_peer_id
            self.host_binding = projection.binding
            peer = self.link["peer"]
            self.host_installation_id = peer["installation_id"]
            self.self_binding, self.catalog, self._catalog = self_binding_for(
                projection.state, self.link, self.bundle, config.installation_id, config.profile)
        except BaseException:
            self.store.close()
            raise
        self.key = _IdentityConsentKey(self.bundle)
        with _FACTORY_LOCK:
            self._factory = _CONTROL_FACTORY
        self._wire = None
        self._wire_failed = False

    # -- lifecycle --------------------------------------------------------------------------------------------
    def close(self) -> None:
        """Release the channel and the store deterministically (never twice)."""
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            wire, self._wire = self._wire, None
        if wire is not None:
            closer = getattr(wire, "close", None)
            if callable(closer):
                try:
                    self._run(closer())
                except Exception:
                    pass
        try:
            self.store.close()
        except Exception:
            pass

    def __enter__(self) -> "JoiningOwner":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # -- admission --------------------------------------------------------------------------------------------
    def admit(self) -> bool:
        """R1-05 seam: re-read the journal under its lock, then re-check the deadline.

        Synchronous and side-effect free apart from the journal read lock.  A
        revoked/superseded link, a changed peer or an expired budget all raise
        ``control_unavailable`` (a ``ValueError``) so no effect can be emitted
        after an unpair or after the queue wait.
        """
        try:
            state = self.journal.snapshot()
        except JournalError:
            raise JoiningError("control_unavailable") from None
        budget = self.deadline if self._call_deadline is None else min(self.deadline, self._call_deadline)
        if time.monotonic() >= budget:
            raise JoiningError("control_unavailable")
        link = state.links.get(self.endpoint_id)
        if (link is None or link.get("protocol_state") not in _ACTIVE_STATES
                or link.get("generation") != self.generation
                or link["peer"]["peer_id"] != self.peer_id):
            raise JoiningError("control_unavailable")
        return True

    # -- control ----------------------------------------------------------------------------------------------
    def control(self, request: Mapping, *, lobby_id: Optional[str] = None) -> dict:
        """One bounded control call: stored lobby id, absolute deadline, admission callback."""
        if not isinstance(lobby_id, str) or not _LOBBY_ID.fullmatch(lobby_id):
            raise JoiningError("control_unavailable")
        wire = self._channel()
        budget = min(self.deadline, time.monotonic() + float(TIMEOUT))
        if time.monotonic() >= budget:
            raise JoiningError("control_unavailable")
        payload = dict(request)
        payload["lobby_id"] = lobby_id
        if payload.get("op") in _WIRE_OP:
            payload["op"] = _WIRE_OP[payload["op"]]
        self._call_deadline = budget
        try:
            response = self._run(wire.call(payload, deadline=budget, admit=self.admit))
        except JoiningError:
            raise
        except Exception:
            raise JoiningError("control_unavailable") from None
        finally:
            self._call_deadline = None
        if not isinstance(response, dict):
            raise JoiningError("control_unavailable")
        return response

    def _channel(self):
        if self._wire is not None:
            return self._wire
        if self._wire_failed or self._factory is None:
            raise JoiningError("control_unavailable")
        try:
            wire = self._factory(self.config, dict(self.link))
        except Exception:
            self._wire_failed = True
            raise JoiningError("control_unavailable") from None
        if wire is None or not callable(getattr(wire, "call", None)):
            self._wire_failed = True
            raise JoiningError("control_unavailable")
        self._wire = wire
        return wire

    @staticmethod
    def _run(result):
        """Drive a channel result from the HTTP worker thread (never a stray loop)."""
        if not inspect.isawaitable(result):
            return result
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(result)
        close = getattr(result, "close", None)
        if callable(close):
            close()
        raise JoiningError("control_unavailable")

    # -- service ----------------------------------------------------------------------------------------------
    def service(self, record: Optional[Mapping] = None):
        lobby_id = record["lobby_id"] if isinstance(record, Mapping) else None
        return JoiningOwnerService(self.store, self.registry, host_peer_id=self.host_peer_id,
                                   origin=self.origin, consent_key=self.key,
                                   channel=_ServiceChannel(self, lobby_id), self_binding=self.self_binding)

    # -- actions ----------------------------------------------------------------------------------------------
    def execute(self, action: str, import_id: Optional[str] = None, body: Optional[Mapping] = None) -> dict:
        payload = body if isinstance(body, Mapping) else {}
        if action == "preview":
            return self._preview(payload)
        handler = _ACTIONS.get(action)
        if handler is None:
            # Unknown action is not a route: identical to an unknown import.
            raise JoiningError("invalid_import_id")
        return handler(self, self.record(import_id), payload)

    def record(self, import_id: str) -> dict:
        """The import, or 404 -- never another host's and never another generation's."""
        record = self.store.get_import(import_id)
        if record["host_peer_id"] != self.host_peer_id:
            raise JoiningError("peer_binding_mismatch")
        return record

    def _preview(self, body: Mapping) -> dict:
        code = body.get("code")
        if not isinstance(code, str):
            raise JoiningError("invalid_invitation")
        host_id = body.get("host_id")
        if host_id is not None and host_id != self.peer_id:
            raise JoiningError("unknown_host")
        result = self.service().preview(code)
        return self.view(self.record(result["import_id"]))

    def _metadata(self, record: Mapping, body: Mapping) -> dict:
        if record["state"] != "previewed":
            raise JoiningError("import_state_conflict")
        result = self.service(record).refresh_metadata(record["import_id"])
        return self.view(record, status="metadata", metadata={
            "name": result["name"], "expires_at": _rfc3339(result["expires_at"])})

    def _redeem(self, record: Mapping, body: Mapping) -> dict:
        try:
            result = self.service(record).redeem(record["import_id"])
        except JoiningError as exc:
            if str(exc) == "control_unavailable" and _state_of(self.store, record["import_id"]) == "redeem_ambiguous":
                # The secret may have been sent: report the ambiguity, never a retryable failure.
                raise JoiningError("redeem_ambiguous") from None
            raise
        remote_status = result.get("status")
        return self.view(record, remote=(remote_status if remote_status in _REMOTE_STATUS else None, None))

    def _scope(self, record: Mapping, body: Mapping) -> dict:
        import_id = record["import_id"]
        room_id, member_id = body.get("room_id"), body.get("member_id")
        if not isinstance(room_id, str) or not isinstance(member_id, str):
            raise JoiningError("invalid_owner_scope")
        if record["state"] != "redeemed":
            raise JoiningError("import_state_conflict")
        remote_status, scope = self._remote(record)
        if scope is None or remote_status not in ("consent_required", "ready"):
            raise JoiningError("host_approval_required")
        if room_id != scope["room_id"] or member_id != scope["member_id"]:
            # The owner must confirm the EXACT host-approved scope, never a guess.
            raise JoiningError("consent_scope_conflict")
        self.admit()  # re-check admission AFTER the status round-trip, before the local decision
        # Always through the equality-enforcing service/store: idempotent for the exact same scope,
        # consent_scope_conflict if the host now presents a different one.
        self.service(record).confirm_scope(import_id, room_id=scope["room_id"],
                                           member_id=scope["member_id"])
        return self.view(self.record(import_id), status="consent", scope=scope)

    def _consent(self, record: Mapping, body: Mapping) -> dict:
        approve = body.get("approve")
        if type(approve) is not bool:
            raise JoiningError("invalid_decision")
        result = self.service(record).consent(record["import_id"], approve=approve)
        status = "rejected" if result["status"] == "refused" else "activating"
        return self.view(record, status=status)

    def _resolve(self, record: Mapping, body: Mapping) -> dict:
        """Never guesses: this surface carries no authenticated redemption evidence."""
        if record["state"] not in ("redeeming", "redeem_ambiguous"):
            raise JoiningError("import_state_conflict")
        raise JoiningError("manual_reconciliation_required")

    def _get(self, record: Mapping, body: Mapping) -> dict:
        """Read model only: no consumption, and the host is only asked once redeemed."""
        if record["state"] in _REMOTE_LOCAL_STATES:
            return self.view(record, remote=self._remote(record))
        return self.view(record)

    # -- read model -------------------------------------------------------------------------------------------
    def view(self, record: Mapping, *, status: Optional[str] = None,
             metadata: Optional[Mapping] = None, scope: Optional[Mapping] = None,
             remote: Optional[tuple] = None) -> dict:
        import_id = record["import_id"]
        state = _state_of(self.store, import_id) or record["state"]
        confirmed = self.store.confirmed_scope(import_id)
        document: dict[str, Any] = {
            "id": import_id,
            "import_id": import_id,
            "local_state": state,
            "host": self.host_view(),
        }
        if confirmed is not None:
            document["scope_confirmed"] = True
        if metadata is not None:
            document["metadata"] = dict(metadata)
        if status is None:
            status, scope = self._project(record, state, confirmed, remote)
        document["status"] = status
        if scope is not None:
            document["scope"] = self.scope_view(scope)
        elif status in ("rejected", "expired", "cancelled"):
            document["host_decision"] = status
        if status == "active" and isinstance(confirmed, Mapping):
            document["room_id"] = confirmed["room_id"]
        return document

    def _project(self, record: Mapping, state: str, confirmed: Optional[Mapping],
                 remote: Optional[tuple]) -> tuple:
        if state == "previewed":
            return "preview", None
        if state in ("redeeming", "redeem_ambiguous"):
            return "redeem_ambiguous", None
        if state == "refused":
            return "rejected", None
        if state == "left":
            return "left", None
        if state not in _REMOTE_LOCAL_STATES:
            raise JoiningError("import_state_conflict")
        remote_status, scope = remote if remote is not None else self._remote(record)
        if remote_status is None:
            # Post-effect view: the effect already happened, so no fresh round-trip.
            if state == "consent_given":
                return "activating", None
            if state == "consent_pending_delivery":
                return "consent_pending_delivery", None
            return ("consent" if confirmed is not None else "awaiting_host"), None
        if remote_status == "awaiting_host_approval":
            return "awaiting_host", None
        if remote_status in ("denied", "refused"):
            return "rejected", None
        if remote_status in ("expired", "cancelled"):
            return remote_status, None
        if remote_status == "active":
            if state == "consent_given" and confirmed is not None:
                return "active", None
            if state in ("consent_given", "consent_pending_delivery"):
                # 'ready'/'active' before consent is NEVER activation evidence.
                return "activating", None
            return ("consent" if confirmed is not None else "awaiting_host"), None
        if remote_status in ("consent_required", "ready"):
            if state == "consent_given":
                return "activating", None
            if state == "consent_pending_delivery":
                return "consent_pending_delivery", None
            if confirmed is not None:
                return "consent", None
            return "scope", scope
        raise JoiningError("control_unavailable")

    def _remote(self, record: Mapping) -> tuple:
        """One authenticated, non-consuming host status read + exact scope validation."""
        response = self.control({"op": "status", "invite_id": record["invite_id"]},
                                lobby_id=record["lobby_id"])
        if set(response) != {"status", "scope"}:
            raise JoiningError("control_unavailable")
        status = response["status"]
        if not isinstance(status, str) or status not in _REMOTE_STATUS:
            raise JoiningError("control_unavailable")
        scope = response["scope"]
        if scope is not None:
            scope = self.validated_scope(record, scope)
        return status, scope

    def validated_scope(self, record: Mapping, scope: Any) -> dict:
        if (not isinstance(scope, Mapping) or set(scope) != _SCOPE_FIELDS
                or any(not isinstance(scope[key], str) for key in _SCOPE_FIELDS - {"binding"})
                or not isinstance(scope["binding"], Mapping)):
            raise JoiningError("control_unavailable")
        try:
            binding = validate_peer_binding(dict(scope["binding"]))
        except PeerBindingError:
            raise JoiningError("control_unavailable") from None
        if (binding != dict(self.self_binding) or scope["lobby_id"] != record["lobby_id"]
                or scope["invite_id"] != record["invite_id"]
                or scope["host_id"] != self.host_installation_id
                or not _ROOM_ID.fullmatch(scope["room_id"]) or not _TOKEN.fullmatch(scope["member_id"])):
            raise JoiningError("consent_scope_conflict")
        return {"room_id": scope["room_id"], "member_id": scope["member_id"], "lobby_id": scope["lobby_id"],
                "invite_id": scope["invite_id"], "host_id": scope["host_id"], "binding": binding}

    def scope_view(self, scope: Mapping) -> dict:
        return {
            "room_id": scope["room_id"], "member_id": scope["member_id"], "lobby_id": scope["lobby_id"],
            "invite_id": scope["invite_id"], "host_id": scope["host_id"], "binding": dict(scope["binding"]),
            "catalog": copy.deepcopy(self.catalog), "policy_digest": self.self_binding["policy_digest"],
            # Rights the LOCAL pinned catalog actually grants; nothing invented.
            "permissions": sorted(self._catalog.execution_policy.enabled_toolsets),
        }

    def host_view(self) -> dict:
        binding = self.host_binding
        return {"peer_id": self.peer_id, "installation_id": binding["target_install_id"],
                "profile": binding["target_profile"], "fingerprint": binding["fingerprint"],
                "catalog_digest": binding["catalog_digest"], "policy_digest": binding["policy_digest"]}


_ACTIONS = {
    "get": JoiningOwner._get,
    "metadata": JoiningOwner._metadata,
    "redeem": JoiningOwner._redeem,
    "scope": JoiningOwner._scope,
    "consent": JoiningOwner._consent,
    "resolve": JoiningOwner._resolve,
}


# ---------------------------------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------------------------------
def _rfc3339(epoch) -> str:
    return datetime.fromtimestamp(float(epoch), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _state_of(store: JoiningStore, import_id: str) -> Optional[str]:
    try:
        return store.get_import(import_id)["state"]
    except (JoiningStoreError, ValueError):
        return None


def _joining_settings(config) -> dict:
    """Validate the ``joining`` block: imports only, never a partial feature."""
    raw = getattr(config, "joining_owner", None)
    pairing = getattr(config, "pairing", None)
    if pairing is None or not isinstance(raw, Mapping):
        raise JoiningError("unknown_host")

    def _path(key):
        value = raw.get(key)
        if not isinstance(value, str) or not value or "\x00" in value or not os.path.isabs(value):
            raise JoiningError("unknown_host")
        return Path(os.path.abspath(value))

    store_dir = _path("store_dir")
    identity_dir = _path("identity_dir")
    hosts_path = _path("hosts_path")
    origins = raw.get("origins")
    if not isinstance(origins, Mapping) or not origins:
        raise JoiningError("unknown_host")
    if {key: value for key, value in origins.items() if not isinstance(key, str) or not isinstance(value, str)}:
        raise JoiningError("unknown_host")
    if identity_dir != Path(os.path.abspath(pairing.identity_dir)):
        # A joining identity that is not the paired signing identity is a re-key attempt.
        raise JoiningError("unknown_host")
    if hosts_path == Path(os.path.abspath(pairing.peers_path)) or hosts_path == Path(os.path.abspath(pairing.journal_dir)):
        raise JoiningError("unknown_host")
    return {"store_dir": store_dir, "identity_dir": identity_dir, "hosts_path": hosts_path,
            "origins": dict(origins), "control_endpoints": raw.get("control_endpoints")}


def load_owner(config, *, deadline: Optional[float] = None) -> Optional[JoiningOwner]:
    """Build the request-scoped owner, or ``None`` (= the route does not exist).

    Every failure mode -- absent/misconfigured ``joining`` block, missing
    pairing, unusable identity, unreadable journal, no/too many approved hosts,
    tampered projection, unopenable store -- is deliberately indistinguishable
    from "feature not present".  Nothing is ever repaired or re-initialized.
    """
    if config is None or getattr(config, "joining_owner", None) is None:
        return None
    try:
        return JoiningOwner(config, deadline)
    except (JoiningError, JoiningStoreError, JournalError, InvitationError, PeerBindingError,
            PeerRegistryError, identity_module.IdentityError, private_fs.PrivateStorageError,
            ControlError, OSError, sqlite3.Error, ValueError, TypeError):
        return None
