import { useI18n } from '../lib/i18n';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
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

type SessionTab = 'all' | 'live' | SessionCategory;
type SortKey = 'activity' | 'started' | 'messages' | 'tokens' | 'cost';

const CATEGORY_ORDER: SessionCategory[] = ['conversation', 'automation', 'system', 'unknown'];
const CATEGORY_LABELS: Record<SessionCategory, string> = {
  conversation: 'Conversations',
  automation: 'Automations',
  system: 'System',
  unknown: 'Other',
};

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

function MetricCard({ icon: Icon, label, value, hint }: { icon: React.ElementType; label: string; value: string; hint: string }) {
  return (
    <Card className="!border-0 p-4">
      <div className="flex items-center justify-between">
        <span className="text-xs text-text-muted">{label}</span>
        <Icon className="h-4 w-4 text-text-subtle" />
      </div>
      <p className="mt-2 text-lg font-semibold text-text tabular-nums">{value}</p>
      <p className="mt-1 text-xs text-text-subtle">{hint}</p>
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
  return <Button type="button" size="sm" variant={variant} icon={icon} className={className} onClick={onClick}>{label}</Button>;
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
  const origin = classifySessionOrigin(session);
  const actions = getSessionActionAvailability(session);
  const navigate = useNavigate();
  const totalTokens = session.inputTokens + session.outputTokens;
  const lastActive = session.lastActiveAt ?? session.startedAt ?? 0;

  return (
    <article className={`group px-4 py-3 transition-colors ${
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
            <button type="button" onClick={() => onInspect(session)} className="min-w-0 text-left">
              <p className="truncate text-sm font-semibold text-text hover:text-accent">{session.title}</p>
              <p className="mt-0.5 truncate text-xs text-text-muted">{session.preview || 'No preview available.'}</p>
            </button>
            <StatusBadge status={session.status} />
          </div>

          <div className="mt-2 flex flex-wrap items-center gap-1.5 text-[11px] text-text-subtle">
            <span className="rounded-full bg-surface px-2 py-0.5 font-medium text-text-muted">{origin.label}</span>
            <span className="rounded-full bg-surface px-2 py-0.5 text-text-subtle">{origin.category}</span>
            <span className="max-w-[15rem] truncate">{session.model}</span>
            <span>·</span>
            <span>{session.messageCount} msgs</span>
            <span>·</span>
            <span title={formatTimestamp(lastActive)}>{session.status === 'live' ? 'Active' : 'Last activity'} {formatRelativeTime(lastActive)}</span>
          </div>

          <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-text-subtle">
            {totalTokens > 0 ? <span className="text-sky-400">{formatTokens(totalTokens)} tokens</span> : null}
            {session.estimatedCostUsd > 0 ? <span className="text-emerald-400">{formatCost(session.estimatedCostUsd)}</span> : null}
            <button type="button" onClick={() => onCopy(session.sessionId)} className="inline-flex min-w-0 max-w-[18rem] items-center gap-1 font-mono text-[10px] text-text-subtle hover:text-accent" title="Copy session ID">
              <Copy size={10} />
              <span className="truncate">{session.sessionId}</span>
            </button>
            <span className="ml-auto flex flex-wrap items-center justify-end gap-1.5">
              {actions.resumeChat ? <SessionActionButton label="Resume" icon={<MessageSquare size={14} />} onClick={() => navigate(`/sessions?chatSession=${encodeURIComponent(session.sessionId)}`)} variant="primary" className="!min-w-0 !border-0 !px-3" /> : null}
              {actions.trace ? <SessionActionButton label="Trace" icon={<Workflow size={14} />} onClick={() => navigate(`/agents?session=${encodeURIComponent(session.sessionId)}`)} className="!min-w-0 !border-0 !bg-sky-500/10 !text-sky-300 hover:!bg-sky-500/20 !px-3" /> : null}
              {actions.inspect ? <SessionActionButton label="Inspect" icon={<Search size={14} />} onClick={() => onInspect(session)} variant="ghost" className="!min-w-0 !border-0 !bg-transparent !px-3 text-text-muted hover:!bg-surface-sunken hover:!text-text" /> : null}
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
  const liveCount = sessions.filter((session) => session.status === 'live').length;
  return (
    <section>
      <button type="button" onClick={onToggle} className="flex w-full items-center gap-2 bg-surface-sunken/45 px-4 py-2.5 text-left hover:bg-surface-sunken/70">
        <ChevronDown size={14} className={`text-text-subtle transition-transform ${collapsed ? '-rotate-90' : ''}`} />
        <span className="text-xs font-semibold uppercase tracking-[0.12em] text-text-muted">{CATEGORY_LABELS[category]}</span>
        <span className="text-xs text-text-subtle">{sessions.length}</span>
        {liveCount > 0 ? <Badge variant="positive" dot>{liveCount} live</Badge> : null}
      </button>
      {!collapsed ? sessions.map((session) => <SessionRow key={session.sessionId} session={session} selected={session.sessionId === selectedId} onInspect={onInspect} onCopy={onCopy} />) : null}
    </section>
  );
}

function SessionDetails({ session, onClose, onCopy }: { session: MissionControlAgentSessionItem; onClose: () => void; onCopy: (sessionId: string) => void }) {
  const origin = classifySessionOrigin(session);
  const actions = getSessionActionAvailability(session);
  const navigate = useNavigate();
  return (
    <Modal
      open
      title={session.title}
      subtitle={`${origin.label} · ${origin.category} · ${session.model}`}
      onClose={onClose}
      borderless
      footer={(
        <div className="flex flex-wrap gap-2">
          {actions.resumeChat ? <SessionActionButton label="Resume chat" icon={<MessageSquare size={14} />} onClick={() => navigate(`/sessions?chatSession=${encodeURIComponent(session.sessionId)}`)} variant="primary" className="!border-0" /> : null}
          {actions.trace ? <SessionActionButton label="Open trace" icon={<Workflow size={14} />} onClick={() => navigate(`/agents?session=${encodeURIComponent(session.sessionId)}`)} className="!border-0 !bg-sky-500/10 !text-sky-300 hover:!bg-sky-500/20" /> : null}
          <SessionActionButton label="Copy ID" icon={<Copy size={14} />} onClick={() => onCopy(session.sessionId)} variant="ghost" className="!border-0 !bg-transparent text-text-muted hover:!bg-surface-sunken hover:!text-text" />
        </div>
      )}
    >
      <div className="flex flex-col gap-4">
        <div className="flex flex-wrap items-center gap-2"><StatusBadge status={session.status} /><span className="rounded-full bg-surface-sunken px-2 py-0.5 text-xs text-text-muted">{origin.label}</span><span className="rounded-full bg-surface-sunken px-2 py-0.5 text-xs text-text-muted">{origin.category}</span></div>
        <dl className="grid grid-cols-2 gap-x-4 gap-y-4 text-xs sm:grid-cols-4">
          <div><dt className="text-text-subtle">Messages</dt><dd className="mt-1 font-medium text-text">{session.messageCount}</dd></div>
          <div><dt className="text-text-subtle">Duration</dt><dd className="mt-1 text-text-muted">{formatDuration(session.startedAt, session.endedAt, session.lastActiveAt)}</dd></div>
          <div><dt className="text-text-subtle">Started</dt><dd className="mt-1 text-text-muted">{formatTimestamp(session.startedAt ?? 0)}</dd></div>
          <div><dt className="text-text-subtle">Last activity</dt><dd className="mt-1 text-text-muted">{formatRelativeTime(session.lastActiveAt ?? session.startedAt ?? 0)}</dd></div>
        </dl>
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <div className="rounded-lg bg-surface-sunken/45 p-3"><p className="text-[10px] uppercase tracking-wide text-text-subtle">Input</p><p className="mt-1 text-sm font-semibold text-sky-400">{formatTokens(session.inputTokens)}</p></div>
          <div className="rounded-lg bg-surface-sunken/45 p-3"><p className="text-[10px] uppercase tracking-wide text-text-subtle">Output</p><p className="mt-1 text-sm font-semibold text-sky-400">{formatTokens(session.outputTokens)}</p></div>
          <div className="rounded-lg bg-surface-sunken/45 p-3"><p className="text-[10px] uppercase tracking-wide text-text-subtle">Cache</p><p className="mt-1 text-sm font-semibold text-violet-400">{formatTokens(session.cacheReadTokens)}</p></div>
          <div className="rounded-lg bg-surface-sunken/45 p-3"><p className="text-[10px] uppercase tracking-wide text-text-subtle">Cost</p><p className="mt-1 text-sm font-semibold text-emerald-400">{formatCost(session.estimatedCostUsd)}</p></div>
        </div>
        {session.preview ? <div className="rounded-lg bg-surface-sunken/50 p-3 text-sm leading-relaxed text-text-muted">{session.preview}</div> : null}
        {session.recentMessages.length > 0 ? (
          <div className="space-y-2">
            <p className="text-[10px] font-semibold uppercase tracking-[0.12em] text-text-subtle">Recent messages</p>
            {session.recentMessages.slice(-4).map((message, index) => <div key={`${message.timestamp ?? index}-${index}`} className={`rounded-md px-3 py-2 text-xs ${message.role === 'user' ? 'bg-accent/5 text-text' : 'bg-surface-sunken/40 text-text-muted'}`}><span className="mr-1 font-semibold">{message.role === 'user' ? 'You' : 'Hermes'}:</span>{message.text}</div>)}
          </div>
        ) : null}
        <button type="button" onClick={() => onCopy(session.sessionId)} className="w-fit truncate font-mono text-[10px] text-text-subtle hover:text-accent" title="Copy session ID">{session.sessionId}</button>
      </div>
    </Modal>
  );
}

export function SessionsRoute() {
  const { t } = useI18n();
  const { storedToken } = useMissionControl();
  const [agentSessions, setAgentSessions] = useState<MissionControlAgentsSessionsSnapshot | null>(null);
  const [loadedItems, setLoadedItems] = useState<MissionControlAgentSessionItem[]>([]);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [lastSyncedAt, setLastSyncedAt] = useState<number | null>(null);
  const [filteredTotal, setFilteredTotal] = useState(0);
  const [tab, setTab] = useState<SessionTab>('all');
  const [filters, setFilters] = useState<SessionViewFilters>({ query: '', status: 'all', category: 'all', origin: '', model: '' });
  const [sortKey, setSortKey] = useState<SortKey>('activity');
  const [collapsedGroups, setCollapsedGroups] = useState<Set<SessionCategory>>(new Set(['automation', 'system', 'unknown']));
  const [selectedSession, setSelectedSession] = useState<MissionControlAgentSessionItem | null>(null);
  const containerRef = useRef<HTMLDivElement | null>(null);

  const effectiveFilters = useMemo(() => ({
    ...filters,
    status: tab === 'live' ? 'live' as const : filters.status,
    category: tab === 'all' || tab === 'live' ? filters.category : tab,
  }), [filters, tab]);

  const loadSessions = useCallback(async (silent = false) => {
    if (!silent) setLoading(true);
    try {
      const data = await loadMissionControlAgentSessions(storedToken ?? undefined, PAGE_SIZE, 0, effectiveFilters);
      setAgentSessions(data);
      setLoadedItems(data.items ?? []);
      setFilteredTotal(data.pagination.total);
      setHasMore(data.pagination.hasMore);
      setLastSyncedAt(Date.now());
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
      const next = await loadMissionControlAgentSessions(storedToken ?? undefined, PAGE_SIZE, loadedItems.length, effectiveFilters);
      const nextItems = next.items ?? [];
      setLoadedItems((current) => [...current, ...nextItems]);
      setFilteredTotal(next.pagination.total);
      setHasMore(next.pagination.hasMore);
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
  const modelOptions = useMemo(() => [...new Set(loadedItems.map((session) => session.model).filter(Boolean))].sort(), [loadedItems]);
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

  const tabCount = (value: SessionTab): number => {
    if (value === 'all') return totalSessions;
    if (value === 'live') return liveCount;
    if (value === 'conversation' || value === 'automation' || value === 'system') return facets?.category[value] ?? loadedItems.filter((session) => classifySessionOrigin(session).category === value).length;
    return (facets?.category.unknown ?? 0);
  };

  const copySessionId = (sessionId: string) => { void navigator.clipboard?.writeText(sessionId); };
  const toggleGroup = (category: SessionCategory) => setCollapsedGroups((current) => { const next = new Set(current); if (next.has(category)) next.delete(category); else next.add(category); return next; });

  return (
    <div ref={containerRef} className="flex h-full flex-col gap-5 overflow-y-auto">
      <PullToReloadIndicator state={pullState} />
      <Card padding="none" className="!border-0">
        <div className="flex flex-wrap items-start justify-between gap-4 px-4 pb-4 pt-4">
          <div>
            <span className="eyebrow">{t('nav.sessions')}</span>
            <h2 className="mt-1 text-base font-semibold text-text">Session Control</h2>
            <p className="mt-1 text-xs text-text-muted">Conversations, automated runs, and runtime history in one place.</p>
          </div>
          <div className="flex items-center gap-3 text-xs text-text-subtle">
            <span>{lastSyncedAt ? `Updated ${formatRelativeTime(lastSyncedAt / 1000)}` : 'Not synced yet'}</span>
            <button type="button" onClick={() => void loadSessions()} className="inline-flex items-center gap-1.5 rounded-md bg-surface px-2.5 py-1.5 text-text-muted hover:bg-surface-sunken hover:text-text" disabled={loading}>
              <RefreshCw size={13} className={loading ? 'animate-spin' : ''} /> Refresh
            </button>
          </div>
        </div>
        <div className="grid grid-cols-2 gap-3 p-4 sm:grid-cols-4 xl:grid-cols-6">
          <MetricCard icon={MessagesSquare} label="Total" value={String(totalSessions)} hint="tracked sessions" />
          <MetricCard icon={Activity} label="Live now" value={String(liveCount)} hint="active in last 5m" />
          <MetricCard icon={Clock3} label="Idle" value={String(idleCount)} hint="not recently active" />
          <MetricCard icon={Layers} label="Ended" value={String(endedCount)} hint="completed history" />
          <MetricCard icon={Bot} label="Agents" value={String(activeAgents)} hint="currently active" />
          <MetricCard icon={DollarSign} label="Loaded usage" value={formatCost(totalCost)} hint={`${formatTokens(totalTokens)} tokens`} />
        </div>
      </Card>

      <div className="flex min-h-0 flex-1 flex-col gap-4">
        <Card padding="none" className="min-w-0 !border-0">
          <div className="p-3">
            <div className="flex flex-wrap gap-1.5">
              {(['all', 'live', 'conversation', 'automation', 'system'] as SessionTab[]).map((value) => (
                <button key={value} type="button" onClick={() => setTab(value)} className={`rounded-full px-3 py-1.5 text-xs font-medium transition-colors ${tab === value ? 'bg-accent text-white' : 'bg-surface-sunken text-text-muted hover:bg-border-subtle hover:text-text'}`}>
                  {value === 'all' ? 'All' : value === 'live' ? 'Live now' : CATEGORY_LABELS[value]} <span className="ml-1 opacity-70">{tabCount(value)}</span>
                </button>
              ))}
            </div>
            <div className="mt-3 flex flex-col gap-2 md:flex-row">
              <label className="relative min-w-0 flex-1">
                <Search size={15} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-text-subtle" />
                <input value={filters.query} onChange={(event) => setFilters((current) => ({ ...current, query: event.target.value }))} placeholder="Search title, preview, ID, model…" className="w-full rounded-md bg-surface py-2 pl-9 pr-3 text-sm text-text outline-none placeholder:text-text-subtle focus:ring-1 focus:ring-accent/40" />
              </label>
              <select value={filters.status} onChange={(event) => setFilters((current) => ({ ...current, status: event.target.value as SessionViewFilters['status'] }))} className="rounded-md bg-surface px-3 py-2 text-xs text-text-muted outline-none focus:ring-1 focus:ring-accent/40" aria-label="Filter status">
                <option value="all">All statuses</option><option value="live">Live</option><option value="idle">Idle</option><option value="ended">Ended</option>
              </select>
              <select value={filters.origin} onChange={(event) => setFilters((current) => ({ ...current, origin: event.target.value }))} className="rounded-md bg-surface px-3 py-2 text-xs text-text-muted outline-none focus:ring-1 focus:ring-accent/40" aria-label="Filter origin">
                <option value="">All origins</option>{originOptions.map((origin) => <option key={origin} value={origin}>{origin}</option>)}
              </select>
              <select value={filters.model} onChange={(event) => setFilters((current) => ({ ...current, model: event.target.value }))} className="max-w-full rounded-md bg-surface px-3 py-2 text-xs text-text-muted outline-none focus:ring-1 focus:ring-accent/40" aria-label="Filter model">
                <option value="">All models</option>{modelOptions.map((model) => <option key={model} value={model}>{model}</option>)}
              </select>
              <select value={sortKey} onChange={(event) => setSortKey(event.target.value as SortKey)} className="rounded-md bg-surface px-3 py-2 text-xs text-text-muted outline-none focus:ring-1 focus:ring-accent/40" aria-label="Sort sessions">
                <option value="activity">Last activity</option><option value="started">Started</option><option value="messages">Messages</option><option value="tokens">Tokens</option><option value="cost">Cost</option>
              </select>
            </div>
            <div className="mt-2 flex items-center justify-between text-[11px] text-text-subtle"><span>{filteredSessions.length} shown · {loadedItems.length} loaded of {filteredTotal}</span>{tab === 'live' ? <span className="text-positive">Auto-refreshing every 5s</span> : <span>Auto-refreshing every 10s</span>}</div>
          </div>

          {loading && loadedItems.length === 0 ? <div className="px-4 py-12 text-center text-sm text-text-muted">Loading sessions…</div> : null}
          {!loading && filteredSessions.length === 0 ? <div className="px-4 py-12 text-center text-sm text-text-muted">No sessions match these filters.</div> : null}
          {liveSessions.length > 0 ? (
            <section>
              <div className="flex items-center gap-2 bg-positive/5 px-4 py-2.5"><span className="h-2 w-2 animate-pulse rounded-full bg-positive" /><span className="text-xs font-semibold uppercase tracking-[0.12em] text-positive">Live now</span><Badge variant="positive">{liveSessions.length}</Badge></div>
              {liveSessions.map((session) => <SessionRow key={session.sessionId} session={session} selected={session.sessionId === selectedSession?.sessionId} onInspect={setSelectedSession} onCopy={copySessionId} />)}
            </section>
          ) : null}
          {CATEGORY_ORDER.map((category) => {
            const sessions = groupedSessions.get(category) ?? [];
            if (!sessions.length) return null;
            return <SessionSection key={category} category={category} sessions={sessions} collapsed={collapsedGroups.has(category)} onToggle={() => toggleGroup(category)} selectedId={selectedSession?.sessionId ?? null} onInspect={setSelectedSession} onCopy={copySessionId} />;
          })}
          {hasMore ? <div className="p-3"><button type="button" onClick={() => void loadMore()} disabled={loadingMore} className="w-full rounded-md py-2 text-xs font-medium text-accent hover:bg-surface-sunken disabled:opacity-50">{loadingMore ? 'Loading…' : `Load more · ${loadedItems.length} of ${totalSessions}`}</button></div> : null}
        </Card>
        {selectedSession ? <SessionDetails session={selectedSession} onClose={() => setSelectedSession(null)} onCopy={copySessionId} /> : null}
      </div>
    </div>
  );
}
