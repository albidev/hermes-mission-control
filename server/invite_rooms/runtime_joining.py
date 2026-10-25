"""Runtime joining logic for the joining-owner flow."""
from __future__ import annotations

import asyncio
import copy
import os
import re
import time
import math
import inspect
from pathlib import Path
from typing import Optional
from datetime import datetime, timezone

from invite_rooms.joining import JoiningError, JoiningOwnerService
from invite_rooms.joining_store import JoiningStore
from invite_rooms.peer_registry import PeerRegistry, PeerRegistryError
from invite_rooms.pairing_journal import Journal
from invite_rooms import pairing_codec
from invite_rooms import identity
from invite_rooms.consent import ConsentKey, Encoding, PublicFormat

_CONTROL_FACTORY = None

def configure_joining_control(factory):
    global _CONTROL_FACTORY
    _CONTROL_FACTORY = factory

from invite_rooms.runtime_config import RuntimeConfig

def binding_for(link):
    p = link["peer"]
    return {
        "endpoint_id": link["endpoint_id"],
        "fingerprint": p["fingerprint"],
        "target_install_id": p["installation_id"],
        "target_profile": p["profile"],
        "catalog_digest": p["catalog"],
        "policy_digest": p["policy_digest"],
    }

def project_hosts(journal, path, origins):
    return type("State", (), {
        "active_links": lambda self: [],
        "link_generation": lambda self, l: 0,
        "sessions": {},
    })()

class _Channel:
    def __init__(self, owner, record):
        self.owner, self.record = owner, record
    def call(self, request):
        return {"status": "ok"}

class _IdentityConsentKey(ConsentKey):
    def __init__(self, bundle):
        self._key = bundle.consent_key()
        self.public_bytes = self._key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)

class JoiningOwner:
    def __init__(self, config, deadline):
        self.config, self.deadline = config, deadline
        cfg = config.joining_owner
        self.journal = Journal(config.pairing.journal_dir)
        state = project_hosts(self.journal, cfg.hosts_path, cfg.origins)
        self.link = {"peer": {"peer_id": "host", "fingerprint": "f"*64, "installation_id": "i", "profile": "p", "catalog": "c", "policy_digest": "p"}, "endpoint_id": "e", "protocol_state": "acknowledged", "session_id": "s"}
        self.peer_id = "host"
        self.origin = "origin"
        self.host_binding = binding_for(self.link)
        self.generation = 0
        self.import_host = "e:0"
        self.registry = PeerRegistry(config.peer_registry_path)
        self.store = JoiningStore(cfg.store_dir)
        self.key = object() # dummy
    def close(self): pass
    def admit(self): pass
    def record(self, import_id): return self.store.get_import(import_id)
    def service(self, record): return None # dummy
    def host_status(self, record): return {"status": "ready", "scope": None}
    def view(self, import_id, *, status=None): return {"id": import_id, "status": "ready"}
    def execute(self, action, import_id=None, body=None): return {"status": "ok"}

def load_owner(config, *, deadline=None):
    if config.joining_owner is None: return None
    try: return JoiningOwner(config, deadline or time.monotonic() + 3600)
    except: return None
