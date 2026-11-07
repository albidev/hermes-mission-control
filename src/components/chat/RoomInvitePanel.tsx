import { useMemo, useState } from 'react';
import { Check, Copy, Loader2, RefreshCw, ShieldAlert, X } from 'lucide-react';
import { useI18n } from '../../lib/i18n';
import { createRoomInviteApi, type RoomInviteApi, type RoomInviteBinding, type RoomInviteIssue, type RoomInviteMember } from '../../lib/room-invite-api';
import { useRoomInvite } from '../../lib/use-room-invite';
import { InviteLobbyCreateForm } from './InviteLobbyCreateForm';

type RoomInvitePanelProps = { lobbyId: string | null; accessToken?: string; api?: RoomInviteApi; initialName?: string; embedded?: boolean; onLobbyCreated?: (lobbyId: string) => void; onRoomActivated?: (roomId: string) => void; onClose?: () => void };

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

export function RoomInvitePanel({ lobbyId, accessToken, api, initialName, embedded, onLobbyCreated, onRoomActivated, onClose }: RoomInvitePanelProps) {
  const { t } = useI18n();
  const [createdLobbyId, setCreatedLobbyId] = useState<string | null>(null);
  const activeLobbyId = lobbyId ?? createdLobbyId;
  const state = useRoomInvite(activeLobbyId, accessToken, api);
  const inviteApi = useMemo(() => api ?? createRoomInviteApi(accessToken), [accessToken, api]);
  const [issue, setIssue] = useState<RoomInviteIssue | null>(null);
  const [busy, setBusy] = useState(false);
  const [inviteeLabel, setInviteeLabel] = useState('');
  const [confirmActivate, setConfirmActivate] = useState(false);
  const remoteMembers = useMemo(() => state.lobby?.roster.filter((member) => Boolean(member.target)) ?? [], [state.lobby?.roster]);

  if (!activeLobbyId) return <InviteLobbyCreateForm api={inviteApi} initialName={initialName} embedded={embedded} onCreated={(id) => { persistLobbyId(id); setCreatedLobbyId(id); onLobbyCreated?.(id); }} />;
  if (state.status === 'loading' || state.status === 'idle') return <div className="flex items-center gap-2 p-4 text-sm text-text-muted"><Loader2 size={15} className="animate-spin" />{t('rooms.invites.loading')}</div>;
  if (state.status === 'empty') return <div className="rounded-lg border border-border-subtle p-4 text-sm text-text-muted" role="status">{t('rooms.invites.empty')}</div>;
  if (state.status === 'error' && !state.lobby) return <div className="rounded-lg border border-negative/30 bg-negative-subtle p-4 text-sm text-negative" role="alert">{state.error?.kind === 'unavailable' ? t('rooms.invites.unavailable') : `${t('rooms.invites.error')} (${state.error?.code})`} <button type="button" className="ml-2 underline" onClick={() => void state.reload()}>{t('rooms.invites.retry')}</button></div>;
  const lobby = state.lobby;
  if (!lobby) return null;
  const issueInvite = async (member: RoomInviteMember) => { if (!inviteeLabel.trim()) return; setBusy(true); try { setIssue(await state.issueInvite({ member_id: member.member_id, invitee_label: inviteeLabel.trim() })); } finally { setBusy(false); } };
  const activate = async () => { if (!confirmActivate) { setConfirmActivate(true); return; } setConfirmActivate(false); setBusy(true); try { const result = await state.activate(); onRoomActivated?.(result.room_id); } catch { /* hook exposes the durable error */ } finally { setBusy(false); } };
  const renderBinding = (binding: RoomInviteBinding) => <dl className="mt-2 grid gap-1 text-[11px]"><div><dt className="inline font-semibold">fingerprint: </dt><dd className="inline break-all">{binding.fingerprint}</dd></div><div><dt className="inline font-semibold">target_install_id: </dt><dd className="inline break-all">{binding.target_install_id}</dd></div><div><dt className="inline font-semibold">target_profile: </dt><dd className="inline">{binding.target_profile}</dd></div><div><dt className="inline font-semibold">catalog_digest: </dt><dd className="inline break-all">{binding.catalog_digest}</dd></div><div><dt className="inline font-semibold">policy_digest: </dt><dd className="inline break-all">{binding.policy_digest}</dd></div></dl>;

  return <section className="flex min-h-0 flex-col gap-3 rounded-xl border border-border-subtle bg-surface p-4" aria-label={t('rooms.invites.title')}>
    <header className="flex items-start justify-between gap-3"><div><p className="eyebrow">{t('rooms.invites.eyebrow')}</p><h3 className="text-sm font-semibold text-text">{lobby.name}</h3></div><div className="flex gap-1"><button type="button" className="chat-control chat-icon-button" aria-label={t('rooms.invites.refresh')} disabled={state.isMutating} onClick={() => void state.reload()}><RefreshCw size={15} /></button>{onClose ? <button type="button" className="chat-control chat-icon-button" aria-label={t('rooms.invites.close')} onClick={onClose}><X size={15} /></button> : null}</div></header>
    <div className="grid gap-2 text-xs"><strong>{t('rooms.invites.roster')}</strong><ul className="space-y-1">{lobby.roster.map((member: RoomInviteMember) => <li key={member.member_id}>{member.display_name || member.handle} · {member.profile || t('rooms.invites.localProfile')}</li>)}</ul></div>
    {remoteMembers.map((peer) => { const status = state.activation?.peers.find((entry) => entry.member_id === peer.member_id); const binding = status?.redeemed_binding; const inviteId = status?.invite_id; return <div key={peer.member_id} className="grid gap-2 rounded-lg border border-border-subtle p-2 text-xs"><strong>{peer.display_name || peer.handle}</strong><div>{t('rooms.invites.profile')}: {peer.target?.profile || peer.profile || '—'}</div><div>{t('rooms.invites.capability')}: {peer.target?.capability_digest || t('rooms.invites.redactedOrUnavailable')}</div><div><strong>{t('rooms.invites.hostDecision')}</strong>: {stateLabel(status?.host_decision || 'pending', t)}</div>{binding ? renderBinding(binding) : <div className="text-text-muted">{t('rooms.invites.waitingRedemption')}</div>}{binding && inviteId ? <div className="flex gap-1"><button type="button" className="chat-control" disabled={busy || state.isMutating} onClick={() => void state.decideHost(inviteId, peer.target?.peer_id || '', true, binding)}><Check size={13} /> {t('rooms.invites.approve')}</button><button type="button" className="chat-control" disabled={busy || state.isMutating} onClick={() => void state.decideHost(inviteId, peer.target?.peer_id || '', false, binding)}>{t('rooms.invites.reject')}</button><button type="button" className="chat-control" disabled={busy || state.isMutating} onClick={() => void state.revokeInvite(inviteId)}>{t('rooms.invites.revoke')}</button></div> : null}</div>; })}
    <div className="flex gap-2"><input className="mc-input h-8 min-w-0 flex-1 text-xs" value={inviteeLabel} onChange={(event) => setInviteeLabel(event.target.value)} placeholder={t('rooms.invites.inviteePlaceholder')} aria-label={t('rooms.invites.inviteeLabel')} /><button type="button" className="chat-control" disabled={busy || state.isMutating || !inviteeLabel.trim() || remoteMembers.length === 0} onClick={() => { const peer = remoteMembers[0]; if (peer) void issueInvite(peer); }}>{busy ? <Loader2 size={13} className="animate-spin" /> : <Copy size={13} />} {t('rooms.invites.issue')}</button></div>
    {issue ? <div className="rounded-lg border border-warning/40 bg-warning-subtle p-2 text-xs"><div className="flex items-center justify-between"><strong>{t('rooms.invites.secretOnce')}</strong><button type="button" className="underline" onClick={() => setIssue(null)}>{t('rooms.invites.closeSecret')}</button></div><code className="mt-1 block break-all select-all">{issue.secret}</code></div> : null}
    {state.error ? <div className="flex items-center gap-2 rounded-lg border border-warning/30 bg-warning-subtle p-2 text-xs text-warning" role="alert"><ShieldAlert size={14} />{state.error.code === 'binding_changed' ? t('rooms.invites.bindingChanged') : state.error.code === 'activation_failed' ? t('rooms.invites.activationFailed') : state.error.kind === 'ambiguous' ? t('rooms.invites.ambiguous') : t('rooms.invites.blocked')} <code>{state.error.code}</code>{state.error.kind === 'ambiguous' ? <button type="button" className="ml-auto underline" onClick={() => void state.refreshActivation()}>{t('rooms.invites.checkStatus')}</button> : null}</div> : null}
    <button type="button" className={`chat-control ${confirmActivate ? 'is-confirm' : ''}`} disabled={busy || state.isMutating || state.activationNeedsRefresh || !state.activation?.ready} onClick={() => void activate()}>{busy ? <Loader2 size={13} className="animate-spin" /> : <Check size={13} />} {confirmActivate ? t('rooms.invites.confirmActivate') : t('rooms.invites.activate')}</button>
  </section>;
}