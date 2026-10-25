import assert from 'node:assert/strict';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { JSDOM } from 'jsdom';
import React, { act } from 'react';
import { createServer } from 'vite';
import react from '@vitejs/plugin-react';

// Real mounted components, React effects, event handlers and canonical i18n. Only the API is injected.
const dom = new JSDOM('<!doctype html><button id="outside">Outside</button><div id="root"></div>', {
  url: 'http://localhost/', pretendToBeVisual: true,
});
for (const key of ['window', 'document', 'HTMLElement', 'HTMLInputElement', 'HTMLTextAreaElement', 'Event', 'KeyboardEvent']) {
  globalThis[key] = dom.window[key];
}
Object.defineProperty(globalThis, 'navigator', { configurable: true, value: dom.window.navigator });
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
window.HTMLElement.prototype.getClientRects = function () { return [{ width: 44, height: 44 }]; };
const { createRoot } = await import('react-dom/client');
const cacheDir = mkdtempSync(path.join(tmpdir(), 'invite-ui-mounted-'));
const server = await createServer({
  root: process.cwd(), configFile: false, cacheDir, appType: 'custom', logLevel: 'silent',
  server: { middlewareMode: true, hmr: false }, plugins: [react()],
});
const settle = () => new Promise((resolve) => setImmediate(resolve));
async function flush(action = () => {}) {
  await act(async () => { await action(); for (let i = 0; i < 8; i++) await settle(); });
}
let root;
let assertions = 0;
function check(actual, expected, message) { assert.deepEqual(actual, expected, message); assertions++; }
function button(text) {
  const found = [...document.querySelectorAll('button')].find((node) => node.textContent.trim() === text);
  assert.ok(found, `Missing button: ${text}`);
  return found;
}
async function click(text) {
  const node = button(text);
  assert.equal(node.disabled, false, `${text} enabled`);
  await flush(() => node.click());
}
async function type(selector, value) {
  const node = document.querySelector(selector);
  assert.ok(node, selector);
  const proto = node.tagName === 'TEXTAREA' ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
  await flush(() => {
    Object.getOwnPropertyDescriptor(proto, 'value').set.call(node, value);
    node.dispatchEvent(new Event('input', { bubbles: true }));
  });
}
try {
  const { I18nProvider } = await server.ssrLoadModule('/src/lib/i18n.tsx');
  const { PairingSection } = await server.ssrLoadModule('/src/components/chat/PairingSection.tsx');
  const { ReceivedInvitationPanel } = await server.ssrLoadModule('/src/components/chat/ReceivedInvitationPanel.tsx');
  const { GuestRoomView } = await server.ssrLoadModule('/src/components/chat/GuestRoomView.tsx');
  const { BottomSheet } = await server.ssrLoadModule('/src/components/ui/BottomSheet.tsx');
  const { SegmentedControl } = await server.ssrLoadModule('/src/components/ui/SegmentedControl.tsx');
  const { useRoomInvitesAvailable } = await server.ssrLoadModule('/src/lib/use-room-invites-available.ts');
  async function mount(Component, props = {}) {
    if (root) await flush(() => root.unmount());
    root = createRoot(document.getElementById('root'));
    await flush(() => root.render(React.createElement(I18nProvider, null, React.createElement(Component, props))));
  }
  const calls = [];
  const expiry = Math.floor(Date.now() / 1000) + 600;
  const row = { id: 'p', role: 'host', state: 'confirmed', expires_at: expiry };
  const pairingApi = {
    listPeers: async () => [],
    createOffer: async () => ({ id: 'p', code: 'offer-once', expires_at: expiry }),
    acceptOffer: async (...args) => { calls.push(['accept', ...args]); return { id: 'p', code: 'reveal-once', sas: '000123' }; },
    confirmOffer: async (...args) => { calls.push(['confirmHost', ...args]); return row; },
    importOffer: async () => ({ id: 'p', code: 'accept-once', identity: { profile: 'Basa', installation_id: 'peer' }, expires_at: expiry }),
    revealImport: async (...args) => { calls.push(['reveal', ...args]); return { id: 'p', sas: '000123' }; },
    confirmImport: async (...args) => { calls.push(['confirmJoining', ...args]); return row; },
    cancelOffer: async (id) => calls.push(['cancel', id]),
  };
  await mount(PairingSection, { api: pairingApi });
  await click('Start pairing');
  check(document.querySelector('[role=dialog]') !== null, true, 'code opens real sheet');
  check(document.body.style.overflow, 'hidden', 'scroll locked');
  check(document.body.textContent.includes('offer-once'), true, 'offer shown');
  await flush(() => document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })));
  check(document.querySelector('[role=dialog]'), null, 'Escape closes code sheet');
  check(document.body.style.overflow, '', 'scroll lock restored');
  await type('textarea', 'accept-from-peer');
  await click('Review and continue');
  check(calls[0], ['accept', 'p', 'accept-from-peer'], 'correct host accept endpoint');
  check(document.body.textContent.includes('reveal-once'), true, 'third code shown');
  await flush(() => document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })));
  await click('Reveal sent — compare SAS');
  check(button('Confirm pairing').disabled, true, 'SAS required');
  await type('input[inputmode=numeric]', '12345a');
  check(button('Confirm pairing').disabled, true, 'non-digits rejected');
  await type('input[inputmode=numeric]', '000123');
  await click('Confirm pairing');
  check(calls.at(-1), ['confirmHost', 'p', '000123'], 'host confirmation');
  check(document.querySelector('input[inputmode=numeric]'), null, 'SAS cleared on completion');
  check(document.body.textContent.includes('not ready until both owners'), true, 'confirmation is not operational ack');

  await mount(PairingSection, { api: pairingApi });
  await type('input', 'offer-from-peer');
  await click('Use code');
  await flush(() => document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })));
  await type('textarea', 'reveal-from-peer');
  await click('Review and continue');
  check(calls.at(-1), ['reveal', 'p', 'reveal-from-peer'], 'joining reveals instead of host accept');
  await type('input[inputmode=numeric]', '000123');
  await click('Confirm pairing');
  check(calls.at(-1), ['confirmJoining', 'p', '000123'], 'joining confirms correct role');

  await mount(PairingSection, { api: pairingApi });
  await click('Start pairing');
  await flush(() => document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })));
  await click('Cancel');
  check(calls.at(-1), ['cancel', 'p'], 'cancel reaches API');
  check(document.body.textContent.includes('offer-once'), false, 'cancel clears code');
  await mount(PairingSection, { api: { ...pairingApi, listPeers: async () => { throw { problem: { kind: 'unavailable' } }; } } });
  check(document.querySelector('[aria-labelledby=pairing-title]'), null, '404 hides section');
  await mount(PairingSection, { api: { ...pairingApi, listPeers: async () => { throw { problem: { kind: 'forbidden', code: 'denied' } }; } } });
  check(document.querySelector('[role=alert]').textContent.includes('denied'), true, '403 is visible generic error');

  const scope = { room_id: 'room', member_id: 'bot', permissions: ['build'], catalog: { tools: ['build'] } };
  let consent = false;
  const joiningCalls = [];
  const joiningApi = {
    preview: async () => ({ id: 'i', status: 'preview', host: { profile: 'Albi', installation_id: 'paired' } }),
    metadata: async () => ({
      id: 'i', status: 'metadata', metadata: { name: 'Release', expires_at: new Date(expiry * 1000).toISOString() },
    }),
    redeem: async () => { joiningCalls.push('redeem'); return { id: 'i', status: 'awaiting_host' }; },
    getImport: async () => consent ? { id: 'i', status: 'active', room_id: 'room' } : { id: 'i', status: 'scope', scope },
    confirmScope: async (_id, body) => { joiningCalls.push(body); return { id: 'i', status: 'consent' }; },
    consent: async (_id, approve) => { joiningCalls.push(['consent', approve]); consent = true; return { id: 'i', status: 'activating' }; },
  };
  let opened;
  await mount(ReceivedInvitationPanel, { api: joiningApi, onOpenRoom: (id) => { opened = id; } });
  check(document.body.textContent.includes('Give consent'), false, 'no early consent button');
  await type('textarea', 'invitation');
  await click('Preview invitation');
  check(document.body.textContent.includes('paired'), true, 'paired identity presented');
  await click('Read room details');
  await click('Review details and continue');
  await click('Redeem invitation');
  check(document.body.textContent.includes('room'), true, 'scope returned by polling');
  check(joiningCalls, ['redeem'], 'no implicit scope or consent');
  await click('Confirm this exact scope');
  check(joiningCalls.at(-1), { room_id: 'room', member_id: 'bot' }, 'exact scope submitted');
  check(consent, false, 'confirmation is separate from consent');
  await click('Give consent');
  check(joiningCalls.at(-1), ['consent', true], 'explicit consent only');
  await click('Open room');
  check(opened, 'room', 'opens canonical returned room id');

  const room = { room_id: 'room', name: 'Release', owner: 'Albi', owner_online: false, owner_offline_since: '14:02', members: [], permissions: [] };
  const logCalls = [];
  const event = (seq) => ({ seq, event_id: `e${seq}`, actor: 'Basa', payload: { text: `Message ${seq}` }, created_at: '', kind: 'message' });
  let approvalChoice;
  const guestApi = {
    getMyBot: async () => ({ status: 'online', permissions: ['build'], approvals: [{ request_id: 'req', title: 'Build release' }] }),
    getLog: async (_id, options) => {
      logCalls.push(options);
      return { events: options.sinceSeq ? [event(2), event(3)] : [event(2), event(1), event(2)], has_more: !options.sinceSeq };
    },
    decideApproval: async (_id, _request, choice) => { approvalChoice = choice; },
    leaveRoom: async () => {},
  };
  await mount(GuestRoomView, { room, api: guestApi });
  check(document.querySelector('[role=tab][aria-selected=true]').textContent, 'My bot', 'bot default');
  check(document.querySelector('[role=alert]').textContent.includes('14:02'), true, 'offline alert includes time');
  check(document.querySelector('textarea'), null, 'no composer');
  await click('Reject');
  check(approvalChoice, 'reject', 'real approval handler');
  check(document.body.textContent.includes('Build release'), false, 'decision removes request');
  await click('Chat');
  check([...document.querySelectorAll('[data-seq]')].map((node) => node.dataset.seq), ['1', '2'], 'ordered deduplicated rows');
  await click('Load next events');
  check(logCalls.at(-1), { sinceSeq: 2, limit: 100 }, 'pagination uses max received sequence, not latest_seq');
  check([...document.querySelectorAll('[data-seq]')].map((node) => node.dataset.seq), ['1', '2', '3'], 'page overlap dedup');
  check(document.querySelector('textarea, [contenteditable=true]'), null, 'still read-only after pagination');

  function Availability({ available }) {
    const enabled = useRoomInvitesAvailable(true, undefined, {
      listPairedPeers: async () => { if (!available) throw { problem: { kind: 'unavailable' } }; return []; },
    });
    return React.createElement('div', { 'data-feature': enabled }, enabled ? 'visible' : 'hidden');
  }
  // Keep API identity stable so probes do not become a render loop.
  const unavailableApi = { listPairedPeers: async () => { throw { problem: { kind: 'unavailable' } }; } };
  function Off() {
    const enabled = useRoomInvitesAvailable(true, undefined, unavailableApi);
    return enabled ? React.createElement('button', null, 'Invitations') : null;
  }
  await mount(Off);
  check(document.getElementById('root').textContent, '', 'actual availability hook hides feature on 404');

  let selected;
  await mount(SegmentedControl, { value: 'bot', label: 'Views', options: [{ value: 'bot', label: 'My bot' }, { value: 'chat', label: 'Chat' }], onChange: (value) => { selected = value; } });
  const tabs = document.querySelectorAll('[role=tab]');
  await flush(() => tabs[0].dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true })));
  check(selected, 'chat', 'arrow changes selection');
  check(document.activeElement, tabs[1], 'arrow moves focus');
  await flush(() => tabs[1].dispatchEvent(new KeyboardEvent('keydown', { key: 'Home', bubbles: true })));
  check(selected, 'bot', 'Home selects first');

  const outside = document.getElementById('outside');
  outside.focus();
  await mount(BottomSheet, { open: true, title: 'Sheet', onClose() {}, children: React.createElement('button', null, 'Inside') });
  check(document.activeElement.getAttribute('role'), 'dialog', 'focus enters dialog');
  outside.focus();
  check(document.activeElement.getAttribute('role'), 'dialog', 'focus cannot escape');
  await flush(() => document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab', bubbles: true, cancelable: true })));
  check(document.activeElement.getAttribute('aria-label'), 'Close', 'Tab enters first control');
  await flush(() => document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab', shiftKey: true, bubbles: true, cancelable: true })));
  check(document.activeElement.textContent, 'Inside', 'Shift+Tab wraps to last');
  await flush(() => root.unmount());
  root = null;
  check(document.body.style.overflow, '', 'unmount unlocks body');
  check(document.activeElement, outside, 'unmount restores focus');
  console.log(`Mounted invite rooms: ${assertions} behavioral assertions passed`);
} finally {
  if (root) await flush(() => root.unmount());
  await server.close();
  rmSync(cacheDir, { recursive: true, force: true });
  dom.window.close();
}
