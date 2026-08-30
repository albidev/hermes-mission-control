import { useI18n } from '../lib/i18n';
import { useRef } from 'react';
import { Blocks, CheckCircle2, Hammer, KeyRound } from 'lucide-react';
import { Card } from '../components/ui/Card';
import { Badge } from '../components/ui/Badge';
import { PageHeader } from '../components/PageHeader';
import { useMissionControl } from '../lib/mission-control-store';
import { usePullToReload } from '../hooks/usePullToReload';
import { PullToReloadIndicator } from '../components/PullToReloadIndicator';

function MetricCard({
  icon: Icon,
  label,
  value,
  hint,
  color,
}: {
  icon: React.ElementType;
  label: string;
  value: string;
  hint: string;
  color: string;
}) {
  return (
    <div className="min-w-0 rounded-lg bg-surface-sunken/35 p-4 transition-colors hover:bg-surface-sunken/50">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <span className="text-[11px] font-medium uppercase tracking-wide text-text-muted">{label}</span>
          <p className="mt-1 truncate text-xl font-semibold text-text tabular-nums">{value}</p>
          <p className="mt-1 text-[11px] leading-relaxed text-text-subtle">{hint}</p>
        </div>
        <Icon className={`h-[18px] w-[18px] shrink-0 ${color}`} />
      </div>
    </div>
  );
}

export function ToolsRoute() {
  const { t } = useI18n();
  const { tools } = useMissionControl();
  const containerRef = useRef<HTMLDivElement | null>(null);

  const { state: pullState } = usePullToReload({
    containerRef,
    onReload: async () => {
      // Tools are refreshed by the store polling loop.
    },
  });

  const toolsets = tools.availableToolsets;
  const readyCount = toolsets.filter((toolset) => toolset.available).length;
  const blockedCount = toolsets.filter((toolset) => !toolset.available).length;

  return (
    <div ref={containerRef} className="route-page-scroll flex h-full flex-col gap-5 overflow-y-auto sm:gap-6">
      <PullToReloadIndicator state={pullState} />

      <Card padding="none" className="!border-0">
        <PageHeader
          eyebrow={t('tools.eyebrow')}
          title={t('tools.title')}
          description={t('tools.description')}
          meta={(
            <div className="flex items-center gap-2">
              <span className="truncate">{t('tools.toolCount', { count: tools.toolCatalog.length })}</span>
              <Badge variant={tools.available ? 'positive' : 'warning'}>
                {tools.available ? t('tools.live') : t('tools.fallback')}
              </Badge>
            </div>
          )}
        />

        <div className="grid grid-cols-2 gap-2.5 p-3 sm:gap-3 sm:p-4 xl:grid-cols-4">
          <MetricCard
            icon={Blocks}
            label={t('tools.toolsets')}
            value={String(toolsets.length)}
            hint={t('tools.cataloguedGroups')}
            color="text-sky-400"
          />
          <MetricCard
            icon={CheckCircle2}
            label={t('tools.ready')}
            value={String(readyCount)}
            hint={t('tools.availableNow')}
            color="text-emerald-400"
          />
          <MetricCard
            icon={KeyRound}
            label={t('tools.needsKeys')}
            value={String(blockedCount)}
            hint={t('tools.waitingOnEnv')}
            color="text-amber-400"
          />
          <MetricCard
            icon={Hammer}
            label={t('tools.tools')}
            value={String(tools.toolCatalog.length)}
            hint={t('tools.registeredHandlers')}
            color="text-violet-400"
          />
        </div>
      </Card>

      <Card padding="none" className="!border-0">
        <div className="flex items-center justify-between gap-3 border-b border-border-subtle/60 px-4 pb-3 pt-4">
          <div className="min-w-0">
            <span className="eyebrow">{t('tools.toolsets')}</span>
            <h3 className="text-sm font-semibold text-text">{t('tools.groupedByAvailability')}</h3>
          </div>
          <span className="shrink-0 text-xs text-text-subtle">{t('tools.toolCount', { count: toolsets.length })}</span>
        </div>

        <div className="space-y-1.5 p-3">
          {toolsets.length > 0 ? toolsets.map((toolset) => (
            <article key={toolset.name} className="rounded-lg bg-surface-sunken/25 p-3 transition-colors hover:bg-surface-sunken/50">
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <p className="truncate text-sm font-medium text-text">{toolset.name}</p>
                  <p className="mt-1 line-clamp-2 text-xs leading-relaxed text-text-muted">
                    {toolset.description || t('tools.noDescription')}
                  </p>
                </div>
                <Badge variant={toolset.available ? 'positive' : 'warning'}>
                  {toolset.available ? t('tools.available') : t('tools.needsKey')}
                </Badge>
              </div>

              <div className="mt-2 flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px] text-text-subtle">
                <span>{t('tools.toolCount', { count: toolset.toolCount })}</span>
                <span>·</span>
                <span>{toolset.isComposite ? t('tools.composite') : t('tools.direct')}</span>
              </div>

              {toolset.resolvedTools.length > 0 ? (
                <div className="mt-2 flex flex-wrap gap-2">
                  {toolset.resolvedTools.slice(0, 8).map((tool) => (
                    <Badge key={tool} variant="default">{tool}</Badge>
                  ))}
                </div>
              ) : null}
            </article>
          )) : (
            <p className="p-4 text-sm text-text-muted">{t('tools.notFound')}</p>
          )}
        </div>
      </Card>
    </div>
  );
}
