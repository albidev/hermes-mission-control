export function cronScheduleInput(
  kind: string | undefined,
  expression: string | null | undefined,
  runAt: string | null | undefined,
  display: string,
): string {
  if (kind === 'once' && runAt) return runAt;
  return expression || display;
}

export function cronScheduleExpired(
  kind: string | undefined,
  runAt: string | null | undefined,
): boolean {
  if (kind !== 'once') return false;
  const parsed = Date.parse(runAt || '');
  return !Number.isFinite(parsed) || parsed <= Date.now();
}
