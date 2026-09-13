import { describe, it } from 'node:test';
import { strict as assert } from 'node:assert';
import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');

/**
 * Rendering contract for the handoff trace rows.
 *
 * The projection is unit-tested in handoff-trace.test.ts; this pins the OTHER half — that
 * both halves of a tool row reach the DOM with a label saying which is which. Without the
 * labels an input and an output render as two identical blocks, and for a `terminal` call
 * the output reads as a continuation of the command (the bug this guards).
 */
describe('handoff trace rendering contract', () => {
  it('labels input and output so the two blocks are distinguishable', async () => {
    const source = await readFile(join(ROOT, 'src/components/chat/BotHandoffMessage.tsx'), 'utf8');
    assert.match(source, /bots\.handoff\.traceInput/, 'the input block needs its own label');
    assert.match(source, /bots\.handoff\.traceOutput/, 'the output block needs its own label');
    assert.match(source, /\{row\.input \?/, 'the input must be rendered when present');
    assert.match(source, /\{row\.output \?/, 'the output must be rendered when present');
  });

  it('keeps both i18n labels in every locale', async () => {
    for (const locale of ['en', 'it']) {
      const raw = await readFile(join(ROOT, `src/locales/${locale}.json`), 'utf8');
      const parsed = JSON.parse(raw) as Record<string, string>;
      for (const key of ['bots.handoff.traceInput', 'bots.handoff.traceOutput', 'bots.handoff.traceReasoning', 'bots.handoff.traceLoading', 'bots.handoff.traceUnavailable']) {
        assert.ok(parsed[key], `${locale} is missing ${key}`);
      }
    }
  });
});
