import { useI18n } from '../lib/i18n';
import { useEffect, useMemo, useRef, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { Activity, Bot, Clock3, Gauge, GitBranch, ListTree, RefreshCw } from 'lucide-react';
import { Card } from '../components/ui/Card';
import { Badge } from '../components/ui/Badge';
import { Modal } from '../components/Modal';
import { PageHeader } from '../components/PageHeader';
import { formatRelativeTime, formatTimestamp } from '../lib/format';
import {
  getFallbackCapabilities,
  loadMissionControlAgentSessions,
  loadMissionControlAgentTrace,
  loadMissionControlCapabilities,
  type MissionControlAgentSessionItem,
  type MissionControlAgentTraceEvent,
  type MissionControlAgentTraceSnapshot,
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

function MetricCard({
  icon: Icon,
  label,
  value,
  hint,
  compact = false,
}: {
  icon: React.ElementType;
  label: string;
  value: string;
  hint: string;
  compact?: boolean;
}) {
  return (
    <Card className={`p-3 sm:p-4 ${compact ? 'px-2 py-2 sm:p-4' : ''}`}>
      <div className="flex items-center justify-between gap-2">
        <span className={`min-w-0 text-xs text-text-muted ${compact ? 'truncate text-[10px] sm:text-xs' : ''}`}>{label}</span>
        <Icon className={`h-4 w-4 shrink-0 text-text-subtle ${compact ? 'h-3.5 w-3.5 sm:h-4 sm:w-4' : ''}`} />
      </div>
      <p className={`mt-2 text-lg font-semibold text-text tabular-nums ${compact ? 'mt-1 text-sm sm:mt-2 sm:text-lg' : ''}`}>{value}</p>
      <p className={`mt-1 text-xs text-text-subtle ${compact ? 'hidden sm:block' : ''}`}>{hint}</p>
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

const TRACE_ACTION_LABEL_KEYS: Record<TraceActionFilter, string> = {
  user_message: 'agents.user',
  thought: 'agents.thought',
  tool_call: 'agents.toolCall',
  skill_used: 'agents.skill',
  assistant_response: 'agents.assistant',
  turn: 'agents.turn',
  other: 'agents.other',
};

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

export function AgentsRoute() {
  const { t } = useI18n();
  const [searchParams] = useSearchParams();
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
  const [refreshing, setRefreshing] = useState(false);
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

  const refreshPage = async () => {
    setRefreshing(true);
    try {
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
    } finally {
      setRefreshing(false);
    }
  };

  const { state: pullState } = usePullToReload({
    containerRef,
    onReload: refreshPage,
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

  const baseSessions = liveMode ? trulyLiveSessions : orderedSessions;
  const selectableSessions = baseSessions;

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
  }, [liveMode]);

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
    <div ref={containerRef} className="route-page-scroll flex min-w-0 flex-col gap-6 h-full overflow-x-hidden overflow-y-auto">
      <PullToReloadIndicator state={pullState} />
      <Card padding="none">
        <PageHeader
          eyebrow={t('nav.agents')}
          title={t('agents.title')}
          description={t('agents.description')}
          meta={selectedSessionId ? t('agents.selectedSessionMeta') : t('agents.noSessionMeta')}
          actions={<button type="button" onClick={() => void refreshPage()} className="inline-flex items-center justify-center gap-1.5 rounded-md bg-surface px-2.5 py-1.5 text-text-muted hover:bg-surface-sunken hover:text-text !px-0 sm:!px-2.5" aria-label={t('common.refresh')} title={t('common.refresh')} disabled={refreshing}>
            <RefreshCw size={13} className={refreshing ? 'animate-spin' : ''} /><span className="hidden sm:inline">{t('common.refresh')}</span>
          </button>}
        />
      </Card>

      <Card padding="none">
        <div className="px-4 pt-1 sm:pt-0">
          <span className="eyebrow">{t('agents.statsTitle')}</span>
        </div>
        <div className="grid grid-cols-2 gap-2 p-4 pt-3 sm:grid-cols-4 sm:gap-3 sm:pt-4">
          <MetricCard
            icon={Bot}
            compact
            label={t('agents.activeAgents')}
            value={String(snapshot.activeAgents)}
            hint={t('agents.activeAgentsHint')}
          />
          <MetricCard
            icon={Activity}
            compact
            label={t('agents.liveSessions')}
            value={String(trulyLiveSessions.length)}
            hint={t('agents.liveSessionsHint')}
          />
          <MetricCard
            icon={Clock3}
            compact
            label={t('agents.inFlightQueue')}
            value={String(snapshot.queuedJobs)}
            hint={t('agents.inFlightQueueHint')}
          />
          <MetricCard
            icon={Gauge}
            compact
            label={t('agents.trackedSessions')}
            value={String(snapshot.sessions.totalSessions)}
            hint={t('agents.trackedSessionsHint')}
          />
        </div>
      </Card>

      <Card padding="none">
        <div className="border-b border-border-subtle">
          <div className="flex min-w-0 flex-col gap-4 px-4 py-4 sm:flex-row sm:items-end sm:justify-between sm:gap-6">
            <div className="min-w-0 flex-1">
              <span className="eyebrow">{t('agents.executionTrace')}</span>
              <h3 className="mt-1 text-base font-semibold leading-6 text-text">
                {t('agents.fullChain')}
              </h3>
            </div>

            <div className="flex min-w-0 max-w-full flex-col gap-3 sm:flex-[0_1_auto] sm:flex-row sm:items-end sm:gap-3">
              <div className="flex min-w-0 max-w-full flex-1 flex-row items-end gap-2">
                <div className="flex min-w-0 flex-1 flex-col gap-1.5" role="group" aria-label={t('agents.view')}>
                  <span className="eyebrow">{t('agents.view')}</span>
                  <div className="flex min-w-0 max-w-full flex-nowrap items-center gap-1.5 overflow-x-auto pb-1 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden sm:overflow-visible sm:pb-0">
                    <button type="button" aria-pressed={view === 'timeline'} className={`pill pill-button shrink-0 whitespace-nowrap ${view === 'timeline' ? 'nav-link-active' : 'pill-subtle'}`} onClick={() => setView('timeline')}>
                      <ListTree className="h-3.5 w-3.5" /> {t('agents.timeline')}
                    </button>
                    <button type="button" aria-pressed={view === 'dag'} className={`pill pill-button shrink-0 whitespace-nowrap ${view === 'dag' ? 'nav-link-active' : 'pill-subtle'}`} onClick={() => setView('dag')}>
                      <GitBranch className="h-3.5 w-3.5" /> {t('agents.dag')}
                    </button>
                  </div>
                </div>

                <div className="flex shrink-0 flex-col gap-1.5" role="group" aria-label={t('agents.stream')}>
                  <span className="eyebrow">{t('agents.stream')}</span>
                  <button type="button" aria-pressed={liveMode} className={`pill pill-button shrink-0 whitespace-nowrap ${liveMode ? 'status-online' : 'pill-subtle'}`} onClick={() => setLiveMode((v) => !v)}>
                    <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${liveMode ? 'bg-positive animate-pulse' : 'bg-text-subtle'}`} />
                    {liveMode ? t('agents.live') : t('agents.post')}
                  </button>
                  {!capabilities.trace.stream ? (
                    <p className="max-w-[14rem] text-[10px] leading-4 text-warning">Compatibility mode: live SSE stream unavailable, using polling fallback.</p>
                  ) : null}
                </div>
              </div>

              {liveMode ? (
                <div className="flex min-w-0 flex-col gap-1.5 sm:flex-1" role="group" aria-label={t('agents.scope')}>
                  <span className="eyebrow">{t('agents.scope')}</span>
                  <div className="flex min-w-0 max-w-full flex-nowrap items-center gap-1 overflow-x-auto pb-1 pr-3 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden sm:overflow-x-auto sm:pb-0 sm:pr-4">
                    <button type="button" aria-pressed={liveTraceScope === 'current'} className={`pill pill-button shrink-0 whitespace-nowrap sm:px-2 sm:text-xs ${liveTraceScope === 'current' ? 'nav-link-active' : 'pill-subtle'}`} onClick={() => setLiveTraceScope('current')}>
                      {t('agents.currentTurn')}
                    </button>
                    <button type="button" aria-pressed={liveTraceScope === 'last3'} className={`pill pill-button shrink-0 whitespace-nowrap sm:px-2 sm:text-xs ${liveTraceScope === 'last3' ? 'nav-link-active' : 'pill-subtle'}`} onClick={() => setLiveTraceScope('last3')}>
                      {t('agents.last3Turns')}
                    </button>
                    <button type="button" aria-pressed={liveTraceScope === 'full'} className={`pill pill-button mr-4 shrink-0 whitespace-nowrap sm:px-2 sm:text-xs ${liveTraceScope === 'full' ? 'nav-link-active' : 'pill-subtle'}`} onClick={() => setLiveTraceScope('full')}>
                      {t('agents.fullSession')}
                    </button>
                  </div>
                </div>
              ) : null}
            </div>
          </div>
        </div>

        <div className="flex flex-col gap-4 p-4">
          <div className="flex flex-col sm:flex-row sm:items-center gap-2">
            <label className="text-xs text-text-muted shrink-0">{t('agents.selectedSession')}</label>
            <select
              className="h-11 w-full min-w-0 rounded-md bg-surface px-3 py-0 text-xs text-text outline-none focus:ring-1 focus:ring-accent/40 sm:h-9"
              value={selectedSessionId}
              onChange={(event) => selectSession(event.target.value, true)}
              disabled={selectableSessions.length === 0}
            >
              {selectableSessions.length === 0 ? (
                <option value="">
                  {liveMode ? t('agents.noLiveSessions') : t('agents.noSessions')}
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
                  <span className="text-sm font-semibold text-text">{t('agents.actionTaxonomy')}</span>
                  <span className="text-[11px] text-text-subtle">{t('agents.actionTaxonomyHint')}</span>
                </div>
                {actionFilterActive ? (
                  <button type="button" className="pill pill-subtle pill-button text-[11px]" onClick={() => setSelectedActionFilters([])}>
                    {t('agents.clearFilters')}
                  </button>
                ) : (
                  <Badge variant="default">{t('ui.allActions')}</Badge>
                )}
              </div>
              <div className="-mx-3 flex max-w-[calc(100%+1.5rem)] flex-nowrap items-center gap-1.5 overflow-x-auto px-3 pb-1 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden sm:mx-0 sm:max-w-none sm:flex-wrap sm:overflow-visible sm:px-0 sm:pb-0">
                {TRACE_ACTION_FILTERS.filter((filter) => (actionFilterCounts.get(filter.id) ?? 0) > 0 || actionFilterSet.has(filter.id)).map((filter) => {
                  const count = actionFilterCounts.get(filter.id) ?? 0;
                  const active = actionFilterSet.has(filter.id);
                  return (
                    <button
                      key={filter.id}
                      type="button"
                      aria-pressed={active}
                      className={`pill pill-button shrink-0 justify-center whitespace-nowrap text-[11px] ${active ? 'nav-link-active' : 'pill-subtle'}`}
                      onClick={() => toggleActionFilter(filter.id)}
                      disabled={count === 0 && !active}
                      title={`${getTraceActionLabel(filter.id)} events`}
                    >
                      {t(TRACE_ACTION_LABEL_KEYS[filter.id])}
                      <span className="text-text-subtle">{count}</span>
                    </button>
                  );
                })}
              </div>
            </div>
          ) : null}

          {visibleTrace?.session ? (
            <div className="flex flex-col gap-2 rounded-lg border border-border-subtle bg-surface/50 p-3">
              <div className="flex min-w-0 flex-wrap items-center gap-2 text-xs text-text-subtle">
                <Badge variant={visibleTrace.mode === 'live' ? 'positive' : 'default'}>{visibleTrace.mode}</Badge>
                <span className="min-w-0 flex-1 truncate">{visibleTrace.session.title} · {visibleTrace.session.model}</span>
              </div>
              <div className="-mx-1 flex max-w-[calc(100% + 0.5rem)] flex-nowrap items-center gap-1.5 overflow-x-auto px-1 pb-1 text-[11px] text-text-subtle [scrollbar-width:none] [&::-webkit-scrollbar]:hidden sm:mx-0 sm:max-w-none sm:flex-wrap sm:overflow-visible sm:px-0 sm:pb-0">
                <span className="shrink-0 rounded-full bg-surface px-2 py-0.5">{t('ui.turns')} {visibleTrace.stats.turns}</span>
                <span className="shrink-0 rounded-full bg-surface px-2 py-0.5">{t('ui.tools')} {visibleTrace.stats.toolCalls}</span>
                <span className="shrink-0 rounded-full bg-surface px-2 py-0.5">{t('ui.skills')} {visibleTrace.stats.skills}</span>
                <span className="shrink-0 rounded-full bg-surface px-2 py-0.5">{t('ui.thoughts')} {visibleTrace.stats.thoughts}</span>
                <span className="shrink-0 rounded-full bg-surface px-2 py-0.5">{t('ui.errors')} {visibleTrace.stats.errors}</span>
              </div>
              <div className="flex flex-wrap items-center gap-2 text-[11px] text-text-subtle">
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
                      <p className="line-clamp-2 text-xs text-text-muted break-words">{summarizeEventPreview(event.detail)}</p>
                      <div className="flex min-w-0 flex-nowrap items-center gap-2 overflow-x-auto pb-0.5 text-[11px] text-text-subtle [scrollbar-width:none] [&::-webkit-scrollbar]:hidden">
                        <span className="shrink-0">{t('ui.turn')} {event.turnId}</span>
                        {event.toolName ? <span className="max-w-[12rem] shrink-0 truncate">{t('ui.tool')} {event.toolName}</span> : null}
                        {event.skillName ? <span className="max-w-[12rem] shrink-0 truncate">{t('ui.skills')} {event.skillName}</span> : null}
                        {event.callId ? <span className="max-w-[12rem] shrink-0 truncate">{t('ui.call')} {event.callId}</span> : null}
                        <span className="shrink-0">{formatTimestamp(event.timestamp)}</span>
                        <span className="shrink-0 text-text-muted">{t('knowledge.openDetails')}</span>
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
