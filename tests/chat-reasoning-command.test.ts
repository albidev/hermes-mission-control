import { executeReasoningSlashCommand } from '../src/lib/chat-commands.ts';

function assertDeepEqual(actual: unknown, expected: unknown, message: string) {
  const actualJson = JSON.stringify(actual);
  const expectedJson = JSON.stringify(expected);
  if (actualJson !== expectedJson) throw new Error(`${message}: expected ${expectedJson}, got ${actualJson}`);
}

async function testSessionScopedReasoningUsesConfigSet() {
  const calls: Array<{ method: string; params: Record<string, unknown> }> = [];
  const result = await executeReasoningSlashCommand('high', 'session-46', async (method, params) => {
    calls.push({ method, params });
    return { key: 'reasoning', value: 'high', scope: 'session' };
  });

  assertDeepEqual(calls, [{
    method: 'config.set',
    params: { key: 'reasoning', value: 'high', session_id: 'session-46' },
  }], 'session reasoning command writes through the gateway config RPC');
  assertDeepEqual(result, { value: 'high', scope: 'session', updatesEffort: true }, 'session reasoning result');
}

async function testGlobalReasoningForwardsScope() {
  const calls: Array<{ method: string; params: Record<string, unknown> }> = [];
  await executeReasoningSlashCommand('low --global', 'session-46', async (method, params) => {
    calls.push({ method, params });
    return { key: 'reasoning', value: 'low', scope: 'global' };
  });

  assertDeepEqual(calls[0], {
    method: 'config.set',
    params: { key: 'reasoning', value: 'low', scope: 'global', session_id: 'session-46' },
  }, 'global reasoning command forwards global scope');
}

async function testBareReasoningFallsThroughToWorker() {
  let called = false;
  const result = await executeReasoningSlashCommand('', 'session-46', async () => {
    called = true;
    return {};
  });

  assertDeepEqual(result, null, 'bare reasoning command is left to the slash worker');
  assertDeepEqual(called, false, 'bare reasoning command does not write config');
}

await testSessionScopedReasoningUsesConfigSet();
await testGlobalReasoningForwardsScope();
await testBareReasoningFallsThroughToWorker();
console.log('chat reasoning command tests passed');
