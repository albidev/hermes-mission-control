export type CanonicalChatMode = 'general' | 'canonical' | 'task';

export type CanonicalChatIdentity = {
  profile: string;
  title: 'Bot Chat';
};

/** The durable identity used by every Bot handoff. */
export function canonicalChatIdentity(profile: string): CanonicalChatIdentity {
  const normalized = profile.trim();
  if (!normalized) throw new Error('A Bot profile is required.');
  return { profile: normalized, title: 'Bot Chat' };
}

/**
 * Canonical Bot Chats are durable relationships. A user asking for a new
 * context there must compact the same session; task chats retain normal reset
 * semantics.
 */
export function canonicalChatCommand(mode: CanonicalChatMode, command: string): string {
  const normalized = command.trim().replace(/^\/+/, '').toLowerCase();
  if (mode === 'canonical' && (normalized === 'new' || normalized === 'reset')) return '/compact';
  return `/${normalized}`;
}

export function shouldPreserveCanonicalSession(mode: CanonicalChatMode): boolean {
  return mode === 'canonical';
}
