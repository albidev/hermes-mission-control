import { useEffect, useMemo, useState } from 'react';
import { BrainCircuit, ExternalLink, RefreshCw, ShieldCheck, Users } from 'lucide-react';
import { useI18n } from '../lib/i18n';
import { useMissionControl } from '../lib/mission-control-store';
import {
  configureHonchoLocalIdentity,
  loadHonchoStatus,
  type HonchoProfileStatus,
  type HonchoStatus,
} from '../lib/honcho-settings';
import { Badge } from './ui/Badge';
import { Button } from './ui/Button';
import { Card } from './ui/Card';

function readinessVariant(readiness: HonchoProfileStatus['readiness']) {
  if (readiness === 'ready') return 'positive' as const;
  if (readiness === 'disabled') return 'default' as const;
  return 'warning' as const;
}

export function HonchoSettingsPanel() {
  const { t } = useI18n();
  const { storedToken } = useMissionControl();
  const [status, setStatus] = useState<HonchoStatus | null>(null);
  const [peerName, setPeerName] = useState('');
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = async () => {
    setLoading(true);
    setError(null);
    try {
      const next = await loadHonchoStatus(storedToken || undefined);
      setStatus(next);
      setPeerName(next.peerName ?? '');
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : t('honcho.statusFailed'));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void refresh();
  }, [storedToken]);

  const readyProfiles = useMemo(
    () => status?.profiles.filter((profile) => profile.readiness === 'ready').length ?? 0,
    [status],
  );

  const configure = async () => {
    setSaving(true);
    setError(null);
    setMessage(null);
    try {
      const next = await configureHonchoLocalIdentity(peerName, storedToken || undefined);
      setStatus(next);
      setPeerName(next.peerName ?? peerName);
      setMessage(t('honcho.saved', { count: next.configuredProfiles?.length ?? next.profileCount }));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : t('honcho.saveFailed'));
    } finally {
      setSaving(false);
    }
  };

  const loginHref = `/login?next=${encodeURIComponent(`${window.location.pathname}${window.location.search}`)}`;
  const isReady = Boolean(status?.providerActive && status.configured && status.identityReady);

  // Honcho is a Hermes memory provider: if it is not installed on this host
  // there is nothing to configure, so the whole panel stays out of the page.
  // While the first probe is in flight we render nothing rather than flashing
  // setup UI for a provider that may not exist.
  if (loading && !status) return null;
  if (status && !status.providerInstalled) return null;

  return (
    <Card padding="none" className="!border-0">
      <div className="flex flex-col gap-3 border-b border-border px-4 py-4 sm:flex-row sm:items-start sm:justify-between">
        <div className="flex min-w-0 items-start gap-3">
          <div className="rounded-lg border border-violet-400/20 bg-violet-400/10 p-2 text-violet-300">
            <BrainCircuit className="h-5 w-5" aria-hidden />
          </div>
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <h2 className="text-sm font-semibold text-text">{t('honcho.title')}</h2>
              <Badge variant={isReady ? 'positive' : status?.configured ? 'warning' : 'default'} dot>
                {loading ? t('honcho.checking') : isReady ? t('honcho.ready') : t('honcho.needsSetup')}
              </Badge>
            </div>
            <p className="mt-1 max-w-3xl text-xs leading-relaxed text-text-muted">{t('honcho.description')}</p>
          </div>
        </div>
        <Button
          type="button"
          size="sm"
          variant="ghost"
          icon={<RefreshCw className="h-4 w-4" />}
          onClick={() => void refresh()}
          loading={loading}
        >
          {t('honcho.refresh')}
        </Button>
      </div>

      {error ? (
        <div className="border-b border-negative/20 bg-negative-subtle px-4 py-3 text-xs text-negative">{error}</div>
      ) : null}
      {message ? (
        <div className="border-b border-positive/20 bg-positive-subtle px-4 py-3 text-xs text-positive">{message}</div>
      ) : null}

      <div className="grid min-w-0 gap-4 p-4 xl:grid-cols-[minmax(0,0.9fr)_minmax(0,1.1fr)]">
        <div className="flex min-w-0 flex-col gap-3">
          <div className="rounded-lg border border-border-subtle bg-surface-sunken/30 p-3">
            <div className="flex items-center gap-2 text-xs font-semibold text-text">
              <ShieldCheck className="h-4 w-4 text-emerald-400" aria-hidden />
              {t('honcho.identityTitle')}
            </div>
            <p className="mt-2 text-xs leading-relaxed text-text-muted">{t('honcho.identityHelp')}</p>
            <label className="mt-3 block text-[11px] font-medium uppercase tracking-wide text-text-subtle" htmlFor="honcho-peer-name">
              {t('honcho.peerName')}
            </label>
            <div className="mt-1.5 flex flex-col gap-2 sm:flex-row">
              <input
                id="honcho-peer-name"
                type="text"
                value={peerName}
                maxLength={64}
                pattern="[A-Za-z0-9][A-Za-z0-9._-]{0,63}"
                autoComplete="off"
                placeholder={t('honcho.peerPlaceholder')}
                onChange={(event) => setPeerName(event.target.value)}
                className="h-11 min-w-0 flex-1 rounded-lg border border-border bg-surface px-3 text-sm text-text outline-none focus:border-accent"
              />
              <Button
                type="button"
                variant="primary"
                loading={saving}
                disabled={!peerName.trim() || loading}
                onClick={() => void configure()}
              >
                {status?.identityReady ? t('honcho.updateIdentity') : t('honcho.configureIdentity')}
              </Button>
            </div>
            <p className="mt-2 text-[11px] leading-relaxed text-warning">{t('honcho.singleUserWarning')}</p>
          </div>

          <a
            href={loginHref}
            className="flex min-h-11 items-center justify-between gap-3 rounded-lg border border-border-subtle bg-surface-elevated/20 px-3 py-2 text-xs text-text-muted transition hover:border-accent/40 hover:text-text"
          >
            <span>
              <strong className="block font-medium text-text">{t('honcho.authenticatedTitle')}</strong>
              <span className="mt-0.5 block text-[11px] text-text-subtle">{t('honcho.authenticatedHelp')}</span>
            </span>
            <ExternalLink className="h-4 w-4 shrink-0" aria-hidden />
          </a>

          {status && !status.providerActive ? (
            <div className="rounded-lg border border-warning/20 bg-warning-subtle p-3 text-xs text-warning">
              {t('honcho.providerInactive')}
              <code className="mt-2 block rounded bg-surface/60 px-2 py-1.5 font-mono text-[11px] text-text">memory.provider: honcho</code>
            </div>
          ) : null}
          {status && !status.configured ? (
            <div className="rounded-lg border border-warning/20 bg-warning-subtle p-3 text-xs text-warning">
              {t('honcho.connectionMissing')}
              <code className="mt-2 block rounded bg-surface/60 px-2 py-1.5 font-mono text-[11px] text-text">hermes memory setup honcho</code>
            </div>
          ) : null}
        </div>

        <div className="min-w-0 rounded-lg border border-border-subtle bg-surface-elevated/15">
          <div className="flex items-center justify-between gap-3 border-b border-border px-3 py-3">
            <div className="flex items-center gap-2">
              <Users className="h-4 w-4 text-sky-400" aria-hidden />
              <div>
                <p className="text-xs font-semibold text-text">{t('honcho.profilesTitle')}</p>
                <p className="text-[11px] text-text-subtle">{t('honcho.profilesSummary', { ready: readyProfiles, count: status?.profileCount ?? 0 })}</p>
              </div>
            </div>
            {status ? <Badge variant="accent">{status.workspace}</Badge> : null}
          </div>
          <div className="flex max-h-[360px] flex-col divide-y divide-border overflow-y-auto">
            {status?.profiles.map((profile) => (
              <div key={profile.profile} className="grid min-w-0 grid-cols-[minmax(0,1fr)_auto] gap-3 px-3 py-3">
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-2">
                    <p className="truncate text-xs font-medium text-text">{profile.profile}</p>
                    <Badge variant={readinessVariant(profile.readiness)}>{t(`honcho.readiness.${profile.readiness}`)}</Badge>
                  </div>
                  <p className="mt-1 truncate text-[11px] text-text-subtle">AI peer · {profile.aiPeer}</p>
                  {profile.managedBy === 'profile-local' ? (
                    <p className="mt-1 text-[10px] text-text-subtle">{t('honcho.profileLocal')}</p>
                  ) : null}
                  {profile.managedBy === 'profile-local' && profile.readiness !== 'ready' ? (
                    <code className="mt-1 block truncate text-[10px] text-warning">hermes -p {profile.profile} memory setup honcho</code>
                  ) : !profile.connectionConfigured ? (
                    <code className="mt-1 block truncate text-[10px] text-warning">hermes -p {profile.profile} memory setup honcho</code>
                  ) : null}
                </div>
                <div className="text-right text-[10px] text-text-subtle">
                  <p>{profile.sessionAiPeerPrefix ? t('honcho.namespaced') : t('honcho.sharedSessionNames')}</p>
                  <p>{profile.peerPinned ? t('honcho.peerPinned') : t('honcho.runtimeIdentity')}</p>
                </div>
              </div>
            ))}
            {!loading && (status?.profiles.length ?? 0) === 0 ? (
              <p className="px-3 py-6 text-center text-xs text-text-muted">{t('honcho.noProfiles')}</p>
            ) : null}
          </div>
        </div>
      </div>
    </Card>
  );
}
