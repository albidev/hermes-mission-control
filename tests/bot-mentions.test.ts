import { describe, it } from 'node:test';
import { strict as assert } from 'node:assert';
import {
  findMentionAtCaret,
  extractMentionRequest,
  extractMentionRequests,
  type BotMentionCandidate,
} from '../src/lib/bot-mentions.ts';

const ROSTER: BotMentionCandidate[] = [
  { handle: 'example-bot', displayName: 'Example Bot', description: 'Technical orchestrator' },
  { handle: 'bdhidentity', displayName: 'BDH Identity', description: 'Vault semantics' },
  { handle: 'bdhverifier', displayName: 'BDH Verifier', description: 'Verification' },
];

describe('findMentionAtCaret', () => {
  it('returns matches for a partial handle after @', () => {
    const match = findMentionAtCaret('fai @cro', 8, ROSTER);
    assert.ok(match);
    assert.strictEqual(match.start, 4);
    assert.strictEqual(match.end, 8);
    assert.strictEqual(match.query, 'cro');
    assert.deepStrictEqual(match.matches.map((c) => c.handle), ['example-bot']);
  });

  it('matches case-insensitively', () => {
    const match = findMentionAtCaret('@CROSS', 6, ROSTER);
    assert.ok(match);
    assert.deepStrictEqual(match.matches.map((c) => c.handle), ['example-bot']);
  });

  it('returns null for email-like token without boundary', () => {
    assert.strictEqual(findMentionAtCaret('ciao@azienda.it', 15, ROSTER), null);
  });

  it('returns null for unknown handle', () => {
    assert.strictEqual(findMentionAtCaret('@sconosciuto', 12, ROSTER), null);
  });

  it('returns null when roster is empty', () => {
    assert.strictEqual(findMentionAtCaret('@cro', 4, []), null);
  });

  it('shows the full roster for a bare @', () => {
    const match = findMentionAtCaret('@', 1, ROSTER);
    assert.ok(match);
    assert.strictEqual(match.query, '');
    assert.deepStrictEqual(match.matches.map((c) => c.handle), ['example-bot', 'bdhidentity', 'bdhverifier']);
  });

  it('returns null when caret is not at the mention token', () => {
    assert.strictEqual(findMentionAtCaret('@example-bot poi', 17, ROSTER), null);
  });
});

describe('extractMentionRequest', () => {
  it('extracts mention and trailing request', () => {
    const parsed = extractMentionRequest('@example-bot analizza questo trace', ROSTER);
    assert.deepStrictEqual(parsed, { mention: '@example-bot', request: 'analizza questo trace' });
  });

  it('extracts mention in the middle of the input', () => {
    const parsed = extractMentionRequest('fai questo @example-bot poi dimmi', ROSTER);
    assert.deepStrictEqual(parsed, { mention: '@example-bot', request: 'poi dimmi' });
  });

  it('returns null for email-like text', () => {
    assert.strictEqual(extractMentionRequest('scrivi a ciao@azienda.it', ROSTER), null);
  });

  it('returns null for unknown handle', () => {
    assert.strictEqual(extractMentionRequest('@sconosciuto fai x', ROSTER), null);
  });

  it('returns null when roster is empty', () => {
    assert.strictEqual(extractMentionRequest('@example-bot fai x', []), null);
  });

  it('matches handle case-insensitively and returns canonical casing', () => {
    const parsed = extractMentionRequest('@Example Bot fai x', ROSTER);
    assert.strictEqual(parsed?.mention, '@example-bot');
  });
});

describe('extractMentionRequests', () => {
  it('resolves each mention with only its own trailing text', () => {
    const parsed = extractMentionRequests('@example-bot e @bdhidentity cosa ne pensate?', ROSTER);
    assert.deepStrictEqual(parsed, [
      { mention: '@example-bot', request: 'e' },
      { mention: '@bdhidentity', request: 'cosa ne pensate?' },
    ]);
    assert.ok(parsed.every((item) => !item.request.includes('@')));
  });
});
