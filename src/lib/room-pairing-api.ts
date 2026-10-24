import { createInviteRequest, type InviteApiProblem } from './room-invite-request';

export type PairingErrorKind = InviteApiProblem['kind'];
export type PairingError = InviteApiProblem;
export class RoomPairingApiError extends Error {
  readonly problem: PairingError;
  constructor(problem: PairingError) {
    super(problem.code);
    this.name = 'RoomPairingApiError';
    this.problem = problem;
  }
}

// contract: P3b.1a-2 must match (redacted journal; no secret fields).
export type PairingPeer = {
  peer_id: string;
  installation_id: string;
  profile: string;
  fingerprint?: string;
  policy_digest?: string;
  catalog_digest?: string;
};
export type PairingRow = {
  id: string;
  role: 'host' | 'joining' | string;
  state: string;
  expires_at: string;
  peer?: PairingPeer | null;
};
export type PairingOffer = { id: string; code: string; expires_at: string };
export type PairingAccept = PairingOffer & { identity: PairingPeer };
export type PairingReveal = PairingOffer & { sas: string };
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

export function createRoomPairingApi(accessToken?: string, fetcher: typeof fetch = fetch): RoomPairingApi {
  const request = createInviteRequest(accessToken, fetcher, (problem) => new RoomPairingApiError(problem));
  const offerPath = (id: string) => `/pairing/offers/${encodeURIComponent(id)}`;
  const importPath = (id: string) => `/pairing/imports/${encodeURIComponent(id)}`;
  return {
    createOffer: async (signal) =>
      (await request<{ offer: PairingOffer }>('/pairing/offers', 'POST', {}, signal)).offer,
    acceptOffer: async (id, code, signal) =>
      (await request<{ reveal: PairingReveal }>(`${offerPath(id)}/accept`, 'POST', { code }, signal)).reveal,
    confirmOffer: async (id, sas, signal) =>
      (await request<{ pairing: PairingRow }>(`${offerPath(id)}/confirm`, 'POST', { sas }, signal)).pairing,
    cancelOffer: async (id, signal) => { await request(`${offerPath(id)}/cancel`, 'POST', {}, signal); },
    importOffer: async (code, signal) =>
      (await request<{ accept: PairingAccept }>('/pairing/imports', 'POST', { code }, signal)).accept,
    revealImport: (id, code, signal) => request(`${importPath(id)}/reveal`, 'POST', { code }, signal),
    confirmImport: async (id, sas, signal) =>
      (await request<{ pairing: PairingRow }>(`${importPath(id)}/confirm`, 'POST', { sas }, signal)).pairing,
    listPairings: async (signal) => {
      const value = await request<{ pairings: PairingRow[] }>('/pairing', 'GET', undefined, signal);
      if (!Array.isArray(value?.pairings)) {
        throw new RoomPairingApiError({ kind: 'error', code: 'invalid_response' });
      }
      return value.pairings;
    },
    removePeer: async (peerId, signal) => {
      await request(`/peers/${encodeURIComponent(peerId)}`, 'DELETE', undefined, signal);
    },
  };
}

export function isValidSas(value: string): boolean {
  return /^\d{6}$/.test(value);
}
