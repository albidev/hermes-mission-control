function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

export async function deleteBotProfile(name: string, accessToken?: string): Promise<void> {
  const normalizedName = name.trim();
  if (!normalizedName) throw new Error('A Bot profile name is required.');
  if (normalizedName === 'default') throw new Error('The default profile cannot be deleted.');
  const response = await fetch(`/api/profiles/${encodeURIComponent(normalizedName)}`, {
    method: 'DELETE',
    credentials: 'include',
    headers: {
      Accept: 'application/json',
      ...(accessToken?.trim() ? { Authorization: `Bearer ${accessToken.trim()}` } : {}),
    },
  });
  let payload: unknown = null;
  try {
    payload = await response.json();
  } catch {
    // Keep the HTTP status below when the gateway returns no JSON body.
  }
  if (!response.ok) {
    const detail = isRecord(payload)
      ? (typeof payload.detail === 'string' ? payload.detail : typeof payload.message === 'string' ? payload.message : '')
      : '';
    throw new Error(detail || `Bot deletion failed (HTTP ${response.status}).`);
  }
  if (!isRecord(payload) || payload.ok !== true) {
    throw new Error('The gateway did not confirm Bot deletion.');
  }
}
