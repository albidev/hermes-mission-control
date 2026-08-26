import type { ChatModelIdentity } from '../../lib/chat-protocol';
import { useI18n } from '../../lib/i18n';

type ChatStatusLineProps = {
  statusLineLabel: string;
  running: boolean;
  streamingKaomoji: string;
  modelIdentity: ChatModelIdentity | null;
  contextTokens: number | null;
  contextWindow: number;
  contextPercent: number;
};

function formatTokens(tokens: number): string {
  if (tokens < 1000) return `${tokens}`;
  if (tokens < 1_000_000) return `${(tokens / 1000).toFixed(tokens < 10_000 ? 1 : 0)}k`;
  return `${(tokens / 1_000_000).toFixed(tokens < 10_000_000 ? 1 : 0).replace(/\.0$/, '')}M`;
}

export function ChatStatusLine({
  statusLineLabel,
  running,
  streamingKaomoji,
  modelIdentity,
  contextTokens,
  contextWindow,
  contextPercent,
}: ChatStatusLineProps) {
  const { t } = useI18n();
  return (
    <div className="chat-status-line" role="status">
      <span className={`chat-status-line-verb ${running ? 'is-streaming' : statusLineLabel === 'Ready' ? 'is-ready' : ''}`}>
        {running ? <span className="chat-status-line-kaomoji" aria-hidden>{streamingKaomoji}</span> : null}
        {statusLineLabel}
      </span>
      <span className="chat-status-line-separator">|</span>
      <span className="chat-status-line-model" title={modelIdentity ? `${modelIdentity.model}${modelIdentity.provider ? ` via ${modelIdentity.provider}` : ''}` : t('chatDrawer.modelUnavailable')}>
        {modelIdentity?.model || t('chatDrawer.modelUnavailable')}
      </span>
      <span className="chat-status-line-separator">|</span>
      <span className="chat-status-line-reasoning">
        {modelIdentity?.reasoningEffort || '—'}
      </span>
      <span className="chat-status-line-separator">|</span>
      <span className="chat-status-line-ctx" title={contextTokens == null ? t('chatDrawer.contextUnavailable') : `${contextTokens.toLocaleString()} / ${contextWindow.toLocaleString()} ${t('chatDrawer.contextTokensLabel')}`}>
        {contextTokens == null ? `—/${formatTokens(contextWindow)}` : `${formatTokens(contextTokens)}/${formatTokens(contextWindow)}`}
      </span>
      <span className="chat-status-line-separator">|</span>
      <span
        className="chat-status-line-bar"
        role="progressbar"
        aria-label={t('chatDrawer.contextWindowUsage')}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={Math.round(contextPercent)}
        title={contextTokens == null ? t('chatDrawer.contextUnavailable') : t('chatDrawer.contextPercent', { percent: Math.round(contextPercent) })}
      >
        <span className="chat-status-line-bar-fill" style={{ width: `${contextPercent}%` }} />
      </span>
      <span className="chat-status-line-percent">{contextTokens == null ? '—' : `${Math.round(contextPercent)}%`}</span>
    </div>
  );
}
