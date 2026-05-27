import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom';
import { MissionControlProvider } from './lib/mission-control-store';
import { MissionControlShell } from './components/MissionControlShell';
import { OverviewLayout } from './routes/OverviewRoute';
import { OverviewDashboard } from './components/overview/OverviewDashboard';
import { SessionsRoute } from './routes/SessionsRoute';
import { AgentsRoute } from './routes/AgentsRoute';
import { UsageRoute } from './routes/UsageRoute';
import { KnowledgeRoute } from './routes/KnowledgeRoute';
import { ToolsRoute } from './routes/ToolsRoute';
import { SkillsRoute } from './routes/SkillsRoute';
import { ConfigRoute } from './routes/ConfigRoute';
import { LogsRoute } from './routes/LogsRoute';

function App() {
  return (
    <MissionControlProvider>
      <BrowserRouter>
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
            <Route path="*" element={<Navigate to="/" replace />} />
          </Route>
        </Routes>
      </BrowserRouter>
    </MissionControlProvider>
  );
}

export default App;
