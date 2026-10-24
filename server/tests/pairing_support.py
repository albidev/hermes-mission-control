"""Shared fixtures for the P3b.1a-2 pairing tests (not a test module: discovery ignores it)."""
from __future__ import annotations

import asyncio
import hashlib
import itertools
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402

from gateway.hosted_room_peer import catalog_mapping  # noqa: E402
from invite_rooms import identity as ident  # noqa: E402
from invite_rooms.control.dispatcher import ControlDispatcher  # noqa: E402
from invite_rooms.pairing_service import PairingService  # noqa: E402

INSTALL_A = "install:" + "a" * 32
INSTALL_B = "install:" + "b" * 32
INSTALL_C = "install:" + "c" * 32
PROFILE = "default"
_POLICY_UNSIGNED = {"version": 1, "target_profile": PROFILE, "enabled_toolsets": ["bot_room"], "approval_mode": "manual",
                    "max_iterations": 10}
_POLICY = {**_POLICY_UNSIGNED, "policy_digest": hashlib.sha256(
    json.dumps(_POLICY_UNSIGNED, sort_keys=True, separators=(",", ":")).encode("ascii")).hexdigest()}
_RSA_POOL = [rsa.generate_private_key(public_exponent=65537, key_size=2048) for _ in range(3)]


def make_catalog(installation_id: str, *, toolsets=("bot_room",)) -> dict:
    policy = dict(_POLICY_UNSIGNED, enabled_toolsets=sorted(toolsets))
    policy["policy_digest"] = hashlib.sha256(json.dumps(
        {k: v for k, v in policy.items() if k != "policy_digest"}, sort_keys=True,
        separators=(",", ":")).encode("ascii")).hexdigest()
    return catalog_mapping(installation_id=installation_id, protocol_versions=(2,), link_modes=("direct",),
                           persistent_process=True, text=True, attachments=False,
                           endpoint={"available": True, "url": "http://127.0.0.1:19001/links/unit-link",
                                     "transport_security": "loopback"},
                           target_profile=PROFILE, execution_policy=policy)


class Clock:
    def __init__(self, start: float = 1_800_000_000.0):
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def fast_rsa():
    """Patch identity RSA generation with a pooled key (identity correctness is covered by its own suite)."""
    keys = itertools.cycle(_RSA_POOL)
    return patch.object(ident, "_new_rsa_key", side_effect=lambda: next(keys))


def private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


class Side:
    """One installation: its own identity_dir, journal_dir and peers.json (all owner-private)."""

    def __init__(self, root: Path, name: str, installation_id: str, *, template: Path | None = None,
                 catalog: dict | None = None):
        self.name, self.installation_id = name, installation_id
        self.root = private_dir(root / name)
        self.identity_dir = self.root / "identity"
        self.journal_dir = self.root / "journal"
        cfg = private_dir(self.root / "cfg")
        self.peers_path = cfg / "peers.json"
        fd = os.open(self.peers_path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        os.write(fd, b"[]")
        os.close(fd)
        if template is not None:
            shutil.copytree(template, self.identity_dir, copy_function=shutil.copy2)
        self.config = SimpleNamespace(identity_dir=self.identity_dir, journal_dir=self.journal_dir,
                                      peers_path=self.peers_path)
        self.catalog = catalog or make_catalog(installation_id)

    def service(self, clock=None, **kw) -> PairingService:
        return PairingService(self.config, installation_id=self.installation_id, profile=PROFILE,
                              catalog_source=lambda: self.catalog, clock=clock or __import__("time").time, **kw)

    def peers(self):
        return json.loads(self.peers_path.read_text())

    def journal_bytes(self) -> bytes:
        return (self.journal_dir / "pairing.journal").read_bytes()


def identity_template(installation_id: str) -> Path:
    """An initialized identity directory built once and copied per test."""
    root = Path(tempfile.mkdtemp(dir=os.environ.get("MC_TEST_TMP")))
    os.chmod(root, 0o700)
    with fast_rsa():
        ident.initialize_identity(root / "identity", installation_id=installation_id, profile=PROFILE)
    return root / "identity"


def drive_pairing(a: PairingService, b: PairingService) -> dict:
    """Run the three-code exchange to both confirmations; returns every artefact the tests assert on."""
    offer = a.start_offer()
    accepted = b.import_offer(offer["code"])
    revealed = a.accept_offer(offer["id"], accepted["code"])
    b_sas = b.reveal_import(accepted["id"], revealed["code"])
    row_a = a.confirm_offer(offer["id"], b_sas["sas"])
    row_b = b.confirm_import(accepted["id"], revealed["sas"])
    return {"offer": offer, "accepted": accepted, "revealed": revealed, "b_sas": b_sas, "row_a": row_a, "row_b": row_b,
            "a_id": offer["id"], "b_id": accepted["id"]}


def dispatcher_for(service: PairingService, **kw) -> ControlDispatcher:
    return ControlDispatcher(service.authority, handlers={"pair_ack": service.handle_pair_ack}, **kw)


def link_sender(dispatcher: ControlDispatcher):
    def send(endpoint_id, generation, request):
        return asyncio.run(dispatcher.dispatch(endpoint_id, generation, request))
    return send
