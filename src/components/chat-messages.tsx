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
import { formatChatMessageTime } from '../lib/chat-time';
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

export function boldLeadingMention(text: string, mentionHandles: string[] = []): string {
  const match = text.match(/^(@[a-z0-9][a-z0-9_-]*)(?=\s|$)/i);
  if (!match) return text;
  const known = new Set(mentionHandles.map((handle) => handle.trim().toLowerCase()));
  if (!known.has(match[1].slice(1).toLowerCase())) return text;
  return `**${match[1]}**${text.slice(match[1].length)}`;
}

export const ChatMessageCard = memo(function ChatMessageCard({
  message,
  mentionHandles = [],
}: {
  message: ChatMessage;
  mentionHandles?: string[];
}) {
  const { t } = useI18n();
  const visualKind = message.kind ?? message.role;
  const isTool = visualKind === 'tool';
  const isTodoTool = isTool && message.toolName?.trim().toLowerCase() === 'todo';
  const isReasoning = visualKind === 'reasoning';
  const attribution = message.attribution;
  const messageTimestamp = typeof message.createdAt === 'number' && Number.isFinite(message.createdAt) ? message.createdAt : null;
  const messageTime = messageTimestamp === null ? '' : formatChatMessageTime(messageTimestamp);
  const messageText = visualKind === 'user' ? boldLeadingMention(message.text, mentionHandles) : message.text;
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
            <span>{attribution?.displayName || attribution?.handle || label}</span>
            {attribution ? <span className="chat-message-attribution">@{attribution.handle}{attribution.model ? ` · ${attribution.model}` : ''}{attribution.provider ? ` · ${attribution.provider}` : ''}</span> : null}
            {message.status === 'streaming'
              ? <Loader2 size={12} className="chat-spin" aria-label={t('chatDrawer.streaming')} />
              : messageTime ? (
                <time className="chat-message-time" dateTime={new Date(messageTimestamp ?? 0).toISOString()}>
                  {messageTime}
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
                text={messageText}
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
