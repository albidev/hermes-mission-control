import {
  applyGatewayEvent,
  attachmentRpcMethod,
  classifyAttachment,
  createRpcRequest,
  extractInteractionRequest,
  extractSessionId,
  extractSessionKey,
  extractTranscript,
  extractInjectedSessionToken,
  isResponseFor,
  nextReconnectDelay,
  normalizeTranscript,
  parseGatewayFrame,
} from '../src/lib/chat-protocol.ts';

function assertEqual<T>(actual: T, expected: T) {
  if (actual !== expected) {
    throw new Error(`Expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`);
  }
}

function assertDeepEqual(actual: unknown, expected: unknown) {
  const actualJson = JSON.stringify(actual);
  const expectedJson = JSON.stringify(expected);
  if (actualJson !== expectedJson) {
    throw new Error(`Expected ${expectedJson}, got ${actualJson}`);
  }
}

const eventFrame = JSON.stringify({
  jsonrpc: '2.0',
  method: 'event',
  params: { type: 'message.delta', session_id: 'sid', payload: { text: 'hello' } },
});

assertDeepEqual(parseGatewayFrame(eventFrame), {
  kind: 'event',
  event: { type: 'message.delta', session_id: 'sid', payload: { text: 'hello' } },
});

assertEqual(parseGatewayFrame('{nope').kind, 'malformed');
assertEqual(parseGatewayFrame(JSON.stringify({ method: 'event', params: { payload: {} } })).kind, 'unknown');

const responseFrame = parseGatewayFrame(JSON.stringify({ jsonrpc: '2.0', id: 'r3', result: { ok: true } }));
assertEqual(responseFrame.kind, 'response');
if (responseFrame.kind === 'response') {
  assertEqual(isResponseFor(responseFrame.response, 'r3'), true);
  assertEqual(isResponseFor(responseFrame.response, 'r4'), false);
}

assertDeepEqual(createRpcRequest('r1', 'session.create', { cols: 80 }), {
  jsonrpc: '2.0',
  id: 'r1',
  method: 'session.create',
  params: { cols: 80 },
});

assertEqual(extractSessionId({ session_id: 'abc' }), 'abc');
assertEqual(extractSessionId({ session_id: '' }), null);
assertEqual(extractSessionKey({ stored_session_id: 'stored-abc' }), 'stored-abc');
assertEqual(extractSessionKey({ session_key: 'stored-def' }), 'stored-def');
assertEqual(extractSessionKey({ resumed: 'stored-ghi' }), 'stored-ghi');
assertEqual(extractInjectedSessionToken('<script>window.__HERMES_SESSION_TOKEN__ = "gateway-token";</script>'), 'gateway-token');
assertEqual(extractInjectedSessionToken('<html>no token</html>'), null);
assertEqual(classifyAttachment('image/png', 'photo.bin'), 'image');
assertEqual(classifyAttachment('application/pdf', 'notes.txt'), 'pdf');
assertEqual(classifyAttachment('', 'report.pdf'), 'pdf');
assertEqual(classifyAttachment('text/plain', 'notes.txt'), 'file');
assertEqual(attachmentRpcMethod('image'), 'image.attach_bytes');
assertEqual(attachmentRpcMethod('pdf'), 'pdf.attach');
assertEqual(attachmentRpcMethod('file'), 'file.attach');
assertDeepEqual(extractInteractionRequest({
  type: 'clarify.request',
  session_id: 'sid',
  payload: { request_id: 'req-1', question: 'Pick one', choices: ['a', 'b'], multi_select: true },
}), {
  kind: 'clarify',
  sessionId: 'sid',
  requestId: 'req-1',
  payload: { request_id: 'req-1', question: 'Pick one', choices: ['a', 'b'], multi_select: true },
});
assertDeepEqual(extractInteractionRequest({
  type: 'approval.request',
  session_id: 'sid',
  payload: { command: 'git push', description: 'Push changes', choices: ['once', 'deny'] },
}), {
  kind: 'approval',
  sessionId: 'sid',
  requestId: null,
  payload: { command: 'git push', description: 'Push changes', choices: ['once', 'deny'] },
});
assertEqual(extractInteractionRequest({ type: 'message.delta', payload: {} }), null);
assertDeepEqual(extractTranscript({ messages: [{ role: 'user', text: 'hi' }, null] }), [{ role: 'user', text: 'hi' }]);

const restored = normalizeTranscript([
  { role: 'user', text: 'Question' },
  { role: 'assistant', text: 'Answer' },
  { role: 'assistant', content: [{ type: 'text', text: 'Block one' }, { type: 'text', text: 'Block two' }] },
], 1000);
assertEqual(restored.length, 3);
assertEqual(restored[0].role, 'user');
assertEqual(restored[1].text, 'Answer');
assertEqual(restored[2].text, 'Block one\nBlock two');

let messages = applyGatewayEvent([], { type: 'message.start' }, 2000);
messages = applyGatewayEvent(messages, { type: 'message.delta', payload: { text: 'A' } }, 2001);
messages = applyGatewayEvent(messages, { type: 'message.delta', payload: { text: 'B' } }, 2002);
messages = applyGatewayEvent(messages, { type: 'message.complete', payload: { text: 'ABC' } }, 2003);
assertEqual(messages.length, 1);
assertEqual(messages[0].text, 'ABC');
assertEqual(messages[0].status, 'complete');

const unknownApplied = applyGatewayEvent(messages, { type: 'future.event', payload: { text: 'ignored' } }, 2004);
assertEqual(unknownApplied, messages);

assertEqual(nextReconnectDelay({ attempts: 0 }), 500);
assertEqual(nextReconnectDelay({ attempts: 4 }), 8000);
assertEqual(nextReconnectDelay({ attempts: 20 }), 8000);

console.log('chat protocol tests passed');
