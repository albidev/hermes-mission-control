import { useI18n } from '../lib/i18n';
import {
  forwardRef,
  useEffect,
  type ChangeEvent,
  type ClipboardEvent,
  type FormEvent,
  type KeyboardEvent,
  type RefObject,
} from 'react';
import { Loader2, Paperclip, Pause, Send, X } from 'lucide-react';
import { ChatSlashPopover, type ChatSlashPopoverHandle, type ChatSlashCompletionResponse } from './ChatSlashPopover';
import { AttachmentIcon } from './chat-messages';
import type { PendingAttachment } from '../lib/chat-gateway';

export type ChatComposerProps = {
  draft: string;
  onDraftChange: (value: string) => void;
  pendingAttachments: PendingAttachment[];
  attachmentNotice: string | null;
  onRemoveAttachment: (id: string) => void;
  onFileInput: (event: ChangeEvent<HTMLInputElement>) => void;
  onPaste: (event: ClipboardEvent<HTMLTextAreaElement>) => void;
  onKeyDown: (event: KeyboardEvent<HTMLTextAreaElement>) => void;
  onSubmit: (event: FormEvent<HTMLFormElement>) => void;
  onStop: () => void;
  completeSlash: (text: string) => Promise<ChatSlashCompletionResponse>;
  textareaRef: RefObject<HTMLTextAreaElement | null>;
  slashPopoverRef: RefObject<ChatSlashPopoverHandle | null>;
  running: boolean;
  submitting: boolean;
  disabled: boolean;
};

export const ChatComposer = forwardRef<HTMLDivElement, ChatComposerProps>(function ChatComposer({
  draft,
  onDraftChange,
  pendingAttachments,
  attachmentNotice,
  onRemoveAttachment,
  onFileInput,
  onPaste,
  onKeyDown,
  onSubmit,
  onStop,
  completeSlash,
  textareaRef,
  slashPopoverRef,
  running,
  submitting,
  disabled,
}, _ref) {
  const { t } = useI18n();
  const canSend = Boolean(draft.trim() || pendingAttachments.length);

  useEffect(() => {
    const textarea = textareaRef.current;
    if (!textarea) return;
    textarea.style.height = 'auto';
    textarea.style.height = `${Math.min(textarea.scrollHeight, 128)}px`;
  }, [draft, textareaRef]);

  return (
    <>
      {pendingAttachments.length ? (
        <div className="chat-pending-attachments" aria-label={t('chatDrawer.pendingAttachments')}>
          {pendingAttachments.map((attachment) => (
            <div className="chat-pending-attachment" key={attachment.id}>
              {attachment.previewUrl ? <img src={attachment.previewUrl} alt="" /> : <AttachmentIcon kind={attachment.kind} />}
              <div className="chat-pending-attachment-copy"><strong>{attachment.name}</strong><span>{formatBytes(attachment.size ?? 0)}</span></div>
              <button type="button" onClick={() => onRemoveAttachment(attachment.id)} aria-label={`Remove ${attachment.name}`}><X size={14} /></button>
            </div>
          ))}
        </div>
      ) : null}
      {attachmentNotice ? <p className="chat-attachment-notice" role="status">{attachmentNotice}</p> : null}
      <ChatSlashPopover ref={slashPopoverRef} input={draft} complete={completeSlash} onApply={onDraftChange} />
      <form className="chat-composer" onSubmit={onSubmit}>
        <div className="chat-composer-main">
          <textarea
            ref={textareaRef}
            value={draft}
            onChange={(event) => onDraftChange(event.target.value)}
            onPaste={onPaste}
            onKeyDown={onKeyDown}
            placeholder={running ? 'Steer Hermes…' : 'Message Hermes'}
            rows={1}
            disabled={disabled}
            aria-label={running ? 'Steer Hermes' : 'Message Hermes'}
          />
          <div className="chat-composer-toolbar">
            <label className="chat-composer-attach" title={t('chatDrawer.attach')} aria-label={t('chatDrawer.attach')}>
              <Paperclip size={16} />
              <input className="chat-file-input" type="file" multiple accept="image/*,application/pdf,*/*" onChange={onFileInput} />
            </label>
            <span className="chat-composer-hint">{running ? 'Enter sends steer' : 'Shift+Enter for newline'}</span>
            <span className="chat-composer-spacer" />
            {running ? (
              <button className="chat-composer-action chat-stop" type="button" onClick={onStop} aria-label={t('chatDrawer.interrupt')} title="Interrupt">
                <Pause size={16} />
              </button>
            ) : null}
            <button className="chat-composer-action chat-send" type="submit" disabled={!canSend || submitting || disabled} aria-label={running ? 'Send steer' : 'Send message'} title={running ? 'Steer' : 'Send'}>
              {submitting ? <Loader2 size={16} className="chat-spin" /> : <Send size={16} />}
            </button>
          </div>
        </div>
      </form>
    </>
  );
});

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}
