import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const root = new URL('../', import.meta.url);
const styles = readFileSync(new URL('src/styles.css', root), 'utf8');
const routeFiles = [
  'OverviewDashboard.tsx',
  'SessionsRoute.tsx',
  'KanbanRoute.tsx',
  'AgentsRoute.tsx',
  'UsageRoute.tsx',
  'KnowledgeRoute.tsx',
  'ToolsRoute.tsx',
  'CronRoute.tsx',
  'SkillsRoute.tsx',
  'ConfigRoute.tsx',
  'LogsRoute.tsx',
];

assert.match(styles, /\.route-page-scroll\s*\{/);
assert.match(styles, /scroll-padding-bottom:\s*max\(1rem, env\(safe-area-inset-bottom\)\)/);
assert.match(styles, /padding-bottom:\s*max\(1rem, env\(safe-area-inset-bottom\)\)/);
assert.match(styles, /\.route-stage\s*\{[\s\S]*?overflow:\s*hidden;/);
assert.match(styles, /\.route-stage:not\(\.is-overview\)\s*\{[\s\S]*?padding:[^;]*0;/);

for (const file of routeFiles) {
  const path = file === 'OverviewDashboard.tsx'
    ? new URL('src/components/overview/OverviewDashboard.tsx', root)
    : new URL(`src/routes/${file}`, root);
  const source = readFileSync(path, 'utf8');
  assert.match(source, /route-page-scroll/, `${file} must use the shared mobile scroll container`);
}

console.log('mobile route layout contract tests passed');
