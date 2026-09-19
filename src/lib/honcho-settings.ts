export type HonchoIdentityMode = 'unresolved' | 'configured-peer' | 'local-single-user';
export type HonchoProfileReadiness = 'ready' | 'disabled' | 'credentials-required' | 'identity-required';

export interface HonchoProfileStatus {
  profile: string;
  host: string;
  managedBy: 'shared-default' | 'profile-local';
  enabled: boolean;
  connectionConfigured: boolean;
  identityReady: boolean;
  peerPinned: boolean;
  sessionAiPeerPrefix: boolean;
  aiPeer: string;
  workspace: string;
  readiness: HonchoProfileReadiness;
  configPath: string;
}

export interface HonchoStatus {
  available: boolean;
  configured: boolean;
  enabled: boolean;
  providerInstalled: boolean;
  providerActive: boolean;
  identityMode: HonchoIdentityMode;
  identityReady: boolean;
  authenticatedRuntimeSupported: boolean;
  peerName: string | null;
  workspace: string;
  aiPeer: string;
  sessionAiPeerPrefix: boolean;
  configPath: string;
  profileCount: number;
  profiles: HonchoProfileStatus[];
  success?: boolean;
  backupPath?: string | null;
  backupPaths?: string[];
  configuredProfiles?: string[];
  skippedProfiles?: string[];
}

function localApiUrl(path: string): string {
  const env = (import.meta as ImportMeta & { env?: Record<string, string | undefined> }).env;
  const base = (env?.VITE_MISSION_CONTROL_LOCAL_API_BASE_URL || '/api/local').replace(/\/$/, '');
  return `${base}${path.startsWith('/') ? path : `/${path}`}`;
}

function headers(accessToken?: string): Record<string, string> {
  return accessToken?.trim() ? { Authorization: `Bearer ${accessToken.trim()}` } : {};
}

async function payloadOrThrow<T>(response: Response, operation: string): Promise<T> {
  const payload = await response.json().catch(() => ({})) as Record<string, unknown>;
  if (!response.ok) {
    const detail = typeof payload.detail === 'string'
      ? payload.detail
      : typeof payload.error === 'string'
        ? payload.error
        : `${operation} failed with HTTP ${response.status}.`;
    throw new Error(detail);
  }
  return payload as T;
}

export async function loadHonchoStatus(accessToken?: string): Promise<HonchoStatus> {
  const response = await fetch(localApiUrl('/memory/honcho'), {
    headers: headers(accessToken),
    cache: 'no-store',
  });
  return payloadOrThrow<HonchoStatus>(response, 'Honcho status');
}

export async function configureHonchoLocalIdentity(peerName: string, accessToken?: string): Promise<HonchoStatus> {
  const response = await fetch(localApiUrl('/memory/honcho/local-identity'), {
    method: 'POST',
    headers: { ...headers(accessToken), 'Content-Type': 'application/json' },
    body: JSON.stringify({ peerName }),
  });
  return payloadOrThrow<HonchoStatus>(response, 'Honcho local identity setup');
}
