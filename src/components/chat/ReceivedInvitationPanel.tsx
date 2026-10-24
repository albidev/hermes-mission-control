import { useEffect, useMemo, useRef, useState } from 'react';
import { Loader2, ShieldCheck } from 'lucide-react';
import { useI18n } from '../../lib/i18n';
import { createRoomJoiningApi, type JoiningImport, type RoomJoiningApi } from '../../lib/room-joining-api';
import { canGiveConsent, startJoiningPoll, type JoiningStep } from '../../lib/room-joining-state';
import { inviteErrorCode, isInviteUnavailable } from '../../lib/invite-ui-errors';
import { Button } from '../ui/Button';
import { Stepper } from '../ui/Stepper';

type ReceivedInvitationPanelProps = {
  accessToken?: string;
  api?: RoomJoiningApi;
  onOpenRoom?: (roomId: string) => void;
  onPendingChange?: (count: number) => void;
};
const steps: JoiningStep[] = [
  'paste', 'host', 'metadata', 'redeem', 'approval', 'scope', 'scope_confirmed', 'activation', 'open',
];

export function ReceivedInvitationPanel({
  accessToken, api: override, onOpenRoom, onPendingChange,
}: ReceivedInvitationPanelProps) {
  const { t } = useI18n();
  const api = useMemo(() => override ?? createRoomJoiningApi(accessToken), [accessToken, override]);
  const [step, setStep] = useState<JoiningStep>('paste');
  const [code, setCode] = useState('');
  const [record, setRecord] = useState<JoiningImport | null>(null);
  const [scopeConfirmed, setScopeConfirmed] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [hidden, setHidden] = useState(false);
  const [busy, setBusy] = useState(false);
  const pendingCallback = useRef(onPendingChange);
  pendingCallback.current = onPendingChange;
  useEffect(() => {
    pendingCallback.current?.(record && !['open', 'expired', 'cancelled'].includes(step) ? 1 : 0);
  }, [record, step]);
  const lifecycle = useRef<AbortController | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    lifecycle.current = controller;
    return () => controller.abort();
  }, []);

  const showError = (cause: unknown) => {
    if (isInviteUnavailable(cause)) setHidden(true);
    else setError(inviteErrorCode(cause));
  };
  const run = async (action: (signal: AbortSignal) => Promise<JoiningImport>, next: JoiningStep) => {
    const signal = lifecycle.current?.signal;
    if (!signal || busy) return;
    setBusy(true);
    setError(null);
    try {
      const result = await action(signal);
      if (signal.aborted) return;
      setRecord((current) => ({ ...current, ...result }));
      setCode('');
      if (result.status === 'expired') setStep('expired');
      else if (next === 'host' && !result.host) setStep('metadata');
      else setStep(next);
    } catch (cause) {
      if (!signal.aborted) showError(cause);
    } finally { if (!signal.aborted) setBusy(false); }
  };

  useEffect(() => {
    if (!record?.id || !['approval', 'activation'].includes(step)) return;
    return startJoiningPoll({
      read: (signal) => api.getImport(record.id, signal),
      onValue: (next) => {
        setRecord((current) => ({ ...current, ...next }));
        if (['expired', 'rejected', 'cancelled'].includes(next.status)) {
          setStep(next.status === 'expired' ? 'expired' : 'cancelled');
          return true;
        }
        if (step === 'approval' && next.scope) {
          setScopeConfirmed(false);
          setStep('scope');
          return true;
        }
        if (step === 'activation' && next.status === 'active' && next.room_id) {
          setStep('open');
          return true;
        }
        return false;
      },
      onError: showError,
    });
  }, [api, record?.id, step]);

  if (hidden) return null;
  const id = record?.id ?? '';
  const scope = record?.scope;
  const content = (() => {
    switch (step) {
      case 'paste':
        return (
          <div className="grid gap-3">
            <label className="grid gap-2 text-sm text-text-muted">
              {t('inviteUi.pasteInvitation')}
              <textarea className="mc-input min-h-24 w-full" value={code} autoComplete="off" spellCheck={false}
                onChange={(event) => setCode(event.target.value)} />
            </label>
            <Button variant="primary" loading={busy} disabled={!code.trim()}
              onClick={() => void run((signal) => api.preview({ code: code.trim() }, signal), 'host')}>
              {t('inviteUi.preview')}
            </Button>
          </div>
        );
      case 'host':
        return (
          <div className="grid gap-3">
            <div className="invite-notice">
              <ShieldCheck size={18} aria-hidden="true" />
              <div className="min-w-0 break-words">
                <strong>{record?.host?.profile}</strong>
                <p>{record?.host?.installation_id}</p>
                <code className="break-all">{record?.host?.fingerprint}</code>
              </div>
            </div>
            <Button loading={busy} onClick={() => void run((signal) => api.metadata(id, signal), 'metadata')}>
              {t('inviteUi.fetchMetadata')}
            </Button>
          </div>
        );
      case 'metadata':
        return (
          <div className="grid gap-3">
            {record?.metadata ? (
              <div className="text-sm text-text">
                <p className="font-semibold">{record.metadata.name}</p>
                <p className="mt-1 text-xs text-text-muted">{record.metadata.expires_at}</p>
              </div>
            ) : <p className="text-sm text-text-muted">{t('inviteUi.metadataHint')}</p>}
            <Button loading={busy} onClick={() => setStep('redeem')}>
              {t('inviteUi.reviewMetadata')}
            </Button>
          </div>
        );
      case 'redeem':
        return (
          <Button variant="primary" loading={busy} onClick={() => void run((signal) => api.redeem(id, signal), 'approval')}>
            {t('inviteUi.redeem')}
          </Button>
        );
      case 'approval':
      case 'activation':
        return (
          <div className="invite-notice" role="status">
            <Loader2 size={18} className="shrink-0 animate-spin" aria-hidden="true" />
            <span>{t(step === 'approval' ? 'inviteUi.waitApproval' : 'inviteUi.waitActivation')}</span>
          </div>
        );
      case 'scope':
        return scope ? (
          <div className="grid gap-3">
            <dl className="invite-scope">
              <dt>{t('inviteUi.roomId')}</dt><dd>{scope.room_id}</dd>
              <dt>{t('inviteUi.memberId')}</dt><dd>{scope.member_id}</dd>
              <dt>{t('inviteUi.permissions')}</dt><dd>{scope.permissions.join(', ')}</dd>
              <dt>{t('inviteUi.policyDigest')}</dt><dd>{scope.policy_digest ?? '—'}</dd>
              <dt>{t('inviteUi.catalog')}</dt><dd><pre>{JSON.stringify(scope.catalog, null, 2)}</pre></dd>
            </dl>
            <Button loading={busy} onClick={() => void run(async (signal) => {
              const result = await api.confirmScope(id, { room_id: scope.room_id, member_id: scope.member_id }, signal);
              if (!signal.aborted) setScopeConfirmed(true);
              return result;
            }, 'scope_confirmed')}>
              {t('inviteUi.confirmScope')}
            </Button>
          </div>
        ) : null;
      case 'scope_confirmed':
        return (
          <div className="grid gap-4">
            <div className="invite-notice">
              <ShieldCheck size={20} className="shrink-0" aria-hidden="true" />
              <div>
                <strong>{t('inviteUi.scopeChecked')}</strong>
                <p className="mt-1">{scope?.permissions.join(', ')}</p>
              </div>
            </div>
            <p className="text-sm leading-relaxed text-text-muted">{t('inviteUi.consentHint')}</p>
            <Button variant="primary" loading={busy} disabled={!canGiveConsent(step, scopeConfirmed)}
              onClick={() => void run((signal) => api.consent(id, true, signal), 'activation')}>
              {t('inviteUi.giveConsent')}
            </Button>
          </div>
        );
      case 'open':
        return (
          <Button variant="primary" disabled={!record?.room_id} onClick={() => onOpenRoom?.(record!.room_id!)}>
            {t('inviteUi.openRoom')}
          </Button>
        );
      default: return <p role="status">{t(`inviteUi.pairResult.${step}`)}</p>;
    }
  })();
  const visibleSteps = record && !record.host ? steps.filter((item) => item !== 'host') : steps;
  return (
    <section className="invite-ui grid gap-4" aria-label={t('inviteUi.invitations')}>
      <header>
        <p className="eyebrow">{t('inviteUi.invitations')}</p>
        <h2 className="mt-1 text-xl font-semibold text-text">{t('inviteUi.received')}</h2>
        <p className="mt-2 text-sm leading-relaxed text-text-muted">{t('inviteUi.invitationLead')}</p>
      </header>
      {error ? <p className="invite-error" role="alert">{t('inviteUi.error')} ({error})</p> : null}
      {['cancelled', 'expired'].includes(step) ? content : (
        <Stepper active={step} steps={visibleSteps.map((item) => ({
          id: item, label: t(`inviteUi.joinStep.${item}`), content: item === step ? content : undefined,
        }))} />
      )}
      {!['open', 'cancelled', 'expired'].includes(step) ? (
        <Button variant="ghost" disabled={busy} onClick={() => {
          lifecycle.current?.abort();
          setCode('');
          setScopeConfirmed(false);
          setStep('cancelled');
        }}>{t('inviteUi.cancel')}</Button>
      ) : null}
    </section>
  );
}
