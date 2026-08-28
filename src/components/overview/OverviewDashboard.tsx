import { useI18n } from '../../lib/i18n';
import { useRef } from 'react';
import { Link } from 'react-router-dom';
import { Activity, ArrowRight, Bot, PanelLeftClose, RefreshCw, Rocket } from 'lucide-react';
import { useMissionControl } from '../../lib/mission-control-store';
import { Card } from '../ui/Card';
import { Badge } from '../ui/Badge';
import { Button } from '../ui/Button';
import { AgentStatusBar } from './AgentStatusBar';
import { SystemHealthPanel } from './SystemHealthPanel';
import { AttentionNeeded } from './AttentionNeeded';
import { QuickActions } from './QuickActions';
import { UsagePanel } from './UsagePanel';
import { ProviderUsagePanel } from './ProviderUsagePanel';
import { DashboardGrid, type DashboardWidget } from './DashboardGrid';
import { formatRelativeTime } from '../../lib/format';
import { usePullToReload } from '../../hooks/usePullToReload';
import { PullToReloadIndicator } from '../PullToReloadIndicator';

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
      <div className="flex items-center justify-between px-3 pt-3 pb-2 border-b border-border-subtle">
        <div className="flex flex-col gap-0.5">
          <span className="eyebrow">{eyebrow}</span>
          <h2 className="text-sm font-semibold text-text">{title}</h2>
        </div>
        {actions ? <div className="flex items-center gap-2">{actions}</div> : null}
      </div>
      <div className="p-3">{children}</div>
    </Card>
  );
}

export function OverviewDashboard() {
  const { t } = useI18n();
  const {
    snapshot,
    loading,
    lastUpdatedAt,
    actionLoading,
    gatewayActions,
    runGatewayAction,
  } = useMissionControl();

  const { machine, sessions, cron, alerts, backendHealth, gatewayStatus } = snapshot;
  const liveSessionItems = sessions.items.filter((session) => session.status === 'live').slice(0, 3);
  const cronItems = cron.items.slice(0, 3);
  const runningCron = cron.items.filter((job) => job.state === 'running').length;
  const queuedCron = cron.items.filter((job) => job.state === 'queued').length;

  const widgets: DashboardWidget[] = [
    {
      id: 'health',
      label: 'System health',
      className: 'widget-health',
      content: (
        <SystemHealthPanel machine={machine} gatewayStatus={gatewayStatus} backendHealth={backendHealth} />
      ),
    },
    {
      id: 'provider-usage',
      label: 'Provider usage',
      className: 'widget-provider-usage',
      locked: true,
      content: <ProviderUsagePanel />,
    },
    {
      id: 'usage',
      label: 'Token usage',
      className: 'widget-usage',
      content: <UsagePanel />,
    },
    {
      id: 'attention',
      label: 'Attention needed',
      className: 'widget-attention',
      content: <AttentionNeeded alerts={alerts.items} />,
    },
    {
      id: 'current-session',
      label: 'Live sessions',
      className: 'widget-current-session',
      content: (
        <SectionCard
          eyebrow={t('overview.currentSession')}
          title={liveSessionItems.length > 0 ? t('overview.liveSessionsTitle', { count: liveSessionItems.length }) : t('overview.noActiveSession')}
          actions={
            <SectionLink to="/sessions" label={t('overview.allSessions')}>
              <PanelLeftClose className="h-3 w-3" />
            </SectionLink>
          }
        >
          {liveSessionItems.length > 0 ? (
            <div className="flex flex-col gap-3">
              {liveSessionItems.map((session) => (
                <div key={session.id} className="flex flex-col gap-2 rounded-lg border border-border-subtle bg-surface-sunken/30 p-2.5">
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0">
                      <div className="flex items-center gap-2">
                        <span className="h-2 w-2 shrink-0 rounded-full bg-positive shadow-[0_0_0_3px_rgba(52,211,153,0.14)] animate-pulse" />
                        <p className="truncate text-sm font-medium text-text">{session.title}</p>
                        <Badge variant="positive" dot>{t('sessions.liveStatus')}</Badge>
                      </div>
                      <p className="mt-1 line-clamp-1 text-xs text-text-muted">{session.preview || t('overview.noPreview')}</p>
                    </div>
                    <Link
                      to={`/sessions?chatSession=${encodeURIComponent(session.id)}`}
                      className="inline-flex shrink-0 items-center gap-1 rounded-md bg-accent px-2 py-1 text-[11px] font-medium text-white transition-colors hover:bg-accent/85"
                      aria-label={t('sessions.resumeAria', { title: session.title })}
                    >
                      <Bot className="h-3 w-3" />
                      {t('sessions.resumeChat')}
                    </Link>
                  </div>
                  <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-text-subtle">
                    <span className="flex items-center gap-1"><Rocket className="h-3 w-3" />{session.source}</span>
                    <span className="flex items-center gap-1"><Activity className="h-3 w-3" />{session.messageCount} msgs</span>
                    <span>{formatRelativeTime(session.lastActive)}</span>
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <p className="text-sm text-text-muted italic py-1">{t('overview.noActiveSessions')}</p>
          )}
        </SectionCard>
      ),
    },
    {
      id: 'quick-actions',
      label: 'Quick actions',
      className: 'widget-quick-actions',
      content: (
        <QuickActions
          gatewayActions={gatewayActions}
          runGatewayAction={runGatewayAction}
          actionLoading={actionLoading}
        />
      ),
    },
    ...(cronItems.length > 0
      ? [
          {
            id: 'cron',
            label: 'Scheduled jobs',
            className: 'widget-cron',
            content: (
              <SectionCard eyebrow="Scheduled jobs" title={t('overview.cronStatusAtGlance')}>
                <div className="flex flex-wrap items-center gap-2 mb-3">
                  <Badge variant="default">{cron.queuedJobs} queued</Badge>
                  <Badge variant={runningCron > 0 ? 'positive' : 'default'}>{runningCron} running</Badge>
                  <Badge variant={queuedCron > 0 ? 'warning' : 'default'}>{queuedCron} pending</Badge>
                </div>
                <div className="flex flex-col gap-2">
                  {cronItems.map((job: { id: string; label: string; nextRunAt?: string | null; scheduleDisplay: string }) => (
                    <div key={job.id} className="flex items-center justify-between gap-3 text-sm">
                      <span className="min-w-0 font-medium text-text truncate">{job.label}</span>
                      <span className="text-xs text-text-muted ml-3 shrink-0">
                        {job.nextRunAt ? formatRelativeTime(job.nextRunAt) : job.scheduleDisplay}
                      </span>
                    </div>
                  ))}
                </div>
              </SectionCard>
            ),
          } as DashboardWidget,
        ]
      : []),
  ].filter((widget): widget is DashboardWidget => Boolean(widget.content));

  const containerRef = useRef<HTMLDivElement | null>(null);
  const { refreshAll, storedToken } = useMissionControl();
  const { state: pullState } = usePullToReload({
    containerRef,
    onReload: async () => {
      await refreshAll(storedToken || undefined, { silent: false, includeReference: true, includeSnapshot: true });
    },
  });

  return (
    <div ref={containerRef} className="flex flex-col h-full overflow-y-auto">
      <PullToReloadIndicator state={pullState} />
      <AgentStatusBar />
      <div className="p-4 sm:p-6">
        <DashboardGrid widgets={widgets} />
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
