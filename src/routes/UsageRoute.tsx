import { useEffect, useState } from 'react';
import { DollarSign, Layers, Zap, TrendingUp, RefreshCw } from 'lucide-react';
import { Card } from '../components/ui/Card';
import { Badge } from '../components/ui/Badge';
import { Button } from '../components/ui/Button';
import { loadSessionsUsage, type MissionControlSessionsUsageSnapshot } from '../lib/hermes-api';
import { useMissionControl } from '../lib/mission-control-store';

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

export function UsageRoute() {
  const { storedToken } = useMissionControl();
  const [usage, setUsage] = useState<MissionControlSessionsUsageSnapshot | null>(null);
  const [loading, setLoading] = useState(true);

  const fetchUsage = async () => {
    setLoading(true);
    try {
      const data = await loadSessionsUsage(storedToken ?? undefined);
      setUsage(data);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchUsage();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [storedToken]);

  const totals = usage?.totals;
  const byModel = usage?.byModel ?? [];

  return (
    <div className="flex flex-col gap-6">
      {/* Header */}
      <Card padding="none">
        <div className="px-4 pt-4 pb-3 border-b border-border-subtle flex items-center justify-between">
          <div className="flex flex-col gap-0.5">
            <span className="eyebrow">Usage</span>
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
              label="Total tokens"
              value={formatTokens(totals.totalTokens)}
              hint={`${formatTokens(totals.inputTokens)} in · ${formatTokens(totals.outputTokens)} out`}
              color="bg-sky-500/10 text-sky-400"
            />
            <StatCard
              icon={DollarSign}
              label="Estimated cost"
              value={formatCost(totals.estimatedCostUsd)}
              hint="across all sessions"
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
          <div className="p-8 text-center text-sm text-text-muted">Loading usage data…</div>
        ) : (
          <div className="p-8 text-center text-sm text-text-muted">No usage data available.</div>
        )}
      </Card>

      {/* Per-model breakdown */}
      {byModel.length > 0 ? (
        <Card padding="none">
          <div className="px-4 pt-4 pb-3 border-b border-border-subtle">
            <span className="eyebrow">Breakdown</span>
            <h3 className="text-sm font-semibold text-text">Per-model usage</h3>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="bg-surface/30 text-text-muted">
                  <th className="text-left px-4 py-2.5 font-medium">Model</th>
                  <th className="text-right px-4 py-2.5 font-medium">Sessions</th>
                  <th className="text-right px-4 py-2.5 font-medium">Input tokens</th>
                  <th className="text-right px-4 py-2.5 font-medium hidden md:table-cell">Output tokens</th>
                  <th className="text-right px-4 py-2.5 font-medium hidden lg:table-cell">Cache read</th>
                  <th className="text-right px-4 py-2.5 font-medium hidden lg:table-cell">Reasoning</th>
                  <th className="text-right px-4 py-2.5 font-medium">Total tokens</th>
                  <th className="text-right px-4 py-2.5 font-medium">Est. cost</th>
                </tr>
              </thead>
              <tbody>
                {byModel.map((entry) => {
                  const pct = totals && totals.totalTokens > 0
                    ? ((entry.totalTokens / totals.totalTokens) * 100).toFixed(1)
                    : '0';
                  return (
                    <tr key={entry.model} className="border-t border-border-subtle/50 hover:bg-surface/20">
                      <td className="px-4 py-2.5">
                        <div className="flex items-center gap-2">
                          <span className="font-mono text-text break-all">{entry.model}</span>
                          <Badge variant="default" className="text-[10px] shrink-0 hidden sm:inline-flex">{pct}%</Badge>
                        </div>
                      </td>
                      <td className="px-4 py-2.5 text-right tabular-nums text-text-muted">{entry.sessionCount}</td>
                      <td className="px-4 py-2.5 text-right tabular-nums text-text-muted">{formatTokens(entry.inputTokens)}</td>
                      <td className="px-4 py-2.5 text-right tabular-nums text-text-muted hidden md:table-cell">{formatTokens(entry.outputTokens)}</td>
                      <td className="px-4 py-2.5 text-right tabular-nums text-text-muted hidden lg:table-cell">{formatTokens(entry.cacheReadTokens)}</td>
                      <td className="px-4 py-2.5 text-right tabular-nums text-text-muted hidden lg:table-cell">{formatTokens(entry.reasoningTokens)}</td>
                      <td className="px-4 py-2.5 text-right tabular-nums font-medium text-text">{formatTokens(entry.totalTokens)}</td>
                      <td className="px-4 py-2.5 text-right tabular-nums font-medium text-text">{formatCost(entry.estimatedCostUsd)}</td>
                    </tr>
                  );
                })}
              </tbody>
              {totals ? (
                <tfoot>
                  <tr className="border-t-2 border-border-subtle bg-surface/30 font-medium">
                    <td className="px-4 py-2.5 text-text">Total</td>
                    <td className="px-4 py-2.5 text-right tabular-nums text-text">{totals.sessionCount}</td>
                    <td className="px-4 py-2.5 text-right tabular-nums text-text">{formatTokens(totals.inputTokens)}</td>
                    <td className="px-4 py-2.5 text-right tabular-nums text-text hidden md:table-cell">{formatTokens(totals.outputTokens)}</td>
                    <td className="px-4 py-2.5 text-right tabular-nums text-text hidden lg:table-cell">{formatTokens(totals.cacheReadTokens)}</td>
                    <td className="px-4 py-2.5 text-right tabular-nums text-text hidden lg:table-cell">{formatTokens(totals.reasoningTokens)}</td>
                    <td className="px-4 py-2.5 text-right tabular-nums text-text">{formatTokens(totals.totalTokens)}</td>
                    <td className="px-4 py-2.5 text-right tabular-nums text-text">{formatCost(totals.estimatedCostUsd)}</td>
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
