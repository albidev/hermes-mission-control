import { Bot, Check, CircleAlert, Loader2, RotateCcw } from 'lucide-react';
import { useI18n } from '../../lib/i18n';

export type BotHandoffStatus = 'queued' | 'running' | 'completed' | 'failed';

export type BotHandoffMessageProps = {
  handle: string;
  displayName?: string;
  request: string;
  status: BotHandoffStatus;
  reply?: string | null;
  error?: string | null;
  onRetry?: () => void;
};

export function BotHandoffMessage({ handle, displayName, request, status, reply, error, onRetry }: BotHandoffMessageProps) {
  const { t } = useI18n();
  const label = displayName || handle;
  return (
    <div className="bot-handoff-card" data-status={status}>
      <div className="bot-handoff-head">
        <span className="bot-handoff-avatar"><Bot size={14} /></span>
        <span className="bot-handoff-name">{label}</span>
        <span className="bot-handoff-status">
          {status === 'queued' ? <Loader2 size={12} className="chat-spin" /> : null}
          {status === 'running' ? <Loader2 size={12} className="chat-spin" /> : null}
          {status === 'completed' ? <Check size={12} /> : null}
          {status === 'failed' ? <CircleAlert size={12} /> : null}
          {t(`bots.handoff.${status}`)}
        </span>
      </div>
      <p className="bot-handoff-request">@{handle} {request}</p>
      {status === 'completed' && reply ? <p className="bot-handoff-reply">{reply}</p> : null}
      {status === 'failed' && error ? <p className="bot-handoff-error">{error}</p> : null}
      {status === 'failed' && onRetry ? (
        <button type="button" className="bot-handoff-retry" onClick={onRetry}>
          <RotateCcw size={12} /> {t('bots.handoff.retry')}
        </button>
      ) : null}
    </div>
  );
}
