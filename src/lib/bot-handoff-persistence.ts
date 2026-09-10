import type { BotHandoffStatus } from '../components/chat/BotHandoffMessage';
import type { BotHandoffFailureReason } from './bot-handoff-reasons';

export type PersistedBotHandoff = {
  id: string;
  handle: string;
  targetSessionId?: string;
  displayName?: string;
  model?: string;
  provider?: string;
  request: string;
  status: BotHandoffStatus;
  reason?: BotHandoffFailureReason;
  retryable?: boolean;
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

export async function claimBotHandoff(accessToken: string, sessionId: string, handoff: PersistedBotHandoff): Promise<boolean | null> {
  try {
    const response = await fetch('/api/local/chat/handoffs/claim', {
      method: 'POST',
      headers: { ...authHeaders(accessToken), 'Content-Type': 'application/json' },
      cache: 'no-store',
      body: JSON.stringify({ session_id: sessionId, handoff }),
    });
    if (!response.ok) return null;
    const payload = await response.json() as { accepted?: unknown };
    return payload.accepted === true;
  } catch {
    // Sidecar outage must not make the live gateway handoff unusable.
    return null;
  }
}

export async function loadAllPersistedBotHandoffs(accessToken: string): Promise<Array<{ sessionId: string; handoff: PersistedBotHandoff }>> {
  try {
    const response = await fetch('/api/local/chat/handoffs/all', {
      headers: authHeaders(accessToken),
      cache: 'no-store',
    });
    if (!response.ok) return [];
    const payload = await response.json() as { handoffs?: unknown };
    return Array.isArray(payload.handoffs)
      ? payload.handoffs.filter((item): item is { sessionId: string; handoff: PersistedBotHandoff } => Boolean(item) && typeof item === 'object' && typeof (item as { sessionId?: unknown }).sessionId === 'string' && typeof (item as { handoff?: unknown }).handoff === 'object')
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
