export function addChatProfile(
  params: Record<string, unknown>,
  profile?: string | null,
): Record<string, unknown> {
  const normalizedProfile = profile?.trim();
  return normalizedProfile ? { ...params, profile: normalizedProfile } : params;
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
