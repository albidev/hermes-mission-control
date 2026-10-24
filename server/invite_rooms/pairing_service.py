"""P3b.1a-2: pairing service (design section 1) over the journal and the P3b.1a-1 codec/identity.

Roles: A = ``host`` (emits the offer, pins B in ``peers.json``), B = ``joining`` (imports the offer, emits the accept).
Every refusal is a generic machine code (``PairingError``); no code, SAS, key or path is ever put in an error.
The grant never appears in a pairing code: codes carry public identity only.
"""
from __future__ import annotations

import base64
import copy
import hmac
import json
import logging
import re
import secrets
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from invite_rooms import identity as ident
from invite_rooms import pairing_codec as pc
from invite_rooms.consent import canonical
from invite_rooms.pairing_journal import (E_BUSY, Journal, JournalError, JournalLinkAuthority, Projector, State, Txn,
                                          binding_for)
from invite_rooms.peer_registry import PeerRegistry, PeerRegistryError, _native_catalog

_LOG = logging.getLogger("invite_rooms.pairing")

PAIR_TTL = pc.MAX_TTL_SECONDS  # 15 minutes: the whole three-code exchange lives inside one window
SAS_RE = re.compile(r"[0-9]{6}")
ACK_PURPOSE = b"mc-pair-ack-v1\x00"

INVALID = "pairing_code_invalid"
UNAVAILABLE = "pairing_unavailable"
SAS_MISMATCH = "pairing_sas_mismatch"
SAS_INVALID = "pairing_sas_invalid"
PENDING = "pairing_pending_exists"
COOLDOWN = "pairing_cooldown"
NATIVE = "pairing_native_unavailable"
IDENTITY = "pairing_identity_unavailable"
BUSY = "pairing_busy"

UNPAIR_NOTICE = ("Unpairing closes this link and bumps its generation, so no new operation is admitted on it. "
                 "It does NOT revoke native grants that were already minted, does NOT stop runs that were already "
                 "admitted, and the other side may keep a stale record of this pairing.")


class PairingError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class AckRequest:
    endpoint_id: str
    generation: int
    request: dict


def _peer_id(installation_id: str) -> str:
    return "peer-" + installation_id.split(":", 1)[1]


def _codec(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except pc.PairingCodecError as exc:
        raise PairingError(UNAVAILABLE if exc.code == pc.UNAVAILABLE else INVALID) from None


def _same(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("ascii", "replace"), b.encode("ascii", "replace"))


class PairingService:
    """One instance per request is fine: all state lives in the journal.

    ``config`` needs ``identity_dir``, ``journal_dir`` and ``peers_path``.  ``catalog_source`` returns this
    installation's catalog (``groups.capabilities`` -> ``room_link.catalog``); it is called OUTSIDE any lock.
    """

    def __init__(self, config, *, installation_id: str, profile: str, catalog_source: Callable[[], Mapping],
                 clock: Callable[[], float] = time.time, token_bytes: Callable[[int], bytes] = secrets.token_bytes,
                 anchor=None, journal: Optional[Journal] = None):
        self.config = config
        self.installation_id, self.profile = installation_id, profile
        self._catalog_source = catalog_source
        self._clock, self._token = clock, token_bytes
        self._anchor = anchor
        self.journal = journal or Journal(config.journal_dir, anchor=anchor, clock=clock)
        self.projector = Projector(config.peers_path)
        self.authority = JournalLinkAuthority(self.journal)

    # ---- helpers --------------------------------------------------------------------------------------------
    def _now(self) -> int:
        return int(self._clock())

    def _bundle(self, *, create: bool):
        try:
            fn = ident.initialize_identity if create else ident.load_identity
            return fn(self.config.identity_dir, installation_id=self.installation_id, profile=self.profile,
                      now=self._clock(), anchor=self._anchor)
        except ident.IdentityError:
            raise PairingError(IDENTITY) from None

    def _own_catalog(self) -> dict:
        try:
            catalog = json.loads(json.dumps(dict(self._catalog_source())))
            self._validate_catalog(catalog, self.installation_id, self.profile)
        except PairingError:
            raise PairingError(NATIVE) from None
        except Exception:
            raise PairingError(NATIVE) from None
        return catalog

    @staticmethod
    def _validate_catalog(catalog: Mapping, installation_id: str, profile: str):
        try:
            validated = _native_catalog(copy.deepcopy(dict(catalog)))
        except PeerRegistryError:
            raise PairingError(INVALID) from None
        if validated.installation_id != installation_id or validated.execution_policy.target_profile != profile:
            raise PairingError(INVALID)
        return validated

    def _peer_from_identity(self, who: pc.PeerIdentity) -> dict:
        """Core-validate a self-asserted remote identity exactly as the registry reader will (dry registration)."""
        if who.installation_id == self.installation_id:
            raise PairingError(INVALID)  # self-pairing
        catalog = json.loads(json.dumps(dict(who.catalog)))
        validated = self._validate_catalog(catalog, who.installation_id, who.profile)
        peer = {"peer_id": _peer_id(who.installation_id), "installation_id": who.installation_id,
                "profile": who.profile, "fingerprint": who.leaf_fingerprint, "ca_pem": who.ca_pem,
                "consent_public_key_b64": base64.b64encode(who.consent_public_key).decode("ascii"),
                "catalog": catalog, "catalog_digest": validated.catalog_digest,
                "policy_digest": validated.execution_policy.policy_digest}
        try:
            PeerRegistry().register(peer_id=peer["peer_id"], installation_id=who.installation_id, profile=who.profile,
                                    fingerprint=who.leaf_fingerprint, endpoint_id="link-" + "0" * 32, catalog=catalog,
                                    consent_public_key=bytes(who.consent_public_key), tls_ca_pem=who.ca_pem,
                                    pairing_generation=1)
        except PeerRegistryError:
            raise PairingError(INVALID) from None
        return peer

    @staticmethod
    def _peer_view(peer: Mapping) -> dict:
        return {k: peer[k] for k in ("peer_id", "installation_id", "profile", "fingerprint", "policy_digest",
                                     "catalog_digest")}

    def _row(self, state: State, sess: Mapping, now: int) -> dict:
        shown = sess["state"]
        link = state.links.get(sess["endpoint_id"]) if sess["endpoint_id"] else None
        if link is not None and link["protocol_state"] == "revoked":
            shown = "unpaired"
        elif shown in ("offered", "accept_imported", "revealed", "offer_imported", "accepted",
                       "reveal_imported") and sess["expires_at"] < now:
            shown = "expired"
        return {"id": sess["id"], "role": sess["role"], "state": shown, "expires_at": sess["expires_at"],
                "peer": self._peer_view(sess["peer"]) if sess["peer"] else None}

    def _txn(self):
        return _Guard(self.journal.transaction())

    def _publish(self, txn: Txn) -> None:
        """Best effort after a durable decision: a failure leaves ``publish_pending`` for the next reconcile."""
        try:
            self.projector.reconcile(txn)
        except JournalError as exc:
            _LOG.warning("pairing projection deferred: %s", exc.code)

    def _admit_new(self, txn: Txn, now: int) -> None:
        if txn.state.cooldown_active(now):
            raise PairingError(COOLDOWN)
        if txn.state.pending_sessions(now):
            raise PairingError(PENDING)

    def _live(self, txn: Txn, session_id: Any, role: str, now: int, *states: str) -> dict:
        sess = txn.state.sessions.get(session_id) if isinstance(session_id, str) else None
        if sess is None or sess["role"] != role or sess["state"] not in states or sess["expires_at"] < now:
            raise PairingError(UNAVAILABLE)
        return sess

    def _abort(self, txn: Txn, sess: Mapping, reason: str) -> None:
        txn.append("aborted", {"id": sess["id"], "reason": reason})

    # ---- A: host --------------------------------------------------------------------------------------------
    def start_offer(self) -> dict:
        catalog = self._own_catalog()
        bundle = self._bundle(create=True)
        now = self._now()
        nonce, r_a = self._token(32), self._token(32)
        expires = now + PAIR_TTL
        offer = _codec(pc.build_offer, bundle.consent_key(), bundle.peer_identity(catalog), nonce=nonce, r_a=r_a,
                       expires_at=expires, now=now)
        sid = "pairing-" + self._token(16).hex()
        with self._txn() as txn:
            self._admit_new(txn, now)
            txn.append("offer_created", {"id": sid, "expires_at": expires, "nonce": nonce.hex(), "r_a": r_a.hex(),
                                          "offer": offer.wire, "offer_hash": offer.digest_hex})
        return {"id": sid, "code": offer.wire, "expires_at": expires}

    def accept_offer(self, session_id: str, code: str) -> dict:
        bundle = self._bundle(create=False)
        now = self._now()
        abort_conflict = False
        with self._txn() as txn:
            sess = self._live(txn, session_id, "host", now, "offered", "accept_imported", "revealed")
            offer = pc.parse_offer(sess["offer"], now=None)
            accept = _codec(pc.parse_accept, code, offer=offer, now=now)
            if sess["state"] != "offered" and sess["accept_hash"] != accept.digest_hex:
                self._abort(txn, sess, "conflict")  # a second, different accept for this offer kills the attempt
                abort_conflict = True
            elif sess["state"] == "offered":
                if accept.digest_hex in txn.state.accept_hashes:
                    raise PairingError(UNAVAILABLE)
                peer = self._peer_from_identity(accept.identity)
                txn.append("accept_imported", {"id": sess["id"], "accept": accept.wire,
                                               "accept_hash": accept.digest_hex, "peer": peer,
                                               "endpoint_id": pc.endpoint_id_for_session(offer, accept)})
            if not abort_conflict:
                if sess["state"] == "accept_imported":
                    reveal = _codec(pc.build_reveal, bundle.consent_key(), offer, accept,
                                    r_a=bytes.fromhex(sess["r_a"]), expires_at=accept.expires_at, now=now)
                    txn.append("revealed", {"id": sess["id"], "reveal": reveal.wire})
                reveal = pc.parse_reveal(sess["reveal"], offer=offer, accept=accept, now=None)
                sas = pc.sas_for_session(offer, accept, bytes.fromhex(sess["r_a"]))
                return {"id": sess["id"], "code": reveal.wire, "sas": sas, "expires_at": reveal.expires_at}
        raise PairingError(UNAVAILABLE)

    def _confirm(self, session_id: str, sas: Any, role: str, state_name: str) -> dict:
        if not isinstance(sas, str) or not SAS_RE.fullmatch(sas):
            raise PairingError(SAS_INVALID)
        now = self._now()
        mismatch = False
        with self._txn() as txn:
            sess = txn.state.sessions.get(session_id) if isinstance(session_id, str) else None
            if sess is None or sess["role"] != role:
                raise PairingError(UNAVAILABLE)
            offer = pc.parse_offer(sess["offer"], now=None)
            if sess["state"] in ("confirmed", "acknowledged"):
                expected = self._expected_sas(sess, offer)
                if not _same(sas, expected):
                    raise PairingError(UNAVAILABLE)
                return self._row(txn.state, sess, now)  # identical retry of a durable decision
            if sess["state"] != state_name or sess["expires_at"] < now:
                raise PairingError(UNAVAILABLE)
            if not _same(sas, self._expected_sas(sess, offer)):
                self._abort(txn, sess, "sas_mismatch")
                mismatch = True
            else:
                txn.append("confirmed", {"id": sess["id"],
                                         "generation": txn.state.peer_gen.get(sess["peer"]["peer_id"], 0) + 1})
                if role == "host":
                    self._publish(txn)
                return self._row(txn.state, txn.state.sessions[sess["id"]], now)
        if mismatch:
            raise PairingError(SAS_MISMATCH)
        raise PairingError(UNAVAILABLE)

    @staticmethod
    def _expected_sas(sess: Mapping, offer: pc.Offer) -> str:
        accept = pc.parse_accept(sess["accept"], offer=offer, now=None)
        if sess["role"] == "host":
            r_a = bytes.fromhex(sess["r_a"])
        else:
            r_a = pc.parse_reveal(sess["reveal"], offer=offer, accept=accept, now=None).r_a
        return pc.sas_for_session(offer, accept, r_a)

    def confirm_offer(self, session_id: str, sas: Any) -> dict:
        return self._confirm(session_id, sas, "host", "revealed")

    def cancel_offer(self, session_id: str) -> None:
        with self._txn() as txn:
            sess = txn.state.sessions.get(session_id) if isinstance(session_id, str) else None
            if sess is None or sess["role"] != "host" or sess["state"] in ("confirmed", "acknowledged"):
                raise PairingError(UNAVAILABLE)
            if sess["state"] != "aborted":
                self._abort(txn, sess, "cancelled")

    # ---- B: joining -----------------------------------------------------------------------------------------
    def import_offer(self, code: str) -> dict:
        catalog = self._own_catalog()
        bundle = self._bundle(create=True)
        now = self._now()
        offer = _codec(pc.parse_offer, code, now=now)
        peer = self._peer_from_identity(offer.identity)
        r_b = self._token(32)
        sid = "pairing-" + self._token(16).hex()
        with self._txn() as txn:
            known = next((s for s in txn.state.sessions.values() if s["offer_hash"] == offer.digest_hex), None)
            if known is not None:
                if known["role"] != "joining" or known["state"] not in ("offer_imported", "accepted"):
                    raise PairingError(UNAVAILABLE)  # consumed offers are tombstoned
                sess = known
            else:
                self._admit_new(txn, now)
                txn.append("offer_imported", {"id": sid, "expires_at": offer.expires_at, "offer": offer.wire,
                                              "offer_hash": offer.digest_hex, "peer": peer})
                sess = txn.state.sessions[sid]
            if sess["state"] == "offer_imported":
                accept = _codec(pc.build_accept, bundle.consent_key(), bundle.peer_identity(catalog), offer,
                                r_b=r_b, expires_at=offer.expires_at, now=now)
                txn.append("accepted", {"id": sess["id"], "accept": accept.wire, "accept_hash": accept.digest_hex,
                                        "r_b": r_b.hex(), "endpoint_id": pc.endpoint_id_for_session(offer, accept)})
            accept = pc.parse_accept(sess["accept"], offer=offer, now=None)
            return {"id": sess["id"], "code": accept.wire, "identity": self._peer_view(peer),
                    "expires_at": sess["expires_at"]}

    def reveal_import(self, session_id: str, code: str) -> dict:
        now = self._now()
        abort_conflict = False
        with self._txn() as txn:
            sess = self._live(txn, session_id, "joining", now, "accepted", "reveal_imported")
            offer = pc.parse_offer(sess["offer"], now=None)
            accept = pc.parse_accept(sess["accept"], offer=offer, now=None)
            reveal = _codec(pc.parse_reveal, code, offer=offer, accept=accept, now=now)
            if sess["state"] == "reveal_imported" and sess["reveal"] != reveal.wire:
                self._abort(txn, sess, "conflict")
                abort_conflict = True
            else:
                if sess["state"] == "accepted":
                    txn.append("reveal_imported", {"id": sess["id"], "reveal": reveal.wire})
                return {"id": sess["id"], "sas": pc.sas_for_session(offer, accept, reveal.r_a),
                        "expires_at": sess["expires_at"]}
        if abort_conflict:
            raise PairingError(UNAVAILABLE)
        raise PairingError(UNAVAILABLE)

    def confirm_import(self, session_id: str, sas: Any) -> dict:
        return self._confirm(session_id, sas, "joining", "reveal_imported")

    def cancel_import(self, session_id: str) -> None:
        """Owner abandons a pending joining attempt (service-level; the design's route table has no route for it)."""
        with self._txn() as txn:
            sess = txn.state.sessions.get(session_id) if isinstance(session_id, str) else None
            if sess is None or sess["role"] != "joining" or sess["state"] in ("confirmed", "acknowledged"):
                raise PairingError(UNAVAILABLE)
            if sess["state"] != "aborted":
                self._abort(txn, sess, "cancelled")

    # ---- read model -----------------------------------------------------------------------------------------
    def reconcile(self) -> None:
        """Re-open path: finish a pending publication or fail closed on a tampered projection."""
        with self._txn() as txn:
            self.projector.reconcile(txn)

    def list_pairings(self, limit: int = 50) -> list[dict]:
        self.reconcile()
        state, now = self.journal.snapshot(), self._now()
        return [self._row(state, state.sessions[sid], now) for sid in reversed(state.order[-limit:])]

    def list_peers(self) -> list[dict]:
        self.reconcile()
        state = self.journal.snapshot()
        return [{**self._peer_view(l["peer"]), "role": l["role"], "state": l["protocol_state"],
                 "generation": l["generation"]} for l in state.active_links()]

    # ---- unpair ---------------------------------------------------------------------------------------------
    def unpair(self, peer_id: str) -> dict:
        with self._txn() as txn:
            link = txn.state.active_link_for_peer(peer_id) if isinstance(peer_id, str) else None
            if link is None:
                raise PairingError(UNAVAILABLE)
            generation = txn.state.peer_gen.get(peer_id, 0) + 1
            txn.append("unpaired", {"peer_id": peer_id, "endpoint_id": link["endpoint_id"], "generation": generation})
            if link["role"] == "host":
                self._publish(txn)
        return {"peer_id": peer_id, "generation": generation, "notice": UNPAIR_NOTICE}

    # ---- activation: pair_ack (R1-02) -----------------------------------------------------------------------
    @staticmethod
    def _ack_message(role: str, offer_hash: str, accept_hash: str, endpoint_id: str) -> bytes:
        return ACK_PURPOSE + canonical({"role": role, "offer_hash": offer_hash, "accept_hash": accept_hash,
                                        "endpoint_id": endpoint_id})

    def _make_ack(self, bundle, sess: Mapping, role: str) -> dict:
        body = {"role": role, "offer_hash": sess["offer_hash"], "accept_hash": sess["accept_hash"],
                "endpoint_id": sess["endpoint_id"]}
        sig = bundle.consent_key().sign(self._ack_message(**body))
        return {"v": 1, **body, "signature": pc.b64e(sig)}

    def _verify_ack(self, ack: Any, sess: Mapping, role: str) -> None:
        keys = {"v", "role", "offer_hash", "accept_hash", "endpoint_id", "signature"}
        if (not isinstance(ack, dict) or set(ack) != keys or ack["v"] != 1 or ack["role"] != role
                or not all(isinstance(ack[k], str) for k in keys - {"v"})
                or ack["offer_hash"] != sess["offer_hash"] or ack["accept_hash"] != sess["accept_hash"]
                or ack["endpoint_id"] != sess["endpoint_id"]):
            raise PairingError(UNAVAILABLE)
        try:
            sig = base64.urlsafe_b64decode(ack["signature"] + "=" * (-len(ack["signature"]) % 4))
            key = Ed25519PublicKey.from_public_bytes(base64.b64decode(sess["peer"]["consent_public_key_b64"]))
            key.verify(sig, self._ack_message(role, ack["offer_hash"], ack["accept_hash"], ack["endpoint_id"]))
        except (InvalidSignature, ValueError):
            raise PairingError(UNAVAILABLE) from None
        if pc.b64e(sig) != ack["signature"]:
            raise PairingError(UNAVAILABLE)

    def ack_request(self, session_id: str) -> AckRequest:
        """A: the signed ``pair_ack`` for the control link, journaled once; a repeat returns the same bytes."""
        bundle = self._bundle(create=False)
        with self._txn() as txn:
            sess = txn.state.sessions.get(session_id) if isinstance(session_id, str) else None
            if sess is None or sess["role"] != "host" or sess["state"] not in ("confirmed", "acknowledged"):
                raise PairingError(UNAVAILABLE)
            link = txn.state.links[sess["endpoint_id"]]
            if link["protocol_state"] == "revoked":
                raise PairingError(UNAVAILABLE)
            if sess["ack_sent"] is None:
                txn.append("ack_sent", {"id": sess["id"], "ack": self._make_ack(bundle, sess, "host")})
            return AckRequest(sess["endpoint_id"], txn.state.link_generation(link),
                              {"op": "pair_ack", "ack": copy.deepcopy(txn.state.sessions[sess["id"]]["ack_sent"])})

    def handle_pair_ack(self, request: Any) -> dict:
        """B: control handler for ``pair_ack`` (dispatcher admits it on a ``confirmed`` link only)."""
        if not isinstance(request, dict) or set(request) != {"op", "ack"} or not isinstance(request["ack"], dict):
            raise PairingError(UNAVAILABLE)
        ack = request["ack"]
        endpoint = ack.get("endpoint_id")
        bundle = self._bundle(create=False)
        with self._txn() as txn:
            link = txn.state.links.get(endpoint) if isinstance(endpoint, str) else None
            sess = txn.state.sessions.get(link["session_id"]) if link is not None else None
            if (sess is None or sess["role"] != "joining" or link["protocol_state"] not in ("confirmed", "acknowledged")
                    or sess["state"] not in ("confirmed", "acknowledged")):
                raise PairingError(UNAVAILABLE)
            self._verify_ack(ack, sess, "host")
            if sess["state"] == "acknowledged":
                if sess["ack_recv"] != ack:
                    raise PairingError(UNAVAILABLE)  # a variant is a conflict
                return copy.deepcopy(sess["ack_sent"])
            reply = self._make_ack(bundle, sess, "joining")
            txn.append("acknowledged", {"id": sess["id"], "ack_recv": ack, "ack_sent": reply})
            return reply

    def complete_ack(self, session_id: str, reply: Any) -> dict:
        """A: B's answer arrived -> ``acknowledged``."""
        with self._txn() as txn:
            sess = txn.state.sessions.get(session_id) if isinstance(session_id, str) else None
            if (sess is None or sess["role"] != "host" or sess["state"] not in ("confirmed", "acknowledged")
                    or sess["ack_sent"] is None):
                raise PairingError(UNAVAILABLE)
            link = txn.state.links[sess["endpoint_id"]]
            if link["protocol_state"] == "revoked":
                raise PairingError(UNAVAILABLE)
            self._verify_ack(reply, sess, "joining")
            if sess["state"] == "acknowledged":
                if sess["ack_recv"] != reply:
                    raise PairingError(UNAVAILABLE)
            else:
                txn.append("acknowledged", {"id": sess["id"], "ack_recv": reply, "ack_sent": None})
            return self._row(txn.state, txn.state.sessions[sess["id"]], self._now())

    def send_pair_ack(self, session_id: str, send: Callable[[str, int, dict], Any]) -> dict:
        req = self.ack_request(session_id)
        return self.complete_ack(session_id, send(req.endpoint_id, req.generation, req.request))


class _Guard:
    """Maps lock contention to a generic code; every other journal fault is propagated (fail closed)."""

    def __init__(self, cm):
        self._cm = cm

    def __enter__(self) -> Txn:
        try:
            return self._cm.__enter__()
        except JournalError as exc:
            if exc.code == E_BUSY:
                raise PairingError(BUSY) from None
            raise

    def __exit__(self, *exc):
        return self._cm.__exit__(*exc)
