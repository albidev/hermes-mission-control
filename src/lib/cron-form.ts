export type CronScheduleFields = {
  scheduleKind?: string;
  scheduleExpr?: string | null;
  scheduleRunAt?: string | null;
};

export function cronScheduleFields(input: CronScheduleFields | undefined): CronScheduleFields {
  return {
    scheduleKind: input?.scheduleKind,
    scheduleExpr: input?.scheduleExpr ?? null,
    scheduleRunAt: input?.scheduleRunAt ?? null,
  };
}

export function cronScheduleInput(
  kind: string | undefined,
  expression: string | null | undefined,
  runAt: string | null | undefined,
  display: string,
): string {
  if (kind === 'once' && runAt) return runAt;
  return expression || display;
}
