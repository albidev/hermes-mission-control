import {
  refreshModelAfterCommandDispatch,
  type GatewayCommandDispatch,
} from '../src/lib/chat-protocol.ts';

function assertEqual<T>(actual: T, expected: T, message: string) {
  if (actual !== expected) throw new Error(`${message}: expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`);
}

async function testStateChangingDispatchRefreshesBeforeResolve() {
  const calls: string[] = [];
  const dispatch: GatewayCommandDispatch = { type: 'exec', output: 'Reasoning effort set to high.' };

  await refreshModelAfterCommandDispatch(dispatch, 'session-46', async (sessionId) => {
    calls.push(`start:${sessionId}`);
    await Promise.resolve();
    calls.push('complete');
  });

  assertEqual(calls.join(','), 'start:session-46,complete', 'exec dispatch refreshes the active session before resolving');
}

async function testReadOnlyDispatchDoesNotRefresh() {
  const calls: string[] = [];
  const dispatch: GatewayCommandDispatch = { type: 'prefill', message: 'queued prompt' };

  await refreshModelAfterCommandDispatch(dispatch, 'session-46', async () => {
    calls.push('unexpected refresh');
  });

  assertEqual(calls.length, 0, 'prefill dispatch does not trigger a model refresh');
}

await testStateChangingDispatchRefreshesBeforeResolve();
await testReadOnlyDispatchDoesNotRefresh();
console.log('chat slash dispatch tests passed');
