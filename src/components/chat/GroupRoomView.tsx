import { useCallback, useMemo, useRef, useState } from 'react';
import { AlertTriangle, Check, ChevronRight, Circle, Loader2, Plus, RefreshCw, Send, ShieldAlert, Users, X, XCircle } from 'lucide-react';
import { useI18n } from '../../lib/i18n';
import { ChatMarkdown } from '../chat-messages';
import { Badge } from '../ui/Badge';
import type { GroupEvent } from '../../lib/group-gateway';
import type { GroupRoomResult } from '../../lib/use-group-room';
import { ChatMentionPopover, type ChatMentionPopoverHandle } from '../ChatMentionPopover';
import type { BotMentionCandidate } from '../../lib/bot-mentions';
import { GroupGatewayClient } from '../../lib/group-gateway';
import { deriveGroupMemberStatus, groupEventLabel, visibleGroupEvents } from './group-room-view-model';
import type { GroupMemberStatus } from './group-room-view-model';
export { deriveGroupMemberStatus, groupEventLabel, visibleGroupEvents } from './group-room-view-model';

type GroupRoomViewProps = {
  state: GroupRoomResult;
  onSend?: (text: string) => Promise<unknown>;
  onCreateRoom?: (name: string, members: string[]) => Promise<unknown>;
  className?: string;
  mentionRoster?: BotMentionCandidate[];
};


function formatTime(value: string | number | null): string {
  if (value === null) return '';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

const statusKey: Record<GroupMemberStatus, string> = {
  idle: 'rooms.status.idle',
  working: 'rooms.status.working',
  settled: 'rooms.status.settled',
  unavailable: 'rooms.status.unavailable',
};
const statusVariant: Record<GroupMemberStatus, 'default' | 'positive' | 'warning' | 'negative'> = { idle: 'default', working: 'warning', settled: 'positive', unavailable: 'negative' };

function StatusBadge({ status }: { status: GroupMemberStatus }) {
  const { t } = useI18n();
  return <Badge variant={statusVariant[status]} dot>{t(statusKey[status])}</Badge>;
}

function StateNotice({ state }: { state: GroupRoomResult }) {
  const { t } = useI18n();
  if (state.serviceUnavailable) return <div className="rounded-lg border border-negative/30 bg-negative-subtle p-3 text-sm text-negative" role="alert"><XCircle size={16} className="mr-2 inline" />{t('rooms.serviceUnavailable')} <button type="button" className="underline" onClick={() => void state.retry()}>{t('rooms.retry')}</button></div>;
  if (state.authorityChanged) return <div className="rounded-lg border border-warning/30 bg-warning-subtle p-3 text-sm text-warning" role="status"><ShieldAlert size={16} className="mr-2 inline" />{t('rooms.authorityConflict')}</div>;
  if (state.disbanded) return <div className="rounded-lg border border-border-subtle bg-surface-sunken/50 p-3 text-sm text-text-muted" role="status"><XCircle size={16} className="mr-2 inline" />{t('rooms.disbanded')}</div>;
  if (state.approval || state.blocked || state.pendingActions.length) return <div className="rounded-lg border border-warning/30 bg-warning-subtle p-3 text-sm text-warning" role="status"><AlertTriangle size={16} className="mr-2 inline" />{t('rooms.pendingApprovalOrRetry')}</div>;
  return null;
}

function GroupRoomComposer({ state, onSend, mentionRoster }: { state: GroupRoomResult; onSend: (text: string) => Promise<unknown>; mentionRoster: BotMentionCandidate[] }) {
  const { t } = useI18n();
  const [draft, setDraft] = useState('');
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const mentionPopoverRef = useRef<ChatMentionPopoverHandle | null>(null);

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
    <form onSubmit={submit} className="group-room-composer flex shrink-0 gap-2">
      <div className="min-w-0 flex-1">
        <ChatMentionPopover
          ref={mentionPopoverRef}
          input={draft}
          roster={mentionRoster}
          textareaRef={textareaRef}
          onApply={setDraft}
        />
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
          className="group-room-composer-input w-full rounded-lg border border-border-subtle bg-surface px-3 py-2 text-sm text-text outline-none focus:border-accent"
        />
      </div>
      <button type="submit" disabled={!draft.trim() || state.disbanded || state.serviceUnavailable} className="inline-flex shrink-0 items-center gap-1.5 rounded-lg bg-accent px-3 py-2 text-sm text-white disabled:opacity-50">
        <Send size={14} />{t('rooms.send')}
      </button>
    </form>
  );
}

function CreateRoomForm({ members, onCancel, onCreate }: { members: BotMentionCandidate[]; onCancel: () => void; onCreate: (name: string, handles: string[]) => Promise<unknown> }) {
  const { t } = useI18n();
  const [name, setName] = useState('');
  const [selected, setSelected] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const toggle = useCallback((handle: string) => {
    setSelected((current) => current.includes(handle) ? current.filter((item) => item !== handle) : [...current, handle]);
  }, []);

  const submit = useCallback(async (event: React.FormEvent) => {
    event.preventDefault();
    if (!name.trim()) { setError(t('rooms.nameRequired')); return; }
    if (selected.length < 2) { setError(t('rooms.memberRequired')); return; }
    setSaving(true); setError(null);
    try {
      await onCreate(name.trim(), selected);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setSaving(false);
    }
  }, [name, onCreate, selected, t]);

  const toggleAll = useCallback(() => {
    setSelected((current) => current.length === members.length ? [] : members.map((m) => m.handle));
  }, [members]);

  return (
    <form onSubmit={submit} className="flex flex-col gap-3 rounded-xl border border-border-subtle bg-surface-raised/40 p-3 sm:p-4">
      <div className="flex items-center justify-between gap-2">
        <h3 className="text-sm font-semibold text-text">{t('rooms.createTitle')}</h3>
        <button type="button" className="chat-control chat-icon-button" onClick={onCancel} aria-label={t('rooms.close')}><X size={16} /></button>
      </div>
      <label className="flex flex-col gap-1.5 text-xs text-text-muted">
        <span>{t('rooms.name')}</span>
        <input autoFocus value={name} onChange={(event) => setName(event.target.value)} placeholder={t('rooms.namePlaceholder')} className="mc-input h-9" />
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
    </form>
  );
}

export function GroupRoomView({ state, onSend, onCreateRoom, className = '', mentionRoster = [] }: GroupRoomViewProps) {
  const { t } = useI18n();
  const [focusedMember, setFocusedMember] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [draft, setDraft] = useState('');
  const clientRef = useRef<GroupGatewayClient | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const entries = useMemo(() => visibleGroupEvents(state.events, state.room?.members ?? []), [state.events, state.room?.members]);
  const latestByMember = useMemo(() => {
    const result: Record<string, GroupEvent> = {};
    for (const event of state.events) if (event.actor.kind === 'member') result[event.message.member?.id ?? event.actor.id] = event;
    return result;
  }, [state.events]);
  const filtered = focusedMember ? entries.filter(({ event, member }) => event.actor.kind === 'user' || member?.id === focusedMember) : entries;
  const hiddenWorking = state.room?.members.some((member) => member.id !== focusedMember && deriveGroupMemberStatus(member, state.driverStatus, latestByMember[member.id]) === 'working');

  const createRoom = useCallback(async (name: string, handles: string[]) => {
    const client = clientRef.current ?? new GroupGatewayClient();
    clientRef.current = client;
    const roster = handles.map((handle, index) => ({
      id: `member-${handle}-${index}`,
      profile: (handle === 'default' ? 'default' : handle),
      handle,
      displayName: mentionRoster.find((m) => m.handle === handle)?.displayName,
    }));
    const room = await client.create({ roomId: `mc-${Date.now()}`.slice(0, 48), name, roster });
    setCreating(false);
    await state.refresh();
    await state.selectRoom(room.id);
  }, [mentionRoster, state]);

  return <section className={`flex min-h-0 flex-col gap-3 ${className}`} aria-label={t('rooms.title')}>
    <header className="flex flex-wrap items-start justify-between gap-3 rounded-xl border border-border-subtle bg-surface-raised/40 p-3 sm:p-4">
      <div className="min-w-0"><p className="truncate text-base font-semibold text-text">{state.room?.name || t('rooms.title')}</p><p className="mt-1 text-xs text-text-muted">{state.room?.members.length ?? 0} {t('rooms.members').toLowerCase()} · authority epoch {state.room?.authorityEpoch ?? '—'}</p></div>
      <div className="flex items-center gap-2">
        {onCreateRoom ? <button type="button" onClick={() => setCreating((current) => !current)} className="inline-flex min-h-9 items-center gap-1.5 rounded-md border border-border-subtle px-2.5 text-xs text-text-muted hover:bg-surface-sunken disabled:opacity-50">{creating ? <X size={14} /> : <Plus size={14} />}{creating ? t('rooms.close') : t('rooms.create')}</button> : null}
        <button type="button" onClick={() => void state.refresh()} disabled={state.refreshing} className="inline-flex min-h-9 items-center gap-1.5 rounded-md border border-border-subtle px-2.5 text-xs text-text-muted hover:bg-surface-sunken disabled:opacity-50"><RefreshCw size={14} className={state.refreshing ? 'animate-spin' : ''} />{t('rooms.refresh')}</button>
      </div>
    </header>
    <StateNotice state={state} />
    {state.error && !state.serviceUnavailable ? <div className="text-xs text-negative" role="alert">{state.error.message}</div> : null}
    {creating ? <CreateRoomForm members={mentionRoster} onCancel={() => setCreating(false)} onCreate={createRoom} /> : null}
    <div className="flex min-w-0 gap-2 overflow-x-auto pb-1" role="tablist" aria-label={t('rooms.members')}>
      <button type="button" role="tab" aria-selected={!focusedMember} onClick={() => setFocusedMember(null)} className={`inline-flex shrink-0 items-center gap-1.5 rounded-full border px-3 py-1.5 text-xs ${!focusedMember ? 'border-accent bg-accent-subtle text-accent' : 'border-border-subtle text-text-muted hover:bg-surface-sunken'}`}><Users size={13} />{t('rooms.everyone')}</button>
      {(state.room?.members ?? []).map((member) => { const status = deriveGroupMemberStatus(member, state.driverStatus, latestByMember[member.id] ?? null); return <button key={member.id} type="button" role="tab" aria-selected={focusedMember === member.id} onClick={() => setFocusedMember(member.id)} className={`inline-flex shrink-0 items-center gap-2 rounded-full border px-3 py-1.5 text-xs ${focusedMember === member.id ? 'border-accent bg-accent-subtle text-accent' : 'border-border-subtle text-text-muted hover:bg-surface-sunken'}`}><span>{member.displayName || `@${member.handle}`}</span><StatusBadge status={status} /></button>; })}
    </div>
    {hiddenWorking ? <div className="flex items-center gap-2 text-xs text-warning" role="status"><Loader2 size={13} className="animate-spin" />{t('rooms.backgroundMemberWorking')}</div> : null}
    <div className="min-h-[220px] flex-1 overflow-y-auto rounded-xl border border-border-subtle bg-surface-sunken/20 p-3 sm:p-4">
      {state.loading ? <div className="flex items-center gap-2 text-sm text-text-muted"><Loader2 size={16} className="animate-spin" />{t('rooms.loading')}</div> : filtered.length === 0 ? <p className="text-sm text-text-muted">{t('rooms.noMessages')}</p> : <div className="flex flex-col gap-4">{filtered.map(({ event, member }, index) => <div key={event.id}>{event.round !== undefined && (index === 0 || filtered[index - 1].event.round !== event.round) ? <div className="mb-3 flex items-center gap-2 text-[10px] uppercase tracking-widest text-text-subtle"><span className="h-px flex-1 bg-border-subtle" />{t('rooms.round', { round: event.round })}<span className="h-px flex-1 bg-border-subtle" /></div> : null}<article className={`max-w-[92%] rounded-xl border p-3 ${event.actor.kind === 'user' ? 'ml-auto border-accent/30 bg-accent-subtle/30' : 'border-border-subtle bg-surface-raised/50'}`}><div className="mb-1 flex items-center gap-2 text-[11px] text-text-muted"><Circle size={8} className={event.actor.kind === 'user' ? 'fill-accent text-accent' : 'fill-sky-400 text-sky-400'} /><span>{event.actor.kind === 'user' ? t('rooms.you') : member?.displayName || (member ? `@${member.handle}` : event.message.member?.displayName || t('rooms.member'))}</span><span className="text-text-subtle">{event.kind === 'message.user' ? t('rooms.you') : event.kind === 'message.member' || event.message.member ? t('rooms.finalAnswer') : groupEventLabel(event)}</span>{formatTime(event.createdAt) ? <time className="ml-auto text-text-subtle">{formatTime(event.createdAt)}</time> : null}</div><ChatMarkdown text={event.message.text} placeholder="…" /></article></div>)}</div>}
    </div>
    {onSend ? <GroupRoomComposer state={state} onSend={onSend} mentionRoster={mentionRoster} /> : null}
    <footer className="flex flex-wrap items-center gap-3 text-[11px] text-text-subtle"><span>{t('rooms.round', { round: state.round.round })}</span><span>·</span><span>{t('rooms.latestEvent', { sequence: state.events.at(-1)?.seq ?? '—' })}</span>{state.focusHandle ? <span className="inline-flex items-center gap-1 text-accent"><ChevronRight size={12} />{t('rooms.focus', { handle: state.focusHandle })}</span> : null}<span className="ml-auto inline-flex items-center gap-1"><Check size={12} />{t('rooms.protocolVersion', { version: state.capabilities?.protocolVersion ?? '—' })}</span></footer>
  </section>;
}
