export type ChatRole = 'assistant' | 'system' | 'tool' | 'user';

export type AttachmentKind = 'image' | 'pdf' | 'file';

export type ChatAttachmentSummary = {
  id?: string;
  kind: AttachmentKind;
  name: string;
  size?: number;
  mimeType?: string;
};

export type ChatAttachmentUpload = ChatAttachmentSummary & {
  dataUrl: string;
};

export type ChatMessage = {
  id: string;
  role: ChatRole;
  text: string;
  status?: 'streaming' | 'complete' | 'error' | 'interrupted';
  createdAt: number;
  attachments?: ChatAttachmentSummary[];
};

export type GatewayTranscriptMessage = {
  role?: unknown;
  text?: unknown;
  content?: unknown;
  display_kind?: unknown;
};

export type GatewayEvent = {
  type: string;
  session_id?: string;
  payload?: Record<string, unknown>;
};

export type GatewayInteractionKind = 'approval' | 'clarify' | 'secret' | 'sudo' | 'terminal_read';

export type GatewayInteractionRequest = {
  kind: GatewayInteractionKind;
  sessionId: string | null;
  requestId: string | null;
  payload: Record<string, unknown>;
};

export type ChatModelIdentity = {
  model: string;
  provider?: string;
};

export type GatewayCommandDispatch =
  | { type: 'exec' | 'plugin'; output?: string; warning?: string }
  | { type: 'alias'; target: string }
  | { type: 'skill'; name: string; message?: string; display?: string; notice?: string }
  | { type: 'send'; message: string; display?: string; notice?: string }
  | { type: 'prefill'; message: string; notice?: string };

export type ChatActivity = {
  kind: 'status' | 'tool' | 'reasoning';
  label: string;
  detail?: string;
  state: 'running' | 'complete' | 'error';
};

export type JsonRpcRequest = {
  jsonrpc: '2.0';
  id: string;
  method: string;
  params: Record<string, unknown>;
};

export type JsonRpcResponse = {
  jsonrpc?: string;
  id?: string | number | null;
  result?: unknown;
  error?: { code?: unknown; message?: unknown } | unknown;
};

export type ParsedGatewayFrame =
  | { kind: 'event'; event: GatewayEvent }
  | { kind: 'response'; response: JsonRpcResponse }
  | { kind: 'unknown'; value: unknown }
  | { kind: 'malformed'; error: string };

export type ReconnectState = {
  attempts: number;
  baseMs?: number;
  maxMs?: number;
};

const STREAM_CHARS = 72;

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

function safeRole(value: unknown): ChatRole {
  return value === 'assistant' || value === 'system' || value === 'tool' || value === 'user' ? value : 'assistant';
}

function textFromContent(value: unknown): string {
  if (typeof value === 'string') return value;
  if (Array.isArray(value)) {
    return value.map(textFromContent).filter(Boolean).join('\n');
  }
  if (isRecord(value)) {
    if (typeof value.text === 'string') return value.text;
    if ('content' in value) return textFromContent(value.content);
  }
  return '';
}

export function parseGatewayFrame(raw: unknown): ParsedGatewayFrame {
  if (typeof raw !== 'string') {
    return { kind: 'malformed', error: 'Gateway sent a non-text WebSocket frame.' };
  }

  try {
    const value = JSON.parse(raw) as unknown;
    if (!isRecord(value)) {
      return { kind: 'unknown', value };
    }

    if (value.method === 'event') {
      const params = value.params;
      if (isRecord(params) && typeof params.type === 'string') {
        return {
          kind: 'event',
          event: {
            type: params.type,
            session_id: typeof params.session_id === 'string' ? params.session_id : undefined,
            payload: isRecord(params.payload) ? params.payload : undefined,
          },
        };
      }
      return { kind: 'unknown', value };
    }

    if ('id' in value && ('result' in value || 'error' in value)) {
      return { kind: 'response', response: value as JsonRpcResponse };
    }

    return { kind: 'unknown', value };
  } catch {
    return { kind: 'malformed', error: 'Gateway sent malformed JSON.' };
  }
}

export function createRpcRequest(id: string, method: string, params: Record<string, unknown> = {}): JsonRpcRequest {
  return { jsonrpc: '2.0', id, method, params };
}

export function getRpcErrorMessage(error: unknown): string {
  if (isRecord(error) && typeof error.message === 'string' && error.message.trim()) {
    return error.message.trim();
  }
  return 'Gateway request failed.';
}

export function isResponseFor(response: JsonRpcResponse, id: string): boolean {
  return String(response.id ?? '') === id;
}

export function normalizeTranscript(messages: GatewayTranscriptMessage[], now = Date.now()): ChatMessage[] {
  return messages
    .map((message, index) => {
      const rawText = textFromContent(message.text) || textFromContent(message.content);
      return {
        id: `restored-${now}-${index}`,
        role: safeRole(message.role),
        text: rawText,
        status: 'complete' as const,
        createdAt: now + index,
      };
    })
    .filter((message) => message.text.trim().length > 0 || message.role === 'assistant' || message.role === 'user');
}

export function extractSessionId(result: unknown): string | null {
  if (!isRecord(result)) return null;
  return typeof result.session_id === 'string' && result.session_id.trim() ? result.session_id : null;
}

export function extractSessionKey(result: unknown): string | null {
  if (!isRecord(result)) return null;
  for (const key of ['stored_session_id', 'session_key', 'resumed']) {
    const value = result[key];
    if (typeof value === 'string' && value.trim()) return value;
  }
  return null;
}

export function parseSlash(command: string): { name: string; arg: string } {
  const normalized = command.trim().replace(/^\/+/, '');
  const match = normalized.match(/^(\S+)(?:\s+([\s\S]*))?$/);
  return match ? { name: match[1], arg: (match[2] ?? '').trim() } : { name: '', arg: '' };
}

export function parseCommandDispatch(raw: unknown): GatewayCommandDispatch | null {
  if (!isRecord(raw) || typeof raw.type !== 'string') return null;
  const stringValue = (value: unknown): string | undefined =>
    typeof value === 'string' ? value : undefined;

  if (raw.type === 'exec' || raw.type === 'plugin') {
    return { type: raw.type, output: stringValue(raw.output), warning: stringValue(raw.warning) };
  }
  if (raw.type === 'alias' && typeof raw.target === 'string' && raw.target.trim()) {
    return { type: 'alias', target: raw.target.trim() };
  }
  if (raw.type === 'skill' && typeof raw.name === 'string' && raw.name.trim()) {
    return {
      type: 'skill',
      name: raw.name.trim(),
      message: stringValue(raw.message),
      display: stringValue(raw.display),
      notice: stringValue(raw.notice),
    };
  }
  if (raw.type === 'send' && typeof raw.message === 'string') {
    return {
      type: 'send',
      message: raw.message,
      display: stringValue(raw.display),
      notice: stringValue(raw.notice),
    };
  }
  if (raw.type === 'prefill' && typeof raw.message === 'string') {
    return { type: 'prefill', message: raw.message, notice: stringValue(raw.notice) };
  }
  return null;
}

export function extractSessionModel(result: unknown): ChatModelIdentity | null {
  if (!isRecord(result)) return null;

  const candidates: Record<string, unknown>[] = [result];
  if (isRecord(result.info)) candidates.unshift(result.info);
  for (const candidate of candidates) {
    if (typeof candidate.model === 'string' && candidate.model.trim()) {
      const provider = typeof candidate.provider === 'string' && candidate.provider.trim()
        ? candidate.provider.trim()
        : undefined;
      return { model: candidate.model.trim(), ...(provider ? { provider } : {}) };
    }
  }

  if (typeof result.output === 'string') {
    const match = result.output.match(/^Model:\s*(.+?)(?:\s+\(([^)]+)\))?\s*$/m);
    if (match?.[1]?.trim()) {
      return {
        model: match[1].trim(),
        ...(match[2]?.trim() ? { provider: match[2].trim() } : {}),
      };
    }
  }
  return null;
}

export function extractInjectedSessionToken(html: string): string | null {
  const match = html.match(/__HERMES_SESSION_TOKEN__\s*(?:=|:)\s*["']([^"']+)["']/);
  return match?.[1]?.trim() || null;
}

export function classifyAttachment(mimeType: string, filename: string): AttachmentKind {
  const mime = mimeType.trim().toLowerCase();
  const name = filename.trim().toLowerCase();
  if (mime.startsWith('image/') || /\.(avif|bmp|gif|jpe?g|png|webp)$/i.test(name)) return 'image';
  if (mime === 'application/pdf' || name.endsWith('.pdf')) return 'pdf';
  return 'file';
}

export function attachmentRpcMethod(kind: AttachmentKind): 'image.attach_bytes' | 'pdf.attach' | 'file.attach' {
  if (kind === 'image') return 'image.attach_bytes';
  if (kind === 'pdf') return 'pdf.attach';
  return 'file.attach';
}

export function extractInteractionRequest(event: GatewayEvent): GatewayInteractionRequest | null {
  const kindByEvent: Record<string, GatewayInteractionKind> = {
    'approval.request': 'approval',
    'clarify.request': 'clarify',
    'secret.request': 'secret',
    'sudo.request': 'sudo',
    'terminal.read.request': 'terminal_read',
  };
  const kind = kindByEvent[event.type];
  if (!kind) return null;
  const payload = event.payload ?? {};
  const requestId = typeof payload.request_id === 'string' && payload.request_id.trim()
    ? payload.request_id
    : null;
  return {
    kind,
    sessionId: event.session_id ?? null,
    requestId,
    payload,
  };
}

export function eventActivity(event: GatewayEvent): ChatActivity | null {
  const payload = event.payload ?? {};
  const detail = eventText(event) || (typeof payload.message === 'string' ? payload.message : '');
  if (event.type === 'reasoning.delta' || event.type === 'thinking.delta') {
    return { kind: 'reasoning', label: 'Working through the request', detail, state: 'running' };
  }
  if (event.type === 'reasoning.available') {
    return { kind: 'reasoning', label: 'Reasoning available', detail, state: 'complete' };
  }
  if (event.type === 'status.update') {
    const status = typeof payload.kind === 'string' ? payload.kind
      : typeof payload.status === 'string' ? payload.status
      : typeof payload.phase === 'string' ? payload.phase : '';
    const labels: Record<string, string> = {
      compacting: 'Compacting context',
      process: 'Running process',
      processing: 'Processing',
      goal: 'Working toward goal',
      waiting: 'Waiting',
      complete: 'Complete',
    };
    return { kind: 'status', label: labels[status] || detail || 'Working', detail, state: status === 'complete' ? 'complete' : 'running' };
  }
  if (event.type.startsWith('moa.')) {
    const phase = event.type.slice(4).replace(/[._-]+/g, ' ');
    return { kind: 'status', label: `MoA ${phase || 'workflow'}`, detail, state: event.type.endsWith('complete') ? 'complete' : 'running' };
  }
  if (event.type === 'tool.start') {
    const name = typeof payload.name === 'string' ? payload.name
      : typeof payload.tool_name === 'string' ? payload.tool_name
      : typeof payload.tool === 'string' ? payload.tool : 'Tool';
    return { kind: 'tool', label: name, detail, state: 'running' };
  }
  if (event.type === 'tool.progress' || event.type === 'tool.delta') {
    const name = typeof payload.name === 'string' ? payload.name
      : typeof payload.tool_name === 'string' ? payload.tool_name : 'Tool';
    return { kind: 'tool', label: name, detail, state: 'running' };
  }
  if (event.type === 'tool.complete') {
    const name = typeof payload.name === 'string' ? payload.name
      : typeof payload.tool_name === 'string' ? payload.tool_name : 'Tool';
    return { kind: 'tool', label: name, detail, state: 'complete' };
  }
  if (event.type === 'error') return { kind: 'status', label: detail || 'Request failed', state: 'error' };
  return null;
}

export function extractTranscript(result: unknown): GatewayTranscriptMessage[] {
  if (!isRecord(result) || !Array.isArray(result.messages)) return [];
  return result.messages.filter(isRecord) as GatewayTranscriptMessage[];
}

export function extractInflightAssistant(result: unknown): string {
  if (!isRecord(result) || !isRecord(result.inflight)) return '';
  return typeof result.inflight.assistant === 'string' ? result.inflight.assistant : '';
}

export function eventText(event: GatewayEvent): string {
  const payload = event.payload;
  if (!payload) return '';
  const text = typeof payload.text === 'string' ? payload.text : '';
  const rendered = typeof payload.rendered === 'string' ? payload.rendered : '';
  return text || rendered;
}

export function applyGatewayEvent(messages: ChatMessage[], event: GatewayEvent, now = Date.now()): ChatMessage[] {
  if (event.type === 'message.start') {
    if (messages.at(-1)?.role === 'assistant' && messages.at(-1)?.status === 'streaming') {
      return messages;
    }
    return [...messages, { id: `assistant-${now}`, role: 'assistant', text: '', status: 'streaming', createdAt: now }];
  }

  if (event.type === 'message.delta') {
    const delta = eventText(event);
    if (!delta) return messages;
    const next = [...messages];
    const last = next.at(-1);
    if (last?.role === 'assistant' && last.status === 'streaming') {
      next[next.length - 1] = { ...last, text: `${last.text}${delta}` };
      return next;
    }
    return [...messages, { id: `assistant-${now}`, role: 'assistant', text: delta, status: 'streaming', createdAt: now }];
  }

  if (event.type === 'message.interim') {
    const text = eventText(event);
    if (!text) return messages;
    const next = [...messages];
    const last = next.at(-1);
    if (last?.role === 'assistant' && last.status === 'streaming') {
      if (!last.text.trim()) {
        next[next.length - 1] = { ...last, text };
        return next;
      }
      next[next.length - 1] = { ...last, status: 'complete' };
    }
    return [...next, { id: `assistant-interim-${now}`, role: 'assistant', text, status: 'streaming', createdAt: now }];
  }

  if (event.type === 'reasoning.available') {
    const payload = event.payload ?? {};
    const text = eventText(event)
      || (typeof payload.reasoning === 'string' ? payload.reasoning : '')
      || (typeof payload.content === 'string' ? payload.content : '');
    if (!text.trim()) return messages;
    return [...messages, {
      id: `reasoning-${now}`,
      role: 'tool',
      text: `**Reasoning**\n\n${text}`,
      status: 'complete',
      createdAt: now,
    }];
  }

  if (event.type === 'message.complete') {
    const finalText = eventText(event);
    const next = [...messages];
    const last = next.at(-1);
    if (last?.role === 'assistant') {
      next[next.length - 1] = {
        ...last,
        text: finalText || last.text,
        status: 'complete',
      };
      return next;
    }
    if (finalText) {
      return [...messages, { id: `assistant-${now}`, role: 'assistant', text: finalText, status: 'complete', createdAt: now }];
    }
    return messages;
  }

  if (event.type === 'error') {
    const message = eventText(event) || 'Gateway reported an error.';
    const next = [...messages];
    const last = next.at(-1);
    if (last?.role === 'assistant' && last.status === 'streaming') {
      next[next.length - 1] = { ...last, text: last.text || message, status: 'error' };
      return next;
    }
    return [...messages, { id: `assistant-error-${now}`, role: 'assistant', text: message, status: 'error', createdAt: now }];
  }

  return messages;
}

export function nextReconnectDelay({ attempts, baseMs = 500, maxMs = 8000 }: ReconnectState): number {
  const bounded = Math.max(0, Math.min(attempts, 6));
  return Math.min(maxMs, baseMs * 2 ** bounded);
}

export function previewText(text: string): string {
  const compact = text.replace(/\s+/g, ' ').trim();
  return compact.length > STREAM_CHARS ? `${compact.slice(0, STREAM_CHARS)}...` : compact;
}
