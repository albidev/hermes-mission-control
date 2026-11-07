import assert from 'node:assert/strict';
import test from 'node:test';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { JSDOM } from 'jsdom';
import React, { act, useCallback, useState } from 'react';
import { createServer } from 'vite';
import react from '@vitejs/plugin-react';

const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: 'http://localhost/' });
for (const key of ['window', 'document', 'HTMLElement', 'HTMLInputElement', 'HTMLTextAreaElement', 'Event', 'KeyboardEvent']) globalThis[key] = dom.window[key];
Object.defineProperty(globalThis, 'navigator', { configurable: true, value: dom.window.navigator });
globalThis.localStorage = dom.window.localStorage;
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
window.HTMLElement.prototype.scrollTo = function () {};
globalThis.fetch = async () => new Response('{}', { status: 404 });
const { createRoot } = await import('react-dom/client');
const cacheDir = mkdtempSync(path.join(tmpdir(), 'room-selection-'));
const server = await createServer({ root: process.cwd(), configFile: false, cacheDir, appType: 'custom', logLevel: 'silent', server: { middlewareMode: true, hmr: false }, plugins: [react()] });
let root;
try {
  const { GroupGatewayClient } = await server.ssrLoadModule('/src/lib/group-gateway.ts');
  const { I18nProvider } = await server.ssrLoadModule('/src/lib/i18n.tsx');
  const { ChatDrawer } = await server.ssrLoadModule('/src/components/ChatDrawer.tsx');
  const rooms = ['first', 'requested'].map((id) => ({ id, name: id, authorityGatewayId: 'lab', authorityEpoch: 1, latestSeq: 0, disbandedAt: null, members: [] }));
  GroupGatewayClient.prototype.capabilities = async () => ({ driver: true, protocolVersion: 2, methods: [], maxLogLimit: 100, authorityGatewayId: 'lab' });
  GroupGatewayClient.prototype.list = async () => ({ rooms: rooms.map((r) => ({ ...r })), nextOffset: null });
  GroupGatewayClient.prototype.state = async (id) => ({ room: { ...rooms.find((r) => r.id === id) }, driverStatus: {} });
  GroupGatewayClient.prototype.log = async () => ({ events: [], cursor: 0, latestSeq: 0, hasMore: false, authority: null });

  await test('opening a requested room does not publish the automatic first room or oscillate selection', async () => {
    const changes = [];
    function Harness() {
      const [roomId, setRoomId] = useState('requested');
      const onRoomChange = useCallback((id) => {
        changes.push(id);
        // Bound the failing witness without hiding the observed oscillation.
        if (changes.length < 12) setRoomId(id);
      }, []);
      return React.createElement(I18nProvider, null, React.createElement(ChatDrawer, { open: true, chatMode: 'room', roomId, storedToken: '', onClose() {}, onRoomChange }));
    }
    root = createRoot(document.getElementById('root'));
    await act(async () => {
      root.render(React.createElement(Harness));
      for (let i = 0; i < 40; i++) await new Promise(setImmediate);
    });
    assert.deepEqual(changes, [], 'restoring the requested room must not emit transient automatic selections');
    assert.equal(document.querySelector('button[aria-label="Select a room"]')?.textContent.trim(), 'requested');
    assert.doesNotMatch(document.body.textContent, /Loading room/);

    await act(async () => document.querySelector('button[aria-label="Select a room"]').click());
    await act(async () => {
      [...document.querySelectorAll('[role=option]')].find((e) => e.textContent.trim() === 'first').click();
      for (let i = 0; i < 20; i++) await new Promise(setImmediate);
    });
    assert.deepEqual(changes, ['first'], 'a user selection publishes exactly once');
    assert.equal(document.querySelector('button[aria-label="Select a room"]')?.textContent.trim(), 'first');
  });
} finally {
  if (root) await act(async () => root.unmount());
  await server.close();
  dom.window.close();
  rmSync(cacheDir, { recursive: true, force: true });
}
