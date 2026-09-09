import { compareChatTimelineEntries, type ChatTimelineEntry } from '../src/lib/chat-timeline.ts';

function assertEqual<T>(actual: T, expected: T, label: string) {
  if (actual !== expected) throw new Error(`${label}: expected ${String(expected)}, got ${String(actual)}`);
}

function assertDeepEqual<T>(actual: T, expected: T, label: string) {
  const actualJson = JSON.stringify(actual);
  const expectedJson = JSON.stringify(expected);
  if (actualJson !== expectedJson) throw new Error(`${label}: expected ${expectedJson}, got ${actualJson}`);
}

const reasoning: ChatTimelineEntry = {
  kind: 'message',
  createdAt: 1_000,
  order: 0,
  id: 'db:42:reasoning',
  message: { canonicalId: 'db:42', kind: 'reasoning' },
};
const assistant: ChatTimelineEntry = {
  kind: 'message',
  createdAt: 1_000,
  order: 0,
  id: 'db:42',
  message: { canonicalId: 'db:42', kind: 'assistant' },
};

assertEqual(compareChatTimelineEntries(reasoning, assistant) < 0, true, 'reasoning precedes paired assistant');
assertEqual(compareChatTimelineEntries(assistant, reasoning) > 0, true, 'paired assistant follows reasoning');

const sorted = [assistant, reasoning].sort(compareChatTimelineEntries);
assertDeepEqual(sorted.map((entry) => entry.id), ['db:42:reasoning', 'db:42'], 'equal-timestamp pair order');

const unrelatedAssistant: ChatTimelineEntry = {
  ...assistant,
  id: 'db:99',
  message: { canonicalId: 'db:99', kind: 'assistant' },
};
assertEqual(compareChatTimelineEntries(assistant, unrelatedAssistant) < 0, true, 'fallback preserves explicit order');

console.log('chat timeline tests passed');
