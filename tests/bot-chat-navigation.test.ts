import { describe, it } from 'node:test';
import { strict as assert } from 'node:assert';
import { buildBotChatHref } from '../src/lib/bot-chat-navigation.ts';

describe('buildBotChatHref', () => {
  it('preserves unrelated query', () => {
    assert.strictEqual(buildBotChatHref('/bots', 'tab=skills', 's1'), '/bots?tab=skills&chatSession=s1');
  });
  it('replaces stale chatSession exactly once while preserving other query', () => {
    const href = buildBotChatHref('/bots', 'chatSession=old&tab=tools&chatSession=older', 'new');
    const params = new URL(`http://x${href}`).searchParams;
    assert.deepStrictEqual(params.getAll('chatSession'), ['new']);
    assert.strictEqual(params.get('tab'), 'tools');
  });
  it('sets chatSession exactly once and url-encodes', () => {
    const r = buildBotChatHref('/', '', 'a b');
    assert.strictEqual(r, '/?chatSession=a+b');
    assert.strictEqual(new URL('http://x' + r).searchParams.get('chatSession'), 'a b');
  });
  it('blank session throws', () => {
    assert.throws(() => buildBotChatHref('/', '', ''), /blank/i);
    assert.throws(() => buildBotChatHref('/', '', '  '), /blank/i);
  });
  it('hash-free pathname preserved', () => {
    assert.strictEqual(buildBotChatHref('/a/b#hash', '', 'x'), '/a/b?chatSession=x');
  });
});
