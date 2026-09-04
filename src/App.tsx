import { lazy } from 'react';
import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom';
import { MissionControlProvider } from './lib/mission-control-store';
import { I18nProvider } from './lib/i18n';
import { MissionControlShell } from './components/MissionControlShell';
import { OverviewLayout } from './routes/OverviewRoute';
import { OverviewDashboard } from './components/overview/OverviewDashboard';
import { PluginRegistry } from './core/plugins/registry';
import { CuratePlugin } from './plugins/curate/route';
import { curateManifest } from './plugins/curate/manifest';

// Busy routes — lazy-loaded
const SessionsRoute = lazy(() => import('./routes/SessionsRoute').then((m) => ({ default: m.SessionsRoute })));
const AgentsRoute = lazy(() => import('./routes/AgentsRoute').then((m) => ({ default: m.AgentsRoute })));
const UsageRoute = lazy(() => import('./routes/UsageRoute').then((m) => ({ default: m.UsageRoute })));
const ToolsRoute = lazy(() => import('./routes/ToolsRoute').then((m) => ({ default: m.ToolsRoute })));
const CronRoute = lazy(() => import('./routes/CronRoute').then((m) => ({ default: m.CronRoute })));
const SkillsRoute = lazy(() => import('./routes/SkillsRoute').then((m) => ({ default: m.SkillsRoute })));
const ConfigRoute = lazy(() => import('./routes/ConfigRoute').then((m) => ({ default: m.ConfigRoute })));
const LogsRoute = lazy(() => import('./routes/LogsRoute').then((m) => ({ default: m.LogsRoute })));
const KanbanRoute = lazy(() => import('./routes/KanbanRoute').then((m) => ({ default: m.KanbanRoute })));

// Plugin registry — initialized synchronously at module level so routes
// and nav items exist from the very first render (no async gap).
const registry = new PluginRegistry();
registry.load([
  {
    manifest: curateManifest,
    component: CuratePlugin,
    loadRoute: () => Promise.resolve({ default: CuratePlugin }),
  },
]);

// Default routes (hardcoded, non-plugin)
const defaultRoutes = [
  { path: 'sessions', element: <SessionsRoute /> },
  { path: 'agents', element: <AgentsRoute /> },
  { path: 'usage', element: <UsageRoute /> },
  { path: 'tools', element: <ToolsRoute /> },
  { path: 'cron', element: <CronRoute /> },
  { path: 'skills', element: <SkillsRoute /> },
  { path: 'config', element: <ConfigRoute /> },
  { path: 'logs', element: <LogsRoute /> },
  { path: 'kanban', element: <KanbanRoute /> },
];

// Plugin routes (available from first render)
const pluginRoutes = registry.getRoutes();

function App() {
  return (
    <I18nProvider>
      <MissionControlProvider>
        <BrowserRouter>
          <Routes>
            <Route element={<MissionControlShell registry={registry} />}>
              <Route element={<OverviewLayout />}>
                <Route index element={<OverviewDashboard />} />
              </Route>
              {defaultRoutes.map((r) => (
                <Route key={r.path} path={r.path} element={r.element} />
              ))}
              {/* Plugin routes */}
              {pluginRoutes.map((r) => (
                <Route key={r.path} path={r.path} element={r.element} />
              ))}
              <Route path="*" element={<Navigate to="/" replace />} />
            </Route>
          </Routes>
        </BrowserRouter>
      </MissionControlProvider>
    </I18nProvider>
  );
}

export default App;
