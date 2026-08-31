import type { ChatMessage, GatewayEvent } from './chat-protocol';

export type ChatSyncEnvelope = {
  session_id: string;
  relay_seq: number;
  dedupe_key: string;
  kind: 'gateway_event' | 'user_message';
  payload: Record<string, unknown>;
};

const CHAT_SYNC_CLIENT_KEY = 'mission-control-chat-sync-client-v1';

let inMemoryClientId: string | null = null;
let publishQueue: Promise<void> = Promise.resolve();

export function getChatSyncClientId(): string {
  if (inMemoryClientId) return inMemoryClientId;
  try {
    const existing = window.sessionStorage.getItem(CHAT_SYNC_CLIENT_KEY);
    if (existing?.trim()) {
      inMemoryClientId = existing;
      return existing;
    }
  } catch {
    // Private browsing can deny sessionStorage; the in-memory id is enough.
  }
  const generated = typeof crypto?.randomUUID === 'function'
    ? crypto.randomUUID()
    : `mc-${Date.now()}-${Math.random().toString(36).slice(2)}`;
  inMemoryClientId = generated;
  try {
    window.sessionStorage.setItem(CHAT_SYNC_CLIENT_KEY, generated);
  } catch {
    // Continue with the in-memory id.
  }
  return generated;
}

export function chatSyncStreamUrl(sessionId: string, accessToken: string, since?: number): string {
  const params = new URLSearchParams({
    session_id: sessionId,
    client_id: getChatSyncClientId(),
    access_token: accessToken,
  });
  if (typeof since === 'number' && Number.isFinite(since) && since >= 0) params.set('since', String(Math.floor(since)));
  return `/api/local/chat/sync/stream?${params.toString()}`;
}

export function publishChatSync(
  accessToken: string,
  sessionId: string,
  kind: 'gateway_event' | 'user_message',
  payload: Record<string, unknown>,
  dedupeKey?: string,
): Promise<void> {
  const body: Record<string, unknown> = {
    session_id: sessionId,
    client_id: getChatSyncClientId(),
    kind,
    ...(kind === 'gateway_event' ? { event: payload } : { message: payload }),
  };
  if (dedupeKey) body.dedupe_key = dedupeKey;
  publishQueue = publishQueue.then(async () => {
    try {
      const response = await fetch('/api/local/chat/sync/publish', {
        method: 'POST',
        headers: {
          Accept: 'application/json',
          Authorization: `Bearer ${accessToken}`,
          'Content-Type': 'application/json',
        },
        cache: 'no-store',
        body: JSON.stringify(body),
      });
      if (!response.ok) return;
    } catch {
      // The direct gateway remains authoritative if the sidecar is unavailable.
    }
  });
  return publishQueue;
}

export function applySyncedUserMessage(messages: ChatMessage[], message: ChatMessage): ChatMessage[] {
  if (messages.some((candidate) => candidate.id === message.id)) return messages;
  return [...messages, message];
}

function messageKey(message: ChatMessage): string {
  if (message.kind === 'tool' || message.role === 'tool') {
    if (message.toolId) return `tool:${message.toolId}`;
    return `tool:${message.toolName || ''}:${message.toolInput || ''}:${message.output || ''}`;
  }
  return `${message.role}:${message.kind || ''}:${message.text}:${message.attachments?.map((item) => item.name).join(',') || ''}`;
}

function findMatchingMessage(messages: ChatMessage[], candidate: ChatMessage): number {
  if (candidate.kind === 'assistant' && candidate.status === 'streaming') {
    for (let index = messages.length - 1; index >= 0; index -= 1) {
      if (messages[index].kind === 'assistant') return index;
    }
  }
  const key = messageKey(candidate);
  return messages.findIndex((message) => messageKey(message) === key);
}

function mergeStreamingMessage(durable: ChatMessage, local: ChatMessage): ChatMessage {
  if (local.kind === 'assistant' && local.status === 'streaming') {
    const text = local.text.length >= durable.text.length ? local.text : durable.text;
    return { ...durable, ...local, text, status: 'streaming' };
  }
  if (local.kind === 'tool' && local.status === 'streaming') {
    return {
      ...durable,
      ...local,
      output: local.output || durable.output,
      detail: [durable.detail, local.detail].filter(Boolean).join('\n') || undefined,
      status: 'streaming',
    };
  }
  return durable;
}

/** Merge a server snapshot without dropping optimistic or currently streaming local messages. */
export function mergeDurableChatMessages(local: ChatMessage[], durable: ChatMessage[]): ChatMessage[] {
  if (durable.length === 0) return local;
  const merged = durable.map((message) => ({ ...message }));
  const consumed = new Set<number>();

  for (const localMessage of local) {
    const match = findMatchingMessage(merged, localMessage);
    if (match >= 0 && !consumed.has(match)) {
      consumed.add(match);
      if (localMessage.status === 'streaming') merged[match] = mergeStreamingMessage(merged[match], localMessage);
      continue;
    }
    // Preserve local user/tool/streaming messages until the next snapshot contains them.
    if (localMessage.role === 'user' || localMessage.role === 'tool' || localMessage.status === 'streaming') {
      merged.push(localMessage);
    }
  }

  return merged;
}

/** Record a sequenced event only when it advances the watermark for its session. */
export function shouldApplySequencedEvent(watermarks: Map<string, number>, event: GatewayEvent): boolean {
  const sid = event.session_id;
  const seq = event.seq;
  if (!sid || typeof seq !== 'number' || !Number.isFinite(seq)) return true;
  const previous = watermarks.get(sid) ?? 0;
  if (seq <= previous) return false;
  watermarks.set(sid, seq);
  return true;
}
