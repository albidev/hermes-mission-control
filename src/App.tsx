import { lazy, useEffect, useState } from 'react';
import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom';
import { MissionControlProvider } from './lib/mission-control-store';
import { I18nProvider } from './lib/i18n';
import { MissionControlShell } from './components/MissionControlShell';
import { OverviewLayout } from './routes/OverviewRoute';
import { OverviewDashboard } from './components/overview/OverviewDashboard';
import { PluginRegistry } from './core/plugins/registry';
import { setPluginRegistry } from './core/plugin-registry';
import { loadPlugins, type InternalPlugin } from './core/plugins/plugin-loader';

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

// Plugin registry — populated at runtime from installed plugins
const registry = new PluginRegistry();
setPluginRegistry(registry);

function App() {
  const [pluginRoutes, setPluginRoutes] = useState<React.ReactElement[]>([]);
  const [pluginNavItems, setPluginNavItems] = useState<any[]>([]);
  const [pluginsLoaded, setPluginsLoaded] = useState(false);

  useEffect(() => {
    loadPlugins().then((plugins) => {
      if (plugins.length > 0) {
        registry.load(plugins);
        const routes = registry.getRoutes().map((r) => (
          <Route key={r.path} path={r.path} element={r.element} />
        ));
        setPluginRoutes(routes);
        setPluginNavItems(registry.getNavItems());
      }
      setPluginsLoaded(true);
    });
  }, []);

  if (!pluginsLoaded) {
    return null; // or loading spinner
  }

  return (
    <I18nProvider>
      <MissionControlProvider>
        <BrowserRouter>
          <Routes>
            <Route element={<MissionControlShell registry={registry} navItems={pluginNavItems} />}>
              <Route element={<OverviewLayout />}>
                <Route index element={<OverviewDashboard />} />
              </Route>
              {defaultRoutes.map((r) => (
                <Route key={r.path} path={r.path} element={r.element} />
              ))}
              {pluginRoutes}
              <Route path="*" element={<Navigate to="/" replace />} />
            </Route>
          </Routes>
        </BrowserRouter>
      </MissionControlProvider>
    </I18nProvider>
  );
}

export default App;
