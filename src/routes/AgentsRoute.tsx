import { useI18n } from '../lib/i18n';
import { memo, useEffect, useMemo, useRef, useState } from 'react';
import { Link, useParams, useSearchParams } from 'react-router-dom';
import { Activity, Bot, Clock3, Cpu, Gauge, GitBranch, Layers, ListTree, Workflow } from 'lucide-react';
import { Card } from '../components/ui/Card';
import { Badge } from '../components/ui/Badge';
import { Modal } from '../components/Modal';
import { formatRelativeTime, formatTimestamp } from '../lib/format';
import {
  getFallbackCapabilities,
  loadMissionControlAgentSessions,
  loadMissionControlAgentTrace,
  loadMissionControlCapabilities,
  type MissionControlAgentSessionItem,
  type MissionControlAgentTraceEvent,
  type MissionControlAgentTraceSnapshot,
  type MissionControlAgentRegistryItem,
  type MissionControlCapabilities,
} from '../lib/hermes-api';
import { useMissionControl } from '../lib/mission-control-store';
import { usePullToReload } from '../hooks/usePullToReload';
import { PullToReloadIndicator } from '../components/PullToReloadIndicator';

function formatTokenCount(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(n);
}

type AgentAggregate = MissionControlAgentRegistryItem & {
  id: string;
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

type BadgeVisual = {
  variant: 'default' | 'positive' | 'warning' | 'negative' | 'accent';
  className: string;
};

type TraceActionFilter = 'user_message' | 'thought' | 'tool_call' | 'skill_used' | 'assistant_response' | 'turn' | 'other';

const TRACE_ACTION_FILTERS: Array<{ id: TraceActionFilter; label: string }> = [
  { id: 'user_message', label: 'User' },
  { id: 'thought', label: 'Thought' },
  { id: 'tool_call', label: 'Tool call' },
  { id: 'skill_used', label: 'Skill' },
  { id: 'assistant_response', label: 'Assistant' },
  { id: 'turn', label: 'Turn' },
  { id: 'other', label: 'Other' },
];

function getTraceActionFilter(event: MissionControlAgentTraceEvent): TraceActionFilter {
  if (event.type === 'user_message') return 'user_message';
  if (event.type === 'thought') return 'thought';
  if (event.type.startsWith('tool_call')) return 'tool_call';
  if (event.type === 'skill_used') return 'skill_used';
  if (event.type === 'assistant_response') return 'assistant_response';
  if (event.type.startsWith('turn_')) return 'turn';
  return 'other';
}

function getTraceActionLabel(filter: TraceActionFilter): string {
  return TRACE_ACTION_FILTERS.find((item) => item.id === filter)?.label ?? 'Other';
}

function getEventTypeBadge(event: MissionControlAgentTraceEvent): BadgeVisual {
  if (event.type.startsWith('tool_call')) {
    return {
      variant: 'default',
      className: '!bg-sky-500/15 !text-sky-600 dark:!text-sky-300 !border !border-sky-400/35',
    };
  }

  if (event.type === 'skill_used') {
    return {
      variant: 'default',
      className: '!bg-emerald-500/15 !text-emerald-700 dark:!text-emerald-300 !border !border-emerald-400/35',
    };
  }

  if (event.type === 'thought') {
    return {
      variant: 'default',
      className: '!bg-violet-500/15 !text-violet-700 dark:!text-violet-300 !border !border-violet-400/35',
    };
  }

  if (event.type === 'user_message') {
    return {
      variant: 'default',
      className: '!bg-cyan-500/15 !text-cyan-700 dark:!text-cyan-300 !border !border-cyan-400/35',
    };
  }

  if (event.type === 'assistant_response') {
    return {
      variant: 'default',
      className: '!bg-fuchsia-500/15 !text-fuchsia-700 dark:!text-fuchsia-300 !border !border-fuchsia-400/35',
    };
  }

  if (event.type.startsWith('turn_')) {
    return {
      variant: 'default',
      className: '!bg-amber-500/15 !text-amber-700 dark:!text-amber-300 !border !border-amber-400/35',
    };
  }

  if (event.tone === 'bad') {
    return { variant: 'negative', className: '' };
  }

  if (event.tone === 'warn') {
    return { variant: 'warning', className: '' };
  }

  return { variant: 'default', className: '' };
}

function getEffectiveEventStatus(event: MissionControlAgentTraceEvent, completedToolCallIds: Set<string>): string {
  if (event.type === 'tool_call_started' && event.callId && completedToolCallIds.has(event.callId)) {
    return 'completed';
  }

  if (event.status === 'failed' || event.tone === 'bad') return 'failed';
  if (event.status === 'running') return 'running';
  if (event.tone === 'warn') return 'warning';
  return event.status || 'completed';
}

function getEventStatusBadge(event: MissionControlAgentTraceEvent, completedToolCallIds: Set<string>): BadgeVisual | null {
  const status = getEffectiveEventStatus(event, completedToolCallIds);

  if (status === 'failed') {
    return { variant: 'negative', className: '' };
  }

  if (status === 'running' || status === 'warning') {
    return { variant: 'warning', className: '' };
  }

  return null;
}

function getEventStatusLabel(event: MissionControlAgentTraceEvent, completedToolCallIds: Set<string>): string {
  return getEffectiveEventStatus(event, completedToolCallIds);
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
type LiveTraceScope = 'current' | 'last3' | 'full';
const DAG_NODE_WIDTH = 260;
const DAG_NODE_HEIGHT = 76;
const DAG_COL_GAP = 340;
const DAG_ROW_GAP = 112;
const DAG_PADDING_X = 48;
const DAG_PADDING_Y = 40;

function getAgentKey(source?: string, model?: string): string {
  return `${source || 'unknown'}::${model || 'unknown'}`;
}

const AgentRegistryCard = memo(function AgentRegistryCard({
  registry,
  selectedAgentId,
}: {
  registry: AgentAggregate[];
  selectedAgentId: string;
}) {
  const { t } = useI18n();
  return (
    <Card padding="none">
      <div className="px-4 pt-4 pb-3 border-b border-border-subtle flex items-center justify-between">
        <div className="flex flex-col gap-0.5">
          <span className="eyebrow">{t('agents.registry')}</span>
          <h3 className="text-sm font-semibold text-text">{t('agents.perAgentStats')}</h3>
        </div>
        <Badge variant="default">{registry.length} agents</Badge>
      </div>

      <div className="divide-y divide-border-subtle">
        {registry.length > 0 ? (
          registry.map((agent) => {
            const isSelected = selectedAgentId === agent.id;
            return <AgentRegistryRow key={agent.id} agent={agent} isSelected={isSelected} />;
          })
        ) : (
          <div className="px-4 py-8 text-center text-sm text-text-muted italic">{t('agents.noRegistry')}</div>
        )}
      </div>
    </Card>
  );
});

const AgentRegistryRow = memo(function AgentRegistryRow({
  agent,
  isSelected,
}: {
  agent: AgentAggregate;
  isSelected: boolean;
}) {
  return (
    <div className={`px-4 py-3 grid grid-cols-1 lg:grid-cols-12 gap-2 items-center ${isSelected ? 'bg-surface-sunken/70' : ''}`}>
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
          to={`/agents/${encodeURIComponent(agent.id)}?mode=${agent.liveSessions > 0 ? 'live' : 'post'}`}
          className={`pill pill-button text-[11px] ${isSelected ? 'nav-link-active' : 'pill-subtle'}`}
        >
          Open workflow
        </Link>
      </div>
    </div>
  );
});

export function AgentsRoute() {
  const { t } = useI18n();
  const { agentId } = useParams<{ agentId?: string }>();
  const [searchParams] = useSearchParams();
  const selectedAgentId = agentId ? decodeURIComponent(agentId) : '';
  const requestedMode = searchParams.get('mode');
  const requestedSession = searchParams.get('session');
  const { snapshot, storedToken } = useMissionControl();
  const [view, setView] = useState<'timeline' | 'dag'>('timeline');
  const [liveMode, setLiveMode] = useState(() => requestedMode !== 'post');
  const [liveTraceScope, setLiveTraceScope] = useState<LiveTraceScope>('current');
  const [selectedActionFilters, setSelectedActionFilters] = useState<TraceActionFilter[]>([]);
  const [selectedSessionId, setSelectedSessionId] = useState<string>(requestedSession ?? '');
  const [agentSessions, setAgentSessions] = useState<MissionControlAgentSessionItem[]>([]);
  const [trace, setTrace] = useState<MissionControlAgentTraceSnapshot | null>(null);
  const [traceLoading, setTraceLoading] = useState(false);
  const [capabilities, setCapabilities] = useState<MissionControlCapabilities>(getFallbackCapabilities());
  const [sseFallbackToPolling, setSseFallbackToPolling] = useState(false);
  const [selectedEvent, setSelectedEvent] = useState<MissionControlAgentTraceEvent | null>(null);
  const [rawPayloadViewer, setRawPayloadViewer] = useState<{ title: string; content: string } | null>(null);
  const hasTraceRef = useRef(false);
  const manualSessionSelectionRef = useRef(false);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const traceStreamAvailable = capabilities.trace.stream;
  const traceCompactAvailable = capabilities.trace.compact;
  const traceNamedSseEventAvailable = capabilities.trace.namedSseTraceEvent;

  const { state: pullState } = usePullToReload({
    containerRef,
    onReload: async () => {
      try {
        const resolved = await loadMissionControlCapabilities(storedToken);
        setCapabilities(resolved);
      } catch {
        setCapabilities(getFallbackCapabilities());
      }
      try {
        const resolved = await loadMissionControlAgentSessions(storedToken, 200);
        setAgentSessions(resolved.items);
      } catch {
        setAgentSessions([]);
      }
      if (selectedSessionId) {
        try {
          const payload = await loadMissionControlAgentTrace(
            selectedSessionId,
            storedToken || undefined,
            liveMode ? LIVE_TRACE_LIMIT : 0,
            liveMode && traceCompactAvailable,
          );
          setTrace(payload);
        } catch {
          setTrace(null);
        }
      }
    },
  });

  useEffect(() => {
    hasTraceRef.current = Boolean(trace);
  }, [trace]);

  useEffect(() => {
    if (requestedMode === 'live') setLiveMode(true);
    if (requestedMode === 'post') setLiveMode(false);
  }, [requestedMode]);

  const selectSession = (sessionId: string, manual = false) => {
    manualSessionSelectionRef.current = manual;
    hasTraceRef.current = false;
    setTrace(null);
    setTraceLoading(Boolean(sessionId));
    setSelectedSessionId(sessionId);
  };

  const orderedSessions = useMemo<MissionControlAgentSessionItem[]>(
    () => [...agentSessions].sort((a, b) => (b.lastActiveAt ?? 0) - (a.lastActiveAt ?? 0)),
    [agentSessions],
  );

  const trulyLiveSessions = useMemo(() => {
    const cutoff = Date.now() / 1000 - LIVE_FRESHNESS_SECONDS;
    return orderedSessions.filter((session) => session.status === 'live' && (session.lastActiveAt ?? 0) >= cutoff);
  }, [orderedSessions]);

  const allSelectedAgentSessions = useMemo(() => {
    if (!selectedAgentId) return orderedSessions;
    return orderedSessions.filter((session) => getAgentKey(session.source, session.model) === selectedAgentId);
  }, [orderedSessions, selectedAgentId]);

  const baseSessions = liveMode ? trulyLiveSessions : orderedSessions;
  const selectableSessions = useMemo(() => {
    if (!selectedAgentId) return baseSessions;
    return baseSessions.filter((session) => getAgentKey(session.source, session.model) === selectedAgentId);
  }, [baseSessions, selectedAgentId]);

  useEffect(() => {
    if (!selectedAgentId || !liveMode) return;
    if (selectableSessions.length > 0 || allSelectedAgentSessions.length === 0) return;

    manualSessionSelectionRef.current = false;
    setSelectedSessionId('');
    setLiveMode(false);
  }, [allSelectedAgentSessions.length, liveMode, selectableSessions.length, selectedAgentId]);

  useEffect(() => {
    if (selectedSessionId) return;

    const liveRichSession = selectableSessions.find((session) => session.messageCount >= 4);
    const richSession = selectableSessions.find((session) => session.messageCount >= 4);
    const preferred = liveRichSession ?? richSession ?? selectableSessions[0];

    if (preferred) {
      selectSession(preferred.sessionId);
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

  const visibleTrace = useMemo(() => {
    if (!trace) return null;
    if (!liveMode || liveTraceScope === 'full') return trace;

    const turnIds = new Set<number>();
    for (const event of trace.events) turnIds.add(event.turnId);
    for (const node of trace.nodes) turnIds.add(node.turnId);

    const sortedTurns = [...turnIds].sort((a, b) => a - b);
    if (sortedTurns.length === 0) return trace;

    const currentTurn = sortedTurns[sortedTurns.length - 1];
    const visibleTurns =
      liveTraceScope === 'current'
        ? new Set([currentTurn])
        : new Set(sortedTurns.filter((turn) => turn >= currentTurn - 2));

    const events = trace.events.filter((event) => visibleTurns.has(event.turnId));
    const nodes = trace.nodes.filter((node) => visibleTurns.has(node.turnId));
    const nodeIds = new Set(nodes.map((node) => node.id));
    const edges = trace.edges.filter((edge) => nodeIds.has(edge.from) && nodeIds.has(edge.to));

    const turns = new Set<number>();
    for (const event of events) turns.add(event.turnId);
    for (const node of nodes) turns.add(node.turnId);

    const toolCalls = events.filter((event) => event.type.startsWith('tool_call_started')).length;
    const skills = events.filter((event) => event.type === 'skill_used').length;
    const thoughts = events.filter((event) => event.type === 'thought').length;
    const errors =
      events.filter((event) => event.tone === 'bad').length + nodes.filter((node) => node.status === 'failed').length;

    const timestamps = [...events.map((event) => event.timestamp), ...nodes.map((node) => node.timestamp)].filter((value) => Number.isFinite(value));
    const durationSeconds =
      timestamps.length > 1 ? Math.max(0, Math.floor(Math.max(...timestamps) - Math.min(...timestamps))) : 0;

    return {
      ...trace,
      events,
      nodes,
      edges,
      stats: {
        turns: turns.size,
        toolCalls,
        skills,
        thoughts,
        errors,
        durationSeconds,
      },
    };
  }, [trace, liveMode, liveTraceScope]);

  const completedToolCallIds = useMemo(() => {
    const ids = new Set<string>();
    for (const event of visibleTrace?.events ?? []) {
      if (event.type === 'tool_call_completed' && event.callId) {
        ids.add(event.callId);
      }
    }
    return ids;
  }, [visibleTrace?.events]);

  const actionFilterSet = useMemo(() => new Set(selectedActionFilters), [selectedActionFilters]);
  const actionFilterActive = selectedActionFilters.length > 0;

  const toggleActionFilter = (filter: TraceActionFilter) => {
    setSelectedActionFilters((current) =>
      current.includes(filter) ? current.filter((item) => item !== filter) : [...current, filter],
    );
  };

  const actionFilterCounts = useMemo(() => {
    const counts = new Map<TraceActionFilter, number>();
    for (const event of visibleTrace?.events ?? []) {
      const filter = getTraceActionFilter(event);
      counts.set(filter, (counts.get(filter) ?? 0) + 1);
    }
    return counts;
  }, [visibleTrace?.events]);

  const filteredTrace = useMemo(() => {
    if (!visibleTrace || !actionFilterActive) return visibleTrace;

    const events = visibleTrace.events.filter((event) => actionFilterSet.has(getTraceActionFilter(event)));
    const eventIds = new Set(events.map((event) => event.id));
    const nodes = visibleTrace.nodes.filter((node) => eventIds.has(node.id));
    const nodeIds = new Set(nodes.map((node) => node.id));
    const edges = visibleTrace.edges.filter((edge) => nodeIds.has(edge.from) && nodeIds.has(edge.to));

    return {
      ...visibleTrace,
      events,
      nodes,
      edges,
    };
  }, [visibleTrace, actionFilterActive, actionFilterSet]);

  const timelineEvents = useMemo(() => {
    return [...(filteredTrace?.events ?? [])].sort((a, b) => {
      if (b.timestamp !== a.timestamp) return b.timestamp - a.timestamp;
      if (b.turnId !== a.turnId) return b.turnId - a.turnId;
      return b.id.localeCompare(a.id);
    });
  }, [filteredTrace?.events]);

  const dagLayout = useMemo(() => {
    if (!filteredTrace || filteredTrace.nodes.length === 0) {
      return {
        width: 1200,
        height: 420,
        turns: [] as number[],
        nodes: [] as Array<{ node: MissionControlAgentTraceSnapshot['nodes'][number]; x: number; y: number }>,
        edges: [] as Array<{ key: string; d: string; kind: string }>,
      };
    }

    const sortedNodes = [...filteredTrace.nodes].sort((a, b) => {
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

    const edges = filteredTrace.edges
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
  }, [filteredTrace]);
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const resolved = await loadMissionControlCapabilities(storedToken);
        if (!cancelled) {
          setCapabilities(resolved);
        }
      } catch {
        if (!cancelled) {
          setCapabilities(getFallbackCapabilities());
        }
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [storedToken]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const resolved = await loadMissionControlAgentSessions(storedToken, 200);
        if (!cancelled) {
          setAgentSessions(resolved.items);
        }
      } catch {
        if (!cancelled) {
          setAgentSessions([]);
        }
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [storedToken]);

  useEffect(() => {
    setSseFallbackToPolling(false);
    setSelectedEvent(null);
  }, [selectedSessionId, liveMode]);

  useEffect(() => {
    manualSessionSelectionRef.current = false;
  }, [liveMode, selectedAgentId]);

  useEffect(() => {
    if (!selectedEvent || !filteredTrace) return;
    if (filteredTrace.events.some((event) => event.id === selectedEvent.id)) return;
    setSelectedEvent(null);
  }, [selectedEvent, filteredTrace]);

  useEffect(() => {
    if (!selectedSessionId) return;
    if (selectableSessions.some((session) => session.sessionId === selectedSessionId)) return;
    manualSessionSelectionRef.current = false;
    selectSession(selectableSessions[0]?.sessionId ?? '');
  }, [selectedSessionId, selectableSessions]);

  useEffect(() => {
    if (!liveMode) return;
    if (selectableSessions.length === 0) return;

    const freshest = selectableSessions[0];
    if (!selectedSessionId) {
      setSelectedSessionId(freshest.sessionId);
      return;
    }

    const current = selectableSessions.find((session) => session.sessionId === selectedSessionId);
    if (!current) {
      manualSessionSelectionRef.current = false;
      selectSession(freshest.sessionId);
      return;
    }

    if (manualSessionSelectionRef.current) {
      return;
    }

    if (freshest.sessionId !== current.sessionId && (freshest.lastActiveAt ?? 0) > (current.lastActiveAt ?? 0) + 5) {
      selectSession(freshest.sessionId);
    }
  }, [liveMode, selectableSessions, selectedSessionId]);

  const registry = useMemo<AgentAggregate[]>(() => {
    const map = new Map<string, AgentAggregate>();

    for (const session of orderedSessions) {
      const key = session.agentId;
      const current = map.get(key);

      if (!current) {
        map.set(key, {
          id: key,
          agentId: key,
          source: session.source,
          model: session.model,
          label: `${session.source} / ${session.model}`,
          totalSessions: 1,
          liveSessions: session.status === 'live' ? 1 : 0,
          lastActiveAt: session.lastActiveAt,
          traceMode: session.traceMode,
          totalMessages: session.messageCount,
          lastActive: session.lastActiveAt ?? 0,
        });
        continue;
      }

      current.totalSessions += 1;
      if (session.status === 'live') current.liveSessions += 1;
      current.totalMessages += session.messageCount;
      current.lastActiveAt = Math.max(current.lastActiveAt ?? 0, session.lastActiveAt ?? 0) || null;
      current.lastActive = Math.max(current.lastActive, session.lastActiveAt ?? 0);
      if (current.traceMode !== 'native' && session.traceMode === 'native') current.traceMode = 'native';
      else if (current.traceMode === 'unavailable' && session.traceMode === 'transcript') current.traceMode = 'transcript';
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
    if (!liveMode || sseFallbackToPolling || !traceStreamAvailable) {
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

    const localBase = (import.meta.env.VITE_MISSION_CONTROL_LOCAL_API_BASE_URL || '/api/local').replace(/\/$/, '');
    const officialBase = (import.meta.env.VITE_HERMES_API_BASE_URL || '/api').replace(/\/$/, '');
    const params = new URLSearchParams();
    if (selectedSessionId) params.set('session_id', selectedSessionId);
    params.set('limit', String(LIVE_TRACE_LIMIT));
    params.set('interval', '1.5');
    if (storedToken) params.set('access_token', storedToken);
    if (traceCompactAvailable) {
      params.set('compact', '1');
    }

    if (!hasTraceRef.current) {
      setTraceLoading(true);
    }
    const streamCandidates = [
      {
        url: `${localBase}/mission-control/agents/trace/stream?${params.toString()}`,
      },
      {
        url: `${officialBase}/mission-control/agents/trace/stream?${new URLSearchParams(
          Array.from(params.entries()).filter(([key]) => key !== 'access_token'),
        ).toString()}`,
      },
    ].filter((candidate, index, array) => array.findIndex((item) => item.url === candidate.url) === index);

    let candidateIndex = 0;
    let source: EventSource | null = null;

    const handleTraceFrame = (rawData: string) => {
      try {
        const payload = JSON.parse(rawData) as MissionControlAgentTraceSnapshot;
        setTrace(payload);
        setTraceLoading(false);
      } catch {
        // ignore malformed frame
      }
    };

    const connect = (index: number) => {
      source?.close();
      source = new EventSource(streamCandidates[index].url, { withCredentials: true });
      source.onmessage = (event) => {
        handleTraceFrame(event.data);
      };

      if (traceNamedSseEventAvailable) {
        source.addEventListener('trace', (event) => {
          const messageEvent = event as MessageEvent<string>;
          handleTraceFrame(messageEvent.data);
        });
      }

      source.onerror = () => {
        source?.close();
        if (candidateIndex + 1 < streamCandidates.length) {
          candidateIndex += 1;
          connect(candidateIndex);
          return;
        }
        setSseFallbackToPolling(true);
      };
    };

    connect(candidateIndex);

    return () => {
      source?.close();
    };
  }, [selectedSessionId, liveMode, storedToken, sseFallbackToPolling, selectableSessions.length, traceCompactAvailable, traceNamedSseEventAvailable, traceStreamAvailable]);

  useEffect(() => {
    if (liveMode && !sseFallbackToPolling && traceStreamAvailable) {
      return;
    }

    let cancelled = false;

    const fetchTrace = async () => {
      if (!selectedSessionId && selectableSessions.length === 0) {
        setTrace(null);
        setTraceLoading(false);
        return;
      }

      if (!hasTraceRef.current) {
        setTraceLoading(true);
      }
      const payload = await loadMissionControlAgentTrace(
        selectedSessionId || undefined,
        storedToken || undefined,
        liveMode ? LIVE_TRACE_LIMIT : 0,
        liveMode && traceCompactAvailable,
      );
      if (!cancelled) {
        setTrace(payload);
        setTraceLoading(false);
      }
    };

    void fetchTrace();

    if (liveMode) {
      const timer = window.setInterval(() => {
        if (document.visibilityState === 'visible') void fetchTrace();
      }, 2500);
      return () => {
        cancelled = true;
        window.clearInterval(timer);
      };
    }

    return () => {
      cancelled = true;
    };
  }, [selectedSessionId, liveMode, storedToken, selectableSessions.length, sseFallbackToPolling, traceCompactAvailable, traceStreamAvailable]);

  return (
    <div ref={containerRef} className="flex flex-col gap-6 h-full overflow-y-auto">
      <PullToReloadIndicator state={pullState} />
      <Card padding="none">
        <div className="px-4 pt-4 pb-3 border-b border-border-subtle flex items-center justify-between">
          <div className="flex flex-col gap-0.5">
            <span className="eyebrow">{t('nav.agents')}</span>
            <h2 className="text-sm font-semibold text-text">
              {selectedAgent ? `Agent cockpit · ${selectedAgent.source}` : 'Runtime + trace chain (Timeline and DAG)'}
            </h2>
          </div>
          <Badge variant={selectedAgent ? 'warning' : registry.some((agent) => agent.liveSessions > 0) ? 'positive' : 'default'} dot>
            {selectedAgent ? 'single-agent view' : `${registry.filter((agent) => agent.liveSessions > 0).length} active`}
          </Badge>
        </div>

        <div className="p-4 grid grid-cols-1 md:grid-cols-2 xl:grid-cols-4 gap-3">
          <MetricCard
            icon={Bot}
            label={selectedAgent ? 'Agent sessions' : 'Active agents'}
            value={selectedAgent ? String(selectedAgent.totalSessions) : String(registry.filter((agent) => agent.liveSessions > 0).length)}
            hint={selectedAgent ? `${selectedAgent.model}` : 'agents with live sessions'}
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
            label={selectedAgent ? 'Last active' : 'Tracked sessions'}
            value={selectedAgent ? formatRelativeTime(selectedAgent.lastActive) : String(orderedSessions.length)}
            hint={selectedAgent ? formatTimestamp(selectedAgent.lastActive) : 'filesystem + session adapter'}
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

          {liveMode ? (
            <div className="flex flex-wrap items-center gap-2 text-xs">
              <span className="text-text-subtle">Scope</span>
              <button
                className={`pill pill-button ${liveTraceScope === 'current' ? 'nav-link-active' : 'pill-subtle'}`}
                onClick={() => setLiveTraceScope('current')}
              >
                Current turn
              </button>
              <button
                className={`pill pill-button ${liveTraceScope === 'last3' ? 'nav-link-active' : 'pill-subtle'}`}
                onClick={() => setLiveTraceScope('last3')}
              >
                Last 3 turns
              </button>
              <button
                className={`pill pill-button ${liveTraceScope === 'full' ? 'nav-link-active' : 'pill-subtle'}`}
                onClick={() => setLiveTraceScope('full')}
              >
                Full session
              </button>
            </div>
          ) : null}

          {!capabilities.trace.stream ? (
            <p className="text-xs text-warning">Compatibility mode: live SSE stream unavailable, using polling fallback.</p>
          ) : null}
        </div>

        <div className="p-4 flex flex-col gap-4">
          <div className="flex flex-col sm:flex-row sm:items-center gap-2">
            <label className="text-xs text-text-muted shrink-0">{t('provider.session')}</label>
            <select
              className="auth-input py-2 text-sm"
              value={selectedSessionId}
              onChange={(event) => selectSession(event.target.value, true)}
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
                <option key={session.sessionId} value={session.sessionId}>
                  {session.title} · {session.source} · {formatRelativeTime(session.lastActiveAt ?? session.startedAt ?? 0)}
                </option>
              ))}
            </select>
          </div>

          {visibleTrace ? (
            <div className="flex flex-col gap-2 rounded-lg border border-border-subtle bg-surface/50 p-3">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="flex flex-col gap-0.5">
                  <span className="text-xs font-medium text-text">Action taxonomy</span>
                  <span className="text-[11px] text-text-subtle">Select one or more event classes to filter Timeline and DAG.</span>
                </div>
                {actionFilterActive ? (
                  <button type="button" className="pill pill-subtle pill-button text-[11px]" onClick={() => setSelectedActionFilters([])}>
                    Clear filters
                  </button>
                ) : (
                  <Badge variant="default">All actions</Badge>
                )}
              </div>
              <div className="flex flex-wrap items-center gap-2">
                {TRACE_ACTION_FILTERS.map((filter) => {
                  const count = actionFilterCounts.get(filter.id) ?? 0;
                  const active = actionFilterSet.has(filter.id);
                  return (
                    <button
                      key={filter.id}
                      type="button"
                      className={`pill pill-button text-[11px] ${active ? 'nav-link-active' : 'pill-subtle'}`}
                      onClick={() => toggleActionFilter(filter.id)}
                      disabled={count === 0 && !active}
                      title={`${getTraceActionLabel(filter.id)} events`}
                    >
                      {filter.label}
                      <span className="text-text-subtle">{count}</span>
                    </button>
                  );
                })}
              </div>
            </div>
          ) : null}

          {visibleTrace?.session ? (
            <div className="flex flex-wrap items-center gap-2 text-xs text-text-subtle">
              <Badge variant={visibleTrace.mode === 'live' ? 'positive' : 'default'}>{visibleTrace.mode}</Badge>
              <span className="break-all">{visibleTrace.session.title} · {visibleTrace.session.model}</span>
              <span>turns {visibleTrace.stats.turns}</span>
              <span>tools {visibleTrace.stats.toolCalls}</span>
              <span>skills {visibleTrace.stats.skills}</span>
              <span>thoughts {visibleTrace.stats.thoughts}</span>
              <span>errors {visibleTrace.stats.errors}</span>
              {(() => {
                const agentSession = orderedSessions.find((s) => s.sessionId === selectedSessionId);
                if (!agentSession || agentSession.inputTokens + agentSession.outputTokens === 0) return null;
                return (
                  <>
                    <span className="tabular-nums" title={`In: ${agentSession.inputTokens.toLocaleString()} · Out: ${agentSession.outputTokens.toLocaleString()} · Cache: ${agentSession.cacheReadTokens.toLocaleString()} · Reasoning: ${agentSession.reasoningTokens.toLocaleString()}`}>
                      tokens {formatTokenCount(agentSession.inputTokens + agentSession.outputTokens)}
                    </span>
                    {agentSession.estimatedCostUsd > 0 ? (
                      <span className="tabular-nums">${agentSession.estimatedCostUsd.toFixed(3)}</span>
                    ) : null}
                  </>
                );
              })()}
              <Badge variant={liveMode && !sseFallbackToPolling ? 'positive' : 'default'}>
                {liveMode && !sseFallbackToPolling ? 'transport: sse' : 'transport: polling'}
              </Badge>
            </div>
          ) : null}

          {liveMode && trace && visibleTrace && trace.events.length > visibleTrace.events.length ? (
            <p className="text-xs text-text-subtle">
              Showing {visibleTrace.events.length} of {trace.events.length} events in live scope.
            </p>
          ) : null}

          {actionFilterActive && visibleTrace && filteredTrace ? (
            <p className="text-xs text-text-subtle">
              Filtered to {filteredTrace.events.length} of {visibleTrace.events.length} scoped events: {selectedActionFilters.map(getTraceActionLabel).join(', ')}.
            </p>
          ) : null}

          {traceLoading ? <p className="text-sm text-text-muted">{t('agents.loadingTrace')}</p> : null}

          {!traceLoading && visibleTrace && visibleTrace.stats.toolCalls === 0 && visibleTrace.stats.skills === 0 ? (
            <div className="card p-3 text-xs text-text-muted">
              Questa sessione ha solo user/assistant. Per vedere tool calls e skills, cambia sessione con una run più lunga.
            </div>
          ) : null}

          {!traceLoading && visibleTrace && view === 'timeline' ? (
            <div className="flex flex-col gap-2">
              {timelineEvents.length > 0 ? (
                timelineEvents.map((event) => {
                  const badge = getEventTypeBadge(event);
                  const statusBadge = getEventStatusBadge(event, completedToolCallIds);
                  return (
                    <button
                      key={event.id}
                      type="button"
                      onClick={() => setSelectedEvent(event)}
                      className="card p-3 flex flex-col gap-1.5 min-w-0 text-left hover:border-border focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand/50"
                    >
                      <div className="flex flex-wrap items-center gap-2">
                        <Badge variant={badge.variant} className={badge.className}>{event.type.replaceAll('_', ' ')}</Badge>
                        {statusBadge ? <Badge variant={statusBadge.variant} className={statusBadge.className}>{getEventStatusLabel(event, completedToolCallIds)}</Badge> : null}
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
                  );
                })
              ) : (
                <div className="text-sm text-text-muted italic">
                  {actionFilterActive ? 'No trace events match the selected action filters.' : 'No trace events yet for this session.'}
                </div>
              )}
            </div>
          ) : null}

          {!traceLoading && visibleTrace && view === 'dag' ? (
            <div className="card p-2.5 sm:p-3 flex flex-col gap-2">
              <div className="flex flex-wrap items-center justify-between gap-2 px-1">
                <p className="text-xs text-text-muted">Execution graph canvas</p>
                <p className="text-[11px] text-text-subtle">
                  {dagLayout.nodes.length} nodes · {dagLayout.edges.length} edges
                </p>
              </div>

              <div className="mobile-agent-dag rounded-lg border border-border-subtle bg-surface-sunken overflow-auto max-h-[640px]">
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
                    const effectiveStatus = linkedEvent ? getEffectiveEventStatus(linkedEvent, completedToolCallIds) : item.node.status;
                    const statusAccent =
                      effectiveStatus === 'failed'
                        ? 'border-l-negative'
                        : effectiveStatus === 'running'
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

      <AgentRegistryCard registry={registry} selectedAgentId={selectedAgentId} />

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
              const isToolEvent = selectedEvent.type.startsWith('tool_call');
              const requestDetail = isToolEvent
                ? selectedEvent.request ?? linked?.request
                : selectedEvent.type === 'turn_started'
                  ? selectedEvent.detail
                  : undefined;
              const responseDetail = isToolEvent
                ? selectedEvent.response ?? linked?.response
                : selectedEvent.type === 'assistant_response'
                  ? selectedEvent.detail
                  : undefined;
              const eventDetail = !requestDetail && !responseDetail ? selectedEvent.detail : undefined;
              const requestLabel = selectedEvent.type.startsWith('tool_call') ? 'Tool request' : 'Request';
              const responseLabel = selectedEvent.type.startsWith('tool_call') ? 'Tool response' : 'Response';
              const eventLabel = selectedEvent.type === 'user_message'
                ? 'User message'
                : selectedEvent.type === 'thought'
                  ? 'Thought'
                  : 'Event detail';

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

                  {eventDetail ? (
                    <div className="card p-3">
                      <div className="flex items-center justify-between gap-2 mb-1">
                        <p className="text-[11px] uppercase tracking-wide text-text-subtle">{eventLabel}</p>
                      </div>
                      <pre className="text-xs text-text break-words whitespace-pre-wrap font-mono">{summarizeRawPayload(eventDetail)}</pre>
                      <button
                        type="button"
                        className="mt-2 w-full text-xs px-3 py-2 rounded border border-border-subtle text-text hover:bg-surface-raised"
                        onClick={() => setRawPayloadViewer({ title: `${eventLabel} (raw)`, content: eventDetail })}
                      >
                        Open raw detail
                      </button>
                    </div>
                  ) : null}

                  {!requestDetail && !responseDetail && !eventDetail ? (
                    <div className="card p-3">
                      <p className="text-sm text-text-muted">No detail payload available for this event.</p>
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
