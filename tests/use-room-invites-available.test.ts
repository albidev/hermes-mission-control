import assert from 'node:assert/strict';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { JSDOM } from 'jsdom';
import React, { act } from 'react';
import { createServer } from 'vite';
import react from '@vitejs/plugin-react';

const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: 'http://localhost/rooms', pretendToBeVisual: true });
for (const key of ['window', 'document', 'HTMLElement', 'Event', 'Node']) {
  globalThis[key] = dom.window[key];
}
Object.defineProperty(globalThis, 'navigator', { configurable: true, value: dom.window.navigator });
globalThis.IS_REACT_ACT_ENVIRONMENT = true;

const cacheDir = mkdtempSync(path.join(tmpdir(), 'mc-room-invites-available-'));
const server = await createServer({
  root: process.cwd(),
  configFile: false,
  cacheDir,
  appType: 'custom',
  logLevel: 'silent',
  server: { middlewareMode: true, hmr: false },
  plugins: [react()],
});
const { createRoot } = await import('react-dom/client');
const { useRoomInvitesAvailable } = await server.ssrLoadModule('/src/lib/use-room-invites-available.ts');
const { RoomInviteApiError } = await server.ssrLoadModule('/src/lib/room-invite-api.ts');

const settle = () => new Promise<void>((resolve) => setImmediate(resolve));

async function flush(action: () => void | Promise<void> = () => {}) {
  await act(async () => {
    await action();
    for (let i = 0; i < 4; i++) {
      await settle();
    }
  });
}

function fakeApi(outcome: 'ok' | 'off' | 'unauthorized') {
  const calls: string[] = [];
  const api = {
    listPairedPeers: async () => {
      calls.push('paired-peers');
      if (outcome === 'off') throw new RoomInviteApiError({ kind: 'unavailable', code: 'not_found' });
      if (outcome === 'unauthorized') throw new RoomInviteApiError({ kind: 'unauthorized', code: 'unauthorized' });
      return [];
    },
  };
  return { api, calls };
}

async function render(enabled: boolean, api: unknown) {
  const host = document.createElement('div');
  document.body.appendChild(host);
  const root = createRoot(host);
  const seen: boolean[] = [];
  function Probe({ on }: { on: boolean }) {
    const available = useRoomInvitesAvailable(on, 'token', api);
    seen.push(available);
    return React.createElement('span', { 'data-available': String(available) });
  }
  await flush(() => root.render(React.createElement(Probe, { on: enabled })));
  const rerender = (on: boolean) => flush(() => root.render(React.createElement(Probe, { on })));
  const value = () => host.querySelector('span')?.getAttribute('data-available');
  return { root, seen, rerender, value };
}

const cases: Array<[string, () => Promise<void>]> = [
  ['feature off (404) keeps the entry hidden', async () => {
    const { api, calls } = fakeApi('off');
    const view = await render(true, api);
    assert.deepEqual(calls, ['paired-peers']);
    assert.equal(view.value(), 'false');
    assert.ok(!view.seen.includes(true), 'never flashes visible');
    await flush(() => view.root.unmount());
  }],
  ['feature on shows the entry after the probe succeeds', async () => {
    const { api, calls } = fakeApi('ok');
    const view = await render(true, api);
    assert.deepEqual(calls, ['paired-peers']);
    assert.equal(view.value(), 'true');
    assert.equal(view.seen[0], false, 'hidden until the probe answers');
    await flush(() => view.root.unmount());
  }],
  ['auth failure keeps the entry hidden', async () => {
    const { api } = fakeApi('unauthorized');
    const view = await render(true, api);
    assert.equal(view.value(), 'false');
    await flush(() => view.root.unmount());
  }],
  ['closed drawer does not probe and hides the entry', async () => {
    const { api, calls } = fakeApi('ok');
    const view = await render(false, api);
    assert.deepEqual(calls, []);
    assert.equal(view.value(), 'false');
    await view.rerender(true);
    assert.deepEqual(calls, ['paired-peers']);
    assert.equal(view.value(), 'true');
    await view.rerender(false);
    assert.equal(view.value(), 'false');
    await flush(() => view.root.unmount());
  }],
];

let failed = 0;
try {
  for (const [name, run] of cases) {
    try {
      await run();
      console.log(`ok - ${name}`);
    } catch (error) {
      failed += 1;
      console.error(`not ok - ${name}`);
      console.error(error);
    }
  }
} finally {
  await server.close();
  rmSync(cacheDir, { recursive: true, force: true });
}
if (failed > 0) {
  console.error(`${failed} of ${cases.length} cases failed`);
  process.exit(1);
}
console.log(`${cases.length} of ${cases.length} cases passed`);
