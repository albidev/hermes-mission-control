import {
  type ClipboardEvent,
  type DragEvent,
  type FormEvent,
  type KeyboardEvent,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import {
  applyGatewayEvent,
  attachmentRpcMethod,
  classifyAttachment,
  createRpcRequest,
  eventActivity,
  extractInflightAssistant,
  extractInjectedSessionToken,
  extractInteractionRequest,
  extractSessionId,
  extractSessionKey,
  extractSessionModel,
  extractTranscript,
  getRpcErrorMessage,
  isResponseFor,
  parseCommandDispatch,
  parseSlash,
  nextReconnectDelay,
  normalizeTranscript,
  parseGatewayFrame,
  type AttachmentKind,
  type ChatActivity,
  type ChatAttachmentSummary,
  type ChatAttachmentUpload,
  type ChatMessage,
  type ChatModelIdentity,
  type ChatModelSwitchResult,
  type GatewayCommandDispatch,
  type GatewayInteractionRequest,
} from './chat-protocol';
import type { ChatSlashCompletionResponse } from '../components/ChatSlashPopover';

type ConnectionState = 'idle' | 'ticket' | 'connecting' | 'connected' | 'reconnecting' | 'error';

type PersistedChat = {
  sessionId: string | null;
  sessionKey: string | null;
  modelIdentity: ChatModelIdentity | null;
  messages: ChatMessage[];
  updatedAt: number;
};

type PendingRpc = {
  id: string;
  method: string;
  resolve: (value: unknown) => void;
  reject: (error: Error) => void;
  timeout: number;
};

export type PendingAttachment = Omit<ChatAttachmentSummary, 'id'> & {
  id: string;
  file: File;
  previewUrl: string | null;
};

const STORAGE_KEY = 'mission-control-chat-drawer-v1';
const RPC_TIMEOUT_MS = 120000;
const MAX_RECONNECTS = 6;
export const MAX_ATTACHMENTS = 5;
export const MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024;
export { classifyAttachment } from './chat-protocol';

function readPersistedChat(): PersistedChat {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return { sessionId: null, sessionKey: null, modelIdentity: null, messages: [], updatedAt: 0 };
    const parsed = JSON.parse(raw) as Partial<PersistedChat>;
    return {
      sessionId: typeof parsed.sessionId === 'string' ? parsed.sessionId : null,
      sessionKey: typeof parsed.sessionKey === 'string' ? parsed.sessionKey : null,
      modelIdentity: extractSessionModel(parsed.modelIdentity),
      messages: Array.isArray(parsed.messages) ? (parsed.messages as ChatMessage[]) : [],
      updatedAt: typeof parsed.updatedAt === 'number' ? parsed.updatedAt : 0,
    };
  } catch {
    return { sessionId: null, sessionKey: null, modelIdentity: null, messages: [], updatedAt: 0 };
  }
}

function persistChat(sessionId: string | null, sessionKey: string | null, modelIdentity: ChatModelIdentity | null, messages: ChatMessage[]) {
  try {
    window.localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({ sessionId, sessionKey, modelIdentity, messages: messages.slice(-200), updatedAt: Date.now() }),
    );
  } catch {
    // Storage is best effort; the live session remains authoritative.
  }
}

function getWebSocketUrl(ticketOrToken: { kind: 'ticket' | 'token'; value: string }) {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const url = new URL('/api/ws', `${protocol}//${window.location.host}`);
  url.searchParams.set(ticketOrToken.kind, ticketOrToken.value);
  return url.toString();
}

async function readLoopbackSessionToken(): Promise<string | null> {
  try {
    const response = await fetch('/api/gateway-root', { credentials: 'include', cache: 'no-store' });
    if (!response.ok) return null;
    return extractInjectedSessionToken(await response.text());
  } catch {
    return null;
  }
}

async function requestWsTicket(headers: Record<string, string> = {}): Promise<Response> {
  return fetch('/api/auth/ws-ticket', {
    method: 'POST',
    headers: { Accept: 'application/json', ...headers },
    credentials: 'include',
    cache: 'no-store',
  });
}

async function readTicketResponse(response: Response): Promise<{ kind: 'ticket'; value: string }> {
  const payload = (await response.json()) as { ticket?: unknown };
  if (typeof payload.ticket === 'string' && payload.ticket) {
    return { kind: 'ticket', value: payload.ticket };
  }
  throw new Error('The gateway returned an invalid WebSocket ticket.');
}

async function mintWsCredential(accessToken: string): Promise<{ kind: 'ticket' | 'token'; value: string }> {
  // Mission Control is served by the same Hermes dashboard host. Prefer the
  // injected dashboard session token: it works through localhost, Tailscale,
  // and the HTTPS reverse proxy without a separate cookie-bound ticket flow.
  const loopbackToken = await readLoopbackSessionToken();
  if (loopbackToken) return { kind: 'token', value: loopbackToken };

  // Keep the short-lived ticket as a compatibility fallback for deployments
  // where the dashboard-root proxy is unavailable.
  let response = await requestWsTicket();
  if (response.ok) return readTicketResponse(response);

  let lastStatus = response.status;
  if (accessToken.trim()) {
    response = await requestWsTicket({ Authorization: `Bearer ${accessToken.trim()}` });
    if (response.ok) return readTicketResponse(response);
    lastStatus = response.status;
  }

  if (lastStatus === 401) {
    throw new Error('Chat WebSocket authentication failed. Authenticate the Hermes gateway or unlock Mission Control first.');
  }
  if (lastStatus === 403 || lastStatus === 404) {
    throw new Error('Chat WebSocket is not available on this gateway.');
  }
  throw new Error(`Chat WebSocket ticket request failed with HTTP ${lastStatus}.`);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

function resultText(value: unknown): string {
  if (!isRecord(value)) return '';
  for (const key of ['ref_text', 'text', 'message']) {
    if (typeof value[key] === 'string' && value[key].trim()) return value[key].trim();
  }
  return '';
}

function commandOutput(value: unknown): string {
  if (!isRecord(value)) return '';
  for (const key of ['output', 'display', 'message', 'notice', 'warning']) {
    if (typeof value[key] === 'string' && value[key].trim()) return value[key].trim();
  }
  return '';
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(bytes > 10 * 1024 * 1024 ? 0 : 1)} MB`;
}

export function readFileAsDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(typeof reader.result === 'string' ? reader.result : '');
    reader.onerror = () => reject(reader.error ?? new Error(`Could not read ${file.name}.`));
    reader.readAsDataURL(file);
  });
}

export function interactionTitle(interaction: GatewayInteractionRequest): string {
  if (interaction.kind === 'approval') return 'Hermes needs permission';
  if (interaction.kind === 'clarify') return 'Hermes needs your answer';
  if (interaction.kind === 'sudo') return 'Hermes needs elevated access';
  if (interaction.kind === 'terminal_read') return 'Hermes needs terminal output';
  return 'Hermes needs a secret';
}

export function useGatewayChat(storedToken: string, open: boolean, initialSessionId?: string | null) {
  const initial = useMemo(readPersistedChat, []);
  const [messages, setMessages] = useState<ChatMessage[]>(initial.messages);
  const [sessionId, setSessionId] = useState<string | null>(initial.sessionId);
  const [sessionKey, setSessionKey] = useState<string | null>(initial.sessionKey);
  const [connectionState, setConnectionState] = useState<ConnectionState>('idle');
  const [statusText, setStatusText] = useState('Disconnected');
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [running, setRunning] = useState(false);
  const [activity, setActivity] = useState<ChatActivity | null>(null);
  const [interaction, setInteraction] = useState<GatewayInteractionRequest | null>(null);
  const [modelIdentity, setModelIdentity] = useState<ChatModelIdentity | null>(initial.modelIdentity);
  const [modelPickerOpen, setModelPickerOpen] = useState(false);
  const [modelPickerRefresh, setModelPickerRefresh] = useState(false);
  const [commandPrefill, setCommandPrefill] = useState<string | null>(null);
  const [previewMode, setPreviewMode] = useState<boolean>(Boolean(initialSessionId?.trim()));
  const wsRef = useRef<WebSocket | null>(null);
  const pendingRef = useRef(new Map<string, PendingRpc>());
  const requestSeqRef = useRef(0);
  const reconnectAttemptsRef = useRef(0);
  const reconnectTimerRef = useRef<number | null>(null);
  const activityTimerRef = useRef<number | null>(null);
  const readyTimerRef = useRef<number | null>(null);
  const sessionIdRef = useRef(sessionId);
  const sessionKeyRef = useRef(sessionKey);
  const interactionRef = useRef(interaction);
  const intentionalCloseRef = useRef(false);
  const requestedSessionIdRef = useRef<string | null>(initialSessionId ?? null);
  const previewModeRef = useRef<boolean>(Boolean(initialSessionId?.trim()));
  const connectRef = useRef<() => Promise<void>>(async () => {});
  const readyResolveRef = useRef<(() => void) | null>(null);

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
    if (!requested) return;
    setSessionId(requested);
    sessionIdRef.current = requested;
    setSessionKey(requested);
    sessionKeyRef.current = requested;
    setMessages([]);
    setModelIdentity(null);
    setModelPickerOpen(false);
    setModelPickerRefresh(false);
    setInteraction(null);
    setActivity(null);
  }, [initialSessionId]);

  useEffect(() => {
    persistChat(sessionId, sessionKey, modelIdentity, messages);
  }, [messages, modelIdentity, sessionId, sessionKey]);

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

  const adoptModel = useCallback((result: unknown) => {
    const next = extractSessionModel(result);
    if (next) setModelIdentity(next);
    return next;
  }, []);

  const refreshModel = useCallback(async (activeSessionId: string) => {
    try {
      adoptModel(await request<unknown>('session.status', { session_id: activeSessionId }));
    } catch {
      // The create/resume payload already carries the model; status is a best-effort refresh.
    }
  }, [adoptModel, request]);

  const ensureSession = useCallback(async () => {
    const existingKey = requestedSessionIdRef.current || sessionKeyRef.current || sessionIdRef.current;
    if (existingKey) {
      try {
        const resumed = await request<unknown>('session.resume', {
          session_id: existingKey,
          cols: 80,
          eager_build: true,
          source: 'mission-control',
        });
        adoptModel(resumed);
        const resolvedSessionId = extractSessionId(resumed) ?? sessionIdRef.current ?? existingKey;
        const resolvedSessionKey = extractSessionKey(resumed) ?? existingKey;
        setSessionId(resolvedSessionId);
        sessionIdRef.current = resolvedSessionId;
        setSessionKey(resolvedSessionKey);
        sessionKeyRef.current = resolvedSessionKey;
        const transcript = normalizeTranscript(extractTranscript(resumed));
        const inflight = extractInflightAssistant(resumed);
        if (transcript.length > 0 || inflight) {
          setMessages(
            inflight
              ? [...transcript, { id: `inflight-${Date.now()}`, role: 'assistant', text: inflight, status: 'streaming', createdAt: Date.now() }]
              : transcript,
          );
        }
        setRunning(Boolean(inflight));
        return resolvedSessionId;
      } catch {
        if (requestedSessionIdRef.current === existingKey) {
          throw new Error(`Session ${existingKey} could not be recovered from the gateway.`);
        }
        setStatusText('Stored session could not be resumed; creating a new chat.');
      }
    }

    const created = await request<unknown>('session.create', { cols: 80, source: 'mission-control' });
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
  }, [request, adoptModel]);

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
      const activeSessionId = await ensureSession();
      if (activeSessionId) void refreshModel(activeSessionId);
      return activeSessionId ?? null;
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to resume the selected session.');
      return null;
    }
  }, [ensureSession, refreshModel]);

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
    if (wsRef.current?.readyState === WebSocket.OPEN || wsRef.current?.readyState === WebSocket.CONNECTING) return;
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
      if (!open || intentionalCloseRef.current) return;
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
        if (parsed.event.type === 'session.info') {
          adoptModel(eventPayload);
        }

        const incomingInteraction = extractInteractionRequest(parsed.event);
        if (incomingInteraction) {
          setInteraction(incomingInteraction);
          setStatusText('Waiting for your input');
        }
        if (parsed.event.type.endsWith('.expire')) {
          const requestId = typeof parsed.event.payload?.request_id === 'string' ? parsed.event.payload.request_id : null;
          if (!requestId || requestId === interactionRef.current?.requestId) setInteraction(null);
        }

        const incomingActivity = eventActivity(parsed.event);
        if (incomingActivity) {
          setActivity(incomingActivity);
          if (activityTimerRef.current !== null) window.clearTimeout(activityTimerRef.current);
          if (incomingActivity.state === 'complete' || incomingActivity.state === 'error') {
            activityTimerRef.current = window.setTimeout(() => setActivity(null), 1800);
          }
        }
        if (parsed.event.type === 'message.start') setRunning(true);
        if (parsed.event.type === 'message.complete' || parsed.event.type === 'error') {
          setRunning(false);
          if (parsed.event.type === 'message.complete') setActivity(null);
        }
        setMessages((current) => applyGatewayEvent(current, parsed.event));
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
          void ensureSession().then((activeSessionId) => refreshModel(activeSessionId)).catch((err: unknown) => {
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
    } catch (err) {
      setConnectionState('error');
      setStatusText('Connection failed');
      setError(err instanceof Error ? err.message : 'Chat connection failed.');
    }
  }, [adoptModel, ensureSession, open, refreshModel, rejectPending, scheduleReconnect, storedToken]);

  useEffect(() => {
    connectRef.current = connect;
  }, [connect]);

  useEffect(() => {
    if (!open) {
      intentionalCloseRef.current = true;
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
      if (reconnectTimerRef.current !== null) window.clearTimeout(reconnectTimerRef.current);
      if (readyTimerRef.current !== null) window.clearTimeout(readyTimerRef.current);
      readyResolveRef.current?.();
      rejectPending('Chat drawer closed.');
      wsRef.current?.close();
      wsRef.current = null;
    };
  }, [connect, open, rejectPending]);

  const appendSystemMessage = useCallback((text: string) => {
    const trimmed = text.trim();
    if (!trimmed) return;
    setMessages((current) => [
      ...current,
      {
        id: `system-${Date.now()}-${Math.random().toString(36).slice(2)}`,
        role: 'system',
        text: trimmed,
        status: 'complete',
        createdAt: Date.now(),
      },
    ]);
  }, []);

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
      const activeSessionId = await ensureSession();
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
      setMessages((current) => [
        ...current,
        {
          id: `user-${Date.now()}`,
          role: 'user',
          text: displayText.trim() || trimmed || 'Attached files',
          attachments: summaries,
          status: 'complete',
          createdAt: Date.now(),
        },
      ]);
      setRunning(true);
      await request('prompt.submit', { session_id: activeSessionId, text: promptText });
      void refreshModel(activeSessionId);
      return true;
    } catch (err) {
      setRunning(false);
      setError(err instanceof Error ? err.message : 'Prompt submission failed.');
      return false;
    } finally {
      setSubmitting(false);
    }
  }, [ensureSession, refreshModel, request, submitting]);

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
      setModelIdentity(identity);
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

    const handleDispatch = async (dispatch: GatewayCommandDispatch): Promise<boolean> => {
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
      const normalizedCommand = command.trim().replace(/^\/+/, '');
      try {
        const result = await request<unknown>('slash.exec', {
          command: normalizedCommand,
          session_id: activeSessionId,
        });
        const dispatch = parseCommandDispatch(result);
        if (dispatch) return handleDispatch(dispatch);
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
      return handleDispatch(dispatch);
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
        await request('clarify.respond', { request_id: pending.requestId, answer });
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
    // Replicate the classic `/new` flow. The gateway's own `/new` resets the
    // session IN-PLACE (reset_session on the same key) without ever emitting a
    // `session.close` RPC — a close triggers the full teardown path
    // (_finalize_session + agent.close()), which is what leaves the shared
    // state.db handle with `_conn = None` and makes the very next turn crash
    // with `'NoneType' object has no attribute 'execute'`. So here we do the
    // same: abandon the old session (the gateway reclaims it on idle-timeout)
    // and mint a fresh one, instead of closing the old one first.
    setMessages([]);
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
    persistChat(null, null, null, []);
    try {
      await ensureSession();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create a new chat.');
    }
  }, [ensureSession]);

  return {
    messages,
    sessionId,
    connectionState,
    statusText,
    error,
    submitting,
    running,
    activity,
    interaction,
    previewMode,
    modelIdentity,
    modelPickerOpen,
    modelPickerRefresh,
    request,
    switchModel,
    closeModelPicker,
    commandPrefill,
    connect,
    resumeSession,
    completeSlash,
    clearCommandPrefill,
    submitPrompt,
    respondInteraction,
    interrupt,
    reset,
  };
}
