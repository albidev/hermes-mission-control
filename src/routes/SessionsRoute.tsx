import { useI18n } from '../lib/i18n';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import {
  Activity,
  Bot,
  ChevronDown,
  Clock3,
  Copy,
  DollarSign,
  Layers,
  MessageSquare,
  MessagesSquare,
  RefreshCw,
  Search,
  SlidersHorizontal,
  Workflow,
} from 'lucide-react';
import { Card } from '../components/ui/Card';
import { Badge } from '../components/ui/Badge';
import { Button } from '../components/ui/Button';
import { Modal } from '../components/Modal';
import { formatRelativeTime, formatTimestamp } from '../lib/format';
import { useMissionControl } from '../lib/mission-control-store';
import { usePullToReload } from '../hooks/usePullToReload';
import { PullToReloadIndicator } from '../components/PullToReloadIndicator';
import { PageHeader } from '../components/PageHeader';
import {
  loadMissionControlAgentSessions,
  type MissionControlAgentSessionItem,
  type MissionControlAgentSessionStatus,
  type MissionControlAgentsSessionsSnapshot,
} from '../lib/hermes-api';
import {
  classifySessionOrigin,
  getSessionActionAvailability,
  getSessionStatusMeta,
  matchesSessionFilters,
  type SessionCategory,
  type SessionViewFilters,
} from '../lib/session-view';

const PAGE_SIZE = 50;
const POLL_INTERVAL_MS = 10_000;
const LIVE_POLL_INTERVAL_MS = 5_000;
const SESSION_LOAD_TIMEOUT_MS = 15_000;

type SessionTab = 'all' | 'live' | SessionCategory;
type SortKey = 'activity' | 'started' | 'messages' | 'tokens' | 'cost';
type SessionViewPatch = Partial<{
  tab: SessionTab;
  query: string;
  status: SessionViewFilters['status'];
  origin: string;
  model: string;
  sortKey: SortKey;
}>;

const SESSION_VIEW_QUERY_KEYS = ['tab', 'query', 'status', 'origin', 'model', 'sort'] as const;
const SESSION_TABS: SessionTab[] = ['all', 'live', 'conversation', 'automation', 'system'];
const SESSION_SORT_KEYS: SortKey[] = ['activity', 'started', 'messages', 'tokens', 'cost'];

function parseSessionTab(value: string | null): SessionTab {
  return value && SESSION_TABS.includes(value as SessionTab) ? value as SessionTab : 'all';
}

function parseSessionSortKey(value: string | null): SortKey {
  return value && SESSION_SORT_KEYS.includes(value as SortKey) ? value as SortKey : 'activity';
}

function parseSessionStatus(value: string | null): SessionViewFilters['status'] {
  return value === 'live' || value === 'idle' || value === 'ended' ? value : 'all';
}

function readSessionFilters(params: URLSearchParams): SessionViewFilters {
  return {
    query: params.get('query') ?? '',
    status: parseSessionStatus(params.get('status')),
    category: 'all',
    origin: params.get('origin') ?? '',
    model: params.get('model') ?? '',
  };
}

const CATEGORY_ORDER: SessionCategory[] = ['conversation', 'automation', 'system', 'unknown'];
const CATEGORY_LABEL_KEYS: Record<SessionCategory, string> = {
  conversation: 'sessions.conversations',
  automation: 'sessions.automations',
  system: 'sessions.systemCategory',
  unknown: 'sessions.other',
};

function withTimeout<T>(promise: Promise<T>, timeoutMs: number): Promise<T> {
  return new Promise((resolve, reject) => {
    const timer = window.setTimeout(() => reject(new Error('Session request timed out.')), timeoutMs);
    promise.then(
      (value) => { window.clearTimeout(timer); resolve(value); },
      (error) => { window.clearTimeout(timer); reject(error); },
    );
  });
}

function formatTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(n);
}

function formatCost(usd: number): string {
  if (usd >= 1) return `$${usd.toFixed(2)}`;
  if (usd >= 0.01) return `$${usd.toFixed(3)}`;
  if (usd > 0) return `$${usd.toFixed(4)}`;
  return '$0.00';
}

function formatDuration(startedAt: number | null, endedAt: number | null, lastActiveAt: number | null): string {
  if (!startedAt) return '—';
  const end = endedAt ?? lastActiveAt ?? startedAt;
  const seconds = Math.max(0, Math.floor(end - startedAt));
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`;
}

function MetricCard({ icon: Icon, label, value, hint, className = '', compact = false }: { icon: React.ElementType; label: string; value: string; hint: string; className?: string; compact?: boolean }) {
  return (
    <Card className={`!border-0 p-3 sm:p-4 ${compact ? 'px-2 py-2 sm:p-4' : ''} ${className}`}>
      <div className="flex items-center justify-between">
        <span className={`text-xs text-text-muted ${compact ? 'truncate text-[10px] sm:text-xs' : ''}`}>{label}</span>
        <Icon className={`h-4 w-4 text-text-subtle ${compact ? 'h-3.5 w-3.5 sm:h-4 sm:w-4' : ''}`} />
      </div>
      <p className={`mt-2 text-base font-semibold text-text tabular-nums sm:text-lg ${compact ? 'mt-1 text-sm sm:mt-2 sm:text-lg' : ''}`}>{value}</p>
      <p className={`mt-1 text-xs text-text-subtle ${compact ? 'hidden sm:block' : ''}`}>{hint}</p>
    </Card>
  );
}

function StatusBadge({ status }: { status: MissionControlAgentSessionStatus }) {
  const meta = getSessionStatusMeta(status);
  return <Badge variant={meta.tone} dot title={meta.description}>{meta.label}</Badge>;
}

function SessionActionButton({
  label,
  icon,
  onClick,
  variant = 'secondary',
  className = '',
}: {
  label: string;
  icon: React.ReactNode;
  onClick: () => void;
  variant?: 'primary' | 'secondary' | 'ghost' | 'danger';
  className?: string;
}) {
  return <Button type="button" size="sm" variant={variant} icon={icon} className={className} onClick={onClick} aria-label={label} title={label}>{label}</Button>;
}

function SessionRow({
  session,
  selected,
  onInspect,
  onCopy,
}: {
  session: MissionControlAgentSessionItem;
  selected: boolean;
  onInspect: (session: MissionControlAgentSessionItem) => void;
  onCopy: (sessionId: string) => void;
}) {
  const { t } = useI18n();
  const origin = classifySessionOrigin(session);
  const actions = getSessionActionAvailability(session);
  const navigate = useNavigate();
  const totalTokens = session.inputTokens + session.outputTokens;
  const lastActive = session.lastActiveAt ?? session.startedAt ?? 0;

  return (
    <article className={`group px-3 py-3 transition-colors sm:px-4 ${
      session.status === 'live' ? 'bg-positive/5 hover:bg-positive/10' :
      session.status === 'idle' ? 'hover:bg-surface-sunken/40' :
      'hover:bg-surface-sunken/40'
    } ${selected ? 'bg-surface-sunken/70 ring-1 ring-inset ring-accent/30' : ''}`}>
      <div className="flex items-start gap-3">
        <div className="mt-1 shrink-0">
          <span className={`block h-2.5 w-2.5 rounded-full ${session.status === 'live' ? 'bg-positive shadow-[0_0_0_3px_rgba(52,211,153,0.14)] animate-pulse' : session.status === 'idle' ? 'bg-warning' : 'bg-text-subtle/50'}`} />
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-start justify-between gap-x-3 gap-y-1">
            <button type="button" onClick={() => onInspect(session)} className="block min-w-0 max-w-full flex-1 text-left">
              <p className="truncate text-sm font-semibold text-text hover:text-accent">{session.title}</p>
              <p className="mt-0.5 max-w-full truncate text-xs text-text-muted">{session.preview || t('sessions.noPreview')}</p>
            </button>
            <StatusBadge status={session.status} />
          </div>

          <div className="mt-2 flex flex-wrap items-center gap-1.5 text-[11px] text-text-subtle">
            <span className="min-w-0 max-w-[8rem] truncate rounded-full bg-surface px-2 py-0.5 font-medium text-text-muted">{origin.label}</span>
            <span className="rounded-full bg-surface px-2 py-0.5 text-text-subtle">{t(CATEGORY_LABEL_KEYS[origin.category])}</span>
            <span className="min-w-0 max-w-[11rem] truncate sm:max-w-[15rem]">{session.model}</span>
            <span>·</span>
            <span>{t('sessions.msgs', { count: session.messageCount })}</span>
            <span>·</span>
            <span title={formatTimestamp(lastActive)}>{session.status === 'live' ? t('sessions.activeNow') : t('sessions.lastActivity')} {formatRelativeTime(lastActive)}</span>
          </div>

          <div className="mt-2 flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-text-subtle">
            {totalTokens > 0 ? <span className="text-sky-400">{t('sessions.tokensCount', { count: formatTokens(totalTokens) })}</span> : null}
            {session.estimatedCostUsd > 0 ? <span className="text-emerald-400">{formatCost(session.estimatedCostUsd)}</span> : null}
            <button type="button" onClick={() => onCopy(session.sessionId)} className="inline-flex min-w-0 max-w-full flex-1 items-center gap-1 overflow-hidden font-mono text-[10px] text-text-subtle hover:text-accent sm:max-w-[18rem] sm:flex-none" title={t('sessions.copySessionId')}>
              <Copy size={10} />
              <span className="truncate">{session.sessionId}</span>
            </button>
            <span className="flex w-full flex-wrap items-center justify-end gap-1.5 sm:ml-auto sm:w-auto">
              {actions.resumeChat ? <SessionActionButton label={t('sessions.resumeChat')} icon={<MessageSquare size={14} />} onClick={() => navigate(`/sessions?chatSession=${encodeURIComponent(session.sessionId)}`)} variant="primary" className="!min-w-0 !border-0 !px-2 sm:!px-3" /> : null}
              {actions.trace ? <SessionActionButton label={t('sessions.trace')} icon={<Workflow size={14} />} onClick={() => navigate(`/agents?session=${encodeURIComponent(session.sessionId)}`)} className="!min-w-0 !border-0 !bg-sky-500/10 !text-sky-300 hover:!bg-sky-500/20 !px-2 sm:!px-3" /> : null}
              {actions.inspect ? <SessionActionButton label={t('sessions.searchAction')} icon={<Search size={14} />} onClick={() => onInspect(session)} variant="ghost" className="!min-w-0 !border-0 !bg-transparent !px-2 text-text-muted hover:!bg-surface-sunken hover:!text-text sm:!px-3" /> : null}
            </span>
          </div>
        </div>
      </div>
    </article>
  );
}

function SessionSection({
  category,
  sessions,
  collapsed,
  onToggle,
  selectedId,
  onInspect,
  onCopy,
}: {
  category: SessionCategory;
  sessions: MissionControlAgentSessionItem[];
  collapsed: boolean;
  onToggle: () => void;
  selectedId: string | null;
  onInspect: (session: MissionControlAgentSessionItem) => void;
  onCopy: (sessionId: string) => void;
}) {
  const { t } = useI18n();
  const liveCount = sessions.filter((session) => session.status === 'live').length;
  return (
    <section>
      <button type="button" onClick={onToggle} aria-expanded={!collapsed} aria-controls={`sessions-${category}-panel`} className="flex w-full items-center gap-2 bg-surface-sunken/45 px-4 py-2.5 text-left hover:bg-surface-sunken/70">
        <ChevronDown size={14} className={`text-text-subtle transition-transform ${collapsed ? '-rotate-90' : ''}`} />
        <span className="text-xs font-semibold uppercase tracking-[0.12em] text-text-muted">{t(CATEGORY_LABEL_KEYS[category])}</span>
        <span className="text-xs text-text-subtle">{sessions.length}</span>
        {liveCount > 0 ? <Badge variant="positive" dot>{liveCount} {t('sessions.liveStatus').toLowerCase()}</Badge> : null}
      </button>
      {!collapsed ? <div id={`sessions-${category}-panel`} role="region" aria-label={t(CATEGORY_LABEL_KEYS[category])}>{sessions.map((session) => <SessionRow key={session.sessionId} session={session} selected={session.sessionId === selectedId} onInspect={onInspect} onCopy={onCopy} />)}</div> : null}
    </section>
  );
}

function SessionDetails({ session, onClose, onCopy }: { session: MissionControlAgentSessionItem; onClose: () => void; onCopy: (sessionId: string) => void }) {
  const { t } = useI18n();
  const origin = classifySessionOrigin(session);
  const actions = getSessionActionAvailability(session);
  const navigate = useNavigate();
  return (
    <Modal
      open
      title={session.title}
      subtitle={`${origin.label} · ${t(CATEGORY_LABEL_KEYS[origin.category])} · ${session.model}`}
      onClose={onClose}
      borderless
      footer={(
        <div className="flex flex-wrap gap-2">
          {actions.resumeChat ? <SessionActionButton label={t('sessions.resumeChat')} icon={<MessageSquare size={14} />} onClick={() => navigate(`/sessions?chatSession=${encodeURIComponent(session.sessionId)}`)} variant="primary" className="!border-0" /> : null}
          {actions.trace ? <SessionActionButton label={t('sessions.openTrace')} icon={<Workflow size={14} />} onClick={() => navigate(`/agents?session=${encodeURIComponent(session.sessionId)}`)} className="!border-0 !bg-sky-500/10 !text-sky-300 hover:!bg-sky-500/20" /> : null}
          <SessionActionButton label={t('sessions.copySessionId')} icon={<Copy size={14} />} onClick={() => onCopy(session.sessionId)} variant="ghost" className="!border-0 !bg-transparent text-text-muted hover:!bg-surface-sunken hover:!text-text" />
        </div>
      )}
    >
      <div className="flex flex-col gap-4">
        <div className="flex flex-wrap items-center gap-2"><StatusBadge status={session.status} /><span className="rounded-full bg-surface-sunken px-2 py-0.5 text-xs text-text-muted">{origin.label}</span><span className="rounded-full bg-surface-sunken px-2 py-0.5 text-xs text-text-muted">{t(CATEGORY_LABEL_KEYS[origin.category])}</span></div>
        <dl className="grid grid-cols-2 gap-x-4 gap-y-4 text-xs sm:grid-cols-4">
          <div><dt className="text-text-subtle">{t('sessions.messagesSort')}</dt><dd className="mt-1 font-medium text-text">{session.messageCount}</dd></div>
          <div><dt className="text-text-subtle">{t('sessions.duration')}</dt><dd className="mt-1 text-text-muted">{formatDuration(session.startedAt, session.endedAt, session.lastActiveAt)}</dd></div>
          <div><dt className="text-text-subtle">{t('sessions.startedSort')}</dt><dd className="mt-1 text-text-muted">{formatTimestamp(session.startedAt ?? 0)}</dd></div>
          <div><dt className="text-text-subtle">{t('sessions.lastActivity')}</dt><dd className="mt-1 text-text-muted">{formatRelativeTime(session.lastActiveAt ?? session.startedAt ?? 0)}</dd></div>
        </dl>
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <div className="rounded-lg bg-surface-sunken/45 p-3"><p className="text-[10px] uppercase tracking-wide text-text-subtle">{t('sessions.input')}</p><p className="mt-1 text-sm font-semibold text-sky-400">{formatTokens(session.inputTokens)}</p></div>
          <div className="rounded-lg bg-surface-sunken/45 p-3"><p className="text-[10px] uppercase tracking-wide text-text-subtle">{t('sessions.output')}</p><p className="mt-1 text-sm font-semibold text-sky-400">{formatTokens(session.outputTokens)}</p></div>
          <div className="rounded-lg bg-surface-sunken/45 p-3"><p className="text-[10px] uppercase tracking-wide text-text-subtle">{t('sessions.cache')}</p><p className="mt-1 text-sm font-semibold text-violet-400">{formatTokens(session.cacheReadTokens)}</p></div>
          <div className="rounded-lg bg-surface-sunken/45 p-3"><p className="text-[10px] uppercase tracking-wide text-text-subtle">{t('sessions.costSort')}</p><p className="mt-1 text-sm font-semibold text-emerald-400">{formatCost(session.estimatedCostUsd)}</p></div>
        </div>
        {session.preview ? <div className="rounded-lg bg-surface-sunken/50 p-3 text-sm leading-relaxed text-text-muted">{session.preview}</div> : null}
        {session.recentMessages.length > 0 ? (
          <div className="space-y-2">
            <p className="text-[10px] font-semibold uppercase tracking-[0.12em] text-text-subtle">{t('sessions.recentMessages')}</p>
            {session.recentMessages.slice(-4).map((message, index) => <div key={`${message.timestamp ?? index}-${index}`} className={`rounded-md px-3 py-2 text-xs ${message.role === 'user' ? 'bg-accent/5 text-text' : 'bg-surface-sunken/40 text-text-muted'}`}><span className="mr-1 font-semibold">{message.role === 'user' ? t('sessions.you') : t('sessions.hermes')}:</span>{message.text}</div>)}
          </div>
        ) : null}
        <button type="button" onClick={() => onCopy(session.sessionId)} className="w-fit truncate font-mono text-[10px] text-text-subtle hover:text-accent" title={t('sessions.copySessionId')}>{session.sessionId}</button>
      </div>
    </Modal>
  );
}

export function SessionsRoute() {
  const { t, locale } = useI18n();
  const { storedToken } = useMissionControl();
  const [searchParams, setSearchParams] = useSearchParams();
  const [agentSessions, setAgentSessions] = useState<MissionControlAgentsSessionsSnapshot | null>(null);
  const [loadedItems, setLoadedItems] = useState<MissionControlAgentSessionItem[]>([]);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [lastSyncedAt, setLastSyncedAt] = useState<number | null>(null);
  const [filteredTotal, setFilteredTotal] = useState(0);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [filtersOpen, setFiltersOpen] = useState(false);
  const [collapsedGroups, setCollapsedGroups] = useState<Set<SessionCategory>>(new Set(['automation', 'system', 'unknown']));
  const [selectedSession, setSelectedSession] = useState<MissionControlAgentSessionItem | null>(null);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const tab = parseSessionTab(searchParams.get('tab'));
  const filters = useMemo(() => readSessionFilters(searchParams), [searchParams]);
  const sortKey = parseSessionSortKey(searchParams.get('sort'));
  const updateView = useCallback((patch: SessionViewPatch) => {
    const next = new URLSearchParams(searchParams);
    const setOrDelete = (key: typeof SESSION_VIEW_QUERY_KEYS[number], value: string | undefined, defaultValue = '') => {
      if (value && value !== defaultValue) next.set(key, value);
      else next.delete(key);
    };
    if ('tab' in patch) setOrDelete('tab', patch.tab, 'all');
    if ('query' in patch) setOrDelete('query', patch.query);
    if ('status' in patch) setOrDelete('status', patch.status);
    if ('origin' in patch) setOrDelete('origin', patch.origin);
    if ('model' in patch) setOrDelete('model', patch.model);
    if ('sortKey' in patch) setOrDelete('sort', patch.sortKey, 'activity');
    setSearchParams(next, { replace: true });
  }, [searchParams, setSearchParams]);
  const activeFilterCount = [filters.query, filters.status !== 'all' ? filters.status : '', filters.origin, filters.model].filter(Boolean).length;

  const effectiveFilters = useMemo(() => ({
    ...filters,
    status: tab === 'live' ? 'live' as const : filters.status,
    category: tab === 'all' || tab === 'live' ? filters.category : tab,
  }), [filters, tab]);

  const loadSessions = useCallback(async (silent = false) => {
    if (!silent) {
      setLoading(true);
      setLoadError(null);
      setFilteredTotal(0);
      setHasMore(false);
    }
    try {
      const data = await withTimeout(loadMissionControlAgentSessions(storedToken ?? undefined, PAGE_SIZE, 0, { ...filters, tab }), SESSION_LOAD_TIMEOUT_MS);
      setAgentSessions(data);
      setLoadedItems(data.items ?? []);
      setFilteredTotal(data.pagination.total);
      setHasMore(data.pagination.hasMore);
      setLoadError(null);
      setLastSyncedAt(Date.now());
    } catch (error) {
      const message = error instanceof Error ? error.message : 'Unable to load sessions.';
      setLoadError(message);
      if (!silent) {
        setAgentSessions(null);
        setLoadedItems([]);
        setFilteredTotal(0);
        setHasMore(false);
      }
    } finally {
      if (!silent) setLoading(false);
    }
  }, [effectiveFilters, storedToken]);

  const { state: pullState } = usePullToReload({ containerRef, onReload: async () => { await loadSessions(); } });

  useEffect(() => {
    void loadSessions();
    const interval = window.setInterval(() => {
      if (document.visibilityState === 'visible') void loadSessions(true);
    }, tab === 'live' ? LIVE_POLL_INTERVAL_MS : POLL_INTERVAL_MS);
    return () => window.clearInterval(interval);
  }, [loadSessions]);

  const loadMore = async () => {
    if (loadingMore) return;
    setLoadingMore(true);
    try {
      const next = await withTimeout(loadMissionControlAgentSessions(storedToken ?? undefined, PAGE_SIZE, loadedItems.length, { ...filters, tab }), SESSION_LOAD_TIMEOUT_MS);
      const nextItems = next.items ?? [];
      setLoadedItems((current) => [...current, ...nextItems]);
      setFilteredTotal(next.pagination.total);
      setHasMore(next.pagination.hasMore);
      setLoadError(null);
    } catch (error) {
      setLoadError(error instanceof Error ? error.message : 'Unable to load more sessions.');
    } finally {
      setLoadingMore(false);
    }
  };

  const facets = agentSessions?.facets;
  const totalSessions = agentSessions?.stats.totalSessions ?? 0;
  const liveCount = facets?.status.live ?? agentSessions?.stats.liveSessions ?? 0;
  const idleCount = facets?.status.idle ?? 0;
  const endedCount = facets?.status.ended ?? 0;
  const activeAgents = agentSessions?.stats.activeAgents ?? 0;
  const totalTokens = loadedItems.reduce((sum, session) => sum + session.inputTokens + session.outputTokens, 0);
  const totalCost = loadedItems.reduce((sum, session) => sum + session.estimatedCostUsd, 0);

  const originOptions = useMemo(() => Object.keys(facets?.origin ?? {}).sort(), [facets?.origin]);
  const modelOptions = useMemo(() => Object.keys(facets?.model ?? {}).sort(), [facets?.model]);
  const filteredSessions = useMemo(() => {
    const visible = loadedItems.filter((session) => matchesSessionFilters(session, effectiveFilters));
    return visible.sort((a, b) => {
      if (sortKey === 'started') return (b.startedAt ?? 0) - (a.startedAt ?? 0);
      if (sortKey === 'messages') return b.messageCount - a.messageCount;
      if (sortKey === 'tokens') return (b.inputTokens + b.outputTokens) - (a.inputTokens + a.outputTokens);
      if (sortKey === 'cost') return b.estimatedCostUsd - a.estimatedCostUsd;
      if (a.status !== b.status) return a.status === 'live' ? -1 : b.status === 'live' ? 1 : a.status === 'idle' ? -1 : 1;
      return (b.lastActiveAt ?? b.startedAt ?? 0) - (a.lastActiveAt ?? a.startedAt ?? 0);
    });
  }, [effectiveFilters, loadedItems, sortKey]);

  useEffect(() => {
    if (selectedSession && !loadedItems.some((session) => session.sessionId === selectedSession.sessionId)) setSelectedSession(null);
  }, [loadedItems, selectedSession]);

  const liveSessions = filteredSessions.filter((session) => session.status === 'live');
  const groupedSessions = useMemo(() => {
    const groups = new Map<SessionCategory, MissionControlAgentSessionItem[]>();
    for (const category of CATEGORY_ORDER) groups.set(category, []);
    for (const session of filteredSessions) {
      if (session.status === 'live') continue;
      const category = classifySessionOrigin(session).category;
      groups.get(category)?.push(session);
    }
    return groups;
  }, [filteredSessions]);

  const tabCount = (value: SessionTab): number | null => {
    if (value === tab) return loading ? null : filteredTotal;
    if (value === 'all' || value === 'live' || value === 'conversation' || value === 'automation' || value === 'system') {
      return agentSessions?.tabCounts[value] ?? (value === 'all' ? totalSessions : value === 'live' ? liveCount : facets?.category[value] ?? 0);
    }
    return facets?.category.unknown ?? 0;
  };

  const selectTab = (value: SessionTab) => {
    updateView({ tab: value });
    setCollapsedGroups(new Set());
  };
  const copySessionId = (sessionId: string) => { void navigator.clipboard?.writeText(sessionId); };
  const toggleGroup = (category: SessionCategory) => setCollapsedGroups((current) => { const next = new Set(current); if (next.has(category)) next.delete(category); else next.add(category); return next; });
  const groupsForcedOpen = tab !== 'all' || activeFilterCount > 0;

  useEffect(() => {
    if (tab !== 'all' || activeFilterCount > 0) setCollapsedGroups(new Set());
  }, [activeFilterCount, tab]);

  return (
    <div ref={containerRef} className="route-page-scroll flex flex-col gap-4 sm:gap-5">
      <PullToReloadIndicator state={pullState} />
      <Card padding="none" className="!border-0">
        <PageHeader
          eyebrow={t('nav.sessions')}
          title={t('sessions.controlTitle')}
          description={t('sessions.description')}
          meta={lastSyncedAt ? t('sessions.updated', { time: formatRelativeTime(lastSyncedAt / 1000) }) : t('sessions.notSynced')}
          actions={<button type="button" onClick={() => void loadSessions()} className="inline-flex items-center justify-center gap-1.5 rounded-md bg-surface px-2.5 py-1.5 text-text-muted hover:bg-surface-sunken hover:text-text !px-0 sm:!px-2.5" aria-label={t('sessions.refresh')} title={t('sessions.refresh')} disabled={loading}>
            <RefreshCw size={13} className={loading ? 'animate-spin' : ''} /><span className="hidden sm:inline">{t('sessions.refresh')}</span>
          </button>}
        />
        <div className="grid grid-cols-4 gap-1.5 p-2 sm:grid-cols-4 sm:gap-3 sm:p-4 xl:grid-cols-6">
          <MetricCard icon={MessagesSquare} label={t('sessions.total')} value={String(totalSessions)} hint={t('sessions.trackedLabel')} compact />
          <MetricCard icon={Activity} label={t('sessions.liveNow')} value={String(liveCount)} hint={t('sessions.active')} compact />
          <MetricCard icon={Clock3} label={t('sessions.idleStatus')} value={String(idleCount)} hint={t('sessions.lastActivity')} compact />
          <MetricCard icon={Layers} label={t('sessions.endedStatus')} value={String(endedCount)} hint={t('sessions.latest')} compact />
          <MetricCard icon={Bot} label={t('sessions.agents')} value={String(activeAgents)} hint={t('sessions.active')} className="hidden sm:block" />
          <MetricCard icon={DollarSign} label={t('sessions.cost')} value={formatCost(totalCost)} hint={t('sessions.tokensCount', { count: formatTokens(totalTokens) })} className="hidden sm:block" />
        </div>
      </Card>

      <div className="flex min-h-0 flex-1 flex-col gap-4">
        <Card padding="none" className="min-w-0 !border-0" id="sessions-results" aria-busy={loading}>
          <div className="p-3">
            <div role="tablist" aria-label={t('sessions.viewTabs')} className="flex flex-nowrap gap-1.5 overflow-x-auto pb-1 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden sm:flex-wrap sm:overflow-visible sm:pb-0">
              {(['all', 'live', 'conversation', 'automation', 'system'] as SessionTab[]).map((value) => (
                <button key={value} type="button" role="tab" aria-selected={tab === value} aria-controls="sessions-results" onClick={() => selectTab(value)} className={`shrink-0 whitespace-nowrap rounded-full px-3 py-1.5 text-xs font-medium transition-colors ${tab === value ? 'bg-accent text-white' : 'bg-surface-sunken text-text-muted hover:bg-border-subtle hover:text-text'}`}>
                  {value === 'all' ? t('sessions.all') : value === 'live' ? t('sessions.liveNow') : t(CATEGORY_LABEL_KEYS[value])} <span className="ml-1 opacity-70">{tabCount(value) === null ? '…' : tabCount(value)}</span>
                </button>
              ))}
            </div>
            <div className="mt-3 md:flex md:items-center md:gap-2">
              <label className="relative block min-w-0 md:flex-1">
                <Search size={15} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-text-subtle" />
                <input value={filters.query} onChange={(event) => updateView({ query: event.target.value })} placeholder={t('sessions.searchPlaceholder')} aria-label={t('sessions.searchPlaceholder')} className="w-full min-w-0 rounded-md bg-surface h-9 py-0 pl-9 pr-3 text-sm text-text outline-none placeholder:text-text-subtle focus:ring-1 focus:ring-accent/40" />
              </label>
              <div className="mt-2 flex items-center justify-between md:hidden">
                <span className="text-[11px] text-text-subtle">{activeFilterCount ? t('sessions.activeFilters', { count: activeFilterCount, suffix: locale === 'it' ? (activeFilterCount > 1 ? 'i' : 'o') : activeFilterCount > 1 ? 's' : '' }) : t('sessions.noFilters')}</span>
                <Button type="button" size="sm" variant="ghost" icon={<SlidersHorizontal size={14} />} className="!min-w-0 !border-0 !bg-transparent !px-2 text-xs text-text-muted hover:!bg-surface-sunken hover:!text-text" aria-expanded={filtersOpen} onClick={() => setFiltersOpen((open) => !open)}>{filtersOpen ? t('sessions.hideFilters') : t('sessions.filters')}</Button>
              </div>
              <div className={`${filtersOpen ? 'grid' : 'hidden'} mt-2 grid-cols-2 gap-2 md:mt-0 md:flex md:min-w-0 md:flex-1 md:gap-2`}>
                <select value={filters.status} onChange={(event) => updateView({ status: event.target.value as SessionViewFilters['status'] })} className="min-w-0 w-full rounded-md bg-surface h-9 px-3 py-0 text-xs text-text-muted outline-none focus:ring-1 focus:ring-accent/40 md:flex-1" aria-label={t('sessions.allStatuses')}>
                  <option value="all">{t('sessions.allStatuses')}</option><option value="live">{t('sessions.liveStatus')}</option><option value="idle">{t('sessions.idleStatus')}</option><option value="ended">{t('sessions.endedStatus')}</option>
                </select>
                <select value={filters.origin} onChange={(event) => updateView({ origin: event.target.value })} className="min-w-0 w-full rounded-md bg-surface h-9 px-3 py-0 text-xs text-text-muted outline-none focus:ring-1 focus:ring-accent/40 md:flex-1" aria-label={t('sessions.allOrigins')}>
                  <option value="">{t('sessions.allOrigins')}</option>{originOptions.map((origin) => <option key={origin} value={origin}>{origin}</option>)}
                </select>
                <select value={filters.model} onChange={(event) => updateView({ model: event.target.value })} className="min-w-0 w-full rounded-md bg-surface h-9 px-3 py-0 text-xs text-text-muted outline-none focus:ring-1 focus:ring-accent/40 md:flex-1" aria-label={t('sessions.allModels')}>
                  <option value="">{t('sessions.allModels')}</option>{modelOptions.map((model) => <option key={model} value={model}>{model}</option>)}
                </select>
                <select value={sortKey} onChange={(event) => updateView({ sortKey: event.target.value as SortKey })} className="min-w-0 w-full rounded-md bg-surface h-9 px-3 py-0 text-xs text-text-muted outline-none focus:ring-1 focus:ring-accent/40 md:flex-1" aria-label={t('sessions.costSort')}>
                  <option value="activity">{t('sessions.lastActivitySort')}</option><option value="started">{t('sessions.startedSort')}</option><option value="messages">{t('sessions.messagesSort')}</option><option value="tokens">{t('sessions.tokensSort')}</option><option value="cost">{t('sessions.costSort')}</option>
                </select>
              </div>
            </div>
            <div className="mt-2 flex flex-col gap-1 text-[11px] text-text-subtle sm:flex-row sm:items-center sm:justify-between"><span className="min-w-0 break-words">{loading ? t('sessions.loading') : t('sessions.shownLoadedOf', { shown: filteredSessions.length, loaded: loadedItems.length, total: filteredTotal })}</span>{tab === 'live' ? <span className="text-positive">{t('sessions.autoRefresh5')}</span> : <span>{t('sessions.autoRefresh10')}</span>}</div>
          </div>

          {loadError ? <div className="px-4 py-8 text-center"><p className="text-sm font-medium text-text">{t('sessions.unableToLoad')}</p><p className="mt-1 text-xs text-text-muted">{loadError}</p><button type="button" onClick={() => void loadSessions()} className="mt-4 rounded-md bg-accent px-3 py-2 text-xs font-medium text-white hover:bg-accent-hover">{t('sessions.retry')}</button></div> : null}
          {!loadError && loading && !loadingMore ? <div role="status" aria-live="polite" className="flex items-center justify-center gap-2 bg-surface-sunken/35 px-4 py-3 text-sm text-text-muted"><RefreshCw size={14} className="animate-spin" />{t('sessions.loading')}</div> : null}
          {!loadError && !loading && filteredSessions.length === 0 ? <div className="px-4 py-12 text-center text-sm text-text-muted">{t('sessions.noMatches')}</div> : null}
          {liveSessions.length > 0 ? (
            <section>
              <div className="flex items-center gap-2 bg-positive/5 px-4 py-2.5"><span className="h-2 w-2 animate-pulse rounded-full bg-positive" /><span className="text-xs font-semibold uppercase tracking-[0.12em] text-positive">{t('sessions.liveNow')}</span><Badge variant="positive">{liveSessions.length}</Badge></div>
              {liveSessions.map((session) => <SessionRow key={session.sessionId} session={session} selected={session.sessionId === selectedSession?.sessionId} onInspect={setSelectedSession} onCopy={copySessionId} />)}
            </section>
          ) : null}
          {CATEGORY_ORDER.map((category) => {
            const sessions = groupedSessions.get(category) ?? [];
            if (!sessions.length) return null;
            return <SessionSection key={category} category={category} sessions={sessions} collapsed={groupsForcedOpen ? false : collapsedGroups.has(category)} onToggle={groupsForcedOpen ? () => undefined : () => toggleGroup(category)} selectedId={selectedSession?.sessionId ?? null} onInspect={setSelectedSession} onCopy={copySessionId} />;
          })}
          {hasMore ? <div className="p-3"><button type="button" onClick={() => void loadMore()} disabled={loadingMore} className="w-full rounded-md py-2 text-xs font-medium text-accent hover:bg-surface-sunken disabled:opacity-50">{loadingMore ? t('sessions.loading') : t('sessions.loadMoreOf', { loaded: loadedItems.length, total: filteredTotal })}</button></div> : null}
        </Card>
        {selectedSession ? <SessionDetails session={selectedSession} onClose={() => setSelectedSession(null)} onCopy={copySessionId} /> : null}
      </div>
    </div>
  );
}
