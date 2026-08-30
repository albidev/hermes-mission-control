import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';

const root = new URL('../', import.meta.url);
const pageHeaderPath = new URL('src/components/PageHeader.tsx', root);
const sessions = readFileSync(new URL('src/routes/SessionsRoute.tsx', root), 'utf8');
const agents = readFileSync(new URL('src/routes/AgentsRoute.tsx', root), 'utf8');
const app = readFileSync(new URL('src/App.tsx', root), 'utf8');

assert.ok(existsSync(pageHeaderPath), 'PageHeader must exist as a shared component');
const pageHeader = readFileSync(pageHeaderPath, 'utf8');
for (const className of ['route-page-header', 'route-page-header-copy', 'route-page-header-actions']) {
  assert.match(pageHeader, new RegExp(className), `${className} must be defined by PageHeader`);
}
assert.match(sessions, /PageHeader/, 'Sessions must use the shared PageHeader');
assert.match(agents, /PageHeader/, 'Agents must use the shared PageHeader');
assert.match(agents, /TRACE_ACTION_FILTERS\.filter\(/, 'Action taxonomy should hide empty filter chips');
assert.match(agents, /flex-nowrap items-center gap-1\.5 overflow-x-auto/, 'Trace controls must use a horizontal mobile rail');
assert.match(agents, /max-w-\[calc\(100%\+1\.5rem\)\] flex-nowrap items-center gap-1\.5 overflow-x-auto/, 'Action taxonomy must use an edge-to-edge mobile rail');
assert.match(agents, /line-clamp-2 text-xs text-text-muted/, 'Timeline previews should be capped on mobile');
assert.match(agents, /flex min-w-0 flex-nowrap items-center gap-2 overflow-x-auto/, 'Timeline metadata must not wrap into a tall mobile card');
assert.match(agents, /role="group" aria-label=\{t\('agents\.view'\)\}/, 'Trace view controls must be grouped semantically');
assert.match(agents, /role="group" aria-label=\{t\('agents\.stream'\)\}/, 'Trace stream control must be grouped semantically');
assert.match(agents, /role="group" aria-label=\{t\('agents\.scope'\)\}/, 'Trace scope controls must be grouped semantically');
assert.match(agents, /aria-pressed=\{liveMode\}/, 'Stream mode must expose its pressed state');
assert.doesNotMatch(agents, /AgentRegistryCard|selectedAgentId|agents\.registry/, 'Agents must stay focused on session tracing, not the historical registry');
assert.match(sessions, /navigate\(`\/agents\?session=/, 'Sessions must be the navigation source for opening a trace');
assert.doesNotMatch(app, /path="agents\/:agentId"/, 'Agents must use session query navigation instead of agent-id routes');

console.log('sessions/agents shared header and trace mobile contract passed');
