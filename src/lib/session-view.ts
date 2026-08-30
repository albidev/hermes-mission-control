export type SessionCategory = 'conversation' | 'automation' | 'system' | 'unknown';

export type SessionStatus = 'live' | 'idle' | 'ended';

export type SessionViewFilters = {
  query: string;
  status: SessionStatus | 'all';
  category: SessionCategory | 'all';
  origin: string;
  model: string;
};

type SessionLike = {
  sessionId?: string | null;
  title?: string | null;
  preview?: string | null;
  source?: string | null;
  platform?: string | null;
  chatType?: string | null;
  displayName?: string | null;
  model?: string | null;
  status?: SessionStatus | null;
  traceMode?: 'native' | 'transcript' | 'unavailable' | null;
  category?: SessionCategory | null;
  originLabel?: string | null;
  isResumable?: boolean | null;
};

export type SessionOrigin = {
  category: SessionCategory;
  label: string;
  resumable: boolean;
};

const CONVERSATION_ORIGINS = new Set(['tui', 'discord', 'telegram', 'mission-control']);
const AUTOMATION_ORIGINS = new Set(['cron', 'kanban']);
const SYSTEM_ORIGINS = new Set(['cli', 'system', 'test', 'smoke']);

const ORIGIN_LABELS: Record<string, string> = {
  tui: 'TUI',
  discord: 'Discord',
  telegram: 'Telegram',
  'mission-control': 'Mission Control',
  cron: 'Cron',
  kanban: 'Kanban',
  cli: 'CLI',
  system: 'System',
};

function normalizedOrigin(session: SessionLike): string {
  return String(session.source || session.platform || '').trim().toLowerCase();
}

function isCategory(value: unknown): value is SessionCategory {
  return value === 'conversation' || value === 'automation' || value === 'system' || value === 'unknown';
}

export function classifySessionOrigin(session: SessionLike): SessionOrigin {
  const origin = normalizedOrigin(session);
  const category = isCategory(session.category)
    ? session.category
    : CONVERSATION_ORIGINS.has(origin)
      ? 'conversation'
      : AUTOMATION_ORIGINS.has(origin)
        ? 'automation'
        : SYSTEM_ORIGINS.has(origin)
          ? 'system'
          : 'unknown';
  const label = session.originLabel?.trim() || ORIGIN_LABELS[origin] || (origin ? origin : 'Unknown');
  return {
    category,
    label,
    resumable: session.isResumable ?? category === 'conversation',
  };
}

export function getSessionStatusMeta(status: SessionStatus | null | undefined): {
  label: string;
  tone: 'positive' | 'warning' | 'default';
  description: string;
} {
  if (status === 'live') return { label: 'LIVE', tone: 'positive', description: 'Active within the live window' };
  if (status === 'idle') return { label: 'IDLE', tone: 'warning', description: 'No recent activity' };
  return { label: 'ENDED', tone: 'default', description: 'Session has ended' };
}

export function getSessionActionAvailability(session: SessionLike): {
  resumeChat: boolean;
  trace: boolean;
  inspect: boolean;
} {
  const origin = classifySessionOrigin(session);
  const traceAvailable = session.traceMode !== 'unavailable';
  return {
    resumeChat: origin.resumable,
    trace: traceAvailable,
    inspect: traceAvailable,
  };
}

export function matchesSessionFilters(session: SessionLike, filters: SessionViewFilters): boolean {
  const origin = classifySessionOrigin(session);
  const query = filters.query.trim().toLowerCase();
  const haystack = [
    session.sessionId,
    session.title,
    session.preview,
    session.source,
    session.platform,
    session.chatType,
    session.displayName,
    session.model,
    session.originLabel,
    origin.label,
  ].filter(Boolean).join(' ').toLowerCase();
  if (query && !haystack.includes(query)) return false;
  if (filters.status !== 'all' && session.status !== filters.status) return false;
  if (filters.category !== 'all' && origin.category !== filters.category) return false;
  if (filters.origin && normalizedOrigin(session) !== filters.origin.toLowerCase()) return false;
  if (filters.model && session.model !== filters.model) return false;
  return true;
}
