import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import {
  applyGatewayEvent,
  attachmentRpcMethod,
  createRpcRequest,
  ConnectionAttemptGate,
  eventActivity,
  extractInflightAssistant,
  extractInteractionRequest,
  extractSessionId,
  extractSessionKey,
  extractSessionModel,
  extractSessionRunning,
  extractTranscript,
  getRpcErrorMessage,
  isResponseFor,
  parseCommandDispatch,
  parseSlash,
  refreshModelAfterCommandDispatch,
  normalizeTranscript,
  parseGatewayFrame,
  pendingPromptWasPersisted,
  shouldCloseBackendSessionForNewChat,
  type ChatActivity,
  type ChatAttachmentSummary,
  type ChatAttachmentUpload,
  type ChatMessage,
  type ChatModelIdentity,
  type ChatModelSwitchResult,
  type GatewayCommandDispatch,
  type GatewayEvent,
  type GatewayInteractionRequest,
} from './chat-protocol';
import { deriveTodoPlan, normalizeTodoPlanSnapshot, type TodoPlan } from './todo-plan';
import type { ChatSlashCompletionResponse } from '../components/ChatSlashPopover';
import { CHAT_PRESENCE_EVENT, getChatPresence, getChatReadState, publishChatPresence } from './chat-presence';
import { fetchServerLastChat, persistChat, persistChatTitle, readPersistedChat, syncLastChatToServer } from './chat-persistence';
import { canClaimLastChatPointer, createChatBootstrapGuard, shouldAdoptServerPointer, type LastChatClaimAction, type ServerLastChat } from './chat-bootstrap';
import { clearPendingChatSubmit, persistPendingChatSubmit, readPendingChatSubmit, type PendingChatSubmit } from './chat-outbox';
import { applySyncedAssistantMessage, applySyncedChatMessage, applySyncedUserMessage, chatSyncStreamUrl, fetchChatTranscript, publishChatSync, replaceWithCanonicalChatMessages, shouldApplySequencedEvent, type ChatSyncEnvelope } from './chat-sync';
import { getWebSocketUrl, MAX_RECONNECTS, mintWsCredential, nextReconnectDelay, RPC_TIMEOUT_MS } from './chat-transport';
import { commandOutput, executeReasoningSlashCommand, resultText } from './chat-commands';
import {
  extractClarifyToolContent,
  interactionTitle,
  mergeClarifyInteractionContent,
  normalizeClarifyInteraction,
  type ClarifyInteractionContent,
} from './chat-interactions';
import { recordReloadDiagnostic } from './reload-diagnostics';
import { publishChatRuntimePresence } from './chat-runtime-presence';
import { addChatProfile } from './chat-session-params';

// Backward-compatible re-export for ChatDrawer consumers during the gateway split.
export { interactionTitle };

type ConnectionState = 'idle' | 'ticket' | 'connecting' | 'connected' | 'reconnecting' | 'error';

type PendingRpc = {
  id: string;
  method: string;
  resolve: (value: unknown) => void;
  reject: (error: Error) => void;
  timeout: number;
};

type PendingPrompt = PendingChatSubmit;

type ResumedRuntimePresence = {
  runtimeSessionId: string;
  resumedFrom: string;
  sessionKey: string | null;
};

export type PendingAttachment = Omit<ChatAttachmentSummary, 'id'> & {
  id: string;
  file: File;
  previewUrl: string | null;
};

export const MAX_ATTACHMENTS = 5;
export const MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024;
export { classifyAttachment } from './chat-protocol';

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

export function readFileAsDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(typeof reader.result === 'string' ? reader.result : '');
    reader.onerror = () => reject(reader.error ?? new Error(`Could not read ${file.name}.`));
    reader.readAsDataURL(file);
  });
}

function persistedRunWasCompleted(): boolean {
  const presence = getChatPresence();
  if (presence.phase !== 'running') return false;
  const persisted = readPersistedChat();
  const lastTurnMessage = [...persisted.messages].reverse().find((message) => message.role === 'assistant' || message.role === 'user');
  const hasVisibleCompletedAssistant = lastTurnMessage?.role === 'assistant' && Boolean(lastTurnMessage.text.trim());
  const hasStreamingAssistant = persisted.messages.some((message) => message.role === 'assistant' && message.status === 'streaming');
  const hasCompletedAssistant = persisted.messages.some((message) => message.role === 'assistant' && message.status !== 'streaming' && message.text.trim());
  return hasVisibleCompletedAssistant || (!hasStreamingAssistant && hasCompletedAssistant);
}

async function loadCanonicalTranscript(
  accessToken: string,
  sessionId: string | null,
  sessionKey: string | null,
): Promise<ChatMessage[] | null> {
  const payload = await fetchChatTranscript(accessToken, sessionId, sessionKey);
  if (!payload?.complete) return null;
  return normalizeTranscript(payload.messages);
}

function applyLiveGatewayEvent(messages: ChatMessage[], event: GatewayEvent): ChatMessage[] {
  const existingIds = new Set(messages.map((message) => message.id));
  return applyGatewayEvent(messages, event).map((message) => existingIds.has(message.id)
    ? message
    : { ...message, source: 'live' as const });
}

export function useGatewayChat(
  storedToken: string,
  open: boolean,
  initialSessionId?: string | null,
  botProfile?: string | null,
) {
  const initial = useMemo(readPersistedChat, []);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [todoPlan, setTodoPlan] = useState<TodoPlan | null>(null);
  const [sessionId, setSessionId] = useState<string | null>(initial.sessionId);
  const [sessionKey, setSessionKey] = useState<string | null>(initial.sessionKey);
  const [connectionState, setConnectionState] = useState<ConnectionState>('idle');
  const [statusText, setStatusText] = useState('Disconnected');
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const restoredCompleted = persistedRunWasCompleted();
  const [running, setRunning] = useState(() => getChatPresence().phase === 'running' && !restoredCompleted);
  const [completed, setCompleted] = useState(() => getChatPresence().phase === 'completed' || restoredCompleted);
  const [activity, setActivity] = useState<ChatActivity | null>(() => {
    const presence = getChatPresence();
    return presence.phase === 'running' && !restoredCompleted && presence.verb
      ? { kind: 'status', label: presence.verb, detail: '', state: 'running' }
      : null;
  });
  const [interaction, setInteraction] = useState<GatewayInteractionRequest | null>(null);
  const [modelIdentity, setModelIdentity] = useState<ChatModelIdentity | null>(initial.modelIdentity);
  const [contextTokens, setContextTokens] = useState<number | null>(null);
  const [contextMax, setContextMax] = useState<number | null>(null);
  const [sessionTitle, setSessionTitle] = useState<string | null>(null);
  const [modelPickerOpen, setModelPickerOpen] = useState(false);
  const [modelPickerRefresh, setModelPickerRefresh] = useState(false);
  const [commandPrefill, setCommandPrefill] = useState<string | null>(null);
  const [previewMode, setPreviewMode] = useState<boolean>(Boolean(initialSessionId?.trim()));
  const [resumedRuntime, setResumedRuntime] = useState<ResumedRuntimePresence | null>(null);
  const [pointerRevision, setPointerRevision] = useState<number | null>(initial.revision);
  const wsRef = useRef<WebSocket | null>(null);
  const pendingRef = useRef(new Map<string, PendingRpc>());
  const pendingPromptRef = useRef<PendingPrompt | null>(readPendingChatSubmit());
  const durableTranscriptRef = useRef<ReturnType<typeof extractTranscript>>([]);
  const transcriptReadyRef = useRef(false);
  const replayInFlightRef = useRef(false);
  const connectionAttemptGateRef = useRef(new ConnectionAttemptGate());
  const connectionGenerationRef = useRef(0);
  const requestSeqRef = useRef(0);
  const reconnectAttemptsRef = useRef(0);
  const reconnectTimerRef = useRef<number | null>(null);
  const activityTimerRef = useRef<number | null>(null);
  const readyTimerRef = useRef<number | null>(null);
  const presenceTimerRef = useRef<number | null>(null);
  const sessionIdRef = useRef(sessionId);
  const sessionKeyRef = useRef(sessionKey);
  const interactionRef = useRef(interaction);
  const pendingClarifyContentRef = useRef<ClarifyInteractionContent | null>(null);
  const intentionalCloseRef = useRef(false);
  const requestedSessionIdRef = useRef<string | null>(initialSessionId ?? null);
  const previewModeRef = useRef<boolean>(Boolean(initialSessionId?.trim()));
  const connectRef = useRef<() => Promise<void>>(async () => {});
  const readyResolveRef = useRef<(() => void) | null>(null);
  const eventWatermarksRef = useRef(new Map<string, number>());
  const replayEpochRef = useRef<string | null>(null);
  const replayHoldRef = useRef<Map<string, GatewayEvent[]> | null>(null);
  const eventReplayInFlightRef = useRef(false);
  const snapshotSyncInFlightRef = useRef(false);
  const chatSyncSourceRef = useRef<EventSource | null>(null);
  const chatSyncReconnectTimerRef = useRef<number | null>(null);
  const chatSyncRelaySeqRef = useRef(new Map<string, number>());
  const pointerRef = useRef<ServerLastChat | null>(initial.sessionId && initial.revision ? {
    sessionId: initial.sessionId,
    sessionKey: initial.sessionKey,
    modelIdentity: initial.modelIdentity,
    revision: initial.revision,
  } : null);
  const pointerBootstrapPromiseRef = useRef<Promise<void> | null>(null);
  const pointerBootstrapGuardRef = useRef(createChatBootstrapGuard());

  useEffect(() => {
    setMessages((current) => {
      const hasCompletedAssistant = current.some((message) => message.kind === 'assistant' && message.status !== 'streaming' && message.text.trim());
      if (!hasCompletedAssistant) return current;
      const cleaned = current.filter((message) => !(message.kind === 'assistant' && message.status === 'streaming' && !message.text.trim()));
      return cleaned.length === current.length ? current : cleaned;
    });
  }, []);

  useEffect(() => {
    sessionIdRef.current = sessionId;
  }, [sessionId]);

  useEffect(() => {
    sessionKeyRef.current = sessionKey;
  }, [sessionKey]);

  useEffect(() => {
    interactionRef.current = interaction;
  }, [interaction]);

  useEffect(() => {
    const requested = initialSessionId?.trim() || null;
    requestedSessionIdRef.current = requested;
    previewModeRef.current = Boolean(requested);
    setPreviewMode(Boolean(requested));
    setResumedRuntime(null);
    if (!requested) return;
    setSessionId(requested);
    sessionIdRef.current = requested;
    setSessionKey(requested);
    sessionKeyRef.current = requested;
    setMessages([]);
    setTodoPlan(null);
    transcriptReadyRef.current = false;
    setModelIdentity(null);
    setModelPickerOpen(false);
    setModelPickerRefresh(false);
    setInteraction(null);
    setActivity(null);
  }, [initialSessionId]);

  useEffect(() => {
    if (!transcriptReadyRef.current) return;
    persistChat(sessionId, sessionKey, modelIdentity, messages, pointerRevision);
  }, [messages, modelIdentity, pointerRevision, sessionId, sessionKey]);

  const adoptServerPointer = useCallback((serverChat: ServerLastChat) => {
    pointerRef.current = serverChat;
    setPointerRevision(serverChat.revision);
    const currentSessionId = sessionIdRef.current;
    if (currentSessionId !== serverChat.sessionId) {
      setResumedRuntime(null);
      setSessionId(serverChat.sessionId);
      sessionIdRef.current = serverChat.sessionId;
      setSessionKey(serverChat.sessionKey ?? serverChat.sessionId);
      sessionKeyRef.current = serverChat.sessionKey ?? serverChat.sessionId;
      setModelIdentity(serverChat.modelIdentity ?? null);
      requestedSessionIdRef.current = serverChat.sessionId;
      return;
    }
    if (serverChat.sessionKey) {
      setSessionKey(serverChat.sessionKey);
      sessionKeyRef.current = serverChat.sessionKey;
    }
    if (serverChat.modelIdentity) setModelIdentity(serverChat.modelIdentity);
  }, []);

  // Generic drawer open is a read/adopt barrier. It never claims the shared
  // pointer; only an explicit create/resume/submit action may do that.
  const bootstrapPointer = useCallback(async (): Promise<void> => {
    if (initialSessionId?.trim()) return;
    const existing = pointerBootstrapPromiseRef.current;
    if (existing) {
      await existing;
      return;
    }
    const attempt = pointerBootstrapGuardRef.current.begin();
    const bootstrap = fetchServerLastChat(storedToken, attempt.signal).then((serverChat) => {
      if (!pointerBootstrapGuardRef.current.isCurrent(attempt) || !serverChat) return;
      const local = readPersistedChat();
      if (shouldAdoptServerPointer({ sessionId: local.sessionId, revision: local.revision }, serverChat)) {
        adoptServerPointer(serverChat);
      } else {
        pointerRef.current = serverChat;
        setPointerRevision(serverChat.revision);
      }
    }).finally(() => {
      if (pointerBootstrapPromiseRef.current === bootstrap) pointerBootstrapPromiseRef.current = null;
    });
    pointerBootstrapPromiseRef.current = bootstrap;
    return bootstrap;
  }, [adoptServerPointer, initialSessionId, storedToken]);

  useEffect(() => {
    if (presenceTimerRef.current !== null) window.clearTimeout(presenceTimerRef.current);
    presenceTimerRef.current = window.setTimeout(() => {
      const readState = getChatReadState();
      const assistantCount = messages.filter((message) => message.role === 'assistant' && message.status !== 'streaming').length;
      const unreadCount = open || readState.sessionKey !== sessionKey
        ? (open ? 0 : assistantCount)
        : Math.max(0, assistantCount - readState.assistantCount);
      const preview = [...messages].reverse().find((message) => message.role === 'assistant' && message.text)?.text?.replace(/\s+/g, ' ').slice(0, 120) || null;
      const phase = interaction ? 'waiting' : running ? 'running' : completed ? 'completed' : !open && unreadCount > 0 ? 'unread' : 'idle';
      publishChatPresence({ sessionKey, sessionTitle, phase, verb: activity?.label || null, preview, unreadCount });
      presenceTimerRef.current = null;
    }, 120);
    return () => {
      if (presenceTimerRef.current !== null) {
        window.clearTimeout(presenceTimerRef.current);
        presenceTimerRef.current = null;
      }
    };
  }, [activity, completed, interaction, messages, open, running, sessionKey, sessionTitle]);

  useEffect(() => {
    const handlePresenceAck = (event: Event) => {
      if (!(event instanceof CustomEvent)) return;
      const detail = event.detail as { phase?: unknown } | undefined;
      if (detail?.phase === 'idle') setCompleted(false);
    };
    window.addEventListener(CHAT_PRESENCE_EVENT, handlePresenceAck);
    return () => window.removeEventListener(CHAT_PRESENCE_EVENT, handlePresenceAck);
  }, []);

  useEffect(() => {
    if (!open || initialSessionId?.trim()) return;
    void bootstrapPointer();
    return () => {
      // A closed drawer starts a new bootstrap lifecycle on reopen. Do not let
      // the old GET adopt its response, even if the transport ignores abort.
      pointerBootstrapGuardRef.current.invalidate();
      pointerBootstrapPromiseRef.current = null;
    };
  }, [bootstrapPointer, initialSessionId, open]);

  const rejectPending = useCallback((message: string) => {
    for (const pending of pendingRef.current.values()) {
      window.clearTimeout(pending.timeout);
      pending.reject(new Error(message));
    }
    pendingRef.current.clear();
  }, []);

  const request = useCallback(<T,>(method: string, params: Record<string, unknown> = {}) => {
    const ws = wsRef.current;
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      return Promise.reject(new Error('Chat gateway is not connected.'));
    }

    const id = `mc-${++requestSeqRef.current}`;
    const frame = createRpcRequest(id, method, params);
    return new Promise<T>((resolve, reject) => {
      const timeout = window.setTimeout(() => {
        pendingRef.current.delete(id);
        reject(new Error(`Gateway request timed out: ${method}`));
      }, RPC_TIMEOUT_MS);
      pendingRef.current.set(id, { id, method, resolve: (value) => resolve(value as T), reject, timeout });
      try {
        ws.send(JSON.stringify(frame));
      } catch (err) {
        window.clearTimeout(timeout);
        pendingRef.current.delete(id);
        reject(err instanceof Error ? err : new Error('Failed to send chat request.'));
      }
    });
  }, []);

  const claimLastChatPointer = useCallback(async (
    action: LastChatClaimAction,
    activeSessionId: string,
  ): Promise<string> => {
    if (!canClaimLastChatPointer(action)) return activeSessionId;
    const result = await syncLastChatToServer(
      activeSessionId,
      sessionKeyRef.current || activeSessionId,
      modelIdentity,
      storedToken,
      pointerRef.current?.revision ?? null,
    );
    if (result.lastChat) adoptServerPointer(result.lastChat);
    return result.lastChat?.sessionId ?? activeSessionId;
  }, [adoptServerPointer, modelIdentity, storedToken]);

  const prepareEventReplay = useCallback((): { sessionId: string; lastSeen: number } | null => {
    if (eventReplayInFlightRef.current || replayHoldRef.current) return null;
    const activeSessionId = sessionIdRef.current;
    if (!activeSessionId) return null;
    const lastSeen = eventWatermarksRef.current.get(activeSessionId);
    if (lastSeen === undefined) return null;
    replayHoldRef.current = new Map([[activeSessionId, []]]);
    return { sessionId: activeSessionId, lastSeen };
  }, []);

  const finishEventReplay = useCallback(async (replay: { sessionId: string; lastSeen: number } | null) => {
    if (!replay || eventReplayInFlightRef.current) return;
    eventReplayInFlightRef.current = true;
    let replayEvents: GatewayEvent[] = [];
    try {
      const result = await request<unknown>('session.events.since', {
        session_id: replay.sessionId,
        last_seen: replay.lastSeen,
      });
      if (isRecord(result)) {
        const epoch = typeof result.epoch === 'string' ? result.epoch : null;
        if (epoch && replayEpochRef.current && epoch !== replayEpochRef.current) {
          eventWatermarksRef.current.clear();
          replayEvents = [];
        } else if (!result.truncated && Array.isArray(result.events)) {
          replayEvents = result.events.filter((event): event is GatewayEvent => isRecord(event) && typeof event.type === 'string');
        }
        if (epoch) replayEpochRef.current = epoch;
      }
    } catch {
      // Resume reconciliation below remains the authoritative fallback.
    } finally {
      const heldEvents = replayHoldRef.current?.get(replay.sessionId) || [];
      const candidates = [...replayEvents, ...heldEvents];
      const accepted = candidates.filter((event) => shouldApplySequencedEvent(eventWatermarksRef.current, event));
      if (accepted.length > 0) {
        setMessages((current) => accepted.reduce((messages, event) => applyLiveGatewayEvent(messages, event), current));
      }
      replayHoldRef.current = null;
      eventReplayInFlightRef.current = false;
    }
  }, [request]);

  const hydrateSessionSnapshot = useCallback(async (
    resumed: unknown,
    activeSessionId: string | null = null,
    activeSessionKey: string | null = null,
  ) => {
    const rawTranscript = extractTranscript(resumed);
    const resolvedSessionId = activeSessionId ?? extractSessionId(resumed);
    const resolvedSessionKey = activeSessionKey ?? extractSessionKey(resumed) ?? resolvedSessionId;
    const canonicalTranscript = await loadCanonicalTranscript(storedToken, resolvedSessionId, resolvedSessionKey);
    const transcript = canonicalTranscript ?? [];
    const inflight = extractInflightAssistant(resumed);
    const inflightMessage: ChatMessage | null = inflight
      ? {
        id: `inflight-sync-${Date.now()}`,
        role: 'assistant',
        kind: 'assistant',
        text: inflight,
        status: 'streaming',
        source: 'live',
        createdAt: Date.now(),
      }
      : null;

    durableTranscriptRef.current = rawTranscript;
    transcriptReadyRef.current = canonicalTranscript !== null;
    if (canonicalTranscript !== null) {
      setMessages((current) => {
        const next = replaceWithCanonicalChatMessages(current, transcript);
        return inflightMessage ? [...next, inflightMessage] : next;
      });
    }
    const resumedTodoPlan = normalizeTodoPlanSnapshot(isRecord(resumed) ? resumed.todo_state : undefined);
    if (resumedTodoPlan) setTodoPlan(resumedTodoPlan);
    else if (transcript.length > 0) setTodoPlan(deriveTodoPlan(transcript));
    return { transcript, inflight };
  }, [storedToken]);

  const reconcileSessionSnapshot = useCallback(async (activeSessionId: string) => {
    try {
      const resumed = await request<unknown>('session.resume', addChatProfile({
        session_id: activeSessionId,
        cols: 80,
        eager_build: true,
        source: 'mission-control',
      }, botProfile));
      await hydrateSessionSnapshot(resumed, activeSessionId, sessionKeyRef.current ?? activeSessionId);
    } catch {
      // A disconnected or busy gateway is retried by the next reconciliation tick.
    }
  }, [botProfile, hydrateSessionSnapshot, request]);

  useEffect(() => {
    if (!open || previewMode || connectionState !== 'connected') return;
    const timer = window.setInterval(() => {
      const activeSessionId = sessionIdRef.current;
      if (!activeSessionId || snapshotSyncInFlightRef.current) return;
      snapshotSyncInFlightRef.current = true;
      void reconcileSessionSnapshot(activeSessionId).finally(() => {
        snapshotSyncInFlightRef.current = false;
      });
    }, 2000);
    return () => window.clearInterval(timer);
  }, [connectionState, open, previewMode, reconcileSessionSnapshot]);

  useEffect(() => {
    if (!open || previewMode || connectionState !== 'connected') return;
    const refreshOnReturn = () => {
      if (document.visibilityState === 'hidden') return;
      const activeSessionId = sessionIdRef.current;
      if (!activeSessionId || snapshotSyncInFlightRef.current) return;
      snapshotSyncInFlightRef.current = true;
      void reconcileSessionSnapshot(activeSessionId).finally(() => {
        snapshotSyncInFlightRef.current = false;
      });
    };
    const handleVisibility = () => {
      if (document.visibilityState === 'visible') refreshOnReturn();
    };
    document.addEventListener('visibilitychange', handleVisibility);
    window.addEventListener('pageshow', refreshOnReturn);
    return () => {
      document.removeEventListener('visibilitychange', handleVisibility);
      window.removeEventListener('pageshow', refreshOnReturn);
    };
  }, [connectionState, open, previewMode, reconcileSessionSnapshot]);

  useEffect(() => {
    const activeSessionId = sessionId;
    if (!open || previewMode || connectionState !== 'connected' || !activeSessionId || !storedToken.trim()) {
      chatSyncSourceRef.current?.close();
      chatSyncSourceRef.current = null;
      if (chatSyncReconnectTimerRef.current !== null) window.clearTimeout(chatSyncReconnectTimerRef.current);
      chatSyncReconnectTimerRef.current = null;
      return;
    }

    let disposed = false;
    let source: EventSource | null = null;

    const handleEnvelope = (raw: Event) => {
      let value: unknown;
      try {
        value = JSON.parse((raw as MessageEvent<string>).data) as unknown;
      } catch {
        return;
      }
      if (!isRecord(value) || value.session_id !== activeSessionId || typeof value.relay_seq !== 'number' || !isRecord(value.payload)) return;
      const envelope = value as unknown as ChatSyncEnvelope;
      const previousRelaySeq = chatSyncRelaySeqRef.current.get(activeSessionId) ?? 0;
      if (envelope.relay_seq <= previousRelaySeq) return;
      chatSyncRelaySeqRef.current.set(activeSessionId, envelope.relay_seq);

      if (envelope.kind === 'gateway_event') {
        const relayedEvent = envelope.payload as unknown as GatewayEvent;
        if (relayedEvent.session_id && relayedEvent.session_id !== activeSessionId) return;
        if (!relayedEvent.session_id) relayedEvent.session_id = activeSessionId;
        if (!shouldApplySequencedEvent(eventWatermarksRef.current, relayedEvent)) return;
        if (relayedEvent.type === 'todo.updated') {
          const relayedTodo = normalizeTodoPlanSnapshot(relayedEvent.payload);
          if (relayedTodo) setTodoPlan(relayedTodo);
        }
        const relayedActivity = eventActivity(relayedEvent);
        if (relayedActivity) {
          setActivity(relayedActivity);
          if (relayedActivity.state === 'running') setRunning(true);
          if (relayedActivity.state === 'complete') {
            setRunning(false);
            setCompleted(true);
          }
          if (relayedActivity.state === 'error') setRunning(false);
        }
        setMessages((current) => applyLiveGatewayEvent(current, relayedEvent));
        return;
      }

      if (envelope.kind === 'user_message' || envelope.kind === 'system_message' || envelope.kind === 'assistant_message') {
        const message = envelope.payload as unknown as ChatMessage;
        const expectedRole = envelope.kind === 'user_message' ? 'user' : envelope.kind === 'assistant_message' ? 'assistant' : 'system';
        if (message.role !== expectedRole || typeof message.id !== 'string' || typeof message.text !== 'string') return;
        setMessages((current) => envelope.kind === 'user_message'
          ? applySyncedUserMessage(current, message)
          : envelope.kind === 'assistant_message'
            ? applySyncedAssistantMessage(current, message)
            : applySyncedChatMessage(current, message));
      }
    };

    const connectRelay = () => {
      if (disposed) return;
      const since = chatSyncRelaySeqRef.current.get(activeSessionId);
      const nextSource = new EventSource(chatSyncStreamUrl(activeSessionId, storedToken, since));
      source = nextSource;
      chatSyncSourceRef.current = nextSource;
      nextSource.addEventListener('chat-sync-ready', (event) => {
        try {
          const ready = JSON.parse((event as MessageEvent<string>).data) as { latest_seq?: unknown };
          // A fresh Mission Control client must replay the existing relay buffer:
          // it contains MC-only user/assistant projections that are not in Hermes
          // SessionDB. The message/event IDs and relay watermark dedupe repeats.
          if (since !== undefined && typeof ready.latest_seq === 'number') {
            chatSyncRelaySeqRef.current.set(activeSessionId, Math.max(since, ready.latest_seq));
          }
        } catch {
          // A malformed readiness event must not disable the direct gateway.
        }
      });
      nextSource.addEventListener('chat-sync', handleEnvelope);
      nextSource.onerror = () => {
        nextSource.close();
        if (disposed) return;
        chatSyncReconnectTimerRef.current = window.setTimeout(connectRelay, 1000);
      };
    };

    connectRelay();
    return () => {
      disposed = true;
      source?.close();
      if (chatSyncSourceRef.current === source) chatSyncSourceRef.current = null;
      if (chatSyncReconnectTimerRef.current !== null) window.clearTimeout(chatSyncReconnectTimerRef.current);
      chatSyncReconnectTimerRef.current = null;
    };
  }, [connectionState, open, previewMode, sessionId, storedToken]);

  const adoptModel = useCallback((result: unknown) => {
    const next = extractSessionModel(result);
    if (next) setModelIdentity((current) => ({ ...current, ...next }));
    if (isRecord(result)) {
      const title = typeof result.title === 'string' && result.title.trim() ? result.title.trim() : null;
      if (title) setSessionTitle(title);
    }
    return next;
  }, []);

  const refreshReasoning = useCallback(async (activeSessionId: string) => {
    try {
      // `reasoning` is session-scoped in the gateway. Do not infer it from
      // config.yaml: a `/reasoning high` override must win for this chat only.
      const result = await request<unknown>('config.get', {
        key: 'reasoning',
        session_id: activeSessionId,
      });
      if (!isRecord(result) || typeof result.value !== 'string' || !result.value.trim()) return;
      const reasoningEffort = result.value.trim();
      setModelIdentity((current) => current ? { ...current, reasoningEffort } : current);
    } catch {
      // The session payload remains the fallback; this is a display-only refresh.
    }
  }, [request]);

  useEffect(() => {
    if (!open) return;
    const timer = window.setInterval(() => {
      const activeSessionId = sessionIdRef.current;
      if (activeSessionId) void refreshReasoning(activeSessionId);
    }, 2000);
    return () => window.clearInterval(timer);
  }, [open, refreshReasoning]);

  const refreshModel = useCallback(async (activeSessionId: string) => {
    try {
      adoptModel(await request<unknown>('session.status', { session_id: activeSessionId }));
    } catch {
      // The create/resume payload already carries the model; status is a best-effort refresh.
    }
    await refreshReasoning(activeSessionId);
  }, [adoptModel, refreshReasoning, request]);

  const refreshContext = useCallback(async (activeSessionId: string) => {
    try {
      const result = await request<unknown>('session.context_breakdown', { session_id: activeSessionId });
      if (!isRecord(result)) return;
      if (typeof result.context_used === 'number') setContextTokens(result.context_used);
      if (typeof result.context_max === 'number' && result.context_max > 0) setContextMax(result.context_max);
    } catch {
      // Context metrics are best-effort; the status line remains usable without them.
    }
  }, [request]);

  const ensureSession = useCallback(async (preferredSessionId?: string | null) => {
    const explicitSessionId = preferredSessionId?.trim() || initialSessionId?.trim() || null;
    const existingKey = explicitSessionId || requestedSessionIdRef.current || sessionKeyRef.current || sessionIdRef.current;
    const isExplicitResume = Boolean(explicitSessionId);
    if (existingKey) {
      try {
        const resumed = await request<unknown>('session.resume', addChatProfile({
          session_id: existingKey,
          cols: 80,
          eager_build: true,
          source: 'mission-control',
        }, botProfile));
        adoptModel(resumed);
        const resolvedSessionId = extractSessionId(resumed) ?? sessionIdRef.current ?? existingKey;
        const resolvedSessionKey = extractSessionKey(resumed) ?? existingKey;
        setSessionId(resolvedSessionId);
        sessionIdRef.current = resolvedSessionId;
        setSessionKey(resolvedSessionKey);
        sessionKeyRef.current = resolvedSessionKey;
        setResumedRuntime({
          runtimeSessionId: resolvedSessionId,
          resumedFrom: existingKey,
          sessionKey: resolvedSessionKey,
        });
        const { inflight } = await hydrateSessionSnapshot(resumed, resolvedSessionId, resolvedSessionKey);
        setRunning(Boolean(inflight) || extractSessionRunning(resumed));
        return resolvedSessionId;
      } catch (err) {
        recordReloadDiagnostic('chat-session-resume-fallback', {
          reason: 'requested-session-unavailable',
          requested: existingKey,
          error: err instanceof Error ? err.message : String(err),
        });
        if (isExplicitResume) throw err;
        // A stale/deleted non-explicit pointer is a normal recovery path: discard
        // it and start a fresh chat. Explicit URL resumes must never silently
        // become a new session with the global default model.
        requestedSessionIdRef.current = null;
        previewModeRef.current = false;
        setPreviewMode(false);
        setError(null);
        // Transport failures still fall through to the fresh-session attempt;
        // if that fails, the actual creation error is surfaced to the user.
      }
    }

    const created = await request<unknown>('session.create', addChatProfile({ cols: 80, source: 'mission-control' }, botProfile));
    setResumedRuntime(null);
    adoptModel(created);
    const createdSessionId = extractSessionId(created);
    if (!createdSessionId) throw new Error('Gateway did not return a session id.');
    const createdSessionKey = extractSessionKey(created);
    setSessionId(createdSessionId);
    sessionIdRef.current = createdSessionId;
    setSessionKey(createdSessionKey);
    sessionKeyRef.current = createdSessionKey;
    setRunning(false);
    return createdSessionId;
  }, [adoptModel, botProfile, hydrateSessionSnapshot, initialSessionId, request]);

  const clearPendingPrompt = useCallback(() => {
    pendingPromptRef.current = null;
    clearPendingChatSubmit();
  }, []);

  const replayPendingPrompt = useCallback(async (activeSessionId: string) => {
    const pending = pendingPromptRef.current;
    if (!pending || replayInFlightRef.current) return;
    const activeKey = sessionKeyRef.current || activeSessionId;
    if (pending.sessionKey && pending.sessionKey !== activeKey && pending.sessionKey !== activeSessionId) return;
    if (pendingPromptWasPersisted(pending, durableTranscriptRef.current)) {
      clearPendingPrompt();
      return;
    }

    replayInFlightRef.current = true;
    setSubmitting(true);
    setRunning(true);
    setError(null);
    const replayedUserMessage: ChatMessage = {
      id: pending.messageId || `user-replay-${Date.now()}-${Math.random().toString(36).slice(2)}`,
      role: 'user',
      kind: 'user',
      source: 'live',
      text: pending.displayText,
      attachments: pending.attachments,
      status: 'complete',
      createdAt: Date.now(),
    };
    if (!pending.messageId) {
      const persistedPending = { ...pending, messageId: replayedUserMessage.id };
      pendingPromptRef.current = persistedPending;
      persistPendingChatSubmit(persistedPending);
    }
    setMessages((current) => {
      const userMessages = current.filter((message) => message.role === 'user');
      const lastUser = userMessages.at(-1);
      if (userMessages.length > pending.baselineUserCount && lastUser?.text === pending.displayText) return current;
      return [...current, replayedUserMessage];
    });
    void publishChatSync(storedToken, activeSessionId, 'user_message', replayedUserMessage as unknown as Record<string, unknown>);
    try {
      await request('prompt.submit', { session_id: activeSessionId, text: pending.text });
      clearPendingPrompt();
      void refreshModel(activeSessionId);
    } catch {
      // Keep the outbox entry. The next successful reconnect/resume gets one
      // more reconciliation chance instead of silently dropping the prompt.
      setError('Message queued until the chat connection is restored.');
      setRunning(false);
    } finally {
      replayInFlightRef.current = false;
      setSubmitting(false);
    }
  }, [clearPendingPrompt, refreshModel, request, storedToken]);

  useEffect(() => {
    if (!open || !initialSessionId || wsRef.current?.readyState !== WebSocket.OPEN) return;
    // In preview mode the drawer opens showing the session preview; resume is
    // explicit via the "Resume session" button, not automatic.
    if (previewModeRef.current) return;
    void ensureSession().catch((err: unknown) => {
      setError(err instanceof Error ? err.message : 'Failed to recover the selected session.');
    });
  }, [ensureSession, initialSessionId, open]);

  const resumeSession = useCallback(async (): Promise<string | null> => {
    previewModeRef.current = false;
    setPreviewMode(false);
    try {
      let activeSessionId = await ensureSession(initialSessionId);
      if (activeSessionId) {
        const canonicalSessionId = await claimLastChatPointer('resume', activeSessionId);
        if (canonicalSessionId !== activeSessionId) activeSessionId = await ensureSession(canonicalSessionId);
        void refreshModel(activeSessionId);
        void refreshContext(activeSessionId);
        void replayPendingPrompt(activeSessionId);
      }
      return activeSessionId ?? null;
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to resume the selected session.');
      return null;
    }
  }, [claimLastChatPointer, ensureSession, initialSessionId, refreshContext, refreshModel, replayPendingPrompt]);

  useEffect(() => {
    if (!open || previewMode || connectionState !== 'connected' || !resumedRuntime) return;

    const payload = {
      runtimeSessionId: resumedRuntime.runtimeSessionId,
      resumedFrom: resumedRuntime.resumedFrom,
      sessionKey: resumedRuntime.sessionKey,
      model: modelIdentity?.model ?? null,
      title: sessionTitle ?? null,
      phase: 'connected' as const,
      source: 'mission-control',
    };
    const publish = () => void publishChatRuntimePresence(storedToken, payload);

    publish();
    const timer = window.setInterval(publish, 5_000);
    return () => {
      window.clearInterval(timer);
      void publishChatRuntimePresence(storedToken, { ...payload, phase: 'closed' });
    };
  }, [connectionState, open, previewMode, resumedRuntime, storedToken]);

  const scheduleReconnect = useCallback(() => {
    if (intentionalCloseRef.current || reconnectAttemptsRef.current >= MAX_RECONNECTS) {
      setConnectionState('error');
      setStatusText('Disconnected');
      setError('Chat connection closed. Reopen or retry to reconnect.');
      return;
    }

    const delay = nextReconnectDelay({ attempts: reconnectAttemptsRef.current });
    reconnectAttemptsRef.current += 1;
    setConnectionState('reconnecting');
    setStatusText(`Reconnecting in ${Math.ceil(delay / 1000)}s`);
    reconnectTimerRef.current = window.setTimeout(() => {
      void connectRef.current();
    }, delay);
  }, []);

  const connect = useCallback(async () => {
    if (!open) return;
    // A prior drawer cleanup marks its socket intentional. Clear that stale
    // marker before awaiting bootstrap; a new cleanup can still set it again.
    intentionalCloseRef.current = false;
    if (!initialSessionId?.trim()) await bootstrapPointer();
    if (!open || intentionalCloseRef.current) return;
    if (wsRef.current?.readyState === WebSocket.OPEN || wsRef.current?.readyState === WebSocket.CONNECTING) return;
    if (!connectionAttemptGateRef.current.tryAcquire()) return;
    const attemptGeneration = ++connectionGenerationRef.current;
    const releaseAttempt = () => {
      if (connectionGenerationRef.current === attemptGeneration) {
        connectionAttemptGateRef.current.release();
      }
    };
    if (reconnectTimerRef.current !== null) {
      window.clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = null;
    }

    intentionalCloseRef.current = false;
    setError(null);
    setConnectionState('ticket');
    setStatusText('Authorizing');

    try {
      const credential = await mintWsCredential(storedToken);
      if (!open || intentionalCloseRef.current || connectionGenerationRef.current !== attemptGeneration) {
        releaseAttempt();
        return;
      }
      const ws = new WebSocket(getWebSocketUrl(credential));
      wsRef.current = ws;
      setConnectionState('connecting');
      setStatusText('Connecting');

      let readySettled = false;
      const settleReady = () => {
        if (readySettled) return;
        readySettled = true;
        if (readyTimerRef.current !== null) window.clearTimeout(readyTimerRef.current);
        readyTimerRef.current = null;
        readyResolveRef.current = null;
      };
      const readyPromise = new Promise<void>((resolve) => {
        readyResolveRef.current = () => {
          settleReady();
          resolve();
        };
        readyTimerRef.current = window.setTimeout(() => {
          // Older gateways may not broadcast gateway.ready; keep a bounded compatibility fallback.
          settleReady();
          resolve();
        }, 3000);
      });

      ws.addEventListener('message', (event) => {
        const parsed = parseGatewayFrame(event.data);
        if (parsed.kind === 'malformed') {
          setError(parsed.error);
          return;
        }
        if (parsed.kind === 'response') {
          for (const pending of pendingRef.current.values()) {
            if (!isResponseFor(parsed.response, pending.id)) continue;
            window.clearTimeout(pending.timeout);
            pendingRef.current.delete(pending.id);
            if (parsed.response.error) pending.reject(new Error(getRpcErrorMessage(parsed.response.error)));
            else pending.resolve(parsed.response.result);
            break;
          }
          return;
        }
        if (parsed.kind !== 'event') return;

        if (parsed.event.type === 'gateway.ready') {
          setStatusText('Gateway ready');
          const epoch = parsed.event.payload && typeof parsed.event.payload.replay_epoch === 'string'
            ? parsed.event.payload.replay_epoch
            : null;
          if (epoch && replayEpochRef.current && epoch !== replayEpochRef.current) eventWatermarksRef.current.clear();
          if (epoch) replayEpochRef.current = epoch;
          readyResolveRef.current?.();
          return;
        }
        const eventPayload = parsed.event.payload ?? {};
        const eventSessionRefs = [
          parsed.event.session_id,
          typeof eventPayload.session_id === 'string' ? eventPayload.session_id : undefined,
          typeof eventPayload.stored_session_id === 'string' ? eventPayload.stored_session_id : undefined,
        ].filter((value): value is string => Boolean(value));
        const activeSessionRefs = [sessionIdRef.current, sessionKeyRef.current].filter((value): value is string => Boolean(value));
        if (activeSessionRefs.length > 0 && eventSessionRefs.length > 0 && !eventSessionRefs.some((value) => activeSessionRefs.includes(value))) return;
        if (parsed.event.session_id && parsed.event.type !== 'gateway.ready') {
          void publishChatSync(storedToken, parsed.event.session_id, 'gateway_event', parsed.event as unknown as Record<string, unknown>);
        }
        if (parsed.event.session_id && parsed.event.seq !== undefined) {
          const heldEvents = replayHoldRef.current?.get(parsed.event.session_id);
          if (heldEvents) {
            heldEvents.push(parsed.event);
            return;
          }
          if (!shouldApplySequencedEvent(eventWatermarksRef.current, parsed.event)) return;
        }
        if (parsed.event.type === 'todo.updated') {
          const liveTodoPlan = normalizeTodoPlanSnapshot(eventPayload);
          if (liveTodoPlan) setTodoPlan(liveTodoPlan);
        }
        if (parsed.event.type === 'session.info') {
          adoptModel(eventPayload);
          const activeSessionId = sessionIdRef.current;
          if (activeSessionId) void refreshReasoning(activeSessionId);
          if (isRecord(eventPayload)) {
            const title = typeof eventPayload.title === 'string' && eventPayload.title.trim() ? eventPayload.title.trim() : null;
            if (title) setSessionTitle(title);
          }
        }

        const clarifyToolContent = extractClarifyToolContent(parsed.event);
        if (clarifyToolContent) {
          pendingClarifyContentRef.current = clarifyToolContent;
          const activeInteraction = interactionRef.current;
          if (activeInteraction?.kind === 'clarify') {
            const enrichedInteraction = {
              ...activeInteraction,
              payload: mergeClarifyInteractionContent(activeInteraction.payload, clarifyToolContent),
            };
            interactionRef.current = enrichedInteraction;
            setInteraction(enrichedInteraction);
          }
        }

        const incomingInteraction = extractInteractionRequest(parsed.event);
        if (incomingInteraction) {
          const enrichedInteraction = incomingInteraction.kind === 'clarify'
            ? {
              ...incomingInteraction,
              payload: mergeClarifyInteractionContent(incomingInteraction.payload, pendingClarifyContentRef.current),
            }
            : incomingInteraction;
          interactionRef.current = enrichedInteraction;
          setInteraction(enrichedInteraction);
          setStatusText('Waiting for your input');
        }
        if (parsed.event.type.endsWith('.expire')) {
          const requestId = typeof parsed.event.payload?.request_id === 'string' ? parsed.event.payload.request_id : null;
          if (!requestId || requestId === interactionRef.current?.requestId) {
            pendingClarifyContentRef.current = null;
            setInteraction(null);
          }
        }

        const incomingActivity = eventActivity(parsed.event);
        if (incomingActivity) {
          setActivity(incomingActivity);
          if (incomingActivity.state === 'running') setRunning(true);
          if (incomingActivity.state === 'error') setRunning(false);
          if (parsed.event.type === 'status.update' && incomingActivity.state === 'complete') {
            setRunning(false);
            setCompleted(true);
          }
          if (activityTimerRef.current !== null) window.clearTimeout(activityTimerRef.current);
          if (incomingActivity.state === 'complete' || incomingActivity.state === 'error') {
            activityTimerRef.current = window.setTimeout(() => setActivity(null), 1800);
          }
        }
        const isTurnStarted = parsed.event.type === 'run.started' || parsed.event.type === 'message.start' || parsed.event.type === 'message.started' || parsed.event.type === 'response.started';
        const isTurnCompleted = parsed.event.type === 'message.complete'
          || parsed.event.type === 'message.completed'
          || parsed.event.type === 'message.done'
          || parsed.event.type === 'assistant.completed'
          || parsed.event.type === 'assistant.done'
          || parsed.event.type === 'response.completed'
          || parsed.event.type === 'response.done'
          || parsed.event.type === 'run.completed'
          || parsed.event.type === 'run.finished'
          || parsed.event.type === 'run.done';
        if (isTurnStarted) {
          setRunning(true);
          setCompleted(false);
        }
        if (isTurnCompleted || parsed.event.type === 'error') {
          setRunning(false);
          if (parsed.event.type !== 'error') {
            setCompleted(true);
            setActivity(null);
          }
        }
        if (parsed.event.type === 'run.completed') {
          const payload = parsed.event.payload ?? {};
          const usage = isRecord(payload.usage) ? payload.usage : null;
          const inputTokens = usage && typeof usage.input_tokens === 'number' ? usage.input_tokens : null;
          if (inputTokens != null) setContextTokens(inputTokens);
          const title = typeof payload.title === 'string' && payload.title.trim() ? payload.title.trim() : null;
          if (title) setSessionTitle(title);
          const activeSessionId = sessionIdRef.current;
          if (activeSessionId) {
            void refreshContext(activeSessionId);
            void refreshReasoning(activeSessionId);
          }
        }
        setMessages((current) => applyLiveGatewayEvent(current, parsed.event));
      });

      ws.addEventListener('open', () => {
        reconnectAttemptsRef.current = 0;
        setStatusText('Waiting for gateway');
        void readyPromise.then(() => {
          if (wsRef.current !== ws || intentionalCloseRef.current) return;
          setConnectionState('connected');
          setStatusText('Connected');
          // Auto-resume only for the generic chat (no specific session target).
          // When a session was picked from the list, stay in preview mode until
          // the user clicks "Resume session".
          if (previewModeRef.current) return;
          const eventReplay = prepareEventReplay();
          void ensureSession().then((activeSessionId) => finishEventReplay(eventReplay).then(() => {
            void refreshModel(activeSessionId);
            void refreshContext(activeSessionId);
            void replayPendingPrompt(activeSessionId);
          })).catch((err: unknown) => {
            void finishEventReplay(eventReplay);
            setError(err instanceof Error ? err.message : 'Failed to create or resume chat session.');
          });
        });
      });

      ws.addEventListener('close', (event) => {
        if (wsRef.current !== ws) return;
        wsRef.current = null;
        readyResolveRef.current?.();
        rejectPending('Chat gateway disconnected.');
        if (event.code === 4401) {
          setConnectionState('error');
          setStatusText('Authentication failed');
          setError('Chat WebSocket authentication failed. Unlock Mission Control again.');
          return;
        }
        if (event.code === 4403) {
          setConnectionState('error');
          setStatusText('Unavailable');
          setError('Chat WebSocket is disabled or rejected by the gateway boundary checks.');
          return;
        }
        scheduleReconnect();
      });

      ws.addEventListener('error', () => {
        setError('Chat WebSocket connection failed.');
      });
      releaseAttempt();
    } catch (err) {
      releaseAttempt();
      setConnectionState('error');
      setStatusText('Connection failed');
      setError(err instanceof Error ? err.message : 'Chat connection failed.');
    }
  }, [adoptModel, bootstrapPointer, ensureSession, finishEventReplay, initialSessionId, open, prepareEventReplay, refreshContext, refreshModel, refreshReasoning, rejectPending, replayPendingPrompt, scheduleReconnect, storedToken]);

  useEffect(() => {
    connectRef.current = connect;
  }, [connect]);

  useEffect(() => {
    if (!open) {
      intentionalCloseRef.current = true;
      connectionGenerationRef.current += 1;
      connectionAttemptGateRef.current.release();
      if (reconnectTimerRef.current !== null) window.clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = null;
      readyResolveRef.current?.();
      rejectPending('Chat drawer closed.');
      wsRef.current?.close();
      wsRef.current = null;
      setConnectionState('idle');
      setStatusText('Open chat to connect');
      return;
    }

    void connect();
    return () => {
      intentionalCloseRef.current = true;
      connectionGenerationRef.current += 1;
      connectionAttemptGateRef.current.release();
      if (reconnectTimerRef.current !== null) window.clearTimeout(reconnectTimerRef.current);
      if (readyTimerRef.current !== null) window.clearTimeout(readyTimerRef.current);
      readyResolveRef.current?.();
      rejectPending('Chat drawer closed.');
      wsRef.current?.close();
      wsRef.current = null;
    };
  }, [connect, open, rejectPending]);

  const appendChatMessage = useCallback((message: ChatMessage, kind: 'user_message' | 'system_message' = 'system_message') => {
    setMessages((current) => current.some((candidate) => candidate.id === message.id) ? current : [...current, message]);
    const activeSessionId = sessionIdRef.current;
    if (activeSessionId) void publishChatSync(storedToken, activeSessionId, kind, message as unknown as Record<string, unknown>);
  }, [storedToken]);

  const titleSession = useCallback(async (activeSessionId: string, title: string): Promise<void> => {
    const trimmed = title.trim().slice(0, 120);
    if (!activeSessionId.trim() || !trimmed) return;
    try {
      await request('session.title', { session_id: activeSessionId, title: trimmed });
      setSessionTitle(trimmed);
    } catch {
      // A title failure must not abort the Bot handoff; the UI still has a useful local title.
      setSessionTitle(trimmed);
    }
    void persistChatTitle(activeSessionId, sessionKeyRef.current, trimmed, storedToken);
  }, [request, storedToken]);

  const appendSystemMessage = useCallback((text: string) => {
    const trimmed = text.trim();
    if (!trimmed) return;
    const systemMessage: ChatMessage = {
      id: `system-${Date.now()}-${Math.random().toString(36).slice(2)}`,
      role: 'system',
      kind: 'system',
      source: 'live',
      text: trimmed,
      status: 'complete',
      createdAt: Date.now(),
    };
    setMessages((current) => [...current, systemMessage]);
    const activeSessionId = sessionIdRef.current;
    if (activeSessionId) {
      void publishChatSync(storedToken, activeSessionId, 'system_message', systemMessage as unknown as Record<string, unknown>);
    }
  }, [storedToken]);

  const submitAgentPrompt = useCallback(async (
    text: string,
    attachments: ChatAttachmentUpload[] = [],
    displayText = text,
  ): Promise<boolean> => {
    const trimmed = text.trim();
    if ((!trimmed && attachments.length === 0) || submitting) return false;
    setSubmitting(true);
    setError(null);
    try {
      let activeSessionId = await ensureSession();
      const canonicalSessionId = await claimLastChatPointer('submit', activeSessionId);
      if (canonicalSessionId !== activeSessionId) {
        activeSessionId = await ensureSession();
      }
      const attachmentRefs: string[] = [];
      for (const attachment of attachments) {
        const method = attachmentRpcMethod(attachment.kind);
        const params: Record<string, unknown> = { session_id: activeSessionId };
        if (attachment.kind === 'image') {
          params.content_base64 = attachment.dataUrl;
          params.filename = attachment.name;
        } else if (attachment.kind === 'pdf') {
          params.content_base64 = attachment.dataUrl;
          params.filename = attachment.name;
        } else {
          params.data_url = attachment.dataUrl;
          params.name = attachment.name;
          params.path = attachment.name;
        }
        const result = await request<unknown>(method, params);
        const reference = resultText(result);
        if (reference) attachmentRefs.push(reference);
      }

      const promptText = [trimmed, ...attachmentRefs].filter(Boolean).join('\n\n') || 'Please analyze the attached files.';
      const summaries = attachments.map(({ dataUrl: _dataUrl, ...summary }) => summary);
      const userMessageId = `user-${Date.now()}-${Math.random().toString(36).slice(2)}`;
      const pendingPrompt: PendingPrompt = {
        text: promptText,
        displayText: displayText.trim() || trimmed || 'Attached files',
        attachments: summaries,
        baselineUserCount: durableTranscriptRef.current.filter((message) => message.role === 'user').length,
        sessionKey: sessionKeyRef.current || activeSessionId,
        messageId: userMessageId,
      };
      pendingPromptRef.current = pendingPrompt;
      persistPendingChatSubmit(pendingPrompt);
      const userMessage: ChatMessage = {
        id: userMessageId,
        role: 'user',
        kind: 'user',
        source: 'live',
        text: displayText.trim() || trimmed || 'Attached files',
        attachments: summaries,
        status: 'complete',
        createdAt: Date.now(),
      };
      setMessages((current) => [...current, userMessage]);
      void publishChatSync(storedToken, activeSessionId, 'user_message', userMessage as unknown as Record<string, unknown>);
      setRunning(true);
      await request('prompt.submit', { session_id: activeSessionId, text: promptText });
      clearPendingPrompt();
      void refreshModel(activeSessionId);
      return true;
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Prompt submission failed.';
      const retryable = /disconnected|not connected|connection|timed out|closed|send chat request/i.test(message);
      if (!retryable) clearPendingPrompt();
      setRunning(false);
      setError(retryable ? 'Message queued until the chat connection is restored.' : message);
      return false;
    } finally {
      setSubmitting(false);
    }
  }, [claimLastChatPointer, clearPendingPrompt, ensureSession, refreshModel, request, submitting]);

  const closeModelPicker = useCallback(() => {
    setModelPickerOpen(false);
    setModelPickerRefresh(false);
  }, []);

  const switchModel = useCallback(async (
    model: string,
    provider = '',
    confirmExpensiveModel = false,
  ): Promise<ChatModelSwitchResult> => {
    const selectedModel = model.trim();
    if (!selectedModel) return { ok: false, error: 'Model value required.' };
    try {
      const activeSessionId = await ensureSession();
      const value = provider.trim()
        ? `${selectedModel} --provider ${provider.trim()} --session`
        : selectedModel;
      const raw = await request<Record<string, unknown>>('config.set', {
        key: 'model',
        value,
        session_id: activeSessionId,
        ...(confirmExpensiveModel ? { confirm_expensive_model: true } : {}),
      });
      const result = raw && typeof raw === 'object' ? raw : {};
      if (result.confirm_required === true) {
        return {
          ok: false,
          confirmRequired: true,
          confirmMessage: typeof result.confirm_message === 'string' ? result.confirm_message : undefined,
          warning: typeof result.warning === 'string' ? result.warning : undefined,
        };
      }
      const identity = extractSessionModel(raw) ?? { model: selectedModel, ...(provider.trim() ? { provider: provider.trim() } : {}) };
      setModelIdentity((current) => ({ ...current, ...identity }));
      const deferred = result.deferred === true;
      appendSystemMessage(`${deferred ? 'Model queued for next turn' : 'Model switched'}: ${identity.model}${identity.provider ? ` · ${identity.provider}` : ''}`);
      setModelPickerOpen(false);
      setModelPickerRefresh(false);
      await refreshModel(activeSessionId);
      return { ok: true, warning: typeof result.warning === 'string' ? result.warning : undefined };
    } catch (err) {
      return { ok: false, error: err instanceof Error ? err.message : 'Could not switch model.' };
    }
  }, [appendSystemMessage, ensureSession, refreshModel, request]);

  const executeSlashCommand = useCallback(async (command: string, depth = 0): Promise<boolean> => {
    const parsed = parseSlash(command);
    if (!parsed.name) {
      appendSystemMessage('Empty slash command. Type / to see available commands.');
      return false;
    }
    if (depth > 5) {
      appendSystemMessage(`/${parsed.name}: alias chain exceeded the safety limit.`);
      return false;
    }

    const handleDispatch = async (
      dispatch: GatewayCommandDispatch,
      activeSessionId: string,
    ): Promise<boolean> => {
      if (dispatch.type === 'alias') {
        return executeSlashCommand(`/${dispatch.target}${parsed.arg ? ` ${parsed.arg}` : ''}`, depth + 1);
      }
      if (dispatch.type === 'prefill') {
        if (dispatch.notice) appendSystemMessage(dispatch.notice);
        setCommandPrefill(dispatch.message);
        return true;
      }
      if (dispatch.type === 'send' || dispatch.type === 'skill') {
        if (dispatch.notice) appendSystemMessage(dispatch.notice);
        if (dispatch.type === 'skill') appendSystemMessage(`⚡ loading skill: ${dispatch.name}`);
        if (!dispatch.message?.trim()) throw new Error(`/${parsed.name}: command returned an empty prompt.`);
        return submitAgentPrompt(dispatch.message, [], dispatch.display || command);
      }
      const output = [dispatch.warning, dispatch.output]
        .filter((value): value is string => Boolean(value?.trim()))
        .join('\n\n');
      appendSystemMessage(output || `/${parsed.name}: no output`);
      await refreshModelAfterCommandDispatch(dispatch, activeSessionId, refreshModel);
      return true;
    };

    setSubmitting(true);
    setError(null);
    try {
      const activeSessionId = await ensureSession();
      if (parsed.name.toLowerCase() === 'model') {
        if (!parsed.arg || parsed.arg === '--refresh') {
          setModelPickerRefresh(parsed.arg === '--refresh');
          setModelPickerOpen(true);
          return true;
        }
        const switched = await switchModel(parsed.arg);
        if (!switched.ok) throw new Error(switched.error || 'Could not switch model.');
        return true;
      }
      if (parsed.name.toLowerCase() === 'reasoning' && parsed.arg) {
        const reasoning = await executeReasoningSlashCommand(parsed.arg, activeSessionId, request);
        if (reasoning) {
          if (reasoning.updatesEffort) {
            setModelIdentity((current) => current ? { ...current, reasoningEffort: reasoning.value } : current);
            const scopeLabel = reasoning.scope === 'global' ? 'saved globally' : 'session-scoped';
            appendSystemMessage(`✓ Reasoning effort set to '${reasoning.value}' (${scopeLabel})`);
          } else {
            appendSystemMessage(`✓ Reasoning display: ${reasoning.value}`);
          }
          await refreshModel(activeSessionId);
          return true;
        }
      }
      const normalizedCommand = command.trim().replace(/^\/+/, '');
      try {
        const result = await request<unknown>('slash.exec', {
          command: normalizedCommand,
          session_id: activeSessionId,
        });
        const dispatch = parseCommandDispatch(result);
        if (dispatch) return await handleDispatch(dispatch, activeSessionId);
        const output = commandOutput(result) || `/${parsed.name}: no output`;
        appendSystemMessage(output);
        adoptModel(result);
        await refreshModel(activeSessionId);
        return true;
      } catch {
        // Commands that need client-side behavior use the typed fallback below.
      }

      const dispatch = parseCommandDispatch(await request<unknown>('command.dispatch', {
        name: parsed.name,
        arg: parsed.arg,
        session_id: activeSessionId,
      }));
      if (!dispatch) throw new Error(`/${parsed.name}: invalid command response.`);
      return await handleDispatch(dispatch, activeSessionId);
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Slash command failed.';
      appendSystemMessage(`/${parsed.name}: ${message}`);
      setError(message);
      return false;
    } finally {
      setSubmitting(false);
    }
  }, [adoptModel, appendSystemMessage, ensureSession, refreshModel, request, submitAgentPrompt, switchModel]);

  const submitPrompt = useCallback(async (text: string, attachments: ChatAttachmentUpload[] = []): Promise<boolean> => {
    if (text.trim().startsWith('/') && attachments.length === 0) {
      return executeSlashCommand(text.trim());
    }
    return submitAgentPrompt(text, attachments);
  }, [executeSlashCommand, submitAgentPrompt]);

  const completeSlash = useCallback(async (text: string): Promise<ChatSlashCompletionResponse> => {
    const response = await request<ChatSlashCompletionResponse>('complete.slash', { text });
    const token = text.trim().slice(1).split(/\s+/)[0]?.toLowerCase() ?? '';
    const localItems = !text.trim().includes(' ') && 'model'.startsWith(token)
      ? [{ display: '/model', text: '/model', meta: 'Choose the active model' }]
      : [];
    const existing = Array.isArray(response.items) ? response.items : [];
    const items = [...localItems, ...existing.filter((item) => !localItems.some((local) => local.text === item.text))];
    return { ...response, items, replace_from: typeof response.replace_from === 'number' ? response.replace_from : 1 };
  }, [request]);

  const clearCommandPrefill = useCallback(() => setCommandPrefill(null), []);

  const respondInteraction = useCallback(async (answer: string, choice?: string, resolveAll = false) => {
    const pending = interactionRef.current;
    if (!pending) return false;
    try {
      if (pending.kind === 'approval') {
        await request('approval.respond', {
          choice: choice || answer || 'deny',
          all: resolveAll || choice === 'always',
          session_id: sessionIdRef.current ?? undefined,
        });
      } else if (pending.kind === 'clarify') {
        if (!pending.requestId) throw new Error('Clarify request is missing its request id.');
        const questionId = normalizeClarifyInteraction(pending.payload).questionId;
        await request('clarify.respond', {
          request_id: pending.requestId,
          answer,
          ...(questionId ? { question_id: questionId } : {}),
        });
      } else if (pending.kind === 'sudo') {
        if (!pending.requestId) throw new Error('Sudo request is missing its request id.');
        await request('sudo.respond', { request_id: pending.requestId, password: answer });
      } else if (pending.kind === 'terminal_read') {
        if (!pending.requestId) throw new Error('Terminal read request is missing its request id.');
        await request('terminal.read.respond', { request_id: pending.requestId, text: answer });
      } else {
        if (!pending.requestId) throw new Error('Secret request is missing its request id.');
        await request('secret.respond', { request_id: pending.requestId, value: answer });
      }
      pendingClarifyContentRef.current = null;
      setInteraction(null);
      setStatusText('Connected');
      return true;
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not answer Hermes.');
      return false;
    }
  }, [request]);

  const interrupt = useCallback(async () => {
    if (!sessionIdRef.current) return;
    try {
      await request('session.interrupt', { session_id: sessionIdRef.current });
      setRunning(false);
      setActivity(null);
      setMessages((current) => {
        const next = [...current];
        const last = next.at(-1);
        if (last?.role === 'assistant' && last.status === 'streaming') {
          next[next.length - 1] = { ...last, status: 'interrupted' };
        }
        return next;
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Interrupt failed.');
    }
  }, [request]);

  const reset = useCallback(async () => {
    const previousSessionId = sessionIdRef.current;
    const recoveredSessionId = requestedSessionIdRef.current;
    if (
      shouldCloseBackendSessionForNewChat()
      && previousSessionId
      && previousSessionId !== recoveredSessionId
    ) {
      try {
        await request('session.close', { session_id: previousSessionId });
      } catch {
        // A completed session may already have been finalized by the gateway.
      }
    }
    setMessages([]);
    setTodoPlan(null);
    transcriptReadyRef.current = false;
    setSessionId(null);
    setSessionKey(null);
    requestedSessionIdRef.current = null;
    setModelIdentity(null);
    setModelPickerOpen(false);
    setModelPickerRefresh(false);
    setCommandPrefill(null);
    sessionIdRef.current = null;
    sessionKeyRef.current = null;
    setInteraction(null);
    setActivity(null);
    clearPendingPrompt();
    persistChat(null, null, null, [], pointerRevision);
    try {
      let activeSessionId = await ensureSession();
      if (activeSessionId) {
        const canonicalSessionId = await claimLastChatPointer('create', activeSessionId);
        if (canonicalSessionId !== activeSessionId) activeSessionId = await ensureSession();
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create a new chat.');
    }
  }, [claimLastChatPointer, clearPendingPrompt, ensureSession, pointerRevision, request]);

  return {
    messages,
    todoPlan,
    sessionId,
    sessionKey,
    connectionState,
    statusText,
    error,
    submitting,
    running,
    activity,
    interaction,
    previewMode,
    modelIdentity,
    contextTokens,
    contextMax,
    sessionTitle,
    modelPickerOpen,
    modelPickerRefresh,
    request,
    ensureSession,
    claimLastChatPointer,
    switchModel,
    closeModelPicker,
    commandPrefill,
    connect,
    resumeSession,
    completeSlash,
    clearCommandPrefill,
    submitPrompt,
    appendChatMessage,
    titleSession,
    appendSystemMessage,
    respondInteraction,
    interrupt,
    reset,
  };
}
