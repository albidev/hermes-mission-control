import assert from 'node:assert/strict';
import { toolsetMatches } from '../src/lib/tools-browser.ts';
import type { MissionControlToolsetItem } from '../src/lib/hermes-api.ts';

const skills: MissionControlToolsetItem = {
  name: 'skills',
  available: true,
  description: 'Skills',
  requirements: [],
  directTools: ['list', 'view', 'manage'],
  includes: [],
  resolvedTools: ['list', 'view', 'manage'],
  toolCount: 3,
  isComposite: false,
};
const original = structuredClone(skills);

assert.equal(toolsetMatches(skills, ' SKILLS '), true);
assert.equal(toolsetMatches(skills, ' MANAGE '), true);
assert.equal(toolsetMatches(skills, 'missing'), false);
assert.equal(toolsetMatches(skills, ''), true);
assert.equal(toolsetMatches(skills, '   '), true);
assert.equal(toolsetMatches({ ...skills, resolvedTools: [] }, 'manage'), false);
assert.deepEqual(skills, original);
console.log('Tools search helper passed.');
