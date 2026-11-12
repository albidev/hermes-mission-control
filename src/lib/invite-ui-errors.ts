type InviteProblem = { kind?: string; code?: string };

export function inviteErrorCode(error: unknown): string {
  if (!error || typeof error !== 'object') return 'request_failed';
  const value = error as { problem?: InviteProblem; code?: string };
  return value.problem?.code ?? value.code ?? 'request_failed';
}

export function isInviteUnavailable(error: unknown): boolean {
  if (!error || typeof error !== 'object') return false;
  const value = error as { problem?: InviteProblem; code?: string };
  return value.problem?.kind === 'unavailable' || value.code === 'http_404' || value.code === 'not_found';
}
