"""Home owner-private once-only operations. Unimported; no remote/native listener."""
import json
from lobby_control import ControlError
from lobby_consent import canonical
from lobby_material import MintJournal


class OperationAmbiguous(ControlError):
    def __init__(self, operation):
        code = ('creation_ambiguous' if operation == 'create' else
                'registration_ambiguous' if operation.startswith('register:') else 'send_ambiguous')
        super().__init__(code)


class OperationJournal:
    """Reuse audited private file/key setup; no native secrets in this table."""
    def __init__(self, root):
        self.storage = MintJournal(root)
        self.path = self.storage.path
        self._db = self.storage._db
        self._scope = self.storage._scope
        with self._db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS operations(scope TEXT, operation TEXT, state TEXT NOT NULL, result TEXT, PRIMARY KEY(scope,operation))')

    def begin(self, scope, operation):
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT state,result FROM operations WHERE scope=? AND operation=?', (self._scope(scope), operation)).fetchone()
            if row:
                if row[0] != 'done':
                    raise OperationAmbiguous(operation)
                return json.loads(row[1])
            db.execute("INSERT INTO operations VALUES(?,?,'intent',NULL)", (self._scope(scope), operation))
            db.commit()
        return None

    def complete(self, scope, operation, result):
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute("UPDATE operations SET state='done',result=? WHERE scope=? AND operation=? AND state='intent'", (canonical(result).decode(), self._scope(scope), operation)).rowcount != 1:
                raise OperationAmbiguous(operation)
            db.commit()


class OnceActivation:
    """Only a local OwnerSurface may call this provisioned home workflow."""
    def __init__(self, store, scope, registry, journal, rpc):
        from room_lobby_service import RoomLobbyService
        self.store, self.scope, self.registry = store, json.loads(canonical(scope)), registry
        self.journal, self.rpc = journal, rpc
        async def resolve(peer):
            fields = ('endpoint_id', 'fingerprint', 'target_install_id', 'target_profile', 'member_id')
            exact = {k: peer[k] for k in fields}
            exact.update(lobby_id=self.scope['lobby_id'], room_id=self.scope['room_id'])
            return await registry.resolve(exact)
        self.planner = RoomLobbyService(store, rpc, resolve)

    def validate(self, payload):
        if payload != {k: self.scope[k] for k in ('lobby_id', 'invite_id')}:
            raise ControlError('invalid_owner_scope')

    async def readback(self, spec, authority, operation):
        try:
            state = await self.rpc('groups.state', {'room_id': spec['room_id']})
            self.planner._verify_room(state['room'], spec, authority)
            return state
        except Exception:
            raise OperationAmbiguous(operation) from None

    @staticmethod
    def route_ready(state, member_id, room_id):
        driver = state.get('driver_status')
        routes = driver.get('peer_routes') if isinstance(driver, dict) else None
        return (isinstance(routes, list) and all(isinstance(r, dict) for r in routes)
                and sum(r.get('member_id') == member_id and r.get('room_id') == room_id
                        and r.get('status') == 'ready' for r in routes) == 1)

    async def activate(self, payload):
        self.validate(payload)
        from lobby_reconcile import refuse_if_quarantined
        refuse_if_quarantined(self.journal.storage, self.scope)  # typed block, never replay
        spec, authority, materials = await self.planner._plan(self.scope['lobby_id'])
        if spec['room_id'] != self.scope['room_id'] or authority != self.scope['host_id'] or set(materials) != {self.scope['member_id']}:
            raise ControlError('invalid_owner_scope')
        intent = dict(self.scope, spec=spec)
        completed = self.journal.begin(intent, 'create')
        if completed is None:
            try:
                await self.rpc('groups.create', spec)
                await self.readback(spec, authority, 'create')
                self.journal.complete(intent, 'create', {'room_id': spec['room_id']})
            except Exception:
                raise OperationAmbiguous('create') from None
        else:
            await self.readback(spec, authority, 'create')
        self.store.mark_created(self.scope['lobby_id'], spec['room_id'])
        for member_id, material in materials.items():
            operation = 'register:' + member_id
            completed = self.journal.begin(intent, operation)
            if completed is None:
                try:
                    registered = await self.rpc('groups.peer.register', {
                        'room_id': spec['room_id'], 'member_id': member_id, 'target_url': material.adapter_url,
                        'target_profile': material.binding['target_profile'], 'catalog': material.catalog, 'grant': material.grant,
                        'cancellation_scope_id': 'cancel-' + spec['room_id'], 'trace_id': 'trace-' + spec['room_id']})
                    state = await self.readback(spec, authority, operation)
                    if (registered.get('registered') is not True or registered.get('target_install_id') != material.binding['target_install_id']
                            or registered.get('target_profile') != material.binding['target_profile']
                            or not self.route_ready(state, member_id, spec['room_id'])):
                        raise ValueError()
                    self.journal.complete(intent, operation, {'room_id': spec['room_id'], 'member_id': member_id})
                except Exception:
                    raise OperationAmbiguous(operation) from None
            else:
                state = await self.readback(spec, authority, operation)
                if not self.route_ready(state, member_id, spec['room_id']):
                    raise OperationAmbiguous(operation)
            self.store.mark_peer_registered(self.scope['lobby_id'], member_id)
        return {'status': 'active', 'room_id': spec['room_id']}

    async def first_send(self, payload):
        import hashlib
        if (not isinstance(payload, dict) or set(payload) != {'lobby_id', 'invite_id', 'text'}
                or not isinstance(payload['text'], str) or not 0 < len(payload['text'].encode()) <= 4096):
            raise ControlError('invalid_owner_scope')
        self.validate({k: payload[k] for k in ('lobby_id', 'invite_id')})
        from lobby_reconcile import refuse_if_quarantined
        refuse_if_quarantined(self.journal.storage, self.scope)
        # Read back completed activation; this cannot initiate a send itself.
        await self.activate({k: payload[k] for k in ('lobby_id', 'invite_id')})
        digest = hashlib.sha256(payload['text'].encode()).hexdigest()
        operation = 'send'
        completed = self.journal.begin(self.scope, operation)
        if completed is not None:
            if completed.get('text_sha256') != digest:
                raise ControlError('first_send_conflict')
            return {k: v for k, v in completed.items() if k != 'text_sha256'}
        event_id = 'owner-first-' + self.scope['room_id']
        try:
            response = await self.rpc('groups.send', {'room_id': self.scope['room_id'], 'event_id': event_id,
                'payload': {'text': payload['text'], 'thread_id': 'first-owner'}})
            event = response['event']
            if (response.get('client_event_id') != event_id or event.get('kind') != 'message.user'
                    or not isinstance(event.get('event_id'), str) or event.get('payload', {}).get('text') != payload['text']
                    or type(event['seq']) is not int or event['seq'] <= 0):
                raise ValueError()
            page = await self.rpc('groups.log', {'room_id': self.scope['room_id'], 'since_seq': event['seq'] - 1, 'limit': 1})
            if len(page['events']) != 1 or page['events'][0] != event:
                raise ValueError()
            result = {'status': 'sent', 'room_id': self.scope['room_id'], 'event_id': event['event_id'], 'seq': event['seq']}
            self.journal.complete(self.scope, operation, dict(result, text_sha256=digest))
            return result
        except Exception:
            raise OperationAmbiguous(operation) from None
