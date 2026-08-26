import { useCallback, useEffect, useRef, useState } from 'react';
import { Kanban as KanbanIcon, RefreshCw, Plus, X, MessageSquare, GitBranch } from 'lucide-react';
import { Card } from '../components/ui/Card';
import { Button } from '../components/ui/Button';
import {
  loadKanbanBoard,
  loadKanbanBoards,
  loadKanbanTaskDetail,
  moveKanbanTask,
  createKanbanTask,
  addKanbanComment,
  type MissionControlKanbanBoard,
  type MissionControlKanbanBoardMeta,
  type MissionControlKanbanTask,
  type MissionControlKanbanTaskDetail,
} from '../lib/hermes-api';
import { useMissionControl } from '../lib/mission-control-store';

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

// Mobile: horizontal scroll columns; Desktop: grid fits all columns.
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
  return (
    <div
      className={`kanban-column flex flex-col min-w-[14rem] w-[17rem] sm:w-auto shrink-0 sm:min-w-0 gap-2 rounded-xl border p-2.5 ${dragOver ? 'border-sky-400/60 bg-sky-400/5' : 'border-border-subtle bg-surface/30'}`}
      data-status={name}
      onDragOver={(e) => {
        e.preventDefault();
        setDragOver(true);
      }}
      onDragLeave={() => setDragOver(false)}
      onDrop={(e) => {
        e.preventDefault();
        setDragOver(false);
        const id = e.dataTransfer.getData('text/plain');
        if (id) onDropTask(id, name);
      }}
    >
      <div className="flex items-center justify-between px-0.5">
        <span className={`text-[10px] font-semibold uppercase tracking-wide ${STATUS_TONES[name] ?? 'text-text-muted'}`}>{name}</span>
        <div className="flex items-center gap-1">
          <span className="text-[10px] text-text-subtle tabular-nums">{tasks.length}</span>
          <button
            type="button"
            className="rounded p-0.5 text-text-subtle hover:text-text hover:bg-surface-sunken"
            aria-label={`Add task to ${name}`}
            onClick={() => onAddTask(name)}
          >
            <Plus size={12} />
          </button>
        </div>
      </div>
      <div className="flex sm:flex-col gap-2 overflow-x-auto sm:overflow-x-visible sm:overflow-y-auto pb-1" role="list">
        {tasks.map((task) => (
          <button
            key={task.id}
            type="button"
            role="listitem"
            draggable
            onDragStart={(e) => e.dataTransfer.setData('text/plain', task.id)}
            onClick={() => onOpenTask(task.id)}
            className={`kanban-card w-full min-w-[10rem] sm:min-w-0 rounded-md border p-2 text-left transition-colors ${activeTaskId === task.id ? 'border-sky-400/70 bg-sky-400/10' : 'border-border-subtle bg-surface hover:border-border'}`}
          >
            <div className="text-xs font-medium text-text line-clamp-2">{task.title}</div>
            <div className="mt-1 flex items-center gap-2 text-[10px] text-text-subtle">
              {task.assignee ? <span className="truncate max-w-[6rem]">@{task.assignee}</span> : null}
              {(task.children?.length ?? 0) > 0 ? (
                <span className="inline-flex items-center gap-0.5">
                  <GitBranch size={9} aria-hidden />
                  {task.progress ? `${task.progress.done}/${task.progress.total}` : task.children!.length}
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
        ))}
        {tasks.length === 0 ? (
          <div className="hidden sm:block rounded-md border border-dashed border-border-subtle p-3 text-center text-[10px] text-text-subtle">Empty</div>
        ) : null}
      </div>
    </div>
  );
}

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
      .then((d) => {
        if (!cancelled) setDetail(d);
      })
      .catch(() => {
        if (!cancelled) setDetail(null);
      });
    return () => {
      cancelled = true;
    };
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
    <div
      className="fixed inset-0 z-50 flex items-end sm:items-center justify-center bg-black/40 backdrop-blur-sm"
      role="dialog"
      aria-modal="true"
      aria-label={`Task ${detail?.title ?? taskId}`}
      onClick={onClose}
    >
      <div
        className="w-full sm:max-w-lg max-h-[85vh] overflow-y-auto rounded-t-xl sm:rounded-xl border border-border-subtle bg-surface p-4 shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        {!detail ? (
          <p className="text-sm text-text-muted py-8 text-center">Loading task…</p>
        ) : (
          <>
            <div className="flex items-start justify-between gap-3">
              <div>
                <h2 className="text-sm font-semibold text-text">{detail.title}</h2>
                <p className="mt-0.5 text-[10px] uppercase tracking-wide text-text-subtle">{detail.status}</p>
              </div>
              <Button variant="ghost" size="sm" aria-label="Close task" onClick={onClose}>
                <X size={14} />
              </Button>
            </div>

            {detail.body ? (
              <p className="mt-3 whitespace-pre-wrap text-xs text-text-muted leading-relaxed">{detail.body}</p>
            ) : null}

            {detail.latest_summary ? (
              <div className="mt-3 rounded-md border border-border-subtle bg-surface-sunken p-2">
                <span className="text-[10px] uppercase tracking-wide text-text-subtle">Latest summary</span>
                <p className="mt-1 whitespace-pre-wrap text-xs text-text-muted leading-relaxed line-clamp-6">{detail.latest_summary}</p>
              </div>
            ) : null}

            {detail.comments.length > 0 ? (
              <div className="mt-4">
                <span className="text-[10px] font-semibold uppercase tracking-wide text-text-subtle">Comments ({detail.comments.length})</span>
                <ul className="mt-1.5 space-y-2">
                  {detail.comments.map((c) => (
                    <li key={c.id} className="rounded-md border border-border-subtle bg-surface/60 p-2">
                      <div className="flex items-center justify-between text-[10px] text-text-subtle">
                        <span>@{c.author}</span>
                        <span>{new Date(c.created_at * 1000).toLocaleString()}</span>
                      </div>
                      <p className="mt-1 whitespace-pre-wrap text-xs text-text-muted">{c.body}</p>
                    </li>
                  ))}
                </ul>
              </div>
            ) : null}

            <form
              className="mt-3 flex items-end gap-2"
              onSubmit={(e) => {
                e.preventDefault();
                void postComment();
              }}
            >
              <textarea
                value={commentDraft}
                onChange={(e) => setCommentDraft(e.target.value)}
                placeholder="Add a comment…"
                rows={2}
                className="min-h-0 flex-1 resize-none rounded-md border border-border-subtle bg-surface-sunken px-2 py-1.5 text-xs text-text placeholder:text-text-subtle focus:border-border focus:outline-none"
                aria-label="New comment"
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

function BoardPicker({
  boards,
  activeBoard,
  onSelect,
}: {
  boards: MissionControlKanbanBoardMeta[];
  activeBoard: string;
  onSelect: (slug: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDocClick = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false);
    };
    document.addEventListener('mousedown', onDocClick);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onDocClick);
      document.removeEventListener('keydown', onKey);
    };
  }, [open]);

  const active = boards.find((b) => b.slug === activeBoard);
  return (
    <div className="kanban-board-picker relative" ref={ref}>
      <button
        type="button"
        className="kanban-board-trigger flex items-center gap-2 rounded-lg border border-border-subtle bg-surface px-3 py-1.5 text-xs font-medium text-text hover:border-border focus:border-sky-400/60 focus:outline-none"
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-label="Select kanban board"
        onClick={() => setOpen((v) => !v)}
      >
        <KanbanIcon size={13} className="text-sky-400 shrink-0" />
        <span className="max-w-[11rem] truncate">{active ? active.name || active.slug : 'Select board'}</span>
        {typeof active?.total === 'number' ? (
          <span className="rounded-full bg-surface-sunken px-1.5 py-px text-[10px] tabular-nums text-text-subtle">{active.total}</span>
        ) : null}
        <svg
          className={`ml-auto h-3.5 w-3.5 shrink-0 text-text-subtle transition-transform duration-200 ${open ? 'rotate-180' : ''}`}
          viewBox="0 0 20 20"
          fill="currentColor"
          aria-hidden
        >
          <path fillRule="evenodd" d="M5.23 7.21a.75.75 0 011.06.02L10 10.94l3.71-3.71a.75.75 0 111.06 1.06l-4.24 4.24a.75.75 0 01-1.06 0L5.23 8.29a.75.75 0 010-1.08z" clipRule="evenodd" />
        </svg>
      </button>
      {open ? (
        <ul
          role="listbox"
          aria-label="Kanban boards"
          className="kanban-board-menu absolute left-0 top-[calc(100%+4px)] z-40 min-w-full w-max max-w-[18rem] overflow-hidden rounded-xl border border-border-subtle bg-surface p-1 shadow-xl"
        >
          {boards.map((b) => {
            const selected = b.slug === activeBoard;
            return (
              <li key={b.slug} role="option" aria-selected={selected}>
                <button
                  type="button"
                  className={`flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-left text-xs transition-colors ${selected ? 'bg-sky-400/10 text-text font-medium' : 'text-text-muted hover:bg-surface-sunken hover:text-text'}`}
                  onClick={() => {
                    onSelect(b.slug);
                    setOpen(false);
                  }}
                >
                  <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${b.is_current ? 'bg-emerald-400' : 'bg-transparent border border-border-subtle'}`} title={b.is_current ? 'Active board (CLI/gateway)' : undefined} />
                  <span className="truncate flex-1">{b.name || b.slug}</span>
                  {typeof b.total === 'number' ? (
                    <span className="rounded-full bg-surface-sunken px-1.5 py-px text-[10px] tabular-nums text-text-subtle">{b.total}</span>
                  ) : null}
                </button>
              </li>
            );
          })}
        </ul>
      ) : null}
    </div>
  );
}

export function KanbanRoute() {
  const { storedToken } = useMissionControl();
  const [board, setBoard] = useState<MissionControlKanbanBoard | null>(null);
  const [boards, setBoards] = useState<MissionControlKanbanBoardMeta[]>([]);
  const [activeBoard, setActiveBoard] = useState<string>('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [openTaskId, setOpenTaskId] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const latestEventRef = useRef(0);

  const refresh = useCallback(
    async (boardSlug?: string) => {
      const target = boardSlug !== undefined ? boardSlug : activeBoard;
      setRefreshing(true);
      try {
        const next = await loadKanbanBoard(storedToken || undefined, target || undefined);
        setBoard(next);
        latestEventRef.current = next.latestEventId;
        setError(null);
      } catch (e) {
        setError(e instanceof Error ? e.message : 'Failed to load board.');
      } finally {
        setRefreshing(false);
        setLoading(false);
      }
    },
    [storedToken, activeBoard],
  );

  // Initial boards list + board selection.
  useEffect(() => {
    let cancelled = false;
    loadKanbanBoards(storedToken || undefined)
      .then(({ boards: list, current }) => {
        if (cancelled) return;
        setBoards(list);
        setActiveBoard(current || '');
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [storedToken]);

  useEffect(() => {
    void refresh(activeBoard);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeBoard]);

  // Live updates: poll the event tail; refresh the board when new events arrive.
  useEffect(() => {
    if (!board) return;
    let cancelled = false;
    const interval = window.setInterval(async () => {
      if (document.hidden) return;
      try {
        const params = new URLSearchParams({ since: String(latestEventRef.current) });
        if (activeBoard) params.set('board', activeBoard);
        const response = await fetch(`/api/local/kanban/events?${params.toString()}`, {
          headers: storedToken ? { Authorization: `Bearer ${storedToken}` } : undefined,
          cache: 'no-store',
        });
        if (!response.ok || cancelled) return;
        const data = (await response.json()) as { events?: unknown[]; cursor?: number };
        if (data.cursor && data.cursor > latestEventRef.current) {
          latestEventRef.current = data.cursor;
          void refresh(activeBoard);
        }
      } catch {
        /* transient — next tick retries */
      }
    }, 5000);
    return () => {
      cancelled = true;
      window.clearInterval(interval);
    };
  }, [board, activeBoard, storedToken, refresh]);

  const handleMove = async (taskId: string, status: string) => {
    if (!board) return;
    const current = board.columns.find((c) => c.tasks.some((t) => t.id === taskId));
    if (current?.name === status) return;
    // Optimistic move; rollback on failure.
    setBoard((prev) =>
      prev
        ? {
            ...prev,
            columns: prev.columns.map((c) => {
              const tasks = c.name === status
                ? [...c.tasks, ...prev.columns.find((x) => x.name === current?.name)?.tasks.filter((t) => t.id === taskId) ?? []]
                : c.tasks.filter((t) => t.id !== taskId);
              return { ...c, tasks };
            }),
          }
        : prev,
    );
    try {
      await moveKanbanTask(storedToken || undefined, taskId, status, activeBoard || undefined);
      await refresh(activeBoard);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Move failed.');
      await refresh(activeBoard);
    }
  };

  const [newTaskStatus, setNewTaskStatus] = useState<string | null>(null);
  const [newTaskTitle, setNewTaskTitle] = useState('');
  const [newTaskBody, setNewTaskBody] = useState('');
  const [newTaskPriority, setNewTaskPriority] = useState(0);
  const [creating, setCreating] = useState(false);

  const closeNewTask = () => {
    setNewTaskStatus(null);
    setNewTaskTitle('');
    setNewTaskBody('');
    setNewTaskPriority(0);
  };

  const submitNewTask = async () => {
    const title = newTaskTitle.trim();
    if (!title || !newTaskStatus) return;
    setCreating(true);
    try {
      await createKanbanTask(
        storedToken || undefined,
        { title, body: newTaskBody.trim() || undefined, priority: newTaskPriority },
        activeBoard || undefined,
      );
      closeNewTask();
      await refresh(activeBoard);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Create failed.');
    } finally {
      setCreating(false);
    }
  };

  if (loading) {
    return (
      <Card padding="none">
        <div className="p-6 text-center text-sm text-text-muted">Loading Kanban board…</div>
      </Card>
    );
  }

  return (
    <div className="flex flex-col gap-3 h-full min-h-0">
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2 min-w-0">
          <KanbanIcon size={16} className="text-sky-400 shrink-0" />
          <BoardPicker boards={boards} activeBoard={activeBoard} onSelect={setActiveBoard} />
        </div>
        <div className="flex items-center gap-2">
          {error ? <span className="text-[10px] text-red-400 truncate max-w-[14rem]" role="alert">{error}</span> : null}
          {refreshing ? <RefreshCw size={12} className="text-text-subtle animate-spin" /> : null}
          <span className="text-[10px] text-text-subtle hidden sm:inline">Live · 5s</span>
        </div>
      </div>

      {board ? (
        <div className="kanban-scroller flex gap-2 overflow-x-auto sm:overflow-x-auto pb-2 -mx-1 px-1 sm:grid sm:grid-cols-4 xl:grid-cols-8 sm:gap-2 flex-1 min-h-0">
          {board.columns.map((col) => (
            <BoardColumn
              key={col.name}
              name={col.name}
              tasks={col.tasks}
              activeTaskId={openTaskId}
              onOpenTask={setOpenTaskId}
              onDropTask={(id, s) => void handleMove(id, s)}
              onAddTask={setNewTaskStatus}
            />
          ))}
        </div>
      ) : (
        <Card padding="none">
          <div className="p-6 text-center text-sm text-text-muted">{error ?? 'No board available.'}</div>
        </Card>
      )}

      {openTaskId ? <TaskDrawer taskId={openTaskId} onClose={() => setOpenTaskId(null)} /> : null}

      {newTaskStatus ? (
        <div
          className="fixed inset-0 z-50 flex items-end sm:items-center justify-center bg-black/40 backdrop-blur-sm"
          role="dialog"
          aria-modal="true"
          aria-label="New task"
          onClick={() => setNewTaskStatus(null)}
        >
          <form
            className="w-full sm:max-w-md rounded-t-xl sm:rounded-xl border border-border-subtle bg-surface p-4 shadow-xl"
            onClick={(e) => e.stopPropagation()}
            onSubmit={(e) => {
              e.preventDefault();
              void submitNewTask();
            }}
          >
            <div className="flex items-center justify-between">
              <h2 className="text-sm font-semibold text-text">New task</h2>
              <Button variant="ghost" size="sm" type="button" aria-label="Cancel new task" onClick={closeNewTask}>
                <X size={14} />
              </Button>
            </div>

            <label className="mt-3 block">
              <span className="text-[10px] font-semibold uppercase tracking-wide text-text-subtle">Column</span>
              <div className="mt-1 flex flex-wrap gap-1">
                {(board?.columns ?? []).map((c) => (
                  <button
                    key={c.name}
                    type="button"
                    className={`rounded-full border px-2.5 py-1 text-[10px] font-medium uppercase tracking-wide transition-colors ${newTaskStatus === c.name ? 'border-sky-400/70 bg-sky-400/10 text-text' : 'border-border-subtle text-text-muted hover:border-border hover:text-text'}`}
                    aria-pressed={newTaskStatus === c.name}
                    onClick={() => setNewTaskStatus(c.name)}
                  >
                    {c.name}
                  </button>
                ))}
              </div>
            </label>

            <label className="mt-3 block">
              <span className="text-[10px] font-semibold uppercase tracking-wide text-text-subtle">Title *</span>
              <input
                autoFocus
                value={newTaskTitle}
                onChange={(e) => setNewTaskTitle(e.target.value)}
                placeholder="Short, actionable title"
                maxLength={200}
                className="mt-1 w-full rounded-lg border border-border-subtle bg-surface-sunken px-2.5 py-2 text-xs text-text placeholder:text-text-subtle focus:border-border focus:outline-none"
              />
            </label>

            <label className="mt-3 block">
              <span className="text-[10px] font-semibold uppercase tracking-wide text-text-subtle">Description</span>
              <textarea
                value={newTaskBody}
                onChange={(e) => setNewTaskBody(e.target.value)}
                placeholder="Context, acceptance criteria, links…"
                rows={4}
                className="mt-1 w-full resize-y rounded-lg border border-border-subtle bg-surface-sunken px-2.5 py-2 text-xs text-text placeholder:text-text-subtle focus:border-border focus:outline-none min-h-[5rem]"
              />
            </label>

            <div className="mt-3 flex items-center justify-between gap-3">
              <label className="flex items-center gap-2">
                <span className="text-[10px] font-semibold uppercase tracking-wide text-text-subtle">Priority</span>
                <select
                  value={newTaskPriority}
                  onChange={(e) => setNewTaskPriority(Number(e.target.value))}
                  className="rounded-lg border border-border-subtle bg-surface px-2 py-1.5 text-xs text-text focus:outline-none"
                  aria-label="Task priority"
                >
                  <option value={2}>High</option>
                  <option value={1}>Medium</option>
                  <option value={0}>Normal</option>
                  <option value={-1}>Low</option>
                </select>
              </label>
              <div className="flex items-center gap-2">
                <Button variant="ghost" size="sm" type="button" onClick={closeNewTask}>Cancel</Button>
                <Button type="submit" size="sm" disabled={!newTaskTitle.trim() || creating}>
                  {creating ? 'Creating…' : 'Create'}
                </Button>
              </div>
            </div>
          </form>
        </div>
      ) : null}
    </div>
  );
}
