import { useEffect, useMemo, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { Activity, Bot, Clock3, Cpu, Gauge, GitBranch, Layers, ListTree, Workflow } from 'lucide-react';
import { Card } from '../components/ui/Card';
import { Badge } from '../components/ui/Badge';
import { Modal } from '../components/Modal';
import { formatRelativeTime, formatTimestamp } from '../lib/format';
import {
  loadMissionControlAgentTrace,
  type MissionControlAgentTraceEvent,
  type MissionControlAgentTraceSnapshot,
} from '../lib/hermes-api';
import { useMissionControl } from '../lib/mission-control-store';

type AgentAggregate = {
  id: string;
  source: string;
  model: string;
  totalSessions: number;
  liveSessions: number;
  totalMessages: number;
  lastActive: number;
};

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

function toneToVariant(tone?: string) {
  if (tone === 'bad') return 'negative' as const;
  if (tone === 'warn') return 'warning' as const;
  return 'positive' as const;
}

function summarizeRawPayload(value: string, limit = 1200): string {
  if (value.length <= limit) return value;
  return `${value.slice(0, limit)}\n\n… [preview clipped, open raw for full payload]`;
}

function summarizeEventPreview(value?: string, limit = 180): string {
  if (!value) return '—';
  const singleLine = value.replace(/\s+/g, ' ').trim();
  if (!singleLine) return '—';
  if (singleLine.length <= limit) return singleLine;
  return `${singleLine.slice(0, limit)}…`;
}

const LIVE_FRESHNESS_SECONDS = 5 * 60;
const LIVE_TRACE_LIMIT = 320;
const DAG_NODE_WIDTH = 260;
const DAG_NODE_HEIGHT = 76;
const DAG_COL_GAP = 340;
const DAG_ROW_GAP = 112;
const DAG_PADDING_X = 48;
const DAG_PADDING_Y = 40;

function getAgentKey(source?: string, model?: string): string {
  return `${source || 'unknown'}::${model || 'unknown'}`;
}

export function AgentsRoute() {
  const { agentId } = useParams<{ agentId?: string }>();
  const selectedAgentId = agentId ? decodeURIComponent(agentId) : '';
  const { snapshot, storedToken } = useMissionControl();
  const [view, setView] = useState<'timeline' | 'dag'>('timeline');
  const [liveMode, setLiveMode] = useState(true);
  const [selectedSessionId, setSelectedSessionId] = useState<string>('');
  const [trace, setTrace] = useState<MissionControlAgentTraceSnapshot | null>(null);
  const [traceLoading, setTraceLoading] = useState(false);
  const [sseFallbackToPolling, setSseFallbackToPolling] = useState(false);
  const [selectedEvent, setSelectedEvent] = useState<MissionControlAgentTraceEvent | null>(null);
  const [rawPayloadViewer, setRawPayloadViewer] = useState<{ title: string; content: string } | null>(null);

  const orderedSessions = useMemo(
    () => [...snapshot.sessions.items].sort((a, b) => b.lastActive - a.lastActive),
    [snapshot.sessions.items],
  );

  const trulyLiveSessions = useMemo(() => {
    const cutoff = Date.now() / 1000 - LIVE_FRESHNESS_SECONDS;
    return orderedSessions.filter((session) => session.endedAt === null && session.lastActive >= cutoff);
  }, [orderedSessions]);

  const baseSessions = liveMode ? trulyLiveSessions : orderedSessions;
  const selectableSessions = useMemo(() => {
    if (!selectedAgentId) return baseSessions;
    return baseSessions.filter((session) => getAgentKey(session.source, session.model) === selectedAgentId);
  }, [baseSessions, selectedAgentId]);

  useEffect(() => {
    if (selectedSessionId) return;

    const liveRichSession = selectableSessions.find((session) => session.messageCount >= 4);
    const richSession = selectableSessions.find((session) => session.messageCount >= 4);
    const preferred = liveRichSession ?? richSession ?? selectableSessions[0];

    if (preferred) {
      setSelectedSessionId(preferred.id);
    }
  }, [selectableSessions, selectedSessionId]);

  const eventById = useMemo(() => {
    const map = new Map<string, MissionControlAgentTraceEvent>();
    for (const event of trace?.events ?? []) {
      map.set(event.id, event);
    }
    return map;
  }, [trace?.events]);

  const callDetailsByCallId = useMemo(() => {
    const map = new Map<string, { request?: string; response?: string }>();
    for (const event of trace?.events ?? []) {
      if (!event.callId) continue;
      const current = map.get(event.callId) ?? {};
      map.set(event.callId, {
        request: current.request ?? event.request,
        response: current.response ?? event.response,
      });
    }
    return map;
  }, [trace?.events]);

  const dagLayout = useMemo(() => {
    if (!trace || trace.nodes.length === 0) {
      return {
        width: 1200,
        height: 420,
        turns: [] as number[],
        nodes: [] as Array<{ node: MissionControlAgentTraceSnapshot['nodes'][number]; x: number; y: number }>,
        edges: [] as Array<{ key: string; d: string; kind: string }>,
      };
    }

    const sortedNodes = [...trace.nodes].sort((a, b) => {
      if (a.turnId !== b.turnId) return a.turnId - b.turnId;
      if (a.timestamp !== b.timestamp) return a.timestamp - b.timestamp;
      return a.id.localeCompare(b.id);
    });

    const turns = [...new Set(sortedNodes.map((node) => node.turnId))].sort((a, b) => a - b);
    const turnToColumn = new Map<number, number>(turns.map((turn, index) => [turn, index]));
    const turnRowCounters = new Map<number, number>();

    const nodes = sortedNodes.map((node) => {
      const col = turnToColumn.get(node.turnId) ?? 0;
      const row = turnRowCounters.get(node.turnId) ?? 0;
      turnRowCounters.set(node.turnId, row + 1);

      return {
        node,
        x: DAG_PADDING_X + col * DAG_COL_GAP,
        y: DAG_PADDING_Y + row * DAG_ROW_GAP,
      };
    });

    const byId = new Map(nodes.map((item) => [item.node.id, item]));

    const edges = trace.edges
      .map((edge, index) => {
        const from = byId.get(edge.from);
        const to = byId.get(edge.to);
        if (!from || !to) return null;

        const sx = from.x + DAG_NODE_WIDTH;
        const sy = from.y + DAG_NODE_HEIGHT / 2;
        const tx = to.x;
        const ty = to.y + DAG_NODE_HEIGHT / 2;
        const bend = Math.max(48, Math.abs(tx - sx) * 0.5);
        const c1x = sx + bend;
        const c2x = tx - bend;
        const d = `M ${sx} ${sy} C ${c1x} ${sy}, ${c2x} ${ty}, ${tx} ${ty}`;

        return {
          key: `${edge.from}-${edge.to}-${index}`,
          d,
          kind: edge.kind,
        };
      })
      .filter((item): item is { key: string; d: string; kind: string } => Boolean(item));

    const maxX = nodes.reduce((acc, item) => Math.max(acc, item.x), DAG_PADDING_X);
    const maxY = nodes.reduce((acc, item) => Math.max(acc, item.y), DAG_PADDING_Y);

    return {
      width: Math.max(1200, maxX + DAG_NODE_WIDTH + DAG_PADDING_X),
      height: Math.max(420, maxY + DAG_NODE_HEIGHT + DAG_PADDING_Y),
      turns,
      nodes,
      edges,
    };
  }, [trace]);

  useEffect(() => {
    setSseFallbackToPolling(false);
    setSelectedEvent(null);
  }, [selectedSessionId, liveMode]);

  useEffect(() => {
    if (!selectedSessionId) return;
    if (selectableSessions.some((session) => session.id === selectedSessionId)) return;
    setSelectedSessionId(selectableSessions[0]?.id ?? '');
  }, [selectedSessionId, selectableSessions]);

  useEffect(() => {
    if (!liveMode) return;
    if (selectableSessions.length === 0) return;

    const freshest = selectableSessions[0];
    if (!selectedSessionId) {
      setSelectedSessionId(freshest.id);
      return;
    }

    const current = selectableSessions.find((session) => session.id === selectedSessionId);
    if (!current) {
      setSelectedSessionId(freshest.id);
      return;
    }

    if (freshest.id !== current.id && freshest.lastActive > current.lastActive + 5) {
      setSelectedSessionId(freshest.id);
    }
  }, [liveMode, selectableSessions, selectedSessionId]);

  const registry = useMemo<AgentAggregate[]>(() => {
    const map = new Map<string, AgentAggregate>();

    for (const session of orderedSessions) {
      const source = session.source || 'unknown';
      const model = session.model || 'unknown';
      const key = getAgentKey(source, model);
      const current = map.get(key);

      if (!current) {
        map.set(key, {
          id: key,
          source,
          model,
          totalSessions: 1,
          liveSessions: session.endedAt === null ? 1 : 0,
          totalMessages: session.messageCount,
          lastActive: session.lastActive,
        });
        continue;
      }

      current.totalSessions += 1;
      if (session.endedAt === null) current.liveSessions += 1;
      current.totalMessages += session.messageCount;
      current.lastActive = Math.max(current.lastActive, session.lastActive);
    }

    return [...map.values()].sort((a, b) => {
      if (b.liveSessions !== a.liveSessions) return b.liveSessions - a.liveSessions;
      return b.lastActive - a.lastActive;
    });
  }, [orderedSessions]);

  const selectedAgent = useMemo(() => {
    if (!selectedAgentId) return null;
    return registry.find((agent) => agent.id === selectedAgentId) ?? null;
  }, [registry, selectedAgentId]);

  const selectedAgentAvgMessages = useMemo(() => {
    if (!selectedAgent || selectedAgent.totalSessions === 0) return 0;
    return selectedAgent.totalMessages / selectedAgent.totalSessions;
  }, [selectedAgent]);

  useEffect(() => {
    if (!liveMode || sseFallbackToPolling) {
      return;
    }

    if (selectableSessions.length === 0) {
      setTrace(null);
      setTraceLoading(false);
      return;
    }

    if (typeof window === 'undefined' || typeof EventSource === 'undefined') {
      setSseFallbackToPolling(true);
      return;
    }

    const base = (import.meta.env.VITE_HERMES_API_BASE_URL || '/api').replace(/\/$/, '');
    const params = new URLSearchParams();
    if (selectedSessionId) params.set('session_id', selectedSessionId);
    params.set('limit', String(LIVE_TRACE_LIMIT));
    params.set('interval', '1.5');
    if (storedToken) params.set('access_token', storedToken);
    params.set('compact', '1');

    setTraceLoading(true);
    const source = new EventSource(`${base}/mission-control/agents/trace/stream?${params.toString()}`, { withCredentials: true });

    const handleTraceFrame = (rawData: string) => {
      try {
        const payload = JSON.parse(rawData) as MissionControlAgentTraceSnapshot;
        setTrace(payload);
        setTraceLoading(false);
      } catch {
        // ignore malformed frame
      }
    };

    source.onmessage = (event) => {
      handleTraceFrame(event.data);
    };

    source.addEventListener('trace', (event) => {
      const messageEvent = event as MessageEvent<string>;
      handleTraceFrame(messageEvent.data);
    });

    source.addEventListener('error', () => {
      source.close();
      setSseFallbackToPolling(true);
    });

    source.onerror = () => {
      source.close();
      setSseFallbackToPolling(true);
    };

    return () => {
      source.close();
    };
  }, [selectedSessionId, liveMode, storedToken, sseFallbackToPolling, selectableSessions.length]);

  useEffect(() => {
    if (liveMode && !sseFallbackToPolling) {
      return;
    }

    let cancelled = false;

    const fetchTrace = async () => {
      if (!selectedSessionId && selectableSessions.length === 0) {
        setTrace(null);
        setTraceLoading(false);
        return;
      }

      setTraceLoading(true);
      const payload = await loadMissionControlAgentTrace(
        selectedSessionId || undefined,
        storedToken || undefined,
        liveMode ? LIVE_TRACE_LIMIT : 0,
        liveMode,
      );
      if (!cancelled) {
        setTrace(payload);
        setTraceLoading(false);
      }
    };

    void fetchTrace();

    if (liveMode) {
      const timer = window.setInterval(() => {
        void fetchTrace();
      }, 2500);
      return () => {
        cancelled = true;
        window.clearInterval(timer);
      };
    }

    return () => {
      cancelled = true;
    };
  }, [selectedSessionId, liveMode, storedToken, selectableSessions.length, sseFallbackToPolling]);

  return (
    <div className="flex flex-col gap-6">
      <Card padding="none">
        <div className="px-4 pt-4 pb-3 border-b border-border-subtle flex items-center justify-between">
          <div className="flex flex-col gap-0.5">
            <span className="eyebrow">Agents</span>
            <h2 className="text-sm font-semibold text-text">
              {selectedAgent ? `Agent cockpit · ${selectedAgent.source}` : 'Runtime + trace chain (Timeline and DAG)'}
            </h2>
          </div>
          <Badge variant={selectedAgent ? 'warning' : snapshot.activeAgents > 0 ? 'positive' : 'default'} dot>
            {selectedAgent ? 'single-agent view' : `${snapshot.activeAgents} active`}
          </Badge>
        </div>

        <div className="p-4 grid grid-cols-1 md:grid-cols-2 xl:grid-cols-4 gap-3">
          <MetricCard
            icon={Bot}
            label={selectedAgent ? 'Agent sessions' : 'Active agents'}
            value={selectedAgent ? String(selectedAgent.totalSessions) : String(snapshot.activeAgents)}
            hint={selectedAgent ? `${selectedAgent.model}` : 'current runtime'}
          />
          <MetricCard
            icon={Activity}
            label={selectedAgent ? 'Agent live sessions' : 'Live sessions'}
            value={selectedAgent ? String(selectedAgent.liveSessions) : String(trulyLiveSessions.length)}
            hint="active in last 5 min"
          />
          <MetricCard
            icon={Clock3}
            label={selectedAgent ? 'Avg messages/session' : 'In-flight queue'}
            value={selectedAgent ? selectedAgentAvgMessages.toFixed(1) : String(snapshot.queuedJobs)}
            hint={selectedAgent ? 'session depth' : 'scheduled/pending jobs'}
          />
          <MetricCard
            icon={Gauge}
            label={selectedAgent ? 'Last active' : 'Tool calls'}
            value={selectedAgent ? formatRelativeTime(selectedAgent.lastActive) : String(snapshot.sessions.toolCallsToday)}
            hint={selectedAgent ? formatTimestamp(selectedAgent.lastActive) : 'today'}
          />
        </div>
      </Card>

      {selectedAgent ? (
        <Card className="p-4 sticky top-0 z-10 border-border-subtle">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="min-w-0">
              <p className="eyebrow">Single agent</p>
              <p className="text-sm font-semibold text-text break-all">{selectedAgent.source}</p>
              <p className="text-xs text-text-muted break-all">{selectedAgent.model}</p>
            </div>
            <Link to="/agents" className="pill pill-subtle pill-button text-xs">
              Show all agents
            </Link>
          </div>
        </Card>
      ) : selectedAgentId ? (
        <Card className="p-4 border-border-subtle">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <p className="text-sm text-warning">Agent not found in current registry snapshot.</p>
            <Link to="/agents" className="pill pill-subtle pill-button text-xs">
              Back to registry
            </Link>
          </div>
        </Card>
      ) : null}

      <Card padding="none">
        <div className="px-4 pt-4 pb-3 border-b border-border-subtle flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
          <div className="flex flex-col gap-0.5">
            <span className="eyebrow">Execution trace</span>
            <h3 className="text-sm font-semibold text-text">
              {selectedAgent ? 'Single-agent flow: thoughts, tools, skills, responses' : 'Full chain: thoughts, tools, skills, responses'}
            </h3>
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <button className={`pill ${view === 'timeline' ? 'nav-link-active' : 'pill-subtle'} pill-button`} onClick={() => setView('timeline')}>
              <ListTree className="h-3.5 w-3.5" /> Timeline
            </button>
            <button className={`pill ${view === 'dag' ? 'nav-link-active' : 'pill-subtle'} pill-button`} onClick={() => setView('dag')}>
              <GitBranch className="h-3.5 w-3.5" /> DAG
            </button>
            <button className={`pill ${liveMode ? 'status-online' : 'pill-subtle'} pill-button`} onClick={() => setLiveMode((v) => !v)}>
              <Workflow className="h-3.5 w-3.5" /> {liveMode ? 'Live' : 'Post'}
            </button>
          </div>
        </div>

        <div className="p-4 flex flex-col gap-4">
          <div className="flex flex-col sm:flex-row sm:items-center gap-2">
            <label className="text-xs text-text-muted shrink-0">Session</label>
            <select
              className="auth-input py-2 text-sm"
              value={selectedSessionId}
              onChange={(event) => setSelectedSessionId(event.target.value)}
              disabled={selectableSessions.length === 0}
            >
              {selectableSessions.length === 0 ? (
                <option value="">
                  {selectedAgent
                    ? liveMode
                      ? 'No truly live sessions for this agent right now'
                      : 'No sessions for this agent'
                    : 'No truly live sessions right now'}
                </option>
              ) : null}
              {selectableSessions.map((session) => (
                <option key={session.id} value={session.id}>
                  {session.title} · {session.source} · {formatRelativeTime(session.lastActive)}
                </option>
              ))}
            </select>
          </div>

          {trace?.session ? (
            <div className="flex flex-wrap items-center gap-2 text-xs text-text-subtle">
              <Badge variant={trace.mode === 'live' ? 'positive' : 'default'}>{trace.mode}</Badge>
              <span className="break-all">{trace.session.title} · {trace.session.model}</span>
              <span>turns {trace.stats.turns}</span>
              <span>tools {trace.stats.toolCalls}</span>
              <span>skills {trace.stats.skills}</span>
              <span>thoughts {trace.stats.thoughts}</span>
              <span>errors {trace.stats.errors}</span>
              <Badge variant={liveMode && !sseFallbackToPolling ? 'positive' : 'default'}>
                {liveMode && !sseFallbackToPolling ? 'transport: sse' : 'transport: polling'}
              </Badge>
            </div>
          ) : null}

          {traceLoading ? <p className="text-sm text-text-muted">Loading trace…</p> : null}

          {!traceLoading && trace && trace.stats.toolCalls === 0 && trace.stats.skills === 0 ? (
            <div className="card p-3 text-xs text-text-muted">
              Questa sessione ha solo user/assistant. Per vedere tool calls e skills, cambia sessione con una run più lunga.
            </div>
          ) : null}

          {!traceLoading && trace && view === 'timeline' ? (
            <div className="flex flex-col gap-2">
              {trace.events.length > 0 ? (
                trace.events.map((event) => (
                  <button
                    key={event.id}
                    type="button"
                    onClick={() => setSelectedEvent(event)}
                    className="card p-3 flex flex-col gap-1.5 min-w-0 text-left hover:border-border focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand/50"
                  >
                    <div className="flex flex-wrap items-center gap-2">
                      <Badge variant={toneToVariant(event.tone)}>{event.type.replaceAll('_', ' ')}</Badge>
                      <span className="text-sm font-medium text-text">{event.label}</span>
                      <span className="text-xs text-text-subtle ml-auto">{formatRelativeTime(event.timestamp)}</span>
                    </div>
                    <p className="text-xs text-text-muted break-words">{summarizeEventPreview(event.detail)}</p>
                    <div className="flex flex-wrap items-center gap-2 text-[11px] text-text-subtle">
                      <span>turn {event.turnId}</span>
                      {event.toolName ? <span>tool {event.toolName}</span> : null}
                      {event.skillName ? <span>skill {event.skillName}</span> : null}
                      {event.callId ? <span>call {event.callId}</span> : null}
                      <span>{formatTimestamp(event.timestamp)}</span>
                      <span className="text-text-muted">tap for details</span>
                    </div>
                  </button>
                ))
              ) : (
                <div className="text-sm text-text-muted italic">No trace events yet for this session.</div>
              )}
            </div>
          ) : null}

          {!traceLoading && trace && view === 'dag' ? (
            <div className="card p-2.5 sm:p-3 flex flex-col gap-2">
              <div className="flex flex-wrap items-center justify-between gap-2 px-1">
                <p className="text-xs text-text-muted">Execution graph canvas</p>
                <p className="text-[11px] text-text-subtle">
                  {dagLayout.nodes.length} nodes · {dagLayout.edges.length} edges
                </p>
              </div>

              <div className="rounded-lg border border-border-subtle bg-surface-sunken overflow-auto max-h-[640px]">
                <div
                  className="relative"
                  style={{
                    width: `${dagLayout.width}px`,
                    height: `${dagLayout.height}px`,
                    minWidth: '100%',
                    minHeight: '420px',
                  }}
                >
                  <svg className="absolute inset-0 h-full w-full" viewBox={`0 0 ${dagLayout.width} ${dagLayout.height}`} preserveAspectRatio="xMinYMin meet">
                    <defs>
                      <marker id="dag-arrow" markerWidth="10" markerHeight="10" refX="8" refY="3" orient="auto" markerUnits="strokeWidth">
                        <path d="M0,0 L0,6 L9,3 z" fill="var(--color-text-subtle)" />
                      </marker>
                    </defs>
                    {dagLayout.turns.map((turn, index) => {
                      const x = DAG_PADDING_X + index * DAG_COL_GAP - 16;
                      return (
                        <g key={`lane-${turn}`}>
                          <rect
                            x={x}
                            y={12}
                            width={DAG_COL_GAP - 16}
                            height={Math.max(0, dagLayout.height - 24)}
                            fill={index % 2 === 0 ? 'var(--color-surface)' : 'var(--color-surface-raised)'}
                            fillOpacity={0.3}
                            rx={10}
                          />
                          <text x={x + 10} y={30} fill="var(--color-text-subtle)" fontSize="11">{`Turn ${turn}`}</text>
                        </g>
                      );
                    })}
                    {dagLayout.edges.map((edge) => (
                      <path
                        key={edge.key}
                        d={edge.d}
                        fill="none"
                        stroke={edge.kind === 'parent' ? 'var(--color-accent)' : 'var(--color-text-subtle)'}
                        strokeOpacity={edge.kind === 'parent' ? 0.75 : 0.45}
                        strokeWidth={edge.kind === 'parent' ? 1.8 : 1.4}
                        markerEnd="url(#dag-arrow)"
                      />
                    ))}
                  </svg>

                  {dagLayout.nodes.map((item) => {
                    const linkedEvent = eventById.get(item.node.id);
                    const statusAccent =
                      item.node.status === 'failed'
                        ? 'border-l-negative'
                        : item.node.status === 'running'
                          ? 'border-l-warning'
                          : 'border-l-positive';

                    return (
                      <button
                        key={item.node.id}
                        type="button"
                        onClick={() => linkedEvent && setSelectedEvent(linkedEvent)}
                        className={`absolute w-[260px] h-[76px] rounded-lg border border-border bg-surface-raised shadow-sm border-l-2 ${statusAccent} p-2 text-left flex flex-col gap-1 hover:border-border-subtle`}
                        style={{ left: `${item.x}px`, top: `${item.y}px` }}
                      >
                        <div className="flex items-start justify-between gap-2 min-w-0">
                          <p className="text-xs font-medium text-text break-words line-clamp-2">{item.node.label}</p>
                          <span className="text-[10px] text-text-subtle shrink-0">T{item.node.turnId}</span>
                        </div>
                        <p className="text-[11px] text-text-subtle break-words">{item.node.kind.replaceAll('_', ' ')}</p>
                      </button>
                    );
                  })}
                </div>
              </div>
            </div>
          ) : null}
        </div>
      </Card>

      <Card padding="none">
        <div className="px-4 pt-4 pb-3 border-b border-border-subtle flex items-center justify-between">
          <div className="flex flex-col gap-0.5">
            <span className="eyebrow">Agent registry</span>
            <h3 className="text-sm font-semibold text-text">Per-agent stats by source + model</h3>
          </div>
          <Badge variant="default">{registry.length} agents</Badge>
        </div>

        <div className="divide-y divide-border-subtle">
          {registry.length > 0 ? (
            registry.map((agent) => {
              const isSelected = selectedAgentId === agent.id;
              return (
                <div key={agent.id} className={`px-4 py-3 grid grid-cols-1 lg:grid-cols-12 gap-2 items-center ${isSelected ? 'bg-surface-sunken/70' : ''}`}>
                  <div className="lg:col-span-5 min-w-0">
                    <p className="text-sm font-medium text-text break-all">{agent.source}</p>
                    <p className="text-xs text-text-muted break-all">{agent.model}</p>
                  </div>

                  <div className="lg:col-span-2 flex items-center gap-2">
                    <Badge variant={agent.liveSessions > 0 ? 'positive' : 'default'}>{agent.liveSessions > 0 ? 'running' : 'idle'}</Badge>
                  </div>

                  <div className="lg:col-span-5 flex flex-wrap items-center gap-2 text-xs text-text-subtle">
                    <span className="inline-flex items-center gap-1"><Cpu className="h-3.5 w-3.5" /> sessions {agent.totalSessions}</span>
                    <span className="inline-flex items-center gap-1"><Layers className="h-3.5 w-3.5" /> msgs {agent.totalMessages}</span>
                    <span>last active {formatRelativeTime(agent.lastActive)}</span>
                    <span className="text-text-muted">({formatTimestamp(agent.lastActive)})</span>
                    <Link
                      to={`/agents/${encodeURIComponent(agent.id)}`}
                      className={`pill pill-button text-[11px] ${isSelected ? 'nav-link-active' : 'pill-subtle'}`}
                    >
                      Open flow
                    </Link>
                  </div>
                </div>
              );
            })
          ) : (
            <div className="px-4 py-8 text-center text-sm text-text-muted italic">No agent registry data yet.</div>
          )}
        </div>
      </Card>

      <Modal
        open={Boolean(selectedEvent)}
        title={selectedEvent?.label ?? 'Trace event'}
        subtitle={selectedEvent ? `${selectedEvent.type} · turn ${selectedEvent.turnId}` : undefined}
        onClose={() => {
          setSelectedEvent(null);
          setRawPayloadViewer(null);
        }}
      >
        {selectedEvent ? (
          <div className="flex flex-col gap-3 text-sm">
            {(() => {
              const linked = selectedEvent.callId ? callDetailsByCallId.get(selectedEvent.callId) : undefined;
              const requestDetail =
                selectedEvent.request ??
                linked?.request ??
                (selectedEvent.type === 'turn_started' ? selectedEvent.detail : undefined);
              const responseDetail =
                selectedEvent.response ??
                linked?.response ??
                (selectedEvent.type === 'assistant_response' ? selectedEvent.detail : undefined);
              const requestLabel = selectedEvent.type.startsWith('tool_call') ? 'Tool request' : 'Request';
              const responseLabel = selectedEvent.type.startsWith('tool_call') ? 'Tool response' : 'Response';

              return (
                <>
                  {requestDetail ? (
                    <div className="card p-3">
                      <div className="flex items-center justify-between gap-2 mb-1">
                        <p className="text-[11px] uppercase tracking-wide text-text-subtle">{requestLabel}</p>
                      </div>
                      <pre className="text-xs text-text break-words whitespace-pre-wrap font-mono">{summarizeRawPayload(requestDetail)}</pre>
                      <button
                        type="button"
                        className="mt-2 w-full text-xs px-3 py-2 rounded border border-border-subtle text-text hover:bg-surface-raised"
                        onClick={() => setRawPayloadViewer({ title: `${requestLabel} (raw)`, content: requestDetail })}
                      >
                        Open raw request
                      </button>
                    </div>
                  ) : null}

                  {responseDetail ? (
                    <div className="card p-3">
                      <div className="flex items-center justify-between gap-2 mb-1">
                        <p className="text-[11px] uppercase tracking-wide text-text-subtle">{responseLabel}</p>
                      </div>
                      <pre className="text-xs text-text break-words whitespace-pre-wrap font-mono">{summarizeRawPayload(responseDetail)}</pre>
                      <button
                        type="button"
                        className="mt-2 w-full text-xs px-3 py-2 rounded border border-border-subtle text-text hover:bg-surface-raised"
                        onClick={() => setRawPayloadViewer({ title: `${responseLabel} (raw)`, content: responseDetail })}
                      >
                        Open raw response
                      </button>
                    </div>
                  ) : null}

                  {!requestDetail && !responseDetail ? (
                    <div className="card p-3">
                      <p className="text-sm text-text-muted">No request/response payload available for this event.</p>
                    </div>
                  ) : null}
                </>
              );
            })()}
          </div>
        ) : null}
      </Modal>

      <Modal
        open={Boolean(rawPayloadViewer)}
        title={rawPayloadViewer?.title ?? 'Raw payload'}
        subtitle={rawPayloadViewer ? `${rawPayloadViewer.content.length.toLocaleString()} chars` : undefined}
        onClose={() => setRawPayloadViewer(null)}
      >
        {rawPayloadViewer ? (
          <div className="card p-3">
            <pre className="text-xs text-text break-words whitespace-pre-wrap font-mono">{rawPayloadViewer.content}</pre>
          </div>
        ) : null}
      </Modal>
    </div>
  );
}
