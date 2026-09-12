import type { HandoffEvent } from './bot-handoff-observer';

const COMPLETE_TYPES = new Set([
  'message.complete',
  'message.completed',
  'message.done',
  'assistant.completed',
  'assistant.done',
  'response.completed',
  'response.done',
  'run.completed',
  'run.finished',
  'run.done',
]);

function eventText(event: HandoffEvent): string {
  const payload = event.payload;
  if (!payload) return '';
  if (typeof payload.text === 'string') return payload.text;
  if (typeof payload.content === 'string') return payload.content;
  if (typeof payload.message === 'string') return payload.message;
  return '';
}

/**
 * Recover a completion already written by Hermes in a canonical Bot Chat.
 * Matching is fail-closed: without the handoff marker we never attribute an
 * unrelated historical completion to the current request.
 */
export function findHandoffCompletion(events: HandoffEvent[], handoffId: string): string | null {
  const marker = handoffId.trim();
  if (!marker) return null;
  const markerIndex = events.findIndex((event) => {
    try {
      return JSON.stringify(event).includes(marker);
    } catch {
      return false;
    }
  });
  if (markerIndex < 0) return null;
  for (const event of events.slice(markerIndex)) {
    if (!COMPLETE_TYPES.has(event.type)) continue;
    const text = eventText(event).trim();
    if (text) return text;
  }
  return null;
}
