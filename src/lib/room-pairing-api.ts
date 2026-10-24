import { buildHeaders, localApiUrl } from './hermes-api';

export type PairingErrorKind = 'unavailable' | 'unauthorized' | 'forbidden' | 'conflict' | 'error';
export type PairingError = { kind: PairingErrorKind; code: string };
export class RoomPairingApiError extends Error { readonly problem: PairingError; constructor(problem: PairingError) { super(problem.code); this.name = 'RoomPairingApiError'; this.problem = problem; } }

// contract: P3b.1a-2 must match
export type PairingPeer = { peer_id: string; installation_id: string; profile: string; fingerprint?: string; policy_digest?: string; catalog_digest?: string };
// contract: P3b.1a-2 must match
export type PairingRow = { id: string; role: 'host' | 'joining' | string; state: string; expires_at: string; peer?: PairingPeer | null };
// contract: P3b.1a-2 must match
export type PairingOffer = { id: string; code: string; expires_at: string };
// contract: P3b.1a-2 must match
export type PairingAccept = { id: string; code: string; identity: PairingPeer; expires_at: string };
// contract: P3b.1a-2 must match
export type PairingReveal = { id: string; code: string; sas: string; expires_at: string };

export type RoomPairingApi = {
  createOffer(signal?: AbortSignal): Promise<PairingOffer>;
  acceptOffer(id: string, code: string, signal?: AbortSignal): Promise<PairingReveal>;
  confirmOffer(id: string, sas: string, signal?: AbortSignal): Promise<PairingRow>;
  cancelOffer(id: string, signal?: AbortSignal): Promise<void>;
  importOffer(code: string, signal?: AbortSignal): Promise<PairingAccept>;
  revealImport(id: string, code: string, signal?: AbortSignal): Promise<{ id: string; sas: string; expires_at: string }>;
  confirmImport(id: string, sas: string, signal?: AbortSignal): Promise<PairingRow>;
  listPairings(signal?: AbortSignal): Promise<PairingRow[]>;
  removePeer(peerId: string, signal?: AbortSignal): Promise<void>;
};

const kindFor = (status: number): PairingErrorKind => status === 401 ? 'unauthorized' : status === 403 ? 'forbidden' : status === 404 ? 'unavailable' : status === 409 ? 'conflict' : 'error';
async function parse<T>(response: Response): Promise<T> {
  let value: unknown = null; try { value = await response.json(); } catch { /* generic error below */ }
  if (!response.ok) { const body = value && typeof value === 'object' ? value as Record<string, unknown> : {}; const code = typeof body.code === 'string' ? body.code : `http_${response.status}`; throw new RoomPairingApiError({ kind: kindFor(response.status), code }); }
  return value as T;
}

export function createRoomPairingApi(accessToken?: string, fetcher: typeof fetch = fetch): RoomPairingApi {
  const request = async <T>(path: string, init: RequestInit = {}, signal?: AbortSignal) => fetcher(localApiUrl(`/room-invites/pairing${path}`), { ...init, signal, cache: 'no-store', credentials: 'same-origin', headers: { ...buildHeaders(accessToken), ...(init.body ? { 'Content-Type': 'application/json' } : {}) } }).then(parse<T>);
  return {
    createOffer: async (signal) => (await request<{ offer: PairingOffer }>('/offers', { method: 'POST', body: '{}' }, signal)).offer,
    acceptOffer: async (id, code, signal) => (await request<{ reveal: PairingReveal }>(`/offers/${encodeURIComponent(id)}/accept`, { method: 'POST', body: JSON.stringify({ code }) }, signal)).reveal,
    confirmOffer: async (id, sas, signal) => (await request<{ pairing: PairingRow }>(`/offers/${encodeURIComponent(id)}/confirm`, { method: 'POST', body: JSON.stringify({ sas }) }, signal)).pairing,
    cancelOffer: async (id, signal) => { await request(`/offers/${encodeURIComponent(id)}/cancel`, { method: 'POST', body: '{}' }, signal); },
    importOffer: async (code, signal) => (await request<{ accept: PairingAccept }>('/imports', { method: 'POST', body: JSON.stringify({ code }) }, signal)).accept,
    revealImport: (id, code, signal) => request(`/imports/${encodeURIComponent(id)}/reveal`, { method: 'POST', body: JSON.stringify({ code }) }, signal),
    confirmImport: async (id, sas, signal) => (await request<{ pairing: PairingRow }>(`/imports/${encodeURIComponent(id)}/confirm`, { method: 'POST', body: JSON.stringify({ sas }) }, signal)).pairing,
    listPairings: async (signal) => (await request<{ pairings: PairingRow[] }>('', { method: 'GET' }, signal)).pairings,
    removePeer: async (peerId, signal) => { await request(`/peers/${encodeURIComponent(peerId)}`, { method: 'DELETE' }, signal); },
  };
}

export function isValidSas(value: string): boolean { return /^\d{6}$/.test(value); }
