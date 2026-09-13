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
 * Pairing: a `tool_call_completed` closes the most recent open `tool_call_started` with the
 * same tool name — the same association rule the server uses when it emits the pairs, applied
 * client-side so an unpaired call still renders (server order is not guaranteed across turns).
 */
export function summarizeHandoffTrace(events: MissionControlAgentTraceEvent[] | null | undefined): HandoffTraceSummary {
  if (!Array.isArray(events) || events.length === 0) return EMPTY;

  const rows: HandoffTraceRow[] = [];
  const openCalls = new Map<string, number>();
  let answer = '';
  let request = '';

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
      const row: HandoffTraceRow = {
        kind: 'tool',
        id: text(event.id) || `t-${rows.length}`,
        toolName,
        input: text(event.request) || detailOf(event),
        output: '',
        status: text(event.status) || 'running',
        timestamp: event.timestamp ?? 0,
        durationSeconds: null,
      };
      rows.push(row);
      openCalls.set(`${toolName}#${row.id}`, rows.length - 1);
      continue;
    }

    if (type === 'tool_call_completed') {
      const toolName = toolNameOf(event);
      const body = text(event.response) || detailOf(event);
      // Close the newest still-open call with this name; otherwise render it standalone.
      let targetIndex = -1;
      for (const [key, index] of [...openCalls.entries()].reverse()) {
        if (key.startsWith(`${toolName}#`)) {
          targetIndex = index;
          openCalls.delete(key);
          break;
        }
      }
      if (targetIndex >= 0) {
        const row = rows[targetIndex];
        if (row?.kind === 'tool') {
          row.output = body;
          row.status = text(event.status) || 'complete';
          const elapsed = (event.timestamp ?? 0) - row.timestamp;
          row.durationSeconds = elapsed > 0 ? Math.round(elapsed * 100) / 100 : null;
        }
        continue;
      }
      if (!toolName) continue;
      rows.push({
        kind: 'tool',
        id: text(event.id) || `t-${rows.length}`,
        toolName,
        input: '',
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
