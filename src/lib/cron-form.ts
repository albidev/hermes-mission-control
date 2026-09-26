export function cronScheduleInput(
  kind: string | undefined,
  expression: string | null | undefined,
  runAt: string | null | undefined,
  display: string,
): string {
  if (kind === 'once' && runAt) return runAt;
  return expression || display;
}
