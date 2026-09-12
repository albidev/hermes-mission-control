export function addChatProfile(
  params: Record<string, unknown>,
  profile?: string | null,
): Record<string, unknown> {
  const normalizedProfile = profile?.trim();
  return normalizedProfile ? { ...params, profile: normalizedProfile } : params;
}
