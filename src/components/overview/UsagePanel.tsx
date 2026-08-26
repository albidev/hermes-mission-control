import { useI18n } from '../../lib/i18n';
import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { ArrowRight } from 'lucide-react';
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

function formatCostCoverage(usd: number, pricedSessions: number, totalSessions: number): string {
  if (pricedSessions <= 0 || totalSessions <= 0) return 'N/A';
  if (pricedSessions < totalSessions) return 'Partial';
  return formatCost(usd);
}

function MiniStat({ label, value, sub, color }: { label: string; value: string; sub?: string; color: string }) {
  return (
    <div className="flex flex-col gap-0.5">
      <span className="text-[10px] text-text-muted uppercase tracking-wide">{label}</span>
      <span className={`text-sm font-semibold tabular-nums ${color}`}>{value}</span>
      {sub ? <span className="text-[10px] text-text-subtle">{sub}</span> : null}
    </div>
  );
}

export function UsagePanel() {
  const { t } = useI18n();
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
      <div className="px-3 pt-3 pb-2 border-b border-border-subtle flex items-center justify-between">
        <div className="flex flex-col gap-0.5">
          <span className="eyebrow">{t('nav.usage')}</span>
          <h2 className="text-sm font-semibold text-text">Token consumption &amp; cost estimate</h2>
        </div>
        <Link to="/usage" className="pill pill-subtle pill-button text-xs flex items-center gap-1">
          Details <ArrowRight size={12} />
        </Link>
      </div>

      <div className="p-3 grid grid-cols-2 sm:grid-cols-4 gap-3">
        <MiniStat
          label="Processed tokens"
          value={formatTokens(totals.totalTokens)}
          sub={`${formatTokens(totals.inputTokens)} new · ${formatTokens(totals.cacheReadTokens)} cached · ${formatTokens(totals.outputTokens)} out`}
          color="text-sky-400"
        />
        <MiniStat
          label="Estimated cost"
          value={formatCostCoverage(totals.estimatedCostUsd, totals.pricedSessionCount, totals.sessionCount)}
          sub={`${totals.pricedSessionCount}/${totals.sessionCount} sessions priced`}
          color="text-emerald-400"
        />
        <MiniStat
          label="Cache read"
          value={formatTokens(totals.cacheReadTokens)}
          sub="tokens cached"
          color="text-violet-400"
        />
        <MiniStat
          label="Reasoning"
          value={formatTokens(totals.reasoningTokens)}
          sub="extended thinking"
          color="text-amber-400"
        />
      </div>
    </Card>
  );
}
