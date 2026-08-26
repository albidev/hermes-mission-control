import { useI18n } from '../lib/i18n';
import { memo } from 'react';
import {
  Bot,
  CheckCircle2,
  ChevronDown,
  Clock3,
  Cpu,
  FileText,
  Image as ImageIcon,
  Loader2,
  MessageSquare,
  Sparkles,
  Wrench,
  XCircle,
} from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import remarkBreaks from 'remark-breaks';
import remarkGfm from 'remark-gfm';
import type { AttachmentKind, ChatMessage } from '../lib/chat-protocol';

export function AttachmentIcon({ kind }: { kind: AttachmentKind }) {
  if (kind === 'image') return <ImageIcon size={15} aria-hidden />;
  return <FileText size={15} aria-hidden />;
}

export function formatToolDuration(durationS: number | undefined): string | null {
  if (typeof durationS !== 'number' || !Number.isFinite(durationS)) return null;
  if (durationS < 1) return `${Math.round(durationS * 1000)}ms`;
  return `${durationS.toFixed(durationS < 10 ? 1 : 0)}s`;
}

export function ToolMessage({ message }: { message: ChatMessage }) {
  const running = message.status === 'streaming';
  const failed = message.status === 'error';
  const input = message.toolInput || message.text;
  const duration = formatToolDuration(message.durationS);
  const stateLabel = failed ? 'Failed' : running ? 'Running' : 'Completed';

  return (
    <div className="chat-tool-surface">
      <div className="chat-tool-header">
        <span className="chat-tool-avatar" aria-hidden><Wrench size={15} /></span>
        <div className="chat-tool-heading">
          <strong>{message.toolName || 'Tool'}</strong>
          <span>Hermes tool call</span>
        </div>
        <span className={`chat-tool-state is-${failed ? 'error' : running ? 'running' : 'complete'}`}>
          {failed ? <XCircle size={13} /> : running ? <Loader2 size={13} className="chat-spin" /> : <CheckCircle2 size={13} />}
          {stateLabel}
        </span>
      </div>

      {input ? (
        <div className="chat-tool-section chat-tool-input-section">
          <div className="chat-tool-section-label"><span>Input</span><span>{message.toolId ? `#${message.toolId.slice(-8)}` : 'request'}</span></div>
          <pre>{input}</pre>
        </div>
      ) : null}

      {message.detail ? (
        <div className="chat-tool-live">
          <span><Sparkles size={12} /> Live output</span>
          <pre>{message.detail}</pre>
        </div>
      ) : null}

      {message.output ? (
        <div className="chat-tool-section chat-tool-output-section">
          <div className="chat-tool-section-label"><span>Output</span>{duration ? <span><Clock3 size={11} /> {duration}</span> : null}</div>
          <pre>{message.output}</pre>
        </div>
      ) : running ? (
        <div className="chat-tool-waiting"><Loader2 size={13} className="chat-spin" /> Waiting for tool result…</div>
      ) : null}

      {!input && !message.output && !message.detail && !running ? <div className="chat-tool-waiting">No payload returned.</div> : null}
    </div>
  );
}

export const ChatMessageCard = memo(function ChatMessageCard({ message }: { message: ChatMessage }) {
  const { t } = useI18n();
  const visualKind = message.kind ?? message.role;
  const isTool = visualKind === 'tool';
  const isReasoning = visualKind === 'reasoning';
  const label = visualKind === 'assistant'
    ? 'Hermes'
    : visualKind === 'user'
      ? 'You'
      : isReasoning
        ? 'Reasoning'
        : 'System';
  return (
    <article className={`chat-message chat-message-${visualKind} ${message.status ? `is-${message.status}` : ''}`}>
      {isTool ? <ToolMessage message={message} /> : (
        <>
          <div className="chat-message-meta">
            <span className="chat-message-kind-icon" aria-hidden>
              {visualKind === 'assistant' ? <Bot size={12} /> : isReasoning ? <Cpu size={12} /> : visualKind === 'user' ? <MessageSquare size={12} /> : <FileText size={12} />}
            </span>
            <span>{label}</span>
            {message.status === 'streaming'
              ? <Loader2 size={12} className="chat-spin" aria-label={t('chatDrawer.streaming')} />
              : message.createdAt ? (
                <time className="chat-message-time" dateTime={new Date(message.createdAt).toISOString()}>
                  {new Date(message.createdAt).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
                </time>
              ) : null}
          </div>
          {message.attachments?.length ? (
            <div className="chat-message-attachments">
              {message.attachments.map((attachment) => (
                <span className="chat-file-chip" key={`${message.id}-${attachment.name}`}>
                  <AttachmentIcon kind={attachment.kind} />
                  <span>{attachment.name}</span>
                </span>
              ))}
            </div>
          ) : null}
          {isReasoning ? (
            <details className="chat-reasoning-surface" open={message.status === 'streaming'}>
              <summary>
                <span className="chat-reasoning-summary-icon"><Sparkles size={13} /></span>
                <span>{message.status === 'streaming' ? 'Thinking' : 'Reasoning trace'}</span>
                <ChevronDown size={14} className="chat-reasoning-chevron" />
              </summary>
              <div className="chat-reasoning-copy">
                {message.status === 'streaming' ? (
                  <div className="chat-streaming-copy">{message.text || 'Working through the request…'}</div>
                ) : (
                  <ReactMarkdown remarkPlugins={[remarkGfm, remarkBreaks]}>{message.text || ''}</ReactMarkdown>
                )}
              </div>
            </details>
          ) : (
            <div className={`chat-message-body chat-${visualKind}-body`}>
              {message.status === 'streaming' ? (
                <div className="chat-streaming-copy">{message.text || '...'}</div>
              ) : (
                <ReactMarkdown remarkPlugins={[remarkGfm, remarkBreaks]}>{message.text || ''}</ReactMarkdown>
              )}
            </div>
          )}
        </>
      )}
    </article>
  );
});
