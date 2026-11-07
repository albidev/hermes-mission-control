import { createInviteRequest, type InviteApiProblem } from './room-invite-request';

// Verbatim groups.log wire events, not the gateway-owned GroupRoomResult state.
export type GuestRoom = {
  room_id: string;
  name: string;
  owner: string;
  owner_online: boolean;
  owner_offline_since?: string | null;
  members: Array<{ id: string; name: string; role?: string }>;
  permissions: string[];
};
export type GuestLogEvent = {
  seq: number;
  event_id: string;
  kind: string;
  actor: string;
  authority_epoch: number;
  payload: unknown;
  created_at: string;
};
export type GuestLogPage = {
  events: GuestLogEvent[];
  cursor: string | null;
  latest_seq: number;
  has_more: boolean;
  authority?: { gateway_id: string; epoch: number };
};
export type GuestBot = {
  status: 'online' | 'offline' | 'pending' | string;
  approvals: Array<{ request_id: string; title: string; detail?: string }>;
  permissions: string[];
};
export type RoomGuestApi = {
  listRooms(signal?: AbortSignal): Promise<GuestRoom[]>;
  getLog(roomId: string, options?: { sinceSeq?: number; limit?: number }, signal?: AbortSignal): Promise<GuestLogPage>;
  getMyBot(roomId: string, signal?: AbortSignal): Promise<GuestBot>;
  decideApproval(roomId: string, requestId: string, choice: 'approve' | 'reject', signal?: AbortSignal): Promise<void>;
  leaveRoom(roomId: string, signal?: AbortSignal): Promise<void>;
};
export class RoomGuestApiError extends Error {
  readonly code: string;
  readonly problem: InviteApiProblem;
  constructor(problem: InviteApiProblem | string) {
    const value = typeof problem === 'string' ? { code: problem, kind: 'error' as const } : problem;
    super(value.code);
    this.name = 'RoomGuestApiError';
    this.code = value.code;
    this.problem = value;
  }
}
export function createRoomGuestApi(accessToken?: string, fetcher: typeof fetch = fetch): RoomGuestApi {
  const request = createInviteRequest(accessToken, fetcher, (problem) => new RoomGuestApiError(problem));
  const path = (roomId: string) => `/guest-rooms/${encodeURIComponent(roomId)}`;
  return {
    listRooms: async (signal) => (await request<{ rooms: GuestRoom[] }>('/guest-rooms', 'GET', undefined, signal)).rooms,
    getLog: (roomId, options = {}, signal) => {
      const query = new URLSearchParams();
      if (options.sinceSeq !== undefined) query.set('since_seq', String(options.sinceSeq));
      if (options.limit !== undefined) query.set('limit', String(options.limit));
      return request<GuestLogPage>(`${path(roomId)}/log${query.size ? `?${query}` : ''}`, 'GET', undefined, signal);
    },
    getMyBot: (roomId, signal) => request<GuestBot>(`${path(roomId)}/my-bot`, 'GET', undefined, signal),
    decideApproval: async (roomId, requestId, choice, signal) => {
      await request(`${path(roomId)}/approvals/${encodeURIComponent(requestId)}`, 'POST', { choice }, signal);
    },
    leaveRoom: async (roomId, signal) => { await request(`${path(roomId)}/leave`, 'POST', {}, signal); },
  };
}

export function mergeGuestEvents(current: GuestLogEvent[], incoming: GuestLogEvent[]): GuestLogEvent[] {
  const byId = new Map<string, GuestLogEvent>();
  for (const event of [...current, ...incoming]) {
    // An immutable log identity cannot be overwritten by a later page.
    if (!byId.has(event.event_id)) byId.set(event.event_id, event);
  }
  return [...byId.values()].sort((a, b) => a.seq - b.seq);
}
