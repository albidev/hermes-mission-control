import type { ChatMessage, ChatModelIdentity } from './chat-protocol';
import { extractSessionModel } from './chat-protocol';
import { buildLastChatClaimPayload, normalizeServerLastChat, type ServerLastChat } from './chat-bootstrap';

const STORAGE_KEY = 'mission-control-chat-drawer-v1';

export type PersistedChat = {
  sessionId: string | null;
  sessionKey: string | null;
  modelIdentity: ChatModelIdentity | null;
  messages: ChatMessage[];
  updatedAt: number;
  revision: number | null;
};

const emptyPersistedChat = (): PersistedChat => ({
  sessionId: null,
  sessionKey: null,
  modelIdentity: null,
  messages: [],
  updatedAt: 0,
  revision: null,
});

export function readPersistedChat(): PersistedChat {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return emptyPersistedChat();
    const parsed = JSON.parse(raw) as Partial<PersistedChat>;
    return {
      sessionId: typeof parsed.sessionId === 'string' ? parsed.sessionId : null,
      sessionKey: typeof parsed.sessionKey === 'string' ? parsed.sessionKey : null,
      modelIdentity: extractSessionModel(parsed.modelIdentity),
      messages: Array.isArray(parsed.messages) ? (parsed.messages as ChatMessage[]) : [],
      updatedAt: typeof parsed.updatedAt === 'number' ? parsed.updatedAt : 0,
      revision: typeof parsed.revision === 'number' && Number.isInteger(parsed.revision) && parsed.revision > 0
        ? parsed.revision
        : null,
    };
  } catch {
    return emptyPersistedChat();
  }
}

export function persistChat(
  sessionId: string | null,
  sessionKey: string | null,
  modelIdentity: ChatModelIdentity | null,
  messages: ChatMessage[],
  revision: number | null = null,
) {
  try {
    window.localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({
        sessionId,
        sessionKey,
        modelIdentity,
        messages: messages.slice(-200),
        updatedAt: Date.now(),
        revision,
      }),
    );
  } catch {
    // Storage is best effort; the live session remains authoritative.
  }
}

export type LastChatClaimResult = {
  accepted: boolean;
  lastChat: ServerLastChat | null;
};

function parseLastChatPayload(value: unknown): ServerLastChat | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const record = value as { lastChat?: unknown };
  return normalizeServerLastChat(record.lastChat);
}

/**
 * Claim the shared pointer with CAS. This remains best-effort for transport
 * failures, but a 409 is returned to the caller so it can adopt the canonical
 * pointer instead of silently continuing with stale local state.
 */
export async function syncLastChatToServer(
  sessionId: string | null,
  sessionKey: string | null,
  modelIdentity: ChatModelIdentity | null,
  storedToken: string,
  expectedRevision: number | null = null,
): Promise<LastChatClaimResult> {
  if (!sessionId || !sessionId.trim()) return { accepted: false, lastChat: null };
  try {
    const body = buildLastChatClaimPayload(sessionId, sessionKey, modelIdentity, expectedRevision);
    const res = await fetch('/api/local/chat/last', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...(storedToken ? { Authorization: `Bearer ${storedToken}` } : {}),
      },
      body: JSON.stringify(body),
    });
    const payload = await res.json().catch(() => null);
    const lastChat = parseLastChatPayload(payload);
    return { accepted: res.ok, lastChat };
  } catch {
    return { accepted: false, lastChat: null };
  }
}

export async function fetchServerLastChat(storedToken: string, signal?: AbortSignal): Promise<ServerLastChat | null> {
  try {
    const res = await fetch('/api/local/chat/last', {
      headers: storedToken ? { Authorization: `Bearer ${storedToken}` } : {},
      ...(signal ? { signal } : {}),
    });
    if (!res.ok) return null;
    const data = await res.json() as { lastChat?: unknown };
    return parseLastChatPayload(data);
  } catch {
    return null;
  }
}
