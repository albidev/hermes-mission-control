import {
  type FormEvent,
  type KeyboardEvent,
  lazy,
  memo,
  Suspense,
  useCallback,
  useEffect,
  useRef,
  useState,
} from 'react';
import {
  Bot,
  ChevronDown,
  Cpu,
  Loader2,
  MessageSquare,
  Paperclip,
  SquarePen,
  X,
} from 'lucide-react';
import { ChatModelPicker } from './ChatModelPicker';
import { ChatComposer } from './ChatComposer';
import { ChatStatusLine } from './chat/ChatStatusLine';
import { ChatInteractionPanel } from './chat/ChatInteractionPanel';
import { ChatCanvasErrorBoundary, ChatCanvasLoadingShell } from './chat/ChatCanvasBoundary';
import { useChatAttachments } from '../hooks/useChatAttachments';
import { useChatDrawerResize } from '../hooks/useChatDrawerResize';
import { useChatTranscriptScroll } from '../hooks/useChatTranscriptScroll';
import type { ChatSlashPopoverHandle } from './ChatSlashPopover';
import type { CanvasScreenshotAttachment } from './TLDrawCanvas';
import { ChatMessageCard } from './chat-messages';
import {
  readFileAsDataUrl,
  useGatewayChat,
} from '../lib/chat-gateway';
import { type ChatAttachmentUpload } from '../lib/chat-protocol';
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

// Best-effort context-window estimate for the progress bar. The gateway does
// not stream context usage mid-turn, so we derive a percentage from the model
// name against known windows, falling back to a conservative default. This is
// display-only and never affects the actual context budget.
const CONTEXT_WINDOWS: Array<[RegExp, number]> = [
  [/gemma4|gemma-4/i, 128_000],
  [/qwen3\.5|qwen3-?5/i, 128_000],
  [/deepseek/i, 128_000],
  [/gpt-4o|gpt-4\.1/i, 128_000],
  [/claude/i, 200_000],
  [/llama-?3/i, 128_000],
  [/mistral/i, 128_000],
];

const DEFAULT_CONTEXT_WINDOW = 128_000;

function estimateContextWindow(model: string | null | undefined): number {
  if (!model) return DEFAULT_CONTEXT_WINDOW;
  for (const [pattern, window] of CONTEXT_WINDOWS) {
    if (pattern.test(model)) return window;
  }
  return DEFAULT_CONTEXT_WINDOW;
}

const LazyTLDrawCanvas = lazy(() => import('./TLDrawCanvas').then(({ TLDrawCanvas }) => ({ default: TLDrawCanvas })));

const TUI_VERBS = [
  'pondering',
  'contemplating',
  'musing',
  'cogitating',
  'ruminating',
  'deliberating',
  'mulling',
  'reflecting',
  'processing',
  'reasoning',
  'analyzing',
  'computing',
  'synthesizing',
  'formulating',
  'brainstorming',
];

// Hermes thinking kaomoji — same set as hermes-avatar-esp (simulator/gen_sprites.py THINKING).
// Cycled in lockstep with TUI_VERBS so the face and verb stay in sync.
const TUI_KAOMOJI = [
  '(._.)', '(◔_◔)', '(¬_¬)', '(•_•)', '(⌐■_■)', '(~_~)',
  '◉_◉', '(°_°)', '(˘_˘)♡', '(>_>)', '(o‿o)', '(◉_◉)',
  '(¬_¬)', '(ಠ_ಠ)', 'ಠ_ಠ',
];

function TldrawMark({ size = 16 }: { size?: number }) {
  return <span className="tldraw-brand-icon" aria-hidden="true" style={{ width: size, height: size }} />;
}

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

export const ChatDrawer = memo(function ChatDrawer({ open, storedToken, initialSessionId, onClose }: ChatDrawerProps) {
  const [draft, setDraft] = useState('');
  const [interactionDraft, setInteractionDraft] = useState('');
  const [selectedChoices, setSelectedChoices] = useState<string[]>([]);
  const [newChatLoading, setNewChatLoading] = useState(false);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const slashPopoverRef = useRef<ChatSlashPopoverHandle | null>(null);
  const [verbTick, setVerbTick] = useState(0);
  const [isExpanded, setIsExpanded] = useState(false);
  const [isCanvasLoading, setIsCanvasLoading] = useState(false);
  const [canvasMountReady, setCanvasMountReady] = useState(false);
  const { pendingAttachments, attachmentNotice, isDragging, removeAttachment, handleFileInput, handleDrop, handlePaste, setIsDragging, setAttachmentNotice, clearAttachments } = useChatAttachments();
  const { drawerWidth, startResize } = useChatDrawerResize(isExpanded);

  const {
    messages,
    sessionId,
    sessionKey,
    connectionState,
    statusText,
    error,
    submitting,
    running,
    interaction,
    previewMode,
    modelIdentity,
    contextTokens,
    contextMax,
    sessionTitle,
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
    appendSystemMessage,
    respondInteraction,
    interrupt,
    reset,
  } = useGatewayChat(storedToken, open, initialSessionId);

  const { scrollRef, nearBottom, nearBottomRef, setNearBottom, handleTranscriptScroll, scrollToBottom } = useChatTranscriptScroll({ messages, open, sessionKey });

  useEffect(() => {
    if (!running) {
      setVerbTick(0);
      return;
    }
    const timer = window.setInterval(() => setVerbTick((tick) => tick + 1), 2400);
    return () => window.clearInterval(timer);
  }, [running]);

  useEffect(() => {
    if (!isExpanded) {
      setCanvasMountReady(false);
      return;
    }
    const frame = window.requestAnimationFrame(() => setCanvasMountReady(true));
    return () => window.cancelAnimationFrame(frame);
  }, [isExpanded]);

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
    setInteractionDraft('');
    setSelectedChoices([]);
  }, [interaction?.requestId, interaction?.kind]);

  const statusClass = connectionState === 'connected'
    ? 'is-online'
    : connectionState === 'reconnecting' || connectionState === 'connecting' || connectionState === 'ticket'
      ? 'is-pending'
      : 'is-offline';
  const headerSessionTitle = sessionTitle || preview?.title || 'Untitled session';
  const streamingVerb = TUI_VERBS[verbTick % TUI_VERBS.length] || 'processing';
  const streamingKaomoji = TUI_KAOMOJI[verbTick % TUI_KAOMOJI.length] || '(._.)';
  const statusLineLabel = interaction
    ? 'Waiting for input'
    : running
      ? streamingVerb
      : connectionState === 'connected'
        ? 'Ready'
        : statusText;
  const contextWindow = contextMax || estimateContextWindow(modelIdentity?.model);
  const contextPercent = contextTokens == null ? 0 : Math.min(100, (contextTokens / contextWindow) * 100);
  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const text = draft;
    if (!text.trim() && pendingAttachments.length === 0) return;
    if (running && pendingAttachments.length > 0) {
      setAttachmentNotice('Attachments are available after the current response finishes. Send steer text only.');
      return;
    }
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
      const sent = await submitPrompt(running && !uploads.length ? `/steer ${text}` : text, uploads);
      if (!sent) return;
      setDraft('');
      // Sending a message always snaps back to the bottom, even if the user
      // had scrolled up to read earlier content. Reset the near-bottom flag so
      // the messages effect also follows once the new message lands.
      nearBottomRef.current = true;
      setNearBottom(true);
      scrollToBottom('auto');
      clearAttachments();
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
    if (event.key === 'Escape') {
      if (isExpanded) {
        setIsExpanded(false);
        setIsCanvasLoading(false);
      }
      else onClose();
    }
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
  const canvasSendSelection = useCallback(async (text: string, canvasAttachments: CanvasScreenshotAttachment[] = []) => {
    return submitPrompt(text, canvasAttachments.map((a) => ({
      id: a.name,
      kind: a.kind,
      name: a.name,
      size: Math.round(a.dataUrl.length * 0.75),
      mimeType: a.mimeType,
      dataUrl: a.dataUrl,
    })));
  }, [submitPrompt]);
  const canvasReady = useCallback(() => setIsCanvasLoading(false), []);
  const canvasRetry = useCallback(() => window.location.reload(), []);
  const openCanvas = useCallback(() => {
    setCanvasMountReady(false);
    setIsCanvasLoading(true);
    setIsExpanded(true);
  }, []);

  const canvasClose = useCallback(() => {
    setCanvasMountReady(false);
    setIsExpanded(false);
    setIsCanvasLoading(false);
  }, []);

  return (
    <>
      {open ? <button className="chat-backdrop is-open" type="button" aria-label="Close chat" onClick={onClose} /> : null}
      <aside
        className={`chat-drawer ${open ? 'is-open' : ''} ${isExpanded ? 'is-expanded' : ''}`}
        style={drawerWidth && !isExpanded ? { width: drawerWidth } : undefined}
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
        {!isExpanded ? (
          <div
            className="chat-drawer-resize-handle"
            role="separator"
            aria-orientation="vertical"
            aria-label="Resize chat width"
            title="Drag to resize"
            onMouseDown={startResize}
          />
        ) : null}
        <header className="chat-drawer-head">
          <div className="chat-head-main">
            <div className="chat-head-identity">
              <span className="chat-mark" aria-hidden><Bot size={18} /></span>
              <div className="chat-head-copy">
                <p className="eyebrow">Hermes</p>
                <h2 id="chat-drawer-title">Chat</h2>
                <span className="chat-session-title" title={headerSessionTitle}>{headerSessionTitle}</span>
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
              <button className="chat-control chat-icon-button chat-tldraw-button" type="button" onPointerDown={openCanvas} onClick={openCanvas} title="Open TLDrawCanvas" aria-label="Open TLDrawCanvas">
                {isCanvasLoading ? <Loader2 size={16} className="chat-spin" /> : <TldrawMark size={34} />}
              </button>
              <button className="chat-control chat-icon-button" type="button" onClick={onClose} title="Close chat" aria-label="Close chat">
                <X size={18} />
              </button>
            </div>
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
              onClick={() => {
                nearBottomRef.current = true;
                setNearBottom(true);
                scrollToBottom('auto');
              }}
              aria-label="Scroll to latest message"
              title="Scroll to latest message"
            >
              <ChevronDown size={18} />
            </button>
          ) : null}
        </div>

        {interaction ? (
          <ChatInteractionPanel
            interaction={interaction}
            choices={interactionChoices}
            multiSelect={multiSelect}
            selectedChoices={selectedChoices}
            interactionDraft={interactionDraft}
            onDraftChange={setInteractionDraft}
            onChoice={(choice) => {
              const selected = selectedChoices.includes(choice);
              if (multiSelect) setSelectedChoices((current) => selected ? current.filter((item) => item !== choice) : [...current, choice]);
              else void respondInteraction(choice);
            }}
            onSubmit={(answer, choice, resolveAll) => void respondInteraction(answer, choice, resolveAll)}
            approvalCommand={approvalCommand}
            approvalDescription={approvalDescription}
            interactionQuestion={interactionQuestion}
            interactionPrompt={interactionPrompt}
            secretEnvVar={secretEnvVar}
          />
        ) : null}

        {error ? (
          <div className="chat-error" role="alert">
            <span>{error}</span>
            <button type="button" onClick={() => void connect()}>Retry</button>
          </div>
        ) : null}

        <ChatStatusLine
          statusLineLabel={statusLineLabel}
          running={running}
          streamingKaomoji={streamingKaomoji}
          modelIdentity={modelIdentity}
          contextTokens={contextTokens}
          contextWindow={contextWindow}
          contextPercent={contextPercent}
        />
        <ChatComposer
          draft={draft}
          onDraftChange={setDraft}
          pendingAttachments={pendingAttachments}
          attachmentNotice={attachmentNotice}
          onRemoveAttachment={removeAttachment}
          onFileInput={handleFileInput}
          onPaste={handlePaste}
          onKeyDown={handleComposerKeyDown}
          onSubmit={handleSubmit}
          onStop={() => void interrupt()}
          completeSlash={completeSlash}
          textareaRef={textareaRef}
          slashPopoverRef={slashPopoverRef}
          running={running}
          submitting={submitting}
          disabled={connectionState !== 'connected'}
        />
      </aside>
      {open && isExpanded ? (canvasMountReady ? (
        <ChatCanvasErrorBoundary onClose={canvasClose} onRetry={canvasRetry}>
          <Suspense fallback={<ChatCanvasLoadingShell onClose={canvasClose} />}>
            <LazyTLDrawCanvas sessionId={sessionId} sessionKey={sessionKey} sessionTitle={headerSessionTitle} storedToken={storedToken} onSendSelection={canvasSendSelection} onActionApplied={appendSystemMessage} onReady={canvasReady} loading={isCanvasLoading} expanded={isExpanded} onClose={canvasClose} />
          </Suspense>
        </ChatCanvasErrorBoundary>
      ) : <ChatCanvasLoadingShell onClose={canvasClose} />) : null}
    </>
  );
});
