import type { ChatMessage } from './chat-protocol';

export type HandoffContextMessage = {
  role: string;
  text: string;
};

export type HandoffContextEntry = {
  kind: 'message' | 'handoff';
  message?: ChatMessage;
  handoff?: {
    handle: string;
    request: string;
    status: string;
    reply?: string | null;
  };
};

export type HandoffContextOptions = {
  maxEntries?: number;
  maxCharacters?: number;
};

const DEFAULT_MAX_ENTRIES = 24;
const DEFAULT_MAX_CHARACTERS = 12_000;

/** Serialize the latest chat entries in chronological order for a Bot handoff. */
export function serializeHandoffContext(
  entries: HandoffContextEntry[],
  options: HandoffContextOptions = {},
): HandoffContextMessage[] {
  const maxEntries = Math.max(0, options.maxEntries ?? DEFAULT_MAX_ENTRIES);
  const maxCharacters = Math.max(0, options.maxCharacters ?? DEFAULT_MAX_CHARACTERS);
  if (maxEntries === 0 || maxCharacters === 0 || entries.length === 0) return [];

  const selected = entries.slice(-maxEntries);
  const result: HandoffContextMessage[] = [];
  let used = 0;
  for (const entry of selected) {
    const role = entry.kind === 'message'
      ? entry.message?.role ?? 'system'
      : 'handoff';
    const text = entry.kind === 'message'
      ? entry.message?.text?.trim() ?? ''
      : entry.handoff
        ? `@${entry.handoff.handle} ${entry.handoff.request}`.trim()
          + ` [${entry.handoff.status}]`
          + (entry.handoff.reply?.trim() ? `: ${entry.handoff.reply.trim()}` : '')
        : '';
    if (!text) continue;
    const remaining = maxCharacters - used;
    if (remaining <= 0) break;
    const boundedText = text.slice(0, remaining);
    result.push({ role, text: boundedText });
    used += boundedText.length;
    if (boundedText.length < text.length) break;
  }
  return result;
}
