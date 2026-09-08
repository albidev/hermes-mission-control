function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

export type ChatCommandRequest = (method: string, params: Record<string, unknown>) => Promise<unknown>;

export type ReasoningSlashCommandResult = {
  value: string;
  scope: 'session' | 'global';
  updatesEffort: boolean;
};

const REASONING_EFFORTS = new Set(['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra']);

export async function executeReasoningSlashCommand(
  arg: string,
  activeSessionId: string,
  request: ChatCommandRequest,
): Promise<ReasoningSlashCommandResult | null> {
  const tokens = arg.trim().split(/\s+/).filter(Boolean);
  if (tokens.length === 0) return null;
  const scope = tokens.includes('--global') ? 'global' : 'session';
  const values = tokens.filter((token) => token !== '--global');
  if (values.length !== 1) return null;
  const value = values[0].toLowerCase();
  const raw = await request('config.set', {
    key: 'reasoning',
    value,
    ...(scope === 'global' ? { scope } : {}),
    session_id: activeSessionId,
  });
  const responseValue = isRecord(raw) && typeof raw.value === 'string' && raw.value.trim()
    ? raw.value.trim()
    : value;
  const responseScope = isRecord(raw) && raw.scope === 'global' ? 'global' : scope;
  return {
    value: responseValue,
    scope: responseScope,
    updatesEffort: REASONING_EFFORTS.has(value),
  };
}

export function resultText(value: unknown): string {
  if (!isRecord(value)) return '';
  for (const key of ['ref_text', 'text', 'message']) {
    if (typeof value[key] === 'string' && value[key].trim()) return value[key].trim();
  }
  return '';
}

export function commandOutput(value: unknown): string {
  if (!isRecord(value)) return '';
  for (const key of ['output', 'display', 'message', 'notice', 'warning']) {
    if (typeof value[key] === 'string' && value[key].trim()) return value[key].trim();
  }
  return '';
}
