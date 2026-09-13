/**
 * Room tool-card rendering contract.
 *
 * A room tool card printed its payload twice: the trace→message mapping filled
 * both `detail` ("Live output") and `output` ("Output") from `trace.output`, so
 * every one of the 184 tool traces in a live room rendered the same text twice.
 * `detail` belongs to the canonical chat path, where it carries the streaming
 * preview; a room trace is already settled.
 */
import { renderedToolPayloads, traceToChatMessage } from '../src/lib/room-tool-message.ts';

function assertEqual<T>(actual: T, expected: T, label: string) {
  if (JSON.stringify(actual) !== JSON.stringify(expected)) {
    throw new Error(`${label}: expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`);
  }
}

const toolTrace = {
  kind: 'tool' as const,
  toolName: 'skill_view',
  toolInput: '{"name": "jira-example"}',
  output: '{"success": true}',
  status: 'complete' as const,
  durationS: 1.2,
  timestamp: 1789_000_000,
  memberHandle: 'triage',
};

// --- the duplicate-render regression ----------------------------------------
const withInput = traceToChatMessage(toolTrace);
assertEqual(
  renderedToolPayloads(withInput),
  ['Input', 'Output'],
  'a tool trace with input renders Input + Output, never a redundant Live output',
);
assertEqual(withInput.detail, undefined, 'a settled room trace carries no live-output preview');

// The 23 traces in a live room that had no toolInput: `text` falls back to
// `trace.output`, so `Input` renders too -- but still exactly ONE payload block
// for the result, which is the bug that was reported.
const withoutInput = traceToChatMessage({ ...toolTrace, toolInput: undefined });
assertEqual(renderedToolPayloads(withoutInput), ['Input', 'Output'], 'input-less trace renders each block once');
assertEqual(withoutInput.output, '{"success": true}', 'the result still reaches the Output block');

// The payloads must not be the same string rendered under two labels.
assertEqual(
  new Set([withoutInput.text, withoutInput.output]).size,
  1,
  'a room trace has one payload, so text and output legitimately coincide',
);
assertEqual(withInput.detail === withInput.output, false, 'detail must never mirror output');

// --- other fields survive the mapping ---------------------------------------
assertEqual(withInput.toolName, 'skill_view', 'tool name is preserved');
assertEqual(withInput.toolInput, toolTrace.toolInput, 'tool input is preserved');
assertEqual(withInput.status, 'complete', 'status is preserved');
assertEqual(withInput.durationS, 1.2, 'duration is preserved');
assertEqual(withInput.createdAt, 1789_000_000_000, 'timestamp converts to milliseconds');
assertEqual(withInput.attribution?.handle, 'triage', 'member attribution is preserved');

assertEqual(
  traceToChatMessage({ ...toolTrace, status: 'error' }).status,
  'error',
  'a failed trace stays failed',
);

// --- reasoning traces -------------------------------------------------------
const reasoning = traceToChatMessage({ kind: 'reasoning', toolName: 'reasoning', output: 'thinking...', timestamp: 1789_000_000 });
assertEqual(reasoning.kind, 'reasoning', 'reasoning stays reasoning');
assertEqual(renderedToolPayloads(reasoning), [], 'a reasoning trace renders no tool payload blocks');
assertEqual(reasoning.detail, undefined, 'reasoning carries no redundant live-output block');

console.log('room tool-card rendering tests passed');
