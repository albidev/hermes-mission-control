import { useEffect, useMemo, useRef, useState } from 'react';
import { Check, Clipboard, Copy, Loader2, RefreshCw, ShieldAlert, X } from 'lucide-react';
import { useI18n } from '../../lib/i18n';
import { createRoomInviteApi, type RoomInviteApi, type RoomInviteBinding, type RoomInviteIssue, type RoomInviteMember } from '../../lib/room-invite-api';
import { useRoomInvite } from '../../lib/use-room-invite';
import { InviteLobbyCreateForm } from './InviteLobbyCreateForm';

type RoomInvitePanelProps = { lobbyId: string | null; accessToken?: string; api?: RoomInviteApi; initialName?: string; embedded?: boolean; onBusyChange?: (busy: boolean) => void; onLobbyCreated?: (lobbyId: string) => void; onRoomActivated?: (roomId: string) => void; onClose?: () => void };

function stateLabel(value: string, t: (key: string) => string) {
  if (value === 'approved' || value === 'given') return t('rooms.invites.state.approved');
  if (value === 'rejected') return t('rooms.invites.state.rejected');
  return t('rooms.invites.state.pending');
}

function persistLobbyId(lobbyId: string) {
  const url = new URL(window.location.href);
  url.searchParams.set('lobbyId', lobbyId);
  window.history.replaceState(window.history.state, '', url);
}

export function RoomInvitePanel({ lobbyId, accessToken, api, initialName, embedded, onBusyChange, onLobbyCreated, onRoomActivated, onClose }: RoomInvitePanelProps) {
  const { t } = useI18n();
  const [createdLobbyId, setCreatedLobbyId] = useState<string | null>(null);
  const activeLobbyId = lobbyId ?? createdLobbyId;
  const state = useRoomInvite(activeLobbyId, accessToken, api);
  const inviteApi = useMemo(() => api ?? createRoomInviteApi(accessToken), [accessToken, api]);
  const [issue, setIssue] = useState<RoomInviteIssue | null>(null);
  const [busy, setBusy] = useState(false);
  const [inviteeLabel, setInviteeLabel] = useState('');
  const [confirmActivate, setConfirmActivate] = useState(false);
  const [copyState, setCopyState] = useState<'idle' | 'copied' | 'failed'>('idle');
  const [historyApproval, setHistoryApproval] = useState<{ scope: object; peer: string; result: 'approved' | 'failed' } | null>(null);
  const [historyPending, setHistoryPending] = useState(false);
  const issueRef = useRef<RoomInviteIssue | null>(null);
  issueRef.current = issue;
  const mountedRef = useRef(true);
  const scopeRef = useRef<{ lobbyId: string | null; accessToken?: string; api: RoomInviteApi }>({ lobbyId: activeLobbyId, accessToken, api: inviteApi });
  const operationRef = useRef(0);
  const issueScopeRef = useRef(0);
  const busyScopeRef = useRef(-1);
  if (scopeRef.current.lobbyId !== activeLobbyId || scopeRef.current.accessToken !== accessToken || scopeRef.current.api !== inviteApi) {
    scopeRef.current = { lobbyId: activeLobbyId, accessToken, api: inviteApi };
    operationRef.current += 1;
    issueScopeRef.current = -1;
  }
  useEffect(() => () => { mountedRef.current = false; operationRef.current += 1; }, []);
  const visibleIssue = issueScopeRef.current === operationRef.current ? issue : null;
  const visibleBusy = busyScopeRef.current === operationRef.current && busy;
  const remoteMembers = useMemo(() => state.lobby?.roster.filter((member) => Boolean(member.target)) ?? [], [state.lobby?.roster]);

  if (!activeLobbyId) return <InviteLobbyCreateForm api={inviteApi} initialName={initialName} embedded={embedded} onBusyChange={onBusyChange} onCreated={(id) => { persistLobbyId(id); setCreatedLobbyId(id); onLobbyCreated?.(id); }} />;
  if (state.status === 'loading' || state.status === 'idle') return <div className="flex items-center gap-2 p-4 text-sm text-text-muted"><Loader2 size={15} className="animate-spin" />{t('rooms.invites.loading')}</div>;
  if (state.status === 'empty') return <div className="rounded-lg border border-border-subtle p-4 text-sm text-text-muted" role="status">{t('rooms.invites.empty')}</div>;
  if (state.status === 'error' && !state.lobby) return <div className="rounded-lg border border-negative/30 bg-negative-subtle p-4 text-sm text-negative" role="alert">{state.error?.kind === 'unavailable' ? t('rooms.invites.unavailable') : `${t('rooms.invites.error')} (${state.error?.code})`} <button type="button" className="ml-2 underline" onClick={() => void state.reload()}>{t('rooms.invites.retry')}</button></div>;
  const lobby = state.lobby;
  if (!lobby) return null;
  const approveHistory = async (peerId: string) => {
    if (!lobby.planned_room_id || historyPending) return;
    const scope = scopeRef.current;
    setHistoryPending(true);
    try {
      const result = await inviteApi.approveHistory(lobby.planned_room_id, peerId);
      if (result.status !== 'owner_approved') throw new Error('history_approval_unconfirmed');
      if (mountedRef.current && scopeRef.current === scope) setHistoryApproval({ scope, peer: peerId, result: 'approved' });
    } catch {
      if (mountedRef.current && scopeRef.current === scope) setHistoryApproval({ scope, peer: peerId, result: 'failed' });
    } finally { if (mountedRef.current && scopeRef.current === scope) setHistoryPending(false); }
  };
  const issueInvite = async (member: RoomInviteMember) => {
    if (!inviteeLabel.trim()) return;
    const operation = ++operationRef.current;
    issueScopeRef.current = -1;
    setIssue(null);
    setCopyState('idle');
    busyScopeRef.current = operation;
    setBusy(true);
    try {
      const issued = await state.issueInvite({ member_id: member.member_id, invitee_label: inviteeLabel.trim() });
      if (mountedRef.current && operationRef.current === operation) {
        issueScopeRef.current = operation;
        setIssue(issued);
      }
    } catch { /* hook exposes the durable error */ } finally { if (mountedRef.current && operationRef.current === operation) { busyScopeRef.current = -1; setBusy(false); } }
  };
  const copyInvite = async () => {
    const currentIssue = visibleIssue;
    if (!currentIssue?.invite_url) return;
    const operation = operationRef.current;
    let nextState: 'copied' | 'failed' = 'failed';
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(currentIssue.invite_url);
        nextState = 'copied';
      } else {
        throw new Error('clipboard_unavailable');
      }
    } catch {
      // The browser write may have been started before a scope change and
      // cannot be cancelled. Never start a second, legacy write with stale
      // material after that scope has been invalidated.
      if (!mountedRef.current || operationRef.current !== operation || issueScopeRef.current !== operation || issueRef.current !== currentIssue) return;
      const textarea = document.createElement('textarea');
      textarea.value = currentIssue.invite_url;
      textarea.setAttribute('readonly', '');
      textarea.style.position = 'fixed'; textarea.style.opacity = '0';
      try {
        document.body.appendChild(textarea);
        textarea.select();
        nextState = document.execCommand('copy') ? 'copied' : 'failed';
      } catch {
        nextState = 'failed';
      } finally {
        textarea.remove();
      }
    } finally {
      if (mountedRef.current && operationRef.current === operation && issueScopeRef.current === operation) setCopyState(nextState);
    }
  };
  const activate = async () => { if (!confirmActivate) { setConfirmActivate(true); return; } setConfirmActivate(false); setBusy(true); try { const result = await state.activate(); onRoomActivated?.(result.room_id); } catch { /* hook exposes the durable error */ } finally { setBusy(false); } };
  const renderBinding = (binding: RoomInviteBinding) => <dl className="mt-2 grid gap-1 text-[11px]"><div><dt className="inline font-semibold">fingerprint: </dt><dd className="inline break-all">{binding.fingerprint}</dd></div><div><dt className="inline font-semibold">target_install_id: </dt><dd className="inline break-all">{binding.target_install_id}</dd></div><div><dt className="inline font-semibold">target_profile: </dt><dd className="inline">{binding.target_profile}</dd></div><div><dt className="inline font-semibold">catalog_digest: </dt><dd className="inline break-all">{binding.catalog_digest}</dd></div><div><dt className="inline font-semibold">policy_digest: </dt><dd className="inline break-all">{binding.policy_digest}</dd></div></dl>;

  return <section className="invite-panel flex min-h-0 min-w-0 max-w-full flex-col gap-3 overflow-x-hidden overflow-y-auto rounded-xl border border-border-subtle bg-surface p-4" aria-label={t('rooms.invites.title')}>
    <header className="flex min-w-0 items-start justify-between gap-3"><div className="min-w-0 flex-1"><p className="eyebrow">{t('rooms.invites.eyebrow')}</p><h3 className="invite-lobby-title text-sm font-semibold text-text">{lobby.name}</h3></div><div className="flex shrink-0 gap-1"><button type="button" className="chat-control chat-icon-button" aria-label={t('rooms.invites.refresh')} disabled={state.isMutating} onClick={() => void state.reload()}><RefreshCw size={15} /></button>{onClose ? <button type="button" className="chat-control chat-icon-button" aria-label={t('rooms.invites.close')} onClick={onClose}><X size={15} /></button> : null}</div></header>
    <div className="grid gap-2 text-xs"><strong>{t('rooms.invites.roster')}</strong><ul className="space-y-1">{lobby.roster.map((member: RoomInviteMember) => <li key={member.member_id}>{member.display_name || member.handle} · {member.profile || t('rooms.invites.localProfile')}</li>)}</ul></div>
    {remoteMembers.map((peer) => { const status = state.activation?.peers.find((entry) => entry.member_id === peer.member_id); const binding = status?.redeemed_binding; const inviteId = status?.invite_id; return <div key={peer.member_id} className="grid gap-2 rounded-lg border border-border-subtle p-2 text-xs"><strong>{peer.display_name || peer.handle}</strong><div>{t('rooms.invites.profile')}: {peer.target?.profile || peer.profile || '—'}</div><div>{t('rooms.invites.capability')}: {peer.target?.capability_digest || t('rooms.invites.redactedOrUnavailable')}</div><div><strong>{t('rooms.invites.hostDecision')}</strong>: {stateLabel(status?.host_decision || 'pending', t)}</div>{binding ? renderBinding(binding) : <div className="text-text-muted">{t('rooms.invites.waitingRedemption')}</div>}{binding && inviteId ? <div className="flex gap-1"><button type="button" className="chat-control" disabled={visibleBusy || state.isMutating} onClick={() => void state.decideHost(inviteId, peer.target?.peer_id || '', true, binding)}><Check size={13} /> {t('rooms.invites.approve')}</button><button type="button" className="chat-control" disabled={visibleBusy || state.isMutating} onClick={() => void state.decideHost(inviteId, peer.target?.peer_id || '', false, binding)}>{t('rooms.invites.reject')}</button><button type="button" className="chat-control" disabled={visibleBusy || state.isMutating} onClick={() => void state.revokeInvite(inviteId)}>{t('rooms.invites.revoke')}</button></div> : null}</div>; })}
    <div className="flex gap-2"><input className="mc-input h-8 min-w-0 flex-1 text-xs" value={inviteeLabel} onChange={(event) => setInviteeLabel(event.target.value)} placeholder={t('rooms.invites.inviteePlaceholder')} aria-label={t('rooms.invites.inviteeLabel')} /><button type="button" className="chat-control" disabled={visibleBusy || state.isMutating || !inviteeLabel.trim() || remoteMembers.length === 0} onClick={() => { const peer = remoteMembers[0]; if (peer) void issueInvite(peer); }}>{visibleBusy ? <Loader2 size={13} className="animate-spin" /> : <Copy size={13} />} {t('rooms.invites.issue')}</button></div>
    {visibleIssue ? <div className="invite-issue min-w-0 rounded-lg border border-warning/40 bg-warning-subtle p-2 text-xs"><div className="flex items-center justify-between gap-2"><strong>{t('rooms.invites.secretOnce')}</strong><button type="button" className="min-h-11 underline" onClick={() => { setIssue(null); issueScopeRef.current = -1; operationRef.current += 1; setCopyState('idle'); }}>{t('rooms.invites.closeSecret')}</button></div><code className="invite-code mt-2 block max-w-full select-all break-all">{visibleIssue.invite_url}</code><button type="button" className="chat-control chat-text-action mt-2 w-full justify-center" onClick={() => void copyInvite()}><Clipboard size={14} /> {copyState === 'copied' ? t('rooms.invites.copied') : copyState === 'failed' ? t('rooms.invites.copyFailed') : t('rooms.invites.copyLink')}</button></div> : null}
    {state.error ? <div className="flex items-start gap-2 rounded-lg border border-warning/30 bg-warning-subtle p-2 text-xs text-warning" role="alert"><ShieldAlert size={14} />{state.error.code === 'invite_origin_unavailable' ? t('rooms.invites.originUnavailable') : state.error.code === 'invite_response_incompatible' ? t('rooms.invites.compatibilityError') : state.error.code === 'binding_changed' ? t('rooms.invites.bindingChanged') : state.error.code === 'activation_failed' ? t('rooms.invites.activationFailed') : state.error.kind === 'ambiguous' ? t('rooms.invites.ambiguous') : t('rooms.invites.blocked')}<code>{state.error.code}</code>{state.error.kind === 'ambiguous' ? <button type="button" className="ml-auto underline" onClick={() => void state.refreshActivation()}>{t('rooms.invites.checkStatus')}</button> : null}</div> : null}
    {lobby.state === 'created' && lobby.history_enabled === true && lobby.planned_room_id ? <div className="grid min-w-0 gap-2 rounded-lg border border-border-subtle p-3 text-xs">
      <p>{t(lobby.history_policy === 'full' ? 'rooms.invites.history.onHint' : 'rooms.invites.history.offHint')}</p>
      {remoteMembers.map(peer => <div key={peer.member_id} className="grid min-w-0 gap-1">
        <span className="break-all">{peer.display_name || peer.handle}</span>
        <button type="button" className="chat-control chat-text-action" disabled={historyPending || !peer.target?.peer_id} onClick={() => void approveHistory(peer.target!.peer_id)}>{t('rooms.invites.history.approve')}</button>
        {historyApproval?.scope === scopeRef.current && historyApproval.peer === peer.target?.peer_id ? <p role={historyApproval.result === 'approved' ? 'status' : 'alert'}>{t(historyApproval.result === 'approved' ? 'rooms.invites.history.approved' : 'rooms.invites.history.failed')}</p> : null}
      </div>)}
    </div> : null}
    <button type="button" className={`chat-control ${confirmActivate ? 'is-confirm' : ''}`} disabled={visibleBusy || state.isMutating || state.activationNeedsRefresh || !state.activation?.ready} onClick={() => void activate()}>{visibleBusy ? <Loader2 size={13} className="animate-spin" /> : <Check size={13} />} {confirmActivate ? t('rooms.invites.confirmActivate') : t('rooms.invites.activate')}</button>
  </section>;
}