export const BOT_HANDOFF_REASONS = [
  'provider_auth_or_access',
  'provider_quota_limit',
  'provider_rate_limit',
  'provider_server_error',
  'context_overflow',
  'missing_config',
  'model_unavailable',
  'runtime_offline',
  'queued_expired',
  'delivery_timeout',
  'target_busy',
  'unknown',
] as const;

export type BotHandoffFailureReason = typeof BOT_HANDOFF_REASONS[number];

const RETRYABLE = new Set<BotHandoffFailureReason>([
  'provider_rate_limit',
  'provider_server_error',
  'context_overflow',
  'runtime_offline',
  'queued_expired',
  'delivery_timeout',
  'target_busy',
]);

function isReason(value: unknown): value is BotHandoffFailureReason {
  return typeof value === 'string' && (BOT_HANDOFF_REASONS as readonly string[]).includes(value);
}

function reasonFromRecord(record: Record<string, unknown>): BotHandoffFailureReason | undefined {
  if (isReason(record.reason)) return record.reason;
  if (record.data && typeof record.data === 'object' && isReason((record.data as Record<string, unknown>).reason)) {
    return (record.data as Record<string, unknown>).reason as BotHandoffFailureReason;
  }
  return undefined;
}

function valueText(value: unknown): string {
  if (value instanceof Error) return value.message;
  if (typeof value === 'string') return value;
  if (value && typeof value === 'object') {
    const record = value as Record<string, unknown>;
    return [record.reason, record.message, record.error, record.detail].filter((item): item is string => typeof item === 'string').join(' ');
  }
  return '';
}

export function isRetryableHandoffReason(reason: BotHandoffFailureReason): boolean {
  return RETRYABLE.has(reason);
}

export function classifyHandoffFailure(value: unknown): { reason: BotHandoffFailureReason; retryable: boolean } {
  const text = valueText(value);
  const structuredReason = value && typeof value === 'object'
    ? reasonFromRecord(value as Record<string, unknown>)
    : undefined;
  const marker = text.match(/\[reason:\s*([a-z_]+)\]/i)?.[1]?.toLowerCase();
  const reason = structuredReason ?? (isReason(marker) ? marker : 'unknown');
  return { reason, retryable: isRetryableHandoffReason(reason) };
}
