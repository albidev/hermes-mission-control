import { extractInjectedSessionToken, nextReconnectDelay } from './chat-protocol';

export const RPC_TIMEOUT_MS = 120000;
export const MAX_RECONNECTS = 6;

export function getWebSocketUrl(credential: { kind: 'ticket' | 'token'; value: string }, path = '/api/ws') {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const url = new URL(path, `${protocol}//${window.location.host}`);
  url.searchParams.set(credential.kind, credential.value);
  return url.toString();
}

export async function readLoopbackSessionToken(): Promise<string | null> {
  try {
    const response = await fetch('/api/gateway-root', { credentials: 'include', cache: 'no-store' });
    if (!response.ok) return null;
    return extractInjectedSessionToken(await response.text());
  } catch {
    return null;
  }
}

async function requestWsTicket(headers: Record<string, string> = {}): Promise<Response> {
  return fetch('/api/auth/ws-ticket', {
    method: 'POST',
    headers: { Accept: 'application/json', ...headers },
    credentials: 'include',
    cache: 'no-store',
  });
}

async function readTicketResponse(response: Response): Promise<{ kind: 'ticket'; value: string }> {
  const payload = (await response.json()) as { ticket?: unknown };
  if (typeof payload.ticket === 'string' && payload.ticket) return { kind: 'ticket', value: payload.ticket };
  throw new Error('The gateway returned an invalid WebSocket ticket.');
}

export async function mintWsCredential(accessToken: string): Promise<{ kind: 'ticket' | 'token'; value: string }> {
  const loopbackToken = await readLoopbackSessionToken();
  if (loopbackToken) return { kind: 'token', value: loopbackToken };

  let response = await requestWsTicket();
  if (response.ok) return readTicketResponse(response);

  let lastStatus = response.status;
  if (accessToken.trim()) {
    response = await requestWsTicket({ Authorization: `Bearer ${accessToken.trim()}` });
    if (response.ok) return readTicketResponse(response);
    lastStatus = response.status;
  }

  if (lastStatus === 401) throw new Error('Chat WebSocket authentication failed. Authenticate the Hermes gateway or unlock Mission Control first.');
  if (lastStatus === 403 || lastStatus === 404) throw new Error('Chat WebSocket is not available on this gateway.');
  throw new Error(`Chat WebSocket ticket request failed with HTTP ${lastStatus}.`);
}

export { nextReconnectDelay };
