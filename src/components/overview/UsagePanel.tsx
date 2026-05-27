import { useEffect, useState } from 'react';
import { DollarSign, Layers, Zap } from 'lucide-react';
import { Card } from '../ui/Card';
import { loadSessionsUsage, type MissionControlSessionsUsageSnapshot } from '../../lib/hermes-api';
import { useMissionControl } from '../../lib/mission-control-store';

function formatTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(n);
}

function formatCost(usd: number): string {
  if (usd >= 1) return `$${usd.toFixed(2)}`;
  if (usd >= 0.01) return `$${usd.toFixed(3)}`;
  if (usd > 0) return `$${usd.toFixed(4)}`;
  return '$0.00';
}

export function UsagePanel() {
  const { storedToken } = useMissionControl();
  const [usage, setUsage] = useState<MissionControlSessionsUsageSnapshot | null>(null);

  useEffect(() => {
    let cancelled = false;
    loadSessionsUsage(storedToken ?? undefined).then((data) => {
      if (!cancelled) setUsage(data);
    });
    return () => { cancelled = true; };
  }, [storedToken]);

  if (!usage || !usage.available) return null;

  const { totals, byModel } = usage;

  return (
    <Card padding="none">
      <div className="px-4 pt-4 pb-3 border-b border-border-subtle flex items-center justify-between">
        <div className="flex flex-col gap-0.5">
          <span className="eyebrow">Usage</span>
          <h2 className="text-sm font-semibold text-text">Token consumption &amp; cost estimate</h2>
        </div>
        <span className="text-xs text-text-muted">{totals.sessionCount} sessions tracked</span>
      </div>

      <div className="p-4 grid grid-cols-1 sm:grid-cols-3 gap-3">
        <div className="flex items-center gap-3 rounded-lg border border-border-subtle bg-surface/50 p-3">
          <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-sky-500/10 text-sky-400">
            <Layers size={18} />
          </div>
          <div className="min-w-0">
            <p className="text-[11px] text-text-muted uppercase tracking-wide">Total tokens</p>
            <p className="text-lg font-semibold text-text tabular-nums">{formatTokens(totals.totalTokens)}</p>
            <p className="text-[11px] text-text-subtle">{formatTokens(totals.inputTokens)} in · {formatTokens(totals.outputTokens)} out</p>
          </div>
        </div>

        <div className="flex items-center gap-3 rounded-lg border border-border-subtle bg-surface/50 p-3">
          <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-emerald-500/10 text-emerald-400">
            <DollarSign size={18} />
          </div>
          <div className="min-w-0">
            <p className="text-[11px] text-text-muted uppercase tracking-wide">Estimated cost</p>
            <p className="text-lg font-semibold text-text tabular-nums">{formatCost(totals.estimatedCostUsd)}</p>
            <p className="text-[11px] text-text-subtle">across all sessions</p>
          </div>
        </div>

        <div className="flex items-center gap-3 rounded-lg border border-border-subtle bg-surface/50 p-3">
          <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-violet-500/10 text-violet-400">
            <Zap size={18} />
          </div>
          <div className="min-w-0">
            <p className="text-[11px] text-text-muted uppercase tracking-wide">Cache savings</p>
            <p className="text-lg font-semibold text-text tabular-nums">{formatTokens(totals.cacheReadTokens)}</p>
            <p className="text-[11px] text-text-subtle">cache read tokens</p>
          </div>
        </div>
      </div>

      {byModel.length > 0 && (
        <div className="px-4 pb-4">
          <div className="rounded-lg border border-border-subtle overflow-hidden">
            <table className="w-full text-xs">
              <thead>
                <tr className="bg-surface/30 text-text-muted">
                  <th className="text-left px-3 py-2 font-medium">Model</th>
                  <th className="text-right px-3 py-2 font-medium">Sessions</th>
                  <th className="text-right px-3 py-2 font-medium">In tokens</th>
                  <th className="text-right px-3 py-2 font-medium">Out tokens</th>
                  <th className="text-right px-3 py-2 font-medium">Cost</th>
                </tr>
              </thead>
              <tbody>
                {byModel.map((entry) => (
                  <tr key={entry.model} className="border-t border-border-subtle/50">
                    <td className="px-3 py-2 font-mono text-text break-all">{entry.model}</td>
                    <td className="px-3 py-2 text-right tabular-nums text-text-muted">{entry.sessionCount}</td>
                    <td className="px-3 py-2 text-right tabular-nums text-text-muted">{formatTokens(entry.inputTokens)}</td>
                    <td className="px-3 py-2 text-right tabular-nums text-text-muted">{formatTokens(entry.outputTokens)}</td>
                    <td className="px-3 py-2 text-right tabular-nums text-text">{formatCost(entry.estimatedCostUsd)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </Card>
  );
}
