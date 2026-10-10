import assert from 'node:assert/strict';
import { routeHarness } from './helpers/route-dom-harness.mjs';
import { toolsSnapshotFromToolsets } from '../src/lib/tools-snapshot.ts';

// Rows shaped like the core dashboard's GET /api/tools/toolsets, mapped by the
// same function the loader uses: what the route renders for real users.
function row(name, tools = [name], overrides = {}) {
  return {
    name,
    label: `${name} label`,
    description: tools.join(', '),
    platform: 'cli',
    platform_label: 'CLI',
    enabled: true,
    available: true,
    configured: true,
    tools,
    ...overrides,
  };
}

const browserTools = Array.from({ length: 18 }, (_, index) => `browser_tool_${String(index + 1).padStart(2, '0')}`);
const coreRows = [
  row('terminal', ['process', 'terminal']),
  row('browser', browserTools),
  row('skills', ['skill_manage', 'skill_view', 'skills_list']),
  row('kanban', ['kanban_create', 'kanban_list'], { enabled: false, available: false }),
  row('discord', ['discord'], { configured: false }),
];

const store = { tools: toolsSnapshotFromToolsets(coreRows), storedToken: 'fixture' };
const h = await routeHarness('ToolsRoute', store, () => {
  throw Error('Tools search must not fetch');
});
const groupNames = () => [...document.querySelectorAll('[data-toolset]')]
  .map(item => item.getAttribute('data-toolset'));
const group = name => h.find(`[data-toolset="${name}"]`);
const metric = label => [...document.querySelectorAll('span')]
  .find(item => item.textContent === label)?.nextElementSibling?.textContent;

try {
  const search = h.find('input[type="search"][aria-label="tools.search"]');
  assert.deepEqual(groupNames(), ['terminal', 'browser', 'skills', 'kanban', 'discord']);

  // Status comes from core config: enabled per platform, credentials per profile.
  assert.ok(group('terminal').textContent.includes('tools.available'));
  assert.ok(group('kanban').textContent.includes('tools.disabled'));
  assert.ok(!group('kanban').textContent.includes('tools.available'));
  assert.ok(group('discord').textContent.includes('tools.needsKey'));
  assert.equal(metric('tools.toolsets'), '5');
  assert.equal(metric('tools.ready'), '3', 'enabled and configured');
  assert.equal(metric('tools.needsKeys'), '1', 'only toolsets missing credentials');
  assert.equal(metric('tools.tools'), String(2 + 18 + 3 + 2 + 1));

  // A large toolset shows the first 8 tools and says how many are hidden.
  assert.ok(group('browser').textContent.includes('browser_tool_08'));
  assert.ok(!group('browser').textContent.includes('browser_tool_17'));
  assert.ok(group('browser').textContent.includes('tools.moreTools 10'));

  // Searching a tool hidden past the first 8 shows that tool in its group.
  await h.change(search, 'BROWSER_TOOL_17 ');
  assert.deepEqual(groupNames(), ['browser']);
  assert.ok(group('browser').textContent.includes('browser_tool_17'));

  await h.change(search, 'skill_view');
  assert.deepEqual(groupNames(), ['skills']);
  await h.change(search, 'no-such-tool');
  assert.deepEqual(groupNames(), []);
  assert.ok(document.body.textContent.includes('tools.noMatch'));
  await h.click('tools.resetSearch');
  assert.equal(search.value, '');
  assert.equal(groupNames().length, 5);

  store.tools = toolsSnapshotFromToolsets([]);
  await h.emit();
  assert.ok(document.body.textContent.includes('tools.notFound'));
  assert.equal(h.calls.length, 0);
  console.log('Tools route passed: core-shaped rows, enabled/configured status, metrics, hidden-tool search, empty state.');
} finally {
  await h.close();
}
