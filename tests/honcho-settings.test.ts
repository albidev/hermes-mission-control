import assert from 'node:assert/strict';
import {
  configureHonchoLocalIdentity,
  loadHonchoStatus,
} from '../src/lib/honcho-settings.ts';

const calls: Array<{ url: string; init: RequestInit }> = [];
let responsePayload: unknown = {
  available: true,
  configured: true,
  enabled: true,
  providerInstalled: true,
  providerActive: true,
  identityMode: 'unresolved',
  identityReady: false,
  authenticatedRuntimeSupported: true,
  peerName: null,
  workspace: 'hermes',
  aiPeer: 'hermes',
  sessionAiPeerPrefix: false,
  configPath: '~/.hermes/honcho.json',
  profileCount: 1,
  profiles: [],
};

globalThis.fetch = (async (url: string | URL | Request, init: RequestInit = {}) => {
  calls.push({ url: String(url), init });
  return {
    ok: true,
    status: 200,
    json: async () => responsePayload,
  } as Response;
}) as typeof fetch;

const status = await loadHonchoStatus('mc-token');
assert.equal(status.identityMode, 'unresolved');
assert.equal(calls[0]?.url, '/api/local/memory/honcho');
assert.equal((calls[0]?.init.headers as Record<string, string>).Authorization, 'Bearer mc-token');
assert.equal(calls[0]?.init.cache, 'no-store');

responsePayload = {
  ...responsePayload as object,
  success: true,
  identityMode: 'local-single-user',
  identityReady: true,
  peerName: 'operator',
  sessionAiPeerPrefix: true,
};
const configured = await configureHonchoLocalIdentity('operator', 'mc-token');
assert.equal(configured.identityReady, true);
assert.equal(calls[1]?.url, '/api/local/memory/honcho/local-identity');
assert.equal(calls[1]?.init.method, 'POST');
assert.deepEqual(JSON.parse(String(calls[1]?.init.body)), { peerName: 'operator' });
assert.equal((calls[1]?.init.headers as Record<string, string>).Authorization, 'Bearer mc-token');
assert.equal((calls[1]?.init.headers as Record<string, string>)['Content-Type'], 'application/json');

responsePayload = { ...responsePayload as object, providerInstalled: false };
const withoutProvider = await loadHonchoStatus('mc-token');
assert.equal(withoutProvider.providerInstalled, false, 'providerInstalled must survive normalization for the panel gate');

responsePayload = { error: 'invalid_peer_name', detail: 'Bad peer.' };
globalThis.fetch = (async () => ({
  ok: false,
  status: 400,
  json: async () => responsePayload,
})) as typeof fetch;
await assert.rejects(
  () => configureHonchoLocalIdentity('bad peer', 'mc-token'),
  /Bad peer\./,
);

console.log('honcho settings: ok');
