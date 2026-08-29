import { useI18n } from '../lib/i18n';
import { useEffect, useMemo, useRef, useState } from 'react';
import { DollarSign, Layers, Zap, TrendingUp, RefreshCw, ArrowUp, ArrowDown, ArrowUpDown } from 'lucide-react';
import { Card } from '../components/ui/Card';
import { Badge } from '../components/ui/Badge';
import { Button } from '../components/ui/Button';
import { loadSessionsUsage, type MissionControlSessionsUsageSnapshot } from '../lib/hermes-api';
import { useMissionControl } from '../lib/mission-control-store';
import { usePullToReload } from '../hooks/usePullToReload';
import { PullToReloadIndicator } from '../components/PullToReloadIndicator';

type ModelEntry = MissionControlSessionsUsageSnapshot['byModel'][number];
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

function costCoverageHint(pricedSessions: number, totalSessions: number): string {
  if (pricedSessions <= 0 || totalSessions <= 0) return 'pricing unavailable';
  if (pricedSessions < totalSessions) return `${pricedSessions}/${totalSessions} sessions priced`;
  return 'all sessions priced';
}

function StatCard({
  icon: Icon,
  label,
  value,
  hint,
  color,
}: {
  icon: React.ComponentType<{ size?: number }>;
  label: string;
  value: string;
  hint?: string;
  color: string;
}) {
  return (
    <div className="flex items-center gap-3 rounded-lg border border-border-subtle bg-surface/50 p-4">
      <div className={`flex h-10 w-10 shrink-0 items-center justify-center rounded-lg ${color}`}>
        <Icon size={20} />
      </div>
      <div className="min-w-0">
        <p className="text-[11px] text-text-muted uppercase tracking-wide">{label}</p>
        <p className="text-xl font-semibold text-text tabular-nums">{value}</p>
        {hint ? <p className="text-[11px] text-text-subtle">{hint}</p> : null}
      </div>
    </div>
  );
}

const SORT_LABELS: Record<SortKey, string> = {
  model: 'Model',
  sessionCount: 'Sessions',
  inputTokens: 'Input tokens',
  outputTokens: 'Output tokens',
  cacheReadTokens: 'Cache read',
  reasoningTokens: 'Reasoning',
  totalTokens: 'Total tokens',
  estimatedCostUsd: 'Est. cost',
};

function SortIcon({ active, dir }: { active: boolean; dir: SortDir }) {
  const { t } = useI18n();
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
  return (
    <th
      className={`px-4 py-2.5 font-medium cursor-pointer select-none hover:text-text transition-colors ${className}`}
      onClick={() => onSort(sortKey)}
    >
      <span className="inline-flex items-center gap-1">
        {label}
        <SortIcon active={activeKey === sortKey} dir={dir} />
      </span>
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
    <div ref={containerRef} className="route-page-scroll flex flex-col gap-6 h-full overflow-y-auto">
      <PullToReloadIndicator state={pullState} />
      {/* Header */}
      <Card padding="none">
        <div className="px-4 pt-4 pb-3 border-b border-border-subtle flex items-center justify-between">
          <div className="flex flex-col gap-0.5">
            <span className="eyebrow">{t('nav.usage')}</span>
            <h2 className="text-sm font-semibold text-text">Token consumption &amp; cost estimate</h2>
          </div>
          <div className="flex items-center gap-2">
            {totals ? (
              <span className="text-xs text-text-muted">{totals.sessionCount} sessions</span>
            ) : null}
            <Button variant="ghost" size="sm" onClick={fetchUsage} disabled={loading}>
              <RefreshCw size={14} className={loading ? 'animate-spin' : ''} />
            </Button>
          </div>
        </div>

        {/* Summary cards */}
        {totals ? (
          <div className="p-4 grid grid-cols-2 lg:grid-cols-4 gap-3">
            <StatCard
              icon={Layers}
              label="Processed tokens"
              value={formatTokens(totals.totalTokens)}
              hint={`${formatTokens(totals.inputTokens)} new · ${formatTokens(totals.cacheReadTokens)} cached · ${formatTokens(totals.outputTokens)} out`}
              color="bg-sky-500/10 text-sky-400"
            />
            <StatCard
              icon={DollarSign}
              label="Estimated cost"
              value={formatCostCoverage(totals.estimatedCostUsd, totals.pricedSessionCount, totals.sessionCount)}
              hint={costCoverageHint(totals.pricedSessionCount, totals.sessionCount)}
              color="bg-emerald-500/10 text-emerald-400"
            />
            <StatCard
              icon={Zap}
              label="Cache read tokens"
              value={formatTokens(totals.cacheReadTokens)}
              hint="tokens served from cache"
              color="bg-violet-500/10 text-violet-400"
            />
            <StatCard
              icon={TrendingUp}
              label="Reasoning tokens"
              value={formatTokens(totals.reasoningTokens)}
              hint="extended thinking"
              color="bg-amber-500/10 text-amber-400"
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
          <div className="px-4 pt-4 pb-3 border-b border-border-subtle flex items-center justify-between">
            <div>
              <span className="eyebrow">{t('usage.breakdown')}</span>
              <h3 className="text-sm font-semibold text-text">{t('usage.perModelUsage')}</h3>
            </div>
            {/* Mobile sort control */}
            <div className="md:hidden flex items-center gap-1.5">
              <span className="text-[11px] text-text-muted">{t('usage.sort')}</span>
              <select
                className="bg-surface border border-border-subtle rounded px-2 py-1 text-xs text-text"
                value={`${sortKey}:${sortDir}`}
                onChange={(e) => {
                  const [k, d] = e.target.value.split(':') as [SortKey, SortDir];
                  setSortKey(k);
                  setSortDir(d);
                }}
              >
                {(Object.keys(SORT_LABELS) as SortKey[]).flatMap(k => {
                  const dirs: SortDir[] = k === 'model' ? ['asc', 'desc'] : ['desc', 'asc'];
                  return dirs.map(d => (
                    <option key={`${k}:${d}`} value={`${k}:${d}`}>
                      {SORT_LABELS[k]} {d === 'asc' ? '↑' : '↓'}
                    </option>
                  ));
                })}
              </select>
            </div>
          </div>

          {/* Mobile: stacked cards */}
          <div className="md:hidden divide-y divide-border-subtle/50">
            {sorted.map((entry) => {
              const pct = totals && totals.totalTokens > 0
                ? ((entry.totalTokens / totals.totalTokens) * 100).toFixed(1)
                : '0';
              return (
                <div key={entry.model} className="px-4 py-3">
                  <div className="flex items-center justify-between gap-2 mb-1.5">
                    <span className="font-mono text-xs text-text truncate">{entry.model}</span>
                    <Badge variant="default" className="text-[10px] shrink-0">{pct}%</Badge>
                  </div>
                  <div className="flex items-baseline justify-between mb-1.5">
                    <span className="text-sm font-semibold text-text tabular-nums">{formatTokens(entry.totalTokens)}</span>
                    <span className="text-xs text-text-muted">{entry.sessionCount} sessions</span>
                  </div>
                  <span className="text-xs font-medium text-emerald-400">
                    {formatCostCoverage(entry.estimatedCostUsd, entry.pricedSessionCount, entry.sessionCount)}
                  </span>
                  <div className="flex gap-3 mt-1.5 text-[11px] text-text-subtle tabular-nums">
                    <span>{formatTokens(entry.inputTokens)} in</span>
                    <span>{formatTokens(entry.outputTokens)} out</span>
                    {entry.cacheReadTokens > 0 ? <span>{formatTokens(entry.cacheReadTokens)} cache</span> : null}
                    {entry.reasoningTokens > 0 ? <span>{formatTokens(entry.reasoningTokens)} reason</span> : null}
                  </div>
                </div>
              );
            })}
            {totals ? (
              <div className="px-4 py-3 bg-surface/30">
                <div className="flex items-baseline justify-between mb-1">
                  <span className="text-xs font-medium text-text">{t('usage.total')}</span>
                  <span className="text-xs text-text-muted">{totals.sessionCount} sessions</span>
                </div>
                <div className="flex items-baseline justify-between">
                  <span className="text-sm font-semibold text-text tabular-nums">{formatTokens(totals.totalTokens)}</span>
                  <span className="text-xs font-medium text-emerald-400">
                    {formatCostCoverage(totals.estimatedCostUsd, totals.pricedSessionCount, totals.sessionCount)}
                  </span>
                </div>
              </div>
            ) : null}
          </div>

          {/* Desktop: table */}
          <div className="hidden md:block overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="bg-surface/30 text-text-muted">
                  <ThSortable label="Model" sortKey="model" activeKey={sortKey} dir={sortDir} onSort={handleSort} className="text-left" />
                  <ThSortable label="Sessions" sortKey="sessionCount" activeKey={sortKey} dir={sortDir} onSort={handleSort} className="text-right" />
                  <ThSortable label="Input tokens" sortKey="inputTokens" activeKey={sortKey} dir={sortDir} onSort={handleSort} className="text-right" />
                  <ThSortable label="Output tokens" sortKey="outputTokens" activeKey={sortKey} dir={sortDir} onSort={handleSort} className="text-right" />
                  <ThSortable label="Cache read" sortKey="cacheReadTokens" activeKey={sortKey} dir={sortDir} onSort={handleSort} className="text-right hidden lg:table-cell" />
                  <ThSortable label="Reasoning" sortKey="reasoningTokens" activeKey={sortKey} dir={sortDir} onSort={handleSort} className="text-right hidden lg:table-cell" />
                  <ThSortable label="Processed tokens" sortKey="totalTokens" activeKey={sortKey} dir={sortDir} onSort={handleSort} className="text-right" />
                  <ThSortable label="Est. cost" sortKey="estimatedCostUsd" activeKey={sortKey} dir={sortDir} onSort={handleSort} className="text-right" />
                </tr>
              </thead>
              <tbody>
                {sorted.map((entry) => {
                  const pct = totals && totals.totalTokens > 0
                    ? ((entry.totalTokens / totals.totalTokens) * 100).toFixed(1)
                    : '0';
                  return (
                    <tr key={entry.model} className="border-t border-border-subtle/50 hover:bg-surface/20">
                      <td className="px-4 py-2.5">
                        <div className="flex items-center gap-2">
                          <span className="font-mono text-text break-all">{entry.model}</span>
                          <Badge variant="default" className="text-[10px] shrink-0">{pct}%</Badge>
                        </div>
                      </td>
                      <td className="px-4 py-2.5 text-right tabular-nums text-text-muted">{entry.sessionCount}</td>
                      <td className="px-4 py-2.5 text-right tabular-nums text-text-muted">{formatTokens(entry.inputTokens)}</td>
                      <td className="px-4 py-2.5 text-right tabular-nums text-text-muted">{formatTokens(entry.outputTokens)}</td>
                      <td className="px-4 py-2.5 text-right tabular-nums text-text-muted hidden lg:table-cell">{formatTokens(entry.cacheReadTokens)}</td>
                      <td className="px-4 py-2.5 text-right tabular-nums text-text-muted hidden lg:table-cell">{formatTokens(entry.reasoningTokens)}</td>
                      <td className="px-4 py-2.5 text-right tabular-nums font-medium text-text">{formatTokens(entry.totalTokens)}</td>
                      <td className="px-4 py-2.5 text-right tabular-nums font-medium text-text">{formatCostCoverage(entry.estimatedCostUsd, entry.pricedSessionCount, entry.sessionCount)}</td>
                    </tr>
                  );
                })}
              </tbody>
              {totals ? (
                <tfoot>
                  <tr className="border-t-2 border-border-subtle bg-surface/30 font-medium">
                    <td className="px-4 py-2.5 text-text">{t('usage.total')}</td>
                    <td className="px-4 py-2.5 text-right tabular-nums text-text">{totals.sessionCount}</td>
                    <td className="px-4 py-2.5 text-right tabular-nums text-text">{formatTokens(totals.inputTokens)}</td>
                    <td className="px-4 py-2.5 text-right tabular-nums text-text">{formatTokens(totals.outputTokens)}</td>
                    <td className="px-4 py-2.5 text-right tabular-nums text-text hidden lg:table-cell">{formatTokens(totals.cacheReadTokens)}</td>
                    <td className="px-4 py-2.5 text-right tabular-nums text-text hidden lg:table-cell">{formatTokens(totals.reasoningTokens)}</td>
                    <td className="px-4 py-2.5 text-right tabular-nums text-text">{formatTokens(totals.totalTokens)}</td>
                    <td className="px-4 py-2.5 text-right tabular-nums text-text">{formatCostCoverage(totals.estimatedCostUsd, totals.pricedSessionCount, totals.sessionCount)}</td>
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
