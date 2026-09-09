export type RuntimePresencePhase = 'connected' | 'running' | 'closed';

type RuntimePresencePayload = {
  runtimeSessionId: string;
  resumedFrom: string;
  sessionKey?: string | null;
  phase: RuntimePresencePhase;
  source?: string;
  title?: string | null;
  model?: string | null;
};

const CLIENT_ID_STORAGE_KEY = 'mission-control-runtime-presence-client-id';

function getClientId(): string {
  try {
    const existing = window.localStorage.getItem(CLIENT_ID_STORAGE_KEY);
    if (existing) return existing;
    const created = typeof crypto?.randomUUID === 'function'
      ? crypto.randomUUID()
      : `mc-${Date.now()}-${Math.random().toString(36).slice(2)}`;
    window.localStorage.setItem(CLIENT_ID_STORAGE_KEY, created);
    return created;
  } catch {
    return `mc-${Date.now()}-${Math.random().toString(36).slice(2)}`;
  }
}

/**
 * Best-effort runtime lease. It is intentionally independent from the shared
 * last-chat pointer: a failed presence call must never interrupt chat or alter
 * cross-device adoption.
 */
export async function publishChatRuntimePresence(
  storedToken: string,
  payload: RuntimePresencePayload,
): Promise<void> {
  if (!payload.runtimeSessionId.trim() || !payload.resumedFrom.trim()) return;
  try {
    await fetch('/api/local/chat/presence', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...(storedToken ? { Authorization: `Bearer ${storedToken}` } : {}),
      },
      body: JSON.stringify({
        ...payload,
        clientId: getClientId(),
      }),
      keepalive: payload.phase === 'closed',
    });
  } catch {
    // Presence is advisory. The chat transport and pointer must remain intact.
  }
}
