"""S2 closing aggregate, one-machine real boundaries ONLY. Never production-imported."""
import asyncio
import concurrent.futures
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time
import aiohttp

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parents[1] / 'server')]
import gate_s1_native as gs
from gate_s2b_native import post, hashes, native_inventory
from gate_p2_p4_local import ready_socket
from lobby_control import HostLobbyControl, ControlError, parse_invitation
from lobby_control_lab import ControlLab
from lobby_owner import JoiningOwner, HostOwner, OwnerSurface
from lobby_native_rpc import BoundedNativeRpc
from lobby_material import MintJournal, TargetMint, PrivateRegistry
from lobby_activation import OnceActivation, OperationJournal
from room_invite_store import RoomInviteStore


def await_on(loop, coro):
    return asyncio.run_coroutine_threadsafe(coro, loop).result(45)


def discussion_complete(events, event_id):
    # A member turn settles before the driver appends final discussion activity.
    # Wait for that actual event; otherwise API and DB reads race an append.
    return (any(event['kind'] == 'turn.settled' for event in events)
            and any(event['kind'] == 'room.activity'
                    and event['payload'].get('status') == 'settled'
                    and event['payload'].get('discussion_event_id') == event_id
                    for event in events))


def method_rows(lab, room):
    return [r for r in map(json.loads, lab.audit.read_text().splitlines())
            if r.get('event') == 's2e_native_method' and r.get('room_id') == room]


def projected_links(lab, scope, material=None, url=None):
    """Native private read-only projection; bearer compared in memory, never emitted."""
    import sqlite3
    from contextlib import closing
    dbpath = lab.home / 'shared-state.db'
    with closing(sqlite3.connect('file:' + str(dbpath) + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        # Native lazily creates this table on supported persistence.
        if 'hosted_room_links' not in tables:
            return []
        rows = [dict(r) for r in db.execute('SELECT * FROM hosted_room_links WHERE room_id=?', (scope['room_id'],))]
    out = []
    for row in rows:
        projected = {k: row[k] for k in ('room_id', 'member_id', 'target_url', 'target_profile', 'cancellation_scope_id', 'trace_id', 'status')}
        if material:
            projected['exact_private_material'] = (row['grant'] == material.grant and json.loads(row['catalog_json']) == material.catalog
                and row['target_url'] == url and row['target_profile'] == scope['binding']['target_profile'])
        out.append(projected)
    return out


class Scenario:
    """One manually confirmed fixed-roster scope, real owner surfaces/control/material."""
    def __init__(self, lab, name, host_rpc, target_rpc, loop):
        self.lab, self.root, self.name = lab, lab.out / name, name
        self.host_rpc, self.target_rpc, self.loop = host_rpc, target_rpc, loop
        self.network = ControlLab(self.root / 'control', host_id=lab.report['identity']['gateway_id'],
            target_id=lab.report['identity']['second_gateway_id'], target_catalog=lab.target_catalog,
            require_target_catalog=True, owner_a=lab.transport.owner_a, owner_b=lab.transport.owner_b)
        lab.networks.append(self.network)
        self.transport = None
        self.values = []

    def check(self, name, ok, **detail):
        self.lab.check(self.name + '_' + name, ok, **detail)

    def __enter__(self):
        self.network.__enter__()
        try:
            n = self.network
            self.store = RoomInviteStore(self.root / 'mc.sqlite')
            self.lobby = self.store.create_lobby(name='S2.E ' + self.name, roster=n.roster)
            self.lid = self.lobby['lobby_id']
            self.invite = self.store.issue_invite(self.lid, 'peer-b', invitee_label='Manual owner trust exchange')
            self.join = JoiningOwner(self.root / 'joining', n.joining, self.lid, n.binding, n.host_id)
            self.join.confirm_scope(self.lobby['planned_room_id'], 'peer-b')
            self.scope = dict(lobby_id=self.lid, invite_id=self.invite.invite_id, room_id=self.lobby['planned_room_id'],
                              member_id='peer-b', binding=n.binding, host_id=n.host_id)
            from adapter import LoopbackAdapter
            self.adapter = LoopbackAdapter()
            self.adapter.start(host='127.0.0.1')
            self.url = self.adapter.url('s2e')
            self.registry = PrivateRegistry(self.root / 'home-private', self.scope, self.url)
            n.serve(HostLobbyControl(self.store, self.lid, n.binding, joining_owner_key=self.join.public_key,
                                    host_id=n.host_id, material_registry=self.registry))
            self.mint = TargetMint(self.join, MintJournal(self.root / 'target-private'), self.target_rpc)
            self.activation = OnceActivation(self.store, self.scope, self.registry, OperationJournal(self.root / 'activation'), self.host_rpc)
            self.payload = dict(lobby_id=self.lid, invite_id=self.invite.invite_id)
            self.host = OwnerSurface(self.root / 'host-owner', {'approve': HostOwner(self.store, self.lid, n.binding).approve,
                'activate': lambda p: await_on(self.loop, self.activation.activate(p)),
                'send': lambda p: await_on(self.loop, self.activation.first_send(p))})
            self.host.__enter__()
            self.target = OwnerSurface(self.root / 'target-owner', {'consent': self.join.consent,
                'mint': lambda p: await_on(self.loop, self.mint.mint(p))})
            self.target.__enter__()
            self.values = [self.invite.secret, self.host.token, self.target.token]
            return self
        except BaseException:
            self.__exit__(*sys.exc_info())
            raise

    def prepare(self):
        n, p = self.network, self.payload
        before = native_inventory(self.lab)['mint_count']
        self.check('fragment_parser', parse_invitation('https://rooms.lab/invite/' + self.lid + '/' + self.invite.invite_id + '#' + self.invite.secret)
                   == (self.lid, self.invite.invite_id, self.invite.secret))
        preview = n.call(dict(op='preview', invite_id=self.invite.invite_id))
        self.check('remote_preview', preview['name'] == self.lobby['name'] and self.invite.secret not in json.dumps(preview))
        self.check('no_preconsent_activation', post(self.host, dict(p, op='activate'))[0] == 409)
        def redeem(_):
            try:
                return n.call(dict(op='redeem', invite_id=self.invite.invite_id, secret=self.invite.secret))['status']
            except ControlError as exc:
                return str(exc)
        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
            results = list(pool.map(redeem, range(20)))
        self.check('remote_race_1_of_20', results.count('awaiting_host_approval') == 1 and results.count('control_unavailable') == 19)
        self.check('wrong_host_owner', post(self.host, dict(p, op='approve'), self.target.token) == (403, {'error': 'owner_required'}))
        self.check('joining_before_host', post(self.target, dict(p, op='consent'))[0] == 409)
        self.check('host_approval', post(self.host, dict(p, op='approve')) == (200, {'status': 'consent_required'}))
        self.check('host_only_activation', post(self.host, dict(p, op='activate'))[0] == 409)
        self.check('host_only_mint', post(self.target, dict(p, op='mint'))[0] == 409)
        self.check('wrong_joining_owner', post(self.target, dict(p, op='consent'), self.host.token)[0] == 403)
        self.check('joining_consent', post(self.target, dict(p, op='consent')) == (200, {'status': 'ready'}))
        self.consent_before = self.store.get_redemption(self.lid, self.invite.invite_id)
        self.check('no_automatic_mint', native_inventory(self.lab)['mint_count'] == before)
        self.check('real_target_mint', post(self.target, dict(p, op='mint')) == (200, {'status': 'material_ready'}))
        self.material = await_on(self.loop, self.registry.resolve(dict(n.binding, lobby_id=self.lid, room_id=self.scope['room_id'], member_id='peer-b')))
        self.values.append(self.material.grant)
        self.check('one_target_mint', native_inventory(self.lab)['mint_count'] == before + 1)
        self.check('replayed_mint_identical', post(self.target, dict(p, op='mint')) == (200, {'status': 'material_ready'})
                   and native_inventory(self.lab)['mint_count'] == before + 1)
        self.check('consent_never_renewed', self.store.get_redemption(self.lid, self.invite.invite_id) == self.consent_before)
        for field in ('endpoint_id', 'fingerprint', 'target_install_id', 'target_profile', 'lobby_id', 'room_id', 'member_id'):
            peer = dict(n.binding, lobby_id=self.lid, room_id=self.scope['room_id'], member_id='peer-b')
            peer[field] = '0' * 64 if field == 'fingerprint' else 'wrong-scope'
            blocked = False
            try:
                await_on(self.loop, self.registry.resolve(peer))
            except ControlError:
                blocked = True
            self.check('resolver_rejects_' + field, blocked)
        from connector import RelayClient
        from lobby_control import PinnedControl
        old = n.joining.client
        old.stop()
        fresh = RelayClient(relay_url=old.relay_url, identity=old.identity, peer_identity=old.peer_identity,
            link_id=old.link_id, room_id=old.room_id, credential=old._credential, ssl_context=old.ssl_context)
        n.clients.append(fresh)
        fresh.start()
        n.joining = PinnedControl(fresh, dict(peer_ca_file=n.owner_a['ca_file'], peer_fingerprint=n.owner_a['fingerprint'],
            cert_file=n.owner_b['cert_file'], key_file=n.owner_b['key_file']))
        self.mint = TargetMint(JoiningOwner(self.root / 'joining', n.joining, self.lid, n.binding, n.host_id),
                               MintJournal(self.root / 'target-private'), self.target_rpc)
        self.check('actual_connector_reconnect_no_renewal', old.incarnation != fresh.incarnation
            and post(self.target, dict(p, op='mint')) == (200, {'status':'material_ready'})
            and native_inventory(self.lab)['mint_count'] == before + 1
            and self.store.get_redemption(self.lid, self.invite.invite_id) == self.consent_before)
        from gate_f1_local import NetworkTransport
        from room_firewall import RoomFirewall
        from run_bindings import HostRunBindings
        self.transport = NetworkTransport()
        self.lab.transports.append(self.transport)
        self.transport.start()
        self.transport.native_receipt_db = self.lab.target / 'runs_idempotency.db'
        self.bindings = HostRunBindings(self.root / 'host-bindings.sqlite')
        data_scope = dict(room_id=self.scope['room_id'], home_install_id=n.host_id, authority_gateway_id=n.host_id,
                          authority_epoch=1, member_id='peer-b', target_install_id=n.target_id, target_profile='default')
        self.transport.bind(self.name, RoomFirewall(expected_scope=data_scope,
            backend=gs.gc.native_backend('http://127.0.0.1:18791', self.material.grant), verify_native=self.bindings,
            on_admitted=self.bindings.record_ack), target_url='http://127.0.0.1:18791', scope=data_scope)
        self.adapter.bind('s2e', self.transport.host_firewall, target_url='relay:' + self.name, scope=data_scope)

    def succeed(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _: post(self.host, dict(self.payload, op='activate')), range(2)))
        expected = (200, {'status': 'active', 'room_id': self.scope['room_id']})
        self.check('once_activation', expected in responses, responses=responses)
        resumed = OnceActivation(RoomInviteStore(self.store.path), self.scope, self.registry, OperationJournal(self.root / 'activation'), self.host_rpc)
        self.check('restart_readback', await_on(self.loop, resumed.activate(self.payload)) == expected[1])
        page = await_on(self.loop, self.host_rpc('groups.log', dict(room_id=self.scope['room_id'], since_seq=0, limit=500)))
        self.check('activation_no_send', page['events'] == [])
        rows = method_rows(self.lab, self.scope['room_id'])
        self.check('native_method_entries_exact_once', sum(r['method'] == 'groups.create' and r['phase'] == 'enter' for r in rows) == 1
                   and sum(r['method'] == 'groups.peer.register' and r['phase'] == 'enter' for r in rows) == 1, methods=rows)
        links = projected_links(self.lab, self.scope, self.material, self.url)
        self.check('exact_native_private_link', len(links) == 1 and links[0]['exact_private_material'], links=links)
        spec = gs.gc.read_db(self.store.path, 'SELECT native_spec_json FROM lobbies WHERE lobby_id=?', (self.lid,))[0]
        state = await_on(self.loop, self.host_rpc('groups.state', {'room_id': self.scope['room_id']}))
        self.check('exact_roster', state['room']['members'] == json.loads(spec['native_spec_json'])['members'])
        send = dict(self.payload, op='send', text='@peer Reply only LAB_READY, without mentioning other members. For any subsequent round reply PASS.')
        self.check('wrong_owner_send', post(self.host, send, self.target.token)[0] == 403)
        sent = post(self.host, send)
        self.check('explicit_send', sent[0] == 200 and sent[1].get('status') == 'sent')
        self.check('send_resume_same_event', await_on(self.loop, resumed.first_send({k:v for k,v in send.items() if k != 'op'})) == sent[1])
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            page = await_on(self.loop, self.host_rpc('groups.log', dict(room_id=self.scope['room_id'], since_seq=0, limit=500)))
            replies = [e for e in page['events'] if e['kind'] == 'message.member' and e['payload'].get('member_id') == 'peer-b']
            if replies and discussion_complete(page['events'], sent[1]['event_id']):
                break
            if any(e['kind'] in {'turn.failed','turn.deferred'} for e in page['events']):
                raise RuntimeError('native_turn_failed')
            time.sleep(.5)
        self.check('discussion_settled', discussion_complete(page['events'], sent[1]['event_id']))
        self.check('actual_LAB_READY', len(replies) == 1 and replies[0]['payload']['text'] == 'LAB_READY')
        tasks = gs.gc.read_db(self.lab.home / 'shared-state.db', 'SELECT task_id,execution_generation,run_id FROM hosted_room_remote_runs WHERE room_id=?', (self.scope['room_id'],))
        self.check('one_actual_remote_run', len(tasks) == 1, tasks=tasks)
        from gate_g_native import _receipts
        receipts = _receipts(self.lab.target, 'room:' + tasks[0]['task_id'] + ':' + str(tasks[0]['execution_generation']))
        self.check('one_actual_receipt', receipts == [(tasks[0]['run_id'],)], receipts=receipts)
        canonical = [{'seq':e['seq'],'event_id':e['event_id']} for e in page['events']]
        db = gs.gc.read_db(self.lab.home / 'shared-state.db', 'SELECT seq,event_id FROM hosted_room_events WHERE room_id=? ORDER BY seq', (self.scope['room_id'],))
        self.check('canonical_ids_order', canonical == db and not page['has_more'] and len(canonical) == len({e['event_id'] for e in canonical})
                   and all(a['seq'] < b['seq'] for a,b in zip(canonical,canonical[1:])), canonical=canonical)
        self.lab.report.setdefault('successful_scopes', []).append(self.scope)

    def __exit__(self, *args):
        from urllib.parse import urlsplit
        failures = []
        def attempt(code, action):
            try:
                return action()
            except BaseException as exc:
                failures.append((code, exc))
                return None

        ports = {}
        for key in ('host', 'target'):
            item = getattr(self, key, None)
            if item:
                ports[key] = attempt(key + '_port', lambda item=item: urlsplit(item.base).port)
        if hasattr(self, 'url'):
            ports['adapter'] = attempt('adapter_port', lambda: urlsplit(self.url).port)
        if self.network.daemon:
            ports['control_relay'] = self.network.daemon.bound_port
        roots = [self.root]
        if self.transport:
            roots.append(self.transport.root)
            if self.transport.daemon:
                ports['data_relay'] = self.transport.daemon.bound_port
            if getattr(self.transport, 'host_adapter', None):
                ports['data_adapter'] = attempt('data_adapter_port', lambda: urlsplit(self.transport.host_adapter.url(self.name)).port)

        transport_closed = False
        try:
            # Inventory is best-effort only.  It must never prevent teardown.
            for root in roots:
                try:
                    paths = root.rglob('*')
                    for p in paths:
                        if p.is_file() and (p.suffix in ('.key', '.token') or p.parent.name == 'creds'):
                            value = p.read_bytes().strip()
                            if value:
                                self.lab.secret_bytes.append(value)
                except BaseException as exc:
                    failures.append(('secret_inventory', exc))
        finally:
            for key in ('host', 'target', 'adapter'):
                item = getattr(self, key, None)
                if item:
                    attempt(key + '_teardown', lambda item=item, key=key:
                            item.__exit__(*args) if key != 'adapter' else item.close())
            if self.transport:
                before = len(failures)
                attempt('transport_teardown', self.transport.close)
                transport_closed = len(failures) == before
                # Failed close remains owned by the top-level cleanup path.
                if transport_closed and self.transport in self.lab.transports:
                    self.lab.transports.remove(self.transport)
            attempt('control_network_teardown', self.network.close)

        if self.values:
            try:
                blobs = [p.read_bytes() for p in self.root.rglob('*.sqlite*')] + [b''.join(self.network.observed)]
                values = [v.encode() for v in self.values + self.lab.keys] + self.lab.secret_bytes
                self.check('secrets_absent_mc_private_db_relay', all(v not in b for v in values for b in blobs))
                self.lab.secret_values.extend(self.values)
            except BaseException as exc:
                failures.append(('secret_scan', exc))
        try:
            free = {name: dict(port=port, free=gs.gc.pf.check_port_available(port)['available'])
                    for name, port in ports.items() if port is not None}
            self.check('all_scenario_ports_free', all(r['free'] for r in free.values()), ports=free)
            self.check('control_teardown', self.network.loop.is_closed() and not self.network.thread.is_alive()
                       and all(not c._thread.is_alive() and not c.application_threads for c in self.network.clients))
        except BaseException as exc:
            failures.append(('post_teardown_check', exc))
        if failures:
            raise RuntimeError('scenario_teardown_failed:' + ','.join(code for code, _ in failures))


async def native(lab):
    from s2e_activation_faults import run_activation_faults
    async with aiohttp.ClientSession(trust_env=False, timeout=aiohttp.ClientTimeout(total=200)) as session:
        host = await ready_socket(session, 'http://127.0.0.1:18791', 18790, lab.keys[1], lab.keys[2], lab.children)
        deadline = time.monotonic() + 150
        while True:
            try:
                target = await session.ws_connect(f'ws://127.0.0.1:{lab.target_tui_port}/api/ws?token={lab.target_ws_key}')
                break
            except (OSError, aiohttp.ClientError):
                if time.monotonic() > deadline or lab.target_tui.poll() is not None:
                    await host.close()
                    raise RuntimeError('target_native_readiness') from None
                await asyncio.sleep(.5)
        async with host, target:
            hr, tr = BoundedNativeRpc(host), BoundedNativeRpc(target)
            caps = await tr('groups.capabilities', {})
            lab.target_catalog = caps['room_link']['catalog']
            lab.check('actual_native_target_identity', caps['authority_gateway_id'] == lab.report['identity']['second_gateway_id'])
            from room_lobby_service import NativeRpcRejected
            rejected = False
            try:
                await hr('groups.state', {'room_id':'s2e-nonexistent-room'})
            except NativeRpcRejected:
                rejected = True
            lab.check('actual_native_explicit_rejection_not_ambiguity', rejected)
            loop = asyncio.get_running_loop()
            def normal():
                with Scenario(lab, 'normal', hr, tr, loop) as scenario:
                    scenario.prepare()
                    scenario.succeed()
            await asyncio.to_thread(normal)
            await asyncio.to_thread(run_activation_faults, lab, hr, tr, loop, Scenario)
            from s2e_activation_faults import q5_recovery
            await asyncio.to_thread(q5_recovery, lab, hr, tr, loop, Scenario)


def main():
    # Missing fault harness must be a RED test, never a silently skipped matrix.
    from s2e_activation_faults import install_native_observer
    lab = gs.NativeLab()
    lab.out = gs.gc.ROOT / 'evidence/s2' / ('s2e-native-' + lab.stamp)
    lab.out.mkdir(parents=True, mode=0o700)
    lab.report['scope'] = 'S2.E fresh aggregate; one-machine actual boundaries; F1 CLOSED'
    lab.secret_values = []
    lab.secret_bytes = []
    start = hashes()
    lab.report['s2e_source_start'] = start
    try:
        with gs.native_signals(lab.report):
            if '--native-only' not in sys.argv:
                argv = [str(gs.PY), '-B', '-X', 'tracemalloc=5', '-W', 'always::ResourceWarning', '-m', 'unittest', 'discover', '-s', 'spikes/invite-rooms/tests']
                with (lab.out / 'spike-aggregate.log').open('w') as log:
                    # Includes isolated preflight/toolchain setup and tracemalloc.
                    # Successful supervisor suites already take 441-480s; the old
                    # 540s cap killed a progressing suite before native startup.
                    result = subprocess.run(argv, cwd=HERE.parents[1], stdout=log, stderr=subprocess.STDOUT,
                        env=dict(os.environ, TMPDIR=str(gs.gc.ROOT)), timeout=900)
                content = (lab.out / 'spike-aggregate.log').read_text()
                lab.report['aggregate_suite'] = {'argv':argv,'exit':result.returncode}
                lab.check('current_socket_adversarial_regressions', result.returncode == 0 and 'ResourceWarning' not in content
                           and 'Traceback (most recent call last)' not in content)
            install_native_observer(gs)
            lab.setup()
            with socket.socket() as sock:
                sock.bind(('127.0.0.1',0))
                lab.target_tui_port = sock.getsockname()[1]
            lab.target_ws_key = secrets.token_hex(32)
            token = lab.target / '.hermes/desktop-ssh' / secrets.token_hex(16) / (secrets.token_hex(8) + '.token')
            token.parent.mkdir(parents=True, mode=0o700)
            token.write_text(lab.target_ws_key)
            token.chmod(0o600)
            from s2c_mint_faults import private_json
            lab.target_token_file = lab.out / 'target-native-private.token'
            lab.target_token_file.write_text(lab.target_ws_key)
            lab.target_token_file.chmod(0o600)
            lab.host_token_file = lab.out / 'host-native-private.token'
            lab.host_token_file.write_text(lab.keys[2])
            lab.host_token_file.chmod(0o600)
            lab.args['target-tui'] = ['serve','--isolated','--host','127.0.0.1','--port',str(lab.target_tui_port),'--ssh-session-token-file',str(token)]
            lab.target_tui = lab.spawn('target-tui')
            asyncio.run(native(lab))
    except BaseException as exc:
        lab.report['error_type'] = type(exc).__name__
        if isinstance(exc, AssertionError):
            lab.report['failed_check'] = str(exc) if str(exc) in lab.report['checks'] else 'unclassified_assertion'
    finally:
        lab.close()
    if hasattr(lab,'target_tui_port'):
        lab.check('extra_native_port_free', gs.gc.pf.check_port_available(lab.target_tui_port)['available'], port=lab.target_tui_port)
    logs = list(lab.out.rglob('*.log')) + list(lab.out.rglob('report.json')) + list(lab.out.rglob('*.jsonl'))
    values = [v.encode() for v in lab.secret_values + lab.keys + ([lab.target_ws_key] if hasattr(lab,'target_ws_key') else [])]
    values.extend(lab.secret_bytes)
    for p in lab.out.rglob('*'):
        if p.is_file() and (p.suffix in ('.key', '.token') or p.parent.name == 'creds'):
            values.append(p.read_bytes() if p.suffix == '.key' else p.read_bytes().strip())
    import re
    lab.report['secret_scan'] = {'files':len(logs), 'exact_hits':sum(v in p.read_bytes() for v in values if v for p in logs),
        'grant_or_private_key_hits':sum(bool(re.search(rb'HermesRoom [A-Za-z0-9_.-]{40,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',p.read_bytes())) for p in logs)}
    lab.report['s2e_source_end'] = hashes()
    lab.report['native_checks_passed'] = (not lab.report.get('error_type') and lab.report.get('teardown_complete')
        and all(c['ok'] for c in lab.report['checks'].values()) and start == hashes()
        and lab.report['secret_scan']['exact_hits'] == lab.report['secret_scan']['grant_or_private_key_hits'] == 0
        and lab.report['source_start']['source_sha256'] == lab.report['source_end']['source_sha256'])
    lab.report['aggregate_complete'] = lab.report['native_checks_passed'] and '--native-only' not in sys.argv
    lab.flush()
    print(json.dumps({'report':str(lab.out/'report.json'),'aggregate_complete':lab.report['aggregate_complete'],
                      'error_type':lab.report.get('error_type'),'failed_check':lab.report.get('failed_check')}))
    return 0 if lab.report['native_checks_passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
