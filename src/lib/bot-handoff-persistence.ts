import type { BotHandoffStatus } from '../components/chat/BotHandoffMessage';

export type PersistedBotHandoff = {
  id: string;
  handle: string;
  targetSessionId?: string;
  displayName?: string;
  model?: string;
  provider?: string;
  request: string;
  status: BotHandoffStatus;
  reply?: string | null;
  error?: string | null;
  createdAt?: number;
  updatedAt: number;
};

function authHeaders(accessToken: string): HeadersInit {
  return accessToken ? { Accept: 'application/json', Authorization: `Bearer ${accessToken}` } : { Accept: 'application/json' };
}

export async function loadPersistedBotHandoffs(accessToken: string, sessionId: string): Promise<PersistedBotHandoff[]> {
  if (!sessionId.trim()) return [];
  try {
    const response = await fetch(`/api/local/chat/handoffs?session_id=${encodeURIComponent(sessionId)}`, {
      headers: authHeaders(accessToken),
      cache: 'no-store',
    });
    if (!response.ok) return [];
    const payload = await response.json() as { handoffs?: unknown };
    return Array.isArray(payload.handoffs)
      ? payload.handoffs.filter((item): item is PersistedBotHandoff => Boolean(item) && typeof item === 'object' && typeof (item as PersistedBotHandoff).id === 'string')
      : [];
  } catch {
    return [];
  }
}

export async function persistBotHandoff(accessToken: string, sessionId: string, handoff: PersistedBotHandoff): Promise<void> {
  const sessionIds = [...new Set([sessionId.trim(), handoff.targetSessionId?.trim() || ''].filter(Boolean))];
  if (sessionIds.length === 0) return;
  await Promise.all(sessionIds.map(async (targetSessionId) => {
    try {
      await fetch('/api/local/chat/handoffs', {
        method: 'POST',
        headers: { ...authHeaders(accessToken), 'Content-Type': 'application/json' },
        cache: 'no-store',
        body: JSON.stringify({ session_id: targetSessionId, handoff }),
      });
    } catch {
      // The live handoff remains authoritative if the sidecar is unavailable.
    }
  }));
}
