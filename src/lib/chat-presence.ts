import { useEffect, useState } from 'react';

export type ChatPresencePhase = 'idle' | 'running' | 'completed' | 'waiting' | 'unread';

export type ChatPresence = {
  sessionKey: string | null;
  sessionTitle: string | null;
  phase: ChatPresencePhase;
  verb: string | null;
  preview: string | null;
  unreadCount: number;
  updatedAt: number;
};

export const CHAT_PRESENCE_EVENT = 'mission-control:chat-presence';
const PRESENCE_KEY = 'mission-control-chat-presence-v1';
const READ_STATE_KEY = 'mission-control-chat-read-state-v1';
const EVENT_NAME = CHAT_PRESENCE_EVENT;

const EMPTY_PRESENCE: ChatPresence = {
  sessionKey: null,
  sessionTitle: null,
  phase: 'idle',
  verb: null,
  preview: null,
  unreadCount: 0,
  updatedAt: 0,
};

function readPresence(): ChatPresence {
  try {
    const parsed = JSON.parse(window.localStorage.getItem(PRESENCE_KEY) || 'null') as Partial<ChatPresence> | null;
    if (!parsed || typeof parsed !== 'object') return EMPTY_PRESENCE;
    return { ...EMPTY_PRESENCE, ...parsed };
  } catch {
    return EMPTY_PRESENCE;
  }
}

export function publishChatPresence(next: Omit<ChatPresence, 'updatedAt'>) {
  const presence: ChatPresence = { ...next, updatedAt: Date.now() };
  try {
    window.localStorage.setItem(PRESENCE_KEY, JSON.stringify(presence));
    window.dispatchEvent(new CustomEvent<ChatPresence>(EVENT_NAME, { detail: presence }));
  } catch {
    // Presence is a UI enhancement; chat must remain usable if storage is unavailable.
  }
}

export type ChatReadState = {
  sessionKey: string | null;
  assistantCount: number;
};

function readPersistedChat(): { sessionKey: string | null; assistantCount: number } {
  try {
    const parsed = JSON.parse(window.localStorage.getItem('mission-control-chat-drawer-v1') || 'null') as { sessionKey?: unknown; messages?: Array<{ role?: unknown; status?: unknown }> } | null;
    return {
      sessionKey: typeof parsed?.sessionKey === 'string' ? parsed.sessionKey : null,
      assistantCount: (parsed?.messages || []).filter((message) => message.role === 'assistant' && message.status !== 'streaming').length,
    };
  } catch {
    return { sessionKey: null, assistantCount: 0 };
  }
}

export function getChatReadState(): ChatReadState {
  try {
    const parsed = JSON.parse(window.localStorage.getItem(READ_STATE_KEY) || 'null') as Partial<ChatReadState> | null;
    return {
      sessionKey: typeof parsed?.sessionKey === 'string' ? parsed.sessionKey : null,
      assistantCount: typeof parsed?.assistantCount === 'number' ? parsed.assistantCount : 0,
    };
  } catch {
    return { sessionKey: null, assistantCount: 0 };
  }
}

export function markChatPresenceRead(sessionKey?: string | null, assistantCount?: number) {
  try {
    const persisted = readPersistedChat();
    window.localStorage.setItem(READ_STATE_KEY, JSON.stringify({
      sessionKey: sessionKey ?? persisted.sessionKey,
      assistantCount: assistantCount ?? persisted.assistantCount,
    } satisfies ChatReadState));
    const current = readPresence();
    publishChatPresence({ ...current, phase: current.phase === 'unread' || current.phase === 'completed' ? 'idle' : current.phase, unreadCount: 0 });
  } catch {
    // Ignore storage failures.
  }
}

export function useChatPresence() {
  const [presence, setPresence] = useState<ChatPresence>(() => readPresence());

  useEffect(() => {
    const update = (event: Event) => {
      if (event instanceof CustomEvent) setPresence(event.detail as ChatPresence);
      else setPresence(readPresence());
    };
    window.addEventListener(EVENT_NAME, update);
    window.addEventListener('storage', update);
    return () => {
      window.removeEventListener(EVENT_NAME, update);
      window.removeEventListener('storage', update);
    };
  }, []);

  return presence;
}
