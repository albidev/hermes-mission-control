export type ChatRole = 'assistant' | 'system' | 'tool' | 'user';

export type ChatMessageKind = 'assistant' | 'event' | 'reasoning' | 'system' | 'tool' | 'user';

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
  /** Stable SessionDB identity for canonical rows; absent on ephemeral live rows. */
  canonicalId?: string;
  source?: 'canonical' | 'resume' | 'live';
  role: ChatRole;
  kind?: ChatMessageKind;
  text: string;
  status?: 'streaming' | 'complete' | 'error' | 'interrupted';
  createdAt: number | null;
  attachments?: ChatAttachmentSummary[];
  detail?: string;
  output?: string;
  toolId?: string;
  toolName?: string;
  toolInput?: string;
  durationS?: number;
  toolCalls?: Array<{ name: string; arguments: string }>;
  attribution?: {
    handle: string;
    displayName?: string;
    model?: string;
    provider?: string;
  };
};

export type GatewayTranscriptMessage = {
  id?: unknown;
  canonical_id?: unknown;
  session_id?: unknown;
  role?: unknown;
  timestamp?: unknown;
  text?: unknown;
  content?: unknown;
  name?: unknown;
  context?: unknown;
  tool_name?: unknown;
  tool_call_id?: unknown;
  tool_calls?: unknown;
  args_text?: unknown;
  result?: unknown;
  result_text?: unknown;
  summary?: unknown;
  inline_diff?: unknown;
  duration_s?: unknown;
  reasoning?: unknown;
  reasoning_content?: unknown;
  reasoning_details?: unknown;
  codex_reasoning_items?: unknown;
  display_kind?: unknown;
  display_metadata?: unknown;
};

export type ChatModelProviderOption = {
  slug: string;
  name: string;
  models: string[];
  total_models: number;
  is_current?: boolean;
  authenticated?: boolean;
  warning?: string;
};

export type ChatModelSwitchResult = {
  ok: boolean;
  confirmRequired?: boolean;
  confirmMessage?: string;
  warning?: string;
  error?: string;
};

export type GatewayEvent = {
  type: string;
  session_id?: string;
  seq?: number;
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
  reasoningEffort?: string;
};

export type GatewayCommandDispatch =
  | { type: 'exec' | 'plugin'; output?: string; warning?: string }
  | { type: 'alias'; target: string }
  | { type: 'skill'; name: string; message?: string; display?: string; notice?: string }
  | { type: 'send'; message: string; display?: string; notice?: string }
  | { type: 'prefill'; message: string; notice?: string };

export async function refreshModelAfterCommandDispatch(
  dispatch: GatewayCommandDispatch,
  activeSessionId: string,
  refreshModel: (sessionId: string) => Promise<void>,
): Promise<void> {
  if (dispatch.type === 'exec' || dispatch.type === 'plugin') {
    await refreshModel(activeSessionId);
  }
}

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

export class ConnectionAttemptGate {
  private active = false;

  tryAcquire(): boolean {
    if (this.active) return false;
    this.active = true;
    return true;
  }

  release(): void {
    this.active = false;
  }
}

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
    for (const key of ['text', 'content', 'summary', 'reasoning']) {
      if (key in value) {
        const text = textFromContent(value[key]);
        if (text) return text;
      }
    }
  }
  return '';
}

function reasoningFromMessage(message: GatewayTranscriptMessage): string {
  for (const key of ['reasoning', 'reasoning_content', 'reasoning_details', 'codex_reasoning_items'] as const) {
    const text = textFromContent(message[key]);
    if (text.trim()) return text.trim();
  }
  return '';
}

function stringValue(value: unknown): string {
  return typeof value === 'string' ? value : '';
}

export function parseChatTimestamp(value: unknown): number | null {
  if (typeof value === 'number' && Number.isFinite(value)) {
    return Math.abs(value) >= 1e11 ? value : value * 1000;
  }
  if (typeof value !== 'string') return null;
  const trimmed = value.trim();
  if (!trimmed) return null;

  const numeric = Number(trimmed);
  if (Number.isFinite(numeric)) {
    return Math.abs(numeric) >= 1e11 ? numeric : numeric * 1000;
  }
  const parsed = Date.parse(trimmed);
  return Number.isFinite(parsed) ? parsed : null;
}

function todoToolCalls(message: GatewayTranscriptMessage): Array<{ name: string; arguments: string }> {
  if (!Array.isArray(message.tool_calls)) return [];
  return message.tool_calls.flatMap((call) => {
    if (!isRecord(call)) return [];
    const fn = isRecord(call.function) ? call.function : call;
    const name = stringValue(fn.name).trim();
    const args = stringValue(fn.arguments);
    return name.toLowerCase() === 'todo' && args.trim() ? [{ name, arguments: args }] : [];
  });
}

function structuredText(value: unknown): string {
  if (typeof value === 'string') return value;
  if (value === undefined || value === null) return '';
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
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
            seq: typeof params.seq === 'number' && Number.isFinite(params.seq) ? params.seq : undefined,
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

/**
 * Mission Control's New action must preserve the durable backend session.
 *
 * The dashboard hosts tui_gateway in-process. Closing the previous session
 * tears down its agent and can invalidate the process-shared SessionDB handle,
 * making every later prompt fail with session_persistence_failed. New therefore
 * means "detach local UI state and create another session", not session.close.
 */
export function shouldCloseBackendSessionForNewChat(): false {
  return false;
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

const SYSTEM_NOTIFICATION_PREFIXES = [
  '[IMPORTANT:',
  '[SYSTEM:',
  '[System note:',
  '[ASYNC DELEGATION',
  '[BACKGROUND PROCESS',
] as const;

export function isSystemNotification(text: string): boolean {
  const trimmed = text.trim();
  return SYSTEM_NOTIFICATION_PREFIXES.some((prefix) => trimmed.startsWith(prefix));
}

export function normalizeTranscript(messages: GatewayTranscriptMessage[], _now = Date.now()): ChatMessage[] {
  const normalized: ChatMessage[] = [];
  messages.forEach((message, index) => {
    const sourceRole = safeRole(message.role);
    const rawText = textFromContent(message.text) || textFromContent(message.content);
    const role = sourceRole === 'user' && isSystemNotification(rawText) ? 'system' : sourceRole;
    const displayKind = stringValue(message.display_kind);
    const createdAt = parseChatTimestamp(message.timestamp);
    const canonicalId = stringValue(message.canonical_id);
    const source = canonicalId ? 'canonical' as const : 'resume' as const;
    const messageId = (suffix: string): string => canonicalId
      ? (suffix === 'message' ? canonicalId : `${canonicalId}:${suffix}`)
      : `restored-${suffix}-${index}`;
    if (displayKind === 'hidden') return;

    if (displayKind === 'model_switch' || displayKind === 'auto_continue' || displayKind === 'async_delegation_complete') {
      const labels: Record<string, string> = {
        model_switch: 'Model changed',
        auto_continue: 'Resumed interrupted turn',
        async_delegation_complete: 'Background agent work finished',
      };
      normalized.push({
        id: messageId('event'),
        ...(canonicalId ? { canonicalId } : {}),
        source,
        role: 'system',
        kind: 'event',
        text: labels[displayKind],
        status: 'complete',
        createdAt,
      });
      return;
    }

    if (role === 'tool') {
      const toolName = stringValue(message.name) || stringValue(message.tool_name) || 'tool';
      const context = stringValue(message.context) || rawText;
      const toolInput = stringValue(message.args_text) || context;
      const output = stringValue(message.result_text)
        || stringValue(message.summary)
        || stringValue(message.inline_diff)
        || structuredText(message.result);
      const durationS = typeof message.duration_s === 'number' ? message.duration_s : undefined;
      normalized.push({
        id: messageId('tool'),
        ...(canonicalId ? { canonicalId } : {}),
        source,
        role: 'tool',
        kind: 'tool',
        toolName,
        toolId: stringValue(message.tool_call_id) || undefined,
        text: context,
        toolInput,
        output: output || undefined,
        durationS,
        status: 'complete',
        createdAt,
      });
      return;
    }

    const reasoning = role === 'assistant' ? reasoningFromMessage(message) : '';
    const todoCalls = role === 'assistant' ? todoToolCalls(message) : [];
    if (reasoning) {
      normalized.push({
        id: messageId('reasoning'),
        ...(canonicalId ? { canonicalId } : {}),
        source,
        role: 'tool',
        kind: 'reasoning',
        text: reasoning,
        status: 'complete',
        createdAt,
      });
    }

    if (!rawText.trim() && role !== 'assistant' && role !== 'user') return;
    normalized.push({
      id: messageId('message'),
      ...(canonicalId ? { canonicalId } : {}),
      source,
      role,
      kind: role === 'assistant' || role === 'user' || role === 'system' ? role : undefined,
      text: rawText,
      status: 'complete',
      createdAt,
      ...(todoCalls.length ? { toolCalls: todoCalls } : {}),
    });
  });
  return normalized;
}

export type ChatTimestampMetadata = {
  role?: unknown;
  content?: unknown;
  text?: unknown;
  tool_name?: unknown;
  toolName?: unknown;
  tool_call_id?: unknown;
  toolId?: unknown;
  tool_calls?: unknown;
  toolCalls?: unknown;
  context?: unknown;
  args_text?: unknown;
  toolInput?: unknown;
  result?: unknown;
  result_text?: unknown;
  output?: unknown;
  summary?: unknown;
  inline_diff?: unknown;
  timestamp?: unknown;
  display_kind?: unknown;
};

function metadataString(value: unknown): string {
  return typeof value === 'string' ? value : '';
}

function messageToolName(message: GatewayTranscriptMessage | ChatTimestampMetadata): string {
  const record = message as Record<string, unknown>;
  return metadataString(record.tool_name) || metadataString(record.toolName) || metadataString(record.name);
}

function messageIdentityVariants(message: GatewayTranscriptMessage | ChatTimestampMetadata): string[] {
  const role = safeRole(message.role);
  const text = textFromContent(message.text) || textFromContent(message.content);
  if (role !== 'tool') return [`${role}:content:${text}`];

  const record = message as Record<string, unknown>;
  const toolName = messageToolName(message);
  const toolId = metadataString(record.tool_call_id) || metadataString(record.toolId);
  const input = metadataString(record.args_text) || metadataString(record.toolInput) || metadataString(record.context) || text;
  const output = metadataString(record.result_text)
    || metadataString(record.output)
    || metadataString(record.summary)
    || metadataString(record.inline_diff)
    || structuredText(record.result);
  const variants = [
    `tool:content:${text}`,
    `tool:shape:${toolName}:${input}:${output}`,
    `tool:name-input:${toolName}:${input}`,
    `tool:name-output:${toolName}:${output}`,
  ];
  if (toolId) variants.unshift(`tool:id:${toolId}`);
  return variants;
}

/** Apply canonical sidecar timestamps to a resume transcript without relying on array ids. */
export function reconcileTranscriptTimestamps(
  transcript: GatewayTranscriptMessage[],
  metadata: ChatTimestampMetadata[],
): GatewayTranscriptMessage[] {
  const buckets = new Map<string, Array<{ index: number; item: ChatTimestampMetadata }>>();
  metadata.forEach((item, index) => {
    for (const key of messageIdentityVariants(item)) {
      const bucket = buckets.get(key) ?? [];
      bucket.push({ index, item });
      buckets.set(key, bucket);
    }
  });
  const consumed = new Set<number>();

  return transcript.map((message) => {
    let matched: { index: number; item: ChatTimestampMetadata } | undefined;
    for (const key of messageIdentityVariants(message)) {
      const bucket = buckets.get(key) ?? [];
      matched = bucket.find((candidate) => !consumed.has(candidate.index));
      if (matched) break;
    }
    if (!matched && safeRole(message.role) === 'tool') {
      const toolName = messageToolName(message);
      matched = metadata
        .map((item, index) => ({ item, index }))
        .find((candidate) => {
          if (consumed.has(candidate.index) || safeRole(candidate.item.role) !== 'tool') return false;
          const candidateName = messageToolName(candidate.item);
          return !toolName || !candidateName || candidateName === toolName;
        });
    }
    const timestamp = matched ? parseChatTimestamp(matched.item.timestamp) : null;
    if (!matched || timestamp === null) return message;
    consumed.add(matched.index);
    return { ...message, timestamp };
  });
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

export function extractSessionRunning(result: unknown): boolean {
  if (!isRecord(result)) return false;
  return result.running === true || result.status === 'streaming';
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
      const reasoningEffort = typeof candidate.reasoning_effort === 'string' && candidate.reasoning_effort.trim()
        ? candidate.reasoning_effort.trim()
        : typeof candidate.reasoningEffort === 'string' && candidate.reasoningEffort.trim()
          ? candidate.reasoningEffort.trim()
          : undefined;
      return {
        model: candidate.model.trim(),
        ...(provider ? { provider } : {}),
        ...(reasoningEffort ? { reasoningEffort } : {}),
      };
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

export type PendingPromptFingerprint = {
  text: string;
  baselineUserCount: number;
};

export function pendingPromptWasPersisted(
  pending: PendingPromptFingerprint,
  transcript: GatewayTranscriptMessage[],
): boolean {
  const baseline = Math.max(0, Math.floor(pending.baselineUserCount));
  const userMessages = transcript.filter((message) => message.role === 'user');
  if (userMessages.length <= baseline) return false;
  const expected = pending.text.trim();
  if (!expected) return false;
  return userMessages.slice(baseline).some((message) => {
    const actual = textFromContent(message.text) || textFromContent(message.content);
    return actual.trim() === expected;
  });
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
  const delta = typeof payload.delta === 'string' ? payload.delta : '';
  const content = typeof payload.content === 'string' ? payload.content : '';
  const output = typeof payload.output === 'string' ? payload.output : '';
  const finalResponse = typeof payload.final_response === 'string' ? payload.final_response : '';
  return text || rendered || delta || content || output || finalResponse;
}

export function applyGatewayEvent(messages: ChatMessage[], event: GatewayEvent, now = Date.now()): ChatMessage[] {
  const payload = event.payload ?? {};
  const eventToolId = stringValue(payload.tool_id) || stringValue(payload.tool_call_id);
  const eventToolName = stringValue(payload.name) || stringValue(payload.tool_name) || stringValue(payload.tool) || 'Tool';
  const isToolStart = event.type === 'tool.start' || event.type === 'tool.started';
  const isToolProgress = event.type === 'tool.progress' || event.type === 'tool.delta' || event.type === 'tool.output';
  const isToolComplete = event.type === 'tool.complete' || event.type === 'tool.completed';
  const isMessageStart = event.type === 'message.start' || event.type === 'message.started';
  const isMessageDelta = event.type === 'message.delta' || event.type === 'assistant.delta';
  const isMessageComplete = event.type === 'message.complete' || event.type === 'message.completed' || event.type === 'assistant.completed';
  const lastIndexOf = (predicate: (message: ChatMessage) => boolean): number => {
    for (let index = messages.length - 1; index >= 0; index -= 1) {
      if (predicate(messages[index])) return index;
    }
    return -1;
  };

  if (isMessageStart) {
    const next = messages
      .filter((message) => !(message.kind === 'assistant' && message.status === 'streaming' && !message.text.trim()))
      .map((message) => message.kind === 'assistant' && message.status === 'streaming'
        ? { ...message, status: message.text.trim() ? 'complete' as const : 'interrupted' as const }
        : message);
    if (next.at(-1)?.kind === 'assistant' && next.at(-1)?.status === 'streaming') return next;
    return [...next, { id: `assistant-${now}`, role: 'assistant', kind: 'assistant', text: '', status: 'streaming', createdAt: now }];
  }

  if (isMessageDelta) {
    const delta = eventText(event);
    if (!delta) return messages;
    const next = [...messages];
    const last = next.at(-1);
    if (last?.kind === 'assistant' && last.status === 'streaming') {
      next[next.length - 1] = { ...last, text: `${last.text}${delta}` };
      return next;
    }
    return [...messages, { id: `assistant-${now}`, role: 'assistant', kind: 'assistant', text: delta, status: 'streaming', createdAt: now }];
  }

  if (event.type === 'message.interim') {
    const text = eventText(event);
    if (!text) return messages;
    const next = [...messages];
    const last = next.at(-1);
    if (last?.kind === 'assistant' && last.status === 'streaming') {
      if (!last.text.trim()) {
        next[next.length - 1] = { ...last, text };
        return next;
      }
      next[next.length - 1] = { ...last, status: 'complete' };
    }
    return [...next, { id: `assistant-interim-${now}`, role: 'assistant', kind: 'assistant', text, status: 'streaming', createdAt: now }];
  }

  const insertReasoning = (next: ChatMessage[], entry: ChatMessage): ChatMessage[] => {
    // If a completed assistant reply already precedes this reasoning event (the
    // gateway sometimes flushes reasoning with the final turn payload), insert
    // the bubble BEFORE that reply instead of appending it below.
    let boundary = next.length;
    for (let i = next.length - 1; i >= 0; i -= 1) {
      const item = next[i];
      if (item.kind === 'assistant' && item.status === 'complete') {
        boundary = i;
      }
      break;
    }
    return [...next.slice(0, boundary), entry, ...next.slice(boundary)];
  };

  if (event.type === 'reasoning.delta' || event.type === 'thinking.delta') {
    const delta = eventText(event);
    if (!delta) return messages;
    const next = [...messages];
    const index = lastIndexOf((message) => message.kind === 'reasoning' && message.status === 'streaming');
    if (index >= 0) {
      const current = next[index];
      next[index] = { ...current, text: `${current.text}${delta}` };
      return next;
    }
    return insertReasoning(next, { id: `reasoning-${now}`, role: 'tool', kind: 'reasoning', text: delta, status: 'streaming', createdAt: now });
  }

  if (event.type === 'reasoning.available') {
    const text = eventText(event)
      || stringValue(payload.reasoning)
      || stringValue(payload.content)
      || textFromContent(payload.reasoning_details);
    if (!text.trim()) return messages;
    const next = [...messages];
    const index = lastIndexOf((message) => message.kind === 'reasoning' && message.status === 'streaming');
    if (index >= 0) {
      const current = next[index];
      next[index] = { ...current, text, status: 'complete' };
      return next;
    }
    const duplicate = lastIndexOf((message) => message.kind === 'reasoning' && message.text.trim() === text.trim());
    if (duplicate >= 0) return messages;
    return insertReasoning(next, { id: `reasoning-${now}`, role: 'tool', kind: 'reasoning', text, status: 'complete', createdAt: now });
  }

  if (isToolStart) {
    const context = stringValue(payload.context);
    const toolInput = stringValue(payload.args_text) || context;
    return [...messages, {
      id: `tool-${eventToolId || now}`,
      role: 'tool',
      kind: 'tool',
      toolId: eventToolId || undefined,
      toolName: eventToolName,
      text: context,
      toolInput,
      status: 'streaming',
      createdAt: now,
    }];
  }

  if (isToolProgress) {
    const detail = eventText(event) || stringValue(payload.preview) || stringValue(payload.text);
    if (!detail) return messages;
    const next = [...messages];
    const index = lastIndexOf((message) => message.kind === 'tool' && (!eventToolId || message.toolId === eventToolId));
    if (index < 0) return [...messages, { id: `tool-${eventToolId || now}`, role: 'tool', kind: 'tool', toolId: eventToolId || undefined, toolName: eventToolName, text: '', toolInput: '', detail, status: 'streaming', createdAt: now }];
    const current = next[index];
    next[index] = { ...current, detail: `${current.detail ? `${current.detail}\n` : ''}${detail}` };
    return next;
  }

  if (isToolComplete) {
    const output = stringValue(payload.result_text)
      || stringValue(payload.summary)
      || stringValue(payload.inline_diff)
      || structuredText(payload.result);
    const input = stringValue(payload.args_text) || stringValue(payload.context);
    const durationS = typeof payload.duration_s === 'number' ? payload.duration_s : undefined;
    const next = [...messages];
    const index = lastIndexOf((message) => message.kind === 'tool' && (!eventToolId || message.toolId === eventToolId));
    if (index < 0) {
      return [...messages, { id: `tool-${eventToolId || now}`, role: 'tool', kind: 'tool', toolId: eventToolId || undefined, toolName: eventToolName, text: '', toolInput: input, output: output || undefined, durationS, status: 'complete', createdAt: now }];
    }
    const current = next[index];
    next[index] = {
      ...current,
      toolName: current.toolName || eventToolName,
      toolInput: current.toolInput || input,
      output: output || current.output,
      durationS,
      status: 'complete',
    };
    return next;
  }

  if (isMessageComplete) {
    const finalText = eventText(event);
    const next = [...messages];
    const index = lastIndexOf((message) => message.kind === 'assistant' && message.status === 'streaming');
    if (index >= 0) {
      next[index] = { ...next[index], text: finalText || next[index].text, status: 'complete' };
      return next.filter((message, candidateIndex) => !(candidateIndex !== index && message.kind === 'assistant' && message.status === 'streaming' && !message.text.trim()));
    }
    if (finalText) return [...messages, { id: `assistant-${now}`, role: 'assistant', kind: 'assistant', text: finalText, status: 'complete', createdAt: now }];
    return messages;
  }

  if (event.type === 'run.completed') {
    const transcript = Array.isArray(payload.messages) ? payload.messages : [];
    const finalMessage = [...transcript].reverse().find((item) => isRecord(item) && item.role === 'assistant');
    const authoritativeText = finalMessage && isRecord(finalMessage)
      ? textFromContent(finalMessage.content ?? finalMessage.text)
      : eventText(event);
    const next = [...messages];
    const index = lastIndexOf((message) => message.kind === 'assistant' && message.status === 'streaming');
    if (index >= 0) {
      next[index] = { ...next[index], text: authoritativeText || next[index].text, status: 'complete' };
      return next.filter((message, candidateIndex) => !(candidateIndex !== index && message.kind === 'assistant' && message.status === 'streaming' && !message.text.trim()));
    }
    return authoritativeText ? [...messages, { id: `assistant-${now}`, role: 'assistant', kind: 'assistant', text: authoritativeText, status: 'complete', createdAt: now }] : messages;
  }

  if (event.type === 'error') {
    const message = eventText(event) || 'Gateway reported an error.';
    const next = [...messages];
    const index = lastIndexOf((item) => item.kind === 'assistant' && item.status === 'streaming');
    if (index >= 0) {
      const current = next[index];
      next[index] = { ...current, text: current.text || message, status: 'error' };
      return next;
    }
    return [...messages, { id: `system-error-${now}`, role: 'system', kind: 'system', text: message, status: 'error', createdAt: now }];
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
