import { Link } from 'react-router-dom';
import { Activity, ArrowRight, PanelLeftClose, RefreshCw, Rocket } from 'lucide-react';
import { useMissionControl } from '../../lib/mission-control-store';
import { Card } from '../ui/Card';
import { Badge } from '../ui/Badge';
import { Button } from '../ui/Button';
import { AgentStatusBar } from './AgentStatusBar';
import { SystemHealthPanel } from './SystemHealthPanel';
import { ActivityFeed } from './ActivityFeed';
import { AttentionNeeded } from './AttentionNeeded';
import { QuickActions } from './QuickActions';
import { AgentsPanel } from './AgentsPanel';
import { UsagePanel } from './UsagePanel';
import { formatRelativeTime } from '../../lib/format';

function SectionLink({
  to,
  label,
  children,
}: {
  to: string;
  label: string;
  children: React.ReactNode;
}) {
  return (
    <Link
      to={to}
      className="inline-flex items-center gap-1.5 text-xs text-text-muted hover:text-accent transition-colors duration-150"
    >
      {children}
      <span>{label}</span>
      <ArrowRight className="h-3 w-3" />
    </Link>
  );
}

function SectionCard({
  title,
  eyebrow,
  actions,
  children,
}: {
  title: string;
  eyebrow: string;
  actions?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <Card padding="none">
      <div className="flex items-center justify-between px-4 pt-4 pb-3 border-b border-border-subtle">
        <div className="flex flex-col gap-0.5">
          <span className="eyebrow">{eyebrow}</span>
          <h2 className="text-sm font-semibold text-text">{title}</h2>
        </div>
        {actions ? <div className="flex items-center gap-2">{actions}</div> : null}
      </div>
      <div className="p-4">{children}</div>
    </Card>
  );
}

export function OverviewDashboard() {
  const {
    snapshot,
    loading,
    lastUpdatedAt,
    actionLoading,
    gatewayActions,
    runGatewayAction,
  } = useMissionControl();

  const { machine, sessions, cron, alerts, backendHealth, gatewayStatus } = snapshot;

  const sessionItems = sessions.items.slice(0, 2);
  const cronItems = cron.items.slice(0, 3);
  const runningCron = cron.items.filter((job) => job.state === 'running').length;
  const queuedCron = cron.items.filter((job) => job.state === 'queued').length;

  const alertSignals: Array<{ label: string; detail: string; tone: 'good' | 'warn' | 'bad' }> = alerts.items
    .filter((alert) => alert.tone !== 'good')
    .slice(0, 2)
    .map((alert) => ({
      label: `${alert.category} alert`,
      detail: alert.title,
      tone: alert.tone === 'bad' ? 'bad' : 'warn',
    }));

  const cronSignals: Array<{ label: string; detail: string; tone: 'good' | 'warn' | 'bad' }> = cron.items
    .filter((job) => job.state === 'running' || job.state === 'queued')
    .slice(0, 2)
    .map((job) => ({
      label: `cron ${job.state}`,
      detail: `${job.label} · ${job.nextRunAt ? formatRelativeTime(job.nextRunAt) : job.scheduleDisplay}`,
      tone: job.state === 'running' ? 'good' : 'warn',
    }));

  const sessionSignals: Array<{ label: string; detail: string; tone: 'good' | 'warn' | 'bad' }> = sessions.items
    .slice(0, 1)
    .map((session) => ({
      label: 'session activity',
      detail: `${session.title} · ${session.messageCount} msgs · ${formatRelativeTime(session.lastActive)}`,
      tone: 'good',
    }));

  const eventSignals: Array<{ label: string; detail: string; tone: 'good' | 'warn' | 'bad' }> = [
    ...alertSignals,
    ...cronSignals,
    ...sessionSignals,
  ].slice(0, 5);

  const feedSignals = eventSignals.length > 0
    ? eventSignals
    : snapshot.recentSignals.filter((signal) => signal.tone !== 'good').slice(0, 5);

  return (
    <div className="flex flex-col h-full">
      <AgentStatusBar />

      <div className="flex-1 overflow-y-auto p-4 sm:p-6">
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-4 sm:gap-6 auto-rows-min">
          <div className="flex flex-col gap-6">
            <SystemHealthPanel
              machine={machine}
              gatewayStatus={gatewayStatus}
              backendHealth={backendHealth}
            />

            <AgentsPanel activeAgents={snapshot.activeAgents} sessions={sessions} />

            <UsagePanel />

            <AttentionNeeded alerts={alerts.items} />
          </div>

          <div className="lg:col-span-2 flex flex-col gap-6">
            <ActivityFeed signals={feedSignals} />

            <SectionCard
              eyebrow="Current session"
              title={sessionItems.length > 0 ? sessionItems[0].title : 'No active session'}
              actions={
                <SectionLink to="/sessions" label="All sessions">
                  <PanelLeftClose className="h-3 w-3" />
                </SectionLink>
              }
            >
              {sessionItems.length > 0 ? (
                <div className="flex flex-col gap-3">
                  <p className="text-sm text-text-muted line-clamp-1">
                    {sessionItems[0].preview || 'No preview available.'}
                  </p>
                  <div className="flex flex-wrap items-center gap-x-4 gap-y-2 text-xs text-text-subtle">
                    <span className="flex items-center gap-1">
                      <Rocket className="h-3.5 w-3.5" />
                      {sessionItems[0].source}
                    </span>
                    <span className="flex items-center gap-1">
                      <Activity className="h-3.5 w-3.5" />
                      {sessionItems[0].messageCount} msgs
                    </span>
                    <span>{formatRelativeTime(sessionItems[0].lastActive)}</span>
                  </div>
                </div>
              ) : (
                <p className="text-sm text-text-muted italic py-1">No active sessions right now.</p>
              )}
            </SectionCard>

            <QuickActions
              gatewayActions={gatewayActions}
              runGatewayAction={runGatewayAction}
              actionLoading={actionLoading}
            />

            {cronItems.length > 0 && (
              <SectionCard eyebrow="Scheduled jobs" title="Cron status at a glance">
                <div className="flex flex-wrap items-center gap-2 mb-3">
                  <Badge variant="default">{cron.queuedJobs} queued</Badge>
                  <Badge variant={runningCron > 0 ? 'positive' : 'default'}>{runningCron} running</Badge>
                  <Badge variant={queuedCron > 0 ? 'warning' : 'default'}>{queuedCron} pending</Badge>
                </div>

                <div className="flex flex-col gap-2">
                  {cronItems.map((job) => (
                    <div key={job.id} className="flex items-center justify-between gap-3 text-sm">
                      <span className="min-w-0 font-medium text-text truncate">{job.label}</span>
                      <span className="text-xs text-text-muted ml-3 shrink-0">
                        {job.nextRunAt ? formatRelativeTime(job.nextRunAt) : job.scheduleDisplay}
                      </span>
                    </div>
                  ))}
                </div>
              </SectionCard>
            )}
          </div>
        </div>

        <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between mt-6 pt-4 border-t border-border-subtle">
          <span className="text-xs text-text-subtle">
            {loading ? 'Refreshing…' : lastUpdatedAt ? `Last synced ${lastUpdatedAt}` : 'Not yet synced'}
          </span>
          <Button
            variant="ghost"
            size="sm"
            icon={<RefreshCw className="h-3.5 w-3.5" />}
            onClick={() => void runGatewayAction(gatewayActions[0])}
            loading={actionLoading === 'refresh'}
          >
            Refresh
          </Button>
        </div>
      </div>
    </div>
  );
}
