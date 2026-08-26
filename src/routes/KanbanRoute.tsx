import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Kanban as KanbanIcon, RefreshCw, Plus, X, MessageSquare, GitBranch, Trash2, Search, ChevronDown } from 'lucide-react';
import { Card } from '../components/ui/Card';
import { Button } from '../components/ui/Button';
import {
  loadKanbanBoard,
  loadKanbanBoards,
  loadKanbanTaskDetail,
  moveKanbanTask,
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
  const [dragOver, setDragOver] = useState(false);
  const hint = COLUMN_HINTS[name];

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
          <button type="button" className="flex h-5 w-5 items-center justify-center rounded p-0 text-text-subtle hover:text-text hover:bg-surface-sunken" aria-label={`Add task to ${name}`} onClick={() => onAddTask(name)}>
            <Plus size={13} />
          </button>
        </div>
      </div>
      {hint ? <p className="text-[10px] text-text-muted px-0.5 -mt-1">{hint}</p> : null}

      {/* Task cards */}
      <div className="flex sm:flex-col gap-2 overflow-x-auto sm:overflow-x-visible sm:overflow-y-auto pb-1" role="list">
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
            — no tasks —
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
  onClose,
}: {
  taskId: string;
  onClose: () => void;
}) {
  const { storedToken } = useMissionControl();
  const [detail, setDetail] = useState<MissionControlKanbanTaskDetail | null>(null);
  const [commentDraft, setCommentDraft] = useState('');
  const [postingComment, setPostingComment] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setDetail(null);
    loadKanbanTaskDetail(storedToken || undefined, taskId)
      .then((d) => { if (!cancelled) setDetail(d); })
      .catch(() => { if (!cancelled) setDetail(null); });
    return () => { cancelled = true; };
  }, [storedToken, taskId]);

  const postComment = async () => {
    const body = commentDraft.trim();
    if (!body) return;
    setPostingComment(true);
    try {
      await addKanbanComment(storedToken || undefined, taskId, body);
      setCommentDraft('');
      const d = await loadKanbanTaskDetail(storedToken || undefined, taskId);
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
          <p className="text-sm text-text-muted py-8 text-center">Loading task…</p>
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
              <Button variant="ghost" size="sm" aria-label="Close task" onClick={onClose}>
                <X size={14} />
              </Button>
            </div>

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
        <ul className="absolute z-30 mt-1 min-w-[14rem] max-w-[20rem] rounded-xl border border-border-subtle bg-surface p-1 shadow-xl" role="listbox" aria-label="Select board">
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
}: {
  value: string;
  options: { value: string; label: string }[];
  onChange: (value: string) => void;
  placeholder?: string;
  ariaLabel: string;
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
        className={`flex items-center gap-1.5 rounded-lg border px-2.5 py-1.5 text-[11px] transition-colors ${value ? 'border-border bg-surface-sunken text-text' : 'border-border-subtle bg-surface text-text-muted hover:border-border hover:text-text'}`}
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        aria-haspopup="listbox"
        aria-label={ariaLabel}
      >
        <span className="truncate max-w-[9rem]">{activeLabel}</span>
        <ChevronDown size={12} className={`shrink-0 text-text-subtle transition-transform ${open ? 'rotate-180' : ''}`} />
      </button>
      {open ? (
        <ul className="absolute z-30 mt-1 min-w-[10rem] rounded-xl border border-border-subtle bg-surface p-1 shadow-xl" role="listbox" aria-label={ariaLabel}>
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
  const [creating, setCreating] = useState(false);

  // Board management
  const [newBoardOpen, setNewBoardOpen] = useState(false);
  const [newBoardName, setNewBoardName] = useState('');
  const [newBoardDesc, setNewBoardDesc] = useState('');
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
  const closeNewTask = () => { setNewTaskStatus(null); setNewTaskTitle(''); setNewTaskBody(''); setNewTaskPriority(0); setCreating(false); };

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
  const closeNewBoard = () => { setNewBoardOpen(false); setNewBoardName(''); setNewBoardDesc(''); setCreatingBoard(false); };
  const submitNewBoard = async () => {
    const name = newBoardName.trim();
    if (!name) return;
    setCreatingBoard(true);
    try {
      await createKanbanBoard(storedToken || undefined, { name, description: newBoardDesc.trim() || undefined });
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
            <Dropdown
              value={filterTenant}
              onChange={setFilterTenant}
              ariaLabel="Filter by tenant"
              placeholder="All tenants"
              options={[{ value: '', label: 'All tenants' }, ...tenants.map((t) => ({ value: t, label: t }))]}
            />
          ) : null}
          {assignees.length > 0 ? (
            <Dropdown
              value={filterAssignee}
              onChange={setFilterAssignee}
              ariaLabel="Filter by assignee"
              placeholder="All assignees"
              options={[{ value: '', label: 'All assignees' }, ...assignees.map((a) => ({ value: a, label: a }))]}
            />
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
              <span className="text-[10px] font-semibold uppercase tracking-wide text-text-subtle">Title *</span>
              <input autoFocus value={newTaskTitle} onChange={(e) => setNewTaskTitle(e.target.value)} placeholder="e.g. Implement vault fallback" maxLength={200} className="mt-1 w-full rounded-lg border border-border-subtle bg-surface-sunken px-2.5 py-2 text-xs text-text placeholder:text-text-subtle focus:border-border focus:outline-none" />
            </label>
            <label className="mt-3 block">
              <span className="text-[10px] font-semibold uppercase tracking-wide text-text-subtle">Description</span>
              <textarea value={newTaskBody} onChange={(e) => setNewTaskBody(e.target.value)} placeholder="What needs to happen?" rows={3} className="mt-1 w-full resize-y rounded-lg border border-border-subtle bg-surface-sunken px-2.5 py-2 text-xs text-text placeholder:text-text-subtle focus:border-border focus:outline-none min-h-[4rem]" />
            </label>
            <label className="mt-3 block">
              <span className="text-[10px] font-semibold uppercase tracking-wide text-text-subtle">Priority</span>
              <div className="mt-1">
                <Dropdown
                  value={String(newTaskPriority)}
                  onChange={(v) => setNewTaskPriority(Number(v))}
                  ariaLabel="Task priority"
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
              <h2 className="text-sm font-semibold text-text">New board</h2>
              <Button variant="ghost" size="sm" type="button" aria-label="Cancel new board" onClick={closeNewBoard}><X size={14} /></Button>
            </div>
            <label className="mt-3 block">
              <span className="text-[10px] font-semibold uppercase tracking-wide text-text-subtle">Name *</span>
              <input autoFocus value={newBoardName} onChange={(e) => setNewBoardName(e.target.value)} placeholder="e.g. Home Renovation" maxLength={80} className="mt-1 w-full rounded-lg border border-border-subtle bg-surface-sunken px-2.5 py-2 text-xs text-text placeholder:text-text-subtle focus:border-border focus:outline-none" />
            </label>
            <label className="mt-3 block">
              <span className="text-[10px] font-semibold uppercase tracking-wide text-text-subtle">Description</span>
              <textarea value={newBoardDesc} onChange={(e) => setNewBoardDesc(e.target.value)} placeholder="What is this board for?" rows={3} className="mt-1 w-full resize-y rounded-lg border border-border-subtle bg-surface-sunken px-2.5 py-2 text-xs text-text placeholder:text-text-subtle focus:border-border focus:outline-none min-h-[4rem]" />
            </label>
            <p className="mt-2 text-[10px] text-text-subtle">The new board becomes the active one for CLI and gateway too.</p>
            <div className="mt-3 flex items-center justify-end gap-2">
              <Button variant="ghost" size="sm" type="button" onClick={closeNewBoard}>Cancel</Button>
              <Button type="submit" size="sm" disabled={!newBoardName.trim() || creatingBoard}>{creatingBoard ? 'Creating…' : 'Create board'}</Button>
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
      {openTaskId ? <TaskDrawer taskId={openTaskId} onClose={() => setOpenTaskId(null)} /> : null}
    </div>
  );
}
