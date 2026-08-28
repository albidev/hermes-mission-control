import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';

const source = readFileSync(new URL('../src/routes/SessionsRoute.tsx', import.meta.url), 'utf8');

test('session details use the shared modal instead of a desktop side panel', () => {
  assert.match(source, /<Modal[\s\S]*\bopen\b/);
  assert.doesNotMatch(source, /lg:grid-cols-\[minmax\(0,1fr\)_340px\]/);
  assert.match(source, /<div className="flex flex-col gap-4">/);
});

test('session page surfaces do not render the bright default card border', () => {
  assert.match(source, /<Card padding="none" className="min-w-0 !border-0">/);
  assert.doesNotMatch(source, /Select a session/);
});

test('session rows and detail surfaces are fully borderless', () => {
  assert.doesNotMatch(source, /border-l-2/);
  assert.doesNotMatch(source, /border-l-positive/);
  assert.doesNotMatch(source, /border-positive/);
  assert.doesNotMatch(source, /border border-border-subtle/);
  assert.doesNotMatch(source, /border-b border-border-subtle/);
  assert.match(source, /<Modal[\s\S]*\bborderless\b/);
});

test('session actions use shared full-size buttons', () => {
  assert.match(source, /import \{ Button \} from ['"]\.\.\/components\/ui\/Button['"];?/);
  assert.match(source, /label="Resume"/);
  assert.match(source, /label="Trace"/);
  assert.match(source, /label="Inspect"/);
  assert.match(source, /size="sm"/);
});

console.log('sessions UI contract tests passed');
