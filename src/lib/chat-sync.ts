import type { ChatMessage, GatewayEvent } from './chat-protocol';

function messageKey(message: ChatMessage): string {
  if (message.kind === 'tool' || message.role === 'tool') {
    if (message.toolId) return `tool:${message.toolId}`;
    return `tool:${message.toolName || ''}:${message.toolInput || ''}:${message.output || ''}`;
  }
  return `${message.role}:${message.kind || ''}:${message.text}:${message.attachments?.map((item) => item.name).join(',') || ''}`;
}

function findMatchingMessage(messages: ChatMessage[], candidate: ChatMessage): number {
  if (candidate.kind === 'assistant' && candidate.status === 'streaming') {
    for (let index = messages.length - 1; index >= 0; index -= 1) {
      if (messages[index].kind === 'assistant') return index;
    }
  }
  const key = messageKey(candidate);
  return messages.findIndex((message) => messageKey(message) === key);
}

function mergeStreamingMessage(durable: ChatMessage, local: ChatMessage): ChatMessage {
  if (local.kind === 'assistant' && local.status === 'streaming') {
    const text = local.text.length >= durable.text.length ? local.text : durable.text;
    return { ...durable, ...local, text, status: 'streaming' };
  }
  if (local.kind === 'tool' && local.status === 'streaming') {
    return {
      ...durable,
      ...local,
      output: local.output || durable.output,
      detail: [durable.detail, local.detail].filter(Boolean).join('\n') || undefined,
      status: 'streaming',
    };
  }
  return durable;
}

/** Merge a server snapshot without dropping optimistic or currently streaming local messages. */
export function mergeDurableChatMessages(local: ChatMessage[], durable: ChatMessage[]): ChatMessage[] {
  if (durable.length === 0) return local;
  const merged = durable.map((message) => ({ ...message }));
  const consumed = new Set<number>();

  for (const localMessage of local) {
    const match = findMatchingMessage(merged, localMessage);
    if (match >= 0 && !consumed.has(match)) {
      consumed.add(match);
      if (localMessage.status === 'streaming') merged[match] = mergeStreamingMessage(merged[match], localMessage);
      continue;
    }
    // Preserve local user/tool/streaming messages until the next snapshot contains them.
    if (localMessage.role === 'user' || localMessage.role === 'tool' || localMessage.status === 'streaming') {
      merged.push(localMessage);
    }
  }

  return merged;
}

/** Record a sequenced event only when it advances the watermark for its session. */
export function shouldApplySequencedEvent(watermarks: Map<string, number>, event: GatewayEvent): boolean {
  const sid = event.session_id;
  const seq = event.seq;
  if (!sid || typeof seq !== 'number' || !Number.isFinite(seq)) return true;
  const previous = watermarks.get(sid) ?? 0;
  if (seq <= previous) return false;
  watermarks.set(sid, seq);
  return true;
}
