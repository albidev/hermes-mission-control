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

test('mobile sessions layout avoids nested scrolling and an oversized filter header', () => {
  assert.doesNotMatch(source, /flex h-full flex-col gap-5 overflow-y-auto/);
  assert.match(source, /flex flex-nowrap gap-1\.5 overflow-x-auto/);
  assert.match(source, /grid-cols-2 gap-2/);
  assert.match(source, /className="hidden sm:block"/);
  assert.doesNotMatch(source, /compactOnMobile/);
  assert.match(source, /filtersOpen/);
  assert.match(source, /max-w-full/);
  assert.match(source, /min-w-0[^\n]*truncate/);
  assert.match(source, /w-full[^\n]*justify-end[^\n]*sm:ml-auto/);
});

test('session loading has a bounded failure state and a centered mobile refresh icon', () => {
  assert.match(source, /SESSION_LOAD_TIMEOUT_MS/);
  assert.match(source, /loadError/);
  assert.match(source, /Unable to load sessions/);
  assert.match(source, /justify-center/);
});

test('desktop search and dropdown controls share the same height contract', () => {
  assert.equal((source.match(/h-9/g) ?? []).length, 5);
  assert.equal((source.match(/py-0(?:\s|")/g) ?? []).length, 5);
});

test('tab changes expose loading state, reset stale totals, and expand filtered groups', () => {
  assert.match(source, /role="status"/);
  assert.match(source, /loading && !loadingMore/);
  assert.match(source, /setFilteredTotal\(0\)/);
  assert.match(source, /setCollapsedGroups\(new Set\(\)\)/);
  assert.match(source, /onClick=\{\(\) => selectTab\(value\)\}/);
  assert.match(source, /const groupsForcedOpen = tab !== 'all' \|\| activeFilterCount > 0/);
  assert.match(source, /collapsed=\{groupsForcedOpen \? false : collapsedGroups\.has\(category\)\}/);
});

test('active tab count uses the filtered result total instead of global facets', () => {
  assert.match(source, /if \(value === tab\) return loading \? null : filteredTotal/);
  assert.match(source, /tabCount\(value\) === null \? '…' : tabCount\(value\)/);
});

console.log('sessions UI contract tests passed');
