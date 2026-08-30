import { useI18n } from '../lib/i18n';
import { useEffect, useMemo, useRef, useState } from 'react';
import { DollarSign, Layers, Zap, TrendingUp, RefreshCw, ArrowUp, ArrowDown, ArrowUpDown } from 'lucide-react';
import { Card } from '../components/ui/Card';
import { Button } from '../components/ui/Button';
import { PageHeader } from '../components/PageHeader';
import { loadSessionsUsage, type MissionControlSessionsUsageSnapshot } from '../lib/hermes-api';
import { useMissionControl } from '../lib/mission-control-store';
import { usePullToReload } from '../hooks/usePullToReload';
import { PullToReloadIndicator } from '../components/PullToReloadIndicator';

type SortKey = 'model' | 'sessionCount' | 'inputTokens' | 'outputTokens' | 'cacheReadTokens' | 'reasoningTokens' | 'totalTokens' | 'estimatedCostUsd';
type SortDir = 'asc' | 'desc';

function formatTokens(n: number): string {
  if (n >= 1_000_000_000) return `${(n / 1_000_000_000).toFixed(2)}B`;
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(n);
}

function formatCost(usd: number): string {
  if (usd >= 100) return `$${usd.toFixed(0)}`;
  if (usd >= 1) return `$${usd.toFixed(2)}`;
  if (usd >= 0.01) return `$${usd.toFixed(3)}`;
  if (usd > 0) return `$${usd.toFixed(4)}`;
  return '$0.00';
}

function formatCostCoverage(usd: number, pricedSessions: number, totalSessions: number): string {
  if (pricedSessions <= 0 || totalSessions <= 0) return 'N/A';
  if (pricedSessions < totalSessions) return 'Partial';
  return formatCost(usd);
}

function costCoverageHint(
  pricedSessions: number,
  totalSessions: number,
  t: (key: string, values?: Record<string, string | number>) => string,
): string {
  if (pricedSessions <= 0 || totalSessions <= 0) return t('usage.pricingUnavailable');
  if (pricedSessions < totalSessions) return t('usage.sessionsPriced', { priced: pricedSessions, total: totalSessions });
  return t('usage.allPriced');
}

function costTone(pricedSessions: number, totalSessions: number): string {
  if (pricedSessions <= 0 || totalSessions <= 0) return 'text-text-subtle';
  if (pricedSessions < totalSessions) return 'text-warning';
  return 'text-positive';
}

function StatCard({
  icon: Icon,
  label,
  value,
  hint,
  color,
}: {
  icon: React.ComponentType<{ size?: number; className?: string }>;
  label: string;
  value: string;
  hint?: string;
  color: string;
}) {
  return (
    <div className="rounded-lg bg-surface-sunken/35 p-4 transition-colors hover:bg-surface-sunken/50">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="text-[11px] font-medium uppercase tracking-wide text-text-muted">{label}</p>
          <p className="mt-1 text-xl font-semibold text-text tabular-nums">{value}</p>
          {hint ? <p className="mt-1 text-[11px] leading-relaxed text-text-subtle">{hint}</p> : null}
        </div>
        <Icon size={18} className={`shrink-0 ${color}`} />
      </div>
    </div>
  );
}

const SORT_LABEL_KEYS: Record<SortKey, string> = {
  model: 'usage.model',
  sessionCount: 'usage.sessionCount',
  inputTokens: 'usage.inputTokens',
  outputTokens: 'usage.outputTokens',
  cacheReadTokens: 'usage.cacheReadTokens',
  reasoningTokens: 'usage.reasoningTokens',
  totalTokens: 'usage.totalTokens',
  estimatedCostUsd: 'usage.estCost',
};

function SortIcon({ active, dir }: { active: boolean; dir: SortDir }) {
  if (!active) return <ArrowUpDown size={12} className="text-text-subtle" />;
  return dir === 'desc' ? <ArrowDown size={12} className="text-sky-400" /> : <ArrowUp size={12} className="text-sky-400" />;
}

function ThSortable({
  label,
  sortKey,
  activeKey,
  dir,
  onSort,
  className = '',
}: {
  label: string;
  sortKey: SortKey;
  activeKey: SortKey;
  dir: SortDir;
  onSort: (key: SortKey) => void;
  className?: string;
}) {
  const { t } = useI18n();
  return (
    <th className={`px-4 py-2.5 font-medium ${className}`}>
      <button
        type="button"
        onClick={() => onSort(sortKey)}
        className="inline-flex items-center gap-1 transition-colors hover:text-text focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/50"
        aria-label={dir === 'desc' ? t('usage.sortDesc', { label }) : t('usage.sortAsc', { label })}
      >
        {label}
        <SortIcon active={activeKey === sortKey} dir={dir} />
      </button>
    </th>
  );
}

export function UsageRoute() {
  const { t } = useI18n();
  const { storedToken } = useMissionControl();
  const [usage, setUsage] = useState<MissionControlSessionsUsageSnapshot | null>(null);
  const [loading, setLoading] = useState(true);
  const [sortKey, setSortKey] = useState<SortKey>('totalTokens');
  const [sortDir, setSortDir] = useState<SortDir>('desc');
  const containerRef = useRef<HTMLDivElement | null>(null);

  const fetchUsage = async () => {
    setLoading(true);
    try {
      const data = await loadSessionsUsage(storedToken ?? undefined);
      setUsage(data);
    } finally {
      setLoading(false);
    }
  };

  const { state: pullState } = usePullToReload({
    containerRef,
    onReload: fetchUsage,
  });

  useEffect(() => {
    fetchUsage();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [storedToken]);

  const totals = usage?.totals;
  const byModel = usage?.byModel ?? [];

  const handleSort = (key: SortKey) => {
    if (key === sortKey) {
      setSortDir(d => d === 'asc' ? 'desc' : 'asc');
    } else {
      setSortKey(key);
      setSortDir(key === 'model' ? 'asc' : 'desc');
    }
  };

  const sorted = useMemo(() => {
    const arr = byModel.filter(e =>
      e.totalTokens > 0 || e.estimatedCostUsd > 0 || e.sessionCount > 0
    );
    arr.sort((a, b) => {
      if (sortKey === 'model') {
        return sortDir === 'asc' ? a.model.localeCompare(b.model) : b.model.localeCompare(a.model);
      }
      const av = a[sortKey];
      const bv = b[sortKey];
      return sortDir === 'asc' ? av - bv : bv - av;
    });
    return arr;
  }, [byModel, sortKey, sortDir]);

  return (
    <div ref={containerRef} className="route-page-scroll flex h-full flex-col gap-5 overflow-y-auto sm:gap-6">
      <PullToReloadIndicator state={pullState} />
      <PageHeader
        eyebrow={t('usage.eyebrow')}
        title={t('usage.title')}
        description={t('usage.description')}
        meta={totals ? t('usage.sessions', { count: totals.sessionCount }) : undefined}
        actions={(
          <Button
            variant="ghost"
            size="sm"
            iconOnly
            icon={<RefreshCw size={14} className={loading ? 'animate-spin' : ''} />}
            onClick={fetchUsage}
            disabled={loading}
            aria-label={t('common.refresh')}
            title={t('common.refresh')}
          />
        )}
      />

      {/* Summary metrics */}
      <Card padding="none">
        {totals ? (
          <div className="grid grid-cols-2 gap-2.5 p-3 sm:gap-3 sm:p-4 lg:grid-cols-4">
            <StatCard
              icon={Layers}
              label={t('usage.processedTokens')}
              value={formatTokens(totals.totalTokens)}
              hint={`${formatTokens(totals.inputTokens)} ${t('usage.new')} · ${formatTokens(totals.cacheReadTokens)} ${t('usage.cached')} · ${formatTokens(totals.outputTokens)} ${t('usage.out')}`}
              color="text-sky-400"
            />
            <StatCard
              icon={DollarSign}
              label={t('usage.estimatedCost')}
              value={formatCostCoverage(totals.estimatedCostUsd, totals.pricedSessionCount, totals.sessionCount)}
              hint={costCoverageHint(totals.pricedSessionCount, totals.sessionCount, t)}
              color="text-emerald-400"
            />
            <StatCard
              icon={Zap}
              label={t('usage.cacheRead')}
              value={formatTokens(totals.cacheReadTokens)}
              hint={t('usage.tokensCached')}
              color="text-violet-400"
            />
            <StatCard
              icon={TrendingUp}
              label={t('usage.reasoning')}
              value={formatTokens(totals.reasoningTokens)}
              hint={t('usage.extendedThinking')}
              color="text-amber-400"
            />
          </div>
        ) : loading ? (
          <div className="p-8 text-center text-sm text-text-muted">{t('usage.loading')}</div>
        ) : (
          <div className="p-8 text-center text-sm text-text-muted">{t('usage.noData')}</div>
        )}
      </Card>

      {/* Per-model breakdown */}
      {sorted.length > 0 ? (
        <Card padding="none">
          <div className="flex items-center justify-between gap-3 px-4 pb-3 pt-4">
            <div className="min-w-0">
              <span className="eyebrow">{t('usage.breakdown')}</span>
              <h3 className="text-sm font-semibold text-text">{t('usage.perModelUsage')}</h3>
            </div>
            {/* Mobile sort control */}
            <div className="flex shrink-0 items-center gap-1.5 md:hidden">
              <span className="text-[11px] text-text-muted">{t('usage.sort')}</span>
              <select
                className="rounded border border-border-subtle bg-surface px-2 py-1 text-xs text-text"
                value={`${sortKey}:${sortDir}`}
                onChange={(e) => {
                  const [k, d] = e.target.value.split(':') as [SortKey, SortDir];
                  setSortKey(k);
                  setSortDir(d);
                }}
              >
                {(Object.keys(SORT_LABEL_KEYS) as SortKey[]).flatMap(k => {
                  const dirs: SortDir[] = k === 'model' ? ['asc', 'desc'] : ['desc', 'asc'];
                  return dirs.map(d => (
                    <option key={`${k}:${d}`} value={`${k}:${d}`}>
                      {t(SORT_LABEL_KEYS[k])} {d === 'asc' ? '↑' : '↓'}
                    </option>
                  ));
                })}
              </select>
            </div>
          </div>

          {/* Mobile: stacked cards */}
          <div className="space-y-1.5 p-3 md:hidden">
            {sorted.map((entry) => {
              const pct = totals && totals.totalTokens > 0
                ? ((entry.totalTokens / totals.totalTokens) * 100).toFixed(1)
                : '0';
              return (
                <div key={entry.model} className="rounded-lg bg-surface-sunken/25 px-3 py-3 transition-colors hover:bg-surface-sunken/45">
                  <div className="flex items-center justify-between gap-2">
                    <span className="min-w-0 truncate font-mono text-xs text-text">{entry.model}</span>
                    <span className="shrink-0 text-[10px] tabular-nums text-sky-300">{pct}%</span>
                  </div>
                  <div className="mt-2 h-1 w-full overflow-hidden rounded-full bg-surface-sunken">
                    <div className="h-full rounded-full bg-sky-400/70" style={{ width: `${Math.min(100, Number(pct))}%` }} />
                  </div>
                  <div className="mt-2 flex items-baseline justify-between gap-2">
                    <span className="text-sm font-semibold text-text tabular-nums">{formatTokens(entry.totalTokens)}</span>
                    <span className="text-xs text-text-muted">{t('usage.sessions', { count: entry.sessionCount })}</span>
                  </div>
                  <span className={`mt-1 block text-xs font-medium ${costTone(entry.pricedSessionCount, entry.sessionCount)}`}>
                    {formatCostCoverage(entry.estimatedCostUsd, entry.pricedSessionCount, entry.sessionCount)}
                  </span>
                  <div className="mt-1.5 flex flex-wrap gap-x-3 gap-y-1 text-[11px] tabular-nums text-text-subtle">
                    <span>{formatTokens(entry.inputTokens)} {t('usage.in')}</span>
                    <span>{formatTokens(entry.outputTokens)} {t('usage.out')}</span>
                    {entry.cacheReadTokens > 0 ? <span>{formatTokens(entry.cacheReadTokens)} {t('usage.cached')}</span> : null}
                    {entry.reasoningTokens > 0 ? <span>{formatTokens(entry.reasoningTokens)} {t('usage.reason')}</span> : null}
                  </div>
                </div>
              );
            })}
            {totals ? (
              <div className="mt-1.5 flex flex-wrap items-baseline justify-between gap-x-4 gap-y-2 rounded-lg bg-surface-sunken/45 px-3 py-3">
                <span className="text-xs font-medium text-text">{t('usage.total')}</span>
                <span className="text-xs text-text-muted">{t('usage.sessions', { count: totals.sessionCount })}</span>
                <span className="text-sm font-semibold text-text tabular-nums">{formatTokens(totals.totalTokens)}</span>
                <span className={`text-xs font-medium ${costTone(totals.pricedSessionCount, totals.sessionCount)}`}>
                  {formatCostCoverage(totals.estimatedCostUsd, totals.pricedSessionCount, totals.sessionCount)}
                </span>
              </div>
            ) : null}
          </div>

          {/* Desktop: table */}
          <div className="hidden overflow-x-auto px-3 pb-3 md:block">
            <table className="w-full border-separate border-spacing-0 text-xs">
              <thead className="border-b border-border-subtle/60">
                <tr className="text-text-muted">
                  <ThSortable label={t('usage.model')} sortKey="model" activeKey={sortKey} dir={sortDir} onSort={handleSort} className="text-left" />
                  <ThSortable label={t('usage.sessionCount')} sortKey="sessionCount" activeKey={sortKey} dir={sortDir} onSort={handleSort} className="text-right" />
                  <ThSortable label={t('usage.inputTokens')} sortKey="inputTokens" activeKey={sortKey} dir={sortDir} onSort={handleSort} className="text-right" />
                  <ThSortable label={t('usage.outputTokens')} sortKey="outputTokens" activeKey={sortKey} dir={sortDir} onSort={handleSort} className="text-right" />
                  <ThSortable label={t('usage.cacheReadTokens')} sortKey="cacheReadTokens" activeKey={sortKey} dir={sortDir} onSort={handleSort} className="hidden text-right lg:table-cell" />
                  <ThSortable label={t('usage.reasoningTokens')} sortKey="reasoningTokens" activeKey={sortKey} dir={sortDir} onSort={handleSort} className="hidden text-right lg:table-cell" />
                  <ThSortable label={t('usage.totalTokens')} sortKey="totalTokens" activeKey={sortKey} dir={sortDir} onSort={handleSort} className="text-right" />
                  <ThSortable label={t('usage.estCost')} sortKey="estimatedCostUsd" activeKey={sortKey} dir={sortDir} onSort={handleSort} className="text-right" />
                </tr>
              </thead>
              <tbody>
                {sorted.map((entry) => {
                  const pct = totals && totals.totalTokens > 0
                    ? ((entry.totalTokens / totals.totalTokens) * 100).toFixed(1)
                    : '0';
                  return (
                    <tr key={entry.model} className="group transition-colors hover:bg-surface-sunken/30">
                      <td className="px-4 py-3">
                        <div className="min-w-[12rem]">
                          <div className="flex items-center gap-2">
                            <span className="min-w-0 truncate font-mono text-text">{entry.model}</span>
                            <span className="shrink-0 text-[10px] tabular-nums text-sky-300">{pct}%</span>
                          </div>
                          <div className="mt-1 h-1 max-w-40 overflow-hidden rounded-full bg-surface-sunken">
                            <div className="h-full rounded-full bg-sky-400/70" style={{ width: `${Math.min(100, Number(pct))}%` }} />
                          </div>
                        </div>
                      </td>
                      <td className="px-4 py-3 text-right tabular-nums text-text-muted">{entry.sessionCount}</td>
                      <td className="px-4 py-3 text-right tabular-nums text-text-muted">{formatTokens(entry.inputTokens)}</td>
                      <td className="px-4 py-3 text-right tabular-nums text-text-muted">{formatTokens(entry.outputTokens)}</td>
                      <td className="hidden px-4 py-3 text-right tabular-nums text-text-muted lg:table-cell">{formatTokens(entry.cacheReadTokens)}</td>
                      <td className="hidden px-4 py-3 text-right tabular-nums text-text-muted lg:table-cell">{formatTokens(entry.reasoningTokens)}</td>
                      <td className="px-4 py-3 text-right font-medium tabular-nums text-text">{formatTokens(entry.totalTokens)}</td>
                      <td className={`px-4 py-3 text-right font-medium tabular-nums ${costTone(entry.pricedSessionCount, entry.sessionCount)}`}>
                        {formatCostCoverage(entry.estimatedCostUsd, entry.pricedSessionCount, entry.sessionCount)}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
              {totals ? (
                <tfoot>
                  <tr>
                    <td colSpan={8} className="pt-3">
                      <div className="flex flex-wrap items-baseline justify-between gap-x-5 gap-y-2 rounded-lg bg-surface-sunken/45 px-4 py-3">
                        <span className="text-xs font-medium text-text">{t('usage.total')}</span>
                        <span className="text-xs text-text-muted">{t('usage.sessions', { count: totals.sessionCount })}</span>
                        <span className="text-xs tabular-nums text-text-muted">{t('usage.inputTokens')}: {formatTokens(totals.inputTokens)}</span>
                        <span className="text-xs tabular-nums text-text-muted">{t('usage.outputTokens')}: {formatTokens(totals.outputTokens)}</span>
                        <span className="text-sm font-semibold tabular-nums text-text">{formatTokens(totals.totalTokens)} {t('usage.totalTokens').toLowerCase()}</span>
                        <span className={`text-sm font-semibold tabular-nums ${costTone(totals.pricedSessionCount, totals.sessionCount)}`}>
                          {formatCostCoverage(totals.estimatedCostUsd, totals.pricedSessionCount, totals.sessionCount)}
                        </span>
                      </div>
                    </td>
                  </tr>
                </tfoot>
              ) : null}
            </table>
          </div>
        </Card>
      ) : null}
    </div>
  );
}
