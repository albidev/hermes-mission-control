import { memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import {AlertTriangle, Check, ChevronRight, Loader2, Plus, RefreshCw, Send, ShieldAlert, Trash2, Users, X, XCircle, XOctagon, ChevronDown, EyeOff} from 'lucide-react';
import { useI18n } from '../../lib/i18n';
import { RoomTranscriptRow } from './RoomTranscriptRow';
import { loadMissionControlVaults, type MissionControlVaultDescriptor } from '../../lib/hermes-api';
import { loadRoomTools, type RoomToolTrace } from '../../lib/room-tools';
import { RoomToolStrip } from './RoomToolPanel';
import type { GroupEvent } from '../../lib/group-gateway';
import type { GroupRoomResult } from '../../lib/use-group-room';
import { ChatMentionPopover, type ChatMentionPopoverHandle } from '../ChatMentionPopover';
import type { BotMentionCandidate } from '../../lib/bot-mentions';
import { deriveGroupMemberStatus, groupEventToChatMessage, visibleGroupEvents } from './group-room-view-model';
import type { GroupMemberStatus } from './group-room-view-model';
export { deriveGroupMemberStatus, groupEventLabel, visibleGroupEvents } from './group-room-view-model';

type GroupRoomViewProps = {
  state: GroupRoomResult;
  onSend?: (text: string) => Promise<unknown>;
  className?: string;
  mentionRoster?: BotMentionCandidate[];
  onNearBottomChange?: (nearBottom: boolean) => void;
};


const statusKey: Record<GroupMemberStatus, string> = {
  idle: 'rooms.status.idle',
  working: 'rooms.status.working',
  settled: 'rooms.status.settled',
  unavailable: 'rooms.status.unavailable',
};
function MemberFilterStatus({ status }: { status: GroupMemberStatus }) {
  const { t } = useI18n();
  return <span className={`chat-room-member-status is-${status}`}>
    {status === 'working' ? <Loader2 size={12} className="animate-spin" aria-hidden="true" /> : <span className="chat-room-member-dot" aria-hidden="true" />}
    <span>{t(statusKey[status])}</span>
  </span>;
}

function notEmptyString(value: unknown): value is string {
  return typeof value === 'string' && value.trim().length > 0;
}

function stringField(value: unknown): string {
  return typeof value === 'string' ? value : '';
}

type PendingAction = { kind?: unknown; task_id?: unknown; request_id?: unknown; member_id?: unknown; taskId?: unknown; execution_generation?: unknown; choice?: unknown };

function ActionCard({ action, state, busy, onRun }: { action: PendingAction; state: GroupRoomResult; busy: boolean; onRun: (label: string, fn: () => Promise<unknown>) => void }) {
  const { t } = useI18n();
  const taskId = stringField(action.taskId) || stringField(action.task_id);
  const requestId = stringField(action.request_id);
  const memberId = stringField(action.member_id);
  const executionGeneration = typeof action.execution_generation === 'number' ? action.execution_generation : 0;
  const member = state.room?.members.find((m) => m.id === memberId);
  const label = member?.displayName || (member ? `@${member.handle}` : memberId || t('rooms.member'));

  if (action.kind === 'retry' || taskId) {
    return (
      <div className="flex items-center justify-between gap-2 rounded-lg border border-warning/30 bg-warning-subtle/60 p-2 text-xs text-warning">
        <span className="inline-flex min-w-0 items-center gap-1.5"><RefreshCw size={12} className="shrink-0" /><span className="truncate">{t('rooms.retryingTask')}{label ? ` · ${label}` : ''}</span></span>
        <button type="button" disabled={busy} className="shrink-0 rounded bg-warning/20 px-2 py-1 text-xs hover:bg-warning/30 disabled:opacity-50" onClick={() => void onRun('retry-action', () => state.retryMember(taskId || null))}>
          {busy ? <Loader2 size={12} className="animate-spin" /> : <RefreshCw size={12} className="inline" />} {t('rooms.retry')}
        </button>
      </div>
    );
  }
  return (
    <div className="flex items-center justify-between gap-2 rounded-lg border border-warning/30 bg-warning-subtle/60 p-2 text-xs text-warning">
      <span className="inline-flex min-w-0 items-center gap-1.5"><AlertTriangle size={12} className="shrink-0" /><span className="truncate">{t('rooms.approveRequest')}{label ? ` · ${label}` : ''}</span></span>
      <button type="button" disabled={busy} className="shrink-0 rounded bg-warning/20 px-2 py-1 text-xs hover:bg-warning/30 disabled:opacity-50" onClick={() => void onRun('approve-action', () => state.approve({ requestId, memberId, taskId, executionGeneration, choice: 'once' }))}>
        {busy ? <Loader2 size={12} className="animate-spin" /> : <Check size={12} className="inline" />} {t('rooms.approve')}
      </button>
    </div>
  );
}

function StateNotice({ state }: { state: GroupRoomResult }) {
  const { t } = useI18n();
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  const runAction = useCallback(async (label: string, fn: () => Promise<unknown>) => {
    setBusyAction(label); setErrorMsg(null);
    try { await fn(); } catch (cause) { setErrorMsg(cause instanceof Error ? cause.message : String(cause)); }
    finally { setBusyAction(null); }
  }, []);

  const canAct = Boolean(state.selectedRoomId) && !state.disbanded && !state.serviceUnavailable;
  if (state.serviceUnavailable) return <div className="rounded-lg border border-negative/30 bg-negative-subtle p-3 text-sm text-negative" role="alert"><XCircle size={16} className="mr-2 inline" />{t('rooms.serviceUnavailable')} <button type="button" className="underline" onClick={() => void state.retry()}>{t('rooms.retry')}</button></div>;
  if (state.authorityChanged) return <div className="rounded-lg border border-warning/30 bg-warning-subtle p-3 text-sm text-warning" role="status"><ShieldAlert size={16} className="mr-2 inline" />{t('rooms.authorityConflict')}</div>;
  if (state.disbanded) return <div className="rounded-lg border border-border-subtle bg-surface-sunken/50 p-3 text-sm text-text-muted" role="status"><XCircle size={16} className="mr-2 inline" />{t('rooms.disbanded')}</div>;

  const actions = Array.isArray(state.pendingActions) ? state.pendingActions as PendingAction[] : [];
  const pendingOrBlocked = Boolean(state.approval) || state.blocked || actions.length > 0;

  return (
    <div className="flex flex-col gap-2">
      {state.working && !pendingOrBlocked && actions.length === 0 ? (
        <div className="flex items-center gap-2 rounded-lg border border-accent/25 bg-accent-subtle/60 p-2 text-xs text-accent" role="status">
          <Loader2 size={13} className="animate-spin shrink-0" />
          <span>{t('rooms.status.working')}</span>
        </div>
      ) : null}
      {pendingOrBlocked && !(state.blocked && canAct) && actions.length === 0 ? (
        <div className="flex items-center gap-2 rounded-lg border border-warning/30 bg-warning-subtle p-2 text-xs text-warning" role="status"><AlertTriangle size={13} className="shrink-0" />{t('rooms.pendingApprovalOrRetry')}</div>
      ) : null}
      {actions.map((action, index) => <ActionCard key={`${stringField(action.request_id)}-${index}`} action={action} state={state} busy={busyAction === `action-${index}`} onRun={(label, fn) => runAction(label, fn)} />)}
      {errorMsg ? <div className="rounded-lg border border-negative/30 bg-negative-subtle p-2 text-xs text-negative" role="alert">{errorMsg}</div> : null}
    </div>
  );
}

export function GroupRoomComposer({ state, onSend, mentionRoster }: { state: GroupRoomResult; onSend: (text: string) => Promise<unknown>; mentionRoster: BotMentionCandidate[] }) {
  const { t } = useI18n();
  const [draft, setDraft] = useState('');
  const [confirmDisband, setConfirmDisband] = useState(false);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const mentionPopoverRef = useRef<ChatMentionPopoverHandle | null>(null);

  useEffect(() => {
    const textarea = textareaRef.current;
    if (!textarea) return;
    textarea.style.height = 'auto';
    textarea.style.height = `${Math.min(textarea.scrollHeight, 160)}px`;
  }, [draft]);

  const submitDraft = useCallback(async () => {
    const text = draft.trim();
    if (!text || state.disbanded || state.serviceUnavailable) return;
    setDraft('');
    await onSend(text);
  }, [draft, onSend, state.disbanded, state.serviceUnavailable]);

  const submit = useCallback((event: React.FormEvent) => {
    event.preventDefault();
    void submitDraft();
  }, [submitDraft]);

  const handleKeyDown = useCallback((event: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (mentionPopoverRef.current?.handleKey(event)) return;
    if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      void submitDraft();
    }
  }, [submitDraft]);

  return (
    <>
      <ChatMentionPopover
        ref={mentionPopoverRef}
        input={draft}
        roster={mentionRoster}
        textareaRef={textareaRef}
        onApply={setDraft}
      />
      <form onSubmit={submit} className="chat-composer group-room-composer shrink-0">
        <div className="chat-composer-main">
          <textarea
            ref={textareaRef}
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={handleKeyDown}
            disabled={state.disbanded || state.serviceUnavailable}
            placeholder={t('rooms.messagePlaceholder')}
            enterKeyHint="send"
            autoCapitalize="sentences"
            autoCorrect="on"
            spellCheck
            rows={1}
            aria-label={t('rooms.messagePlaceholder')}
          />
          <div className="chat-composer-toolbar chat-room-actions">
            {!state.disbanded && !state.serviceUnavailable ? <button type="button" onClick={() => void state.stopRoom()} disabled={state.refreshing} title={t('rooms.stop')} aria-label={t('rooms.stop')} className="chat-composer-action chat-room-icon"><XOctagon size={16} /></button> : null}
            {!state.disbanded && !state.serviceUnavailable ? <button type="button" onClick={() => { if (confirmDisband) { setConfirmDisband(false); void state.disband(); } else { setConfirmDisband(true); window.setTimeout(() => setConfirmDisband(false), 3000); } }} title={t('rooms.disbandAction')} aria-label={t('rooms.disbandAction')} className={`chat-composer-action chat-room-icon chat-room-icon-danger ${confirmDisband ? 'is-confirm' : ''}`}>{confirmDisband ? <Check size={16} /> : <Trash2 size={16} />}</button> : null}
            <button type="button" onClick={() => void state.refresh()} disabled={state.refreshing} title={t('rooms.refresh')} aria-label={t('rooms.refresh')} className="chat-composer-action chat-room-icon"><RefreshCw size={16} className={state.refreshing ? 'animate-spin' : ''} /></button>
            <span className="chat-composer-hint">{t('rooms.round', { round: state.round.round })} · {t('rooms.latestEvent', { sequence: state.events.at(-1)?.seq ?? '—' })}</span>
            <span className="chat-composer-spacer" />
            <button type="submit" disabled={!draft.trim() || state.disbanded || state.serviceUnavailable} aria-label={t('rooms.send')} title={t('rooms.send')} className="chat-composer-action chat-send"><Send size={16} /></button>
          </div>
        </div>
      </form>
    </>
  );
}

type CreateRoomFormProps = {
  members: BotMentionCandidate[];
  onCancel: () => void;
  onCreate: (name: string, handles: string[], vaultId: string) => Promise<unknown>;
  initialVault?: string;
  /** Renders the invite-only creation flow; the room-type switch only appears when it is provided. */
  renderInviteOnly?: (name: string) => React.ReactNode;
};

export function CreateRoomForm({ members, onCancel, onCreate, initialVault = '', renderInviteOnly }: CreateRoomFormProps) {
  const { t } = useI18n();
  const [name, setName] = useState('');
  const [kind, setKind] = useState<'local' | 'invite'>('local');
  const [selected, setSelected] = useState<string[]>([]);
  const [vault, setVault] = useState<string>(initialVault);
  const [vaultOptions, setVaultOptions] = useState<MissionControlVaultDescriptor[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    let cancelled = false;
    const token = typeof window !== 'undefined' ? window.localStorage.getItem('mission-control-token')?.trim() || undefined : undefined;
    void loadMissionControlVaults(token)
      .then((list) => {
        if (cancelled) return;
        setVaultOptions(list.vaults);
        if (!vault && list.default_vault) setVault(list.default_vault);
      })
      .catch(() => { /* vault list is best-effort; the nightly derives from members */ });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const toggle = useCallback((handle: string) => {
    setSelected((current) => current.includes(handle) ? current.filter((item) => item !== handle) : [...current, handle]);
  }, []);

  const submit = useCallback(async (event: React.FormEvent) => {
    event.preventDefault();
    // Enter inside the embedded invite-only flow must not create a local room.
    if (kind === 'invite') return;
    if (!name.trim()) { setError(t('rooms.nameRequired')); return; }
    if (selected.length < 2) { setError(t('rooms.memberRequired')); return; }
    setSaving(true); setError(null);
    try {
      await onCreate(name.trim(), selected, vault);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setSaving(false);
    }
  }, [kind, name, onCreate, selected, t, vault]);

  const toggleAll = useCallback(() => {
    setSelected((current) => current.length === members.length ? [] : members.map((m) => m.handle));
  }, [members]);

  return (
    <form onSubmit={submit} className="flex flex-col gap-3 rounded-xl border border-border-subtle bg-surface-raised/40 p-3 sm:p-4">
      <div className="flex items-center justify-between gap-2">
        <h3 className="text-sm font-semibold text-text">{t('rooms.createTitle')}</h3>
        <button type="button" className="chat-control chat-icon-button" onClick={onCancel} aria-label={t('rooms.close')}><X size={16} /></button>
      </div>
      {renderInviteOnly ? (
        <div className="flex flex-col gap-1.5 text-xs text-text-muted">
          <div role="radiogroup" aria-label={t('rooms.kind')} className="grid grid-cols-2 gap-1 rounded-lg border border-border-subtle bg-surface p-1">
            {(['local', 'invite'] as const).map((option) => (
              <button key={option} type="button" role="radio" aria-checked={kind === option} onClick={() => { setKind(option); setError(null); }} className={`rounded-md px-2 py-1.5 text-xs ${kind === option ? 'bg-accent text-white' : 'text-text-muted hover:bg-surface-sunken'}`}>
                {option === 'local' ? t('rooms.kindLocal') : t('rooms.kindInvite')}
              </button>
            ))}
          </div>
          <span className="text-[11px] text-text-subtle">{kind === 'local' ? t('rooms.kindLocalHint') : t('rooms.kindInviteHint')}</span>
        </div>
      ) : null}
      {renderInviteOnly && kind === 'invite' ? renderInviteOnly(name) : <>
      <label className="flex flex-col gap-1.5 text-xs text-text-muted">
        <span>{t('rooms.name')}</span>
        <input autoFocus value={name} onChange={(event) => setName(event.target.value)} placeholder={t('rooms.namePlaceholder')} className="mc-input h-9" />
      </label>
      <label className="flex flex-col gap-1.5 text-xs text-text-muted">
        <span>{t('rooms.vault')}</span>
        <select value={vault} onChange={(event) => setVault(event.target.value)} className="mc-input h-9">
          {vaultOptions.length === 0 ? <option value="">{t('rooms.vaultDefault')}</option> : null}
          {vaultOptions.map((item) => (
            <option key={item.id} value={item.id}>{item.label || item.name || item.id}</option>
          ))}
          {vault && !vaultOptions.some((item) => item.id === vault) ? <option value={vault}>{vault}</option> : null}
        </select>
        <span className="text-[11px] text-text-subtle">{t('rooms.vaultHint')}</span>
      </label>
      <div className="flex flex-col gap-1.5 text-xs text-text-muted">
        <span>{t('rooms.members')} ({selected.length}/{members.length})</span>
        <div className="flex max-h-40 flex-col gap-1 overflow-y-auto rounded-lg border border-border-subtle bg-surface p-1.5">
          <button type="button" onClick={toggleAll} className="flex items-center gap-2 rounded-md px-2 py-1.5 text-left text-xs text-accent hover:bg-surface-sunken">
            {selected.length === members.length && members.length > 0 ? <Check size={13} /> : <Plus size={13} />}{selected.length === members.length && members.length > 0 ? t('rooms.clearFocus') : t('rooms.addMember')}
          </button>
          {members.map((member) => {
            const active = selected.includes(member.handle);
            return (
              <label key={member.handle} className="flex items-center gap-2 rounded-md px-2 py-1.5 hover:bg-surface-sunken">
                <input type="checkbox" checked={active} onChange={() => toggle(member.handle)} className="accent-[var(--color-accent)]" />
                <span className="font-medium text-text">@{member.handle}</span>
                {member.displayName ? <span className="truncate text-text-muted">{member.displayName}</span> : null}
                {member.description ? <span className="truncate text-text-subtle">{member.description}</span> : null}
              </label>
            );
          })}
        </div>
      </div>
      {error ? <p className="text-xs text-negative" role="alert">{error}</p> : null}
      <button type="submit" disabled={saving} className="inline-flex shrink-0 items-center justify-center gap-1.5 rounded-lg bg-accent px-3 py-2 text-sm text-white disabled:opacity-50">
        {saving ? <Loader2 size={14} className="animate-spin" /> : <Plus size={14} />}{t('rooms.createSubmit')}
      </button>
      </>}
    </form>
  );
}

export const GroupRoomView = memo(function GroupRoomView({ state, onSend, className = '', mentionRoster = [], onNearBottomChange }: GroupRoomViewProps) {
  const { t } = useI18n();
  const [focusedMember, setFocusedMember] = useState<string | null>(null);
  const [nearBottom, setNearBottom] = useState(true);
  const nearBottomRef = useRef(true);
  const transcriptRef = useRef<HTMLDivElement>(null);
  const [roomTools, setRoomTools] = useState<RoomToolTrace[]>([]);
  const [expandedStrips, setExpandedStrips] = useState<Set<string>>(new Set());

  const toggleStrip = useCallback((eventId: string) => {
    setExpandedStrips((current) => {
      const next = new Set(current);
      if (next.has(eventId)) {
        next.delete(eventId);
      } else {
        next.add(eventId);
      }
      return next;
    });
  }, []);

  // Room tool traces are produced by the members' own turns, so they keep
  // growing while a room works. Refresh on the transcript's latest event
  // sequence (the same cadence as the room poll) instead of loading once per
  // room selection — otherwise the strip freezes on the trace set that existed
  // when the room was opened.
  const latestSequence = state.events.at(-1)?.seq ?? 0;
  useEffect(() => {
    let cancelled = false;
    if (!state.selectedRoomId) {
      setRoomTools([]);
      return () => { cancelled = true; };
    }
    let token: string | undefined;
    try { token = localStorage.getItem('mission-control-token') ?? undefined; } catch { token = undefined; }
    loadRoomTools(state.selectedRoomId, token)
      .then((tools) => { if (!cancelled) setRoomTools(tools); })
      .catch(() => { if (!cancelled) setRoomTools([]); });
    return () => { cancelled = true; };
  }, [state.selectedRoomId, latestSequence]);
  const entries = useMemo(() => visibleGroupEvents(state.events, state.room?.members ?? []), [state.events, state.room?.members]);
  const latestByMember = useMemo(() => {
    const result: Record<string, GroupEvent> = {};
    for (const event of state.events) if (event.actor.kind === 'member') result[event.message.member?.id ?? event.actor.id] = event;
    return result;
  }, [state.events]);
  const filtered = focusedMember ? entries.filter(({ event, member }) => event.actor.kind === 'user' || member?.id === focusedMember) : entries;
  const hiddenWorkingMembers = focusedMember === null ? [] : (state.room?.members ?? []).filter((member) =>
    member.id !== focusedMember && deriveGroupMemberStatus(member, state.driverStatus, latestByMember[member.id] ?? null) === 'working');

  // Tools per member-final turn: attach each trace whose timestamp falls
  // between the previous member reply and this member reply, matching the
  // member handle. trace timestamps are unix seconds; event createdAt is ms.
  const toolsByMemberReply = useMemo(() => {
    const result = new Map<string, RoomToolTrace[]>();
    if (!roomTools.length) return result;
    const memberSequence = filtered.filter(({ event }) => event.actor.kind === 'member');
    memberSequence.forEach(({ event, member }, index) => {
      const replyTs = typeof event.createdAt === 'number' ? event.createdAt : null;
      if (replyTs === null) return;
      const boundary = index > 0 && typeof memberSequence[index - 1].event.createdAt === 'number'
        ? (memberSequence[index - 1].event.createdAt as number)
        : 0;
      const handle = member?.handle ?? event.message.member?.handle;
      const turnTools = roomTools.filter((trace) => {
        if (trace.memberHandle !== handle) return false;
        const traceTs = typeof trace.timestamp === 'number' ? trace.timestamp * 1000 : 0;
        return traceTs > boundary && traceTs <= replyTs;
      });
      if (turnTools.length > 0) result.set(event.id, turnTools);
    });
    return result;
  }, [filtered, roomTools]);

  const scrollToBottom = useCallback((behavior: ScrollBehavior = 'auto') => {
    const node = transcriptRef.current;
    if (!node) return;
    nearBottomRef.current = true;
    setNearBottom(true);
    onNearBottomChange?.(true);
    node.scrollTo({ top: node.scrollHeight, behavior });
  }, [onNearBottomChange]);

  // Follow the transcript while the user is already at the bottom.
  //
  // This is a LAYOUT effect keyed on the number of RENDERED rows, not on
  // `state.events.length`: the room log arrives before the roster does, and
  // `visibleGroupEvents` needs the members to produce any row at all. Keyed on
  // the event count the scroll ran against an empty ~600px node (a no-op at the
  // top), the real content committed afterwards with no further trigger, and
  // entering a room left the user at the top of a 43k-px transcript — with the
  // FAB hidden too, because the follow had just set nearBottom=true.
  useLayoutEffect(() => {
    if (nearBottomRef.current) scrollToBottom();
  }, [scrollToBottom, filtered.length]);

  // Safety net for growth that does not change the row count: the pagination
  // tail landing, an expanded tool strip, late-loading content. ResizeObserver
  // cannot see this (a scroll container's box is fixed), so watch the children.
  useEffect(() => {
    const node = transcriptRef.current;
    if (!node || typeof MutationObserver === 'undefined') return undefined;
    let children = node.childElementCount;
    const observer = new MutationObserver(() => {
      if (node.childElementCount === children) return;
      children = node.childElementCount;
      if (nearBottomRef.current) scrollToBottom();
    });
    observer.observe(node, { childList: true });
    return () => observer.disconnect();
  }, [scrollToBottom]);

  const handleScroll = useCallback(() => {
    const node = transcriptRef.current;
    if (!node) return;
    const position = node.scrollTop + node.clientHeight;
    const max = node.scrollHeight;
    const bottom = max - position < 96;
    if (bottom !== nearBottomRef.current) {
      nearBottomRef.current = bottom;
      setNearBottom(bottom);
      onNearBottomChange?.(bottom);
    }
  }, [onNearBottomChange]);

  return <section className={`flex min-h-0 flex-1 flex-col ${className}`} aria-label={t('rooms.title')}>
    <StateNotice state={state} />
    {state.error && !state.serviceUnavailable ? <div className="text-xs text-negative" role="alert">{state.error.message}</div> : null}
    <div ref={transcriptRef} onScroll={handleScroll} className="chat-transcript min-h-[220px] flex-1">
      {state.loading ? <div className="chat-empty"><Loader2 size={16} className="chat-spin" />{t('rooms.loading')}</div> : filtered.length === 0 ? <p className="chat-empty">{t('rooms.noMessages')}</p> : (
        filtered.map(({ event, member }, index) => (
          <div key={event.id} style={{ display: 'contents' }}>
            {event.round !== undefined && (index === 0 || filtered[index - 1].event.round !== event.round) ? (
              <div className="chat-round-divider" aria-hidden>{t('rooms.round', { round: event.round })}</div>
            ) : null}
            <RoomTranscriptRow
              message={groupEventToChatMessage(event, member)}
              mentionHandles={(state.room?.members ?? []).map((item) => item.handle)}
            >
              {event.actor.kind === 'member' && toolsByMemberReply.has(event.id) ? (
                <RoomToolStrip
                  tools={toolsByMemberReply.get(event.id) ?? []}
                  memberHandle={member?.handle ?? ''}
                  expanded={expandedStrips.has(event.id)}
                  onToggle={() => toggleStrip(event.id)}
                />
              ) : null}
            </RoomTranscriptRow>
          </div>
        ))
      )}
      {!nearBottom ? (
        <button
          className="chat-scroll-fab"
          type="button"
          onClick={() => scrollToBottom('smooth')}
          aria-label={t('rooms.scrollLatest')}
          title={t('rooms.scrollLatest')}
        >
          <ChevronDown size={18} />
        </button>
      ) : null}
    </div>
    <div className="chat-room-filters">
      <div className="chat-room-filter-label">{t('rooms.showMessagesFrom')}</div>
      <div className="chat-room-filterbar" role="group" aria-label={t('rooms.showMessagesFrom')}>
        <button type="button" aria-pressed={!focusedMember} onClick={() => setFocusedMember(null)} className="chat-room-filter-chip">
          <Users size={14} aria-hidden="true" />{t('rooms.everyone')}
        </button>
        {(state.room?.members ?? []).map((member) => {
          const status = deriveGroupMemberStatus(member, state.driverStatus, latestByMember[member.id] ?? null);
          return <button key={member.id} type="button" aria-pressed={focusedMember === member.id} onClick={() => setFocusedMember(member.id)} className="chat-room-filter-chip">
            <span className="chat-room-member-name">{member.displayName || `@${member.handle}`}</span>
            <MemberFilterStatus status={status} />
          </button>;
        })}
      </div>
      {hiddenWorkingMembers.length > 0 ? <div className="chat-room-outside-activity" role="status">
        <EyeOff size={13} aria-hidden="true" />
        <span>{t(hiddenWorkingMembers.length === 1 ? 'rooms.outsideMemberWorking' : 'rooms.outsideMembersWorking', {
          names: hiddenWorkingMembers.map((member) => member.displayName || `@${member.handle}`).join(', '),
        })}</span>
        <button type="button" onClick={() => setFocusedMember(null)}>{t('rooms.showAll')}</button>
      </div> : null}
    </div>
    {onSend ? <GroupRoomComposer state={state} onSend={onSend} mentionRoster={mentionRoster} /> : null}
  </section>;
});
