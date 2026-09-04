import assert from 'node:assert/strict';
import fs from 'node:fs';

const source = fs.readFileSync('src/plugins/curate/CurateRoute.tsx', 'utf8');

assert.match(source, /role="tablist"/);
assert.match(source, /role="tab"/);
assert.match(source, /activeTab/);
assert.match(source, /Candidates/);
assert.match(source, /Synthesis activity/);
assert.match(source, /Reverted \/ conflicts/);
assert.match(source, /View details/);
assert.match(source, /selectedActivity/);
assert.match(source, /Revert/);
assert.match(source, /aria-selected/);

console.log('Curate synthesis UI contract passed');
