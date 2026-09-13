/**
 * Pure building blocks for delivering a Bot handoff reply into the origin session.
 *
 * Kept free of React and of the gateway transport so both can be exercised directly:
 * the delivery payload is a value, and the observer registry is a plain container.
 */

export type BotReplyDeliveryPayload = {
  session_id: string;
  text: string;
  display_kind: 'hidden';
};

export type BotReplyDeliveryInput = {
  originSessionId: string;
  replyText: string;
  botHandle: string;
  requestText: string;
};

export const BOT_REPLY_MARKER = '[BOT HANDOFF RESULT';

/**
 * Build the `prompt.submit` payload that lands the reply in the origin transcript.
 *
 * Returns null when there is nothing deliverable, so callers can decline without
 * inventing an empty turn.
 *
 * `originSessionId` must be the id captured when the user SENT the handoff: a settling
 * observer can fire minutes later, by which point re-deriving the session would resolve
 * whatever chat the user has since opened.
 *
 * `display_kind: 'hidden'` keeps the injected row out of the rendered transcript — the
 * reply already renders as the attributed bot-reply card, so the row exists for the model
 * and for search, not for painting.
 */
export function buildBotReplyDelivery(input: BotReplyDeliveryInput): BotReplyDeliveryPayload | null {
  const originSessionId = String(input.originSessionId ?? '').trim();
  const replyText = String(input.replyText ?? '').trim();
  if (!originSessionId || !replyText) return null;
  const botHandle = String(input.botHandle ?? '').trim();
  const requestText = String(input.requestText ?? '').trim();
  return {
    session_id: originSessionId,
    display_kind: 'hidden',
    text: [
      `${BOT_REPLY_MARKER} — @${botHandle}]`,
      `Request: ${requestText}`,
      '',
      'Reply:',
      replyText,
    ].join('\n'),
  };
}

export type HandoffObserver = { start: () => void | Promise<void>; stop: () => void | Promise<void> };

export type HandoffObserverRegistry = {
  set(handoffId: string, observer: HandoffObserver): void;
  get(handoffId: string): HandoffObserver | undefined;
  delete(handoffId: string): boolean;
  size(): number;
};

/**
 * One observer per in-flight handoff.
 *
 * A single shared slot is wrong for a multi-mention submit: fanning out N handoffs would
 * let the last writer orphan every earlier observer, leaving its socket unclosed and its
 * completion undelivered. Keying by handoff id makes concurrent mentions independent.
 */
export function createHandoffObserverRegistry(): HandoffObserverRegistry {
  const observers = new Map<string, HandoffObserver>();
  return {
    set(handoffId, observer) {
      const key = String(handoffId ?? '').trim();
      if (key) observers.set(key, observer);
    },
    get(handoffId) {
      return observers.get(String(handoffId ?? '').trim());
    },
    delete(handoffId) {
      return observers.delete(String(handoffId ?? '').trim());
    },
    size() {
      return observers.size;
    },
  };
}
