import { useI18n } from '../lib/i18n';
import { memo } from 'react';
import {
  Bot,
  ChevronDown,
  Cpu,
  FileText,
  Image as ImageIcon,
  Loader2,
  MessageSquare,
  Sparkles,
} from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import remarkBreaks from 'remark-breaks';
import remarkGfm from 'remark-gfm';
import type { AttachmentKind, ChatMessage } from '../lib/chat-protocol';
import { ToolMessage } from './chat/ToolMessage';

export function AttachmentIcon({ kind }: { kind: AttachmentKind }) {
  if (kind === 'image') return <ImageIcon size={15} aria-hidden />;
  return <FileText size={15} aria-hidden />;
}

export function formatToolDuration(durationS: number | undefined): string | null {
  if (typeof durationS !== 'number' || !Number.isFinite(durationS)) return null;
  if (durationS < 1) return `${Math.round(durationS * 1000)}ms`;
  return `${durationS.toFixed(durationS < 10 ? 1 : 0)}s`;
}

export function ChatMarkdown({
  text,
  placeholder,
  streaming = false,
}: {
  text: string;
  placeholder: string;
  streaming?: boolean;
}) {
  return (
    <div className={`chat-markdown${streaming ? ' is-streaming' : ''}`}>
      <ReactMarkdown remarkPlugins={[remarkGfm, remarkBreaks]}>{text || placeholder}</ReactMarkdown>
    </div>
  );
}

export const ChatMessageCard = memo(function ChatMessageCard({ message }: { message: ChatMessage }) {
  const { t } = useI18n();
  const visualKind = message.kind ?? message.role;
  const isTool = visualKind === 'tool';
  const isTodoTool = isTool && message.toolName?.trim().toLowerCase() === 'todo';
  const isReasoning = visualKind === 'reasoning';
  if (isTodoTool) return null;
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
                <ChatMarkdown
                  text={message.text}
                  placeholder="Working through the request…"
                  streaming={message.status === 'streaming'}
                />
              </div>
            </details>
          ) : (
            <div className={`chat-message-body chat-${visualKind}-body`}>
              <ChatMarkdown
                text={message.text}
                placeholder="..."
                streaming={message.status === 'streaming'}
              />
            </div>
          )}
        </>
      )}
    </article>
  );
});
