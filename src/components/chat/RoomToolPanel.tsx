import type { RoomToolTrace } from '../../lib/room-tools';
import type { ChatMessage } from '../../lib/chat-protocol';
import { ToolMessage } from './ToolMessage';
import { ToolRunSummary } from './ToolRunSummary';

function traceToChatMessage(trace: RoomToolTrace): ChatMessage {
  const text = trace.output ?? trace.toolInput ?? '';
  return {
    id: `room-tool-${trace.memberHandle ?? 'member'}-${trace.timestamp ?? 0}-${trace.toolName}`,
    role: 'tool',
    kind: 'tool',
    text,
    status: trace.status === 'error' ? 'error' : 'complete',
    createdAt: typeof trace.timestamp === 'number' ? trace.timestamp * 1000 : null,
    toolId: undefined,
    toolName: trace.toolName,
    toolInput: trace.toolInput,
    output: trace.output,
    detail: trace.output,
    durationS: trace.durationS ?? undefined,
    attribution: trace.memberHandle
      ? { handle: trace.memberHandle, displayName: trace.memberDisplayName ?? trace.memberHandle }
      : undefined,
  };
}

/**
 * Per-turn tool strip for room transcripts, rendered right above a member's
 * final answer (between the user message that started the turn and the bot
 * reply). Thin wrapper around the shared ToolRunSummary: collapsed rail with
 * the call count + member handle, expansion renders the canonical ToolMessage
 * cards inline into the transcript.
 */
export function RoomToolStrip({
  tools,
  memberHandle,
  expanded,
  onToggle,
}: {
  tools: RoomToolTrace[];
  memberHandle: string;
  expanded: boolean;
  onToggle: () => void;
}) {
  return (
    <ToolRunSummary count={tools.length} label={memberHandle} expanded={expanded} onToggle={onToggle}>
      {tools.map((trace) => (
        <ToolMessage key={traceToChatMessage(trace).id} message={traceToChatMessage(trace)} />
      ))}
    </ToolRunSummary>
  );
}
