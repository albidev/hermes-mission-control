import { buildHeaders, localApiUrl } from './hermes-api';

export type RoomInviteErrorKind = 'unavailable' | 'unauthorized' | 'forbidden' | 'not_found' | 'conflict' | 'ambiguous' | 'error';
export type RoomInviteError = { kind: RoomInviteErrorKind; code: string };
export class RoomInviteApiError extends Error {
  readonly problem: RoomInviteError;
  constructor(problem: RoomInviteError) { super(`${problem.kind}:${problem.code}`); this.name = 'RoomInviteApiError'; this.problem = problem; }
}

export type RoomInviteBinding = {
  fingerprint: string;
  target_install_id: string;
  target_profile: string;
  catalog_digest: string;
  policy_digest: string;
};
export type RoomInviteTarget = { kind?: string; peer_id: string; installation_id?: string; profile?: string; capability_digest?: string };
export type RoomInvitePeer = { peer_id: string; installation_id: string; profile: string; capability_digest: string };
export type RoomInviteMember = { member_id: string; profile: string; handle: string; display_name?: string; target?: RoomInviteTarget };
export type RoomInviteLobby = { lobby_id: string; name: string; state: string; planned_room_id?: string; created_at?: string; updated_at?: string; roster: RoomInviteMember[] };
export type RoomInvitePeerStatus = {
  member_id: string;
  peer_id?: string | null;
  invite_id?: string | null;
  redeemed_binding?: RoomInviteBinding | null;
  host_decision: 'pending' | 'approved' | 'rejected' | string;
  joining_consent: 'pending' | 'given' | 'rejected' | string;
  material_available?: boolean;
  ready?: boolean;
  registered?: boolean;
};
export type RoomInviteActivationStatus = { lobby_id: string; lobby_state: string; ready: boolean; peers: RoomInvitePeerStatus[] };
export type RoomInviteIssue = { invite_id: string; secret: string; expires_at: string };
export type RoomInviteRedemption = { invite_id: string; member_id: string; status: string; host_decision: string; joining_consent: string };

export type RoomInviteApi = {
  getLobby(lobbyId: string, signal?: AbortSignal): Promise<RoomInviteLobby>;
  listPairedPeers(signal?: AbortSignal): Promise<RoomInvitePeer[]>;
  createLobby(input: { name: string; roster: Array<{ member_id: string; profile: string; handle: string; display_name?: string; target?: RoomInviteTarget }> }, signal?: AbortSignal): Promise<RoomInviteLobby>;
  issueInvite(lobbyId: string, input: { member_id: string; invitee_label: string; ttl_seconds?: number }, signal?: AbortSignal): Promise<RoomInviteIssue>;
  revokeInvite(lobbyId: string, inviteId: string, signal?: AbortSignal): Promise<{ revoked: boolean }>;
  decideHost(lobbyId: string, inviteId: string, input: { peer_id: string; approved: boolean; expected_binding: RoomInviteBinding }, signal?: AbortSignal): Promise<RoomInviteRedemption>;
  getActivation(lobbyId: string, signal?: AbortSignal): Promise<RoomInviteActivationStatus>;
  activate(lobbyId: string, signal?: AbortSignal): Promise<{ lobby_id: string; status: 'activated'; room_id: string }>;
};

function errorKind(status: number, code: string): RoomInviteErrorKind {
  if (status === 401) return 'unauthorized';
  if (status === 403) return 'forbidden';
  if (status === 404) return 'unavailable';
  if (status === 409 && code === 'registration_incomplete') return 'ambiguous';
  if (status === 409) return 'conflict';
  if (status === 503) return 'ambiguous';
  return 'error';
}

async function parseResponse<T>(response: Response): Promise<T> {
  let payload: unknown = null;
  try { payload = await response.json(); } catch { /* preserve a stable local error below */ }
  if (!response.ok) {
    const body = payload && typeof payload === 'object' ? payload as Record<string, unknown> : {};
    const code = typeof body.code === 'string' ? body.code : typeof body.error === 'string' ? body.error : `http_${response.status}`;
    throw new RoomInviteApiError({ kind: errorKind(response.status, code), code });
  }
  return payload as T;
}

function roomInviteUrl(path: string): string {
  try { return localApiUrl(path); } catch { return `/api/local${path}`; }
}

export function createRoomInviteApi(accessToken?: string, fetcher: typeof fetch = fetch): RoomInviteApi {
  const request = async <T>(path: string, init: RequestInit = {}, signal?: AbortSignal) => {
    const headers = { ...buildHeaders(accessToken), ...(init.body ? { 'Content-Type': 'application/json' } : {}) };
    const response = await fetcher(roomInviteUrl(`/room-invites${path}`), { ...init, headers, signal, cache: 'no-store', credentials: 'same-origin' });
    return parseResponse<T>(response);
  };
  return {
    getLobby: async (id, signal) => (await request<{ lobby: RoomInviteLobby }>(`/lobbies/${encodeURIComponent(id)}`, { method: 'GET' }, signal)).lobby,
    listPairedPeers: async (signal) => (await request<{ peers: RoomInvitePeer[] }>('/paired-peers', { method: 'GET' }, signal)).peers,
    createLobby: async (input, signal) => (await request<{ lobby: RoomInviteLobby }>('/lobbies', { method: 'POST', body: JSON.stringify(input) }, signal)).lobby,
    issueInvite: (id, input, signal) => request<RoomInviteIssue>(`/lobbies/${encodeURIComponent(id)}/invites`, { method: 'POST', body: JSON.stringify(input) }, signal),
    revokeInvite: (id, invite, signal) => request<{ revoked: boolean }>(`/lobbies/${encodeURIComponent(id)}/invites/${encodeURIComponent(invite)}/revoke`, { method: 'POST', body: '{}' }, signal),
    decideHost: async (id, invite, input, signal) => (await request<{ redemption: RoomInviteRedemption }>(`/lobbies/${encodeURIComponent(id)}/invites/${encodeURIComponent(invite)}/decision`, { method: 'POST', body: JSON.stringify(input) }, signal)).redemption,
    getActivation: (id, signal) => request<RoomInviteActivationStatus>(`/lobbies/${encodeURIComponent(id)}/activation`, { method: 'GET' }, signal),
    activate: (id, signal) => request<{ lobby_id: string; status: 'activated'; room_id: string }>(`/lobbies/${encodeURIComponent(id)}/activate`, { method: 'POST', body: '{}' }, signal),
  };
}