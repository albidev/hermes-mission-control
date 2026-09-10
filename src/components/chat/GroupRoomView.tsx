import { useMemo, useRef, useState } from 'react';
import { AlertTriangle, Check, ChevronRight, Circle, Loader2, RefreshCw, Send, ShieldAlert, Users, XCircle } from 'lucide-react';
import { ChatMarkdown } from '../chat-messages';
import { Badge } from '../ui/Badge';
import type { GroupEvent } from '../../lib/group-gateway';
import type { GroupRoomResult } from '../../lib/use-group-room';
import { ChatMentionPopover } from '../ChatMentionPopover';
import type { BotMentionCandidate } from '../../lib/bot-mentions';
import { deriveGroupMemberStatus, groupEventLabel, visibleGroupEvents } from './group-room-view-model';
import type { GroupMemberStatus } from './group-room-view-model';
export { deriveGroupMemberStatus, groupEventLabel, visibleGroupEvents } from './group-room-view-model';

type GroupRoomViewProps = {
  state: GroupRoomResult;
  onSend?: (text: string) => Promise<unknown>;
  className?: string;
  mentionRoster?: BotMentionCandidate[];
};



function formatTime(value: string | number | null): string {
  if (value === null) return '';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

const statusCopy: Record<GroupMemberStatus, string> = { idle: 'Idle', working: 'Working', settled: 'Settled', unavailable: 'Unavailable' };
const statusVariant: Record<GroupMemberStatus, 'default' | 'positive' | 'warning' | 'negative'> = { idle: 'default', working: 'warning', settled: 'positive', unavailable: 'negative' };

function StatusBadge({ status }: { status: GroupMemberStatus }) {
  return <Badge variant={statusVariant[status]} dot>{statusCopy[status]}</Badge>;
}

function StateNotice({ state }: { state: GroupRoomResult }) {
  if (state.serviceUnavailable) return <div className="rounded-lg border border-negative/30 bg-negative-subtle p-3 text-sm text-negative" role="alert"><XCircle size={16} className="mr-2 inline" />Group Chat service unavailable. <button type="button" className="underline" onClick={() => void state.retry()}>Retry</button></div>;
  if (state.authorityChanged) return <div className="rounded-lg border border-warning/30 bg-warning-subtle p-3 text-sm text-warning" role="status"><ShieldAlert size={16} className="mr-2 inline" />Authority changed. The timeline was reconciled from the new gateway.</div>;
  if (state.disbanded) return <div className="rounded-lg border border-border-subtle bg-surface-sunken/50 p-3 text-sm text-text-muted" role="status"><XCircle size={16} className="mr-2 inline" />This room has been disbanded. Its history remains available.</div>;
  if (state.approval || state.blocked || state.pendingActions.length) return <div className="rounded-lg border border-warning/30 bg-warning-subtle p-3 text-sm text-warning" role="status"><AlertTriangle size={16} className="mr-2 inline" />This room is waiting for approval or a retry.</div>;
  return null;
}

export function GroupRoomView({ state, onSend, className = '', mentionRoster = [] }: GroupRoomViewProps) {
  const [focusedMember, setFocusedMember] = useState<string | null>(null);
  const [draft, setDraft] = useState('');
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const entries = useMemo(() => visibleGroupEvents(state.events, state.room?.members ?? []), [state.events, state.room?.members]);
  const latestByMember = useMemo(() => {
    const result: Record<string, GroupEvent> = {};
    for (const event of state.events) if (event.actor.kind === 'member') result[event.message.member?.id ?? event.actor.id] = event;
    return result;
  }, [state.events]);
  const filtered = focusedMember ? entries.filter(({ event, member }) => event.actor.kind === 'user' || member?.id === focusedMember) : entries;
  const hiddenWorking = state.room?.members.some((member) => member.id !== focusedMember && deriveGroupMemberStatus(member, state.driverStatus, latestByMember[member.id]) === 'working');

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!onSend || !draft.trim() || state.disbanded) return;
    const text = draft.trim();
    setDraft('');
    await onSend(text);
  }

  return <section className={`flex min-h-0 flex-col gap-3 ${className}`} aria-label="Group Chat room">
    <header className="flex flex-wrap items-start justify-between gap-3 rounded-xl border border-border-subtle bg-surface-raised/40 p-3 sm:p-4">
      <div className="min-w-0"><p className="truncate text-base font-semibold text-text">{state.room?.name || 'Group Chat'}</p><p className="mt-1 text-xs text-text-muted">{state.room?.members.length ?? 0} members · authority epoch {state.room?.authorityEpoch ?? '—'}</p></div>
      <button type="button" onClick={() => void state.refresh()} disabled={state.refreshing} className="inline-flex min-h-9 items-center gap-1.5 rounded-md border border-border-subtle px-2.5 text-xs text-text-muted hover:bg-surface-sunken disabled:opacity-50"><RefreshCw size={14} className={state.refreshing ? 'animate-spin' : ''} />Refresh</button>
    </header>
    <StateNotice state={state} />
    {state.error && !state.serviceUnavailable ? <div className="text-xs text-negative" role="alert">{state.error.message}</div> : null}
    <div className="flex min-w-0 gap-2 overflow-x-auto pb-1" role="tablist" aria-label="Group members">
      <button type="button" role="tab" aria-selected={!focusedMember} onClick={() => setFocusedMember(null)} className={`inline-flex shrink-0 items-center gap-1.5 rounded-full border px-3 py-1.5 text-xs ${!focusedMember ? 'border-accent bg-accent-subtle text-accent' : 'border-border-subtle text-text-muted hover:bg-surface-sunken'}`}><Users size={13} />Everyone</button>
      {(state.room?.members ?? []).map((member) => { const status = deriveGroupMemberStatus(member, state.driverStatus, latestByMember[member.id] ?? null); return <button key={member.id} type="button" role="tab" aria-selected={focusedMember === member.id} onClick={() => setFocusedMember(member.id)} className={`inline-flex shrink-0 items-center gap-2 rounded-full border px-3 py-1.5 text-xs ${focusedMember === member.id ? 'border-accent bg-accent-subtle text-accent' : 'border-border-subtle text-text-muted hover:bg-surface-sunken'}`}><span>{member.displayName || `@${member.handle}`}</span><StatusBadge status={status} /></button>; })}
    </div>
    {hiddenWorking ? <div className="flex items-center gap-2 text-xs text-warning" role="status"><Loader2 size={13} className="animate-spin" />Another member is working in the background.</div> : null}
    <div className="min-h-[220px] flex-1 overflow-y-auto rounded-xl border border-border-subtle bg-surface-sunken/20 p-3 sm:p-4">
      {state.loading ? <div className="flex items-center gap-2 text-sm text-text-muted"><Loader2 size={16} className="animate-spin" />Loading room…</div> : filtered.length === 0 ? <p className="text-sm text-text-muted">No user messages or final member answers yet.</p> : <div className="flex flex-col gap-4">{filtered.map(({ event, member }, index) => <div key={event.id}>{event.round !== undefined && (index === 0 || filtered[index - 1].event.round !== event.round) ? <div className="mb-3 flex items-center gap-2 text-[10px] uppercase tracking-widest text-text-subtle"><span className="h-px flex-1 bg-border-subtle" />Round {event.round}<span className="h-px flex-1 bg-border-subtle" /></div> : null}<article className={`max-w-[92%] rounded-xl border p-3 ${event.actor.kind === 'user' ? 'ml-auto border-accent/30 bg-accent-subtle/30' : 'border-border-subtle bg-surface-raised/50'}`}><div className="mb-1 flex items-center gap-2 text-[11px] text-text-muted"><Circle size={8} className={event.actor.kind === 'user' ? 'fill-accent text-accent' : 'fill-sky-400 text-sky-400'} /><span>{event.actor.kind === 'user' ? 'You' : member?.displayName || (member ? `@${member.handle}` : event.message.member?.displayName || 'Member')}</span><span className="text-text-subtle">{groupEventLabel(event)}</span>{formatTime(event.createdAt) ? <time className="ml-auto text-text-subtle">{formatTime(event.createdAt)}</time> : null}</div><ChatMarkdown text={event.message.text} placeholder="…" /></article></div>)}</div>}
    </div>
    {onSend ? <form onSubmit={submit} className="flex gap-2"><div className="min-w-0 flex-1"><ChatMentionPopover input={draft} roster={mentionRoster} textareaRef={textareaRef} onApply={setDraft} /><textarea ref={textareaRef} value={draft} onChange={(event) => setDraft(event.target.value)} disabled={state.disbanded || state.serviceUnavailable} placeholder="Message the room…" rows={1} className="w-full rounded-lg border border-border-subtle bg-surface px-3 py-2 text-sm text-text outline-none focus:border-accent" /></div><button type="submit" disabled={!draft.trim() || state.disbanded || state.serviceUnavailable} className="inline-flex items-center gap-1.5 rounded-lg bg-accent px-3 py-2 text-sm text-white disabled:opacity-50"><Send size={14} />Send</button></form> : null}
    <footer className="flex flex-wrap items-center gap-3 text-[11px] text-text-subtle"><span>Round {state.round.round}</span><span>·</span><span>Latest event {state.events.at(-1)?.seq ?? '—'}</span>{state.focusHandle ? <span className="inline-flex items-center gap-1 text-accent"><ChevronRight size={12} />Focus @{state.focusHandle}</span> : null}<span className="ml-auto inline-flex items-center gap-1"><Check size={12} />Protocol v{state.capabilities?.protocolVersion ?? '—'}</span></footer>
  </section>;
}
