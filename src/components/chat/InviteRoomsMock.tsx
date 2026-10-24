import { useMemo, useState } from 'react';
import { useI18n } from '../../lib/i18n';
import { GuestRoomView } from './GuestRoomView';
import { PairingSection } from './PairingSection';
import { ReceivedInvitationPanel } from './ReceivedInvitationPanel';
import { SegmentedControl } from '../ui/SegmentedControl';
import type { GuestRoom, RoomGuestApi } from '../../lib/room-guest-api';
import type { PairingRow, RoomPairingApi } from '../../lib/room-pairing-api';
import type { JoiningImport, RoomJoiningApi } from '../../lib/room-joining-api';
import { RoomPairingApiError } from '../../lib/room-pairing-api';

// This module is dynamically imported only inside import.meta.env.DEV (see App).
// All actions exercise the real screens; no screenshot-only initial step overrides.
function createFixtures(t: (key: string) => string, offline: boolean, unavailable: boolean) {
  const expiry = () => new Date(Date.now() + 15 * 60 * 1000).toISOString();
  const room: GuestRoom = {
    room_id: 'mock-room', name: 'privacy-guard · release 1.4', owner: 'Albi',
    owner_online: !offline, owner_offline_since: '14:02',
    permissions: ['windows-build', 'signtool-verify'],
    members: [{ id: 'albi', name: 'Albi' }, { id: 'basa', name: 'Basa' }],
  };
  const peer = { peer_id: 'peer-1', installation_id: 'thomas-win-desktop', profile: 'Basa' };
  let rows: PairingRow[] = [{ id: 'previous-pair', role: 'host', state: 'acknowledged', expires_at: expiry(), peer }];
  const confirmed = (role: string): PairingRow => {
    const row = { id: 'pair-1', role, state: 'confirmed', expires_at: expiry(), peer };
    rows = [...rows.filter((item) => item.id !== row.id), row];
    return row;
  };
  const pairingApi: RoomPairingApi = {
    listPairings: async () => {
      if (unavailable) throw new RoomPairingApiError({ kind: 'unavailable', code: 'not_found' });
      return rows;
    },
    createOffer: async () => ({ id: 'pair-1', code: 'mcpair1.offer.MOCK_PUBLIC_OFFER', expires_at: expiry() }),
    acceptOffer: async () => ({
      id: 'pair-1', code: 'mcpair1.reveal.MOCK_PUBLIC_REVEAL', sas: '482913', expires_at: expiry(),
    }),
    confirmOffer: async () => confirmed('host'),
    confirmImport: async () => confirmed('joining'),
    importOffer: async () => ({
      id: 'pair-1', code: 'mcpair1.accept.MOCK_PUBLIC_ACCEPT', identity: peer, expires_at: expiry(),
    }),
    revealImport: async () => ({ id: 'pair-1', sas: '482913', expires_at: expiry() }),
    cancelOffer: async () => undefined,
    removePeer: async (id) => { rows = rows.filter((row) => row.peer?.peer_id !== id); },
  };
  const scope = {
    room_id: room.room_id, member_id: 'basa', permissions: room.permissions,
    catalog: { tools: ['windows-build', 'signtool-verify'], approval: 'manual' },
    policy_digest: 'sha256:mock-policy-v1',
  };
  let consented = false;
  const joiningApi: RoomJoiningApi = {
    preview: async () => ({ id: 'import-1', status: 'preview', host: { installation_id: 'albi-mbp', profile: 'Albi' } }),
    metadata: async () => ({ id: 'import-1', status: 'metadata', metadata: { name: room.name, expires_at: expiry() } }),
    redeem: async () => ({ id: 'import-1', status: 'awaiting_host' }),
    getImport: async (): Promise<JoiningImport> => consented
      ? { id: 'import-1', status: 'active', room_id: room.room_id }
      : { id: 'import-1', status: 'scope', scope },
    confirmScope: async () => ({ id: 'import-1', status: 'consent' }),
    consent: async () => { consented = true; return { id: 'import-1', status: 'activating' }; },
    resolve: async () => ({ id: 'import-1', status: 'active', room_id: room.room_id }),
  };
  const event = (seq: number, actor: string, text: string) => ({
    seq, event_id: `evt-${seq}`, kind: 'message', actor, authority_epoch: 1,
    payload: { text }, created_at: `2026-10-24T14:0${seq}:00+02:00`,
  });
  const events = [event(1, 'Albi', t('inviteUi.mockMessage1')), event(2, 'Basa', t('inviteUi.mockMessage2'))];
  let approvals = [{ request_id: 'approval-1', title: t('inviteUi.mockApproval'), detail: t('inviteUi.mockApprovalDetail') }];
  const guestApi: RoomGuestApi = {
    listRooms: async () => [room],
    getMyBot: async () => ({ status: 'online', approvals, permissions: room.permissions }),
    getLog: async (_id, options) => ({
      events: options?.sinceSeq ? [events[1], event(3, 'Albi', t('inviteUi.mockMessage3'))] : events,
      cursor: null, latest_seq: 3, has_more: !options?.sinceSeq, authority: { gateway_id: 'mock', epoch: 1 },
    }),
    decideApproval: async (_room, id) => { approvals = approvals.filter((item) => item.request_id !== id); },
    leaveRoom: async () => undefined,
  };
  return { room, pairingApi, joiningApi, guestApi };
}

type MockScreen = 'pairing' | 'invitation' | 'guest';
export function InviteRoomsMock() {
  const { t } = useI18n();
  const params = new URLSearchParams(window.location.search);
  const [screen, setScreen] = useState<MockScreen>(() => {
    const requested = params.get('screen');
    return requested === 'guest' || requested === 'invitation' ? requested : 'pairing';
  });
  const offline = params.get('offline') === '1';
  const unavailable = params.get('feature') === 'off';
  const fixtures = useMemo(() => createFixtures(t, offline, unavailable), [t, offline, unavailable]);

  return (
    <main className="invite-mock-page">
      <header className="invite-mock-header">
        <div>
          <p className="eyebrow">Mission Control · {t('inviteUi.mock')}</p>
          <h1 className="mt-1 text-lg font-semibold text-text">{t('inviteUi.title')}</h1>
        </div>
        <nav aria-label={t('inviteUi.title')}>
          <SegmentedControl value={screen} onChange={setScreen} label={t('inviteUi.title')} options={[
            { value: 'pairing', label: t('inviteUi.mockPairing') },
            { value: 'invitation', label: t('inviteUi.invitations') },
            { value: 'guest', label: t('inviteUi.mockGuest') },
          ]} />
        </nav>
      </header>
      <div className="invite-mock-content">
        {screen === 'pairing' ? <PairingSection api={fixtures.pairingApi} /> : null}
        {screen === 'invitation' && !unavailable ? (
          <ReceivedInvitationPanel api={fixtures.joiningApi} onOpenRoom={() => setScreen('guest')} />
        ) : null}
        {screen === 'guest' && !unavailable ? (
          <GuestRoomView room={fixtures.room} api={fixtures.guestApi} onLeave={() => setScreen('pairing')} />
        ) : null}
      </div>
    </main>
  );
}
