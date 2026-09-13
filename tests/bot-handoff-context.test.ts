import { describe, it } from 'node:test';
import { strict as assert } from 'node:assert';
import { serializeHandoffContext } from '../src/lib/bot-handoff-context.ts';
import { createHandoffEnvelope, formatHandoffPrompt } from '../src/lib/bot-handoff.ts';

const message = (id: string, role: 'user' | 'assistant', text: string) => ({
  kind: 'message' as const,
  id,
  message: { id, role, text, createdAt: 1 },
});

describe('serializeHandoffContext', () => {
  it('keeps the latest entries in chronological order and enforces both caps', () => {
    const context = serializeHandoffContext([
      message('1', 'user', 'old'),
      message('2', 'assistant', 'middle'),
      message('3', 'user', 'latest'),
    ], { maxEntries: 2, maxCharacters: 10 });
    assert.deepStrictEqual(context, [
      { role: 'assistant', text: 'middle' },
      { role: 'user', text: 'late' },
    ]);
  });

  it('returns no context for an empty transcript', () => {
    assert.deepStrictEqual(serializeHandoffContext([]), []);
  });

  it('includes handoff cards', () => {
    assert.deepStrictEqual(serializeHandoffContext([{
      kind: 'handoff',
      handoff: { handle: 'example-bot', request: 'inspect this', status: 'completed', reply: 'done' },
    }]), [{ role: 'handoff', text: '@example-bot inspect this [completed]: done' }]);
  });
});

describe('formatHandoffPrompt context', () => {
  it('renders context before the current request', () => {
    const envelope = createHandoffEnvelope(
      { connectionId: 'local', profile: 'default', sessionId: 'origin' },
      { profile: 'example-bot', canonicalTitle: 'Bot Chat' },
      'follow up',
      { mode: 'transcript', messages: [{ role: 'user', text: 'prior message' }] },
    );
    const prompt = formatHandoffPrompt(envelope);
    assert.ok(prompt.indexOf('CONVERSATION CONTEXT:') < prompt.indexOf('CURRENT REQUEST:'));
    assert.match(prompt, /user: prior message/);
  });
});
