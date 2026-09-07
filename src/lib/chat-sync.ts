import type { ChatMessage, ChatTimestampMetadata, GatewayEvent } from './chat-protocol';

export type ChatSyncEnvelope = {
  session_id: string;
  relay_seq: number;
  dedupe_key: string;
  kind: 'gateway_event' | 'user_message' | 'system_message';
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
  kind: 'gateway_event' | 'user_message' | 'system_message',
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

export async function fetchChatTimestampMetadata(
  accessToken: string,
  sessionId: string | null,
  sessionKey: string | null,
): Promise<ChatTimestampMetadata[]> {
  const params = new URLSearchParams();
  if (sessionId?.trim()) params.set('session_id', sessionId.trim());
  if (sessionKey?.trim()) params.set('session_key', sessionKey.trim());
  if (!params.toString()) return [];
  try {
    const response = await fetch(`/api/local/chat/timestamps?${params.toString()}`, {
      headers: accessToken ? { Accept: 'application/json', Authorization: `Bearer ${accessToken}` } : { Accept: 'application/json' },
      cache: 'no-store',
    });
    if (!response.ok) return [];
    const payload = await response.json() as { messages?: unknown };
    return Array.isArray(payload.messages)
      ? payload.messages.filter((item): item is ChatTimestampMetadata => Boolean(item) && typeof item === 'object')
      : [];
  } catch {
    // The sidecar is an enrichment source; direct gateway resume remains authoritative.
    return [];
  }
}

export function applySyncedChatMessage(messages: ChatMessage[], message: ChatMessage): ChatMessage[] {
  if (messages.some((candidate) => candidate.id === message.id)) return messages;
  return [...messages, message];
}

export const applySyncedUserMessage = applySyncedChatMessage;

function messageKey(message: ChatMessage): string {
  if (message.kind === 'tool' || message.role === 'tool') {
    if (message.toolId) return `tool:${message.toolId}`;
    return `tool:${message.toolName || ''}:${message.toolInput || ''}:${message.output || ''}`;
  }
  return `${message.role}:${message.kind || ''}:${message.text}:${message.attachments?.map((item) => item.name).join(',') || ''}`;
}

function findMatchingMessage(messages: ChatMessage[], candidate: ChatMessage, excluded = new Set<number>()): number {
  const byId = messages.findIndex((message, index) => !excluded.has(index) && message.id === candidate.id);
  if (byId >= 0) return byId;
  if (candidate.kind === 'assistant' && candidate.status === 'streaming') {
    for (let index = messages.length - 1; index >= 0; index -= 1) {
      if (!excluded.has(index) && messages[index].kind === 'assistant' && messages[index].status === 'streaming') return index;
    }
  }
  const key = messageKey(candidate);
  return messages.findIndex((message, index) => !excluded.has(index) && messageKey(message) === key);
}

function mergeStreamingMessage(durable: ChatMessage, local: ChatMessage): ChatMessage {
  if (local.kind === 'assistant' && local.status === 'streaming') {
    const text = local.text.length >= durable.text.length ? local.text : durable.text;
    return { ...durable, ...local, text, status: 'streaming', createdAt: durable.createdAt };
  }
  if (local.kind === 'tool' && local.status === 'streaming') {
    return {
      ...durable,
      ...local,
      output: local.output || durable.output,
      detail: [durable.detail, local.detail].filter(Boolean).join('\n') || undefined,
      status: 'streaming',
      createdAt: durable.createdAt,
    };
  }
  return durable;
}

function mergeMessagePair(durable: ChatMessage, local: ChatMessage): ChatMessage {
  if (local.status === 'streaming') return mergeStreamingMessage(durable, local);
  if (durable.status === 'streaming') return mergeStreamingMessage(local, durable);
  // Keep local-only fields (for example optimistic attachments/details), while
  // allowing the snapshot to provide the durable status and canonical content.
  return { ...local, ...durable };
}

/** Merge a server snapshot without dropping any visible local message. */
export function mergeDurableChatMessages(local: ChatMessage[], durable: ChatMessage[]): ChatMessage[] {
  if (durable.length === 0) return local;

  // Start with the live transcript. This is important for repeated reconciliation:
  // an omitted streaming/relay row is already visible and must not be appended
  // again every time the same partial snapshot arrives.
  const merged = local.map((message) => ({ ...message }));
  const sourceRank = local.map(() => 1); // durable rows sort before local rows on ties.
  const consumed = new Set<number>();

  for (const durableMessage of durable) {
    const match = findMatchingMessage(merged, durableMessage, consumed);
    if (match >= 0) {
      consumed.add(match);
      merged[match] = mergeMessagePair(durableMessage, merged[match]);
      continue;
    }
    merged.push({ ...durableMessage });
    sourceRank.push(0);
    // A duplicate row in the snapshot is a separate transcript row, not a
    // second match against the row just appended.
    consumed.add(merged.length - 1);
  }

  // A partial snapshot can contain rows on either side of a relay event. Sort
  // the union by the message timestamps while retaining deterministic ordering
  // for equal timestamps (snapshot rows first, then already-visible local rows).
  return merged
    .map((message, index) => ({ message, index, source: sourceRank[index] ?? 1 }))
    .sort((left, right) => {
      const leftTime = typeof left.message.createdAt === 'number' && Number.isFinite(left.message.createdAt)
        ? left.message.createdAt
        : Number.POSITIVE_INFINITY;
      const rightTime = typeof right.message.createdAt === 'number' && Number.isFinite(right.message.createdAt)
        ? right.message.createdAt
        : Number.POSITIVE_INFINITY;
      if (leftTime !== rightTime) return leftTime - rightTime;
      if (left.source !== right.source) return left.source - right.source;
      return left.index - right.index;
    })
    .map(({ message }) => message);
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
