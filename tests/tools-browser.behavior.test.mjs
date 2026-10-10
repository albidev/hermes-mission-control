import assert from 'node:assert/strict';
import { routeHarness } from './helpers/route-dom-harness.mjs';

// Shape produced by _collect_tools: identical lists, available groups, no
// requirements, and direct names except the three skills operations.
function group(name, resolvedTools = [name]) {
  return {
    name,
    available: true,
    resolvedTools,
    toolCount: resolvedTools.length,
    description: `Fixture ${name}`,
    requirements: [],
    directTools: resolvedTools,
    includes: [],
    isComposite: false,
  };
}

function snapshot(toolsets) {
  const toolCatalog = toolsets.flatMap(item => item.resolvedTools.map(name => ({
    name,
    toolset: item.name,
    available: true,
  })));
  return {
    available: true,
    count: toolsets.length,
    toolCount: toolCatalog.length,
    toolsets,
    availableToolsets: toolsets,
    toolCatalog,
    resolvedTools: toolCatalog.map(item => item.name),
  };
}

const store = {
  tools: snapshot([group('terminal'), group('file'), group('skills', ['list', 'view', 'manage'])]),
  storedToken: 'fixture',
};
const h = await routeHarness('ToolsRoute', store, () => {
  throw Error('Tools search must not fetch');
});
const groupNames = () => [...document.querySelectorAll('[data-toolset]')]
  .map(item => item.getAttribute('data-toolset'));

try {
  const search = h.find('input[type="search"][aria-label="tools.search"]');
  assert.deepEqual(groupNames(), ['terminal', 'file', 'skills']);
  assert.ok(!document.querySelector('[data-tool-catalog]'),
    'Do not repeat the synthetic backend catalog as a separate card');
  assert.ok(!document.querySelector('[aria-expanded]'));
  assert.ok(!document.body.textContent.includes('tools.requirements'));
  assert.ok(!document.body.textContent.includes('tools.unavailable'));

  await h.change(search, ' TERMINAL ');
  assert.deepEqual(groupNames(), ['terminal']);
  await h.change(search, 'MANAGE');
  assert.deepEqual(groupNames(), ['skills']);
  assert.ok(h.find('[data-toolset="skills"]').textContent.includes('manage'));

  await h.change(search, 'no-such-tool');
  assert.deepEqual(groupNames(), []);
  assert.ok(document.body.textContent.includes('tools.noMatch'));
  await h.click('tools.resetSearch');
  assert.equal(search.value, '');
  assert.deepEqual(groupNames(), ['terminal', 'file', 'skills']);

  await h.change(search, 'new-tool');
  store.tools = snapshot([group('new-tool')]);
  await h.emit();
  assert.deepEqual(groupNames(), ['new-tool']);

  store.tools = snapshot([]);
  await h.emit();
  assert.ok(document.body.textContent.includes('tools.notFound'));
  assert.equal(h.calls.length, 0);
  console.log('Tools search passed: production-shaped data, case/whitespace, tool names, reset, empty/update states.');
} finally {
  await h.close();
}
