export function clearNewChatParams(search: string): string {
  const params = new URLSearchParams(search.startsWith('?') ? search.slice(1) : search);
  for (const key of ['chatSession', 'botProfile', 'chatMode', 'roomId']) params.delete(key);
  return params.toString();
}

export function addChatProfile(
  params: Record<string, unknown>,
  profile?: string | null,
): Record<string, unknown> {
  const normalizedProfile = profile?.trim();
  return normalizedProfile ? { ...params, profile: normalizedProfile } : params;
}

/** A New Chat always starts in the default profile store. */
export function profileForNewChat(): null {
  return null;
}

/**
 * The profile a NEW chat starts from, after the chat being left is forgotten.
 *
 * A reset is a new chat, so the profile of the previous context must not
 * survive it: `sessionProfileRef` is hook-level state that outlives a session
 * change, and the creation path (`ensureSession` -> `session.create`) reads it
 * directly. Leaving the carried value in place is how a chat created after a
 * client room landed in that bot's store — a different HERMES_HOME, a different
 * state.db, a different agent, and memory written into the wrong profile.
 *
 * This exists as its own entry point so the reset path and the `useEffect`
 * path cannot drift apart again: both resolve through `nextSessionProfile`, and
 * neither can be fixed while the other is forgotten. Only an explicitly
 * requested profile may scope a new chat.
 */
export function profileAfterReset(
  current: string | null | undefined,
  explicitProfile: string | null | undefined,
): string | null {
  return nextSessionProfile(current, explicitProfile, false);
}

/**
 * Decide the profile to use when a session resume is requested.
 *
 * A session id lives in exactly one profile's state.db, so the owning profile
 * must survive every resume. Only an EXPLICITLY supplied profile may replace a
 * value we already hold: an absent or blank `botProfile` (a persisted pointer, a
 * cross-device link, a stale URL) must leave the known owner intact. Clearing it
 * here sends the resume and every later transcript lookup to the default store,
 * where the session does not exist — the drawer then falls back to an empty
 * preview and the conversation appears to vanish.
 *
 * @param current         the profile currently held (may be null)
 * @param explicitProfile the profile supplied by the caller, if any
 * @param hasRequestedSession whether a resume is actually in flight
 */
export function nextSessionProfile(
  current: string | null | undefined,
  explicitProfile: string | null | undefined,
  hasRequestedSession: boolean,
): string | null {
  const explicit = explicitProfile?.trim();
  if (explicit) return explicit;
  return hasRequestedSession ? current?.trim() || null : null;
}

/**
 * Extract the owning profile from a sessions-index entry.
 *
 * The index reports `profile: "default"` for sessions in the default store;
 * that is not a scoped owner and must not be forwarded as one, so it resolves
 * to null and the caller leaves its current value alone.
 */
export function resolveSessionOwner(entry: { profile?: string | null } | null | undefined): string | null {
  const owner = entry?.profile?.trim();
  return owner && owner !== 'default' ? owner : null;
}
