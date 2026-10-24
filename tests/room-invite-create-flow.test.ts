import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { JSDOM } from 'jsdom';
import React, { act } from 'react';
import { createServer } from 'vite';
import react from '@vitejs/plugin-react';

const dom = new JSDOM('<!doctype html><div id="root"></div>', {
  url: 'http://localhost/rooms',
  pretendToBeVisual: true,
});
for (const key of ['window', 'document', 'HTMLElement', 'HTMLInputElement', 'HTMLSelectElement', 'HTMLFormElement', 'Event', 'MouseEvent', 'Node']) {
  globalThis[key] = dom.window[key];
}
Object.defineProperty(globalThis, 'navigator', { configurable: true, value: dom.window.navigator });
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
// The create form loads vaults best-effort; keep it off the network.
globalThis.fetch = (async () => new Response('{}', { status: 404 })) as typeof fetch;

const cacheDir = mkdtempSync(path.join(tmpdir(), 'mc-room-invite-create-'));
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
const { I18nProvider } = await server.ssrLoadModule('/src/lib/i18n.tsx');
const { buildCreateLobbyRequest, RoomInviteApiError } = await server.ssrLoadModule('/src/lib/room-invite-api.ts');
const { RoomInvitePanel } = await server.ssrLoadModule('/src/components/chat/RoomInvitePanel.tsx');
const { CreateRoomForm } = await server.ssrLoadModule('/src/components/chat/GroupRoomView.tsx');
const { useRoomInvitesAvailable } = await server.ssrLoadModule('/src/lib/use-room-invites-available.ts');

const settle = () => new Promise<void>((resolve) => setImmediate(resolve));
async function flush(action: () => void | Promise<void> = () => {}) {
  await act(async () => {
    await action();
    for (let i = 0; i < 4; i++) await settle();
  });
}

function mount(element: React.ReactElement) {
  document.getElementById('root')!.replaceChildren();
  const root = createRoot(document.getElementById('root')!);
  return { root, render: () => flush(() => root.render(React.createElement(I18nProvider, null, element))) };
}

async function typeInput(input: HTMLInputElement, value: string) {
  await flush(() => {
    Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')!.set!.call(input, value);
    input.dispatchEvent(new window.Event('input', { bubbles: true }));
  });
}

async function choosePeer(peerId: string) {
  const select = document.querySelector('select[aria-label*="paired" i]') as HTMLSelectElement;
  assert.ok(select, 'paired peer select is rendered');
  await flush(() => {
    select.value = peerId;
    select.dispatchEvent(new window.Event('change', { bubbles: true }));
  });
}

async function click(text: string) {
  const button = [...document.querySelectorAll('button')].find((candidate) => candidate.textContent?.includes(text));
  assert.ok(button, `button containing ${text} is present`);
  await flush(() => (button as HTMLButtonElement).click());
}

const peer = { peer_id: 'owner-b', installation_id: `install:${'b'.repeat(32)}`, profile: 'default', capability_digest: 'c'.repeat(64) };
const lobbyView = (id: string) => ({
  lobby_id: id,
  name: 'Release triage',
  state: 'open',
  roster: [
    { member_id: 'local-owner', profile: 'default', handle: 'default' },
    { member_id: 'peer-owner-b', profile: 'default', handle: 'owner-b', target: { kind: 'peer', peer_id: 'owner-b', profile: 'default' } },
  ],
});

function fakeApi(overrides: Record<string, unknown> = {}) {
  return {
    getPairing: async () => ({ peers: [peer], owner_profile: 'default' }),
    listPairedPeers: async () => [peer],
    createLobby: async () => lobbyView('lobby-new'),
    getLobby: async (id: string) => lobbyView(id),
    getActivation: async (id: string) => ({ lobby_id: id, lobby_state: 'open', ready: false, peers: [] }),
    ...overrides,
  };
}

const failures: Array<{ name: string; error: unknown }> = [];
async function runCase(name: string, action: () => Promise<void>) {
  try {
    await action();
    console.log(`ok   ${name}`);
  } catch (error) {
    failures.push({ name, error });
    console.error(`FAIL ${name}`);
  }
}

try {
  await runCase('C1 the create-lobby body is the shared server contract', async () => {
    // The server suite POSTs this same fixture and expects 200, so the two sides cannot drift.
    const fixture = JSON.parse(readFileSync(path.join(process.cwd(), 'tests/fixtures/room-invites/create-lobby.json'), 'utf8'));
    assert.deepEqual(buildCreateLobbyRequest('  Release triage ', 'default', peer), fixture);
  });

  await runCase('C2 a peer whose profile differs from the owner keeps its profile as handle', async () => {
    const body = buildCreateLobbyRequest('x', 'default', { ...peer, profile: 'research' });
    assert.equal(body.roster[1].handle, 'research');
  });

  await runCase('C3 creating a lobby switches the panel to that lobby', async () => {
    let created: any = null;
    const loaded: string[] = [];
    const notified: string[] = [];
    const api = fakeApi({
      createLobby: async (input: any) => { created = input; return lobbyView('lobby-new'); },
      getLobby: async (id: string) => { loaded.push(id); return lobbyView(id); },
    });
    const { root, render } = mount(React.createElement(RoomInvitePanel, { lobbyId: null, api, initialName: 'Release triage', onLobbyCreated: (id: string) => notified.push(id) }));
    await render();
    assert.match(document.body.textContent || '', /@default/);
    await choosePeer('owner-b');
    await click('Create lobby');
    assert.deepEqual(created.roster[1], { member_id: 'peer-owner-b', handle: 'owner-b', target: { peer_id: 'owner-b' } });
    assert.deepEqual(notified, ['lobby-new']);
    assert.deepEqual(loaded, ['lobby-new']);
    assert.equal(document.querySelector('select[aria-label*="paired" i]'), null, 'creation form is gone');
    assert.match(document.body.textContent || '', /Issue invite/);
    assert.equal(new URL(window.location.href).searchParams.get('lobbyId'), 'lobby-new');
    await flush(() => root.unmount());
  });

  await runCase('C4 a rejected creation is shown, not swallowed', async () => {
    const api = fakeApi({
      createLobby: async () => { throw new RoomInviteApiError({ kind: 'error', code: 'invalid_roster' }); },
    });
    const { root, render } = mount(React.createElement(RoomInvitePanel, { lobbyId: null, api, initialName: 'Room' }));
    await render();
    await choosePeer('owner-b');
    await click('Create lobby');
    const alert = document.querySelector('[role="alert"]');
    assert.ok(alert, 'an alert is rendered');
    assert.match(alert!.textContent || '', /invalid_roster/);
    assert.ok(document.querySelector('select[aria-label*="paired" i]'), 'the form stays open for another attempt');
    await flush(() => root.unmount());
  });

  await runCase('C4b an older sidecar without owner_profile cannot create a lobby', async () => {
    let created = false;
    const api = fakeApi({
      getPairing: async () => ({ peers: [peer] }),
      createLobby: async () => { created = true; return lobbyView('x'); },
    });
    const { root, render } = mount(React.createElement(RoomInvitePanel, { lobbyId: null, api, initialName: 'Room' }));
    await render();
    assert.doesNotMatch(document.body.textContent || '', /@undefined/);
    assert.match(document.body.textContent || '', /restart the Mission Control sidecar/);
    await choosePeer('owner-b');
    const button = [...document.querySelectorAll('button')].find((candidate) => candidate.textContent?.includes('Create lobby')) as HTMLButtonElement;
    assert.equal(button.disabled, true);
    await flush(() => button.click());
    assert.equal(created, false);
    await flush(() => root.unmount());
  });

  await runCase('C5 the room-type switch only exists when invite-only is offered', async () => {
    const plain = mount(React.createElement(CreateRoomForm, { members: [], onCancel: () => {}, onCreate: async () => {} }));
    await plain.render();
    assert.equal(document.querySelector('[role="radiogroup"]'), null);
    await flush(() => plain.root.unmount());

    const offered = mount(React.createElement(CreateRoomForm, {
      members: [], onCancel: () => {}, onCreate: async () => {},
      renderInviteOnly: (name: string) => React.createElement('div', { 'data-invite-slot': name }),
    }));
    await offered.render();
    assert.ok(document.querySelector('[role="radiogroup"]'));
    await flush(() => offered.root.unmount());
  });

  await runCase('C6 invite-only replaces the local fields and carries the typed name', async () => {
    const createdLocal: unknown[] = [];
    const { root, render } = mount(React.createElement(CreateRoomForm, {
      members: [{ handle: 'a' }, { handle: 'b' }], onCancel: () => {}, onCreate: async (...args: unknown[]) => { createdLocal.push(args); },
      renderInviteOnly: (name: string) => React.createElement('input', { 'data-invite-slot': name, 'aria-label': 'slot' }),
    }));
    await render();
    await typeInput(document.querySelector('input') as HTMLInputElement, 'Release triage');
    // A fully valid local form, so only the room type keeps the submit from creating it.
    await click('Add member');
    assert.equal(document.querySelectorAll('input[type="checkbox"]:checked').length, 2);
    await click('Invite-only');
    const slot = document.querySelector('[data-invite-slot]');
    assert.ok(slot, 'invite-only slot rendered');
    assert.equal(slot!.getAttribute('data-invite-slot'), 'Release triage');
    assert.equal(document.querySelector('select'), null, 'vault picker hidden');
    // Enter inside the embedded flow submits the outer form; it must not create a local room.
    await flush(() => (document.querySelector('form') as HTMLFormElement).dispatchEvent(new window.Event('submit', { bubbles: true, cancelable: true })));
    assert.deepEqual(createdLocal, []);
    await flush(() => root.unmount());
  });

  await runCase('C7 availability is probed again when the window regains focus', async () => {
    let calls = 0;
    let up = false;
    const api = { listPairedPeers: async () => { calls += 1; if (!up) throw new RoomInviteApiError({ kind: 'unavailable', code: 'not_found' }); return []; } };
    function Probe() {
      const available = useRoomInvitesAvailable(true, undefined, api as any);
      return React.createElement('output', null, String(available));
    }
    const { root, render } = mount(React.createElement(Probe));
    await render();
    assert.equal(document.querySelector('output')!.textContent, 'false');
    up = true;
    await flush(() => { window.dispatchEvent(new window.Event('focus')); });
    assert.equal(calls, 2);
    assert.equal(document.querySelector('output')!.textContent, 'true');
    await flush(() => root.unmount());
  });
} finally {
  await server.close();
  rmSync(cacheDir, { recursive: true, force: true });
}

if (failures.length > 0) {
  for (const failure of failures) console.error(failure.name, failure.error);
  process.exit(1);
}
