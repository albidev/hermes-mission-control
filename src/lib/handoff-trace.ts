import type { MissionControlAgentTraceEvent } from './hermes-api';

/**
 * Projection of a bot handoff's trace into the rows a collapsed summary renders.
 *
 * The Bot Chat trace for one handoff is a real turn: reasoning blocks interleaved with tool
 * calls. Rendering every event as its own card would bury the handoff card, so the client
 * folds them into two kinds of row — `reasoning` and `tool` — and the UI shows a single
 * collapsed rail that expands into the canonical tool surfaces.
 *
 * Kept pure (no React, no fetch) so the projection is unit-testable without a gateway.
 */

export type HandoffTraceRow =
  | { kind: 'reasoning'; id: string; text: string; timestamp: number }
  | {
      kind: 'tool';
      id: string;
      toolName: string;
      input: string;
      output: string;
      status: string;
      timestamp: number;
      durationSeconds: number | null;
    };

export type HandoffTraceSummary = {
  rows: HandoffTraceRow[];
  toolCount: number;
  reasoningCount: number;
  /** First human-readable line of the turn's response, for the collapsed rail. */
  headline: string;
};

const EMPTY: HandoffTraceSummary = { rows: [], toolCount: 0, reasoningCount: 0, headline: '' };

function text(value: unknown): string {
  return typeof value === 'string' ? value.trim() : '';
}

function detailOf(event: MissionControlAgentTraceEvent): string {
  return text(event.detail) || text(event.response) || text(event.request);
}

function toolNameOf(event: MissionControlAgentTraceEvent): string {
  return text(event.toolName) || text(event.label).replace(/^Tool:\s*/i, '').trim();
}

/** First non-blank line, from the RAW detail — trimming first would drop a leading newline. */
function firstLine(event: MissionControlAgentTraceEvent): string {
  const raw = text(event.detail) || text(event.response) || text(event.request);
  if (!raw) return '';
  return raw.split('\n').map((line) => line.trim()).find((line) => line.length > 0) ?? '';
}

/**
 * Build the summary rows for one handoff trace.
 *
 * Headline precedence: the ASSISTANT's answer, then the user request as a fallback. The
 * collapsed rail summarises what the bot produced — leading with the request would label
 * every handoff with the same text the card already shows above it.
 *
 * Pairing: `tool_call_completed` links to its `tool_call_started` through `parentEventId`,
 * which the server always emits. Pairing by tool NAME looks equivalent but is not: a
 * `tool_call_started` frequently carries the generic name `tool_call` while its completion
 * carries the real one (`mcp__sentry_…`), so name matching silently leaves every one of
 * those calls as two half-rows — an input with no output, and an orphan output. The name is
 * only a fallback for events that arrive without a parent link.
 */
export function summarizeHandoffTrace(events: MissionControlAgentTraceEvent[] | null | undefined): HandoffTraceSummary {
  if (!Array.isArray(events) || events.length === 0) return EMPTY;

  const rows: HandoffTraceRow[] = [];
  /** started event id -> index of its row, so a completion can close it exactly. */
  const openByEventId = new Map<string, number>();
  /** Tool name -> indexes of unclosed rows, for events with no usable parent link. */
  const openByName = new Map<string, number[]>();
  let answer = '';
  let request = '';

  const closeRow = (index: number, event: MissionControlAgentTraceEvent, body: string) => {
    const row = rows[index];
    if (row?.kind !== 'tool') return;
    row.output = body;
    row.status = text(event.status) || 'complete';
    // The completion carries the REAL tool name; a started event often only says
    // `tool_call`. Keep the specific one so the rail names the actual tool.
    const resolved = toolNameOf(event);
    if (resolved && resolved !== 'tool_call') row.toolName = resolved;
    const elapsed = (event.timestamp ?? 0) - row.timestamp;
    row.durationSeconds = elapsed > 0 ? Math.round(elapsed * 100) / 100 : null;
  };

  for (const event of events) {
    if (!event || typeof event !== 'object') continue;
    const type = text(event.type);

    if (type === 'thought' || type === 'reasoning') {
      const body = detailOf(event);
      if (body) {
        rows.push({ kind: 'reasoning', id: text(event.id) || `r-${rows.length}`, text: body, timestamp: event.timestamp ?? 0 });
      }
      continue;
    }

    if (type === 'tool_call_started') {
      const toolName = toolNameOf(event);
      if (!toolName) continue;
      rows.push({
        kind: 'tool',
        id: text(event.id) || `t-${rows.length}`,
        toolName,
        input: text(event.request) || detailOf(event),
        output: '',
        status: text(event.status) || 'running',
        timestamp: event.timestamp ?? 0,
        durationSeconds: null,
      });
      const index = rows.length - 1;
      const eventId = text(event.id);
      if (eventId) openByEventId.set(eventId, index);
      const bucket = openByName.get(toolName);
      if (bucket) bucket.push(index);
      else openByName.set(toolName, [index]);
      continue;
    }

    if (type === 'tool_call_completed') {
      const toolName = toolNameOf(event);
      const body = text(event.response) || detailOf(event);
      const parentId = text(event.parentEventId);

      // Preferred: the explicit parent link.
      let targetIndex = parentId ? openByEventId.get(parentId) ?? -1 : -1;
      if (targetIndex >= 0) openByEventId.delete(parentId);
      // Fallback: the newest still-open call with this name.
      if (targetIndex < 0 && toolName) {
        const bucket = openByName.get(toolName);
        while (bucket && bucket.length > 0) {
          const candidate = bucket.pop() as number;
          if (rows[candidate]?.kind === 'tool' && !(rows[candidate] as { output: string }).output) {
            targetIndex = candidate;
            break;
          }
        }
      }
      if (targetIndex >= 0) {
        const closedId = text((rows[targetIndex] as { id?: string }).id);
        closeRow(targetIndex, event, body);
        if (closedId) openByEventId.delete(closedId);
        continue;
      }
      if (!toolName) continue;
      rows.push({
        kind: 'tool',
        id: text(event.id) || `t-${rows.length}`,
        toolName,
        input: text(event.request) || '',
        output: body,
        status: text(event.status) || 'complete',
        timestamp: event.timestamp ?? 0,
        durationSeconds: null,
      });
      continue;
    }

    if (type === 'assistant_response' && !answer) {
      answer = firstLine(event);
      continue;
    }
    if (type === 'user_message' && !request) {
      request = firstLine(event);
    }
  }

  const toolCount = rows.filter((row) => row.kind === 'tool').length;
  const reasoningCount = rows.filter((row) => row.kind === 'reasoning').length;
  return { rows, toolCount, reasoningCount, headline: (answer || request).slice(0, 160) };
}
