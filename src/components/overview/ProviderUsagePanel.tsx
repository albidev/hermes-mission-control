import { useEffect, useState } from 'react';
import { Cloud, RefreshCw } from 'lucide-react';
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

function mainWindow(provider: MissionControlProviderUsage) {
  return provider.primary ?? provider.secondary ?? null;
}

function mainMetricLabel(provider: MissionControlProviderUsage): string {
  if (provider.provider === 'openrouter') return 'Balance';
  return provider.primary ? 'Session' : 'Weekly';
}

function secondaryMetric(provider: MissionControlProviderUsage): string {
  if (provider.provider === 'openrouter') {
    const monthly = provider.openRouter?.keyUsageMonthly;
    return typeof monthly === 'number' ? `Monthly ${formatBalance(monthly)}` : 'Credits & rate limit';
  }
  if (provider.provider === 'codex' && !provider.primary) return 'Reset credits available';
  return provider.secondary?.usedPercent !== undefined
    ? `Weekly ${formatPercent(provider.secondary.usedPercent)}`
    : 'Weekly —';
}

function resetLabel(provider: MissionControlProviderUsage): string {
  if (provider.provider === 'openrouter') return 'API key';
  const reset = mainWindow(provider)?.resetsAt;
  return reset ? formatReset(reset) : 'Reset unknown';
}

function mainMetricValue(provider: MissionControlProviderUsage): string {
  if (provider.provider === 'openrouter') return formatBalance(provider.openRouter?.balance);
  return formatPercent(mainWindow(provider)?.usedPercent);
}

function ProviderCard({ provider }: { provider: MissionControlProviderUsage }) {
  const label = PROVIDER_LABELS[provider.provider] ?? provider.provider;
  const unavailable = !provider.available;
  return (
    <div className="rounded-lg border border-border-subtle bg-surface/40 p-3 flex flex-col gap-3 min-w-0 min-h-[124px]">
      <div className="flex items-center justify-between gap-2">
        <span className="text-xs font-semibold text-text truncate">{label}</span>
        <span className={`h-1.5 w-1.5 rounded-full shrink-0 ${unavailable ? 'bg-amber-400' : 'bg-emerald-400'}`} />
      </div>
      {unavailable ? (
        <div className="flex flex-1 items-center">
          <span className="text-xs text-text-muted line-clamp-2">{provider.error || 'Unavailable'}</span>
        </div>
      ) : (
        <div className="flex flex-1 flex-col justify-between gap-3">
          <div className="flex items-end justify-between gap-2">
            <div className="min-w-0">
              <span className="text-[10px] text-text-muted uppercase tracking-wide">{mainMetricLabel(provider)}</span>
              <div className={`text-xl font-semibold tabular-nums ${provider.provider === 'openrouter' ? 'text-emerald-400' : 'text-sky-400'}`}>
                {mainMetricValue(provider)}
              </div>
            </div>
            {typeof provider.resetCreditsAvailable === 'number' ? (
              <span className="text-[10px] text-text-subtle text-right">{provider.resetCreditsAvailable} reset credits</span>
            ) : null}
          </div>
          <div className="flex items-center justify-between gap-2 text-[10px] text-text-subtle truncate">
            <span className="truncate">{secondaryMetric(provider)}</span>
            <span className="truncate text-right">{resetLabel(provider)}</span>
          </div>
        </div>
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

  if (!snapshot?.available) {
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
          {refreshing ? <RefreshCw size={12} className="text-text-subtle animate-spin" /> : null}
        </div>
        <div className="p-4">
          <p className="text-sm text-text-muted">
            {snapshot ? 'Provider usage is temporarily unavailable.' : 'Loading provider usage…'}
          </p>
        </div>
      </Card>
    );
  }

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
          <span className="text-[10px] text-text-subtle">Live · 60s</span>
        </div>
      </div>
      <div className="p-4 grid grid-cols-1 sm:grid-cols-3 gap-3">
        {snapshot.providers.map((provider) => <ProviderCard key={provider.provider} provider={provider} />)}
      </div>
    </Card>
  );
}
