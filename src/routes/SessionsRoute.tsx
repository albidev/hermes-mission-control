import { useEffect, useMemo, useState } from 'react';
import { Activity, Bot, Clock3, DollarSign, Layers, MessagesSquare } from 'lucide-react';
import { Card } from '../components/ui/Card';
import { Badge } from '../components/ui/Badge';
import { formatRelativeTime, formatTimestamp } from '../lib/format';
import { useMissionControl } from '../lib/mission-control-store';
import {
  loadMissionControlAgentSessions,
  type MissionControlAgentSessionItem,
  type MissionControlAgentsSessionsSnapshot,
} from '../lib/hermes-api';

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

export function SessionsRoute() {
  const { snapshot, storedToken } = useMissionControl();
  const [agentSessions, setAgentSessions] = useState<MissionControlAgentsSessionsSnapshot | null>(null);

  useEffect(() => {
    let cancelled = false;
    loadMissionControlAgentSessions(storedToken ?? undefined).then((data) => {
      if (!cancelled) setAgentSessions(data);
    });
    return () => { cancelled = true; };
  }, [storedToken]);

  // Build a lookup map from agent sessions by session ID
  const agentSessionMap = useMemo(() => {
    const map = new Map<string, MissionControlAgentSessionItem>();
    if (agentSessions?.items) {
      for (const item of agentSessions.items) {
        map.set(item.sessionId, item);
      }
    }
    return map;
  }, [agentSessions]);

  const sortedSessions = useMemo(
    () => [...snapshot.sessions.items].sort((a, b) => b.lastActive - a.lastActive),
    [snapshot.sessions.items],
  );

  // Aggregate totals from agent sessions
  const totalTokens = agentSessions?.items.reduce((sum, s) => sum + s.inputTokens + s.outputTokens, 0) ?? 0;
  const totalCost = agentSessions?.items.reduce((sum, s) => sum + s.estimatedCostUsd, 0) ?? 0;

  return (
    <div className="flex flex-col gap-6">
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
            label="Tracked sessions"
            value={String(snapshot.sessions.totalSessions)}
            hint="total in storage"
          />
          <MetricCard
            icon={Activity}
            label="Messages"
            value={String(snapshot.sessions.totalMessages)}
            hint="conversation history"
          />
          <MetricCard
            icon={Bot}
            label="Active agents"
            value={String(snapshot.sessions.activeAgents)}
            hint="current snapshot"
          />
          <MetricCard
            icon={Clock3}
            label="Tool calls"
            value={String(snapshot.sessions.toolCallsToday)}
            hint="today"
          />
          <MetricCard
            icon={Layers}
            label="Total tokens"
            value={formatTokens(totalTokens)}
            hint="across all sessions"
          />
          <MetricCard
            icon={DollarSign}
            label="Estimated cost"
            value={formatCost(totalCost)}
            hint="total spend"
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
              const agent = agentSessionMap.get(session.id);
              const hasTokens = agent && (agent.inputTokens + agent.outputTokens > 0);
              return (
                <div key={session.id} className="px-4 py-3 flex flex-col gap-2">
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0">
                      <p className="text-sm font-medium text-text truncate">{session.title}</p>
                      <p className="text-xs text-text-muted truncate">{session.source} · {session.model}</p>
                    </div>
                    <div className="flex items-center gap-2 shrink-0">
                      {hasTokens ? (
                        <span className="text-[11px] tabular-nums text-text-subtle" title={`In: ${agent.inputTokens.toLocaleString()} · Out: ${agent.outputTokens.toLocaleString()}`}>
                          {formatTokens(agent.inputTokens + agent.outputTokens)} tok
                        </span>
                      ) : null}
                      {agent && agent.estimatedCostUsd > 0 ? (
                        <span className="text-[11px] tabular-nums text-emerald-400">{formatCost(agent.estimatedCostUsd)}</span>
                      ) : null}
                      <Badge variant="default">{session.messageCount} msgs</Badge>
                    </div>
                  </div>

                  <p className="text-xs text-text-muted line-clamp-2">
                    {session.preview || 'No preview available.'}
                  </p>

                  <div className="flex items-center justify-between text-xs text-text-subtle">
                    <span>Started {formatTimestamp(session.startedAt)}</span>
                    <span>Active {formatRelativeTime(session.lastActive)}</span>
                  </div>
                </div>
              );
            })
          ) : (
            <div className="px-4 py-8 text-center text-sm text-text-muted italic">No sessions yet.</div>
          )}
        </div>
      </Card>
    </div>
  );
}
