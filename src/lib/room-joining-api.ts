import { createInviteRequest, type InviteApiProblem } from './room-invite-request';

export type JoiningError = InviteApiProblem;
export class RoomJoiningApiError extends Error {
  readonly problem: JoiningError;
  constructor(problem: JoiningError) {
    super(problem.code);
    this.name = 'RoomJoiningApiError';
    this.problem = problem;
  }
}
export type ImportStatus =
  | 'preview' | 'metadata' | 'redeemed' | 'awaiting_host' | 'scope' | 'consent'
  | 'activating' | 'active' | 'expired' | string;
export type JoiningImport = {
  id: string;
  status: ImportStatus;
  host?: { installation_id: string; profile: string; fingerprint?: string };
  metadata?: { name: string; expires_at: string };
  scope?: { room_id: string; member_id: string; permissions: string[]; catalog: unknown; policy_digest?: string };
  room_id?: string;
};
export type RoomJoiningApi = {
  preview(input: { code: string; host_id?: string }, signal?: AbortSignal): Promise<JoiningImport>;
  metadata(id: string, signal?: AbortSignal): Promise<JoiningImport>;
  redeem(id: string, signal?: AbortSignal): Promise<JoiningImport>;
  getImport(id: string, signal?: AbortSignal): Promise<JoiningImport>;
  confirmScope(id: string, input: { room_id: string; member_id: string }, signal?: AbortSignal): Promise<JoiningImport>;
  consent(id: string, approve: boolean, signal?: AbortSignal): Promise<JoiningImport>;
  resolve(id: string, signal?: AbortSignal): Promise<JoiningImport>;
};

export function createRoomJoiningApi(accessToken?: string, fetcher: typeof fetch = fetch): RoomJoiningApi {
  const request = createInviteRequest(accessToken, fetcher, (problem) => new RoomJoiningApiError(problem));
  const path = (id: string) => `/imports/${encodeURIComponent(id)}`;
  const unwrap = async (route: string, method: string, body?: unknown, signal?: AbortSignal) =>
    (await request<{ import: JoiningImport }>(route, method, body, signal)).import;
  return {
    preview: (input, signal) => unwrap('/imports/preview', 'POST', input, signal),
    metadata: (id, signal) => unwrap(`${path(id)}/metadata`, 'POST', {}, signal),
    redeem: (id, signal) => unwrap(`${path(id)}/redeem`, 'POST', {}, signal),
    getImport: (id, signal) => unwrap(path(id), 'GET', undefined, signal),
    confirmScope: (id, input, signal) => unwrap(`${path(id)}/scope`, 'POST', input, signal),
    consent: (id, approve, signal) => unwrap(`${path(id)}/consent`, 'POST', { approve }, signal),
    resolve: (id, signal) => unwrap(`${path(id)}/resolve`, 'POST', {}, signal),
  };
}
