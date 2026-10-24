import assert from 'node:assert/strict';
import { mkdtempSync, rmSync } from 'node:fs';
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
for (const key of [
  'window',
  'document',
  'HTMLElement',
  'HTMLInputElement',
  'HTMLSelectElement',
  'Event',
  'MouseEvent',
  'Node',
]) {
  globalThis[key] = dom.window[key];
}
Object.defineProperty(globalThis, 'navigator', {
  configurable: true,
  value: dom.window.navigator,
});
globalThis.IS_REACT_ACT_ENVIRONMENT = true;

const cacheDir = mkdtempSync(path.join(tmpdir(), 'mc-room-invite-behavior-'));
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
const { useRoomInvite } = await server.ssrLoadModule('/src/lib/use-room-invite.ts');
const { RoomInviteApiError } = await server.ssrLoadModule('/src/lib/room-invite-api.ts');
const { RoomInvitePanel } = await server.ssrLoadModule('/src/components/chat/RoomInvitePanel.tsx');

const settle = () => new Promise<void>((resolve) => setImmediate(resolve));

async function flush(action: () => void | Promise<void> = () => {}) {
  await act(async () => {
    await action();
    for (let i = 0; i < 4; i++) {
      await settle();
    }
  });
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function lobby(
  id: string,
  roster = [
    {
      member_id: 'peer-1',
      handle: 'remote',
      display_name: 'Remote',
      target: {
        peer_id: 'peer-1',
        profile: 'profile-1',
        capability_digest: 'cap-1',
      },
    },
  ],
) {
  return { lobby_id: id, name: `Lobby ${id}`, state: 'open', roster };
}

function activation(
  id: string,
  peers = [
    {
      member_id: 'peer-1',
      peer_id: 'peer-1',
      invite_id: 'invite-1',
      redeemed_binding: null,
      host_decision: 'pending',
      joining_consent: 'pending',
    },
  ],
) {
  return {
    lobby_id: id,
    lobby_state: 'open',
    ready: peers.every((peer: any) => peer.redeemed_binding !== null),
    peers,
  };
}

function fakeApi(overrides: Record<string, unknown> = {}) {
  const api: any = {
    getLobby: async (id: string, _signal?: AbortSignal) => lobby(id),
    getActivation: async (id: string, _signal?: AbortSignal) => activation(id),
    createLobby: async () => lobby('created'),
    listPairedPeers: async () => [],
    issueInvite: async () => ({
      invite_id: 'invite-1',
      secret: 'secret-value',
      expires_at: '2030-01-01',
    }),
    revokeInvite: async () => ({ revoked: true }),
    decideHost: async () => ({
      invite_id: 'invite-1',
      member_id: 'peer-1',
      status: 'approved',
      host_decision: 'approved',
      joining_consent: 'given',
    }),
    activate: async () => ({
      lobby_id: 'lobby-1',
      status: 'activated' as const,
      room_id: 'room-1',
    }),
    ...overrides,
  };
  // The creation form reads peers and the trusted owner profile from one call.
  return {
    getPairing: async (signal?: AbortSignal) => ({
      peers: await api.listPairedPeers(signal),
      owner_profile: 'default',
    }),
    ...api,
  };
}

function Probe({ lobbyId, api }: { lobbyId: string | null; api: any }) {
  const state = useRoomInvite(lobbyId, undefined, api);
  (globalThis as any).__roomProbe = state;
  return React.createElement(
    'output',
    {
      'data-status': state.status,
      'data-error': state.error?.code ?? '',
      'data-refresh': String(state.activationNeedsRefresh),
    },
    state.status,
  );
}

function renderProbe(lobbyId: string | null, api: any) {
  document.getElementById('root')!.replaceChildren();
  const root = createRoot(document.getElementById('root')!);
  return {
    root,
    render: (id: string | null) =>
      flush(() => root.render(React.createElement(Probe, { lobbyId: id, api }))),
  };
}

function panelRoot(props: any) {
  document.getElementById('root')!.replaceChildren();
  const root = createRoot(document.getElementById('root')!);
  return {
    root,
    render: () =>
      flush(() =>
        root.render(
          React.createElement(
            I18nProvider,
            null,
            React.createElement(RoomInvitePanel, props),
          ),
        ),
      ),
  };
}

async function typeInput(input: HTMLInputElement, value: string) {
  await flush(() => {
    Object.getOwnPropertyDescriptor(
      window.HTMLInputElement.prototype,
      'value',
    )!.set!.call(input, value);
    input.dispatchEvent(new window.Event('input', { bubbles: true }));
    input.dispatchEvent(new window.Event('change', { bubbles: true }));
  });
}

async function click(text: string) {
  const button = [...document.querySelectorAll('button')].find((candidate) =>
    candidate.textContent?.includes(text),
  );
  assert.ok(button, `button containing ${text} is present`);
  await flush(() => (button as HTMLButtonElement).click());
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
  await runCase('H1 ready load', async () => {
  // H1: the hook resumes both resources for the requested lobby and reaches ready.
  {
    const calls: string[] = [];
    const api = fakeApi({
      getLobby: async (id: string) => {
        calls.push(`lobby:${id}`);
        return lobby(id);
      },
      getActivation: async (id: string) => {
        calls.push(`activation:${id}`);
        return activation(id);
      },
    });
    const { root, render } = renderProbe('lobby-1', api);

    await render('lobby-1');

    assert.deepEqual(calls, ['lobby:lobby-1', 'activation:lobby-1']);
    assert.equal((globalThis as any).__roomProbe.status, 'ready');
    await flush(() => root.unmount());
  }
  });

  await runCase('H2 lobby switch aborts stale load', async () => {
  // H2: changing lobbies aborts A and prevents its late response overwriting B.
  {
    const aLobby = deferred<any>();
    const bLobby = deferred<any>();
    const signals: AbortSignal[] = [];
    const api = fakeApi({
      getLobby: (id: string, signal?: AbortSignal) => {
        signals.push(signal!);
        return id === 'A' ? aLobby.promise : bLobby.promise;
      },
      getActivation: async (id: string) => activation(id),
    });
    const { root, render } = renderProbe('A', api);

    await render('A');
    await render('B');
    bLobby.resolve(lobby('B'));
    await flush();
    await flush(() => aLobby.resolve(lobby('A')));

    assert.equal((globalThis as any).__roomProbe.lobby.lobby_id, 'B');
    assert.equal(signals[0].aborted, true);
    await flush(() => root.unmount());
  }
  });

  await runCase('H3 unmount aborts controllers', async () => {
  // H3: unmount aborts load and mutation controllers.
  {
    const load = deferred<any>();
    const issue = deferred<any>();
    const signals: AbortSignal[] = [];
    const api = fakeApi({
      getLobby: (_id: string, signal?: AbortSignal) => {
        signals.push(signal!);
        return load.promise;
      },
      issueInvite: (
        _id: string,
        _input: any,
        signal?: AbortSignal,
      ) => {
        signals.push(signal!);
        return issue.promise;
      },
    });
    const { root, render } = renderProbe('lobby-1', api);

    await render('lobby-1');
    const probe = (globalThis as any).__roomProbe;
    let mutation: Promise<any>;
    await flush(() => {
      mutation = probe.issueInvite({
        member_id: 'peer-1',
        invitee_label: 'remote',
      });
    });
    const mutationRejected = mutation!.then(
      () => false,
      () => true,
    );
    await flush(() => root.unmount());

    assert.equal(signals.length, 2);
    assert.ok(signals.every((signal) => signal.aborted));
    await flush(() => {
      issue.reject(new DOMException('aborted', 'AbortError'));
      load.resolve(lobby('lobby-1'));
    });
    assert.equal(await mutationRejected, true);
  }
  });

  await runCase('H4 unavailable and empty states', async () => {
  // H4: unavailable and empty are distinct observable states.
  {
    const unavailable = fakeApi({
      getLobby: async () => {
        throw new RoomInviteApiError({ kind: 'unavailable', code: 'not_found' });
      },
    });
    const { root, render } = renderProbe('missing', unavailable);

    await render('missing');

    assert.equal((globalThis as any).__roomProbe.status, 'error');
    assert.equal((globalThis as any).__roomProbe.error.kind, 'unavailable');
    await flush(() => root.unmount());

    const empty = fakeApi({
      getLobby: async () => lobby('empty', []),
    });
    const rendered = renderProbe('empty', empty);
    await rendered.render('empty');

    assert.equal((globalThis as any).__roomProbe.status, 'empty');
    await flush(() => rendered.root.unmount());
  }
  });

  await runCase('H5 reload preserves one-time secret', async () => {
  // H5: a concurrent reload only aborts load; the one-time secret remains in the mutation.
  {
    const issue = deferred<any>();
    const issueSignals: AbortSignal[] = [];
    const api = fakeApi({
      issueInvite: (
        _id: string,
        _input: any,
        signal?: AbortSignal,
      ) => {
        issueSignals.push(signal!);
        return issue.promise;
      },
    });
    const { root, render } = renderProbe('lobby-1', api);

    await render('lobby-1');
    let pending!: Promise<any>;
    await flush(() => {
      pending = (globalThis as any).__roomProbe.issueInvite({
        member_id: 'peer-1',
        invitee_label: 'remote',
      });
    });
    await flush(() => (globalThis as any).__roomProbe.reload());
    await flush(() =>
      issue.resolve({
        invite_id: 'invite-1',
        secret: 'one-time-secret',
        expires_at: '2030-01-01',
      }),
    );
    const result = await pending;

    assert.equal(result.secret, 'one-time-secret');
    assert.equal(issueSignals[0].aborted, false);
    assert.equal(window.localStorage.length, 0);
    assert.equal(window.sessionStorage.length, 0);
    assert.equal(window.location.href.includes('one-time-secret'), false);
    await flush(() => root.unmount());
  }
  });

  await runCase('H6 ambiguity sets refresh gate', async () => {
  // H6: both ambiguity responses set the durable refresh gate, with no retry.
  {
    for (const [status, code] of [
      [409, 'registration_incomplete'],
      [503, 'registration_ambiguous'],
    ] as const) {
      let count = 0;
      const api = fakeApi({
        activate: async () => {
          count++;
          throw new RoomInviteApiError({ kind: 'ambiguous', code });
        },
      });
      const { root, render } = renderProbe('lobby-1', api);

      await render('lobby-1');
      await flush(async () => {
        await assert.rejects((globalThis as any).__roomProbe.activate());
      });

      assert.equal(count, 1);
      assert.equal(
        (globalThis as any).__roomProbe.activationNeedsRefresh,
        true,
      );
      await flush(() => root.unmount());
      void status;
    }
  }
  });

  await runCase('H7 host decision carries binding', async () => {
  // H7: host decisions carry the displayed binding object exactly.
  {
    const binding = {
      fingerprint: 'fp',
      target_install_id: 'install',
      target_profile: 'profile',
      catalog_digest: 'catalog',
      policy_digest: 'policy',
    };
    let received: any;
    const api = fakeApi({
      decideHost: async (_id: string, _invite: string, input: any) => {
        received = input;
        return {} as any;
      },
    });
    const { root, render } = renderProbe('lobby-1', api);

    await render('lobby-1');
    await flush(() =>
      (globalThis as any).__roomProbe.decideHost(
        'invite-1',
        'peer-1',
        true,
        binding,
      ),
    );

    assert.deepEqual(received, {
      peer_id: 'peer-1',
      approved: true,
      expected_binding: binding,
    });
    await flush(() => root.unmount());
  }
  });

  await runCase('H8 binding_changed refreshes once', async () => {
  // H8: binding_changed refetches activation exactly once and never retries the decision.
  {
    const binding = {
      fingerprint: 'fp-old',
      target_install_id: 'install-old',
      target_profile: 'profile',
      catalog_digest: 'cat-old',
      policy_digest: 'pol-old',
    };
    const refreshed = {
      ...activation('lobby-1'),
      ready: true,
      peers: [
        {
          ...activation('lobby-1').peers[0],
          invite_id: 'invite-2',
          redeemed_binding: {
            fingerprint: 'fp-new',
            target_install_id: 'install-new',
            target_profile: 'profile',
            catalog_digest: 'cat-new',
            policy_digest: 'pol-new',
          },
        },
      ],
    };
    let decisions = 0;
    let activations = 0;
    const api = fakeApi({
      decideHost: async () => {
        decisions++;
        throw new RoomInviteApiError({
          kind: 'conflict',
          code: 'binding_changed',
        });
      },
      getActivation: async () => {
        activations++;
        return activations === 1 ? activation('lobby-1') : refreshed;
      },
    });
    const { root, render } = renderProbe('lobby-1', api);

    await render('lobby-1');
    await flush(async () => {
      await assert.rejects(
        (globalThis as any).__roomProbe.decideHost(
          'invite-1',
          'peer-1',
          true,
          binding,
        ),
      );
    });

    assert.equal(decisions, 1);
    assert.equal(activations, 2);
    assert.equal((globalThis as any).__roomProbe.error.code, 'binding_changed');
    assert.equal(
      (globalThis as any).__roomProbe.activation.peers[0].invite_id,
      'invite-2',
    );
    await flush(() => root.unmount());
  }
  });

  await runCase('P0 creation sends selected target', async () => {
  // P0: creation loads paired peers from the API and sends the exact selected target.
  {
    const peerA = {
      peer_id: 'peer-a',
      installation_id: 'install:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
      profile: 'remote_a',
      capability_digest: 'a'.repeat(64),
    };
    const peerB = {
      peer_id: 'peer-b',
      installation_id: 'install:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
      profile: 'remote_b',
      capability_digest: 'b'.repeat(64),
    };
    let received: any;
    const api = fakeApi({
      listPairedPeers: async () => [peerA, peerB],
      createLobby: async (input: any) => {
        received = input;
        return lobby('created');
      },
    });
    const { root, render } = panelRoot({
      lobbyId: null,
      api,
      localBots: [{ handle: 'default', displayName: 'Local' }],
    });

    await render();
    const select = document.querySelector(
      'select[aria-label*="paired" i]',
    ) as HTMLSelectElement;
    assert.ok(select);
    select.value = 'peer-b';
    select.dispatchEvent(new window.Event('change', { bubbles: true }));
    await typeInput(
      document.querySelector('input[aria-label*="lobby name" i]') as HTMLInputElement,
      'Room',
    );
    await click('Create lobby');

    // Browser-safe body only: the server resolves every peer attribute from its registry.
    assert.deepEqual(received.roster, [
      { member_id: 'local-owner', handle: 'default' },
      { member_id: 'peer-peer-b', handle: 'remote_b', target: { peer_id: 'peer-b' } },
    ]);
    await flush(() => root.unmount());
  }
  });

  await runCase('P0-empty paired list blocks safely', async () => {
  // P0-empty: an empty list is an explicit blocker and never exposes endpoint/fingerprint inputs.
  {
    const { root, render } = panelRoot({
      lobbyId: null,
      api: fakeApi({ listPairedPeers: async () => [] }),
      localBots: [{ handle: 'default' }],
    });

    await render();

    assert.match(document.body.textContent || '', /no paired peer available/i);
    assert.equal(
      document.querySelector('input[aria-label*="endpoint" i]'),
      null,
    );
    assert.equal(
      document.querySelector('input[aria-label*="fingerprint" i]'),
      null,
    );
    assert.equal(document.querySelector('select'), null);
    await flush(() => root.unmount());
  }
  });

  await runCase('P0-unavailable exposes retry', async () => {
  // P0-unavailable: a 404 is surfaced as the same unavailable state as the other invite routes, with retry.
  {
    const { root, render } = panelRoot({
      lobbyId: null,
      api: fakeApi({
        listPairedPeers: async () => {
          throw new RoomInviteApiError({
            kind: 'unavailable',
            code: 'not_found',
          });
        },
      }),
      localBots: [{ handle: 'default' }],
    });

    await render();

    assert.match(document.body.textContent || '', /unavailable/i);
    assert.ok(
      [...document.querySelectorAll('button')].some((button) =>
        /retry/i.test(button.textContent || ''),
      ),
    );
    await flush(() => root.unmount());
  }
  });

  await runCase('P0-switch replaces selected target', async () => {
  // P0-switch: changing selection replaces the target; the first peer never leaks into creation.
  {
    const peers = [
      {
        peer_id: 'peer-a',
        installation_id: 'install:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
        profile: 'remote_a',
        capability_digest: 'a'.repeat(64),
      },
      {
        peer_id: 'peer-b',
        installation_id: 'install:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
        profile: 'remote_b',
        capability_digest: 'b'.repeat(64),
      },
    ];
    let received: any;
    const { root, render } = panelRoot({
      lobbyId: null,
      api: fakeApi({
        listPairedPeers: async () => peers,
        createLobby: async (input: any) => {
          received = input;
          return lobby('created');
        },
      }),
      localBots: [{ handle: 'default' }],
    });

    await render();
    const select = document.querySelector('select') as HTMLSelectElement;
    select.value = 'peer-a';
    select.dispatchEvent(new window.Event('change', { bubbles: true }));
    await flush();
    select.value = 'peer-b';
    select.dispatchEvent(new window.Event('change', { bubbles: true }));
    await typeInput(
      document.querySelector('input[aria-label*="lobby name" i]') as HTMLInputElement,
      'Room',
    );
    await click('Create lobby');

    assert.deepEqual(received.roster[1].target, { peer_id: 'peer-b' });
    assert.equal(received.roster[1].handle, 'remote_b');
    await flush(() => root.unmount());
  }
  });

  await runCase('P1 creation form has pairing blocker', async () => {
  // P1: no lobby is a creation form with an explicit pairing blocker, not a dead end.
  {
    const { root, render } = panelRoot({
      lobbyId: null,
      api: fakeApi(),
      localBots: [{ handle: 'bot', displayName: 'Bot' }],
      pairedPeers: [],
    });

    await render();

    assert.ok(document.querySelector('input[aria-label]'));
    assert.ok(document.body.textContent?.includes('paired'));
    assert.equal(
      document.querySelector('input[aria-label*="endpoint" i]'),
      null,
    );
    assert.equal(
      document.querySelector('input[aria-label*="fingerprint" i]'),
      null,
    );
    await flush(() => root.unmount());
  }
  });

  await runCase('P2/P3 members gate decisions by binding', async () => {
  // P2/P3: two remote members have independent blocks; binding gates decisions and exposes both digests.
  {
    const roster = [
      lobby('lobby-1').roster[0],
      {
        member_id: 'peer-2',
        handle: 'second',
        display_name: 'Second',
        target: { peer_id: 'peer-2', profile: 'profile-2' },
      },
    ];
    const peers = [
      activation('lobby-1').peers[0],
      {
        member_id: 'peer-2',
        invite_id: 'invite-2',
        redeemed_binding: null,
        host_decision: 'pending',
        joining_consent: 'pending',
      },
    ];
    const api = fakeApi({
      getLobby: async () => lobby('lobby-1', roster),
      getActivation: async () => activation('lobby-1', peers),
    });
    const { root, render } = panelRoot({ lobbyId: 'lobby-1', api });

    await render();

    assert.equal(document.body.textContent?.match(/Remote/g)?.length, 2);
    assert.equal(document.body.textContent?.match(/Second/g)?.length, 2);
    assert.equal(
      [...document.querySelectorAll('button')].filter((button) =>
        /Approve|Reject/.test(button.textContent || ''),
      ).length,
      0,
    );
    assert.match(
      document.body.textContent || '',
      /waiting for the other owner/i,
    );

    const binding = {
      fingerprint: 'fp',
      target_install_id: 'install',
      target_profile: 'profile',
      catalog_digest: 'cat-ab12',
      policy_digest: 'pol-cd34',
    };
    const boundApi = fakeApi({
      getLobby: async () => lobby('lobby-1', [roster[0]]),
      getActivation: async () =>
        activation('lobby-1', [
          { ...peers[0], redeemed_binding: binding },
        ]),
    });
    await flush(() => root.unmount());
    const next = panelRoot({ lobbyId: 'lobby-1', api: boundApi });

    await next.render();

    assert.ok(
      [...document.querySelectorAll('button')].some((button) =>
        /Approve|Reject/.test(button.textContent || ''),
      ),
    );
    assert.ok(document.body.textContent?.includes('pol-cd34'));
    assert.ok(document.body.textContent?.includes('cat-ab12'));
    await flush(() => next.root.unmount());
  }
  });

  await runCase('P3-retry reloads paired peers', async () => {
  // P3-retry: retry calls listPairedPeers again and renders the recovered list.
  {
    let calls = 0;
    const peer = {
      peer_id: 'peer-retry',
      installation_id: 'install:retry',
      profile: 'retry-profile',
      capability_digest: 'r'.repeat(64),
    };
    const api = fakeApi({
      listPairedPeers: async () => {
        calls++;
        if (calls === 1) {
          throw new RoomInviteApiError({
            kind: 'error',
            code: 'temporary',
          });
        }
        return [peer];
      },
    });
    const { root, render } = panelRoot({
      lobbyId: null,
      api,
      localBots: [{ handle: 'default' }],
    });

    await render();

    assert.match(
      document.body.textContent || '',
      /Unable to load paired peers/i,
    );
    await click('Retry');
    assert.equal(calls, 2);
    assert.match(document.body.textContent || '', /retry-profile/);
    await flush(() => root.unmount());
  }
  });

  await runCase('P3-reissue resets denied invite view', async () => {
  // P3-reissue: denial followed by reissue shows only the new invite awaiting redemption.
  {
    const denied = {
      ...activation('lobby-1'),
      ready: false,
      peers: [
        {
          ...activation('lobby-1').peers[0],
          invite_id: 'invite-old',
          host_decision: 'rejected',
        },
      ],
    };
    const reissued = {
      ...activation('lobby-1'),
      ready: false,
      peers: [
        {
          ...activation('lobby-1').peers[0],
          invite_id: 'invite-new',
          host_decision: 'pending',
          redeemed_binding: null,
        },
      ],
    };
    let reads = 0;
    const api = fakeApi({
      getActivation: async () => (++reads === 1 ? denied : reissued),
      issueInvite: async () => ({
        invite_id: 'invite-new',
        secret: 'new-secret',
        expires_at: '2030-01-01',
      }),
    });
    const { root, render } = panelRoot({ lobbyId: 'lobby-1', api });

    await render();
    await typeInput(
      document.querySelector('input[aria-label*="invitee" i]') as HTMLInputElement,
      'Alice',
    );
    await click('Issue');

    assert.match(
      document.body.textContent || '',
      /waiting for the other owner/i,
    );
    assert.doesNotMatch(document.body.textContent || '', /Rejected/);
    assert.equal(
      [...document.querySelectorAll('button')].filter((button) =>
        /Approve|Reject/.test(button.textContent || ''),
      ).length,
      0,
    );
    await flush(() => root.unmount());
  }
  });

  await runCase('P3-invite-switch uses current binding', async () => {
  // P3-invite-switch: changing invite_id replaces the binding sent with the decision.
  {
    const oldBinding = {
      fingerprint: 'old',
      target_install_id: 'old',
      target_profile: 'p',
      catalog_digest: 'cat-old',
      policy_digest: 'pol-old',
    };
    const newBinding = {
      fingerprint: 'new',
      target_install_id: 'new',
      target_profile: 'p',
      catalog_digest: 'cat-new',
      policy_digest: 'pol-new',
    };
    let received: any;
    let reads = 0;
    const api = fakeApi({
      getActivation: async () => {
        reads++;
        const binding = reads === 1 ? oldBinding : newBinding;
        return activation('lobby-1', [
          {
            ...activation('lobby-1').peers[0],
            invite_id: reads === 1 ? 'invite-old' : 'invite-new',
            redeemed_binding: binding,
          },
        ]);
      },
      decideHost: async (_id: string, invite: string, input: any) => {
        received = { invite, input };
        return {} as any;
      },
    });
    const { root, render } = panelRoot({ lobbyId: 'lobby-1', api });

    await render();
    const refresh = document.querySelector(
      'button[aria-label*="refresh" i]',
    ) as HTMLButtonElement;
    assert.ok(refresh);
    await flush(() => refresh.click());
    const approve = [...document.querySelectorAll('button')].find((button) =>
      /Approva|Approve/.test(button.textContent || ''),
    ) as HTMLButtonElement;
    assert.ok(approve);
    await flush(() => approve.click());

    assert.equal(received.invite, 'invite-new');
    assert.deepEqual(received.input.expected_binding, newBinding);
    await flush(() => root.unmount());
  }
  });

  await runCase('P4 secret is one-shot', async () => {
  // P4: the secret is one-shot, closes, and is not retained after unmount.
  {
    const api = fakeApi();
    const { root, render } = panelRoot({
      lobbyId: 'lobby-1',
      api,
      onClose: () => {},
    });

    await render();
    const input = document.querySelector(
      'input[aria-label*="invitee" i]',
    ) as HTMLInputElement;
    await typeInput(input, 'Alice');
    await click('Issue');
    await flush();

    assert.ok(document.body.textContent?.includes('secret-value'));
    await click('Clear secret');
    assert.equal(
      document.body.textContent?.includes('secret-value'),
      false,
    );
    await flush(() => root.unmount());
    assert.equal(
      document.body.textContent?.includes('secret-value'),
      false,
    );
  }
  });

  if (failures.length > 0) {
    for (const failure of failures) {
      console.error(`--- ${failure.name} ---`);
      console.error(failure.error);
    }
    process.exitCode = 1;
  } else {
    console.log('Room invite behavioural hook/panel tests passed.');
  }
} finally {
  await server.close();
  rmSync(cacheDir, {
    recursive: true,
    force: true,
    maxRetries: 5,
    retryDelay: 100,
  });
  dom.window.close();
}
