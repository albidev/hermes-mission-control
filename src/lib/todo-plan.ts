import type { ChatMessage } from './chat-protocol';

export type TodoStatus = 'pending' | 'in_progress' | 'completed' | 'cancelled';
export type TodoPlanStatus = 'idle' | 'planning' | 'running' | 'complete';

export type TodoPlanItem = {
  id: string;
  content: string;
  status: TodoStatus;
  parent?: string;
};

export type TodoPlan = {
  items: TodoPlanItem[];
  revision: number | null;
  total: number;
  pending: number;
  inProgress: number;
  completed: number;
  cancelled: number;
  current?: TodoPlanItem;
  next?: TodoPlanItem;
  status: TodoPlanStatus;
};

const TODO_STATUSES: ReadonlySet<string> = new Set(['pending', 'in_progress', 'completed', 'cancelled']);

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

function normalizeItems(value: unknown): TodoPlanItem[] | null {
  if (!Array.isArray(value)) return null;
  const items: TodoPlanItem[] = [];
  for (const candidate of value) {
    if (!isRecord(candidate)) continue;
    const id = typeof candidate.id === 'string' ? candidate.id.trim() : '';
    const content = typeof candidate.content === 'string' ? candidate.content.trim() : '';
    const status = typeof candidate.status === 'string' ? candidate.status.trim().toLowerCase() : '';
    if (!id || !content || !TODO_STATUSES.has(status)) continue;
    const parent = typeof candidate.parent === 'string' && candidate.parent.trim()
      ? candidate.parent.trim()
      : undefined;
    items.push({ id, content, status: status as TodoStatus, ...(parent ? { parent } : {}) });
  }
  return items;
}

function readPayload(value: unknown): { todos: TodoPlanItem[]; revision: number | null } | null {
  if (Array.isArray(value)) {
    const todos = normalizeItems(value);
    return todos ? { todos, revision: null } : null;
  }
  if (!isRecord(value) || !('todos' in value)) return null;
  const todos = normalizeItems(value.todos);
  if (!todos) return null;
  const revision = typeof value.revision === 'number' && Number.isFinite(value.revision)
    ? Math.max(0, Math.floor(value.revision))
    : null;
  return { todos, revision };
}

function balancedJsonCandidates(text: string): string[] {
  const candidates: string[] = [];
  for (let start = 0; start < text.length; start += 1) {
    if (text[start] !== '{' && text[start] !== '[') continue;
    let depth = 0;
    let quoted = false;
    let escaped = false;
    for (let index = start; index < text.length; index += 1) {
      const character = text[index];
      if (quoted) {
        if (escaped) escaped = false;
        else if (character === '\\') escaped = true;
        else if (character === '"') quoted = false;
        continue;
      }
      if (character === '"') {
        quoted = true;
        continue;
      }
      if (character === '{' || character === '[') depth += 1;
      if (character === '}' || character === ']') depth -= 1;
      if (depth === 0) {
        candidates.push(text.slice(start, index + 1));
        break;
      }
      if (depth < 0) break;
    }
  }
  return candidates;
}

function parseTodoPayload(raw: string): { todos: TodoPlanItem[]; revision: number | null } | null {
  const text = raw.trim();
  if (!text) return null;
  const candidates = [text];
  const fenced = text.match(/^```(?:json)?\s*([\s\S]*?)\s*```$/i);
  if (fenced?.[1]) candidates.unshift(fenced[1].trim());
  candidates.push(...balancedJsonCandidates(text));

  for (const candidate of candidates) {
    try {
      const parsed = JSON.parse(candidate) as unknown;
      const payload = readPayload(parsed);
      if (payload) return payload;
    } catch {
      // Tool output can contain a short human-readable prefix around the JSON.
    }
  }
  return null;
}

function buildPlan(payload: { todos: TodoPlanItem[]; revision: number | null }): TodoPlan {
  const { todos } = payload;
  const pending = todos.filter((item) => item.status === 'pending').length;
  const inProgress = todos.filter((item) => item.status === 'in_progress').length;
  const completed = todos.filter((item) => item.status === 'completed').length;
  const cancelled = todos.filter((item) => item.status === 'cancelled').length;
  const current = todos.find((item) => item.status === 'in_progress');
  const next = todos.find((item) => item.status === 'pending');
  const resolved = completed + cancelled;
  const status: TodoPlanStatus = todos.length === 0
    ? 'idle'
    : resolved === todos.length
      ? 'complete'
      : inProgress > 0
        ? 'running'
        : 'planning';
  return {
    items: todos,
    revision: payload.revision,
    total: todos.length,
    pending,
    inProgress,
    completed,
    cancelled,
    ...(current ? { current } : {}),
    ...(next ? { next } : {}),
    status,
  };
}

/**
 * Extract the newest authoritative TODO result from the chat transcript.
 *
 * TODO calls return the complete list on every invocation. A streaming call
 * exposes its proposed list in toolInput before the result arrives, while a
 * completed call exposes the authoritative snapshot in output. Walking the
 * transcript backwards keeps the UI live without adding a gateway endpoint.
 */
export function deriveTodoPlan(messages: ChatMessage[]): TodoPlan | null {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index];
    if (message.kind !== 'tool' || message.toolName?.trim().toLowerCase() !== 'todo') continue;
    const sources = [message.output, message.detail, message.toolInput, message.text];
    for (const source of sources) {
      if (!source) continue;
      const payload = parseTodoPayload(source);
      if (payload) return buildPlan(payload);
    }
  }
  return null;
}
