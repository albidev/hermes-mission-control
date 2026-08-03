import { useEffect, useMemo, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { Activity, Bot, Clock3, Copy, DollarSign, Layers, MessageSquare, MessagesSquare, Workflow } from 'lucide-react';
import { Card } from '../components/ui/Card';
import { Badge } from '../components/ui/Badge';
import { formatRelativeTime, formatTimestamp } from '../lib/format';
import { useMissionControl } from '../lib/mission-control-store';
import { usePullToReload } from '../hooks/usePullToReload';
import { PullToReloadIndicator } from '../components/PullToReloadIndicator';
import {
  loadMissionControlAgentSessions,
  type MissionControlAgentSessionItem,
  type MissionControlAgentsSessionsSnapshot,
} from '../lib/hermes-api';

const PAGE_SIZE = 50;

function MetricCard({
  icon: Icon,
  label,
  value,
  hint,
}: {
  icon: React.ElementType;
  label: string;
  value: string;
  hint: string;
}) {
  return (
    <Card className="p-4">
      <div className="flex items-center justify-between">
        <span className="text-xs text-text-muted">{label}</span>
        <Icon className="h-4 w-4 text-text-subtle" />
      </div>
      <p className="text-lg font-semibold text-text mt-2">{value}</p>
      <p className="text-xs text-text-subtle mt-1">{hint}</p>
    </Card>
  );
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

function SessionChatPreview({ session }: { session: MissionControlAgentSessionItem }) {
  const messages = session.recentMessages ?? [];

  return (
    <div className="rounded-xl border border-violet-400/20 bg-violet-500/[0.045] overflow-hidden">
      <div className="px-3 py-2 flex items-center justify-between gap-3 border-b border-violet-400/10">
        <div className="inline-flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-[0.08em] text-violet-300">
          <MessageSquare size={13} aria-hidden />
          <span>Recent chat</span>
        </div>
        <span className="text-[10px] text-text-subtle">
          {messages.length > 0 ? `${messages.length} latest messages` : 'Preview unavailable'}
        </span>
      </div>

      <div className="px-2.5 py-2.5 space-y-2">
        {messages.length > 0 ? messages.map((message, index) => {
          const isUser = message.role === 'user';
          return (
            <div key={`${message.role}-${message.timestamp ?? 'na'}-${index}`} className={`flex ${isUser ? 'justify-end' : 'justify-start'}`}>
              <div
                className={`max-w-[92%] rounded-lg px-2.5 py-2 border ${
                  isUser
                    ? 'border-violet-400/20 bg-violet-500/15'
                    : 'border-border-subtle bg-background/35 border-l-2 border-l-sky-400/60'
                }`}
              >
                <p className={`text-[10px] font-semibold uppercase tracking-[0.08em] ${isUser ? 'text-violet-300' : 'text-sky-300'}`}>
                  {isUser ? 'You' : 'Hermes'}
                </p>
                <p className="mt-1 text-xs leading-relaxed text-text-muted line-clamp-3">{message.text}</p>
              </div>
            </div>
          );
        }) : (
          <p className="text-xs leading-relaxed text-text-muted line-clamp-3">
            {session.preview || 'No recent messages available.'}
          </p>
        )}
      </div>
    </div>
  );
}

export function SessionsRoute() {
  const { snapshot, storedToken } = useMissionControl();
  const [agentSessions, setAgentSessions] = useState<MissionControlAgentsSessionsSnapshot | null>(null);
  const [loadedItems, setLoadedItems] = useState<MissionControlAgentSessionItem[]>([]);
  const [hasMore, setHasMore] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const containerRef = useRef<HTMLDivElement | null>(null);

  const loadSessions = async () => {
    const data = await loadMissionControlAgentSessions(storedToken ?? undefined, PAGE_SIZE, 0);
    setAgentSessions(data);
    setLoadedItems(data.items ?? []);
    setHasMore((data.items?.length ?? 0) >= PAGE_SIZE);
  };

  const { state: pullState } = usePullToReload({
    containerRef,
    onReload: async () => {
      await loadSessions();
    },
  });

  useEffect(() => {
    let cancelled = false;
    loadMissionControlAgentSessions(storedToken ?? undefined, PAGE_SIZE, 0).then((data) => {
      if (!cancelled) {
        setAgentSessions(data);
        setLoadedItems(data.items ?? []);
        setHasMore((data.items?.length ?? 0) >= PAGE_SIZE);
      }
    });
    return () => { cancelled = true; };
  }, [storedToken]);

  const agentSessionMap = useMemo(() => {
    const map = new Map<string, MissionControlAgentSessionItem>();
    if (loadedItems.length) {
      for (const item of loadedItems) {
        map.set(item.sessionId, item);
      }
    }
    return map;
  }, [loadedItems]);

  const sortedSessions = useMemo(
    () => [...loadedItems].sort((a, b) => (b.lastActiveAt ?? b.startedAt ?? 0) - (a.lastActiveAt ?? a.startedAt ?? 0)),
    [loadedItems],
  );

  const loadMore = async () => {
    if (loadingMore) return;
    setLoadingMore(true);
    try {
      const next = await loadMissionControlAgentSessions(storedToken ?? undefined, PAGE_SIZE, loadedItems.length);
      const nextItems = next.items ?? [];
      setLoadedItems((prev) => [...prev, ...nextItems]);
      setHasMore(nextItems.length >= PAGE_SIZE);
    } finally {
      setLoadingMore(false);
    }
  };

  const totalTokens = agentSessions?.items.reduce((sum, s) => sum + s.inputTokens + s.outputTokens, 0) ?? 0;
  const totalCost = agentSessions?.items.reduce((sum, s) => sum + s.estimatedCostUsd, 0) ?? 0;

  return (
    <div ref={containerRef} className="flex flex-col gap-6 h-full overflow-y-auto">
      <PullToReloadIndicator state={pullState} />
      <Card padding="none">
        <div className="px-4 pt-4 pb-3 border-b border-border-subtle flex items-center justify-between">
          <div className="flex flex-col gap-0.5">
            <span className="eyebrow">Sessions</span>
            <h2 className="text-sm font-semibold text-text">Conversation runtime archive</h2>
          </div>
          <Badge variant="default">{snapshot.sessions.totalSessions} tracked</Badge>
        </div>

        <div className="p-4 grid grid-cols-2 xl:grid-cols-6 gap-3">
          <MetricCard
            icon={MessagesSquare}
            label="Tracked"
            value={String(snapshot.sessions.totalSessions)}
            hint="sessions"
          />
          <MetricCard
            icon={Activity}
            label="Messages"
            value={String(snapshot.sessions.totalMessages)}
            hint="total"
          />
          <MetricCard
            icon={Layers}
            label="Tokens"
            value={formatTokens(totalTokens)}
            hint="in + out"
          />
          <MetricCard
            icon={DollarSign}
            label="Cost"
            value={formatCost(totalCost)}
            hint="estimated"
          />
          <MetricCard
            icon={Bot}
            label="Agents"
            value={String(snapshot.sessions.activeAgents)}
            hint="active"
          />
          <MetricCard
            icon={Clock3}
            label="Tool calls"
            value={String(snapshot.sessions.toolCallsToday)}
            hint="today"
          />
        </div>
      </Card>

      <Card padding="none">
        <div className="px-4 pt-4 pb-3 border-b border-border-subtle flex items-center justify-between">
          <div className="flex flex-col gap-0.5">
            <span className="eyebrow">Timeline</span>
            <h3 className="text-sm font-semibold text-text">Latest sessions</h3>
          </div>
          <span className="text-xs text-text-subtle">sorted by last activity</span>
        </div>

        <div className="divide-y divide-border-subtle">
          {sortedSessions.length > 0 ? (
            sortedSessions.map((session) => {
              const agent = agentSessionMap.get(session.sessionId);
              const hasTokens = agent && (agent.inputTokens + agent.outputTokens > 0);
              const totalSessionTokens = agent ? agent.inputTokens + agent.outputTokens : 0;
              const lastActive = session.lastActiveAt ?? session.startedAt ?? 0;
              return (
                <div key={session.sessionId} className="px-4 py-3 flex flex-col gap-2">
                  {/* Row 1: title + msgs badge */}
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0">
                      <p className="text-sm font-medium text-text truncate">{session.title}</p>
                      <p className="text-xs text-text-muted truncate">{session.source} · {session.model}</p>
                    </div>
                    <Badge variant="default" className="shrink-0">{session.messageCount} msgs</Badge>
                  </div>

                  {/* Row 1b: session id (copyable for recovery) */}
                  <div className="flex items-center gap-2 min-w-0">
                    <button
                      type="button"
                      onClick={() => navigator.clipboard?.writeText(session.sessionId)}
                      title="Copy session id"
                      className="inline-flex items-center gap-1 text-[10px] font-mono text-text-subtle hover:text-sky-300 transition-colors truncate"
                    >
                      <Copy size={10} />
                      <span className="truncate">{session.sessionId}</span>
                    </button>
                  </div>

                  {/* Row 2: compact conversation preview */}
                  <SessionChatPreview session={session} />

                  {/* Row 3: token usage — clean two-column layout */}
                  {hasTokens ? (
                    <div className="flex items-center justify-between gap-3 text-xs tabular-nums">
                      <div className="flex items-center gap-3 min-w-0">
                        <span className="text-sky-400" title={`Input: ${agent.inputTokens.toLocaleString()}`}>
                          ↓{formatTokens(agent.inputTokens)}
                        </span>
                        <span className="text-sky-400" title={`Output: ${agent.outputTokens.toLocaleString()}`}>
                          ↑{formatTokens(agent.outputTokens)}
                        </span>
                        <span className="font-medium text-text" title={`Total: ${totalSessionTokens.toLocaleString()}`}>
                          = {formatTokens(totalSessionTokens)}
                        </span>
                        {agent.cacheReadTokens > 0 ? (
                          <span className="text-violet-400 hidden sm:inline" title={`Cache: ${agent.cacheReadTokens.toLocaleString()}`}>
                            ⚡{formatTokens(agent.cacheReadTokens)}
                          </span>
                        ) : null}
                      </div>
                      {agent.estimatedCostUsd > 0 ? (
                        <span className="text-emerald-400 font-medium shrink-0">{formatCost(agent.estimatedCostUsd)}</span>
                      ) : null}
                    </div>
                  ) : null}

                  {/* Row 4: timestamps + trace CTA */}
                  <div className="flex items-center gap-2 text-xs text-text-subtle">
                    <span className="truncate">{formatRelativeTime(lastActive)}</span>
                    <span className="text-border-subtle">·</span>
                    <span className="truncate hidden sm:inline">Started {formatTimestamp(session.startedAt ?? 0)}</span>
                    <Link
                      to={`/sessions?chatSession=${encodeURIComponent(session.sessionId)}`}
                      className="ml-auto inline-flex items-center gap-1.5 rounded-md border border-violet-400/30 bg-violet-500/10 px-2.5 py-1.5 text-violet-200 hover:border-violet-300/50 hover:bg-violet-500/20 transition-colors shrink-0"
                      title="Resume this session in Hermes Chat"
                      aria-label={`Resume chat for ${session.title}`}
                    >
                      <MessageSquare size={13} />
                      <span>Resume chat</span>
                    </Link>
                    <Link
                      to={`/agents?session=${encodeURIComponent(session.sessionId)}`}
                      className="inline-flex items-center gap-1 text-sky-400 hover:text-sky-300 transition-colors shrink-0"
                      title="Open trace in Agents"
                    >
                      <Workflow size={12} />
                      <span>Trace</span>
                    </Link>
                  </div>
                </div>
              );
            })
          ) : (
            <div className="px-4 py-8 text-center text-sm text-text-muted italic">No sessions yet.</div>
          )}
        </div>

        {hasMore ? (
          <div className="p-4 border-t border-border-subtle">
            <button
              type="button"
              onClick={loadMore}
              disabled={loadingMore}
              className="w-full py-2 rounded-md text-sm font-medium text-sky-400 hover:text-sky-300 hover:bg-border-subtle/40 transition-colors disabled:opacity-50"
            >
              {loadingMore ? 'Loading…' : 'Load more'}
            </button>
          </div>
        ) : null}
      </Card>
    </div>
  );
}
