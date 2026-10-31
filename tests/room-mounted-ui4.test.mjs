import assert from 'node:assert/strict';
import test from 'node:test';
import { mkdtempSync, rmSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { JSDOM } from 'jsdom';
import React, { act } from 'react';
import { createServer } from 'vite';
import react from '@vitejs/plugin-react';

const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: 'http://localhost/', pretendToBeVisual: true });
for (const key of ['window', 'document', 'HTMLElement', 'HTMLInputElement', 'HTMLTextAreaElement', 'Event', 'KeyboardEvent']) {
  globalThis[key] = dom.window[key];
}
Object.defineProperty(globalThis, 'navigator', { configurable: true, value: dom.window.navigator });
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
window.HTMLElement.prototype.getClientRects = () => [{ width: 44, height: 44 }];
const { createRoot } = await import('react-dom/client');
const cacheDir = mkdtempSync(path.join(tmpdir(), 'invite-ui4-'));
const server = await createServer({ root: process.cwd(), configFile: false, cacheDir, appType: 'custom',
  logLevel: 'silent', server: { middlewareMode: true, hmr: false }, plugins: [react()] });
const { I18nProvider } = await server.ssrLoadModule('/src/lib/i18n.tsx');
const { PairingSection } = await server.ssrLoadModule('/src/components/chat/PairingSection.tsx');
const { GuestRoomView } = await server.ssrLoadModule('/src/components/chat/GuestRoomView.tsx');
let root;
async function flush(action = () => {}) {
  await act(async () => { await action(); for (let i = 0; i < 8; i++) await new Promise(setImmediate); });
}
async function mount(Component, props) {
  if (root) await flush(() => root.unmount());
  root = createRoot(document.getElementById('root'));
  await flush(() => root.render(React.createElement(I18nProvider, null, React.createElement(Component, props))));
}
function button(label) {
  const node = [...document.querySelectorAll('button')].find((node) => node.textContent.trim() === label);
  assert.ok(node, label);
  return node;
}
async function click(label) { await flush(() => button(label).click()); }
async function type(selector, value) {
  const node = document.querySelector(selector);
  assert.ok(node, selector);
  const proto = node.tagName === 'TEXTAREA' ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
  await flush(() => {
    Object.getOwnPropertyDescriptor(proto, 'value').set.call(node, value);
    node.dispatchEvent(new Event('input', { bubbles: true }));
  });
}
async function closeSheet() {
  await flush(() => document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })));
}
const room = { room_id: 'r', name: 'Release', owner: 'Host', owner_online: true, members: [], permissions: ['build'] };
const event = { seq: 1, event_id: 'e1', kind: 'message', actor: 'Host', payload: { text: 'Canonical log survives 501' } };
const guestApi = {
  getMyBot: async () => { throw { problem: { kind: 'error', code: 'not_supported' } }; },
  getLog: async () => ({ events: [event], cursor: null, latest_seq: 1, has_more: false }),
};
const expires_at = Math.floor(Date.now() / 1000) + 600;
const peer = { peer_id: 'p', installation_id: 'installation', profile: 'Basa', state: 'acknowledged', role: 'host', generation: 1 };
const pairingApi = {
  listPeers: async () => [peer], listPairings: async () => [],
  createOffer: async () => ({ id: 'p', code: 'public-offer', expires_at }),
  acceptOffer: async () => ({ id: 'p', code: 'public-reveal', sas: '001234', expires_at }),
};
try {
  await test('501 shows honest unavailable guidance, no fake actions, no stuck loading, and preserves Chat', async () => {
    await mount(GuestRoomView, { room, api: guestApi });
    assert.match(document.body.textContent, /Room approvals are currently unavailable in Mission Control\./);
    assert.equal(document.querySelector('.guest-approval button') === null, true);
    assert.equal(document.querySelector('[role=alert]'), null);
    assert.doesNotMatch(document.querySelector('.guest-bot-panel header').textContent, /Loading/);
    await click('Chat');
    assert.match(document.body.textContent, /Canonical log survives 501/);
    assert.equal(document.querySelector('textarea, [contenteditable=true]'), null);
  });
  await test('populated approvals show real Approve/Reject handlers with Reject secondary', async () => {
    let decision;
    await mount(GuestRoomView, { room, api: { ...guestApi,
      getMyBot: async () => ({ status: 'online', approvals: [{ request_id: 'q', title: 'Build' }], permissions: ['build'] }),
      decideApproval: async (...args) => { decision = args; },
    } });
    assert.equal(button('Approve').disabled, false);
    assert.equal(button('Reject').disabled, false);
    assert.doesNotMatch(button('Reject').className, /bg-negative/);
    assert.ok(button('Reject').closest('.guest-approval-actions'));
    await click('Approve');
    assert.deepEqual(decision.slice(0, 3), ['r', 'q', 'approve']);
    assert.equal(document.querySelector('.guest-approval'), null);
  });
  await test('501 after an approval decision removes stale actions and shows guidance', async () => {
    await mount(GuestRoomView, { room, api: { ...guestApi,
      getMyBot: async () => ({ status: 'online', approvals: [{ request_id: 'q', title: 'Build' }], permissions: [] }),
      decideApproval: async () => { throw { problem: { kind: 'error', code: 'not_supported' } }; },
    } });
    await click('Reject');
    assert.equal(document.querySelector('.guest-approval button') === null, true);
    assert.match(document.body.textContent, /Room approvals are currently unavailable in Mission Control\./);
  });
  await test('log failure is visible but does not discard independently loaded bot approvals', async () => {
    await mount(GuestRoomView, { room, api: {
      getMyBot: async () => ({ status: 'online', approvals: [{ request_id: 'q', title: 'Build' }], permissions: [] }),
      getLog: async () => { throw { problem: { kind: 'error', code: 'host_offline' } }; },
    } });
    assert.equal(button('Approve').disabled, false);
    assert.match(document.querySelector('[role=alert]').textContent, /host_offline/);
  });
  await test('active peer list and unpair server notice (journal may be empty)', async () => {
    const notice = 'Does not revoke native grants or stop already admitted runs; remote records may remain stale.';
    let removed;
    await mount(PairingSection, { api: { ...pairingApi, removePeer: async (id) => {
      removed = id; return { unpaired: true, peer_id: id, generation: 2, notice };
    } } });
    assert.ok(document.querySelector('.invite-peer'), 'GET /peers must render the active peer');
    assert.match(document.querySelector('.invite-peer').textContent, /Basa/);
    await click('Remove');
    assert.equal(removed, 'p');
    assert.equal(document.querySelector('.invite-peer'), null);
    assert.match(document.querySelector('[role=status]').textContent, new RegExp(notice));
  });
  await test('epoch expiry gives a finite live countdown and SAS mobile attributes accept only six ASCII digits', async () => {
    await mount(PairingSection, { api: pairingApi });
    await click('Start pairing');
    await closeSheet();
    assert.ok(document.querySelector('[role=timer]'), 'integer epoch must keep the pairing session live');
    assert.doesNotMatch(document.querySelector('[role=timer]').textContent, /NaN/);
    await type('textarea', 'accept');
    await click('Review and continue');
    await closeSheet();
    await click('Reveal sent — compare SAS');
    const input = document.querySelector('input[inputmode=numeric]');
    assert.equal(input.getAttribute('pattern'), '[0-9]*');
    assert.equal(input.autocomplete, 'one-time-code');
    assert.equal(input.maxLength, 6);
    await type('input[inputmode=numeric]', '12a345678');
    assert.match(input.value, /^[0-9]{0,6}$/);
    await type('input[inputmode=numeric]', '12345');
    assert.equal(button('Confirm pairing').disabled, true);
    await type('input[inputmode=numeric]', '001234');
    assert.equal(button('Confirm pairing').disabled, false);
  });
  await test('SAS input has numeric autofill and rejects non-digits independently of the epoch countdown', async () => {
    const legacyExpiry = Math.floor(Date.now() / 1000) + 600;
    await mount(PairingSection, { api: { ...pairingApi,
      createOffer: async () => ({ id: 'p', code: 'offer', expires_at: legacyExpiry }),
    } });
    await click('Start pairing');
    await closeSheet();
    await type('textarea', 'accept');
    await click('Review and continue');
    await closeSheet();
    await click('Reveal sent — compare SAS');
    const input = document.querySelector('input[inputmode=numeric]');
    assert.equal(input.getAttribute('pattern'), '[0-9]*');
    assert.equal(input.autocomplete, 'one-time-code');
    await type('input[inputmode=numeric]', '12a345678');
    assert.match(input.value, /^[0-9]{0,6}$/);
  });
  await test('unpair notice survives removal even with a legacy journal fixture', async () => {
    await mount(PairingSection, { api: { ...pairingApi,
      listPairings: async () => [{ id: 'p', peer, state: 'acknowledged' }],
      removePeer: async () => ({ unpaired: true, peer_id: 'p', generation: 2, notice: 'Native grants are not revoked.' }),
    } });
    await click('Remove');
    assert.match(document.body.textContent, /Native grants are not revoked\./);
  });
  await test('cooldown has dedicated English and Italian copy', async () => {
    const api = { ...pairingApi, createOffer: async () => { throw { problem: { code: 'pairing_cooldown', kind: 'error' } }; } };
    await mount(PairingSection, { api });
    await click('Start pairing');
    assert.match(document.querySelector('[role=alert]').textContent, /Too many failed comparisons/);
    for (const locale of ['en', 'it']) {
      const strings = JSON.parse(readFileSync(`src/locales/${locale}.json`, 'utf8'));
      assert.ok(strings['inviteUi.pairingCooldown']);
      assert.ok(strings['inviteUi.approvalsUnavailable']);
    }
  });
  await test('mobile CSS defines stacking, separation, safe area and clickable inactive tabs', () => {
    const css = readFileSync('src/invite-rooms.css', 'utf8');
    assert.match(css, /\.guest-approval-actions\s*\{[^}]*gap:\s*12px/s);
    assert.match(css, /@media\s*\(max-width:\s*419px\)[\s\S]*\.guest-approval-actions[^}]*grid-template-columns:\s*1fr/);
    assert.match(css, /\.guest-disclaimer\s*\{[^}]*padding-bottom:\s*env\(safe-area-inset-bottom\)/s);
    assert.match(css, /\.guest-panel-section\s*\{[^}]*border-top:/s);
    assert.match(css, /\.invite-segments button\[aria-selected='false'\][^}]*cursor:\s*pointer/s);
  });
} finally {
  if (root) await flush(() => root.unmount());
  await server.close();
  rmSync(cacheDir, { recursive: true, force: true });
  dom.window.close();
}
