import type { PersistedBotHandoff } from './bot-handoff-persistence';

export type BotLineageRecord = {
  originSessionId: string;
  handoff: PersistedBotHandoff;
};

type StoredHandoff = {
  sessionId: string;
  handoff: PersistedBotHandoff;
};

/** Collapse the origin/target duplicate persisted by the handoff store. */
export function buildBotLineageRows(entries: StoredHandoff[]): BotLineageRecord[] {
  const rows = new Map<string, BotLineageRecord>();
  for (const entry of entries) {
    const id = entry.handoff.id;
    const existing = rows.get(id);
    if (!existing) {
      rows.set(id, { originSessionId: entry.sessionId, handoff: entry.handoff });
      continue;
    }
    const currentIsTarget = entry.sessionId === entry.handoff.targetSessionId;
    const existingIsTarget = existing.originSessionId === existing.handoff.targetSessionId;
    if (existingIsTarget && !currentIsTarget) {
      rows.set(id, { originSessionId: entry.sessionId, handoff: entry.handoff });
    } else if (entry.handoff.updatedAt > existing.handoff.updatedAt) {
      rows.set(id, { ...existing, handoff: entry.handoff });
    }
  }
  return [...rows.values()].sort((left, right) => (right.handoff.updatedAt ?? 0) - (left.handoff.updatedAt ?? 0));
}
