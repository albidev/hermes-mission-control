import { useI18n } from '../../lib/i18n';
import { useEffect, useState } from 'react';
import { Cloud, RefreshCw } from 'lucide-react';
import { Card } from '../ui/Card';
import {
  loadProviderUsage,
  type MissionControlProviderUsage,
  type MissionControlProviderUsageBalance,
  type MissionControlProviderUsageSnapshot,
  type MissionControlProviderUsageWindow,
} from '../../lib/hermes-api';
import { useMissionControl } from '../../lib/mission-control-store';

const PROVIDER_LABELS: Record<string, string> = {
  codex: 'Codex',
  ollama: 'Ollama Cloud',
  openrouter: 'OpenRouter',
  nous: 'Nous Portal',
};

function formatNumber(value?: number | null): string {
  return typeof value === 'number' ? value.toFixed(value % 1 === 0 ? 0 : 2) : '—';
}

function formatValue(value?: number | null, currency?: string, unit?: string): string {
  if (typeof value !== 'number') return '—';
  if (currency === 'USD') return `$${value.toFixed(2)}`;
  return `${formatNumber(value)}${unit ? ` ${unit}` : ''}`;
}

function formatDate(value: string | undefined): string | null {
  if (!value) return null;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  return date.toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

function formatReset(value: string | undefined, t: (key: string, values?: Record<string, string | number>) => string): string {
  const date = formatDate(value);
  return date ? t('provider.reset', { date }) : t('provider.resetUnknown');
}

function formatRenews(value: string | undefined, t: (key: string, values?: Record<string, string | number>) => string): string | null {
  const date = formatDate(value);
  return date ? t('provider.renews', { date }) : null;
}

function windowLabel(window: MissionControlProviderUsageWindow, t: (key: string) => string): string {
  if (window.id === 'primary') return t('provider.session');
  if (window.id === 'secondary') return t('provider.weekly');
  if (window.id === 'subscription') return t('provider.subscription');
  return window.label;
}

function balanceLabel(balance: MissionControlProviderUsageBalance, t: (key: string) => string): string {
  const labels: Record<string, string> = {
    balance: t('provider.balance'),
    subscription_remaining: t('provider.subscriptionRemaining'),
    topup_remaining: t('provider.topupRemaining'),
    total_spendable: t('provider.totalSpendable'),
    credits_remaining: t('provider.creditsRemaining'),
  };
  return labels[balance.id] ?? balance.label;
}

function metricLabel(metric: { id: string; label: string }, t: (key: string) => string): string {
  if (metric.id === 'reset_credits_available') return t('provider.resetCredits');
  return metric.label;
}

function gaugeTone(value: number): { className?: string; color: string } {
  if (value >= 85) return { className: 'bg-negative', color: '' };
  if (value >= 60) return { className: 'bg-warning', color: '' };
  return { color: 'var(--color-usage-session)' };
}

function UsageGauge({
  label,
  window,
  t,
}: {
  label: string;
  window: MissionControlProviderUsageWindow;
  t: (key: string, values?: Record<string, string | number>) => string;
}) {
  const value = typeof window.usedPercent === 'number'
    ? Math.max(0, Math.min(100, window.usedPercent))
    : null;
  const tone = value === null ? null : gaugeTone(value);
  return (
    <div className="flex flex-col gap-1.5" title={value === null ? `${label}: ${t('provider.unavailableShort')}` : `${label}: ${formatNumber(value)}%`}>
      <div className="flex items-center justify-between gap-2 text-[10px]">
        <span className="text-text-muted uppercase tracking-wide">{label}</span>
        <span className="text-text tabular-nums font-medium">{value === null ? '—' : `${formatNumber(value)}%`}</span>
      </div>
      <div className="h-1.5 w-full overflow-hidden rounded-full bg-surface-sunken" role="progressbar" aria-label={label} aria-valuemin={0} aria-valuemax={100} aria-valuenow={value ?? undefined}>
        {tone ? <div className={`h-full rounded-full transition-[width] duration-300 ${tone.className ?? ''}`} style={{ width: `${value}%`, backgroundColor: tone.color || undefined }} /> : null}
      </div>
      <span className="text-[10px] text-text-subtle truncate">{formatReset(window.resetsAt, t)}</span>
    </div>
  );
}

function ProviderCard({ provider }: { provider: MissionControlProviderUsage }) {
  const { t } = useI18n();
  const label = PROVIDER_LABELS[provider.provider] ?? provider.provider;
  const unavailable = !provider.available;
  const balances = provider.balances.filter((balance) => typeof balance.value === 'number');
  const primaryBalance = balances.find((balance) => balance.id === 'total_spendable' || balance.id === 'balance') ?? balances[0];
  const secondaryBalances = balances.filter((balance) => balance !== primaryBalance);
  const metrics = provider.metrics.filter((metric) => metric.value !== null && metric.value !== undefined);
  const featuredMetrics = metrics.filter((metric) => metric.featured);
  const regularMetrics = metrics.filter((metric) => !metric.featured);

  return (
    <div className="rounded-lg border border-border-subtle bg-surface/40 p-2.5 flex flex-col gap-2 min-w-0 min-h-[108px]">
      <div className="flex items-center justify-between gap-2">
        <div className="flex min-w-0 items-center gap-2">
          <span className="text-xs font-semibold text-text truncate">{label}</span>
          {provider.plan ? <span className="text-[10px] text-text-subtle truncate">{provider.plan}</span> : null}
        </div>
        <div className="flex items-center gap-1.5 shrink-0">
          {provider.stale ? <span className="text-[10px] text-amber-400">{t('provider.stale')}</span> : null}
          <span className={`h-1.5 w-1.5 rounded-full ${unavailable ? 'bg-amber-400' : 'bg-emerald-400'}`} />
        </div>
      </div>
      {unavailable ? (
        <div className="flex flex-1 items-center">
          <span className="text-xs text-text-muted line-clamp-2">{provider.error || t('provider.unavailableShort')}</span>
        </div>
      ) : (
        <div className="flex flex-1 flex-col gap-2.5">
          {featuredMetrics.length > 0 ? (
            <div className="rounded-md border border-sky-400/20 bg-sky-400/5 px-2.5 py-2">
              {featuredMetrics.slice(0, 1).map((metric) => (
                <div key={metric.id}>
                  <span className="text-[10px] text-text-muted uppercase tracking-wide">{metricLabel(metric, t)}</span>
                  <div className="text-2xl font-semibold tabular-nums text-sky-400">
                    {typeof metric.value === 'boolean' ? (metric.value ? t('provider.enabled') : t('provider.disabled')) : String(metric.value)}
                  </div>
                </div>
              ))}
            </div>
          ) : null}
          {primaryBalance ? (
            <div>
              <span className="text-[10px] text-text-muted uppercase tracking-wide">{balanceLabel(primaryBalance, t)}</span>
              <div className="text-lg font-semibold tabular-nums text-emerald-400">
                {formatValue(primaryBalance.value, primaryBalance.currency, primaryBalance.unit)}
              </div>
            </div>
          ) : null}
          {secondaryBalances.length > 0 ? (
            <div className="grid grid-cols-2 gap-2">
              {secondaryBalances.slice(0, 2).map((balance) => (
                <div key={balance.id} className="min-w-0">
                  <span className="text-[10px] text-text-muted uppercase tracking-wide truncate block">{balanceLabel(balance, t)}</span>
                  <span className="text-xs text-text tabular-nums">{formatValue(balance.value, balance.currency, balance.unit)}</span>
                </div>
              ))}
            </div>
          ) : null}
          {provider.windows.map((window) => (
            <UsageGauge key={window.id} label={windowLabel(window, t)} window={window} t={t} />
          ))}
          {provider.renewsAt && formatRenews(provider.renewsAt, t) ? <span className="text-[10px] text-text-subtle">{formatRenews(provider.renewsAt, t)}</span> : null}
          {regularMetrics.length > 0 ? (
            <div className="flex flex-wrap justify-end gap-x-2 gap-y-1">
              {regularMetrics.slice(0, 2).map((metric) => (
                <span key={metric.id} className="text-[10px] text-text-subtle">
                  {metricLabel(metric, t)}: {typeof metric.value === 'boolean' ? (metric.value ? t('provider.enabled') : t('provider.disabled')) : String(metric.value)}
                </span>
              ))}
            </div>
          ) : null}
        </div>
      )}
    </div>
  );
}

export function ProviderUsagePanel() {
  const { t } = useI18n();
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
        <div className="px-3 pt-3 pb-2 border-b border-border-subtle flex items-center justify-between">
          <div className="flex items-center gap-2">
            <Cloud size={15} className="text-sky-400" />
            <div className="flex flex-col gap-0.5">
              <span className="eyebrow">{t('overview.providerUsage')}</span>
              <h2 className="text-sm font-semibold text-text">{t('ui.cloudLimitsBalances')}</h2>
            </div>
          </div>
          {refreshing ? <RefreshCw size={12} className="text-text-subtle animate-spin" /> : null}
        </div>
        <div className="p-3"><p className="text-sm text-text-muted">{snapshot ? t('provider.unavailable') : t('provider.loading')}</p></div>
      </Card>
    );
  }

  return (
    <Card padding="none">
      <div className="px-3 pt-3 pb-2 border-b border-border-subtle flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Cloud size={15} className="text-sky-400" />
          <div className="flex flex-col gap-0.5">
            <span className="eyebrow">{t('overview.providerUsage')}</span>
            <h2 className="text-sm font-semibold text-text">{t('ui.cloudLimitsBalances')}</h2>
          </div>
        </div>
        <div className="flex items-center gap-2">
          {refreshing ? <RefreshCw size={12} className="text-text-subtle animate-spin" /> : null}
          <span className="text-[10px] text-text-subtle">{t('provider.live')}</span>
        </div>
      </div>
      <div className="p-3 grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
        {snapshot.providers.map((provider) => <ProviderCard key={provider.provider} provider={provider} />)}
      </div>
    </Card>
  );
}
