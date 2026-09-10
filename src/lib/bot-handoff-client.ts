import {
  createRpcRequest,
  getRpcErrorMessage,
  isResponseFor,
  parseGatewayFrame,
} from './chat-protocol';
import { getWebSocketUrl, mintWsCredential, RPC_TIMEOUT_MS } from './chat-transport';
import { createBotChatResolver, type BotChatResult } from './bot-chat-routing';

const BOT_RELAY_TIMEOUT_MS = 1_380_000;

export type HandoffClientOptions = {
  accessToken?: string;
  onEvent?: (event: { type: string; seq?: number; payload?: Record<string, unknown> }) => void;
};

export type HandoffClient = {
  resolveCanonical(profile: string, knownCanonicalId?: string | null): Promise<BotChatResult>;
  deliver(profile: string, text: string): Promise<{ reply: string; deferred: boolean }>;
  resume(profile: string, sessionId: string): Promise<string>;
  closeSession(sessionId: string): Promise<void>;
  submit(text: string): Promise<void>;
  eventsSince(lastSeen: number): Promise<{ events?: Array<{ type: string; seq?: number; payload?: Record<string, unknown> }>; truncated?: boolean; epoch?: string | null; latest_seq?: number }>;
  close(): void;
};

/**
 * A persistent WebSocket client for a single Bot handoff. The gateway's
 * ws-orphan reaper (grace 0 on this host) kills a session the moment its
 * transport closes, so resume → submit → observation MUST share one socket
 * that stays open until the handoff settles.
 */
export async function openHandoffClient(options: HandoffClientOptions = {}): Promise<HandoffClient> {
  const credential = await mintWsCredential(options.accessToken ?? '');
  const socket = new WebSocket(getWebSocketUrl(credential));
  const pending = new Map<string, { resolve: (value: unknown) => void; reject: (reason: Error) => void; timer: number }>();
  let requestSeq = 0;
  let closed = false;
  let runtimeId = '';

  const rpc = <T>(method: string, params: Record<string, unknown>, timeoutMs = RPC_TIMEOUT_MS): Promise<T> =>
    new Promise<T>((resolve, reject) => {
      const requestId = `mc-handoff-${Date.now()}-${requestSeq++}`;
      const timer = window.setTimeout(() => {
        pending.delete(requestId);
        reject(new Error(`Gateway request timed out: ${method}`));
      }, timeoutMs);
      pending.set(requestId, { resolve: resolve as (value: unknown) => void, reject, timer });
      socket.send(JSON.stringify(createRpcRequest(requestId, method, params)));
    });

  await new Promise<void>((resolve, reject) => {
    socket.onopen = () => resolve();
    socket.onerror = () => reject(new Error('Could not connect to the gateway for the Bot handoff.'));
  });

  socket.onmessage = (event) => {
    const frame = parseGatewayFrame(event.data);
    if (frame.kind === 'response') {
      const requestId = typeof frame.response.id === 'string' ? frame.response.id : String(frame.response.id ?? '');
      const entry = pending.get(requestId);
      if (!entry) return;
      pending.delete(requestId);
      window.clearTimeout(entry.timer);
      if (frame.response.error) {
        entry.reject(new Error(getRpcErrorMessage(frame.response.error)));
      } else {
        entry.resolve(frame.response.result);
      }
      return;
    }
    if (frame.kind === 'event' && options.onEvent) {
      options.onEvent(frame.event);
    }
  };

  socket.onclose = () => {
    if (closed) return;
    closed = true;
    for (const entry of pending.values()) {
      window.clearTimeout(entry.timer);
      entry.reject(new Error('Gateway connection closed during the Bot handoff.'));
    }
    pending.clear();
  };

  return {
    async resolveCanonical(profile: string, knownCanonicalId?: string | null): Promise<BotChatResult> {
      const resolver = createBotChatResolver({
        list: (params) => rpc('session.list', params),
        create: (params) => rpc('session.create', params),
        title: (params) => rpc('session.title', params),
      });
      return resolver.resolve(profile, knownCanonicalId?.trim() || undefined);
    },
    async deliver(profile: string, text: string): Promise<{ reply: string; deferred: boolean }> {
      const result = await rpc<{ reply?: string }>('bot_relay.deliver', {
        profile,
        message: text,
      }, BOT_RELAY_TIMEOUT_MS);
      const reply = result?.reply?.trim();
      if (!reply) throw new Error('Bot relay returned no reply.');
      return {
        reply,
        deferred: /^Delivered into @.+open Bot Chat; the reply will appear there\.?$/i.test(reply),
      };
    },
    async resume(profile: string, sessionId: string): Promise<string> {
      const resumed = await rpc<{ session_id?: string }>('session.resume', {
        session_id: sessionId,
        profile,
        cols: 80,
        eager_build: true,
        source: 'mission-control',
      });
      const nextRuntimeId = resumed?.session_id?.trim();
      if (!nextRuntimeId) throw new Error('Gateway did not return a runtime session id on resume.');
      runtimeId = nextRuntimeId;
      return runtimeId;
    },
    async closeSession(sessionId: string): Promise<void> {
      await rpc('session.close', { session_id: sessionId });
    },
    async submit(text: string): Promise<void> {
      if (!runtimeId) throw new Error('Resume the canonical session before submitting.');
      await rpc('prompt.submit', { session_id: runtimeId, text });
    },
    async eventsSince(lastSeen: number) {
      if (!runtimeId) throw new Error('Resume the canonical session before observing events.');
      return rpc<{ events?: Array<{ type: string; seq?: number; payload?: Record<string, unknown> }>; truncated?: boolean; epoch?: string | null; latest_seq?: number }>(
        'session.events.since',
        { session_id: runtimeId, last_seen: lastSeen },
      );
    },
    close() {
      closed = true;
      socket.close();
    },
  };
}
