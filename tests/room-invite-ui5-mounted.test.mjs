import assert from 'node:assert/strict';
import { test } from 'node:test';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { JSDOM } from 'jsdom';
import React, { act } from 'react';
import { createServer } from 'vite';
import react from '@vitejs/plugin-react';

const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: 'http://localhost/', pretendToBeVisual: true });
for (const key of ['window', 'document', 'HTMLElement', 'HTMLInputElement', 'HTMLTextAreaElement', 'Event', 'KeyboardEvent']) globalThis[key] = dom.window[key];
Object.defineProperty(globalThis, 'navigator', { configurable: true, value: dom.window.navigator });
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
window.HTMLElement.prototype.getClientRects = function () { return [{ width: 44, height: 44 }]; };
const { createRoot } = await import('react-dom/client');
const cacheDir = mkdtempSync(path.join(tmpdir(), 'invite-ui5-mounted-'));
const server = await createServer({ root: process.cwd(), configFile: false, cacheDir, appType: 'custom', logLevel: 'silent', server: { middlewareMode: true, hmr: false }, plugins: [react()] });
const flush = (action = () => {}) => act(async () => { await action(); for (let i = 0; i < 8; i++) await new Promise(resolve => setImmediate(resolve)); });
let root;
const button = text => {
  const node = [...document.querySelectorAll('button')].find(node => node.textContent.trim() === text);
  assert.ok(node, `Missing button: ${text}`);
  return node;
};
const click = text => flush(() => { const node = button(text); assert.equal(node.disabled, false); node.click(); });
const escape = () => flush(() => document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })));
async function type(selector, value) {
  const node = document.querySelector(selector);
  assert.ok(node, selector);
  await flush(() => {
    const proto = node.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, 'value').set.call(node, value);
    node.dispatchEvent(new Event('input', { bubbles: true }));
  });
}
try {
  const { I18nProvider } = await server.ssrLoadModule('/src/lib/i18n.tsx');
  const { PairingSection } = await server.ssrLoadModule('/src/components/chat/PairingSection.tsx');
  const { GuestRoomView } = await server.ssrLoadModule('/src/components/chat/GuestRoomView.tsx');
  const { InviteRoomsMock } = await server.ssrLoadModule('/src/components/chat/InviteRoomsMock.tsx');
  const { BottomSheet } = await server.ssrLoadModule('/src/components/ui/BottomSheet.tsx');
  const { RoomGuestApiError } = await server.ssrLoadModule('/src/lib/room-guest-api.ts');
  async function mount(Component, props = {}, locale = 'en') {
    if (root) await flush(() => root.unmount());
    localStorage.setItem('mission-control-locale', locale);
    root = createRoot(document.getElementById('root'));
    await flush(() => root.render(React.createElement(I18nProvider, null, React.createElement(Component, props))));
  }
  globalThis.localStorage = window.localStorage;
  const code = 'mcpair1.offer.LOCAL_TEST_ONLY';
  const expiry = Math.floor(Date.now() / 1000) + 600;
  const calls = [];
  const api = {
    listPeers: async () => [],
    createOffer: async () => { calls.push('create'); return { id: 'p', code, expires_at: expiry }; },
    acceptOffer: async (id, input) => { calls.push(['accept', id, input]); return { id, code: 'mcpair1.reveal.LOCAL_TEST_ONLY', sas: '000123' }; },
    confirmOffer: async (id, sas) => { calls.push(['confirm', id, sas]); },
    cancelOffer: async () => {},
  };
  const setClipboard = value => Object.defineProperty(navigator, 'clipboard', { configurable: true, value });
  async function open() { await mount(PairingSection, { api }); await click('Start pairing'); }
  async function progress() {
    await escape();
    await type('textarea', 'peer-accept');
    await click('Review and continue');
    await escape();
    await click('Reveal sent — compare SAS');
    await type('input[inputmode=numeric]', '000123');
    await click('Confirm pairing');
    assert.deepEqual(calls.at(-1), ['confirm', 'p', '000123']);
    assert.equal(document.body.textContent.includes('not ready until both owners'), true);
  }
  for (const scenario of ['native success', 'missing native fallback success', 'native denial fallback success', 'missing native fallback false', 'native denial fallback false', 'fallback throws', 'no clipboard mechanisms']) {
    await test(`copy: ${scenario} preserves active wizard and subsequent progression`, async () => {
      let writes = 0, fallbacks = 0;
      const native = scenario.startsWith('native');
      const success = scenario.endsWith('success');
      setClipboard(native ? { writeText: async value => { writes++; assert.equal(value, code); if (scenario.includes('denial')) throw new DOMException('Denied', 'NotAllowedError'); } } : undefined);
      document.execCommand = scenario === 'no clipboard mechanisms' ? undefined : command => {
        fallbacks++;
        assert.equal(command, 'copy');
        const field = document.querySelector('[role=dialog] textarea[readonly]');
        assert.ok(field, 'fallback uses a visible readonly field');
        assert.equal(document.activeElement === field, true);
        assert.equal(field.value.slice(field.selectionStart, field.selectionEnd), code);
        if (scenario === 'fallback throws') throw new Error('Local fixture failure');
        return success;
      };
      await open();
      assert.equal(writes + fallbacks, 0, 'mount/open never writes clipboard');
      const timer = document.querySelector('[role=timer]').textContent;
      await click('Copy code');
      assert.equal(!!document.querySelector('[role=alert]'), false, 'clipboard failure is not an API error');
      assert.equal(document.querySelector('[role=timer]')?.textContent, timer, 'expiry unchanged');
      const field = document.querySelector('[role=dialog] textarea[readonly]');
      assert.ok(field, 'code remains selectable');
      assert.equal(field.value, code);
      if (success) {
        assert.equal(button('Copied').disabled, false);
        await click('Copied');
        assert.equal(writes, native ? 2 : 0, 'repeated click explicitly copies again');
        assert.equal(fallbacks, scenario === 'native success' ? 0 : 2);
      } else {
        assert.equal(document.querySelector('[role=dialog] [role=status]')?.textContent, 'Automatic copy is unavailable. The code is selected: copy it manually.');
        assert.equal(field.value.slice(field.selectionStart, field.selectionEnd), code);
        assert.equal(document.querySelector('[role=dialog]').textContent.includes('Copied'), false);
        await click('Copy code');
        assert.equal(!!document.querySelector('[role=timer]'), true);
      }
      await progress();
    });
  }
  await test('manual guidance is localized and a failed retry clears previous success', async () => {
    let allowed = true;
    setClipboard(undefined);
    document.execCommand = () => allowed;
    await mount(PairingSection, { api }, 'it');
    await click('Inizio io');
    await click('Copia il codice');
    assert.ok(button('Copiato'));
    allowed = false;
    await click('Copiato');
    assert.equal(document.querySelector('[role=dialog] [role=status]')?.textContent, 'La copia automatica non è disponibile. Il codice è selezionato: copialo manualmente.');
    assert.equal(document.querySelector('[role=dialog]').textContent.includes('Copiato'), false);
  });
  await test('native pending copy is local, not API busy; late completion cannot mark a new code copied', async () => {
    let finish;
    setClipboard({ writeText: () => new Promise(resolve => { finish = resolve; }) });
    await open();
    await click('Copy code');
    assert.equal(!!document.querySelector('[role=timer]'), true);
    await escape();
    assert.equal(button('Cancel').disabled, false);
    await type('textarea', 'peer-accept');
    await click('Review and continue');
    await flush(() => finish());
    assert.equal(document.querySelector('[role=dialog]').textContent.includes('Copied'), false);
    assert.equal(document.querySelector('[role=dialog] textarea').value, 'mcpair1.reveal.LOCAL_TEST_ONLY');
  });
  const room = { room_id: 'r', name: 'Test room', owner: 'Owner', owner_online: true, members: [], permissions: [] };
  for (const locale of ['en', 'it']) {
    await test(`501 guidance is honest (${locale}); transcript survives and controls remain absent`, async () => {
      await mount(GuestRoomView, { room, api: {
        getMyBot: async () => { throw new RoomGuestApiError({ kind: 'error', code: 'not_supported' }); },
        getLog: async () => ({ events: [{ seq: 1, event_id: 'e', actor: 'Owner', payload: { text: 'Room log survives' }, kind: 'message', created_at: '' }], has_more: false }),
      } }, locale);
      const note = document.querySelector('.guest-bot-panel [role=note]');
      assert.equal(note?.textContent, locale === 'en' ? 'Room approvals are currently unavailable in Mission Control.' : 'Le approvazioni della stanza non sono attualmente disponibili in Mission Control.');
      assert.equal(document.querySelectorAll('.guest-approval button').length, 0);
      await click('Chat');
      assert.equal(document.body.textContent.includes('Room log survives'), true);
    });
  }
  await test('mock mount/unmount never alters shell roots; modal restores exact pre-existing lock', async () => {
    document.body.style.overflow = 'hidden';
    document.documentElement.style.height = '100%';
    document.getElementById('root').style.height = '100%';
    const styles = () => [document.body.getAttribute('style'), document.documentElement.getAttribute('style'), document.getElementById('root').getAttribute('style')];
    const before = styles();
    await mount(InviteRoomsMock);
    assert.deepEqual(styles(), before);
    await click('Start pairing');
    await escape();
    assert.deepEqual(styles(), before);
    await mount(BottomSheet, { open: true, title: 'Fixture modal', onClose() {}, children: 'Test' });
    await flush(() => root.unmount()); root = null;
    assert.deepEqual(styles(), before);
  });
} finally {
  if (root) await flush(() => root.unmount());
  await server.close();
  rmSync(cacheDir, { recursive: true, force: true });
  dom.window.close();
}
