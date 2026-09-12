import { ChevronDown, ChevronUp, Wrench } from 'lucide-react';
import { useI18n } from '../../lib/i18n';
import type { RoomToolTrace } from '../../lib/room-tools';
import type { ChatMessage } from '../../lib/chat-protocol';
import { ToolMessage } from './ToolMessage';

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
 * Per-turn tool summary strip, rendered right above a member's final answer
 * (between the user message that started the turn and the bot reply).
 *
 * Anti-noise by design: the strip is collapsed by default showing the call
 * count for the turn; expanding it reveals the full tool cards rendered with
 * the exact same ToolMessage component the normal chat uses.
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
  const { t } = useI18n();
  if (tools.length === 0) return null;

  return (
    <div className="room-tool-strip" style={{ display: expanded ? 'contents' : undefined }}>
      <button
        type="button"
        className="room-tool-panel-bar"
        onClick={onToggle}
        aria-expanded={expanded}
      >
        <Wrench size={13} aria-hidden />
        <span>
          {t('rooms.toolCalls', { count: tools.length })}
          {memberHandle ? ` · ${memberHandle}` : ''}
        </span>
        {expanded ? <ChevronUp size={15} aria-hidden /> : <ChevronDown size={15} aria-hidden />}
      </button>
      {expanded ? (
        <div className="room-tool-inline">
          {tools.map((trace) => (
            <ToolMessage key={traceToChatMessage(trace).id} message={traceToChatMessage(trace)} />
          ))}
        </div>
      ) : null}
    </div>
  );
}
