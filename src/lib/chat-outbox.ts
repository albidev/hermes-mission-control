import type { ChatAttachmentSummary } from './chat-protocol';

export type PendingChatSubmit = {
  text: string;
  displayText: string;
  attachments: ChatAttachmentSummary[];
  baselineUserCount: number;
  sessionKey: string | null;
  messageId?: string;
};

const OUTBOX_STORAGE_KEY = 'mission-control-chat-outbox-v1';

function storage(): Storage | null {
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

export function readPendingChatSubmit(): PendingChatSubmit | null {
  const store = storage();
  if (!store) return null;
  try {
    const raw = store.getItem(OUTBOX_STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<PendingChatSubmit>;
    if (typeof parsed.text !== 'string' || !parsed.text.trim()) return null;
    if (typeof parsed.displayText !== 'string') return null;
    if (!Array.isArray(parsed.attachments)) return null;
    if (typeof parsed.baselineUserCount !== 'number' || typeof parsed.sessionKey !== 'string') return null;
    return {
      text: parsed.text,
      displayText: parsed.displayText,
      attachments: parsed.attachments.filter((item): item is ChatAttachmentSummary => {
        if (!item || typeof item !== 'object' || Array.isArray(item)) return false;
        const candidate = item as Record<string, unknown>;
        return (candidate.kind === 'image' || candidate.kind === 'pdf' || candidate.kind === 'file')
          && typeof candidate.name === 'string'
          && candidate.name.trim().length > 0;
      }),
      baselineUserCount: Math.max(0, Math.floor(parsed.baselineUserCount)),
      sessionKey: parsed.sessionKey,
      ...(typeof parsed.messageId === 'string' && parsed.messageId.trim() ? { messageId: parsed.messageId.trim() } : {}),
    };
  } catch {
    return null;
  }
}

export function persistPendingChatSubmit(pending: PendingChatSubmit): void {
  const store = storage();
  if (!store) return;
  try {
    store.setItem(OUTBOX_STORAGE_KEY, JSON.stringify(pending));
  } catch {
    // The live session remains authoritative if storage is unavailable/full.
  }
}

export function clearPendingChatSubmit(): void {
  const store = storage();
  if (!store) return;
  try {
    store.removeItem(OUTBOX_STORAGE_KEY);
  } catch {
    // Best effort; never break the chat loop.
  }
}
