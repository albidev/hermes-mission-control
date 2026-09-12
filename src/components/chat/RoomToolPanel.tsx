import { useCallback, useEffect, useMemo, useState } from 'react';
import { ChevronDown, ChevronUp, Wrench } from 'lucide-react';
import { useI18n } from '../../lib/i18n';
import { loadRoomTools, type RoomToolTrace } from '../../lib/room-tools';
import type { ChatMessage } from '../../lib/chat-protocol';
import { ToolMessage } from './ToolMessage';

const MAX_COLLAPSED_HANDLES = 3;

function storedToken(): string | undefined {
  try {
    return localStorage.getItem('mission-control-token') ?? undefined;
  } catch {
    return undefined;
  }
}

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
 * Collapsed-by-default tool activity panel for a Group Room.
 *
 * Anti-noise by design (the room can accumulate dozens of tool calls per
 * member turn): the panel renders as a slim strip with a call count and the
 * member handles involved; expanding it reveals the full tool cards rendered
 * with the exact same ToolMessage component the normal chat uses.
 */
export function RoomToolPanel({ roomId, eventsTick }: { roomId: string | null; eventsTick: number }) {
  const { t } = useI18n();
  const [tools, setTools] = useState<RoomToolTrace[]>([]);
  const [loading, setLoading] = useState(false);
  const [expanded, setExpanded] = useState(false);

  const load = useCallback(async () => {
    if (!roomId) {
      setTools([]);
      return;
    }
    setLoading(true);
    try {
      setTools(await loadRoomTools(roomId, storedToken()));
    } catch {
      setTools([]);
    } finally {
      setLoading(false);
    }
  }, [roomId]);

  useEffect(() => {
    void load();
  }, [load, eventsTick]);

  const memberHandles = useMemo(() => {
    const seen: string[] = [];
    for (const trace of tools) {
      const handle = trace.memberHandle ?? trace.memberDisplayName ?? '';
      if (handle && !seen.includes(handle)) seen.push(handle);
      if (seen.length >= MAX_COLLAPSED_HANDLES) break;
    }
    return seen;
  }, [tools]);

  if (!roomId || (!loading && tools.length === 0)) return null;

  const remaining = tools.length - memberHandles.length;

  return (
    <div className="room-tool-panel">
      <button
        type="button"
        className="room-tool-panel-bar"
        onClick={() => setExpanded((current) => !current)}
        aria-expanded={expanded}
      >
        <Wrench size={13} aria-hidden />
        <span>
          {t('rooms.toolCalls', { count: tools.length })}
          {memberHandles.length > 0 ? ` · ${memberHandles.join(', ')}${remaining > 0 ? ` +${remaining}` : ''}` : ''}
        </span>
        {expanded ? <ChevronUp size={15} aria-hidden /> : <ChevronDown size={15} aria-hidden />}
      </button>
      {expanded ? (
        <div className="room-tool-panel-body">
          {tools.map((trace) => (
            <ToolMessage key={traceToChatMessage(trace).id} message={traceToChatMessage(trace)} />
          ))}
        </div>
      ) : null}
    </div>
  );
}
