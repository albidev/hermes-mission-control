import type { ChatMessage } from './chat-protocol';
import type { GuestLogEvent } from './room-guest-api';

/** Keep wire events intact; only adapt the presentation, never rewrite their identity/sequence. */
export function guestEventToChatMessage(event: GuestLogEvent): ChatMessage {
  const payload = event.payload;
  const fields = payload && typeof payload === 'object' ? payload as Record<string, unknown> : {};
  const text = typeof payload === 'string' ? payload
    : typeof fields.text === 'string' ? fields.text : JSON.stringify(payload);
  const timestamp = Date.parse(event.created_at);
  return {
    id: event.event_id,
    role: 'assistant',
    kind: 'assistant',
    text,
    createdAt: Number.isFinite(timestamp) ? timestamp : null,
    attribution: { handle: event.actor, displayName: event.actor },
  };
}

export const defaultGuestTab = 'bot' as const;
export function guestOfflineTime(value?: string | null): string {
  if (!value) return '—';
  if (/^\d{2}:\d{2}$/.test(value)) return value;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}
