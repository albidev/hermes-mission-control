import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Kanban as KanbanIcon, RefreshCw, Plus, X, MessageSquare, GitBranch, Trash2, Search, ChevronDown } from 'lucide-react';
import { Card } from '../components/ui/Card';
import { Button } from '../components/ui/Button';
import {
  loadKanbanBoard,
  loadKanbanBoards,
  loadKanbanTaskDetail,
  loadKanbanTaskLog,
  moveKanbanTask,
  archiveKanbanTask,
  patchKanbanTask,
  linkKanbanTask,
  createKanbanTask,
  createKanbanBoard,
  deleteKanbanBoard,
  addKanbanComment,
  type MissionControlKanbanBoard,
  type MissionControlKanbanBoardMeta,
  type MissionControlKanbanTask,
  type MissionControlKanbanTaskDetail,
} from '../lib/hermes-api';
import { useMissionControl } from '../lib/mission-control-store';
import { useI18n } from '../lib/i18n';

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const STATUS_TONES: Record<string, string> = {
  triage: 'text-amber-400',
  todo: 'text-sky-400',
  scheduled: 'text-violet-400',
  ready: 'text-emerald-400',
  running: 'text-sky-300',
  blocked: 'text-red-400',
  review: 'text-amber-300',
  done: 'text-emerald-500',
};

const STATUS_DOT: Record<string, string> = {
  triage: 'bg-amber-400',
  todo: 'bg-sky-400',
  scheduled: 'bg-violet-400',
  ready: 'bg-emerald-400',
  running: 'bg-sky-300',
  blocked: 'bg-red-400',
  review: 'bg-amber-300',
  done: 'bg-emerald-500',
};

const COLUMN_HINTS: Record<string, string> = {
  triage: 'Needs classification',
  todo: 'Committed work',
  scheduled: 'Planned for a future run',
  ready: 'Dependencies satisfied',
  running: 'Claimed — in-flight',
  blocked: 'Waiting on external input',
  review: 'Pending human approval',
  done: 'Completed',
};

const PRIORITY_COLORS: Record<number, { bg: string; text: string; label: string }> = {
  0: { bg: 'bg-surface-sunken', text: 'text-text-subtle', label: 'P0' },
  1: { bg: 'bg-red-500/15', text: 'text-red-400', label: 'P1' },
  2: { bg: 'bg-amber-500/15', text: 'text-amber-400', label: 'P2' },
  3: { bg: 'bg-sky-500/15', text: 'text-sky-400', label: 'P3' },
  4: { bg: 'bg-surface-sunken', text: 'text-text-subtle', label: 'P4' },
  5: { bg: 'bg-surface-sunken', text: 'text-text-subtle', label: 'P5' },
};

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function formatAge(timestamp?: number | null): string | null {
  if (!timestamp) return null;
  const diffMs = Date.now() / 1000 - timestamp;
  if (diffMs < 60) return 'just now';
  const diffMin = Math.floor(diffMs / 60);
  if (diffMin < 60) return `${diffMin}m ago`;
  const diffH = Math.floor(diffMin / 60);
  if (diffH < 24) return `${diffH}h ago`;
  const diffD = Math.floor(diffH / 24);
  if (diffD === 1) return '1d ago';
  if (diffD < 30) return `${diffD}d ago`;
  const diffMo = Math.floor(diffD / 30);
  return `${diffMo}mo ago`;
}

function shortId(id: string): string {
  // t_446d7581 → 446d75… or just show the last 6 chars after t_ prefix
  const suffix = id.replace(/^[a-z]+_/, '');
  return suffix.length > 6 ? suffix.slice(0, 6) : suffix;
}

// ---------------------------------------------------------------------------
// BoardColumn
// ---------------------------------------------------------------------------

function BoardColumn({
  name,
  tasks,
  activeTaskId,
  onOpenTask,
  onDropTask,
  onAddTask,
}: {
  name: string;
  tasks: MissionControlKanbanTask[];
  activeTaskId: string | null;
  onOpenTask: (id: string) => void;
  onDropTask: (taskId: string, status: string) => void;
  onAddTask: (status: string) => void;
}) {
  const { t } = useI18n();
  const [dragOver, setDragOver] = useState(false);
  const hint = name ? t(`kanban.columnHint.${name}`) : null;

  return (
    <div
      className={`kanban-column flex flex-col w-[19rem] shrink-0 gap-3 rounded-xl border p-3 ${dragOver ? 'border-sky-400/60 bg-sky-400/5' : 'border-border-subtle bg-surface/30'}`}
      data-status={name}
      onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
      onDragLeave={() => setDragOver(false)}
      onDrop={(e) => { e.preventDefault(); setDragOver(false); const id = e.dataTransfer.getData('text/plain'); if (id) onDropTask(id, name); }}
    >
      {/* Header */}
      <div className="flex items-center justify-between px-0.5">
        <div className="flex items-center gap-1.5 min-w-0">
          <span className={`inline-block h-2 w-2 rounded-full shrink-0 ${STATUS_DOT[name] ?? 'bg-text-muted'}`} />
          <span className={`text-xs font-semibold uppercase tracking-wide ${STATUS_TONES[name] ?? 'text-text-muted'}`}>{name}</span>
        </div>
        <div className="flex items-center gap-1">
          <span className="text-[11px] text-text-subtle tabular-nums">{tasks.length}</span>
          <button type="button" className="flex h-5 w-5 items-center justify-center rounded p-0 text-text-subtle hover:text-text hover:bg-surface-sunken" aria-label={t('kanban.addTaskAria', { name })} onClick={() => onAddTask(name)}>
            <Plus size={13} />
          </button>
        </div>
      </div>
      {hint ? <p className="text-[10px] text-text-muted px-0.5 -mt-1">{hint}</p> : null}

      {/* Task cards */}
      <div className="flex flex-col gap-2 overflow-y-auto overflow-x-hidden pb-1" role="list">
        {tasks.map((task) => {
          const pri = PRIORITY_COLORS[task.priority ?? 0] ?? PRIORITY_COLORS[0];
          const age = formatAge(task.created_at);
          return (
            <button
              key={task.id}
              type="button"
              role="listitem"
              draggable
              onDragStart={(e) => e.dataTransfer.setData('text/plain', task.id)}
              onClick={() => onOpenTask(task.id)}
              className={`kanban-card w-full min-w-[10rem] sm:min-w-0 rounded-md border p-2 text-left transition-colors ${activeTaskId === task.id ? 'border-sky-400/70 bg-sky-400/10' : 'border-border-subtle bg-surface hover:border-border'}`}
            >
              {/* Priority badge + ID + age */}
              <div className="flex items-center justify-between gap-1">
                <div className="flex items-center gap-1.5">
                  {task.priority != null && task.priority > 0 ? (
                    <span className={`inline-flex items-center rounded px-1 py-px text-[9px] font-semibold ${pri.bg} ${pri.text}`}>{pri.label}</span>
                  ) : null}
                  <span className="text-[9px] tabular-nums text-text-subtle font-mono">{shortId(task.id)}</span>
                </div>
                {age ? <span className="text-[9px] text-text-subtle">{age}</span> : null}
              </div>

              {/* Title */}
              <div className="mt-1 text-xs font-medium text-text line-clamp-2">{task.title}</div>

              {/* Bottom row: assignee + progress + comments */}
              <div className="mt-1.5 flex items-center gap-2 text-[10px] text-text-subtle">
                {task.assignee ? <span className="truncate max-w-[6rem]">{'@' + task.assignee}</span> : null}
                {(task.progress?.total ?? 0) > 0 ? (
                  <span className="inline-flex items-center gap-0.5">
                    <span className={`h-1.5 w-1.5 rounded-full ${task.progress?.done === task.progress?.total ? 'bg-emerald-400' : 'bg-sky-400/60'}`} />
                    {task.progress!.done}/{task.progress!.total}
                  </span>
                ) : (task.children?.length ?? 0) > 0 ? (
                  <span className="inline-flex items-center gap-0.5">
                    <GitBranch size={9} aria-hidden />
                    {task.children!.length}
                  </span>
                ) : null}
                {(task.comment_count ?? 0) > 0 ? (
                  <span className="inline-flex items-center gap-0.5">
                    <MessageSquare size={9} aria-hidden />
                    {task.comment_count}
                  </span>
                ) : null}
              </div>
            </button>
          );
        })}

        {/* Empty state */}
        {tasks.length === 0 ? (
          <div className="hidden sm:flex items-center justify-center rounded-md border border-dashed border-border-subtle p-6 text-[10px] text-text-subtle">
            {t('kanban.noTasks')}
          </div>
        ) : null}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// TaskDrawer
// ---------------------------------------------------------------------------

function TaskDrawer({
  taskId,
  board,
  onClose,
  onChanged,
}: {
  taskId: string;
  board?: string;
  onClose: () => void;
  onChanged?: () => void;
}) {
  const { storedToken } = useMissionControl();
  const { t } = useI18n();
  const [detail, setDetail] = useState<MissionControlKanbanTaskDetail | null>(null);
  const [workerLog, setWorkerLog] = useState<{ exists: boolean; content: string; size_bytes: number; truncated: boolean } | null>(null);
  const [logLoading, setLogLoading] = useState(false);
  const [logError, setLogError] = useState<string | null>(null);
  const [commentDraft, setCommentDraft] = useState('');
  const [postingComment, setPostingComment] = useState(false);
  const [actionBusy, setActionBusy] = useState(false);
  const [assigneeDraft, setAssigneeDraft] = useState('');
  const [parentDraft, setParentDraft] = useState('');

  const runAction = async (action: 'ready' | 'blocked' | 'done' | 'archive') => {
    if (!detail || actionBusy) return;
    if (action === 'archive' && !window.confirm('Archive this task?')) return;
    setActionBusy(true);
    try {
      if (action === 'archive') {
        await archiveKanbanTask(storedToken || undefined, taskId, board);
        onClose();
      } else {
        await moveKanbanTask(storedToken || undefined, taskId, action, board);
        const refreshed = await loadKanbanTaskDetail(storedToken || undefined, taskId, board);
        setDetail(refreshed);
      }
      onChanged?.();
    } catch (e) {
      setLogError(e instanceof Error ? e.message : 'Task action failed.');
    } finally {
      setActionBusy(false);
    }
  };

  const saveMetadata = async (input: { assignee?: string | null; priority?: number }) => {
    if (!detail || actionBusy) return;
    setActionBusy(true);
    try {
      await patchKanbanTask(storedToken || undefined, taskId, input, board);
      const refreshed = await loadKanbanTaskDetail(storedToken || undefined, taskId, board);
      setDetail(refreshed);
      setAssigneeDraft(refreshed.assignee || '');
      onChanged?.();
    } catch (e) { setLogError(e instanceof Error ? e.message : 'Metadata update failed.'); }
    finally { setActionBusy(false); }
  };

  const addParent = async () => {
    const parentId = parentDraft.trim();
    if (!parentId || !detail || actionBusy) return;
    setActionBusy(true);
    try {
      await linkKanbanTask(storedToken || undefined, taskId, parentId, false, board);
      setParentDraft('');
      const refreshed = await loadKanbanTaskDetail(storedToken || undefined, taskId, board);
      setDetail(refreshed);
      onChanged?.();
    } catch (e) { setLogError(e instanceof Error ? e.message : 'Dependency update failed.'); }
    finally { setActionBusy(false); }
  };

  const removeParent = async (parentId: string) => {
    if (!detail || actionBusy) return;
    setActionBusy(true);
    try {
      await linkKanbanTask(storedToken || undefined, taskId, parentId, true, board);
      const refreshed = await loadKanbanTaskDetail(storedToken || undefined, taskId, board);
      setDetail(refreshed);
      onChanged?.();
    } catch (e) { setLogError(e instanceof Error ? e.message : 'Dependency update failed.'); }
    finally { setActionBusy(false); }
  };

  useEffect(() => {
    if (detail) setAssigneeDraft(detail.assignee || '');
  }, [detail?.id, detail?.assignee]);

  useEffect(() => {
    let cancelled = false;
    setDetail(null);
    setWorkerLog(null);
    setLogError(null);
    setLogLoading(true);
    loadKanbanTaskDetail(storedToken || undefined, taskId, board)
      .then((d) => { if (!cancelled) setDetail(d); })
      .catch(() => { if (!cancelled) setDetail(null); });
    loadKanbanTaskLog(storedToken || undefined, taskId, board)
      .then((log) => { if (!cancelled) setWorkerLog(log); })
      .catch((e) => { if (!cancelled) setLogError(e instanceof Error ? e.message : 'Worker log unavailable.'); })
      .finally(() => { if (!cancelled) setLogLoading(false); });
    return () => { cancelled = true; };
  }, [storedToken, taskId, board]);

  const postComment = async () => {
    const body = commentDraft.trim();
    if (!body) return;
    setPostingComment(true);
    try {
      await addKanbanComment(storedToken || undefined, taskId, body, board);
      setCommentDraft('');
      const d = await loadKanbanTaskDetail(storedToken || undefined, taskId, board);
      setDetail(d);
    } finally {
      setPostingComment(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-end sm:items-center justify-center bg-black/40 backdrop-blur-sm" role="dialog" aria-modal="true" aria-label={`Task ${detail?.title ?? taskId}`} onClick={onClose}>
      <div
        className="kanban-drawer w-full sm:max-w-lg max-h-[88dvh] overflow-y-auto rounded-t-xl sm:rounded-xl border border-border-subtle bg-surface p-4 pb-[max(1rem,env(safe-area-inset-bottom))] shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        {!detail ? (
          <p className="text-sm text-text-muted py-8 text-center">{t('kanban.loadingTask')}</p>
        ) : (
          <>
            {/* Header */}
            <div className="flex items-start justify-between gap-3">
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2 flex-wrap">
                  {detail.priority != null && detail.priority > 0 ? (
                    <span className={`inline-flex items-center rounded px-1.5 py-px text-[10px] font-semibold ${(PRIORITY_COLORS[detail.priority] ?? PRIORITY_COLORS[0]).bg} ${(PRIORITY_COLORS[detail.priority] ?? PRIORITY_COLORS[0]).text}`}>P{detail.priority}</span>
                  ) : null}
                  <span className="text-[10px] font-mono text-text-subtle">{shortId(detail.id)}</span>
                </div>
                <h2 className="mt-1 text-sm font-semibold text-text">{detail.title}</h2>
                <div className="mt-0.5 flex items-center gap-2 text-[10px] text-text-subtle">
                  <span className="uppercase tracking-wide">{detail.status}</span>
                  {detail.assignee ? <span>{'@' + detail.assignee}</span> : null}
                  {formatAge(detail.created_at) ? <span>{formatAge(detail.created_at)}</span> : null}
                </div>
              </div>
              <Button variant="ghost" size="sm" aria-label={t('kanban.closeTask')} onClick={onClose}>
                <X size={14} />
              </Button>
            </div>

            {/* Status actions */}
            <div className="mt-3 flex flex-wrap items-center gap-1.5 rounded-lg border border-border-subtle bg-surface-sunken p-2">
              <span className="mr-1 text-[10px] font-semibold uppercase tracking-wide text-text-subtle">{t('kanban.actions')}</span>
              {detail.status !== 'ready' && detail.status !== 'done' ? <Button size="sm" disabled={actionBusy} onClick={() => void runAction('ready')}>{t('kanban.ready')}</Button> : null}
              {detail.status !== 'blocked' && detail.status !== 'done' ? <Button size="sm" disabled={actionBusy} onClick={() => void runAction('blocked')}>{t('kanban.block')}</Button> : null}
              {detail.status !== 'done' ? <Button size="sm" disabled={actionBusy} onClick={() => void runAction('done')}>{t('kanban.complete')}</Button> : null}
              <Button variant="ghost" size="sm" disabled={actionBusy} onClick={() => void runAction('archive')}>{t('kanban.archive')}</Button>
            </div>

            {/* Editable metadata */}
            <section className="mt-3 grid grid-cols-2 gap-3 rounded-lg border border-border-subtle bg-surface-sunken p-2.5">
              <label className="block">
                <span className="text-[10px] font-semibold uppercase tracking-wide text-text-subtle">{t('kanban.assignee')}</span>
                <div className="mt-1 flex gap-1">
                  <input value={assigneeDraft} onChange={(e) => setAssigneeDraft(e.target.value)} placeholder="blank = dispatcher" className="min-w-0 flex-1 rounded-md border border-border-subtle bg-surface px-2 py-1.5 text-xs text-text placeholder:text-text-subtle focus:border-border focus:outline-none" />
                  <Button size="sm" disabled={actionBusy} onClick={() => void saveMetadata({ assignee: assigneeDraft.trim() || null })}>Save</Button>
                </div>
              </label>
              <label className="block">
                <span className="text-[10px] font-semibold uppercase tracking-wide text-text-subtle">Priority</span>
                <div className="mt-1"><Dropdown value={String(detail.priority ?? 0)} onChange={(v) => void saveMetadata({ priority: Number(v) })} ariaLabel="Edit task priority" dropUp options={[{ value: '0', label: 'Normal' }, { value: '1', label: 'P1 — High' }, { value: '2', label: 'P2 — Medium' }, { value: '3', label: 'P3 — Low' }, { value: '-1', label: 'P4 — Lowest' }]} /></div>
              </label>
            </section>

            {/* Dependencies */}
            <section className="mt-3 rounded-lg border border-border-subtle bg-surface-sunken p-2.5">
              <h3 className="text-[10px] font-semibold uppercase tracking-wide text-text-subtle">{t('kanban.dependencies')}</h3>
              {(detail.parents?.length ?? 0) > 0 ? <div className="mt-1 space-y-1">{detail.parents!.map((parentId) => <div key={parentId} className="flex items-center justify-between text-[10px] text-text-muted"><code>{parentId}</code><button type="button" className="text-text-subtle hover:text-red-400" onClick={() => void removeParent(parentId)}>remove</button></div>)}</div> : <p className="mt-1 text-[10px] text-text-subtle">{t('kanban.noParentTasks')}</p>}
              <div className="mt-2 flex gap-1"><input value={parentDraft} onChange={(e) => setParentDraft(e.target.value)} placeholder="Parent task ID" className="min-w-0 flex-1 rounded-md border border-border-subtle bg-surface px-2 py-1.5 text-xs text-text placeholder:text-text-subtle focus:border-border focus:outline-none" /><Button size="sm" disabled={!parentDraft.trim() || actionBusy} onClick={() => void addParent()}>Add parent</Button></div>
            </section>

            {/* Body */}
            {detail.body ? (
              <p className="mt-3 whitespace-pre-wrap text-xs text-text-muted leading-relaxed">{detail.body}</p>
            ) : null}

            {/* Summary */}
            {detail.latest_summary ? (
              <div className="mt-3 rounded-md border border-border-subtle bg-surface-sunken p-2">
                <span className="text-[10px] uppercase tracking-wide text-text-subtle">Latest summary</span>
                <p className="mt-1 whitespace-pre-wrap text-xs text-text-muted leading-relaxed line-clamp-6">{detail.latest_summary}</p>
              </div>
            ) : null}

            {/* Result / final summary */}
            {(detail.result || detail.latest_summary) ? (
              <section className="mt-4 rounded-lg border border-border-subtle bg-surface-sunken p-3">
                <h3 className="text-[10px] font-semibold uppercase tracking-wide text-text-subtle">{detail.result ? 'Result' : 'Final result (run summary)'}</h3>
                <p className="mt-1 whitespace-pre-wrap text-xs text-text-muted leading-relaxed">{detail.result || detail.latest_summary}</p>
              </section>
            ) : detail.status === 'done' ? (
              <section className="mt-4 rounded-lg border border-border-subtle bg-surface-sunken p-3 text-xs text-text-subtle">No final result recorded. Check Run history, Worker log, or Events.</section>
            ) : null}

            {/* Child results / dependencies */}
            {(detail.children?.length ?? 0) > 0 ? (
              <section className="mt-4">
                <h3 className="text-[10px] font-semibold uppercase tracking-wide text-text-subtle">Child tasks ({detail.children!.length})</h3>
                <div className="mt-1.5 space-y-1">
                  {detail.children!.map((childId) => (
                    <div key={childId} className="flex items-center justify-between rounded-md border border-border-subtle bg-surface/60 px-2 py-1.5">
                      <code className="text-[10px] text-text-muted">{childId}</code>
                      <span className="text-[10px] text-text-subtle">Open from the board</span>
                    </div>
                  ))}
                </div>
              </section>
            ) : null}

            {/* Run history */}
            {detail.runs.length > 0 ? (
              <section className="mt-4">
                <h3 className="text-[10px] font-semibold uppercase tracking-wide text-text-subtle">Run history ({detail.runs.length})</h3>
                <div className="mt-1.5 space-y-1.5">
                  {detail.runs.map((run) => {
                    const elapsed = run.started_at ? Math.max(0, (run.ended_at ?? Date.now() / 1000) - run.started_at) : 0;
                    const elapsedLabel = elapsed < 60 ? `${Math.round(elapsed)}s` : `${Math.round(elapsed / 60)}m`;
                    return (
                      <div key={run.id} className={`rounded-md border p-2 ${run.ended_at ? 'border-border-subtle bg-surface/60' : 'border-sky-400/40 bg-sky-400/5'}`}>
                        <div className="flex items-center gap-2 text-[10px]">
                          <span className="font-medium text-text">{run.ended_at ? (run.outcome || run.status || 'ended') : 'active'}</span>
                          <span className="text-text-muted">{run.profile ? `@${run.profile}` : '(no profile)'}</span>
                          <span className="ml-auto text-text-subtle">{elapsedLabel} · {formatAge(run.started_at)}</span>
                        </div>
                        {run.summary ? <p className="mt-1 whitespace-pre-wrap text-xs text-text-muted">{run.summary}</p> : null}
                        {run.error ? <p className="mt-1 whitespace-pre-wrap text-xs text-red-400">{run.error}</p> : null}
                      </div>
                    );
                  })}
                </div>
              </section>
            ) : null}

            {/* Events / activity timeline */}
            {detail.events.length > 0 ? (
              <details className="mt-4" open={detail.status === 'running'}>
                <summary className="cursor-pointer text-[10px] font-semibold uppercase tracking-wide text-text-subtle">Events ({detail.events.length})</summary>
                <div className="mt-1.5 space-y-1">
                  {detail.events.slice(0, 20).map((event) => (
                    <div key={event.id} className="rounded-md border border-border-subtle bg-surface/60 p-2">
                      <div className="flex items-center justify-between text-[10px]"><code className="text-text-muted">{event.kind}</code><span className="text-text-subtle">{formatAge(event.created_at)}</span></div>
                      {event.payload ? <pre className="mt-1 max-h-32 overflow-auto whitespace-pre-wrap break-all text-[10px] text-text-subtle">{JSON.stringify(event.payload, null, 2)}</pre> : null}
                    </div>
                  ))}
                </div>
              </details>
            ) : null}

            {/* Worker log */}
            <details className="mt-4" open={detail.status === 'running'}>
              <summary className="cursor-pointer text-[10px] font-semibold uppercase tracking-wide text-text-subtle">Worker log{workerLog?.size_bytes ? ` (${workerLog.size_bytes} B)` : ''}</summary>
              <div className="mt-1.5">
                {logLoading ? <p className="text-xs text-text-subtle">Loading log…</p> : logError ? <p className="text-xs text-red-400">{logError}</p> : workerLog?.exists ? <pre className="max-h-64 overflow-auto rounded-md border border-border-subtle bg-black/30 p-2 font-mono text-[10px] leading-relaxed text-text-muted">{workerLog.content || '(empty)'}</pre> : <p className="text-xs italic text-text-subtle">— no worker log yet —</p>}
                {workerLog?.truncated ? <p className="mt-1 text-[10px] text-text-subtle">Showing the last 100 KB.</p> : null}
              </div>
            </details>

            {/* Comments */}
            {detail.comments.length > 0 ? (
              <div className="mt-4">
                <span className="text-[10px] font-semibold uppercase tracking-wide text-text-subtle">Comments ({detail.comments.length})</span>
                <ul className="mt-1.5 space-y-2">
                  {detail.comments.map((c) => (
                    <li key={c.id} className="rounded-md border border-border-subtle bg-surface/60 p-2">
                      <div className="flex items-center justify-between text-[10px] text-text-subtle">
                        <span>{c.author || 'unknown'}</span>
                        <span>{formatAge(c.created_at) ?? '—'}</span>
                      </div>
                      <p className="mt-1 whitespace-pre-wrap text-xs text-text-muted leading-relaxed">{c.body}</p>
                    </li>
                  ))}
                </ul>
              </div>
            ) : null}

            {/* New comment */}
            <form
              className="mt-4 flex gap-2"
              onSubmit={(e) => { e.preventDefault(); void postComment(); }}
            >
              <input
                value={commentDraft}
                onChange={(e) => setCommentDraft(e.target.value)}
                placeholder="Add a comment…"
                className="flex-1 rounded-lg border border-border-subtle bg-surface-sunken px-2.5 py-2 text-xs text-text placeholder:text-text-subtle focus:border-border focus:outline-none"
              />
              <Button type="submit" size="sm" disabled={!commentDraft.trim() || postingComment}>
                {postingComment ? '…' : 'Send'}
              </Button>
            </form>
          </>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// BoardPicker (custom dropdown)
// ---------------------------------------------------------------------------

function BoardPicker({
  boards,
  activeBoard,
  onSelect,
  onDeleteRequest,
}: {
  boards: MissionControlKanbanBoardMeta[];
  activeBoard: string;
  onSelect: (slug: string) => void;
  onDeleteRequest: (slug: string) => void;
}) {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, [open]);

  const active = boards.find((b) => b.slug === activeBoard) ?? boards[0];

  return (
    <div className="relative" ref={ref}>
      <button
        type="button"
        className="flex items-center gap-1.5 rounded-lg border border-border-subtle bg-surface px-2 py-1.5 text-xs text-text hover:border-border transition-colors"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        aria-haspopup="listbox"
      >
        <span className="truncate max-w-[10rem]">{active?.name || activeBoard}</span>
        <span className={`h-1.5 w-1.5 rounded-full shrink-0 ${active?.is_current ? 'bg-emerald-400' : 'bg-border-subtle'}`} title={active?.is_current ? 'Active board (CLI/gateway)' : undefined} />
        <ChevronDown size={13} className={`shrink-0 text-text-subtle transition-transform ${open ? 'rotate-180' : ''}`} />
      </button>
      {open ? (
        <ul className="absolute z-30 mt-1 min-w-[14rem] max-w-[20rem] rounded-xl border border-border-subtle bg-surface p-1 shadow-xl" role="listbox" aria-label={t('kanban.selectBoard')}>
          {boards.map((b) => {
            const selected = b.slug === activeBoard;
            const deletable = b.slug !== 'default' && boards.filter((x) => !x.archived).length > 1;
            return (
              <li key={b.slug} role="option" aria-selected={selected}>
                <div className={`flex w-full items-center gap-2 rounded-lg px-2.5 transition-colors ${selected ? 'bg-sky-400/10' : 'hover:bg-surface-sunken'}`}>
                  <button type="button" className={`flex flex-1 items-center gap-2 py-2 text-left text-xs ${selected ? 'text-text font-medium' : 'text-text-muted hover:text-text'}`} onClick={() => { onSelect(b.slug); setOpen(false); }}>
                    <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${b.is_current ? 'bg-emerald-400' : 'bg-transparent border border-border-subtle'}`} title={b.is_current ? 'Active board (CLI/gateway)' : undefined} />
                    <span className="truncate flex-1">{b.name || b.slug}</span>
                  </button>
                  <span className="rounded-full bg-surface-sunken px-1.5 py-px text-[10px] tabular-nums text-text-subtle">{typeof b.total === 'number' ? b.total : ''}</span>
                  <button type="button" className={`rounded p-1 text-text-subtle hover:bg-red-500/10 hover:text-red-400 disabled:pointer-events-none disabled:opacity-0 ${deletable ? '' : 'invisible'}`} style={{ width: 22, height: 22, display: 'inline-flex', alignItems: 'center', justifyContent: 'center' }} aria-label={`Delete board ${b.name || b.slug}`} title={deletable ? 'Delete board' : 'The default board cannot be deleted'} disabled={!deletable} onClick={() => { setOpen(false); onDeleteRequest(b.slug); }}>
                    <Trash2 size={12} />
                  </button>
                </div>
              </li>
            );
          })}
        </ul>
      ) : null}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Dropdown — shared select-style component (same look as the board picker)
// ---------------------------------------------------------------------------

function Dropdown({
  value,
  options,
  onChange,
  placeholder = 'All',
  ariaLabel,
  dropUp = false,
}: {
  value: string;
  options: { value: string; label: string }[];
  onChange: (value: string) => void;
  placeholder?: string;
  ariaLabel: string;
  dropUp?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, [open]);

  const activeLabel = options.find((o) => o.value === value)?.label ?? placeholder;

  return (
    <div className="relative" ref={ref}>
      <button
        type="button"
        className={`flex w-full items-center justify-between gap-1.5 rounded-lg border px-2.5 py-2 text-xs transition-colors ${value ? 'border-border bg-surface-sunken text-text' : 'border-border-subtle bg-surface text-text-muted hover:border-border hover:text-text'}`}
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        aria-haspopup="listbox"
        aria-label={ariaLabel}
      >
        <span className="truncate">{activeLabel}</span>
        <ChevronDown size={13} className={`shrink-0 text-text-subtle transition-transform ${open ? 'rotate-180' : ''}`} />
      </button>
      {open ? (
        <ul className={`absolute z-30 min-w-[10rem] rounded-xl border border-border-subtle bg-surface p-1 shadow-xl ${dropUp ? 'bottom-full mb-1' : 'mt-1'}`} role="listbox" aria-label={ariaLabel}>
          {options.map((o) => {
            const selected = o.value === value;
            return (
              <li key={o.value || '__all__'} role="option" aria-selected={selected}>
                <button
                  type="button"
                  className={`flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-left text-xs transition-colors ${selected ? 'bg-sky-400/10 text-text font-medium' : 'text-text-muted hover:bg-surface-sunken hover:text-text'}`}
                  onClick={() => { onChange(o.value); setOpen(false); }}
                >
                  <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${selected ? 'bg-sky-400' : 'bg-transparent'}`} />
                  <span className="truncate flex-1">{o.label}</span>
                </button>
              </li>
            );
          })}
        </ul>
      ) : null}
    </div>
  );
}

// ---------------------------------------------------------------------------
// KanbanRoute (main component)
// ---------------------------------------------------------------------------

export function KanbanRoute() {
  const { storedToken } = useMissionControl();
  const { t } = useI18n();
  const [boards, setBoards] = useState<MissionControlKanbanBoardMeta[]>([]);
  const [activeBoard, setActiveBoard] = useState('');
  const [board, setBoard] = useState<MissionControlKanbanBoard | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);

  // Filters
  const [searchQuery, setSearchQuery] = useState('');
  const [filterTenant, setFilterTenant] = useState('');
  const [filterAssignee, setFilterAssignee] = useState('');

  // Task interactions
  const [openTaskId, setOpenTaskId] = useState<string | null>(null);
  const [newTaskStatus, setNewTaskStatus] = useState<string | null>(null);
  const [newTaskTitle, setNewTaskTitle] = useState('');
  const [newTaskBody, setNewTaskBody] = useState('');
  const [newTaskPriority, setNewTaskPriority] = useState(0);
  const [newTaskSpecifier, setNewTaskSpecifier] = useState('');
  const [newTaskSkills, setNewTaskSkills] = useState('');
  const [newTaskWorkspaceKind, setNewTaskWorkspaceKind] = useState('scratch');
  const [newTaskWorkspacePath, setNewTaskWorkspacePath] = useState('');
  const [newTaskParent, setNewTaskParent] = useState('');
  const [newTaskGoalMode, setNewTaskGoalMode] = useState(false);
  const [creating, setCreating] = useState(false);

  // Board management
  const [newBoardOpen, setNewBoardOpen] = useState(false);
  const [newBoardSlug, setNewBoardSlug] = useState('');
  const [newBoardName, setNewBoardName] = useState('');
  const [newBoardDesc, setNewBoardDesc] = useState('');
  const [newBoardWorkdir, setNewBoardWorkdir] = useState('');
  const [newBoardIcon, setNewBoardIcon] = useState('');
  const [newBoardSwitch, setNewBoardSwitch] = useState(true);
  const [creatingBoard, setCreatingBoard] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<MissionControlKanbanBoardMeta | null>(null);
  const [deleteHard, setDeleteHard] = useState(false);
  const [deletingBoard, setDeletingBoard] = useState(false);

  // --- Board list + board load ---
  const refresh = useCallback(async (boardSlug?: string) => {
    if (!storedToken) return;
    setRefreshing(true);
    setError(null);
    try {
      const targetSlug = boardSlug ?? activeBoard ?? undefined;
      const [boardsData, boardData] = await Promise.all([
        loadKanbanBoards(storedToken),
        loadKanbanBoard(storedToken, targetSlug),
      ]);
      setBoards(boardsData.boards);
      setActiveBoard(targetSlug || boardsData.current || '');
      setBoard(boardData);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Load failed');
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, [storedToken, activeBoard]);

  useEffect(() => { void refresh(); }, []); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!activeBoard) return;
    const t = setInterval(() => void refresh(activeBoard), 5000);
    return () => clearInterval(t);
  }, [refresh, activeBoard]);

  // --- Filtered tasks ---
  const filteredColumns = useMemo(() => {
    if (!board) return [];
    const q = searchQuery.toLowerCase().trim();
    return board.columns.map((col) => {
      let tasks = col.tasks;
      if (q) tasks = tasks.filter((t) => t.title.toLowerCase().includes(q) || t.id.includes(q) || (t.assignee ?? '').toLowerCase().includes(q));
      if (filterTenant) tasks = tasks.filter((t) => ((t as MissionControlKanbanTask & { tenant?: string }).tenant ?? '') === filterTenant);
      if (filterAssignee) tasks = tasks.filter((t) => t.assignee === filterAssignee);
      return { ...col, tasks };
    });
  }, [board, searchQuery, filterTenant, filterAssignee]);

  // Tenants + assignees from the board payload (sidecar computes them)
  const tenants = board?.tenants ?? [];
  const assignees = board?.assignees ?? [];

  const hasFilters = searchQuery || filterTenant || filterAssignee;
  const totalVisible = filteredColumns.reduce((s, c) => s + c.tasks.length, 0);
  const totalAll = board?.columns.reduce((s, c) => s + c.tasks.length, 0) ?? 0;

  // --- Move task (optimistic) ---
  const handleMove = useCallback(async (taskId: string, newStatus: string) => {
    if (!board || !storedToken) return;
    setBoard((prev) => {
      if (!prev) return prev;
      let movedTask: MissionControlKanbanTask | null = null;
      const cols = prev.columns.map((c) => {
        const idx = c.tasks.findIndex((t) => t.id === taskId);
        if (idx >= 0) {
          movedTask = { ...c.tasks[idx], status: newStatus };
          return { ...c, tasks: c.tasks.filter((t) => t.id !== taskId) };
        }
        return c;
      });
      if (!movedTask) return prev;
      return {
        ...prev,
        columns: cols.map((c) => c.name === newStatus ? { ...c, tasks: [...c.tasks, movedTask!] } : c),
      };
    });
    try {
      await moveKanbanTask(storedToken, taskId, newStatus);
    } catch {
      void refresh(activeBoard);
    }
  }, [board, storedToken, refresh, activeBoard]);

  // --- Create task ---
  const closeNewTask = () => { setNewTaskStatus(null); setNewTaskTitle(''); setNewTaskBody(''); setNewTaskPriority(0); setNewTaskSpecifier(''); setNewTaskSkills(''); setNewTaskWorkspaceKind('scratch'); setNewTaskWorkspacePath(''); setNewTaskParent(''); setNewTaskGoalMode(false); setCreating(false); };

  const submitNewTask = async () => {
    const title = newTaskTitle.trim();
    if (!title || !newTaskStatus) return;
    setCreating(true);
    try {
      const result = await createKanbanTask(storedToken || undefined, {
        title,
        body: newTaskBody.trim() || undefined,
        priority: newTaskPriority,
        status: newTaskStatus,
        assignee: newTaskSpecifier.trim() || undefined,
        skills: newTaskSkills.trim() || undefined,
        workspace_kind: newTaskWorkspaceKind,
        workspace_path: newTaskWorkspacePath.trim() || undefined,
        parents: newTaskParent || undefined,
        goal_mode: newTaskGoalMode || undefined,
      });
      if (result?.id) {
        setBoard((prev) => {
          if (!prev) return prev;
      const newTask: MissionControlKanbanTask = {
        id: result.id,
        title,
        priority: newTaskPriority,
        status: newTaskStatus,
        created_at: Date.now() / 1000,
        assignee: null,
        children: [],
        comment_count: 0,
      };
          return { ...prev, columns: prev.columns.map((c) => c.name === newTaskStatus ? { ...c, tasks: [...c.tasks, newTask] } : c) };
        });
      }
      closeNewTask();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Create failed.');
    } finally {
      setCreating(false);
    }
  };

  // --- Create board ---
  const closeNewBoard = () => { setNewBoardOpen(false); setNewBoardSlug(''); setNewBoardName(''); setNewBoardDesc(''); setNewBoardWorkdir(''); setNewBoardIcon(''); setNewBoardSwitch(true); setCreatingBoard(false); };
  const submitNewBoard = async () => {
    const slug = newBoardSlug.trim();
    const name = newBoardName.trim();
    if (!slug && !name) return;
    setCreatingBoard(true);
    try {
      await createKanbanBoard(storedToken || undefined, {
        slug: slug || undefined,
        name: name || undefined,
        description: newBoardDesc.trim() || undefined,
        icon: newBoardIcon.trim() || undefined,
        default_workdir: newBoardWorkdir.trim() || undefined,
        switch: newBoardSwitch,
      });
      closeNewBoard();
      const { boards: list, current } = await loadKanbanBoards(storedToken || undefined);
      setBoards(list);
      setActiveBoard(current || '');
      void refresh(current || '');
    } catch (e) { setError(e instanceof Error ? e.message : 'Create board failed.'); }
    finally { setCreatingBoard(false); }
  };

  // --- Delete board ---
  const closeDeleteBoard = () => { setDeleteTarget(null); setDeleteHard(false); setDeletingBoard(false); };
  const confirmDeleteBoard = async () => {
    if (!deleteTarget) return;
    setDeletingBoard(true);
    try {
      await deleteKanbanBoard(storedToken || undefined, deleteTarget.slug, deleteHard);
      closeDeleteBoard();
      const { boards: list, current } = await loadKanbanBoards(storedToken || undefined);
      setBoards(list);
      setActiveBoard((prev) => (list.some((b) => b.slug === prev) ? prev : current || ''));
      void refresh(current || '');
    } catch (e) { setError(e instanceof Error ? e.message : 'Delete failed.'); }
    finally { setDeletingBoard(false); }
  };

  // --- Board switch ---
  const handleBoardSwitch = useCallback((slug: string) => {
    setActiveBoard(slug);
    void refresh(slug);
  }, [refresh]);

  // =========================================================================
  // Render
  // =========================================================================

  return (
    <div className="flex flex-1 flex-col overflow-hidden text-text" style={{ minHeight: 0 }}>
      {/* Header */}
      <div className="flex flex-col gap-2 border-b border-border-subtle px-4 py-3">
        <div className="flex items-center justify-between gap-2">
          <div className="flex items-center gap-2 min-w-0">
            <KanbanIcon size={16} className="text-sky-400 shrink-0" />
            <BoardPicker boards={boards} activeBoard={activeBoard} onSelect={handleBoardSwitch} onDeleteRequest={(slug) => { const target = boards.find((b) => b.slug === slug) ?? null; setDeleteHard(false); setDeleteTarget(target); }} />
          </div>
          <div className="flex items-center gap-2">
            {error ? <span className="text-[10px] text-red-400 truncate max-w-[14rem]" role="alert">{error}</span> : null}
            {refreshing ? <RefreshCw size={12} className="text-text-subtle animate-spin" /> : null}
            <Button size="sm" onClick={() => setNewBoardOpen(true)}>
              <Plus size={13} className="mr-1" /> New
            </Button>
          </div>
        </div>

        {/* Filter toolbar */}
        <div className="flex items-center gap-2 flex-wrap">
          <div className="relative flex-1 min-w-[10rem] max-w-[16rem]">
            <Search size={12} className="absolute left-2 top-1/2 -translate-y-1/2 text-text-subtle pointer-events-none" />
            <input value={searchQuery} onChange={(e) => setSearchQuery(e.target.value)} placeholder="Search tasks…" className="w-full rounded-lg border border-border-subtle bg-surface-sunken pl-6 pr-2 py-1.5 text-[11px] text-text placeholder:text-text-subtle focus:border-border focus:outline-none" />
          </div>
          {tenants.length > 0 ? (
            <div className="w-32">
              <Dropdown
                value={filterTenant}
                onChange={setFilterTenant}
                ariaLabel="Filter by tenant"
                placeholder="All tenants"
                options={[{ value: '', label: 'All tenants' }, ...tenants.map((t) => ({ value: t, label: t }))]}
              />
            </div>
          ) : null}
          {assignees.length > 0 ? (
            <div className="w-32">
              <Dropdown
                value={filterAssignee}
                onChange={setFilterAssignee}
                ariaLabel="Filter by assignee"
                placeholder="All assignees"
                options={[{ value: '', label: 'All assignees' }, ...assignees.map((a) => ({ value: a, label: a }))]}
              />
            </div>
          ) : null}
          {hasFilters ? (
            <button type="button" onClick={() => { setSearchQuery(''); setFilterTenant(''); setFilterAssignee(''); }} className="text-[10px] text-text-subtle hover:text-text transition-colors">
              Clear filters
            </button>
          ) : null}
          {hasFilters ? (
            <span className="text-[10px] text-text-subtle tabular-nums">{totalVisible}/{totalAll} visible</span>
          ) : null}
        </div>
      </div>

      {/* Board columns */}
      {loading ? (
        <div className="flex-1 flex items-center justify-center text-sm text-text-muted">Loading board…</div>
      ) : !board ? (
        <div className="flex-1 flex items-center justify-center text-sm text-text-muted">No board loaded.</div>
      ) : (
        <div className="kanban-scroller flex-1 overflow-x-auto overflow-y-hidden p-4" style={{ minHeight: 0 }}>
          <div className="flex gap-3 h-full items-stretch" style={{ width: 'max-content' }}>
            {filteredColumns.map((col) => (
              <BoardColumn key={col.name} name={col.name} tasks={col.tasks} activeTaskId={openTaskId} onOpenTask={setOpenTaskId} onDropTask={(id, s) => void handleMove(id, s)} onAddTask={setNewTaskStatus} />
            ))}
          </div>
        </div>
      )}

      {/* New Task Modal */}
      {newTaskStatus ? (
        <div className="fixed inset-0 z-50 flex items-end sm:items-center justify-center bg-black/40 backdrop-blur-sm" role="dialog" aria-modal="true" aria-label="New task" onClick={closeNewTask}>
          <form className="kanban-modal w-full sm:max-w-md max-h-[92dvh] overflow-y-auto rounded-t-xl sm:rounded-xl border border-border-subtle bg-surface p-4 pb-[max(1rem,env(safe-area-inset-bottom))] shadow-xl" onClick={(e) => e.stopPropagation()} onSubmit={(e) => { e.preventDefault(); void submitNewTask(); }}>
            <div className="flex items-center justify-between">
              <h2 className="text-sm font-semibold text-text">New task → {newTaskStatus}</h2>
              <Button variant="ghost" size="sm" type="button" aria-label="Cancel new task" onClick={closeNewTask}><X size={14} /></Button>
            </div>
            <label className="mt-3 block">
              <span className="text-[10px] font-semibold uppercase tracking-wide text-text-subtle">Column</span>
              <div className="mt-1 flex flex-wrap gap-1.5">
                {board?.columns.map((col) => {
                  const active = col.name === newTaskStatus;
                  const tone = STATUS_TONES[col.name] ?? 'text-text-muted';
                  return <button key={col.name} type="button" onClick={() => setNewTaskStatus(col.name)} className={`rounded-lg border px-2 py-1 text-[10px] font-medium transition-colors ${active ? 'border-sky-400/50 bg-sky-400/10 text-text' : `border-border-subtle text-text-subtle hover:border-border hover:text-text`} ${tone}`}>{col.name}</button>;
                })}
              </div>
            </label>
            <label className="mt-3 block">
              <span className="text-[10px] font-semibold uppercase tracking-wide text-text-subtle">Title <span className="text-red-400">*</span></span>
              <input autoFocus value={newTaskTitle} onChange={(e) => setNewTaskTitle(e.target.value)} placeholder="e.g. Implement vault fallback" maxLength={200} className="mt-1 w-full rounded-lg border border-border-subtle bg-surface-sunken px-2.5 py-2 text-xs text-text placeholder:text-text-subtle focus:border-border focus:outline-none" />
            </label>
            <div className="mt-3 grid grid-cols-2 gap-3">
              <label className="block">
                <span className="text-[10px] font-semibold uppercase tracking-wide text-text-subtle">Specifier</span>
                <input value={newTaskSpecifier} onChange={(e) => setNewTaskSpecifier(e.target.value)} placeholder="blank = auto" className="mt-1 w-full rounded-lg border border-border-subtle bg-surface-sunken px-2.5 py-2 text-xs text-text placeholder:text-text-subtle focus:border-border focus:outline-none" />
              </label>
              <label className="block">
                <span className="text-[10px] font-semibold uppercase tracking-wide text-text-subtle">Priority</span>
                <div className="mt-1">
                  <Dropdown
                    value={String(newTaskPriority)}
                    onChange={(v) => setNewTaskPriority(Number(v))}
                    ariaLabel="Task priority"
                    dropUp
                    options={[
                      { value: '0', label: 'Normal' },
                      { value: '1', label: 'P1 — High' },
                      { value: '2', label: 'P2 — Medium' },
                      { value: '3', label: 'P3 — Low' },
                      { value: '-1', label: 'P4 — Lowest' },
                    ]}
                  />
                </div>
              </label>
            </div>
            <label className="mt-3 block">
              <span className="text-[10px] font-semibold uppercase tracking-wide text-text-subtle">Skills</span>
              <input value={newTaskSkills} onChange={(e) => setNewTaskSkills(e.target.value)} placeholder="comma-separated, e.g. translation, review" spellCheck={false} className="mt-1 w-full rounded-lg border border-border-subtle bg-surface-sunken px-2.5 py-2 text-xs text-text placeholder:text-text-subtle focus:border-border focus:outline-none" />
            </label>
            <div className="mt-3 grid grid-cols-2 gap-3">
              <label className="block">
                <span className="text-[10px] font-semibold uppercase tracking-wide text-text-subtle">Workspace</span>
                <div className="mt-1">
                  <Dropdown
                    value={newTaskWorkspaceKind}
                    onChange={setNewTaskWorkspaceKind}
                    ariaLabel="Workspace kind"
                    dropUp
                    options={[
                      { value: 'scratch', label: 'Scratch — ephemeral' },
                      { value: 'worktree', label: 'Git worktree — preserved' },
                      { value: 'dir', label: 'Directory — persistent' },
                    ]}
                  />
                </div>
              </label>
              <label className="block">
                <span className="text-[10px] font-semibold uppercase tracking-wide text-text-subtle">Path</span>
                <input value={newTaskWorkspacePath} onChange={(e) => setNewTaskWorkspacePath(e.target.value)} placeholder="defaults to board workdir" spellCheck={false} className="mt-1 w-full rounded-lg border border-border-subtle bg-surface-sunken px-2.5 py-2 font-mono text-xs text-text placeholder:text-text-subtle focus:border-border focus:outline-none" />
              </label>
            </div>
            <div className="mt-3 grid grid-cols-2 gap-3 items-end">
              <label className="block">
                <span className="text-[10px] font-semibold uppercase tracking-wide text-text-subtle">Parent task</span>
                {(() => {
                  const allTasks = (board?.columns ?? []).flatMap((c) => c.tasks);
                  const parentOptions = [{ value: '', label: '— no parent —' }, ...allTasks.map((t) => ({ value: t.id, label: `${t.title.slice(0, 40)}${t.title.length > 40 ? '…' : ''}` }))];
                  return (
                    <div className="mt-1">
                      <Dropdown value={newTaskParent} onChange={setNewTaskParent} ariaLabel="Parent task" dropUp options={parentOptions} />
                    </div>
                  );
                })()}
              </label>
              <label className="flex items-center gap-2 pb-2">
                <input type="checkbox" checked={newTaskGoalMode} onChange={(e) => setNewTaskGoalMode(e.target.checked)} className="h-3.5 w-3.5 accent-sky-500" />
                <span className="text-xs text-text-muted">goal mode</span>
              </label>
            </div>
            <p className="mt-2 text-[10px] text-text-subtle">A child task stays blocked until its parent is done.</p>
            <label className="mt-3 block">
              <span className="text-[10px] font-semibold uppercase tracking-wide text-text-subtle">Description</span>
              <textarea value={newTaskBody} onChange={(e) => setNewTaskBody(e.target.value)} placeholder="Rough idea — AI will spec it…" rows={2} className="mt-1 w-full resize-y rounded-lg border border-border-subtle bg-surface-sunken px-2.5 py-2 text-xs text-text placeholder:text-text-subtle focus:border-border focus:outline-none min-h-[3rem]" />
            </label>
            <div className="mt-3 flex items-center justify-end gap-2">
              <Button variant="ghost" size="sm" type="button" onClick={closeNewTask}>Cancel</Button>
              <Button type="submit" size="sm" disabled={!newTaskTitle.trim() || creating}>{creating ? 'Creating…' : 'Create'}</Button>
            </div>
          </form>
        </div>
      ) : null}

      {/* New Board Modal */}
      {newBoardOpen ? (
        <div className="fixed inset-0 z-50 flex items-end sm:items-center justify-center bg-black/40 backdrop-blur-sm" role="dialog" aria-modal="true" aria-label="New board" onClick={closeNewBoard}>
          <form className="kanban-modal w-full sm:max-w-md max-h-[92dvh] overflow-y-auto rounded-t-xl sm:rounded-xl border border-border-subtle bg-surface p-4 pb-[max(1rem,env(safe-area-inset-bottom))] shadow-xl" onClick={(e) => e.stopPropagation()} onSubmit={(e) => { e.preventDefault(); void submitNewBoard(); }}>
            <div className="flex items-center justify-between">
              <h2 className="text-sm font-semibold text-text">{t('kanban.newBoard')}</h2>
              <Button variant="ghost" size="sm" type="button" aria-label="Cancel new board" onClick={closeNewBoard}><X size={14} /></Button>
            </div>
            <p className="mt-1 text-[10px] text-text-muted">{t('kanban.newBoardSubtitle')}</p>
            <label className="mt-3 block">
              <span className="text-[10px] font-semibold uppercase tracking-wide text-text-subtle">Slug — lowercase, hyphens <span className="text-red-400">*</span></span>
              <input
                autoFocus
                value={newBoardSlug}
                onChange={(e) => setNewBoardSlug(e.target.value)}
                placeholder="atm10-server"
                maxLength={64}
                className="mt-1 w-full rounded-lg border border-border-subtle bg-surface-sunken px-2.5 py-2 font-mono text-xs text-text placeholder:text-text-subtle focus:border-border focus:outline-none"
              />
            </label>
            <label className="mt-3 block">
              <span className="text-[10px] font-semibold uppercase tracking-wide text-text-subtle">Display name (optional)</span>
              <input
                value={newBoardName}
                onChange={(e) => setNewBoardName(e.target.value)}
                placeholder="ATM10 Server"
                maxLength={80}
                className="mt-1 w-full rounded-lg border border-border-subtle bg-surface-sunken px-2.5 py-2 text-xs text-text placeholder:text-text-subtle focus:border-border focus:outline-none"
              />
            </label>
            <label className="mt-3 block">
              <span className="text-[10px] font-semibold uppercase tracking-wide text-text-subtle">Description (optional)</span>
              <textarea
                value={newBoardDesc}
                onChange={(e) => setNewBoardDesc(e.target.value)}
                placeholder="What goes on this board?"
                rows={2}
                className="mt-1 w-full resize-y rounded-lg border border-border-subtle bg-surface-sunken px-2.5 py-2 text-xs text-text placeholder:text-text-subtle focus:border-border focus:outline-none min-h-[3rem]"
              />
            </label>
            <label className="mt-3 block">
              <span className="text-[10px] font-semibold uppercase tracking-wide text-text-subtle">Project directory (recommended)</span>
              <input
                value={newBoardWorkdir}
                onChange={(e) => setNewBoardWorkdir(e.target.value)}
                placeholder="/Users/albi/Projects/my-project"
                spellCheck={false}
                className="mt-1 w-full rounded-lg border border-border-subtle bg-surface-sunken px-2.5 py-2 font-mono text-xs text-text placeholder:text-text-subtle focus:border-border focus:outline-none"
              />
              <span className="mt-1 block text-[10px] text-text-subtle">Sets the default location for task files so project output is preserved.</span>
            </label>
            <label className="mt-3 block">
              <span className="text-[10px] font-semibold uppercase tracking-wide text-text-subtle">Icon (single character or emoji)</span>
              <input
                value={newBoardIcon}
                onChange={(e) => setNewBoardIcon(e.target.value)}
                placeholder="📦"
                maxLength={4}
                className="mt-1 w-20 rounded-lg border border-border-subtle bg-surface-sunken px-2.5 py-2 text-center text-xs text-text placeholder:text-text-subtle focus:border-border focus:outline-none"
              />
            </label>
            <label className="mt-3 flex items-center gap-2">
              <input type="checkbox" checked={newBoardSwitch} onChange={(e) => setNewBoardSwitch(e.target.checked)} className="h-3.5 w-3.5 accent-sky-500" />
              <span className="text-xs text-text-muted">Switch to this board after creating it</span>
            </label>
            <div className="mt-3 flex items-center justify-end gap-2">
              <Button variant="ghost" size="sm" type="button" onClick={closeNewBoard}>Cancel</Button>
              <Button type="submit" size="sm" disabled={(!newBoardSlug.trim() && !newBoardName.trim()) || creatingBoard}>{creatingBoard ? 'Creating…' : 'Create board'}</Button>
            </div>
          </form>
        </div>
      ) : null}

      {/* Delete Board Confirmation */}
      {deleteTarget ? (
        <div className="fixed inset-0 z-50 flex items-end sm:items-center justify-center bg-black/40 backdrop-blur-sm" role="alertdialog" aria-modal="true" aria-label={`Delete board ${deleteTarget.name || deleteTarget.slug}`} onClick={closeDeleteBoard}>
          <form className="kanban-modal w-full sm:max-w-sm max-h-[92dvh] overflow-y-auto rounded-t-xl sm:rounded-xl border border-border-subtle bg-surface p-4 pb-[max(1rem,env(safe-area-inset-bottom))] shadow-xl" onClick={(e) => e.stopPropagation()} onSubmit={(e) => { e.preventDefault(); void confirmDeleteBoard(); }}>
            <h2 className="text-sm font-semibold text-text">Delete board "{deleteTarget.name || deleteTarget.slug}"?</h2>
            <p className="mt-1 text-xs text-text-muted">{typeof deleteTarget.total === 'number' && deleteTarget.total > 0 ? `${deleteTarget.total} task${deleteTarget.total === 1 ? '' : 's'} on this board.` : 'This board has no tasks.'}</p>
            <label className="mt-3 flex items-start gap-2 rounded-lg border border-red-500/30 bg-red-500/5 p-2.5">
              <input type="checkbox" checked={deleteHard} onChange={(e) => setDeleteHard(e.target.checked)} className="mt-0.5 h-3.5 w-3.5 accent-red-500" />
              <span className="text-xs text-text-muted"><span className="font-medium text-text">Permanently delete</span> — unchecked, the board is archived and can be restored later; checked, the board and its database are gone for good.</span>
            </label>
            <div className="mt-3 flex items-center justify-end gap-2">
              <Button variant="ghost" size="sm" type="button" onClick={closeDeleteBoard}>Cancel</Button>
              <Button type="submit" size="sm" disabled={deletingBoard} className={deleteHard ? 'bg-red-500 hover:bg-red-600' : ''}>{deletingBoard ? 'Deleting…' : deleteHard ? 'Delete permanently' : 'Archive board'}</Button>
            </div>
          </form>
        </div>
      ) : null}

      {/* Task Drawer */}
      {openTaskId ? <TaskDrawer taskId={openTaskId} board={activeBoard} onClose={() => setOpenTaskId(null)} onChanged={() => void refresh(activeBoard)} /> : null}
    </div>
  );
}
