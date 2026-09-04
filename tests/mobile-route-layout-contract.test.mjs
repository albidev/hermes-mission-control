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
  let path;
  if (file === 'OverviewDashboard.tsx') {
    path = new URL('src/components/overview/OverviewDashboard.tsx', root);
  } else if (file === 'CurateRoute.tsx') {
    // Curate is a self-contained plugin — its UI lives under src/plugins/curate/
    path = new URL('src/plugins/curate/CurateRoute.tsx', root);
  } else {
    path = new URL(`src/routes/${file}`, root);
  }
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

const logs = readFileSync(new URL('src/routes/LogsRoute.tsx', root), 'utf8');
assert.match(logs, /logs-page route-page-scroll/, 'Logs must use the dedicated page scroll owner');
assert.match(logs, /a\.name\.localeCompare\(b\.name, undefined, \{ numeric: true \}\)/, 'Log file pills must keep a stable order across refreshes');
assert.match(logs, /logs-scroll-top/, 'Logs must expose a scroll-to-top control');
assert.match(logs, /logs-auto-refresh/, 'Logs must expose an auto-refresh selector');
assert.match(logs, /value=\{1000\}/, 'Logs auto-refresh selector must include the 1 second preset');
assert.match(logs, /value=\{2000\}/, 'Logs auto-refresh selector must include the 2 second preset');
assert.match(logs, /value=\{5000\}/, 'Logs auto-refresh selector must include the 5 second preset');
assert.match(logs, /logs-refresh-select/, 'Logs must use the dedicated refresh select styling');
assert.match(logs, /className="hidden sm:block"/, 'Logs file stats must be hidden on mobile');
assert.match(logs, /px-4 py-10.*role="status"/, 'Logs must expose the initial loading state inside the stream card');
assert.match(logs, /Loader2 className="h-4 w-4 animate-spin"/, 'Logs initial loading state must use a compact spinner');
assert.match(styles, /\.logs-refresh-select\s*\{[\s\S]*?height:\s*44px[\s\S]*?min-height:\s*44px/, 'Logs refresh select must be explicitly 44px tall');
assert.match(styles, /\.logs-scroll-top\s*\{[\s\S]*?position:\s*fixed/, 'Logs scroll-to-top control must float above the route content');

console.log('mobile route layout contract tests passed');
