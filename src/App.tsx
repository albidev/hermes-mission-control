import { lazy, Suspense } from 'react';
import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom';
import { MissionControlProvider } from './lib/mission-control-store';
import { I18nProvider } from './lib/i18n';
import { MissionControlShell } from './components/MissionControlShell';
import { OverviewLayout } from './routes/OverviewRoute';
import { OverviewDashboard } from './components/overview/OverviewDashboard';

const SessionsRoute = lazy(() => import('./routes/SessionsRoute').then((m) => ({ default: m.SessionsRoute })));
const AgentsRoute = lazy(() => import('./routes/AgentsRoute').then((m) => ({ default: m.AgentsRoute })));
const UsageRoute = lazy(() => import('./routes/UsageRoute').then((m) => ({ default: m.UsageRoute })));
const KnowledgeRoute = lazy(() => import('./routes/KnowledgeRoute').then((m) => ({ default: m.KnowledgeRoute })));
const ToolsRoute = lazy(() => import('./routes/ToolsRoute').then((m) => ({ default: m.ToolsRoute })));
const SkillsRoute = lazy(() => import('./routes/SkillsRoute').then((m) => ({ default: m.SkillsRoute })));
const ConfigRoute = lazy(() => import('./routes/ConfigRoute').then((m) => ({ default: m.ConfigRoute })));
const LogsRoute = lazy(() => import('./routes/LogsRoute').then((m) => ({ default: m.LogsRoute })));
const CurateRoute = lazy(() => import('./routes/CurateRoute').then((m) => ({ default: m.CurateRoute })));
const KanbanRoute = lazy(() => import('./routes/KanbanRoute').then((m) => ({ default: m.KanbanRoute })));

function App() {
  return (
    <I18nProvider>
      <MissionControlProvider>
        <BrowserRouter>
          <Suspense fallback={<div className="route-loading" role="status">Loading Mission Control…</div>}>
            <Routes>
              <Route element={<MissionControlShell />}>
                <Route element={<OverviewLayout />}>
                  <Route index element={<OverviewDashboard />} />
                </Route>
                <Route path="sessions" element={<SessionsRoute />} />
                <Route path="agents" element={<AgentsRoute />} />
                <Route path="agents/:agentId" element={<AgentsRoute />} />
                <Route path="usage" element={<UsageRoute />} />
                <Route path="knowledge" element={<KnowledgeRoute />} />
                <Route path="tools" element={<ToolsRoute />} />
                <Route path="skills" element={<SkillsRoute />} />
                <Route path="config" element={<ConfigRoute />} />
                <Route path="logs" element={<LogsRoute />} />
                <Route path="curate" element={<CurateRoute />} />
                <Route path="kanban" element={<KanbanRoute />} />
                <Route path="*" element={<Navigate to="/" replace />} />
              </Route>
            </Routes>
          </Suspense>
        </BrowserRouter>
      </MissionControlProvider>
    </I18nProvider>
  );
}

export default App;
