import assert from 'node:assert/strict';
import test from 'node:test';
import * as botGateway from '../src/lib/bot-gateway.ts';
import { configureBotProfile } from '../src/lib/bot-gateway.ts';

type RpcCall = { method: string; params: Record<string, unknown> };

function installGatewayMocks(response: Record<string, unknown>) {
  const originalFetch = globalThis.fetch;
  const originalWebSocket = globalThis.WebSocket;
  const originalWindow = globalThis.window;
  const calls: RpcCall[] = [];

  globalThis.window = {
    location: { protocol: 'http:', host: 'localhost' },
    setTimeout,
    clearTimeout,
  } as unknown as Window & typeof globalThis;
  globalThis.fetch = (async (input) => {
    if (String(input) === '/api/gateway-root') {
      return new Response('', { status: 404 });
    }
    return new Response(JSON.stringify({ ticket: 'test-ticket' }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });
  }) as typeof fetch;

  class MockWebSocket {
    onopen: (() => void) | null = null;
    onmessage: ((event: { data: string }) => void) | null = null;
    onerror: (() => void) | null = null;
    onclose: (() => void) | null = null;

    constructor() {
      queueMicrotask(() => this.onopen?.());
    }

    send(payload: string) {
      const request = JSON.parse(payload) as { id: string; method: string; params: Record<string, unknown> };
      calls.push({ method: request.method, params: request.params });
      queueMicrotask(() => this.onmessage?.({
        data: JSON.stringify({ jsonrpc: '2.0', id: request.id, result: response }),
      }));
    }

    close() {
      // The RPC client closes the socket after the response settles.
    }
  }
  globalThis.WebSocket = MockWebSocket as unknown as typeof WebSocket;

  return {
    calls,
    restore() {
      globalThis.fetch = originalFetch;
      globalThis.WebSocket = originalWebSocket;
      globalThis.window = originalWindow;
    },
  };
}

const INPUT = {
  name: 'example-bot',
  description: 'A test bot',
  soul: 'You test things.',
  model: 'model-a',
  provider: 'provider-a',
  enabledToolsets: [],
  enabledMcpServers: [],
  disabledSkills: [],
  botRoster: true,
};

test('configureBotProfile propagates the gateway result', async () => {
  const gateway = installGatewayMocks({ ok: true, applied: { model: true } });
  try {
    const result = await configureBotProfile(INPUT, 'test-token');
    assert.deepEqual(result, { ok: true, applied: { model: true } });
  } finally {
    gateway.restore();
  }
});

test('configureBotProfile sends confirmation on a guarded retry', async () => {
  const gateway = installGatewayMocks({
    ok: true,
    applied: { model: true },
    confirm_required: false,
  });
  try {
    await configureBotProfile(INPUT, 'test-token', { confirmExpensiveModel: true });
    assert.equal(gateway.calls.length, 1);
    assert.equal(gateway.calls[0]?.params.confirm_expensive_model, true);
  } finally {
    gateway.restore();
  }
});

test('model options are requested in the selected Bot profile scope', async () => {
  const gateway = installGatewayMocks({ providers: [] });
  try {
    await botGateway.loadBotModelOptions('test-token', 'example-bot');
    assert.equal(gateway.calls.length, 1);
    assert.equal(gateway.calls[0]?.method, 'model.options');
    assert.equal(gateway.calls[0]?.params.profile, 'example-bot');
  } finally {
    gateway.restore();
  }
});

test('configure result reports sections the gateway did not apply', () => {
  const failure = botGateway.botProfileConfigureFailure({
    ok: false,
    applied: { model: false, description: true },
  });
  assert.match(failure ?? '', /model/);
});

test('provider and model must be selected as a complete pin', () => {
  assert.match(botGateway.botProfileModelPairError('', 'provider-a') ?? '', /provider.*model/i);
  assert.equal(botGateway.botProfileModelPairError('', ''), null);
  assert.equal(botGateway.botProfileModelPairError('model-a', 'provider-a'), null);
});

test('clearing an existing model pin is reported instead of becoming a no-op', () => {
  assert.match(
    botGateway.botProfileModelResetError('model-a', 'provider-a', '', '') ?? '',
    /clear.*pin|inherit.*not supported/i,
  );
  assert.equal(botGateway.botProfileModelResetError('', '', '', ''), null);
  assert.equal(botGateway.botProfileModelResetError('model-a', 'provider-a', 'model-b', 'provider-b'), null);
});

console.log('bot profile configuration tests passed');
