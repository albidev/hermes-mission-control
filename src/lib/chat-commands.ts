function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
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
