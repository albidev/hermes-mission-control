import assert from 'node:assert/strict';
import { routeHarness } from './helpers/route-dom-harness.mjs';

const skill = (id, name, description, tags, enabled) => ({ id, name, description, tags, enabled, model: '', category: 'fixture' });
const items = [
  skill('z', 'Zeta', 'Deploy runner', ['ops'], true),
  skill('a', 'Alpha', 'Write reports', ['DOCX'], false),
  skill('b', 'Beta', 'Another deploy helper', [], false),
];
const store = {
  skills: { available: true, count: 3, skills: items, categories: [] },
  snapshot: { activeModel: 'fixture-model' },
  storedToken: 'synthetic-token',
  refreshAll: async () => { await h.emit(); },
};
let toggles = 0;

const h = await routeHarness('SkillsRoute', store, async (call) => {
  if (call.path === '/api/local/skills/catalog' && call.method === 'GET') {
    return Response.json({
      available: true,
      count: 2,
      skills: [
        {
          id: 'c1',
          name: 'Browse Alpha',
          description: 'catalog description',
          tags: [],
          identifier: 'fixture/alpha',
          source: 'fixture',
          trustLevel: 'builtin',
          installed: false,
        },
        {
          id: 'c2',
          name: 'Browse Beta',
          description: 'other',
          tags: [],
          identifier: 'fixture/beta',
          source: 'fixture',
          trustLevel: 'builtin',
          installed: false,
        },
      ],
      sources: {},
      timedOut: [],
    });
  }
  if (call.path === '/api/local/skills/files' && call.query === '?skill=Alpha' && call.method === 'GET') {
    return Response.json({ skill: 'Alpha', path: '/fixture', files: [{ name: 'SKILL.md', path: 'SKILL.md', size: 7, content: 'Detail Alpha' }] });
  }
  if (call.path === '/api/local/skills/toggle' && call.method === 'POST') {
    assert.deepEqual(call.body, { skillName: 'Beta', enabled: true });
    toggles++;
    store.skills = { ...store.skills, skills: items.map((s) => s.name === 'Beta' ? { ...s, enabled: true } : s) };
    return Response.json({ success: true });
  }
  throw Error(`Unexpected request ${call.method} ${call.path}`);
});
const names = () => [...document.querySelectorAll('article p.font-medium')].map((e) => e.textContent);

try {
  const search = h.find('input[type="search"][aria-label="skills.installedSearch"]');
  const status = h.find('select[aria-label="skills.installedStatus"]');
  assert.deepEqual(names(), ['Zeta', 'Alpha', 'Beta']);
  await h.change(search, '  DEPLOY ');
  assert.deepEqual(names(), ['Zeta', 'Beta']);
  assert.ok(document.body.textContent.includes('skills.visibleCount 2/3'));
  await h.change(status, 'disabled');
  assert.deepEqual(names(), ['Beta']);
  await h.change(search, 'DOCX');
  assert.deepEqual(names(), ['Alpha']);
  await h.change(search, 'write reports');
  assert.deepEqual(names(), ['Alpha']);
  await h.flush(() => h.find('button[aria-label="Alpha: Write reports"]').click());
  assert.ok(h.calls.some((c) => c.path === '/api/local/skills/files' && c.query === '?skill=Alpha'));
  assert.ok(document.body.textContent.includes('Detail Alpha'));
  await h.flush(() => document.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Escape', bubbles: true })));
  await h.change(search, 'nothing');
  assert.deepEqual(names(), []);
  assert.ok(document.body.textContent.includes('skills.installedNoMatch'));
  await h.click('skills.resetFilters');
  assert.equal(search.value, '');
  assert.equal(status.value, 'all');
  assert.deepEqual(names(), ['Zeta', 'Alpha', 'Beta']);
  assert.equal(h.calls.filter((c) => c.method !== 'GET').length, 0, 'Filtering and details never mutate');
  await h.change(search, 'deploy');
  await h.change(status, 'disabled');
  await h.flush(() => h.find('#toggle-b').click());
  assert.equal(toggles, 1);
  assert.deepEqual(names(), [], 'Refreshed toggle result updates the filtered list');
  await h.click('skills.catalog');
  const catalogSearch = h.find('input[aria-label="skills.search"]');
  await h.change(catalogSearch, 'Browse Alpha');
  assert.ok(document.body.textContent.includes('Browse Alpha'));
  assert.ok(!document.body.textContent.includes('Browse Beta'));
  await h.click('skills.installed');
  assert.equal(h.find('input[type="search"]').value, 'deploy');
  assert.deepEqual(names(), []);
  store.skills = { ...store.skills, count: 0, skills: [] };
  await h.emit();
  assert.ok(document.body.textContent.includes('skills.installedEmpty'));
  console.log('Skills installed behavior passed: search, filters, reset, detail, toggle refresh and independent catalog.');
} finally {
  await h.close();
}
