import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';

const source = readFileSync(new URL('../src/lib/reload-diagnostics.ts', import.meta.url), 'utf8');

test('reload diagnostics only uses keepalive for unload fallback', () => {
  assert.match(source, /keepalive:\s*unload/);
  assert.doesNotMatch(source, /keepalive:\s*true/);
});

test('boot diagnostics do not resend the full breadcrumb payload', () => {
  assert.doesNotMatch(source, /previousBreadcrumbs:\s*previous[,}]/);
  assert.match(source, /previousBreadcrumbCount:\s*previous\.length/);
});

console.log('reload diagnostics contract tests passed');
