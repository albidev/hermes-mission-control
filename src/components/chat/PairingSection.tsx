import { useEffect, useMemo, useReducer, useState } from 'react';
import { Copy, ShieldCheck, Trash2 } from 'lucide-react';
import { useI18n } from '../../lib/i18n';
import { createRoomPairingApi, type PairingRow, type RoomPairingApi } from '../../lib/room-pairing-api';
import { initialPairingWizardState, pairingWizardReducer, secondsRemaining, canConfirmSas } from '../../lib/room-joining-state';
import { inviteErrorCode, isInviteUnavailable } from '../../lib/invite-ui-errors';
import { BottomSheet } from '../ui/BottomSheet';
import { Button } from '../ui/Button';
import { Stepper } from '../ui/Stepper';

type PairingSectionProps = { accessToken?: string; api?: RoomPairingApi };

function PeerList({ rows, busy, onRemove }: {
  rows: PairingRow[];
  busy: boolean;
  onRemove: (id: string) => void;
}) {
  const { t } = useI18n();
  return (
    <section className="invite-card" aria-label={t('inviteUi.peers')}>
      <h3 className="text-base font-semibold text-text">{t('inviteUi.peers')}</h3>
      <div className="mt-4 grid gap-3">
        {!rows.length ? <p className="text-sm text-text-muted">{t('inviteUi.noPeers')}</p> : null}
        {rows.map((row) => (
          <article className="invite-peer" key={row.id}>
            <ShieldCheck size={22} className="shrink-0 text-accent" aria-hidden="true" />
            <div className="min-w-0 flex-1 break-words">
              <h4 className="font-semibold text-text">{row.peer?.profile ?? t('inviteUi.peer')}</h4>
              <p className="mt-1 text-xs text-text-muted">{row.peer?.installation_id ?? '—'}</p>
              <p className="mt-1 text-xs text-text-muted">
                {t(row.state === 'acknowledged' ? 'inviteUi.linkReady' : 'inviteUi.linkPending')}
              </p>
              {row.peer?.policy_digest ? <code className="mt-2 block break-all text-xs">{row.peer.policy_digest}</code> : null}
            </div>
            {row.peer ? (
              <Button
                size="sm" variant="danger" disabled={busy}
                aria-label={t('inviteUi.removePeer', { peer: row.peer.profile })}
                icon={<Trash2 size={14} />} onClick={() => onRemove(row.peer!.peer_id)}
              >
                {t('inviteUi.remove')}
              </Button>
            ) : null}
          </article>
        ))}
      </div>
      <p className="mt-4 text-xs leading-relaxed text-text-muted">{t('inviteUi.unpairHint')}</p>
    </section>
  );
}

export function PairingSection({ accessToken, api: apiOverride }: PairingSectionProps) {
  const { t } = useI18n();
  const api = useMemo(() => apiOverride ?? createRoomPairingApi(accessToken), [accessToken, apiOverride]);
  const [state, dispatch] = useReducer(pairingWizardReducer, initialPairingWizardState);
  const [rows, setRows] = useState<PairingRow[]>([]);
  const [role, setRole] = useState<'host' | 'joining'>('host');
  const [input, setInput] = useState('');
  const [outgoing, setOutgoing] = useState('');
  const [typedSas, setTypedSas] = useState('');
  const [peer, setPeer] = useState('');
  const [busy, setBusy] = useState(false);
  const [hidden, setHidden] = useState(false);
  const [loading, setLoading] = useState(true);
  const [sheetOpen, setSheetOpen] = useState(false);
  const [copied, setCopied] = useState(false);
  const [now, setNow] = useState(Date.now());

  useEffect(() => {
    const controller = new AbortController();
    setHidden(false);
    setLoading(true);
    api.listPairings(controller.signal).then((next) => {
      if (!controller.signal.aborted) setRows(next);
    }).catch((error) => {
      if (controller.signal.aborted) return;
      if (isInviteUnavailable(error)) setHidden(true);
      else dispatch({ type: 'error', code: inviteErrorCode(error) });
    }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [api]);

  const active = ['offer', 'accept', 'reveal', 'sas'].includes(state.step);
  const remaining = secondsRemaining(state.expiresAt, now);
  useEffect(() => {
    if (!active) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [active]);
  useEffect(() => {
    if (active && remaining === 0) {
      dispatch({ type: 'expire' });
      setOutgoing('');
      setInput('');
      setTypedSas('');
      setSheetOpen(false);
    }
  }, [active, remaining]);

  const run = async (action: () => Promise<void>) => {
    if (busy) return;
    setBusy(true);
    try { await action(); }
    catch (error) {
      if (isInviteUnavailable(error)) setHidden(true);
      else dispatch({ type: 'error', code: inviteErrorCode(error) });
    } finally { setBusy(false); }
  };
  const start = () => run(async () => {
    const result = await api.createOffer();
    setRole('host');
    dispatch({ type: 'start_offer', id: result.id, expiresAt: Date.parse(result.expires_at) });
    setOutgoing(result.code);
    setInput('');
    setSheetOpen(true);
  });
  const importOffer = () => run(async () => {
    const result = await api.importOffer(input.trim());
    setRole('joining');
    setPeer(`${result.identity.profile} · ${result.identity.installation_id}`);
    dispatch({ type: 'import_offer', id: result.id, expiresAt: Date.parse(result.expires_at) });
    setOutgoing(result.code);
    setInput('');
    setSheetOpen(true);
  });
  const accept = () => run(async () => {
    if (!state.id) return;
    if (role === 'host') {
      const result = await api.acceptOffer(state.id, input.trim());
      dispatch({ type: 'accept', sas: result.sas });
      setOutgoing(result.code);
      setSheetOpen(true);
    } else {
      const result = await api.revealImport(state.id, input.trim());
      dispatch({ type: 'reveal', sas: result.sas });
      setOutgoing('');
    }
    setInput('');
  });
  const confirm = () => run(async () => {
    if (!state.id || !canConfirmSas(typedSas) || remaining === 0) return;
    const result = role === 'host'
      ? await api.confirmOffer(state.id, typedSas)
      : await api.confirmImport(state.id, typedSas);
    dispatch({ type: 'confirm' });
    setTypedSas('');
    setOutgoing('');
    setRows((current) => [...current.filter((row) => row.id !== result.id), result]);
  });
  const cancel = () => run(async () => {
    if (role === 'host' && state.id) await api.cancelOffer(state.id);
    dispatch({ type: 'cancel' });
    setInput('');
    setOutgoing('');
    setTypedSas('');
  });
  const copy = () => run(async () => {
    await navigator.clipboard.writeText(outgoing);
    setCopied(true);
  });

  if (hidden) return null;
  const codeControl = outgoing ? (
    <Button className="w-full" icon={<Copy size={16} />} onClick={() => { setCopied(false); setSheetOpen(true); }}>
      {t('inviteUi.showCode')}
    </Button>
  ) : null;
  const exchange = (
    <div className="grid gap-3">
      {peer ? <p className="break-words text-sm text-text">{peer}</p> : null}
      {codeControl}
      <label className="grid gap-2 text-sm text-text-muted">
        {t(role === 'host' ? 'inviteUi.pasteAccept' : 'inviteUi.pasteReveal')}
        <textarea className="mc-input min-h-24 w-full" autoComplete="off" value={input}
          onChange={(event) => setInput(event.target.value)} spellCheck={false} />
      </label>
      <Button variant="primary" loading={busy} disabled={!input.trim()} onClick={() => void accept()}>
        {t('inviteUi.continue')}
      </Button>
    </div>
  );
  const sasControl = (
    <div className="grid gap-4">
      <p className="text-sm text-text-muted">{t('inviteUi.sasHint')}</p>
      <div>
        <p className="text-xs text-text-muted">{t('inviteUi.yourSas')}</p>
        <output className="mt-1 block font-mono text-3xl font-semibold tracking-widest text-text">{state.sas}</output>
      </div>
      <label className="grid gap-2 text-sm text-text-muted">
        {t('inviteUi.otherSas')}
        <input className="mc-input min-h-11 w-full font-mono text-xl tracking-widest" inputMode="numeric"
          pattern="[0-9]{6}" maxLength={6} autoComplete="off" value={typedSas}
          onChange={(event) => setTypedSas(event.target.value)} />
      </label>
      <p className="invite-warning">{t('inviteUi.securityHint')}</p>
      <Button variant="primary" loading={busy} disabled={!canConfirmSas(typedSas)} onClick={() => void confirm()}>
        {t('inviteUi.confirmPairing')}
      </Button>
    </div>
  );
  return (
    <section className="invite-ui grid gap-5" aria-labelledby="pairing-title">
      <header>
        <p className="eyebrow">{t('inviteUi.title')}</p>
        <h2 id="pairing-title" className="mt-1 text-xl font-semibold text-text">{t('inviteUi.pairTitle')}</h2>
        <p className="mt-2 max-w-2xl text-sm leading-relaxed text-text-muted">{t('inviteUi.pairLead')}</p>
      </header>
      {state.error ? <p className="invite-error" role="alert">{t('inviteUi.error')} ({state.error})</p> : null}
      <div className="grid items-start gap-5 lg:grid-cols-2">
        <div className="invite-card">
          {state.step === 'start' ? (
            <div className="grid gap-4">
              <h3 className="text-base font-semibold text-text">{t('inviteUi.newPair')}</h3>
              <Button variant="primary" loading={busy} onClick={() => void start()}>{t('inviteUi.start')}</Button>
              <label className="grid gap-2 text-sm text-text-muted">
                {t('inviteUi.haveCode')}
                <input className="mc-input min-h-11 w-full" autoComplete="off" spellCheck={false}
                  value={input} onChange={(event) => setInput(event.target.value)} />
              </label>
              <Button loading={busy} disabled={!input.trim()} onClick={() => void importOffer()}>{t('inviteUi.useCode')}</Button>
              <p className="text-xs leading-relaxed text-text-muted">{t('inviteUi.securityHint')}</p>
            </div>
          ) : active ? (
            <div className="grid gap-4">
              <p className="text-xs text-warning" role="timer">{t('inviteUi.expires', { seconds: remaining })}</p>
              <Stepper active={state.step} steps={[
                { id: role === 'host' ? 'offer' : 'accept', label: t('inviteUi.exchange'), content: exchange },
                { id: 'reveal', label: t('inviteUi.sendReveal'), content: (
                  <div className="grid gap-3">
                    {codeControl}
                    <Button onClick={() => dispatch({ type: 'reveal', sas: state.sas! })}>{t('inviteUi.codeSent')}</Button>
                  </div>
                ) },
                { id: 'sas', label: t('inviteUi.compare'), content: sasControl },
              ]} />
              <Button variant="ghost" disabled={busy} onClick={() => void cancel()}>{t('inviteUi.cancel')}</Button>
            </div>
          ) : (
            <div className="grid gap-4" role="status">
              <p className="text-sm text-text">{t(`inviteUi.pairResult.${state.step}`)}</p>
              <Button onClick={() => dispatch({ type: 'reset' })}>{t('inviteUi.newPair')}</Button>
            </div>
          )}
        </div>
        {loading ? <p role="status">{t('inviteUi.loading')}</p> : (
          <PeerList rows={rows} busy={busy} onRemove={(id) => void run(async () => {
            await api.removePeer(id);
            setRows((current) => current.filter((row) => row.peer?.peer_id !== id));
          })} />
        )}
      </div>
      <BottomSheet open={sheetOpen} title={t('inviteUi.oneTimeCode')} onClose={() => setSheetOpen(false)}>
        <div className="grid gap-4">
          <p className="text-sm text-text-muted">{t('inviteUi.codeHint')}</p>
          <code className="invite-code">{outgoing}</code>
          <p className="text-xs text-warning">{t('inviteUi.expires', { seconds: remaining })}</p>
          <Button variant="primary" className="w-full" icon={<Copy size={18} />} onClick={() => void copy()}>
            {t(copied ? 'inviteUi.copied' : 'inviteUi.copy')}
          </Button>
        </div>
      </BottomSheet>
    </section>
  );
}
