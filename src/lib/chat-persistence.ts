import type { ChatMessage, ChatModelIdentity } from './chat-protocol';
import { extractSessionModel } from './chat-protocol';

const STORAGE_KEY = 'mission-control-chat-drawer-v1';

export type PersistedChat = {
  sessionId: string | null;
  sessionKey: string | null;
  modelIdentity: ChatModelIdentity | null;
  messages: ChatMessage[];
  updatedAt: number;
};

export function readPersistedChat(): PersistedChat {
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

export function persistChat(sessionId: string | null, sessionKey: string | null, modelIdentity: ChatModelIdentity | null, messages: ChatMessage[]) {
  try {
    window.localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({ sessionId, sessionKey, modelIdentity, messages: messages.slice(-200), updatedAt: Date.now() }),
    );
  } catch {
    // Storage is best effort; the live session remains authoritative.
  }
}

export function syncLastChatToServer(sessionId: string | null, sessionKey: string | null, modelIdentity: ChatModelIdentity | null, storedToken: string) {
  if (!sessionId || !sessionId.trim()) return;
  try {
    void fetch('/api/local/chat/last', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...(storedToken ? { Authorization: `Bearer ${storedToken}` } : {}),
      },
      body: JSON.stringify({ sessionId, sessionKey, modelIdentity, updatedAt: Date.now() }),
    }).catch(() => {});
  } catch { /* best effort */ }
}

type ServerLastChat = { sessionId: string; sessionKey?: string | null; updatedAt?: number };

export async function fetchServerLastChat(storedToken: string): Promise<ServerLastChat | null> {
  try {
    const res = await fetch('/api/local/chat/last', {
      headers: storedToken ? { Authorization: `Bearer ${storedToken}` } : {},
    });
    if (!res.ok) return null;
    const data = await res.json() as { lastChat?: ServerLastChat | null };
    return data.lastChat ?? null;
  } catch {
    return null;
  }
}
