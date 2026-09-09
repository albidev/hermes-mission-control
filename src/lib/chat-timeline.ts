import type { ChatMessage } from './chat-protocol';

export type ChatTimelineEntry = {
  kind: 'message' | 'handoff';
  createdAt: number;
  order: number;
  id: string;
  message?: Pick<ChatMessage, 'canonicalId' | 'kind'>;
};

/**
 * Sort the mixed chat timeline without letting canonical IDs reorder a
 * reasoning/final-response pair from the same SessionDB row.
 */
export function compareChatTimelineEntries(left: ChatTimelineEntry, right: ChatTimelineEntry): number {
  const byTimestamp = left.createdAt - right.createdAt;
  if (byTimestamp !== 0) return byTimestamp;

  if (left.kind === 'message' && right.kind === 'message') {
    const leftMessage = left.message;
    const rightMessage = right.message;
    if (leftMessage?.canonicalId && leftMessage.canonicalId === rightMessage?.canonicalId) {
      if (leftMessage.kind === 'reasoning' && rightMessage.kind === 'assistant') return -1;
      if (leftMessage.kind === 'assistant' && rightMessage.kind === 'reasoning') return 1;
    }
  }

  return left.order - right.order || left.id.localeCompare(right.id);
}
