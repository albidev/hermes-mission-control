import React, {useState} from 'react';
import {createRoot} from 'react-dom/client';
import {I18nProvider} from '../../../src/lib/i18n';
import {RoomInvitePanel} from '../../../src/components/chat/RoomInvitePanel';
import {RoomInviteApiError} from '../../../src/lib/room-invite-api';
import '../../../src/styles.css';
import '../../../src/invite-rooms.css';

const state = { mode: 'normal', seq: 0, copies: [], fallbackCalls: [] };
const lobby = id => ({ lobby_id: id, name: 'Long lobby title '.repeat(12), state: 'open', roster: [
  { member_id: 'owner', handle: 'owner', profile: 'default' },
  { member_id: 'peer-b', handle: 'Synthetic peer', profile: 'default', target: { peer_id: 'peer-b', profile: 'synthetic-profile', capability_digest: 'd'.repeat(96) } },
] });
const api = {
  getLobby: async id => lobby(id),
  getActivation: async id => ({ lobby_id: id, lobby_state: 'open', ready: false, peers: [] }),
  issueInvite: async id => {
    if (state.mode === 'missing') throw new RoomInviteApiError({ kind: 'ambiguous', code: 'invite_origin_unavailable', status: 503 });
    const inviteId = `invite-${String(++state.seq).padStart(32, '0')}`;
    const secret = 'A'.repeat(43);
    return { invite_id: inviteId, secret, invite_url: `https://invite.example.test/invite/${id}/${inviteId}#${secret}`, expires_at: '2030-01-01' };
  },
};
Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: async text => state.copies.push(text) } });
document.execCommand = command => { state.fallbackCalls.push(command); return true; };
function App() {
  const [id, setId] = useState('lobby-' + '1'.repeat(32));
  const [owner, setOwner] = useState('owner');
  const [visible, setVisible] = useState(true);
  window.__roomInviteFixture = { state, switchLobby: () => setId('lobby-' + '2'.repeat(32)), switchOwner: () => setOwner('owner-2'), unmount: () => setVisible(false), mount: () => setVisible(true) };
  return <main style={{height:'100dvh',width:'100%',overflowY:'auto',padding:8}}>{visible && <RoomInvitePanel lobbyId={id} accessToken={owner} api={api} onClose={() => setVisible(false)} />}</main>;
}
createRoot(document.getElementById('root')).render(<I18nProvider><App /></I18nProvider>);
