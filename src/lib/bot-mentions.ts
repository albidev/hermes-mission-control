export type BotMentionCandidate = {
  handle: string;
  displayName?: string;
  description?: string;
  model?: string;
  provider?: string;
};

export type MentionMatch = {
  start: number;
  end: number;
  query: string;
  matches: BotMentionCandidate[];
};

const HANDLE_PATTERN = /@([a-z0-9][a-z0-9_-]*)/i;

function isBoundaryBefore(text: string, index: number): boolean {
  if (index <= 0) return true;
  const previous = text[index - 1];
  return !/[a-z0-9_@]/i.test(previous);
}

function normalizeHandle(handle: string): string {
  return handle.trim().toLowerCase();
}

export function findMentionAtCaret(
  text: string,
  caret: number,
  roster: BotMentionCandidate[],
): MentionMatch | null {
  if (!roster.length || caret < 0 || caret > text.length) return null;
  const before = text.slice(0, caret);
  // Find the last '@' that starts a standalone token (not glued to an email
  // prefix like `ciao@azienda.it`).
  let atIndex = -1;
  for (let i = before.length - 1; i >= 0; i--) {
    if (before[i] === '@' && isBoundaryBefore(text, i)) {
      atIndex = i;
      break;
    }
  }
  if (atIndex === -1) return null;
  const token = before.slice(atIndex);
  // A mention token cannot contain whitespace; if it does, the caret is past
  // the mention and the popover must hide.
  if (/\s/.test(token)) return null;
  const query = normalizeHandle(token.slice(1));
  // An empty query (`@` alone) shows the full roster.
  const matches = query
    ? roster.filter((candidate) => normalizeHandle(candidate.handle).startsWith(query))
    : roster;
  if (!matches.length) return null;
  return { start: atIndex, end: caret, query, matches };
}

export function extractMentionRequest(
  text: string,
  roster: BotMentionCandidate[],
): { mention: string; request: string } | null {
  if (!roster.length) return null;
  const match = HANDLE_PATTERN.exec(text);
  if (!match) return null;
  const start = match.index;
  const end = start + match[0].length;
  if (!isBoundaryBefore(text, start)) return null;
  if (end < text.length && /[a-z0-9_-]/i.test(text[end])) return null;
  const query = normalizeHandle(match[1]);
  const candidate = roster.find((c) => normalizeHandle(c.handle) === query);
  if (!candidate) return null;
  const request = text.slice(end).trim();
  return { mention: `@${candidate.handle}`, request };
}
