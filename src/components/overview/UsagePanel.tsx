import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { ArrowRight, DollarSign, Layers, Zap } from 'lucide-react';
import { Card } from '../ui/Card';
import { loadSessionsUsage, type MissionControlSessionsUsageSnapshot } from '../../lib/hermes-api';
import { useMissionControl } from '../../lib/mission-control-store';

function formatTokens(n: number): string {
  if (n >= 1_000_000_000) return `${(n / 1_000_000_000).toFixed(1)}B`;
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

  const { totals } = usage;

  return (
    <Card padding="none">
      <div className="px-4 pt-4 pb-3 border-b border-border-subtle flex items-center justify-between">
        <div className="flex flex-col gap-0.5">
          <span className="eyebrow">Usage</span>
          <h2 className="text-sm font-semibold text-text">Token consumption &amp; cost estimate</h2>
        </div>
        <Link to="/usage" className="pill pill-subtle pill-button text-xs flex items-center gap-1">
          Details <ArrowRight size={12} />
        </Link>
      </div>

      <div className="p-4 flex flex-wrap items-center gap-6">
        <div className="flex items-center gap-2">
          <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-sky-500/10 text-sky-400">
            <Layers size={16} />
          </div>
          <div>
            <p className="text-[11px] text-text-muted uppercase tracking-wide">Tokens</p>
            <p className="text-base font-semibold text-text tabular-nums">{formatTokens(totals.totalTokens)}</p>
          </div>
        </div>

        <div className="h-8 w-px bg-border-subtle" />

        <div className="flex items-center gap-2">
          <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-emerald-500/10 text-emerald-400">
            <DollarSign size={16} />
          </div>
          <div>
            <p className="text-[11px] text-text-muted uppercase tracking-wide">Cost</p>
            <p className="text-base font-semibold text-text tabular-nums">{formatCost(totals.estimatedCostUsd)}</p>
          </div>
        </div>

        <div className="h-8 w-px bg-border-subtle" />

        <div className="flex items-center gap-2">
          <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-violet-500/10 text-violet-400">
            <Zap size={16} />
          </div>
          <div>
            <p className="text-[11px] text-text-muted uppercase tracking-wide">Cache</p>
            <p className="text-base font-semibold text-text tabular-nums">{formatTokens(totals.cacheReadTokens)}</p>
          </div>
        </div>

        <div className="h-8 w-px bg-border-subtle" />

        <div>
          <p className="text-[11px] text-text-muted uppercase tracking-wide">In / Out</p>
          <p className="text-base font-semibold text-text tabular-nums">
            {formatTokens(totals.inputTokens)} <span className="text-text-subtle font-normal">in</span>
            {' · '}
            {formatTokens(totals.outputTokens)} <span className="text-text-subtle font-normal">out</span>
          </p>
        </div>
      </div>
    </Card>
  );
}
