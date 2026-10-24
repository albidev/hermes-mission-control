import { buildHeaders, localApiUrl } from './hermes-api';

export type GuestRoom = { room_id: string; name: string; owner: string; owner_online: boolean; owner_offline_since?: string | null; members: Array<{ id: string; name: string; role?: string }>; permissions: string[] };
export type GuestLogEvent = { seq: number; event_id: string; kind: string; actor: string; authority_epoch: number; payload: unknown; created_at: string };
export type GuestLogPage = { events: GuestLogEvent[]; cursor: string | null; latest_seq: number; has_more: boolean; authority: { gateway_id: string; epoch: number } };
export type GuestBot = { status: 'online' | 'offline' | 'pending' | string; approvals: Array<{ request_id: string; title: string; detail?: string }>; permissions: string[] };
export type RoomGuestApi = { listRooms(signal?: AbortSignal): Promise<GuestRoom[]>; getLog(roomId: string, options?: { sinceSeq?: number; limit?: number }, signal?: AbortSignal): Promise<GuestLogPage>; getMyBot(roomId: string, signal?: AbortSignal): Promise<GuestBot>; decideApproval(roomId: string, requestId: string, choice: 'approve' | 'reject', signal?: AbortSignal): Promise<void>; leaveRoom(roomId: string, signal?: AbortSignal): Promise<void> };
export class RoomGuestApiError extends Error { readonly code: string; constructor(code: string) { super(code); this.name = 'RoomGuestApiError'; this.code = code; } }
async function parse<T>(response: Response): Promise<T> { let value: unknown = null; try { value = await response.json(); } catch {} if (!response.ok) { const body = value && typeof value === 'object' ? value as Record<string, unknown> : {}; throw new RoomGuestApiError(typeof body.code === 'string' ? body.code : `http_${response.status}`); } return value as T; }
export function createRoomGuestApi(accessToken?: string, fetcher: typeof fetch = fetch): RoomGuestApi {
  const request = async <T>(path: string, init: RequestInit = {}, signal?: AbortSignal) => fetcher(localApiUrl(`/room-invites${path}`), { ...init, signal, cache: 'no-store', credentials: 'same-origin', headers: { ...buildHeaders(accessToken), ...(init.body ? { 'Content-Type': 'application/json' } : {}) } }).then(parse<T>);
  return {
    listRooms: async (signal) => (await request<{ rooms: GuestRoom[] }>('/guest-rooms', { method: 'GET' }, signal)).rooms,
    getLog: (roomId, options = {}, signal) => { const query = new URLSearchParams(); if (options.sinceSeq !== undefined) query.set('since_seq', String(options.sinceSeq)); if (options.limit !== undefined) query.set('limit', String(options.limit)); return request<GuestLogPage>(`/guest-rooms/${encodeURIComponent(roomId)}/log${query.size ? `?${query}` : ''}`, { method: 'GET' }, signal); },
    getMyBot: (roomId, signal) => request<GuestBot>(`/guest-rooms/${encodeURIComponent(roomId)}/my-bot`, { method: 'GET' }, signal),
    decideApproval: async (roomId, requestId, choice, signal) => { await request(`/guest-rooms/${encodeURIComponent(roomId)}/approvals/${encodeURIComponent(requestId)}`, { method: 'POST', body: JSON.stringify({ choice }) }, signal); },
    leaveRoom: async (roomId, signal) => { await request(`/guest-rooms/${encodeURIComponent(roomId)}/leave`, { method: 'POST', body: '{}' }, signal); },
  };
}

export function mergeGuestEvents(current: GuestLogEvent[], incoming: GuestLogEvent[]): GuestLogEvent[] {
  const byId = new Map<string, GuestLogEvent>(); for (const event of [...current, ...incoming]) byId.set(event.event_id, event);
  return [...byId.values()].sort((a, b) => a.seq - b.seq);
}
