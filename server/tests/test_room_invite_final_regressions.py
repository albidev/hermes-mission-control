"""R1-R6 regressions: real SQLite/canonical contracts, recording RPC transport only.
No native admission/cryptographic grant verification is claimed by RecordingRpc.
Run with the native contract explicitly available on PYTHONPATH, never a hidden fallback.
"""
import asyncio
import builtins
import copy
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from room_invite_store import RoomInviteStore, InviteError
from room_lobby_service import RoomLobbyService, PeerMaterial
from invite_rooms.peer_registry import PeerRegistryError, _native_catalog
from invite_rooms.service import InviteRoomsService, ServiceError
from test_room_invite_operations import make_service, ROSTER, CATALOG, INSTALL_A, INSTALL_B, ADAPTER_URL, GRANT

BASE = Path('/Users/albi/.hermes/cache/scratch/invite-rooms-mvp1-baseline-dc028f5/server')


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class RecordingRpc:
    """Transport stand-in; no native grant verification, admission or networking."""
    def __init__(self, authority=INSTALL_A):
        self.authority, self.calls, self.room, self.registered = authority, [], None, False

    async def __call__(self, method, params):
        self.calls.append((method, copy.deepcopy(params)))
        if method == 'groups.capabilities':
            return {'driver': True, 'authority_gateway_id': self.authority}
        if method == 'groups.create':
            from gateway.hosted_room_discussion import validate_roster
            validate_roster(params['members'], local_profiles=['default'])
            self.room = dict(copy.deepcopy(params), authority_gateway_id=self.authority, authority_epoch=1)
            return {'room': copy.deepcopy(self.room)}
        if method == 'groups.peer.register':
            self.registered = True
            return {'registered': True, 'target_install_id': INSTALL_B, 'target_profile': 'default'}
        if method == 'groups.state':
            routes = [{'member_id': 'peer-b', 'room_id': self.room['room_id'], 'status': 'ready'}] if self.registered else []
            return {'room': copy.deepcopy(self.room), 'driver_status': {'peer_routes': routes}}
        raise AssertionError(method)

    def mutations(self):
        return [m for m, _ in self.calls if m in {'groups.create', 'groups.peer.register', 'groups.send', 'groups.peer.invite'}]


class SchemaR1Tests(unittest.TestCase):
    def test_real_v1_is_rejected_before_writes_concurrently_and_on_restart(self):
        old = load('final_baseline_store', BASE/'room_invite_store.py')
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'v1.sqlite'
            store = old.RoomInviteStore(path)
            lobby = store.create_lobby(name='Historical', roster=copy.deepcopy(ROSTER))
            inv = store.issue_invite(lobby['lobby_id'], 'peer-b', invitee_label='B')
            binding = {'endpoint_id': 'endpoint-b', 'fingerprint': 'b'*64, 'target_install_id': INSTALL_B, 'target_profile': 'default'}
            store.redeem(lobby['lobby_id'], inv.invite_id, inv.secret, binding=binding)
            store.decide_host(lobby['lobby_id'], inv.invite_id, binding=binding, approved=True)
            store.consent_joining(lobby['lobby_id'], inv.invite_id, binding=binding, consented=True)
            with sqlite3.connect(path) as db:
                before = list(db.iterdump())
            def reopen(_):
                with self.assertRaisesRegex(InviteError, '^legacy_schema_recovery_required$'):
                    RoomInviteStore(path)
            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(reopen, range(16)))
            reopen(0)
            with sqlite3.connect(path) as db:
                self.assertEqual(list(db.iterdump()), before)

    def test_new_schema_version_and_future_write_refusal(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'v2.sqlite'
            store = RoomInviteStore(path)
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute('SELECT version FROM schema_version').fetchall(), [(2,)])
                db.execute('UPDATE schema_version SET version=3')
            with self.assertRaisesRegex(InviteError, '^schema_too_new$'):
                store.bind_owner_scope(INSTALL_A, 'default')
            future = RoomInviteStore(path)
            self.assertTrue(future.read_only)
            with self.assertRaisesRegex(InviteError, '^schema_too_new$'):
                future.bind_owner_scope(INSTALL_A, 'default')


class PortableR3Tests(unittest.TestCase):
    def test_missing_contract_is_sanitized_and_does_not_mutate_path(self):
        original = builtins.__import__
        before = sys.path[:]
        def missing(name, *args, **kwargs):
            if name.startswith('gateway'):
                raise ModuleNotFoundError('private-path-must-not-leak')
            return original(name, *args, **kwargs)
        with patch('builtins.__import__', missing):
            with self.assertRaisesRegex(PeerRegistryError, '^native_contract_unavailable$'):
                _native_catalog(CATALOG)
        self.assertEqual(sys.path, before)

    def test_present_contract_is_real_and_rejects_forged_digest(self):
        self.assertEqual(_native_catalog(CATALOG).installation_id, INSTALL_B)
        bad = copy.deepcopy(CATALOG)
        bad['catalog_digest'] = 'f'*64
        with self.assertRaisesRegex(PeerRegistryError, '^invalid_catalog$'):
            _native_catalog(bad)


class ActivationR2R4R5R6Tests(unittest.IsolatedAsyncioTestCase):
    def setup_service(self, *, authority=INSTALL_A, legacy=False):
        svc, ctx, store, reg, tmp = make_service()
        self.addCleanup(tmp.cleanup)
        rpc = RecordingRpc(authority)
        svc._rpc = rpc
        lobby = svc.create_lobby(ctx, name='Scoped activation', roster=copy.deepcopy(ROSTER))
        lid = lobby['lobby_id']
        inv = svc.issue_invite(ctx, lid, 'peer-b', invitee_label='B')
        binding = reg.binding_for('owner-b')
        if legacy:
            binding = {k:v for k,v in binding.items() if k not in {'catalog_digest','policy_digest'}}
        store.redeem(lid, inv.invite_id, inv.secret, binding=binding)
        store.decide_host(lid, inv.invite_id, binding=binding, approved=True)
        store.consent_joining(lid, inv.invite_id, binding=binding, consented=True)
        return svc, ctx, store, reg, rpc, lobby, inv, binding

    def scoped(self, ctx, lobby, inv, binding):
        from invite_rooms.peer_registry import MaterialScope, ScopedPeerMaterial
        scope = MaterialScope(owner_installation_id=ctx.installation_id, owner_profile=ctx.profile,
                              lobby_id=lobby['lobby_id'], planned_room_id=lobby['planned_room_id'],
                              member_id='peer-b', invite_id=inv.invite_id, peer_id='owner-b', **binding)
        return ScopedPeerMaterial(scope, PeerMaterial(copy.deepcopy(binding), copy.deepcopy(CATALOG), GRANT, ADAPTER_URL))

    async def test_R2_real_once_activation_plan_retains_four_field_lab_contract(self):
        svc, ctx, store, reg, rpc, lobby, inv, binding = self.setup_service(legacy=True)
        spike = Path(__file__).resolve().parents[2]/'spikes/invite-rooms'
        with patch.object(sys, 'path', [str(spike), *sys.path]):
            from lobby_activation import OnceActivation
            from types import SimpleNamespace
            scope = {'lobby_id': lobby['lobby_id'], 'invite_id': inv.invite_id, 'room_id': lobby['planned_room_id'],
                     'member_id': 'peer-b', 'host_id': INSTALL_A, 'binding': binding}
            seen = []
            class PrivateLabResolver:
                async def resolve(self, exact):
                    seen.append(exact)
                    return PeerMaterial(copy.deepcopy(binding), copy.deepcopy(CATALOG), GRANT, ADAPTER_URL)
            workflow = OnceActivation(store, scope, PrivateLabResolver(), None, rpc)
            spec, authority, material = await workflow.planner._plan(lobby['lobby_id'])
            self.assertEqual(authority, INSTALL_A)
            self.assertEqual(spec['room_id'], lobby['planned_room_id'])
            self.assertEqual(material['peer-b'].binding, binding)
            self.assertEqual(seen, [dict(binding, member_id='peer-b', lobby_id=lobby['lobby_id'], room_id=lobby['planned_room_id'])])
            baseline = load('final_baseline_coordinator', BASE/'room_lobby_service.py')
            async def resolve(_):
                return baseline.PeerMaterial(copy.deepcopy(binding), copy.deepcopy(CATALOG), GRANT, ADAPTER_URL)
            old_spec, old_auth, _ = await baseline.RoomLobbyService(store, rpc, resolve)._plan(lobby['lobby_id'])
            self.assertEqual((spec, authority), (old_spec, old_auth))
        self.assertEqual(rpc.mutations(), [])

    async def test_R2_product_cannot_fall_through_to_lab_legacy_binding(self):
        svc, ctx, store, reg, rpc, lobby, inv, binding = self.setup_service(legacy=True)
        async def old_resolver(_):
            return PeerMaterial(binding, CATALOG, GRANT, ADAPTER_URL)
        svc._material_resolver = old_resolver
        with self.assertRaisesRegex(ServiceError, '^activation_not_ready$'):
            await svc.activate(ctx, lobby['lobby_id'])
        self.assertEqual(rpc.calls, [])
        row = store.get_redemption(lobby['lobby_id'], inv.invite_id)
        self.assertIsNone(row['catalog_digest'])
        self.assertIsNone(row['policy_digest'])

    async def test_R4_valid_scoped_approved_material_activates_lazily(self):
        svc, ctx, store, reg, rpc, lobby, inv, binding = self.setup_service()
        scoped = self.scoped(ctx, lobby, inv, binding)
        reg.approve_scoped_material(scoped)
        seen = []
        async def resolve(scope):
            seen.append(scope)
            return scoped
        svc._material_resolver = resolve
        svc.inspect_lobby(ctx, lobby['lobby_id'])
        self.assertTrue(svc.activation_status(ctx, lobby['lobby_id'])['ready'])
        self.assertEqual((seen, rpc.calls), ([], []))
        result = await svc.activate(ctx, lobby['lobby_id'])
        self.assertEqual(result['status'], 'activated')
        self.assertEqual(seen, [scoped.scope])
        self.assertEqual(rpc.mutations(), ['groups.create', 'groups.peer.register'])
        payload = next(p for m,p in rpc.calls if m == 'groups.peer.register')
        self.assertEqual((payload['target_url'],payload['grant']), (ADAPTER_URL,GRANT))

    async def test_R4_rejects_url_grant_catalog_binding_and_scope_substitution_before_mutation(self):
        for field in ('url','grant','catalog','binding','owner_installation_id','owner_profile','lobby_id','planned_room_id','member_id','invite_id','peer_id'):
            with self.subTest(field=field):
                svc, ctx, store, reg, rpc, lobby, inv, binding = self.setup_service()
                good = self.scoped(ctx, lobby, inv, binding)
                reg.approve_scoped_material(good)
                material, scope = good.material, good.scope
                if field == 'url': material = replace(material, adapter_url='http://127.0.0.1:19002/links/unapproved')
                elif field == 'grant': material = replace(material, grant='foreign-room-private-grant')
                elif field == 'catalog':
                    catalog = copy.deepcopy(material.catalog); catalog['catalog_digest'] = 'f'*64
                    material = replace(material, catalog=catalog)
                elif field == 'binding': material = replace(material, binding=dict(binding, fingerprint='c'*64))
                else: scope = replace(scope, **{field: 'install:'+'c'*32 if field == 'owner_installation_id' else 'other'})
                bad = replace(good, scope=scope, material=material)
                async def resolve(_): return bad
                svc._material_resolver = resolve
                with self.assertRaises(ServiceError):
                    await svc.activate(ctx, lobby['lobby_id'])
                self.assertEqual(rpc.mutations(), [])

    async def test_R4_existing_resolver_cannot_forward_unapproved_loopback(self):
        svc, ctx, store, reg, rpc, lobby, inv, binding = self.setup_service()
        async def resolve(_):
            return PeerMaterial(binding, CATALOG, GRANT, 'http://127.0.0.1:19002/links/not-approved')
        svc._material_resolver = resolve
        with self.assertRaises(ServiceError):
            await svc.activate(ctx, lobby['lobby_id'])
        self.assertEqual(rpc.mutations(), [])

    async def test_R5_existing_resolver_cannot_mutate_foreign_gateway(self):
        svc, ctx, store, reg, rpc, lobby, inv, binding = self.setup_service(authority='install:'+'c'*32)
        async def resolve(_): return PeerMaterial(binding, CATALOG, GRANT, ADAPTER_URL)
        svc._material_resolver = resolve
        with self.assertRaisesRegex(ServiceError, '^authority_mismatch$'):
            await svc.activate(ctx, lobby['lobby_id'])
        self.assertEqual(rpc.mutations(), [])

    async def test_R6_malformed_peer_id_is_machine_error(self):
        svc, ctx, store, reg, rpc, lobby, inv, binding = self.setup_service()
        for operation in (reg.lookup, reg.binding_for):
            with self.assertRaises(PeerRegistryError): operation([])
        with self.assertRaises((ServiceError, PeerRegistryError)):
            svc.decide_host(ctx, lobby['lobby_id'], inv.invite_id, peer_id=[], approved=True)

    async def test_R4_global_unscoped_material_is_not_product_authorization(self):
        svc, ctx, store, reg, rpc, lobby, inv, binding = self.setup_service()
        async def resolve(_): return PeerMaterial(binding, CATALOG, GRANT, ADAPTER_URL)
        svc._material_resolver = resolve
        with self.assertRaises(ServiceError): await svc.activate(ctx, lobby['lobby_id'])
        self.assertEqual(rpc.mutations(), [])

    async def test_R5_foreign_authority_rejected_before_native_mutations(self):
        svc, ctx, store, reg, rpc, lobby, inv, binding = self.setup_service(authority='install:'+'c'*32)
        scoped = self.scoped(ctx, lobby, inv, binding)
        reg.approve_scoped_material(scoped)
        async def resolve(_): return scoped
        svc._material_resolver = resolve
        with self.assertRaisesRegex(ServiceError, '^authority_mismatch$'):
            await svc.activate(ctx, lobby['lobby_id'])
        self.assertEqual(rpc.mutations(), [])
        self.assertEqual([m for m,p in rpc.calls], ['groups.capabilities'])

    async def test_R6_exact_mvp_cap_before_persistence_and_sanitized_shapes(self):
        svc, ctx, store, reg, rpc, lobby, inv, binding = self.setup_service()
        roster = copy.deepcopy(ROSTER)
        roster.append(dict(copy.deepcopy(roster[1]), member_id='peer-c', handle='other'))
        local2 = dict(ROSTER[0], member_id='local-c', handle='other', profile='other')
        for bad in (roster, [*ROSTER, local2], None, {}, [[],ROSTER[1]], [{},ROSTER[1]], [ROSTER[0], {'target':[]} ]):
            with self.subTest(roster=bad), self.assertRaises((ServiceError,InviteError,PeerRegistryError)):
                svc.create_lobby(ctx, name='Bad', roster=bad)
        with sqlite3.connect(store.path) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM lobbies').fetchone()[0], 1)
        for operation in (reg.lookup, reg.binding_for):
            with self.assertRaises(PeerRegistryError): operation([])
        with self.assertRaises((ServiceError,PeerRegistryError)):
            svc.decide_host(ctx, lobby['lobby_id'], inv.invite_id, peer_id=[], approved=True)

    async def test_R6_host_decision_is_tied_to_invite_member_not_first_remote(self):
        svc, ctx, store, reg, rpc, lobby, inv, binding = self.setup_service()
        other = store.create_lobby(name='Other invite', roster=copy.deepcopy(ROSTER))
        other_inv = store.issue_invite(other['lobby_id'],'peer-b',invitee_label='B')
        with self.assertRaises(InviteError):
            svc.decide_host(ctx,lobby['lobby_id'],other_inv.invite_id,peer_id='owner-b',approved=True)
        reg.register(peer_id='alias',installation_id=INSTALL_B,profile='default',fingerprint='b'*64,endpoint_id='endpoint-b',catalog=CATALOG,adapter_url=ADAPTER_URL,grant=GRANT)
        with self.assertRaises(ServiceError):
            svc.decide_host(ctx,lobby['lobby_id'],inv.invite_id,peer_id='alias',approved=True)


if __name__ == '__main__': unittest.main(verbosity=2)
