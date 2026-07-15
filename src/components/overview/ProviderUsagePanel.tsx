import { useEffect, useState } from 'react';
import { ArrowRight, Cloud, RefreshCw } from 'lucide-react';
import { Link } from 'react-router-dom';
import { Card } from '../ui/Card';
import { loadProviderUsage, type MissionControlProviderUsage, type MissionControlProviderUsageSnapshot } from '../../lib/hermes-api';
import { useMissionControl } from '../../lib/mission-control-store';

const PROVIDER_LABELS: Record<string, string> = {
  codex: 'Codex',
  ollama: 'Ollama Cloud',
  openrouter: 'OpenRouter',
};

function formatPercent(value?: number): string {
  return typeof value === 'number' ? `${value.toFixed(value % 1 === 0 ? 0 : 1)}%` : '—';
}

function formatBalance(value?: number): string {
  return typeof value === 'number' ? `$${value.toFixed(2)}` : '—';
}

function formatReset(value?: string): string {
  if (!value) return 'reset unknown';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return 'reset unknown';
  return `reset ${date.toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })}`;
}

function providerPrimaryValue(provider: MissionControlProviderUsage): string {
  if (provider.provider === 'openrouter') return formatBalance(provider.openRouter?.balance);
  return formatPercent(provider.primary?.usedPercent);
}

function providerSecondary(provider: MissionControlProviderUsage): string {
  if (provider.provider === 'openrouter') {
    const monthly = provider.openRouter?.keyUsageMonthly;
    return typeof monthly === 'number' ? `monthly ${formatBalance(monthly)}` : 'credits & rate limit';
  }
  return provider.secondary?.usedPercent !== undefined
    ? `weekly ${formatPercent(provider.secondary.usedPercent)}`
    : formatReset(provider.primary?.resetsAt);
}

function ProviderCard({ provider }: { provider: MissionControlProviderUsage }) {
  const label = PROVIDER_LABELS[provider.provider] ?? provider.provider;
  const unavailable = !provider.available;
  return (
    <div className="rounded-lg border border-border-subtle bg-surface/40 p-3 flex flex-col gap-2 min-w-0">
      <div className="flex items-center justify-between gap-2">
        <span className="text-xs font-semibold text-text truncate">{label}</span>
        <span className={`h-1.5 w-1.5 rounded-full shrink-0 ${unavailable ? 'bg-amber-400' : 'bg-emerald-400'}`} />
      </div>
      {unavailable ? (
        <span className="text-xs text-text-muted line-clamp-2">{provider.error || 'Unavailable'}</span>
      ) : (
        <>
          <span className={`text-lg font-semibold tabular-nums ${provider.provider === 'openrouter' ? 'text-emerald-400' : 'text-sky-400'}`}>
            {providerPrimaryValue(provider)}
          </span>
          <span className="text-[10px] text-text-subtle truncate">
            {providerSecondary(provider)}
            {provider.provider !== 'openrouter' && provider.primary?.resetsAt ? ` · ${formatReset(provider.primary.resetsAt)}` : ''}
          </span>
        </>
      )}
    </div>
  );
}

export function ProviderUsagePanel() {
  const { storedToken } = useMissionControl();
  const [snapshot, setSnapshot] = useState<MissionControlProviderUsageSnapshot | null>(null);
  const [refreshing, setRefreshing] = useState(false);

  useEffect(() => {
    let cancelled = false;
    const refresh = async () => {
      setRefreshing(true);
      const next = await loadProviderUsage(storedToken || undefined);
      if (!cancelled) {
        setSnapshot(next);
        setRefreshing(false);
      }
    };
    void refresh();
    const interval = window.setInterval(() => void refresh(), 60_000);
    return () => {
      cancelled = true;
      window.clearInterval(interval);
    };
  }, [storedToken]);

  if (!snapshot?.available) return null;

  return (
    <Card padding="none">
      <div className="px-4 pt-4 pb-3 border-b border-border-subtle flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Cloud size={15} className="text-sky-400" />
          <div className="flex flex-col gap-0.5">
            <span className="eyebrow">Provider usage</span>
            <h2 className="text-sm font-semibold text-text">Cloud limits &amp; balances</h2>
          </div>
        </div>
        <div className="flex items-center gap-2">
          {refreshing ? <RefreshCw size={12} className="text-text-subtle animate-spin" /> : null}
          <Link to="/usage" className="pill pill-subtle pill-button text-xs flex items-center gap-1">
            Details <ArrowRight size={12} />
          </Link>
        </div>
      </div>
      <div className="p-4 grid grid-cols-1 sm:grid-cols-3 gap-3">
        {snapshot.providers.map((provider) => <ProviderCard key={provider.provider} provider={provider} />)}
      </div>
    </Card>
  );
}
