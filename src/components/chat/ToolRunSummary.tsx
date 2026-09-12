import { ChevronDown, ChevronUp, Wrench } from 'lucide-react';
import { useI18n } from '../../lib/i18n';

/**
 * Collapsed anti-noise summary for a run of tool calls, rendered inline in a
 * transcript. Collapsed: a slim rail "N tool calls · <who>". Expanded:
 * the wrapper becomes display:contents so the children (the canonical tool
 * surfaces) anchor directly into the transcript flex flow — the same way the
 * canonical chat shows tool traces between user message and reply.
 *
 * Used by the Rooms per-turn strip (RoomToolStrip) and by the main chat
 * timeline (consecutive kind:'tool' messages collapsed into runs).
 */
export function ToolRunSummary({
  count,
  label,
  expanded,
  onToggle,
  children,
}: {
  count: number;
  label?: string | null;
  expanded: boolean;
  onToggle: () => void;
  children: React.ReactNode;
}) {
  const { t } = useI18n();
  const hasTools = count > 0;
  return (
    <div className="room-tool-strip" style={{ display: expanded ? 'contents' : undefined }}>
      <button
        type="button"
        className="room-tool-panel-bar"
        onClick={onToggle}
        aria-expanded={expanded}
        title={hasTools ? t('rooms.toolCalls', { count }) : 'Reasoning'}
      >
        <Wrench size={13} aria-hidden />
        <span>
          {hasTools ? t('rooms.toolCalls', { count }) : 'Reasoning'}
          {label ? ` · ${label}` : ''}
        </span>
        {expanded ? <ChevronUp size={15} aria-hidden /> : <ChevronDown size={15} aria-hidden />}
      </button>
      {expanded ? <div className="room-tool-inline">{children}</div> : null}
    </div>
  );
}
