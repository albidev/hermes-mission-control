import type { ReactNode } from 'react';
import { Bot, Check, ChevronDown, CircleAlert, Loader2, RotateCcw } from 'lucide-react';
import { useI18n } from '../../lib/i18n';
import { ToolRunSummary } from './ToolRunSummary';
import type { HandoffTraceRow } from '../../lib/handoff-trace';

export type BotHandoffStatus = 'queued' | 'running' | 'completed' | 'failed';

export type BotHandoffTraceState = {
  /** 'loading' while the on-demand fetch is in flight. */
  status: 'idle' | 'loading' | 'ready' | 'unavailable';
  rows: HandoffTraceRow[];
  toolCount: number;
  reasoningCount: number;
  headline: string;
  error?: string;
};

export type BotHandoffMessageProps = {
  handle: string;
  displayName?: string;
  model?: string;
  provider?: string;
  request: string;
  status: BotHandoffStatus;
  reply?: string | null;
  error?: string | null;
  reason?: string;
  onRetry?: () => void;
  /** Trace of the bot's own turn (reasoning + tools). Absent on older handoffs. */
  trace?: BotHandoffTraceState | null;
  traceExpanded?: boolean;
  onToggleTrace?: () => void;
};

function PayloadBlock({ label, value, meta, open = true }: { label: string; value: ReactNode; meta: string; open?: boolean }) {
  return (
    <details className="chat-tool-section chat-tool-payload" open={open}>
      <summary className="chat-tool-section-label">
        <span>{label}</span>
        <span className="chat-tool-section-meta">{meta}</span>
        <ChevronDown className="chat-tool-payload-chevron" size={14} aria-hidden />
      </summary>
      <pre>{value}</pre>
    </details>
  );
}

export function BotHandoffMessage({ handle, displayName, model, provider, request, status, reply, error, reason, onRetry, trace, traceExpanded = false, onToggleTrace }: BotHandoffMessageProps) {
  const { t } = useI18n();
  const label = displayName || handle;
  const modelLabel = model || 'model inherited';
  const providerLabel = provider || 'provider inherited';
  const running = status === 'queued' || status === 'running';
  const failed = status === 'failed';
  const hasTrace = Boolean(trace && trace.status !== 'idle');
  return (
    <div className={`chat-tool-surface chat-tool-family-delegation bot-handoff-tool-surface is-${status}`} data-status={status}>
      <div className="chat-tool-header">
        <span className="chat-tool-avatar" aria-hidden><Bot size={15} /></span>
        <div className="chat-tool-heading">
          <strong>@{handle}</strong>
          <span>{label} · {modelLabel} · {providerLabel}</span>
        </div>
        <span className={`chat-tool-state is-${failed ? 'error' : running ? 'running' : 'complete'}`}>
          {failed ? <CircleAlert size={13} /> : running ? <Loader2 size={13} className="chat-spin" /> : <Check size={13} />}
          {t(`bots.handoff.${status}`)}
        </span>
      </div>

      <PayloadBlock label="Input" value={<><strong className="font-bold text-accent">@{handle}</strong>{request ? ` ${request}` : ''}</>} meta="request" />

      {reply ? <PayloadBlock label="Output" value={reply} meta="result" open={false} /> : running ? (
        <div className="chat-tool-waiting"><Loader2 size={13} className="chat-spin" /> Waiting for Bot result…</div>
      ) : null}

      {hasTrace && trace ? (
        trace.status === 'loading' ? (
          <div className="chat-tool-waiting"><Loader2 size={13} className="chat-spin" /> {t('bots.handoff.traceLoading')}</div>
        ) : trace.status === 'unavailable' ? (
          <p className="chat-tool-detail chat-bot-handoff-trace-empty">{trace.error || t('bots.handoff.traceUnavailable')}</p>
        ) : (
          <ToolRunSummary
            count={trace.toolCount}
            reasoningCount={trace.reasoningCount}
            label={trace.headline || null}
            expanded={traceExpanded}
            onToggle={() => onToggleTrace?.()}
          >
            {trace.rows.map((row) => row.kind === 'reasoning' ? (
              <div key={row.id} className="chat-tool-section chat-tool-payload bot-handoff-trace-reasoning">
                <span className="chat-tool-section-label"><span>{t('bots.handoff.traceReasoning')}</span></span>
                <pre>{row.text}</pre>
              </div>
            ) : (
              <div key={row.id} className="chat-tool-section chat-tool-payload">
                <span className="chat-tool-section-label">
                  <span>{row.toolName}</span>
                  <span className="chat-tool-section-meta">{row.durationSeconds !== null ? `${row.durationSeconds}s` : row.status}</span>
                </span>
                {row.input ? <pre>{row.input}</pre> : null}
                {row.output ? <pre>{row.output}</pre> : null}
              </div>
            ))}
          </ToolRunSummary>
        )
      ) : null}

      {failed && error ? <pre className="chat-tool-detail chat-bot-handoff-error">{reason ? `[reason: ${reason}]\n` : ''}{error}</pre> : null}
      {failed && onRetry ? (
        <button type="button" className="bot-handoff-retry" onClick={onRetry}>
          <RotateCcw size={12} /> {t('bots.handoff.retry')}
        </button>
      ) : null}
    </div>
  );
}
