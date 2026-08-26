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
      className={`kanban-column flex flex-col min-w-[11rem] w-[13rem] sm:w-auto shrink-0 sm:min-w-0 gap-1.5 rounded-lg border p-2 ${dragOver ? 'border-sky-400/60 bg-sky-400/5' : 'border-border-subtle bg-surface/30'}`}
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
      <div className="flex sm:flex-col gap-1.5 overflow-x-auto sm:overflow-x-visible sm:overflow-y-auto pb-1" role="list">
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
  const [creating, setCreating] = useState(false);

  const submitNewTask = async () => {
    const title = newTaskTitle.trim();
    if (!title || !newTaskStatus) return;
    setCreating(true);
    try {
      await createKanbanTask(storedToken || undefined, { title }, activeBoard || undefined);
      setNewTaskTitle('');
      setNewTaskStatus(null);
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
          <select
            value={activeBoard}
            onChange={(e) => setActiveBoard(e.target.value)}
            aria-label="Select kanban board"
            className="max-w-[12rem] truncate rounded-md border border-border-subtle bg-surface px-2 py-1 text-xs text-text focus:outline-none"
          >
            {boards.map((b) => (
              <option key={b.slug} value={b.slug}>
                {b.name || b.slug}{typeof b.total === 'number' ? ` (${b.total})` : ''}
              </option>
            ))}
          </select>
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
            className="w-full sm:max-w-sm rounded-t-xl sm:rounded-xl border border-border-subtle bg-surface p-4 shadow-xl"
            onClick={(e) => e.stopPropagation()}
            onSubmit={(e) => {
              e.preventDefault();
              void submitNewTask();
            }}
          >
            <div className="flex items-center justify-between">
              <h2 className="text-sm font-semibold text-text">New task → {newTaskStatus}</h2>
              <Button variant="ghost" size="sm" type="button" aria-label="Cancel new task" onClick={() => setNewTaskStatus(null)}>
                <X size={14} />
              </Button>
            </div>
            <input
              autoFocus
              value={newTaskTitle}
              onChange={(e) => setNewTaskTitle(e.target.value)}
              placeholder="Task title"
              className="mt-3 w-full rounded-md border border-border-subtle bg-surface-sunken px-2 py-1.5 text-xs text-text placeholder:text-text-subtle focus:border-border focus:outline-none"
              aria-label="Task title"
            />
            <div className="mt-3 flex justify-end gap-2">
              <Button type="submit" size="sm" disabled={!newTaskTitle.trim() || creating}>
                {creating ? 'Creating…' : 'Create'}
              </Button>
            </div>
          </form>
        </div>
      ) : null}
    </div>
  );
}
