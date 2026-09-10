import { Bot, Check, ChevronDown, CircleAlert, Loader2, RotateCcw } from 'lucide-react';
import { useI18n } from '../../lib/i18n';

export type BotHandoffStatus = 'queued' | 'running' | 'completed' | 'failed';

export type BotHandoffMessageProps = {
  handle: string;
  displayName?: string;
  model?: string;
  provider?: string;
  request: string;
  status: BotHandoffStatus;
  reply?: string | null;
  error?: string | null;
  onRetry?: () => void;
};

function PayloadBlock({ label, value, meta, open = true }: { label: string; value: string; meta: string; open?: boolean }) {
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

export function BotHandoffMessage({ handle, displayName, model, provider, request, status, reply, error, onRetry }: BotHandoffMessageProps) {
  const { t } = useI18n();
  const label = displayName || handle;
  const modelLabel = model || 'model inherited';
  const providerLabel = provider || 'provider inherited';
  const running = status === 'queued' || status === 'running';
  const failed = status === 'failed';
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

      <PayloadBlock label="Input" value={`@${handle} ${request}`.trim()} meta="request" />

      {reply ? <PayloadBlock label="Output" value={reply} meta="result" open={false} /> : running ? (
        <div className="chat-tool-waiting"><Loader2 size={13} className="chat-spin" /> Waiting for Bot result…</div>
      ) : null}
      {failed && error ? <pre className="chat-tool-detail chat-bot-handoff-error">{error}</pre> : null}
      {failed && onRetry ? (
        <button type="button" className="bot-handoff-retry" onClick={onRetry}>
          <RotateCcw size={12} /> {t('bots.handoff.retry')}
        </button>
      ) : null}
    </div>
  );
}
