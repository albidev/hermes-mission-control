import type { ChatModelIdentity } from './chat-protocol';

export type LastChatClaimAction = 'passive' | 'bootstrap' | 'create' | 'resume' | 'submit';

export type ServerLastChat = {
  sessionId: string;
  sessionKey?: string | null;
  sessionTitle?: string | null;
  modelIdentity?: ChatModelIdentity | null;
  profile?: string | null;
  revision: number;
  updatedAt?: number;
};

export type LocalLastChat = {
  sessionId: string | null;
  revision: number | null;
};

export type ChatBootstrapAttempt = {
  generation: number;
  signal: AbortSignal;
};

export type ChatBootstrapGuard = {
  begin: () => ChatBootstrapAttempt;
  invalidate: () => void;
  isCurrent: (attempt: ChatBootstrapAttempt) => boolean;
};

/**
 * Scope a pointer GET to one drawer-open lifecycle. Aborting is the fast path;
 * the generation check also protects callers whose fetch implementation resolves
 * an already-started request despite the abort signal.
 */
export function createChatBootstrapGuard(): ChatBootstrapGuard {
  let generation = 0;
  let controller: AbortController | null = null;

  return {
    begin() {
      controller?.abort();
      controller = new AbortController();
      return { generation: ++generation, signal: controller.signal };
    },
    invalidate() {
      generation += 1;
      controller?.abort();
      controller = null;
    },
    isCurrent(attempt) {
      return attempt.generation === generation && !attempt.signal.aborted;
    },
  };
}

/** Only an explicit chat action may publish a pointer to the shared server. */
export function canClaimLastChatPointer(action: LastChatClaimAction): boolean {
  return action === 'create' || action === 'resume' || action === 'submit';
}

/**
 * The server revision is authoritative. Client timestamps are deliberately not
 * consulted: a device can have a newer local write but still be stale globally.
 */
export function shouldAdoptServerPointer(local: LocalLastChat, server: ServerLastChat | null): boolean {
  if (!server?.sessionId) return false;
  if (local.sessionId !== server.sessionId) return true;
  return local.revision !== server.revision;
}

/**
 * A mobile deep link can carry the short-lived runtime session ID while the
 * server pointer keeps the durable SessionDB key. Resolve only an exact alias:
 * a pointer for another chat must never clobber an explicit Resume selection.
 */
export function serverPointerMatchesRequestedSession(requestedSessionId: string | null | undefined, server: ServerLastChat | null): boolean {
  const requested = requestedSessionId?.trim();
  if (!requested || !server?.sessionId) return false;
  return requested === server.sessionId || requested === server.sessionKey?.trim();
}

export function buildLastChatClaimPayload(
  sessionId: string,
  sessionKey: string | null,
  sessionTitle: string | null,
  modelIdentity: ChatModelIdentity | null,
  expectedRevision: number | null,
  profile?: string | null,
): Record<string, unknown> {
  const body: Record<string, unknown> = { sessionId, sessionKey, modelIdentity };
  if (sessionTitle?.trim()) body.sessionTitle = sessionTitle.trim();
  if (profile?.trim()) body.profile = profile.trim();
  if (typeof expectedRevision === 'number' && Number.isInteger(expectedRevision) && expectedRevision > 0) {
    body.expectedRevision = expectedRevision;
  }
  return body;
}

export function normalizeServerLastChat(value: unknown): ServerLastChat | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const record = value as Record<string, unknown>;
  if (typeof record.sessionId !== 'string' || !record.sessionId.trim()) return null;
  const revision = typeof record.revision === 'number' && Number.isInteger(record.revision)
    ? record.revision
    : null;
  if (revision === null || revision < 1) return null;
  return {
    sessionId: record.sessionId,
    sessionKey: typeof record.sessionKey === 'string' ? record.sessionKey : null,
    sessionTitle: typeof record.sessionTitle === 'string' ? record.sessionTitle : null,
    modelIdentity: record.modelIdentity && typeof record.modelIdentity === 'object'
      ? record.modelIdentity as ChatModelIdentity
      : null,
    profile: typeof record.profile === 'string' && record.profile.trim() ? record.profile.trim() : null,
    revision,
    updatedAt: typeof record.updatedAt === 'number' ? record.updatedAt : undefined,
  };
}
