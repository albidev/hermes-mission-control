export type HandoffEvent = {
  type: string;
  seq?: number;
  payload?: Record<string, unknown>;
};

export type HandoffEventRpc = {
  eventsSince(params: { session_id: string; last_seen: number }): Promise<{
    events?: HandoffEvent[];
    truncated?: boolean;
    epoch?: string | null;
  }>;
};

export type HandoffObserverOptions = {
  sessionId: string;
  intervalMs?: number;
  initialLastSeen?: number;
  onEvent: (event: HandoffEvent) => void;
  onComplete: (text: string) => void;
  onError: (message: string) => void;
};

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

function extractText(payload: Record<string, unknown> | undefined): string {
  if (!payload) return '';
  const reason = typeof payload.reason === 'string' && payload.reason.trim() ? `[reason: ${payload.reason.trim()}] ` : '';
  if (typeof payload.text === 'string') return reason + payload.text;
  if (typeof payload.content === 'string') return reason + payload.content;
  if (typeof payload.message === 'string') return reason + payload.message;
  if (reason) return reason.trim();
  return '';
}

export function createHandoffObserver(rpc: HandoffEventRpc, options: HandoffObserverOptions) {
  const intervalMs = options.intervalMs ?? 1200;
  let timer: ReturnType<typeof setTimeout> | null = null;
  let lastSeen = Math.max(0, Math.floor(options.initialLastSeen ?? 0));
  let stopped = false;
  let settled = false;

  async function poll(): Promise<void> {
    if (stopped || settled) return;
    try {
      const result = await rpc.eventsSince({ session_id: options.sessionId, last_seen: lastSeen });
      if (stopped || settled) return;
      const events = Array.isArray(result.events) ? result.events : [];
      for (const event of events) {
        if (stopped || settled) return;
        lastSeen = Math.max(lastSeen, event.seq ?? 0);
        options.onEvent(event);
        if (event.type === 'error') {
          settled = true;
          options.onError(extractText(event.payload) || 'Handoff failed.');
          return;
        }
        if (COMPLETE_TYPES.has(event.type)) {
          settled = true;
          options.onComplete(extractText(event.payload));
          return;
        }
      }
    } catch (err) {
      if (stopped || settled) return;
      settled = true;
      options.onError(err instanceof Error ? err.message : 'Handoff observation failed.');
    }
  }

  return {
    async start(): Promise<void> {
      stopped = false;
      await poll();
      if (stopped || settled) return;
      timer = setInterval(() => { void poll(); }, intervalMs);
    },
    async stop(): Promise<void> {
      stopped = true;
      if (timer !== null) {
        clearInterval(timer);
        timer = null;
      }
    },
  };
}
