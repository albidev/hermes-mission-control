import { buildHeaders, localApiUrl } from './hermes-api';

export type InviteErrorKind = 'unavailable' | 'unauthorized' | 'forbidden' | 'conflict' | 'error';
export type InviteApiProblem = { kind: InviteErrorKind; code: string };

export function inviteProblem(status: number, body: unknown): InviteApiProblem {
  const value = body && typeof body === 'object' ? body as Record<string, unknown> : {};
  const kind = status === 401 ? 'unauthorized' : status === 403 ? 'forbidden'
    : status === 404 ? 'unavailable' : status === 409 ? 'conflict' : 'error';
  return { kind, code: typeof value.code === 'string' ? value.code : `http_${status}` };
}

/** Match the existing invite client's fallback outside Vite (Node contract tests). */
function inviteUrl(path: string): string {
  try { return localApiUrl(path); }
  catch { return `/api/local${path}`; }
}

/** No storage/URL mutations. All authority and secrets remain on the owner-authenticated server. */
export function createInviteRequest(
  accessToken: string | undefined,
  fetcher: typeof fetch,
  errorFor: (problem: InviteApiProblem) => Error,
) {
  return async <T>(path: string, method: string, body?: unknown, signal?: AbortSignal): Promise<T> => {
    const response = await fetcher(inviteUrl(`/room-invites${path}`), {
      method, signal, cache: 'no-store', credentials: 'same-origin',
      headers: { ...buildHeaders(accessToken), ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}) },
      ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
    });
    let value: unknown = null;
    try { value = await response.json(); } catch { /* Empty success or sanitized HTTP error. */ }
    if (!response.ok) throw errorFor(inviteProblem(response.status, value));
    return value as T;
  };
}
