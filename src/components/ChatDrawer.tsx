import {
  type ClipboardEvent,
  type DragEvent,
  type FormEvent,
  type KeyboardEvent,
  useCallback,
  useEffect,
  useRef,
  useState,
} from 'react';
import {
  Bot,
  Check,
  ChevronDown,
  Circle,
  Cpu,
  FileText,
  KeyRound,
  Loader2,
  MessageSquare,
  Paperclip,
  Pause,
  Send,
  ShieldCheck,
  Sparkles,
  SquarePen,
  X,
  XCircle,
} from 'lucide-react';
import { ChatModelPicker } from './ChatModelPicker';
import { ChatSlashPopover, type ChatSlashPopoverHandle } from './ChatSlashPopover';
import { AttachmentIcon, ChatMessageCard } from './chat-messages';
import {
  MAX_ATTACHMENTS,
  MAX_ATTACHMENT_BYTES,
  classifyAttachment,
  formatBytes,
  interactionTitle,
  readFileAsDataUrl,
  useGatewayChat,
  type PendingAttachment,
} from '../lib/chat-gateway';
import { previewText, type ChatAttachmentUpload, type GatewayInteractionRequest } from '../lib/chat-protocol';
import {
  loadMissionControlSessionPreview,
  type MissionControlAgentSessionItem,
  type MissionControlSessionPreviewMessage,
} from '../lib/hermes-api';

type ChatDrawerProps = {
  open: boolean;
  storedToken: string;
  initialSessionId?: string | null;
  onClose: () => void;
};

function ChatPreviewBubble({ message }: { message: MissionControlSessionPreviewMessage }) {
  const isUser = message.role === 'user';
  return (
    <div className={`flex ${isUser ? 'justify-end' : 'justify-start'}`}>
      <div className={`chat-preview-bubble ${isUser ? 'is-user' : ''}`}>
        <p className={`chat-preview-bubble-role ${isUser ? 'text-violet-300' : 'text-sky-300'}`}>
          {isUser ? 'You' : 'Hermes'}
        </p>
        <p className="chat-preview-bubble-text">{message.text}</p>
      </div>
    </div>
  );
}

export function ChatDrawer({ open, storedToken, initialSessionId, onClose }: ChatDrawerProps) {
  const [draft, setDraft] = useState('');
  const [pendingAttachments, setPendingAttachments] = useState<PendingAttachment[]>([]);
  const [attachmentNotice, setAttachmentNotice] = useState<string | null>(null);
  const [isDragging, setIsDragging] = useState(false);
  const [interactionDraft, setInteractionDraft] = useState('');
  const [selectedChoices, setSelectedChoices] = useState<string[]>([]);
  const [newChatLoading, setNewChatLoading] = useState(false);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const [nearBottom, setNearBottom] = useState(true);
  const nearBottomRef = useRef(true);
  const slashPopoverRef = useRef<ChatSlashPopoverHandle | null>(null);
  const pendingRef = useRef<PendingAttachment[]>([]);
  const {
    messages,
    sessionId,
    connectionState,
    statusText,
    error,
    submitting,
    running,
    activity,
    interaction,
    previewMode,
    modelIdentity,
    modelPickerOpen,
    modelPickerRefresh,
    request,
    switchModel,
    closeModelPicker,
    commandPrefill,
    connect,
    resumeSession,
    completeSlash,
    clearCommandPrefill,
    submitPrompt,
    respondInteraction,
    interrupt,
    reset,
  } = useGatewayChat(storedToken, open, initialSessionId);

  useEffect(() => {
    pendingRef.current = pendingAttachments;
  }, [pendingAttachments]);

  const [preview, setPreview] = useState<MissionControlAgentSessionItem | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);

  useEffect(() => {
    if (!previewMode || !initialSessionId) {
      setPreview(null);
      setPreviewLoading(false);
      return;
    }
    let cancelled = false;
    // The telemetry token may not be settled in the store yet when the drawer
    // opens from a deep link. Fall back to the persisted localStorage token.
    const token = storedToken?.trim() || (typeof window !== 'undefined' ? window.localStorage.getItem('mission-control-token') ?? '' : '');
    setPreviewLoading(true);
    // Bounded resolution: never leave the drawer stuck on "Loading" if the
    // preview request stalls (auth bootstrap, proxy hiccup, etc.).
    const timeout = window.setTimeout(() => {
      if (!cancelled) setPreviewLoading(false);
    }, 6000);
    loadMissionControlSessionPreview(token, initialSessionId).then((item) => {
      if (!cancelled) {
        setPreview(item);
        setPreviewLoading(false);
      }
    }).catch(() => {
      if (!cancelled) setPreviewLoading(false);
    }).finally(() => {
      window.clearTimeout(timeout);
    });
    return () => {
      cancelled = true;
      window.clearTimeout(timeout);
    };
  }, [previewMode, initialSessionId, storedToken]);

  useEffect(() => {
    return () => {
      for (const attachment of pendingRef.current) {
        if (attachment.previewUrl) URL.revokeObjectURL(attachment.previewUrl);
      }
    };
  }, []);

  useEffect(() => {
    if (commandPrefill === null) return;
    setDraft(commandPrefill);
    clearCommandPrefill();
    window.requestAnimationFrame(() => {
      textareaRef.current?.focus();
      textareaRef.current?.setSelectionRange(commandPrefill.length, commandPrefill.length);
    });
  }, [clearCommandPrefill, commandPrefill]);

  useEffect(() => {
    if (!open) return;
    const raf = window.requestAnimationFrame(() => textareaRef.current?.focus());
    return () => window.cancelAnimationFrame(raf);
  }, [open]);

  const isNearBottom = () => {
    const el = scrollRef.current;
    if (!el) return true;
    return el.scrollHeight - el.scrollTop - el.clientHeight < 80;
  };

  const handleTranscriptScroll = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    const bottom = isNearBottom();
    nearBottomRef.current = bottom;
    setNearBottom(bottom);
  }, []);

  const scrollToBottom = useCallback((behavior: ScrollBehavior = 'smooth') => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior });
  }, []);

  const renderMessages = () => {
    if (previewMode) {
      if (previewLoading) {
        return (
          <section className="chat-preview-surface">
            <div className="chat-preview-empty">
              <Loader2 size={20} className="chat-spin" />
              <p>Loading session preview…</p>
            </div>
          </section>
        );
      }
      if (preview) {
        return (
          <section className="chat-preview-surface">
            <div className="chat-preview-heading">
              <div>
                <p className="eyebrow">Session preview</p>
                <h3 className="chat-preview-title">{preview.title || 'Untitled session'}</h3>
              </div>
              <span className="chat-preview-meta">
                {preview.model ? <Cpu size={12} aria-hidden /> : null}
                {preview.model}
              </span>
            </div>
            <div className="chat-preview-body">
              {(preview.recentMessages ?? []).length > 0 ? preview.recentMessages!.map((msg, index) => (
                <ChatPreviewBubble key={`${msg.role}-${msg.timestamp ?? 'na'}-${index}`} message={msg} />
              )) : (
                <p className="chat-preview-fallback">{preview.preview || 'No recent messages available.'}</p>
              )}
            </div>
            <button
              type="button"
              className="chat-resume-button"
              onClick={() => void resumeSession()}
              disabled={connectionState !== 'connected'}
            >
              <Bot size={15} aria-hidden />
              Resume session
            </button>
          </section>
        );
      }
      return (
        <section className="chat-preview-surface">
          <div className="chat-preview-empty">
            <MessageSquare size={20} />
            <p>Session preview unavailable.</p>
            <button type="button" className="chat-resume-button" onClick={() => void resumeSession()} disabled={connectionState !== 'connected'}>
              <Bot size={15} aria-hidden />
              Resume session
            </button>
          </div>
        </section>
      );
    }

    if (messages.length === 0) {
      return (
        <section className="chat-empty">
          <MessageSquare size={22} />
          <p>Start a Hermes session from Mission Control.</p>
          <span>Ask, inspect, fix, ship.</span>
        </section>
      );
    }

    return <>{messages.map((message) => <ChatMessageCard key={message.id} message={message} />)}</>;
  };

  useEffect(() => {
    if (!open) return;
    // Only auto-scroll when the user was already near the bottom. If they've
    // scrolled up to read earlier content while Hermes streams, we leave them
    // where they are and show the "jump to bottom" FAB instead.
    if (!nearBottomRef.current) return;
    scrollToBottom('smooth');
  }, [messages, interaction, activity, open, scrollToBottom]);

  useEffect(() => {
    setInteractionDraft('');
    setSelectedChoices([]);
  }, [interaction?.requestId, interaction?.kind]);

  const statusClass = connectionState === 'connected'
    ? 'is-online'
    : connectionState === 'reconnecting' || connectionState === 'connecting' || connectionState === 'ticket'
      ? 'is-pending'
      : 'is-offline';
  const lastPreview = messages.length ? previewText(messages[messages.length - 1].text) : 'No messages yet';

  const addFiles = useCallback((files: File[]) => {
    setAttachmentNotice(null);
    const available = Math.max(0, MAX_ATTACHMENTS - pendingRef.current.length);
    if (available === 0) {
      setAttachmentNotice(`You can attach up to ${MAX_ATTACHMENTS} files per message.`);
      return;
    }
    const accepted: PendingAttachment[] = [];
    for (const file of files.slice(0, available)) {
      if (file.size > MAX_ATTACHMENT_BYTES) {
        setAttachmentNotice(`${file.name} is too large. The limit is 50 MB.`);
        continue;
      }
      const kind = classifyAttachment(file.type, file.name);
      accepted.push({
        id: `${file.name}-${file.lastModified}-${Math.random().toString(36).slice(2)}`,
        kind,
        name: file.name,
        size: file.size,
        mimeType: file.type || undefined,
        file,
        previewUrl: kind === 'image' ? URL.createObjectURL(file) : null,
      });
    }
    if (files.length > available) setAttachmentNotice(`Only ${available} more attachment${available === 1 ? '' : 's'} can be added.`);
    if (accepted.length) setPendingAttachments((current) => [...current, ...accepted]);
  }, []);

  const removeAttachment = useCallback((id: string) => {
    setPendingAttachments((current) => {
      const target = current.find((attachment) => attachment.id === id);
      if (target?.previewUrl) URL.revokeObjectURL(target.previewUrl);
      return current.filter((attachment) => attachment.id !== id);
    });
  }, []);

  const handleFileInput = (event: { target: HTMLInputElement }) => {
    addFiles(Array.from(event.target.files ?? []));
    event.target.value = '';
  };

  const handleDrop = (event: DragEvent<HTMLElement>) => {
    event.preventDefault();
    setIsDragging(false);
    addFiles(Array.from(event.dataTransfer.files ?? []));
  };

  const handlePaste = (event: ClipboardEvent<HTMLTextAreaElement>) => {
    const files = Array.from(event.clipboardData.files ?? []);
    if (files.length) {
      event.preventDefault();
      addFiles(files);
    }
  };

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const text = draft;
    if (!text.trim() && pendingAttachments.length === 0) return;
    setAttachmentNotice(null);
    try {
      const uploads: ChatAttachmentUpload[] = [];
      for (const attachment of pendingAttachments) {
        const dataUrl = await readFileAsDataUrl(attachment.file);
        uploads.push({
          id: attachment.id,
          kind: attachment.kind,
          name: attachment.name,
          size: attachment.size,
          mimeType: attachment.mimeType,
          dataUrl,
        });
      }
      const sent = await submitPrompt(text, uploads);
      if (!sent) return;
      setDraft('');
      for (const attachment of pendingAttachments) {
        if (attachment.previewUrl) URL.revokeObjectURL(attachment.previewUrl);
      }
      setPendingAttachments([]);
    } catch (err) {
      setAttachmentNotice(err instanceof Error ? err.message : 'Could not read the attachment.');
    }
  };

  const handleComposerKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (slashPopoverRef.current?.handleKey(event)) return;
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      event.currentTarget.form?.requestSubmit();
    }
    if (event.key === 'Escape') onClose();
  };

  const handleDrawerKeyDown = (event: KeyboardEvent<HTMLElement>) => {
    if (event.key !== 'Tab') return;
    const focusable = Array.from(
      event.currentTarget.querySelectorAll<HTMLElement>('button:not(:disabled), textarea:not(:disabled), input:not(:disabled), [href], [tabindex]:not([tabindex="-1"])'),
    ).filter((element) => element.offsetParent !== null);
    if (focusable.length === 0) return;

    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  };

  const handleNewChat = async () => {
    if (newChatLoading) return;
    setNewChatLoading(true);
    try {
      await reset();
    } finally {
      setNewChatLoading(false);
    }
  };

  const interactionPayload = interaction?.payload ?? {};
  const interactionChoices = Array.isArray(interactionPayload.choices)
    ? interactionPayload.choices.filter((choice): choice is string => typeof choice === 'string' && choice.trim().length > 0)
    : [];
  const multiSelect = interactionPayload.multi_select === true;
  const interactionQuestion = typeof interactionPayload.question === 'string' ? interactionPayload.question : '';
  const interactionPrompt = typeof interactionPayload.prompt === 'string' ? interactionPayload.prompt : '';
  const secretEnvVar = typeof interactionPayload.env_var === 'string' ? interactionPayload.env_var : '';
  const approvalCommand = typeof interactionPayload.command === 'string' ? interactionPayload.command : '';
  const approvalDescription = typeof interactionPayload.description === 'string' ? interactionPayload.description : '';

  return (
    <>
      {open ? <button className="chat-backdrop is-open" type="button" aria-label="Close chat" onClick={onClose} /> : null}
      <aside
        className={`chat-drawer ${open ? 'is-open' : ''}`}
        role="dialog"
        aria-modal="true"
        aria-label="Hermes chat"
        aria-labelledby="chat-drawer-title"
        aria-hidden={!open}
        inert={!open ? true : undefined}
        onKeyDown={handleDrawerKeyDown}
        onDragOver={(event) => { event.preventDefault(); setIsDragging(true); }}
        onDragLeave={() => setIsDragging(false)}
        onDrop={handleDrop}
      >
        <header className="chat-drawer-head">
          <div className="chat-head-main">
            <div className="chat-title">
              <span className="chat-mark" aria-hidden><Bot size={18} /></span>
              <div>
                <p className="eyebrow">Hermes</p>
                <h2 id="chat-drawer-title">Chat</h2>
              </div>
            </div>
            <div className="chat-head-actions">
              {submitting || running ? <Loader2 size={16} className="chat-header-loader chat-spin" aria-label="Hermes is working" /> : null}
              <span
                className={`chat-led ${statusClass}`}
                title={`Gateway connection: ${statusText}`}
                aria-label={`Gateway connection: ${statusText}`}
              >
                <span className="chat-led-dot" />
              </span>
              <button className="chat-new-button" type="button" onClick={() => void handleNewChat()} disabled={newChatLoading} title="Start a new chat" aria-label="Start a new chat">
                {newChatLoading ? <Loader2 size={15} className="chat-spin" /> : <SquarePen size={15} />}
                <span>New</span>
              </button>
              <button className="chat-icon-button" type="button" onClick={onClose} title="Close chat" aria-label="Close chat">
                <X size={18} />
              </button>
            </div>
          </div>
          <div className="chat-head-meta">
            <div className="chat-model-badge" title={modelIdentity ? `${modelIdentity.model}${modelIdentity.provider ? ` via ${modelIdentity.provider}` : ''}` : 'Resolving active model'}>
              <Cpu size={13} aria-hidden />
              <span>{modelIdentity?.model || 'Resolving model'}</span>
              {modelIdentity?.provider ? <small>{modelIdentity.provider}</small> : null}
            </div>
            <span
              className={`chat-status ${statusClass}`}
              title={statusText}
              aria-label={`Connection: ${statusText}`}
            >
              {statusText}
            </span>
          </div>
        </header>

        {modelPickerOpen ? (
          <ChatModelPicker
            request={request}
            sessionId={sessionId}
            currentModel={modelIdentity ? `${modelIdentity.provider ? `${modelIdentity.provider}/` : ''}${modelIdentity.model}` : undefined}
            initialRefresh={modelPickerRefresh}
            onClose={closeModelPicker}
            onSelect={switchModel}
          />
        ) : null}

        <div ref={scrollRef} onScroll={handleTranscriptScroll} className={`chat-transcript ${isDragging ? 'is-dragging' : ''}`} aria-live="polite">
          {isDragging ? (
            <div className="chat-drop-hint"><Paperclip size={20} /><span>Drop files to attach</span></div>
          ) : null}
          {renderMessages()}
          {!nearBottom ? (
            <button
              className="chat-scroll-fab"
              type="button"
              onClick={() => scrollToBottom('smooth')}
              aria-label="Scroll to latest message"
              title="Scroll to latest message"
            >
              <ChevronDown size={18} />
            </button>
          ) : null}
        </div>

        {activity && activity.kind !== 'tool' ? (
          <div className={`chat-activity is-${activity.state}`} role="status">
            {activity.kind === 'reasoning' ? <Sparkles size={15} className={activity.state === 'running' ? 'chat-spin' : ''} /> : <Loader2 size={15} className={activity.state === 'running' ? 'chat-spin' : ''} />}
            <strong>{activity.label}</strong>
            {activity.detail ? <span>{previewText(activity.detail)}</span> : null}
          </div>
        ) : null}

        {interaction ? (
          <section className={`chat-interaction chat-interaction-${interaction.kind}`} aria-label={interactionTitle(interaction)}>
            <div className="chat-interaction-heading">
              <span className="chat-interaction-icon">
                {interaction.kind === 'approval' ? <ShieldCheck size={16} /> : <KeyRound size={16} />}
              </span>
              <div>
                <strong>{interactionTitle(interaction)}</strong>
                <span>Answering here unblocks the running turn.</span>
              </div>
            </div>
            {interaction.kind === 'approval' ? (
              <>
                {approvalDescription ? <p className="chat-interaction-copy">{approvalDescription}</p> : null}
                {approvalCommand ? <code className="chat-command-preview">{approvalCommand}</code> : null}
                <div className="chat-choice-row">
                  {(interactionChoices.length ? interactionChoices : ['once', 'deny']).map((choice) => (
                    <button key={choice} type="button" className={`chat-choice ${choice === 'deny' ? 'is-danger' : ''}`} onClick={() => void respondInteraction(choice, choice, choice === 'always')}>
                      {choice === 'deny' ? 'Deny' : choice === 'always' ? 'Always allow' : choice === 'session' ? 'This session' : 'Allow once'}
                    </button>
                  ))}
                </div>
              </>
            ) : interaction.kind === 'clarify' ? (
              <>
                <p className="chat-interaction-copy">{interactionQuestion || 'Hermes is asking for a decision.'}</p>
                {interactionChoices.length ? (
                  <div className="chat-choice-row">
                    {interactionChoices.map((choice) => {
                      const selected = selectedChoices.includes(choice);
                      return (
                        <button
                          key={choice}
                          type="button"
                          className={`chat-choice ${selected ? 'is-selected' : ''}`}
                          onClick={() => {
                            if (multiSelect) setSelectedChoices((current) => selected ? current.filter((item) => item !== choice) : [...current, choice]);
                            else void respondInteraction(choice);
                          }}
                        >
                          {selected ? <Check size={14} /> : null}{choice}
                        </button>
                      );
                    })}
                  </div>
                ) : null}
                <div className="chat-interaction-input-row">
                  <input value={interactionDraft} onChange={(event) => setInteractionDraft(event.target.value)} placeholder="Type your answer" aria-label="Answer Hermes" />
                  <button type="button" className="chat-choice is-primary" disabled={!interactionDraft.trim() && (!multiSelect || selectedChoices.length === 0)} onClick={() => void respondInteraction(interactionDraft.trim() || selectedChoices.join(', '))}>Send</button>
                </div>
              </>
            ) : interaction.kind === 'terminal_read' ? (
              <>
                <p className="chat-interaction-copy">{interactionPrompt || 'Paste the requested terminal output.'}</p>
                <div className="chat-interaction-input-row">
                  <textarea value={interactionDraft} onChange={(event) => setInteractionDraft(event.target.value)} placeholder="Paste terminal output" aria-label="Terminal output" rows={3} />
                  <button type="button" className="chat-choice is-primary" disabled={!interactionDraft.trim()} onClick={() => void respondInteraction(interactionDraft.trim())}>Send</button>
                </div>
              </>
            ) : (
              <>
                {interaction.kind === 'secret' ? (
                  <p className="chat-interaction-copy">
                    {interactionPrompt || 'Hermes needs a secret to continue.'}
                    {secretEnvVar ? <><br /><code>{secretEnvVar}</code></> : null}
                  </p>
                ) : null}
                <div className="chat-interaction-input-row">
                  <input type="password" value={interactionDraft} onChange={(event) => setInteractionDraft(event.target.value)} placeholder={interaction.kind === 'sudo' ? 'Password' : secretEnvVar || 'Secret value'} aria-label={interaction.kind === 'sudo' ? 'Sudo password' : interactionPrompt || 'Secret value'} autoComplete="off" />
                  <button type="button" className="chat-choice is-primary" disabled={!interactionDraft} onClick={() => void respondInteraction(interactionDraft)}>Send</button>
                </div>
              </>
            )}
          </section>
        ) : null}

        {error ? (
          <div className="chat-error" role="alert">
            <span>{error}</span>
            <button type="button" onClick={() => void connect()}>Retry</button>
          </div>
        ) : (
          <p className="chat-preview">{lastPreview}</p>
        )}

        {pendingAttachments.length ? (
          <div className="chat-pending-attachments" aria-label="Pending attachments">
            {pendingAttachments.map((attachment) => (
              <div className="chat-pending-attachment" key={attachment.id}>
                {attachment.previewUrl ? <img src={attachment.previewUrl} alt="" /> : <AttachmentIcon kind={attachment.kind} />}
                <div className="chat-pending-attachment-copy"><strong>{attachment.name}</strong><span>{formatBytes(attachment.size ?? 0)}</span></div>
                <button type="button" onClick={() => removeAttachment(attachment.id)} aria-label={`Remove ${attachment.name}`}><X size={14} /></button>
              </div>
            ))}
          </div>
        ) : null}
        {attachmentNotice ? <p className="chat-attachment-notice" role="status">{attachmentNotice}</p> : null}

        <ChatSlashPopover
          ref={slashPopoverRef}
          input={draft}
          complete={completeSlash}
          onApply={setDraft}
        />
        <form className="chat-composer" onSubmit={handleSubmit}>
          <input ref={fileInputRef} type="file" multiple accept="image/*,application/pdf,*/*" className="chat-file-input" onChange={handleFileInput} />
          <button className="chat-icon-button chat-attach" type="button" onClick={() => fileInputRef.current?.click()} disabled={submitting || connectionState !== 'connected'} title="Attach image or file" aria-label="Attach image or file">
            <Paperclip size={17} />
          </button>
          <textarea
            ref={textareaRef}
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            onPaste={handlePaste}
            onKeyDown={handleComposerKeyDown}
            placeholder="Message Hermes"
            rows={1}
            disabled={connectionState !== 'connected'}
            aria-label="Message Hermes"
          />
          {running ? (
            <button className="chat-icon-button chat-stop" type="button" onClick={() => void interrupt()} aria-label="Interrupt response" title="Interrupt">
              <Pause size={17} />
            </button>
          ) : null}
          <button className="chat-send" type="submit" disabled={(!draft.trim() && pendingAttachments.length === 0) || submitting || connectionState !== 'connected'} aria-label="Send message" title="Send">
            {submitting ? <Loader2 size={17} className="chat-spin" /> : <Send size={17} />}
          </button>
        </form>
      </aside>
    </>
  );
}
