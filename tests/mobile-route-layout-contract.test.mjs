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
  'CurateRoute.tsx',
];

assert.match(styles, /\.route-page-scroll\s*\{[\s\S]*?overflow-x:\s*hidden;/, 'Route pages must not expose a horizontal page scroll');
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

const cron = readFileSync(new URL('src/routes/CronRoute.tsx', root), 'utf8');
assert.match(cron, /cron-job-row/, 'Cron job rows must expose a safe-area-aware final row');

const config = readFileSync(new URL('src/routes/ConfigRoute.tsx', root), 'utf8');
assert.match(config, /config-action-bar-buttons[\s\S]*flex-nowrap[\s\S]*overflow-x-auto/, 'Config actions must stay on one horizontal mobile rail');
assert.match(config, /role="tablist"[\s\S]*?overflow-x-auto/, 'Config mode selector must use the shared horizontal tab rail');
assert.match(config, /className="shrink-0 whitespace-nowrap"[\s\S]*?role="tab"/, 'Config mode buttons must use the shared button treatment');
assert.match(config, /config-editor-mode-bar sticky top-0 z-20/, 'Config mode toolbar must not add a second bordered wrapper');
assert.doesNotMatch(config, /allSections[\s\S]*?Badge variant=\{dirty \? 'warning' : 'default'\}/, 'All config sections must not repeat the bottom sync status');
assert.match(styles, /\.config-action-bar\s*\{[\s\S]*?padding-bottom:\s*max\(0\.75rem, env\(safe-area-inset-bottom\)\)/, 'Config action bar must extend through the bottom safe area');
assert.match(styles, /@media \(max-width: 640px\)[\s\S]*?\.config-action-bar\s*\{[\s\S]*?bottom:\s*calc\(-1 \* max\(1rem, env\(safe-area-inset-bottom\)\) - 1px\)/, 'Config action bar must reach the bottom edge on mobile');
assert.match(styles, /\.config-action-bar\s*\{[\s\S]*?width:\s*calc\(100% \+ 1\.5rem \+ env\(safe-area-inset-left\)/, 'Config action bar must be full-bleed on mobile');
assert.match(styles, /\.config-action-bar::before[\s\S]*?bottom:\s*calc\(-1 \* env\(safe-area-inset-bottom\)\)/, 'Config frosted surface must bleed under the bottom safe area');
assert.match(styles, /\.config-action-bar::before[\s\S]*?backdrop-filter:\s*blur\(18px\) saturate\(1\.15\)/, 'Config action bar must keep a translucent frosted surface');

console.log('mobile route layout contract tests passed');
