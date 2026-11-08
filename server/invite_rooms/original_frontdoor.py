"""Ephemeral ORIGINAL owner-approved material -> exact owned private route.

No JWT decoding, backend, credential rotation, persistent membership or cleanup bypass.
Only PeerRegistry's successful signed admission can supply the in-process proof.
"""
from __future__ import annotations
import copy
import hashlib
import threading
from dataclasses import dataclass, field
from urllib.parse import urlsplit
from invite_rooms.consent import verify
from invite_rooms.material_receipts import valid_grant
from .control.firewall import FirewallDenied


class FrontdoorDenied(FirewallDenied):
    pass


def validate_frontdoor(url):
    try:
        p=urlsplit(url)
        host=p.hostname
        authority=f'[{host}]' if host=='::1' else host
        if (type(url) is not str or host not in ('127.0.0.1','::1') or p.scheme!='http'
                or not p.port or not 0<p.port<=65535 or p.username or p.password
                or p.path!='/frontdoor' or p.query or p.fragment
                or url!=f'http://{authority}:{p.port}/frontdoor'):
            raise ValueError()
        return host,p.port
    except (ValueError,TypeError,AttributeError):
        raise FrontdoorDenied('invalid_original_frontdoor') from None


@dataclass(frozen=True)
class OwnedOriginalTarget:
    url: str
    _issuer: object = field(repr=False,compare=False)
    _entry: object = field(repr=False,compare=False)

    def validate(self, material):
        if type(self._issuer) is not OriginalFrontdoor:
            raise FrontdoorDenied('owned_provenance_required')
        entry=self._issuer.select(material.grant)
        if (entry is not self._entry or self.url!=self._issuer.url
                or material.binding!=entry.scope.binding or material.catalog!=entry.envelope['catalog']
                or material.adapter_url!=entry.route.url):
            raise FrontdoorDenied('owned_provenance_mismatch')
        return self.url

    def registration_admission(self, material):
        self.validate(material)
        entry = self._entry
        issuer = self._issuer
        def final():
            with issuer._lock:
                if (issuer._closed or not entry.active or issuer._entries.get(entry.digest) is not entry
                        or not issuer.scopes.ownership_fence(entry.scope,entry.link)):
                    raise FrontdoorDenied('original_route_changed')
        return final


@dataclass(eq=False)
class _Original:
    scope: object
    digest: str
    envelope: dict = field(repr=False)
    key: bytes = field(repr=False)
    route: object
    link: object = field(repr=False)
    active: bool = True
    guard: object = field(default=None,repr=False)


class OriginalFrontdoor:
    def __init__(self, adapter, scopes, url, fresh_registry, scope_ready):
        from .control.adapter import LoopbackAdapter
        from .control.scoped_registry import ScopedAdapterRegistry
        if type(adapter) is not LoopbackAdapter or type(scopes) is not ScopedAdapterRegistry or scopes.adapter is not adapter:
            raise FrontdoorDenied('owned_adapter_required')
        self.host,self.port=validate_frontdoor(url)
        if not callable(fresh_registry) or not callable(scope_ready):
            raise FrontdoorDenied('material_authority_required')
        self.url=url; self.adapter=adapter; self.scopes=scopes
        self._fresh=fresh_registry; self._ready=scope_ready
        self._lock=threading.RLock();self._entries={};self._closed=False
        if getattr(adapter,'original_frontdoor',None) is not None:
            raise FrontdoorDenied('frontdoor_already_owned')
        adapter.original_frontdoor=self

    def _check(self, entry):
        try:
            with self._lock:
                if self._closed or not entry.active or self._entries.get(entry.digest) is not entry:
                    raise FrontdoorDenied('original_unavailable')
            if self.scopes.lookup(entry.scope)!=entry.route:
                raise FrontdoorDenied('private_route_unavailable')
            registry=self._fresh()
            try:
                registry._validate_scope(entry.scope)
                final=registry.check_admission(entry.scope.peer_id,entry.scope.pairing_generation,entry.scope.binding)
                current=registry.lookup(entry.scope.peer_id)
                if current.consent_public_key!=entry.key:
                    raise FrontdoorDenied('consent_binding_changed')
                env=entry.envelope
                signed={k:copy.deepcopy(v) for k,v in env.items() if k!='signature'}
                signed['purpose']='joining-owner-material-v2'
                verify(current.consent_public_key,signed,env['signature'],signed)
                registry._assert_envelope_scope(env['scope'],entry.scope)
                expected=dict(binding=entry.scope.binding,catalog=env['catalog'],grant=env['grant'])
                if registry._receipt_store.load(entry.scope)!=expected:
                    raise FrontdoorDenied('material_changed')
                self._ready(entry.scope)
                final()
            finally:
                # Fresh-registry factories own release. A unit factory can share
                # its store; production installs an explicit release callback.
                release=getattr(registry,'_frontdoor_release',None)
                if release is not None:release()
            # Re-prove ownership AFTER all potentially blocking material reads.
            if not self.scopes.owns_link(entry.scope,entry.link):
                raise FrontdoorDenied('private_route_changed')
            with self._lock:
                if self._closed or not entry.active or self._entries.get(entry.digest) is not entry:
                    raise FrontdoorDenied('original_unavailable')
        except FrontdoorDenied:
            raise
        except Exception:
            raise FrontdoorDenied('original_unavailable') from None

    def enroll(self, registry, scope, *, admit=lambda: True):
        from .peer_registry import PeerRegistry
        if type(registry) is not PeerRegistry:
            raise FrontdoorDenied('trusted_signed_admission_required')
        if admit() is not True:
            raise FrontdoorDenied('admission_cancelled')
        # Never read bare receipt/network PeerMaterial as enrollment authority.
        proof=registry._verified_originals.get(scope)
        if proof is None:
            raise FrontdoorDenied('trusted_signed_admission_required')
        env,key=copy.deepcopy(proof[0]),proof[1]
        if env.get('v')!=2 or not valid_grant(env.get('grant')):
            raise FrontdoorDenied('original_v2_required')
        operation=env['operation']
        if any(operation.get(k)!=v for k,v in dict(endpoint_id=scope.endpoint_id,
                generation=scope.pairing_generation,lobby_id=scope.lobby_id,invite_id=scope.invite_id).items()):
            raise FrontdoorDenied('original_operation_mismatch')
        if env['catalog'].get('endpoint',{}).get('url')!=self.url:
            raise FrontdoorDenied('attested_endpoint_mismatch')
        if (self.adapter._server is None
                or tuple(self.adapter._server.server_address[:2])!=(self.host,self.port)):
            raise FrontdoorDenied('frontdoor_not_served')
        digest=hashlib.sha256(env['grant'].encode('ascii')).hexdigest()
        route=self.scopes.lookup(scope)
        link=self.scopes.owned_link(scope)
        if route is None or link is None:raise FrontdoorDenied('private_route_unavailable')
        with self._lock:
            if self._closed or admit() is not True:raise FrontdoorDenied('frontdoor_closed')
            old=self._entries.get(digest)
            if old is not None:
                if old.scope!=scope or old.envelope!=env or old.link is not link or not old.active:
                    raise FrontdoorDenied('original_binding_conflict')
                entry=old
            else:
                if len(self._entries)>=1024:raise FrontdoorDenied('frontdoor_capacity')
                entry=_Original(scope,digest,env,key,route,link)
                entry.guard=lambda:self._check(entry)
                self._entries[digest]=entry
        try:
            self._check(entry)
            self.scopes.guard_owned_link(scope,link,entry.guard)
            self._check(entry)
            if admit() is not True:raise FrontdoorDenied('admission_cancelled')
        except BaseException:
            # Retain cleanup reference, never erase original material on failure.
            with self._lock:entry.active=False
            raise

    def select(self, grant):
        if not valid_grant(grant):raise FrontdoorDenied('unknown_original')
        digest=hashlib.sha256(grant.encode('ascii')).hexdigest()
        with self._lock:entry=self._entries.get(digest)
        if entry is None:raise FrontdoorDenied('unknown_original')
        self._check(entry)
        return entry

    def target(self, scope, grant):
        entry=self.select(grant)
        if entry.scope!=scope:raise FrontdoorDenied('scope_mismatch')
        return OwnedOriginalTarget(self.url,self,entry)

    def retire(self, scope):
        with self._lock:
            for entry in self._entries.values():
                if entry.scope==scope:entry.active=False

    def cleanup_pending(self,scope):
        with self._lock:
            for entry in self._entries.values():
                if entry.scope==scope and not entry.active:
                    return dict(grant_digest=entry.digest,scope=scope.as_dict(),
                        private_route=entry.route.url,native_cleanup='pending_separate_cleanup_capability',
                        original_receipt_retained=True)
        return None

    def close(self):
        with self._lock:
            self._closed=True
            for entry in self._entries.values():entry.active=False
        if self.adapter.original_frontdoor is self:self.adapter.original_frontdoor=None
