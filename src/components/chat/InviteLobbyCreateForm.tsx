import { useCallback, useEffect, useRef, useState } from 'react';
import { Check, Loader2 } from 'lucide-react';
import { useI18n } from '../../lib/i18n';
import { buildCreateLobbyRequest, isRoomsExtendedTtlEnabled, RoomInviteApiError, type RoomInviteApi, type RoomInviteError, type RoomInvitePairing } from '../../lib/room-invite-api';

type InviteLobbyCreateFormProps = { api: RoomInviteApi; initialName?: string; embedded?: boolean; onBusyChange?: (busy: boolean) => void; onCreated: (lobbyId: string) => void };
type PairingState = { status: 'loading' } | { status: 'ready'; pairing: RoomInvitePairing } | { status: 'error'; error: RoomInviteError };
type InviteTtl = 600 | 3600 | 28800 | 86400;

function asProblem(error: unknown): RoomInviteError {
  return error instanceof RoomInviteApiError ? error.problem : { kind: 'error', code: 'network_error' };
}

/** Owner-side creation of an invite-only lobby: this installation's owner profile plus one paired peer. */
export function InviteLobbyCreateForm({ api, initialName = '', embedded = false, onBusyChange, onCreated }: InviteLobbyCreateFormProps) {
  const { t } = useI18n();
  const [name, setName] = useState(initialName);
  const [selectedPeer, setSelectedPeer] = useState('');
  const [selectedTtl, setSelectedTtl] = useState<InviteTtl>(600);
  const extendedTtl = isRoomsExtendedTtlEnabled();
  const [pairing, setPairing] = useState<PairingState>({ status: 'loading' });
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);
  const [lastSentTtl, setLastSentTtl] = useState<InviteTtl | undefined>(undefined);
  const [createError, setCreateError] = useState<RoomInviteError | null>(null);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    setPairing({ status: 'loading' });
    api.getPairing(controller.signal).then(
      (result) => { if (!controller.signal.aborted) setPairing({ status: 'ready', pairing: result }); },
      (error) => { if (!controller.signal.aborted) setPairing({ status: 'error', error: asProblem(error) }); },
    );
    return () => controller.abort();
  }, [api, attempt]);

  const ready = pairing.status === 'ready' ? pairing.pairing : null;
  // A sidecar older than this UI omits owner_profile (Vite hot-reloads before the sidecar restarts).
  const ownerProfile = ready?.owner_profile || null;
  const peer = ready?.peers.find((candidate) => candidate.peer_id === selectedPeer) ?? null;

  const create = useCallback(async () => {
    if (busyRef.current || !ownerProfile || !peer || !name.trim()) return;
    busyRef.current = true;
    setBusy(true);
    onBusyChange?.(true);
    setCreateError(null);
    const sentTtl = extendedTtl ? selectedTtl : undefined;
    setLastSentTtl(sentTtl);
    try {
      const lobby = await api.createLobby(buildCreateLobbyRequest(name, ownerProfile, peer, sentTtl));
      onCreated(lobby.lobby_id);
    } catch (error) {
      setCreateError(asProblem(error));
    } finally {
      busyRef.current = false;
      setBusy(false);
      onBusyChange?.(false);
    }
  }, [api, extendedTtl, name, onBusyChange, onCreated, ownerProfile, peer, selectedTtl]);

  return <section className={embedded ? 'grid gap-3 text-sm' : 'grid gap-3 rounded-lg border border-border-subtle p-4 text-sm'} aria-label={t('rooms.invites.title')}>
    {embedded ? null : <div><p className="eyebrow">{t('rooms.invites.eyebrow')}</p><h3 className="font-semibold text-text">{t('rooms.invites.createTitle')}</h3></div>}
    <label className="flex flex-col gap-1.5 text-xs text-text-muted">
      <span>{t('rooms.invites.name')}</span>
      <input className="mc-input h-9" value={name} onChange={(event) => setName(event.target.value)} placeholder={t('rooms.invites.namePlaceholder')} aria-label={t('rooms.invites.name')} />
    </label>
    <div className="flex flex-col gap-1.5 text-xs text-text-muted">
      <span>{t('rooms.invites.owner')}</span>
      <span className="font-medium text-text">{ownerProfile ? `@${ownerProfile}` : ready ? t('rooms.invites.ownerUnknown') : '—'}</span>
    </div>
    <div className="flex flex-col gap-1.5 text-xs text-text-muted">
      <span>{t('rooms.invites.pairedPeer')}</span>
      {pairing.status === 'loading'
        ? <div className="flex items-center gap-2"><Loader2 size={13} className="animate-spin" />{t('rooms.invites.loadingPeers')}</div>
        : pairing.status === 'error'
          ? <div className="rounded-lg border border-negative/30 bg-negative-subtle p-2 text-negative" role="alert">{pairing.error.kind === 'unavailable' ? t('rooms.invites.unavailable') : t('rooms.invites.peerLoadError')} <button type="button" className="ml-2 underline" onClick={() => setAttempt((value) => value + 1)}>{t('rooms.invites.retry')}</button></div>
          : pairing.pairing.peers.length === 0
            ? <div className="rounded-lg border border-warning/40 bg-warning-subtle p-2 text-warning" role="status">{t('rooms.invites.noPairedPeer')}</div>
            : <select className="mc-input h-9" value={selectedPeer} onChange={(event) => setSelectedPeer(event.target.value)} aria-label={t('rooms.invites.pairedPeer')}><option value="">{t('rooms.invites.choosePeer')}</option>{pairing.pairing.peers.map((candidate) => <option key={candidate.peer_id} value={candidate.peer_id}>{candidate.profile} · {candidate.peer_id}</option>)}</select>}
    </div>
    <div className="flex flex-col gap-1.5 text-xs text-text-muted">
      <span>{t('rooms.invites.ttl.label')}</span>
      {extendedTtl ? <div role="radiogroup" aria-label={t('rooms.invites.ttl.label')} className="room-create-kind-options">
        {([600, 3600, 28800, 86400] as const).map((seconds) => {
          const label = seconds === 600 ? t('rooms.invites.ttl.default') : seconds === 3600 ? t('rooms.invites.ttl.oneHour') : seconds === 28800 ? t('rooms.invites.ttl.eightHours') : t('rooms.invites.ttl.oneDay');
          return <button key={seconds} type="button" role="radio" disabled={busy} aria-checked={selectedTtl === seconds} tabIndex={selectedTtl === seconds ? 0 : -1}
            onClick={() => setSelectedTtl(seconds)}
            onKeyDown={(event) => {
              if (!['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End'].includes(event.key)) return;
              event.preventDefault();
              const options = [600, 3600, 28800, 86400] as const;
              const index = options.indexOf(selectedTtl);
              const nextIndex = event.key === 'Home' ? 0 : event.key === 'End' ? options.length - 1 : (index + (event.key === 'ArrowLeft' || event.key === 'ArrowUp' ? -1 : 1) + options.length) % options.length;
              setSelectedTtl(options[nextIndex]);
              (event.currentTarget.parentElement?.children[nextIndex] as HTMLButtonElement | undefined)?.focus();
            }}>{label}</button>;
        })}
      </div> : <span className="font-medium text-text">{t('rooms.invites.ttl.default')}</span>}
      <span className="text-xs text-text-muted">{t('rooms.invites.ttl.helper')}</span>
    </div>
    {createError ? <p className="text-xs text-negative" role="alert">{extendedTtl && lastSentTtl !== undefined && lastSentTtl !== 600 && createError.status === 400 && createError.code === 'invalid_lobby' ? t('rooms.invites.ttl.extendedRejected') : <>{t('rooms.invites.createError')} <code>{createError.code}</code></>}</p> : null}
    <button type="button" className="inline-flex items-center justify-center gap-1.5 rounded-lg bg-accent px-3 py-2 text-sm text-white disabled:opacity-50" disabled={busy || !name.trim() || !peer || !ownerProfile} onClick={() => void create()}>
      {busy ? <Loader2 size={14} className="animate-spin" /> : <Check size={14} />}{t('rooms.invites.create')}
    </button>
  </section>;
}
