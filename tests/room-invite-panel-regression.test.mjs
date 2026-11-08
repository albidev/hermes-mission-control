import assert from 'node:assert/strict';
import { test } from 'node:test';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { JSDOM } from 'jsdom';
import React, { act, useState } from 'react';
import { createServer } from 'vite';
import react from '@vitejs/plugin-react';

const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: 'http://localhost/', pretendToBeVisual: true });
for (const key of ['window', 'document', 'HTMLElement', 'HTMLInputElement', 'HTMLTextAreaElement', 'Event', 'KeyboardEvent', 'MutationObserver', 'DOMException']) globalThis[key] = dom.window[key];
Object.defineProperty(globalThis, 'navigator', { configurable: true, value: dom.window.navigator });
globalThis.localStorage = dom.window.localStorage;
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
window.HTMLElement.prototype.getClientRects = function () { return [{ width: 44, height: 44 }]; };
const { createRoot } = await import('react-dom/client');
const cacheDir = mkdtempSync(path.join(tmpdir(), 'invite-panel-regression-'));
const server = await createServer({ root: process.cwd(), configFile: false, cacheDir, appType: 'custom', logLevel: 'silent', server: { middlewareMode: true, hmr: false }, plugins: [react()] });
const flush = (action = () => {}) => act(async () => { await action(); for (let i = 0; i < 8; i++) await new Promise(resolve => setImmediate(resolve)); });
let root;
const byText = (selector, text) => [...document.querySelectorAll(selector)].find(node => node.textContent.trim() === text);
const button = (text) => { const node = byText('button', text); assert.ok(node, `Missing button: ${text}`); return node; };
const click = (text) => flush(() => { const node = button(text); assert.equal(node.disabled, false, `${text} disabled`); node.click(); });
const settle = () => flush();

try {
  const { I18nProvider } = await server.ssrLoadModule('/src/lib/i18n.tsx');
  const { RoomInvitePanel } = await server.ssrLoadModule('/src/components/chat/RoomInvitePanel.tsx');
  const { RoomInviteApiError, createRoomInviteApi } = await server.ssrLoadModule('/src/lib/room-invite-api.ts');
  const oldFetch = async () => new Response(JSON.stringify({ invite_id: 'old-invite', secret: 'B'.repeat(43), expires_at: '2030-01-01' }), { status: 200, headers: { 'Content-Type': 'application/json' } });
  const oldApi = createRoomInviteApi(undefined, oldFetch);
  const state = { mode: 'normal', pending: [], copyPending: [], fallbackCalls: [], insertions: [], copies: [], observers: [], seq: 0 };
  const lobby = (id) => ({ lobby_id: id, name: 'Synthetic long lobby name '.repeat(4), state: 'open', roster: [
    { member_id: 'owner', handle: 'owner', profile: 'default' },
    { member_id: 'peer-b', handle: 'synthetic-peer', display_name: 'Synthetic peer', profile: 'default', target: { peer_id: 'peer-b', profile: 'default', capability_digest: 'c'.repeat(64) } },
  ] });
  const issuePayload = (id) => { const seq = ++state.seq; const inviteId = `invite-${String(seq).padStart(32, '0')}`; const secret = String.fromCharCode(65 + seq % 26).repeat(43); return { invite_id: inviteId, secret, invite_url: `https://invite.example.test/invite/${id}/${inviteId}#${secret}`, expires_at: '2030-01-01' }; };
  const makeApi = () => ({
    getLobby: async id => lobby(id),
    getActivation: async id => ({ lobby_id: id, lobby_state: 'open', ready: false, peers: [] }),
    issueInvite: async id => {
      if (state.mode === 'hold') await new Promise(resolve => state.pending.push(resolve));
      if (state.mode === 'old') return oldApi.issueInvite(id, { member_id: 'peer-b', invitee_label: 'legacy' });
      if (state.mode === 'missing') throw new RoomInviteApiError({ kind: 'ambiguous', code: 'invite_origin_unavailable', status: 503 });
      return issuePayload(id);
    },
  });
  const apiA = makeApi();
  const apiB = makeApi();
  function App() {
    const [id, setId] = useState('lobby-' + '1'.repeat(32));
    const [owner, setOwner] = useState('owner-a');
    const [api, setApi] = useState(apiA);
    const [visible, setVisible] = useState(true);
    globalThis.__inviteControl = { switchLobby: () => setId('lobby-' + '2'.repeat(32)), switchOwner: () => setOwner(value => value === 'owner-a' ? 'owner-b' : 'owner-a'), switchApi: () => setApi(value => value === apiA ? apiB : apiA), unmount: () => setVisible(false), mount: () => setVisible(true), clear: () => document.querySelector('button.min-h-11')?.click() };
    return visible ? React.createElement(I18nProvider, null, React.createElement(RoomInvitePanel, { lobbyId: id, accessToken: owner, api, onClose: () => setVisible(false) })) : null;
  }
  const mount = async (locale = 'en') => {
    localStorage.setItem('mission-control-locale', locale);
    state.mode = 'normal'; state.pending.length = 0; state.copyPending.length = 0; state.fallbackCalls.length = 0; state.insertions.length = 0; state.copies.length = 0;
    if (root) await flush(() => root.unmount());
    root = createRoot(document.getElementById('root'));
    await flush(() => root.render(React.createElement(App)));
    await settle();
    const input = document.querySelector('input[aria-label]');
    assert.ok(input, 'actual RoomInvitePanel mounted');
    await flush(() => { Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(input, 'Synthetic recipient'); input.dispatchEvent(new Event('input', { bubbles: true })); });
  };
  const setupClipboard = ({ native = 'reject', fallback = true } = {}) => {
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: async value => { state.copies.push(value); if (native === 'hold') await new Promise((resolve, reject) => state.copyPending.push({ resolve, reject })); if (native === 'reject') throw new Error('denied'); } } });
    document.execCommand = command => { state.fallbackCalls.push(command); if (fallback === 'throw') throw new Error('fallback failure'); return fallback; };
    state.observers.forEach(observer => observer.disconnect()); state.observers.length = 0;
    const observer = new MutationObserver(records => records.flatMap(record => [...record.addedNodes]).filter(node => node instanceof HTMLTextAreaElement).forEach(() => state.insertions.push(true))); observer.observe(document.body, { childList: true }); state.observers.push(observer);
  };
  const copyLabel = () => localStorage.getItem('mission-control-locale') === 'it' ? 'Copia link invito' : 'Copy invitation link';
  const issueLabel = () => localStorage.getItem('mission-control-locale') === 'it' ? 'Emetti invito' : 'Issue invite';
  const issue = async () => { await click(issueLabel()); await settle(); assert.ok(document.querySelector('.invite-code')); };
  const invalidate = async (action) => { if (action === 'clear') { const clear = [...document.querySelectorAll('button')].find(node => node.textContent.includes('Clear secret')); assert.ok(clear); await flush(() => clear.click()); } else if (action === 'reissue') { await click(issueLabel()); } else if (action === 'unmount') { await flush(() => globalThis.__inviteControl.unmount()); } else { await flush(() => globalThis.__inviteControl[action]()); } await settle(); };

  await test('actual panel current-scope fallback succeeds, fails, throws and cleans textarea', async () => {
    await mount(); setupClipboard({ native: 'reject', fallback: true }); await issue(); await click(copyLabel()); await settle(); assert.equal(state.fallbackCalls.length, 1); assert.equal(state.insertions.length, 1); assert.equal(document.querySelectorAll('body > textarea').length, 0); assert.match(document.body.textContent, /Invitation copied/);
    setupClipboard({ native: 'reject', fallback: false }); await flush(() => document.querySelector('.invite-issue .chat-control').click()); await settle(); assert.equal(document.querySelectorAll('body > textarea').length, 0); assert.match(document.body.textContent, /Copy failed/);
    setupClipboard({ native: 'reject', fallback: 'throw' }); await flush(() => document.querySelector('.invite-issue .chat-control').click()); await settle();
  });
  for (const action of ['switchLobby', 'switchOwner', 'switchApi', 'clear', 'reissue', 'unmount']) {
    for (const outcome of ['resolve', 'reject']) {
      await test(`held clipboard rejection after ${action} has no stale fallback (${outcome})`, async () => {
        await mount(); setupClipboard({ native: 'hold', fallback: true }); await issue(); await click(copyLabel()); assert.equal(state.copyPending.length, 1);
        await invalidate(action);
        const pending = state.copyPending.shift(); assert.ok(pending); outcome === 'resolve' ? pending.resolve() : pending.reject(new Error('late rejection')); await settle();
        assert.equal(state.fallbackCalls.length, 0, 'stale execCommand fallback'); assert.equal(state.insertions.length, 0, 'stale textarea insertion'); assert.equal(document.querySelectorAll('body > textarea').length, 0, 'persistent fallback textarea');
      });
    }
  }
  await test('reissue resets copy state and normal native copy remains available', async () => {
    await mount(); setupClipboard({ native: 'success', fallback: true }); await issue(); await click(copyLabel()); await settle(); assert.match(document.body.textContent, /Invitation copied/);
    await click(issueLabel()); await settle(); assert.ok(document.querySelector('.invite-code')); assert.doesNotMatch(document.querySelector('.invite-issue').textContent, /Invitation copied/); await click(copyLabel()); await settle(); assert.equal(state.copies.length, 2);
  });
  for (const locale of ['en', 'it']) {
    await test(`old sidecar response is explicit localized error (${locale}) without code or button`, async () => {
      await mount(locale); const oldApiPanel = React.createElement(I18nProvider, null, React.createElement(RoomInvitePanel, { lobbyId: 'lobby-' + '1'.repeat(32), accessToken: 'owner', api: { ...apiA, issueInvite: oldApi.issueInvite }, onClose: () => {} }));
      await flush(() => root.render(oldApiPanel)); const input = document.querySelector('input[aria-label]'); await flush(() => { Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(input, 'legacy'); input.dispatchEvent(new Event('input', { bubbles: true })); }); await click(locale === 'en' ? 'Issue invite' : 'Emetti invito'); await settle();
      const alert = document.querySelector('[role=alert]'); assert.ok(alert); assert.match(alert.textContent, locale === 'en' ? /isn't compatible|can't be used|compatib/i : /compatib/i); assert.equal(document.querySelector('.invite-code'), null); assert.equal(document.querySelector('.invite-issue .chat-control'), null); assert.equal(document.body.textContent.includes('B'.repeat(43)), false);
    });
  }
} finally {
  if (root) await flush(() => root.unmount());
  await server.close(); rmSync(cacheDir, { recursive: true, force: true }); dom.window.close();
}
