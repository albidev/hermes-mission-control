import { useI18n } from '../lib/i18n';
import { useRef, useState } from 'react';
import { Blocks, CheckCircle2, Hammer, KeyRound } from 'lucide-react';
import { Card } from '../components/ui/Card';
import { Badge } from '../components/ui/Badge';
import { Button } from '../components/ui/Button';
import { PageHeader } from '../components/PageHeader';
import { useMissionControl } from '../lib/mission-control-store';
import { toolsetMatches, visibleToolNames } from '../lib/tools-browser';
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
  const { tools, refreshTools } = useMissionControl();
  const containerRef = useRef<HTMLDivElement | null>(null);
  const [refreshError, setRefreshError] = useState<string | null>(null);
  const [query, setQuery] = useState('');

  // The hook keeps the spinner until this settles and ignores gestures while
  // it is pending, so one pull means exactly one Tools request.
  const { state: pullState } = usePullToReload({
    containerRef,
    onReload: async () => {
      const result = await refreshTools();
      setRefreshError(result.ok ? null : result.error);
    },
  });

  const toolsets = tools.availableToolsets;
  const filteredToolsets = toolsets.filter((toolset) => toolsetMatches(toolset, query));
  const readyCount = toolsets.filter((toolset) => toolset.available && toolset.configured).length;
  const needsKeysCount = toolsets.filter((toolset) => !toolset.configured).length;

  return (
    <div ref={containerRef} className="route-page-scroll flex h-full flex-col gap-5 overflow-y-auto sm:gap-6">
      <PullToReloadIndicator state={pullState} />
      {refreshError ? (
        <p role="status" className="text-xs text-warning">{t('tools.refreshFailed', { detail: refreshError })}</p>
      ) : null}

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
            value={String(needsKeysCount)}
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

      <div className="flex min-w-0 items-center gap-2">
        <input
          type="search"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          aria-label={t('tools.search')}
          placeholder={t('tools.search')}
          className="mc-input min-w-0 w-full"
        />
        <Button type="button" size="sm" variant="ghost" onClick={() => setQuery('')}>
          {t('tools.resetSearch')}
        </Button>
      </div>

      <Card padding="none" className="!border-0">
        <div className="flex items-center justify-between gap-3 border-b border-border-subtle/60 px-4 pb-3 pt-4">
          <div className="min-w-0">
            <span className="eyebrow">{t('tools.toolsets')}</span>
            <h3 className="text-sm font-semibold text-text">{t('tools.groupedByAvailability')}</h3>
          </div>
          <span className="shrink-0 text-xs text-text-subtle">{t('tools.toolCount', { count: toolsets.length })}</span>
        </div>

        <div className="space-y-1.5 p-3">
          {filteredToolsets.length > 0 ? filteredToolsets.map((toolset) => {
            const shownTools = visibleToolNames(toolset, query);
            const hiddenCount = toolset.resolvedTools.length - shownTools.length;
            const status = !toolset.configured
              ? { variant: 'warning' as const, label: t('tools.needsKey') }
              : toolset.available
                ? { variant: 'positive' as const, label: t('tools.available') }
                : { variant: 'default' as const, label: t('tools.disabled') };
            return (
            <article key={toolset.name} data-toolset={toolset.name} className="rounded-lg bg-surface-sunken/25 p-3 transition-colors hover:bg-surface-sunken/50">
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <p className="truncate text-sm font-medium text-text">{toolset.name}</p>
                  <p className="mt-1 line-clamp-2 text-xs leading-relaxed text-text-muted">
                    {toolset.description || t('tools.noDescription')}
                  </p>
                </div>
                <Badge variant={status.variant}>{status.label}</Badge>
              </div>

              <div className="mt-2 flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px] text-text-subtle">
                <span>{t('tools.toolCount', { count: toolset.toolCount })}</span>
                <span>·</span>
                <span>{toolset.isComposite ? t('tools.composite') : t('tools.direct')}</span>
              </div>

              {shownTools.length > 0 ? (
                <div className="mt-2 flex flex-wrap gap-2">
                  {shownTools.map((tool) => (
                    <Badge key={tool} variant="default">{tool}</Badge>
                  ))}
                  {hiddenCount > 0 ? (
                    <span className="self-center text-[11px] text-text-subtle">{t('tools.moreTools', { count: hiddenCount })}</span>
                  ) : null}
                </div>
              ) : null}
            </article>
            );
          }) : (
            <p className="p-4 text-sm text-text-muted">{t(toolsets.length === 0 ? 'tools.notFound' : 'tools.noMatch')}</p>
          )}
        </div>
      </Card>
    </div>
  );
}
