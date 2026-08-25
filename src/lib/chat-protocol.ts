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
  role: ChatRole;
  kind?: ChatMessageKind;
  text: string;
  status?: 'streaming' | 'complete' | 'error' | 'interrupted';
  createdAt: number;
  attachments?: ChatAttachmentSummary[];
  detail?: string;
  output?: string;
  toolId?: string;
  toolName?: string;
  toolInput?: string;
  durationS?: number;
};

export type GatewayTranscriptMessage = {
  role?: unknown;
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

export function normalizeTranscript(messages: GatewayTranscriptMessage[], now = Date.now()): ChatMessage[] {
  const normalized: ChatMessage[] = [];
  messages.forEach((message, index) => {
    const sourceRole = safeRole(message.role);
    const rawText = textFromContent(message.text) || textFromContent(message.content);
    const role = sourceRole === 'user' && isSystemNotification(rawText) ? 'system' : sourceRole;
    const displayKind = stringValue(message.display_kind);
    if (displayKind === 'hidden') return;

    if (displayKind === 'model_switch' || displayKind === 'auto_continue' || displayKind === 'async_delegation_complete') {
      const labels: Record<string, string> = {
        model_switch: 'Model changed',
        auto_continue: 'Resumed interrupted turn',
        async_delegation_complete: 'Background agent work finished',
      };
      normalized.push({
        id: `restored-event-${now}-${index}`,
        role: 'system',
        kind: 'event',
        text: labels[displayKind],
        status: 'complete',
        createdAt: now + index,
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
        id: `restored-tool-${now}-${index}`,
        role: 'tool',
        kind: 'tool',
        toolName,
        toolId: stringValue(message.tool_call_id) || undefined,
        text: context,
        toolInput,
        output: output || undefined,
        durationS,
        status: 'complete',
        createdAt: now + index,
      });
      return;
    }

    const reasoning = role === 'assistant' ? reasoningFromMessage(message) : '';
    if (reasoning) {
      normalized.push({
        id: `restored-reasoning-${now}-${index}`,
        role: 'tool',
        kind: 'reasoning',
        text: reasoning,
        status: 'complete',
        createdAt: now + index,
      });
    }

    if (!rawText.trim() && role !== 'assistant' && role !== 'user') return;
    normalized.push({
      id: `restored-${now}-${index}`,
      role,
      kind: role === 'assistant' || role === 'user' || role === 'system' ? role : undefined,
      text: rawText,
      status: 'complete',
      createdAt: now + index,
    });
  });
  return normalized;
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
    return [...messages, { id: `reasoning-${now}`, role: 'tool', kind: 'reasoning', text: delta, status: 'streaming', createdAt: now }];
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
    return [...messages, { id: `reasoning-${now}`, role: 'tool', kind: 'reasoning', text, status: 'complete', createdAt: now }];
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
