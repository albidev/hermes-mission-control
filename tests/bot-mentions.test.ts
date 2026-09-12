import { describe, it } from 'node:test';
import { strict as assert } from 'node:assert';
import {
  findMentionAtCaret,
  extractMentionRequest,
  type BotMentionCandidate,
} from '../src/lib/bot-mentions.ts';

const ROSTER: BotMentionCandidate[] = [
  { handle: 'crossnection', displayName: 'Crossnection', description: 'Technical orchestrator' },
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
    assert.deepStrictEqual(match.matches.map((c) => c.handle), ['crossnection']);
  });

  it('matches case-insensitively', () => {
    const match = findMentionAtCaret('@CROSS', 6, ROSTER);
    assert.ok(match);
    assert.deepStrictEqual(match.matches.map((c) => c.handle), ['crossnection']);
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
    assert.deepStrictEqual(match.matches.map((c) => c.handle), ['crossnection', 'bdhidentity', 'bdhverifier']);
  });

  it('returns null when caret is not at the mention token', () => {
    assert.strictEqual(findMentionAtCaret('@crossnection poi', 17, ROSTER), null);
  });
});

describe('extractMentionRequest', () => {
  it('extracts mention and trailing request', () => {
    const parsed = extractMentionRequest('@crossnection analizza questo trace', ROSTER);
    assert.deepStrictEqual(parsed, { mention: '@crossnection', request: 'analizza questo trace' });
  });

  it('extracts mention in the middle of the input', () => {
    const parsed = extractMentionRequest('fai questo @crossnection poi dimmi', ROSTER);
    assert.deepStrictEqual(parsed, { mention: '@crossnection', request: 'poi dimmi' });
  });

  it('returns null for email-like text', () => {
    assert.strictEqual(extractMentionRequest('scrivi a ciao@azienda.it', ROSTER), null);
  });

  it('returns null for unknown handle', () => {
    assert.strictEqual(extractMentionRequest('@sconosciuto fai x', ROSTER), null);
  });

  it('returns null when roster is empty', () => {
    assert.strictEqual(extractMentionRequest('@crossnection fai x', []), null);
  });

  it('matches handle case-insensitively and returns canonical casing', () => {
    const parsed = extractMentionRequest('@CrossNection fai x', ROSTER);
    assert.strictEqual(parsed?.mention, '@crossnection');
  });
});
