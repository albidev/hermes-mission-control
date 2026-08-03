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
  Bot,
  Check,
  Circle,
  FileText,
  Image as ImageIcon,
  KeyRound,
  Loader2,
  MessageSquare,
  Paperclip,
  Pause,
  Send,
  ShieldCheck,
  Trash2,
  X,
} from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
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
  extractTranscript,
  getRpcErrorMessage,
  isResponseFor,
  nextReconnectDelay,
  normalizeTranscript,
  parseGatewayFrame,
  previewText,
  type AttachmentKind,
  type ChatActivity,
  type ChatAttachmentSummary,
  type ChatAttachmentUpload,
  type ChatMessage,
  type GatewayInteractionRequest,
} from '../lib/chat-protocol';

type ChatDrawerProps = {
  open: boolean;
  storedToken: string;
  onClose: () => void;
};

type ConnectionState = 'idle' | 'ticket' | 'connecting' | 'connected' | 'reconnecting' | 'error';

type PersistedChat = {
  sessionId: string | null;
  sessionKey: string | null;
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

type PendingAttachment = Omit<ChatAttachmentSummary, 'id'> & {
  id: string;
  file: File;
  previewUrl: string | null;
};

const STORAGE_KEY = 'mission-control-chat-drawer-v1';
const RPC_TIMEOUT_MS = 120000;
const MAX_RECONNECTS = 6;
const MAX_ATTACHMENTS = 5;
const MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024;

function readPersistedChat(): PersistedChat {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return { sessionId: null, sessionKey: null, messages: [], updatedAt: 0 };
    const parsed = JSON.parse(raw) as Partial<PersistedChat>;
    return {
      sessionId: typeof parsed.sessionId === 'string' ? parsed.sessionId : null,
      sessionKey: typeof parsed.sessionKey === 'string' ? parsed.sessionKey : null,
      messages: Array.isArray(parsed.messages) ? (parsed.messages as ChatMessage[]) : [],
      updatedAt: typeof parsed.updatedAt === 'number' ? parsed.updatedAt : 0,
    };
  } catch {
    return { sessionId: null, sessionKey: null, messages: [], updatedAt: 0 };
  }
}

function persistChat(sessionId: string | null, sessionKey: string | null, messages: ChatMessage[]) {
  try {
    window.localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({ sessionId, sessionKey, messages: messages.slice(-200), updatedAt: Date.now() }),
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

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(bytes > 10 * 1024 * 1024 ? 0 : 1)} MB`;
}

function readFileAsDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(typeof reader.result === 'string' ? reader.result : '');
    reader.onerror = () => reject(reader.error ?? new Error(`Could not read ${file.name}.`));
    reader.readAsDataURL(file);
  });
}

function interactionTitle(interaction: GatewayInteractionRequest): string {
  if (interaction.kind === 'approval') return 'Hermes needs permission';
  if (interaction.kind === 'clarify') return 'Hermes needs your answer';
  if (interaction.kind === 'sudo') return 'Hermes needs elevated access';
  return 'Hermes needs a secret';
}

function useGatewayChat(storedToken: string, open: boolean) {
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
    persistChat(sessionId, sessionKey, messages);
  }, [messages, sessionId, sessionKey]);

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

  const ensureSession = useCallback(async () => {
    const existingKey = sessionKeyRef.current || sessionIdRef.current;
    if (existingKey) {
      try {
        const resumed = await request<unknown>('session.resume', {
          session_id: existingKey,
          cols: 80,
          eager_build: true,
          source: 'mission-control',
        });
        const resolvedSessionId = extractSessionId(resumed) ?? sessionIdRef.current ?? existingKey;
        const resolvedSessionKey = extractSessionKey(resumed) ?? existingKey;
        setSessionId(resolvedSessionId);
        sessionIdRef.current = resolvedSessionId;
        setSessionKey(resolvedSessionKey);
        sessionKeyRef.current = resolvedSessionKey;
        const transcript = normalizeTranscript(extractTranscript(resumed));
        const inflight = extractInflightAssistant(resumed);
        if (transcript.length > 0) {
          setMessages(
            inflight
              ? [...transcript, { id: `inflight-${Date.now()}`, role: 'assistant', text: inflight, status: 'streaming', createdAt: Date.now() }]
              : transcript,
          );
        }
        setRunning(Boolean(inflight));
        return resolvedSessionId;
      } catch {
        setStatusText('Stored session could not be resumed; creating a new chat.');
      }
    }

    const created = await request<unknown>('session.create', { cols: 80, source: 'mission-control' });
    const createdSessionId = extractSessionId(created);
    if (!createdSessionId) throw new Error('Gateway did not return a session id.');
    const createdSessionKey = extractSessionKey(created);
    setSessionId(createdSessionId);
    sessionIdRef.current = createdSessionId;
    setSessionKey(createdSessionKey);
    sessionKeyRef.current = createdSessionKey;
    setRunning(false);
    return createdSessionId;
  }, [request]);

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
        if (parsed.event.session_id && sessionIdRef.current && parsed.event.session_id !== sessionIdRef.current) return;

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
          void ensureSession().catch((err: unknown) => {
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
  }, [ensureSession, open, rejectPending, scheduleReconnect, storedToken]);

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

  const submitPrompt = useCallback(async (text: string, attachments: ChatAttachmentUpload[] = []): Promise<boolean> => {
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
          text: trimmed || 'Attached files',
          attachments: summaries,
          status: 'complete',
          createdAt: Date.now(),
        },
      ]);
      setRunning(true);
      await request('prompt.submit', { session_id: activeSessionId, text: promptText });
      return true;
    } catch (err) {
      setRunning(false);
      setError(err instanceof Error ? err.message : 'Prompt submission failed.');
      return false;
    } finally {
      setSubmitting(false);
    }
  }, [ensureSession, request, submitting]);

  const respondInteraction = useCallback(async (answer: string, choice?: string) => {
    const pending = interactionRef.current;
    if (!pending) return false;
    try {
      if (pending.kind === 'approval') {
        await request('approval.respond', {
          choice: choice || answer || 'deny',
          session_id: sessionIdRef.current ?? undefined,
        });
      } else if (pending.kind === 'clarify') {
        if (!pending.requestId) throw new Error('Clarify request is missing its request id.');
        await request('clarify.respond', { request_id: pending.requestId, answer });
      } else if (pending.kind === 'sudo') {
        if (!pending.requestId) throw new Error('Sudo request is missing its request id.');
        await request('sudo.respond', { request_id: pending.requestId, password: answer });
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
    const previousSessionId = sessionIdRef.current;
    if (previousSessionId) {
      try {
        await request('session.close', { session_id: previousSessionId });
      } catch {
        // A completed session may already have been finalized by the gateway.
      }
    }
    setMessages([]);
    setSessionId(null);
    setSessionKey(null);
    sessionIdRef.current = null;
    sessionKeyRef.current = null;
    setInteraction(null);
    setActivity(null);
    persistChat(null, null, []);
    try {
      await ensureSession();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create a new chat.');
    }
  }, [ensureSession, request]);

  return {
    messages,
    connectionState,
    statusText,
    error,
    submitting,
    running,
    activity,
    interaction,
    connect,
    submitPrompt,
    respondInteraction,
    interrupt,
    reset,
  };
}

function AttachmentIcon({ kind }: { kind: AttachmentKind }) {
  if (kind === 'image') return <ImageIcon size={15} aria-hidden />;
  return <FileText size={15} aria-hidden />;
}

export function ChatDrawer({ open, storedToken, onClose }: ChatDrawerProps) {
  const [draft, setDraft] = useState('');
  const [pendingAttachments, setPendingAttachments] = useState<PendingAttachment[]>([]);
  const [attachmentNotice, setAttachmentNotice] = useState<string | null>(null);
  const [isDragging, setIsDragging] = useState(false);
  const [interactionDraft, setInteractionDraft] = useState('');
  const [selectedChoices, setSelectedChoices] = useState<string[]>([]);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const pendingRef = useRef<PendingAttachment[]>([]);
  const {
    messages,
    connectionState,
    statusText,
    error,
    submitting,
    running,
    activity,
    interaction,
    connect,
    submitPrompt,
    respondInteraction,
    interrupt,
    reset,
  } = useGatewayChat(storedToken, open);

  useEffect(() => {
    pendingRef.current = pendingAttachments;
  }, [pendingAttachments]);

  useEffect(() => {
    return () => {
      for (const attachment of pendingRef.current) {
        if (attachment.previewUrl) URL.revokeObjectURL(attachment.previewUrl);
      }
    };
  }, []);

  useEffect(() => {
    if (!open) return;
    const raf = window.requestAnimationFrame(() => textareaRef.current?.focus());
    return () => window.cancelAnimationFrame(raf);
  }, [open]);

  useEffect(() => {
    if (!open) return;
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' });
  }, [messages, interaction, activity, open]);

  useEffect(() => {
    setInteractionDraft('');
    setSelectedChoices([]);
  }, [interaction?.requestId, interaction?.kind]);

  const statusClass = connectionState === 'connected'
    ? 'is-online'
    : connectionState === 'reconnecting' || connectionState === 'connecting' || connectionState === 'ticket'
      ? 'is-pending'
      : 'is-offline';
  const lastPreview = messages.length ? previewText(messages[messages.length - 1].text) : 'No messages yet';

  const addFiles = useCallback((files: File[]) => {
    setAttachmentNotice(null);
    const available = Math.max(0, MAX_ATTACHMENTS - pendingRef.current.length);
    if (available === 0) {
      setAttachmentNotice(`You can attach up to ${MAX_ATTACHMENTS} files per message.`);
      return;
    }
    const accepted: PendingAttachment[] = [];
    for (const file of files.slice(0, available)) {
      if (file.size > MAX_ATTACHMENT_BYTES) {
        setAttachmentNotice(`${file.name} is too large. The limit is 50 MB.`);
        continue;
      }
      const kind = classifyAttachment(file.type, file.name);
      accepted.push({
        id: `${file.name}-${file.lastModified}-${Math.random().toString(36).slice(2)}`,
        kind,
        name: file.name,
        size: file.size,
        mimeType: file.type || undefined,
        file,
        previewUrl: kind === 'image' ? URL.createObjectURL(file) : null,
      });
    }
    if (files.length > available) setAttachmentNotice(`Only ${available} more attachment${available === 1 ? '' : 's'} can be added.`);
    if (accepted.length) setPendingAttachments((current) => [...current, ...accepted]);
  }, []);

  const removeAttachment = useCallback((id: string) => {
    setPendingAttachments((current) => {
      const target = current.find((attachment) => attachment.id === id);
      if (target?.previewUrl) URL.revokeObjectURL(target.previewUrl);
      return current.filter((attachment) => attachment.id !== id);
    });
  }, []);

  const handleFileInput = (event: { target: HTMLInputElement }) => {
    addFiles(Array.from(event.target.files ?? []));
    event.target.value = '';
  };

  const handleDrop = (event: DragEvent<HTMLElement>) => {
    event.preventDefault();
    setIsDragging(false);
    addFiles(Array.from(event.dataTransfer.files ?? []));
  };

  const handlePaste = (event: ClipboardEvent<HTMLTextAreaElement>) => {
    const files = Array.from(event.clipboardData.files ?? []);
    if (files.length) {
      event.preventDefault();
      addFiles(files);
    }
  };

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const text = draft;
    if (!text.trim() && pendingAttachments.length === 0) return;
    setAttachmentNotice(null);
    try {
      const uploads: ChatAttachmentUpload[] = [];
      for (const attachment of pendingAttachments) {
        const dataUrl = await readFileAsDataUrl(attachment.file);
        uploads.push({
          id: attachment.id,
          kind: attachment.kind,
          name: attachment.name,
          size: attachment.size,
          mimeType: attachment.mimeType,
          dataUrl,
        });
      }
      const sent = await submitPrompt(text, uploads);
      if (!sent) return;
      setDraft('');
      for (const attachment of pendingAttachments) {
        if (attachment.previewUrl) URL.revokeObjectURL(attachment.previewUrl);
      }
      setPendingAttachments([]);
    } catch (err) {
      setAttachmentNotice(err instanceof Error ? err.message : 'Could not read the attachment.');
    }
  };

  const handleComposerKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      event.currentTarget.form?.requestSubmit();
    }
    if (event.key === 'Escape') onClose();
  };

  const handleDrawerKeyDown = (event: KeyboardEvent<HTMLElement>) => {
    if (event.key !== 'Tab') return;
    const focusable = Array.from(
      event.currentTarget.querySelectorAll<HTMLElement>('button:not(:disabled), textarea:not(:disabled), input:not(:disabled), [href], [tabindex]:not([tabindex="-1"])'),
    ).filter((element) => element.offsetParent !== null);
    if (focusable.length === 0) return;

    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  };

  const interactionPayload = interaction?.payload ?? {};
  const interactionChoices = Array.isArray(interactionPayload.choices)
    ? interactionPayload.choices.filter((choice): choice is string => typeof choice === 'string' && choice.trim().length > 0)
    : [];
  const multiSelect = interactionPayload.multi_select === true;
  const interactionQuestion = typeof interactionPayload.question === 'string' ? interactionPayload.question : '';
  const approvalCommand = typeof interactionPayload.command === 'string' ? interactionPayload.command : '';
  const approvalDescription = typeof interactionPayload.description === 'string' ? interactionPayload.description : '';

  return (
    <>
      {open ? <button className="chat-backdrop is-open" type="button" aria-label="Close chat" onClick={onClose} /> : null}
      <aside
        className={`chat-drawer ${open ? 'is-open' : ''}`}
        role="dialog"
        aria-modal="true"
        aria-label="Hermes chat"
        aria-labelledby="chat-drawer-title"
        aria-hidden={!open}
        inert={!open ? true : undefined}
        onKeyDown={handleDrawerKeyDown}
        onDragOver={(event) => { event.preventDefault(); setIsDragging(true); }}
        onDragLeave={() => setIsDragging(false)}
        onDrop={handleDrop}
      >
        <header className="chat-drawer-head">
          <div className="chat-title">
            <span className="chat-mark" aria-hidden><Bot size={18} /></span>
            <div>
              <p className="eyebrow">Hermes</p>
              <h2 id="chat-drawer-title">Chat</h2>
            </div>
          </div>
          <div className="chat-head-actions">
            <span className={`chat-status ${statusClass}`} title={statusText}>
              <Circle size={8} fill="currentColor" />
              {statusText}
            </span>
            <button className="chat-icon-button" type="button" onClick={() => void reset()} title="New chat" aria-label="New chat">
              <Trash2 size={16} />
            </button>
            <button className="chat-icon-button" type="button" onClick={onClose} title="Close chat" aria-label="Close chat">
              <X size={18} />
            </button>
          </div>
        </header>

        <div ref={scrollRef} className={`chat-transcript ${isDragging ? 'is-dragging' : ''}`} aria-live="polite">
          {isDragging ? (
            <div className="chat-drop-hint"><Paperclip size={20} /><span>Drop files to attach</span></div>
          ) : null}
          {messages.length === 0 ? (
            <section className="chat-empty">
              <MessageSquare size={22} />
              <p>Start a Hermes session from Mission Control.</p>
              <span>Ask, inspect, fix, ship.</span>
            </section>
          ) : (
            messages.map((message) => (
              <article key={message.id} className={`chat-message chat-message-${message.role} ${message.status ? `is-${message.status}` : ''}`}>
                <div className="chat-message-meta">
                  <span>{message.role}</span>
                  {message.status === 'streaming' ? <Loader2 size={12} className="chat-spin" aria-label="Streaming" /> : null}
                </div>
                {message.attachments?.length ? (
                  <div className="chat-message-attachments">
                    {message.attachments.map((attachment) => (
                      <span className="chat-file-chip" key={`${message.id}-${attachment.name}`}>
                        <AttachmentIcon kind={attachment.kind} />
                        <span>{attachment.name}</span>
                      </span>
                    ))}
                  </div>
                ) : null}
                <div className="chat-message-body">
                  <ReactMarkdown remarkPlugins={[remarkGfm]}>{message.text || (message.status === 'streaming' ? '...' : '')}</ReactMarkdown>
                </div>
              </article>
            ))
          )}
        </div>

        {activity ? (
          <div className={`chat-activity is-${activity.state}`} role="status">
            {activity.kind === 'tool' ? <ShieldCheck size={15} /> : <Loader2 size={15} className={activity.state === 'running' ? 'chat-spin' : ''} />}
            <strong>{activity.label}</strong>
            {activity.detail ? <span>{previewText(activity.detail)}</span> : null}
          </div>
        ) : null}

        {interaction ? (
          <section className={`chat-interaction chat-interaction-${interaction.kind}`} aria-label={interactionTitle(interaction)}>
            <div className="chat-interaction-heading">
              <span className="chat-interaction-icon">
                {interaction.kind === 'approval' ? <ShieldCheck size={16} /> : <KeyRound size={16} />}
              </span>
              <div>
                <strong>{interactionTitle(interaction)}</strong>
                <span>Answering here unblocks the running turn.</span>
              </div>
            </div>
            {interaction.kind === 'approval' ? (
              <>
                {approvalDescription ? <p className="chat-interaction-copy">{approvalDescription}</p> : null}
                {approvalCommand ? <code className="chat-command-preview">{approvalCommand}</code> : null}
                <div className="chat-choice-row">
                  {(interactionChoices.length ? interactionChoices : ['once', 'deny']).map((choice) => (
                    <button key={choice} type="button" className={`chat-choice ${choice === 'deny' ? 'is-danger' : ''}`} onClick={() => void respondInteraction(choice, choice)}>
                      {choice === 'deny' ? 'Deny' : choice === 'always' ? 'Always allow' : choice === 'session' ? 'This session' : 'Allow once'}
                    </button>
                  ))}
                </div>
              </>
            ) : interaction.kind === 'clarify' ? (
              <>
                <p className="chat-interaction-copy">{interactionQuestion || 'Hermes is asking for a decision.'}</p>
                {interactionChoices.length ? (
                  <div className="chat-choice-row">
                    {interactionChoices.map((choice) => {
                      const selected = selectedChoices.includes(choice);
                      return (
                        <button
                          key={choice}
                          type="button"
                          className={`chat-choice ${selected ? 'is-selected' : ''}`}
                          onClick={() => {
                            if (multiSelect) setSelectedChoices((current) => selected ? current.filter((item) => item !== choice) : [...current, choice]);
                            else void respondInteraction(choice);
                          }}
                        >
                          {selected ? <Check size={14} /> : null}{choice}
                        </button>
                      );
                    })}
                  </div>
                ) : null}
                <div className="chat-interaction-input-row">
                  <input value={interactionDraft} onChange={(event) => setInteractionDraft(event.target.value)} placeholder="Type your answer" aria-label="Answer Hermes" />
                  <button type="button" className="chat-choice is-primary" disabled={!interactionDraft.trim() && (!multiSelect || selectedChoices.length === 0)} onClick={() => void respondInteraction(interactionDraft.trim() || selectedChoices.join(', '))}>Send</button>
                </div>
              </>
            ) : (
              <div className="chat-interaction-input-row">
                <input type="password" value={interactionDraft} onChange={(event) => setInteractionDraft(event.target.value)} placeholder={interaction.kind === 'sudo' ? 'Password' : 'Secret value'} aria-label={interaction.kind === 'sudo' ? 'Sudo password' : 'Secret value'} autoComplete="off" />
                <button type="button" className="chat-choice is-primary" disabled={!interactionDraft} onClick={() => void respondInteraction(interactionDraft)}>Send</button>
              </div>
            )}
          </section>
        ) : null}

        {error ? (
          <div className="chat-error" role="alert">
            <span>{error}</span>
            <button type="button" onClick={() => void connect()}>Retry</button>
          </div>
        ) : (
          <p className="chat-preview">{lastPreview}</p>
        )}

        {pendingAttachments.length ? (
          <div className="chat-pending-attachments" aria-label="Pending attachments">
            {pendingAttachments.map((attachment) => (
              <div className="chat-pending-attachment" key={attachment.id}>
                {attachment.previewUrl ? <img src={attachment.previewUrl} alt="" /> : <AttachmentIcon kind={attachment.kind} />}
                <div className="chat-pending-attachment-copy"><strong>{attachment.name}</strong><span>{formatBytes(attachment.size ?? 0)}</span></div>
                <button type="button" onClick={() => removeAttachment(attachment.id)} aria-label={`Remove ${attachment.name}`}><X size={14} /></button>
              </div>
            ))}
          </div>
        ) : null}
        {attachmentNotice ? <p className="chat-attachment-notice" role="status">{attachmentNotice}</p> : null}

        <form className="chat-composer" onSubmit={handleSubmit}>
          <input ref={fileInputRef} type="file" multiple accept="image/*,application/pdf,*/*" className="chat-file-input" onChange={handleFileInput} />
          <button className="chat-icon-button chat-attach" type="button" onClick={() => fileInputRef.current?.click()} disabled={submitting || connectionState !== 'connected'} title="Attach image or file" aria-label="Attach image or file">
            <Paperclip size={17} />
          </button>
          <textarea
            ref={textareaRef}
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            onPaste={handlePaste}
            onKeyDown={handleComposerKeyDown}
            placeholder="Message Hermes"
            rows={1}
            disabled={connectionState !== 'connected'}
            aria-label="Message Hermes"
          />
          {running ? (
            <button className="chat-icon-button chat-stop" type="button" onClick={() => void interrupt()} aria-label="Interrupt response" title="Interrupt">
              <Pause size={17} />
            </button>
          ) : null}
          <button className="chat-send" type="submit" disabled={(!draft.trim() && pendingAttachments.length === 0) || submitting || connectionState !== 'connected'} aria-label="Send message" title="Send">
            {submitting ? <Loader2 size={17} className="chat-spin" /> : <Send size={17} />}
          </button>
        </form>
      </aside>
    </>
  );
}
