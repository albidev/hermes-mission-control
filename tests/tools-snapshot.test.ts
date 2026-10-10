import assert from 'node:assert/strict';
import test from 'node:test';
import { toolsSnapshotFromToolsets } from '../src/lib/tools-snapshot.ts';

// Rows shaped like the core dashboard's GET /api/tools/toolsets
// (hermes_cli/web_routers/tools.py: get_toolsets).
function row(name: string, overrides: Record<string, unknown> = {}) {
  return {
    name,
    label: `${name} label`,
    description: `${name}_a, ${name}_b`,
    platform: 'cli',
    platform_label: 'CLI',
    enabled: true,
    available: true,
    configured: true,
    tools: [`${name}_a`, `${name}_b`],
    ...overrides,
  };
}

test('an enabled, configured toolset maps to an available group with the real tools from core', () => {
  const snapshot = toolsSnapshotFromToolsets([row('web')]);
  assert.deepEqual(snapshot.toolsets, [{
    name: 'web',
    description: 'web label',
    directTools: ['web_a', 'web_b'],
    includes: [],
    resolvedTools: ['web_a', 'web_b'],
    toolCount: 2,
    isComposite: false,
    available: true,
    configured: true,
  }]);
  assert.equal(snapshot.available, true);
  assert.equal(snapshot.count, 1);
  assert.deepEqual(snapshot.availableToolsets, snapshot.toolsets);
});

test('availability follows enabled, and missing credentials follow configured', () => {
  const snapshot = toolsSnapshotFromToolsets([
    row('kanban', { enabled: false, available: false }),
    row('discord', { configured: false }),
  ]);
  const byName = Object.fromEntries(snapshot.toolsets.map((item) => [item.name, item]));
  assert.equal(byName.kanban.available, false);
  assert.equal(byName.kanban.configured, true);
  assert.equal(byName.discord.available, true);
  assert.equal(byName.discord.configured, false);
});

test('the tool catalog lists every real tool once, with its first toolset', () => {
  const snapshot = toolsSnapshotFromToolsets([
    row('browser', { tools: ['browser_click', 'shared_tool'] }),
    row('web', { tools: ['web_search', 'shared_tool'], enabled: false }),
  ]);
  assert.deepEqual(snapshot.toolCatalog, [
    { name: 'browser_click', toolset: 'browser', available: true },
    { name: 'shared_tool', toolset: 'browser', available: true },
    { name: 'web_search', toolset: 'web', available: false },
  ]);
  assert.deepEqual(snapshot.resolvedTools, ['browser_click', 'shared_tool', 'web_search']);
  assert.equal(snapshot.toolCount, 3);
});

test('rows without a name are skipped and a missing tool list is empty', () => {
  const snapshot = toolsSnapshotFromToolsets([null, { name: '' }, 'web', { name: 'stt', label: 'Speech-to-Text', enabled: true, configured: true }]);
  assert.deepEqual(snapshot.toolsets.map((item) => [item.name, item.toolCount, item.resolvedTools]), [['stt', 0, []]]);
});

test('the label falls back to the core description when no label is sent', () => {
  const snapshot = toolsSnapshotFromToolsets([row('todo', { label: undefined, description: 'todo_list' })]);
  assert.equal(snapshot.toolsets[0].description, 'todo_list');
});

test('a payload that is not a toolset list is rejected', () => {
  assert.throws(() => toolsSnapshotFromToolsets({ toolsets: [] }), TypeError);
  assert.throws(() => toolsSnapshotFromToolsets(null), TypeError);
});
