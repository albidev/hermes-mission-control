import { useI18n } from '../../lib/i18n';
import { useCallback, useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { AlertCircle, Bell, CheckCircle, Clock, ListChecks, PanelLeftClose } from 'lucide-react';
import {
  loadMissionControlCandidates,
  loadMissionControlVaults,
  type MissionControlAlert,
  type MissionControlVaultInfo,
} from '../../lib/hermes-api';
import { useMissionControl } from '../../lib/mission-control-store';
import { Card } from '../ui/Card';
import { Badge } from '../ui/Badge';

type AttentionNeededProps = {
  alerts: MissionControlAlert[];
};

function SectionLink({
  to,
  label,
  children,
}: {
  to: string;
  label: string;
  children: React.ReactNode;
}) {
  return (
    <Link
      to={to}
      className="inline-flex h-11 w-11 items-center justify-center rounded-[var(--control-radius)] text-text-muted transition-colors hover:bg-surface-sunken hover:text-accent sm:h-auto sm:w-auto sm:justify-start sm:gap-1 sm:rounded-none sm:bg-transparent"
      aria-label={label}
      title={label}
    >
      <span aria-hidden="true">{children}</span>
      <span className="hidden sm:inline">{label}</span>
    </Link>
  );
}

export function AttentionNeeded({ alerts }: AttentionNeededProps) {
  const { t } = useI18n();
  const { storedToken } = useMissionControl();
  const [pendingCandidates, setPendingCandidates] = useState(0);
  const [pendingByVault, setPendingByVault] = useState<Array<{ id: string; label: string; count: number }>>([]);

  const refreshPending = useCallback(async () => {
    try {
      const vaults = await loadMissionControlVaults(storedToken ?? undefined);
      const effectiveVaults: MissionControlVaultInfo[] = vaults.length
        ? vaults
        : [{ id: 'core', label: 'Core', candidates_dir: '' }];
      const snapshots = await Promise.all(
        effectiveVaults.map(async (vault) => ({
          id: vault.id,
          label: vault.label,
          count: (await loadMissionControlCandidates(storedToken ?? undefined, 'pending', vault.id)).count,
        })),
      );
      setPendingByVault(snapshots.filter((item) => item.count > 0));
      setPendingCandidates(snapshots.reduce((total, item) => total + item.count, 0));
    } catch {
      // Existing alert telemetry remains visible if Curate data is unavailable.
    }
  }, [storedToken]);

  useEffect(() => {
    void refreshPending();
    const timer = window.setInterval(() => void refreshPending(), 30_000);
    return () => window.clearInterval(timer);
  }, [refreshPending]);

  const errorAlerts = alerts.filter((a) => a.tone === 'bad');
  const warnAlerts = alerts.filter((a) => a.tone === 'warn');
  const goodAlerts = alerts.filter((a) => a.tone === 'good');

  const totalCount = errorAlerts.length + warnAlerts.length + pendingCandidates;
  const hasAttention = totalCount > 0;
  const curateHref = pendingByVault.length === 1
    ? `/curate?vault=${encodeURIComponent(pendingByVault[0].id)}`
    : '/curate';

  return (
    <Card padding="none">
      <div className="flex items-center justify-between px-3 pt-3 pb-2 border-b border-border-subtle">
        <div className="flex flex-col gap-0.5">
          <span className="eyebrow">{t('attention.eyebrow')}</span>
          <h2 className="text-sm font-semibold text-text">{t('attention.title')}</h2>
        </div>
        {hasAttention ? (
          <Badge variant={errorAlerts.length > 0 ? 'negative' : 'warning'}>
            {totalCount}
          </Badge>
        ) : (
          <Badge variant="positive">
            <CheckCircle className="h-3 w-3" />
            All clear
          </Badge>
        )}
      </div>

      <div className="p-3 flex flex-col gap-2.5">
        {hasAttention ? (
          <>
            {pendingCandidates > 0 && (
              <div className="flex items-start gap-2">
                <Bell className="h-4 w-4 text-warning mt-0.5 flex-shrink-0" />
                <div className="flex-1 min-w-0">
                  <p className="text-sm font-medium text-text">{t('attention.curatePending')}</p>
                  <p className="text-xs text-text-muted line-clamp-2 mt-0.5">
                    {pendingCandidates} candidate{pendingCandidates === 1 ? '' : 's'} awaiting review
                    {pendingByVault.length > 0
                      ? ` · ${pendingByVault.map((item) => `${item.label}: ${item.count}`).join(' · ')}`
                      : ''}
                  </p>
                </div>
                <Link
                  to={curateHref}
                  className="inline-flex h-11 w-11 shrink-0 items-center justify-center rounded-[var(--control-radius)] border border-warning/30 text-warning transition-colors hover:bg-warning/10 sm:h-auto sm:w-auto sm:gap-1 sm:px-2 sm:py-1 sm:text-xs"
                  aria-label={t('attention.review')}
                  title={t('attention.review')}
                >
                  <ListChecks aria-hidden="true" className="h-4 w-4 sm:h-3 sm:w-3" />
                  <span className="hidden sm:inline">{t('attention.review')}</span>
                </Link>
              </div>
            )}
            {/* Errors first */}
            {errorAlerts.slice(0, 2).map((alert) => (
              <div key={alert.id} className="flex items-start gap-2">
                <AlertCircle className="h-4 w-4 text-negative mt-0.5 flex-shrink-0" />
                <div className="flex-1 min-w-0">
                  <p className="text-sm font-medium text-text truncate">{alert.title}</p>
                  <p className="text-xs text-text-muted line-clamp-1 mt-0.5">
                    {alert.detail}
                  </p>
                </div>
                <Badge variant="negative" className="flex-shrink-0">
                  {alert.category}
                </Badge>
              </div>
            ))}

            {/* Warnings */}
            {warnAlerts.slice(0, 2).map((alert) => (
              <div key={alert.id} className="flex items-start gap-2">
                <Clock className="h-4 w-4 text-warning mt-0.5 flex-shrink-0" />
                <div className="flex-1 min-w-0">
                  <p className="text-sm font-medium text-text truncate">{alert.title}</p>
                  <p className="text-xs text-text-muted line-clamp-1 mt-0.5">
                    {alert.detail}
                  </p>
                </div>
                <Badge variant="warning" className="flex-shrink-0">
                  {alert.category}
                </Badge>
              </div>
            ))}
          </>
        ) : (
          <div className="flex flex-col items-center gap-2 py-3 text-center">
            <CheckCircle className="h-7 w-7 text-positive" />
            <p className="text-sm text-text-muted">{t('attention.noIssues')}</p>
          </div>
        )}

        {/* Footer link */}
        <div className="flex items-center justify-between pt-2 border-t border-border-subtle">
          <SectionLink
            to={pendingCandidates > 0 ? curateHref : '/sessions'}
            label={pendingCandidates > 0 ? t('attention.reviewCurate') : t('attention.viewAll')}
          >
              {pendingCandidates > 0 ? <ListChecks className="h-4 w-4" /> : <PanelLeftClose className="h-4 w-4" />}
          </SectionLink>
          <span className="text-xs text-text-subtle">
            {goodAlerts.length > 0 ? `${goodAlerts.length} info` : ''}
          </span>
        </div>
      </div>
    </Card>
  );
}
