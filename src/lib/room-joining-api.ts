import { buildHeaders, localApiUrl } from './hermes-api';

export type JoiningError = { kind: 'unavailable' | 'unauthorized' | 'forbidden' | 'conflict' | 'error'; code: string };
export class RoomJoiningApiError extends Error { readonly problem: JoiningError; constructor(problem: JoiningError) { super(problem.code); this.name = 'RoomJoiningApiError'; this.problem = problem; } }
export type ImportStatus = 'preview' | 'metadata' | 'redeemed' | 'awaiting_host' | 'scope' | 'consent' | 'activating' | 'active' | 'expired' | string;
export type JoiningImport = { id: string; status: ImportStatus; host?: { installation_id: string; profile: string; fingerprint?: string }; metadata?: { name: string; expires_at: string }; scope?: { room_id: string; member_id: string; permissions: string[]; catalog: unknown; policy_digest?: string }; room_id?: string };
export type RoomJoiningApi = {
  preview(input: { code: string; host_id?: string }, signal?: AbortSignal): Promise<JoiningImport>;
  metadata(id: string, signal?: AbortSignal): Promise<JoiningImport>;
  redeem(id: string, signal?: AbortSignal): Promise<JoiningImport>;
  getImport(id: string, signal?: AbortSignal): Promise<JoiningImport>;
  confirmScope(id: string, input: { room_id: string; member_id: string }, signal?: AbortSignal): Promise<JoiningImport>;
  consent(id: string, approve: boolean, signal?: AbortSignal): Promise<JoiningImport>;
  resolve(id: string, signal?: AbortSignal): Promise<JoiningImport>;
};
const kindFor = (status: number): JoiningError['kind'] => status === 401 ? 'unauthorized' : status === 403 ? 'forbidden' : status === 404 ? 'unavailable' : status === 409 ? 'conflict' : 'error';
async function parse<T>(response: Response): Promise<T> { let value: unknown = null; try { value = await response.json(); } catch {} if (!response.ok) { const body = value && typeof value === 'object' ? value as Record<string, unknown> : {}; throw new RoomJoiningApiError({ kind: kindFor(response.status), code: typeof body.code === 'string' ? body.code : `http_${response.status}` }); } return value as T; }
export function createRoomJoiningApi(accessToken?: string, fetcher: typeof fetch = fetch): RoomJoiningApi {
  const request = async <T>(path: string, init: RequestInit = {}, signal?: AbortSignal) => fetcher(localApiUrl(`/room-invites${path}`), { ...init, signal, cache: 'no-store', credentials: 'same-origin', headers: { ...buildHeaders(accessToken), ...(init.body ? { 'Content-Type': 'application/json' } : {}) } }).then(parse<T>);
  const unwrap = async (path: string, init: RequestInit, signal?: AbortSignal) => (await request<{ import: JoiningImport }>(path, init, signal)).import;
  return {
    preview: (input, signal) => unwrap('/imports/preview', { method: 'POST', body: JSON.stringify(input) }, signal),
    metadata: (id, signal) => unwrap(`/imports/${encodeURIComponent(id)}/metadata`, { method: 'POST', body: '{}' }, signal),
    redeem: (id, signal) => unwrap(`/imports/${encodeURIComponent(id)}/redeem`, { method: 'POST', body: '{}' }, signal),
    getImport: (id, signal) => unwrap(`/imports/${encodeURIComponent(id)}`, { method: 'GET' }, signal),
    confirmScope: (id, input, signal) => unwrap(`/imports/${encodeURIComponent(id)}/scope`, { method: 'POST', body: JSON.stringify(input) }, signal),
    consent: (id, approve, signal) => unwrap(`/imports/${encodeURIComponent(id)}/consent`, { method: 'POST', body: JSON.stringify({ approve }) }, signal),
    resolve: (id, signal) => unwrap(`/imports/${encodeURIComponent(id)}/resolve`, { method: 'POST', body: '{}' }, signal),
  };
}
