import assert from 'node:assert/strict';
import { filterInstalledSkills } from '../src/lib/installed-skills-filter.ts';
const items = [
  { name: 'Zeta', description: 'Deploy runner', tags: ['ops'], enabled: true },
  { name: 'Alpha', description: 'Write reports', tags: ['DOCX'], enabled: false },
  { name: 'Beta', description: 'Another deploy helper', tags: [], enabled: false },
];
const original = structuredClone(items);
assert.deepEqual(filterInstalledSkills(items, '  DEPLOY ', 'all'), [items[0], items[2]]);
assert.deepEqual(filterInstalledSkills(items, 'docx', 'disabled'), [items[1]]);
assert.deepEqual(filterInstalledSkills(items, '', 'enabled'), [items[0]]);
assert.deepEqual(filterInstalledSkills(items, 'deploy', 'disabled'), [items[2]]);
assert.deepEqual(filterInstalledSkills(items, 'none', 'all'), []);
assert.deepEqual(filterInstalledSkills(items, '', 'all'), items);
assert.deepEqual(filterInstalledSkills([], '', 'all'), []);
assert.deepEqual(items, original);
console.log('Installed skills filter passed.');
