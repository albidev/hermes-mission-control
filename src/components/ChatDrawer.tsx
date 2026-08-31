import { useI18n } from '../lib/i18n';
import {
  Component,
  type ClipboardEvent,
  type CSSProperties,
  type DragEvent,
  type FormEvent,
  type ErrorInfo,
  type KeyboardEvent,
  type ReactNode,
  lazy,
  memo,
  Suspense,
  useCallback,
  useEffect,
  useRef,
  useState,
} from 'react';
import {
  ArrowLeft,
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
  ShieldCheck,
  SquarePen,
  X,
  XCircle,
} from 'lucide-react';
import { ChatModelPicker } from './ChatModelPicker';
import { ChatComposer } from './ChatComposer';
import { ChatTodoPlan } from './chat/ChatTodoPlan';
import { Modal } from './Modal';
import { Button } from './ui/Button';
import type { ChatSlashPopoverHandle } from './ChatSlashPopover';
import type { CanvasScreenshotAttachment } from './TLDrawCanvas';
import { ChatMessageCard } from './chat-messages';
import {
  MAX_ATTACHMENTS,
  MAX_ATTACHMENT_BYTES,
  classifyAttachment,
  interactionTitle,
  readFileAsDataUrl,
  useGatewayChat,
  type PendingAttachment,
} from '../lib/chat-gateway';
import { markChatPresenceRead } from '../lib/chat-presence';
import { previewText, type ChatAttachmentUpload, type GatewayInteractionRequest } from '../lib/chat-protocol';
import {
  loadMissionControlSessionPreview,
  type MissionControlAgentSessionItem,
  type MissionControlSessionPreviewMessage,
} from '../lib/hermes-api';
import { deriveTodoPlan, type TodoPlan } from '../lib/todo-plan';

type ChatDrawerProps = {
  open: boolean;
  storedToken: string;
  initialSessionId?: string | null;
  onClose: () => void;
};

function formatTokens(tokens: number): string {
  if (tokens < 1000) return `${tokens}`;
  if (tokens < 1_000_000) return `${(tokens / 1000).toFixed(tokens < 10_000 ? 1 : 0)}k`;
  return `${(tokens / 1_000_000).toFixed(tokens < 10_000_000 ? 1 : 0).replace(/\.0$/, '')}M`;
}

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

function TLDrawLoadingShell({ onClose, error, onRetry, width }: { onClose: () => void; error?: boolean; onRetry?: () => void; width?: number | null }) {
  const { t } = useI18n();
  return (
    <section className="tldraw-canvas-panel is-expanded" aria-label={error ? 'TLDrawCanvas unavailable' : 'TLDrawCanvas loading'}>
      <header className="tldraw-canvas-head">
        <div className="tldraw-canvas-title">
          <button type="button" className="chat-icon-button tldraw-canvas-back" onClick={onClose} title={t('chatDrawer.backToChat')} aria-label={t('chatDrawer.backToChat')}>
            <ArrowLeft size={18} />
          </button>
          <div>
            <span className="eyebrow">{t('tldraw.sessionCanvas')}</span>
            <h3>TLDrawCanvas</h3>
            <span className="tldraw-canvas-linked-session">{error ? t('chatDrawer.canvasUnavailable') : t('chatDrawer.preparingWorkspace')}</span>
          </div>
        </div>
      </header>
      <div className="tldraw-canvas-loading" role={error ? 'alert' : 'status'}>
        {error ? <XCircle size={24} aria-hidden /> : <Loader2 size={24} className="chat-spin" />}
        <strong>{error ? t('tldraw.unavailable') : t('tldraw.opening')}</strong>
        <span>{error ? t('tldraw.checkConnection') : t('tldraw.preparingWorkspace')}</span>
        {error && onRetry ? <button type="button" className="chat-control" onClick={onRetry}>{t('chatDrawer.retry')}</button> : null}
      </div>
    </section>
  );
}

class TLDrawErrorBoundary extends Component<{
  children: ReactNode;
  onClose: () => void;
  onRetry: () => void;
  width?: number | null;
}, { hasError: boolean }> {
  state = { hasError: false };

  static getDerivedStateFromError() {
    return { hasError: true };
  }

  componentDidCatch(error: Error, _info: ErrorInfo) {
    console.error('[TLDrawCanvas] failed to load:', error);
  }

  render() {
    if (!this.state.hasError) return this.props.children;
    return <TLDrawLoadingShell onClose={this.props.onClose} error onRetry={this.props.onRetry} width={this.props.width} />;
  }
}

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
  const { t } = useI18n();
  const [draft, setDraft] = useState('');
  const [pendingAttachments, setPendingAttachments] = useState<PendingAttachment[]>([]);
  const [attachmentNotice, setAttachmentNotice] = useState<string | null>(null);
  const [isDragging, setIsDragging] = useState(false);
  const [interactionDraft, setInteractionDraft] = useState('');
  const [selectedChoices, setSelectedChoices] = useState<string[]>([]);
  const [newChatLoading, setNewChatLoading] = useState(false);
  const [newChatConfirmOpen, setNewChatConfirmOpen] = useState(false);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const [nearBottom, setNearBottom] = useState(true);
  const nearBottomRef = useRef(true);
  const programmaticScrollRef = useRef(false);
  const scrollFrameRef = useRef<number | null>(null);
  const slashPopoverRef = useRef<ChatSlashPopoverHandle | null>(null);
  const pendingRef = useRef<PendingAttachment[]>([]);
  const [verbTick, setVerbTick] = useState(0);
  const [isExpanded, setIsExpanded] = useState(false);
  const [isCanvasLoading, setIsCanvasLoading] = useState(false);
  const [canvasMountReady, setCanvasMountReady] = useState(false);
  // Desktop drawer width, adjustable via the left-edge resize handle.
  // Default matches the CSS `min(540px, 100vw)`; clamped to a sane range.
  // Persisted to localStorage so the width survives reloads.
  const [drawerWidth, setDrawerWidth] = useState<number | null>(() => {
    if (typeof window === 'undefined') return null;
    const stored = window.localStorage.getItem('mission-control-chat-width');
    if (!stored) return null;
    const parsed = Number(stored);
    return Number.isFinite(parsed) && parsed >= 360 && parsed <= 900 ? parsed : null;
  });
  const [canvasWidth, setCanvasWidth] = useState<number | null>(() => {
    if (typeof window === 'undefined') return null;
    const stored = window.localStorage.getItem('mission-control-tldraw-width');
    const parsed = stored ? Number(stored) : NaN;
    return Number.isFinite(parsed) && parsed >= 420 && parsed <= 1000 ? parsed : null;
  });
  const resizingRef = useRef(false);
  const drawerRef = useRef<HTMLElement | null>(null);
  const canvasResizingRef = useRef(false);
  const canvasResizeFrameRef = useRef<number | null>(null);
  const pendingCanvasWidthRef = useRef<number | null>(null);
  const {
    messages,
    todoPlan: gatewayTodoPlan,
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

  useEffect(() => {
    if (!running) {
      setVerbTick(0);
      return;
    }
    const timer = window.setInterval(() => setVerbTick((tick) => tick + 1), 2400);
    return () => window.clearInterval(timer);
  }, [running]);

  useEffect(() => {
    pendingRef.current = pendingAttachments;
  }, [pendingAttachments]);

  useEffect(() => {
    if (!isExpanded) {
      setCanvasMountReady(false);
      return;
    }
    const frame = window.requestAnimationFrame(() => setCanvasMountReady(true));
    return () => window.cancelAnimationFrame(frame);
  }, [isExpanded]);

  const [preview, setPreview] = useState<MissionControlAgentSessionItem | null>(null);
  const [previewTodoPlan, setPreviewTodoPlan] = useState<TodoPlan | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);

  useEffect(() => {
    if (!initialSessionId) {
      setPreview(null);
      setPreviewTodoPlan(null);
      setPreviewLoading(false);
      return;
    }
    // Keep the sanitized preview plan available as a fallback during an
    // explicit Resume: older live runtimes may not return todo_state.
    if (!previewMode) {
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
        setPreviewTodoPlan(item?.todoPlan ?? null);
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

  const isNearBottom = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return true;
    return el.scrollHeight - el.scrollTop - el.clientHeight < 80;
  }, []);

  const handleTranscriptScroll = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    const bottom = isNearBottom();
    if (programmaticScrollRef.current) {
      // Safari emits intermediate scroll events while a programmatic scroll is
      // settling. Do not let those transient positions resurrect the FAB.
      if (bottom) programmaticScrollRef.current = false;
      else return;
    }
    if (bottom && open) {
      const assistantCount = messages.filter((message) => message.role === 'assistant' && message.status !== 'streaming').length;
      markChatPresenceRead(sessionKey, assistantCount);
      // Reaching the bottom is the read acknowledgement.
    }
    nearBottomRef.current = bottom;
    setNearBottom(bottom);
  }, [isNearBottom, messages, open, sessionKey]);

  useEffect(() => {
    if (!open) return;
    const frame = window.requestAnimationFrame(() => {
      if (!isNearBottom()) return;
      const assistantCount = messages.filter((message) => message.role === 'assistant' && message.status !== 'streaming').length;
      markChatPresenceRead(sessionKey, assistantCount);
      nearBottomRef.current = true;
      setNearBottom(true);
    });
    return () => window.cancelAnimationFrame(frame);
  }, [isNearBottom, messages, open, sessionKey]);

  const scrollToBottom = useCallback((behavior: ScrollBehavior = 'auto') => {
    programmaticScrollRef.current = true;
    if (scrollFrameRef.current !== null) window.cancelAnimationFrame(scrollFrameRef.current);
    const el = scrollRef.current;
    if (!el) { programmaticScrollRef.current = false; return; }
    if (behavior === 'auto') {
      // Sync scroll for immediate responses (FAB, programmatic jumps).
      // No RAF delay — critical on iOS where every frame counts.
      scrollFrameRef.current = null;
      el.scrollTo({ top: el.scrollHeight, behavior: 'auto' });
      programmaticScrollRef.current = false;
      return;
    }
    // 'smooth' is queued via RAF to stay responsive during token streaming.
    scrollFrameRef.current = window.requestAnimationFrame(() => {
      scrollFrameRef.current = null;
      const e = scrollRef.current;
      if (!e) { programmaticScrollRef.current = false; return; }
      e.scrollTo({ top: e.scrollHeight, behavior: 'smooth' });
      programmaticScrollRef.current = false;
    });
  }, []);

  useEffect(() => () => {
    if (scrollFrameRef.current !== null) {
      window.cancelAnimationFrame(scrollFrameRef.current);
      scrollFrameRef.current = null;
    }
    programmaticScrollRef.current = false;
  }, []);

  const renderMessages = () => {
    if (previewMode) {
      if (previewLoading) {
        return (
          <section className="chat-preview-surface">
            <div className="chat-preview-empty">
              <Loader2 size={20} className="chat-spin" />
              <p>{t('chatDrawer.loadingPreview')}</p>
            </div>
          </section>
        );
      }
      if (preview) {
        return (
          <section className="chat-preview-surface">
            <div className="chat-preview-heading">
              <div>
                <p className="eyebrow">{t('chatDrawer.sessionPreview')}</p>
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
            <p>{t('chatDrawer.previewUnavailable')}</p>
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
          <p>{t('chatDrawer.emptyTitle')}</p>
          <span>{t('chatDrawer.emptySubtitle')}</span>
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
    // Never queue smooth animations while messages are changing. One scroll per
    // animation frame keeps Safari/iOS responsive during token streaming.
    scrollToBottom('auto');
  }, [messages, interaction, open, scrollToBottom]);

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
  const derivedTodoPlan = deriveTodoPlan(messages);
  const visibleTodoPlan = gatewayTodoPlan ?? previewTodoPlan ?? derivedTodoPlan;
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

  const addScreenshotAttachment = useCallback(async (attachment: CanvasScreenshotAttachment): Promise<boolean> => {
    if (running) {
      setAttachmentNotice('Attachments are available after the current response finishes.');
      return false;
    }
    if (pendingRef.current.length >= MAX_ATTACHMENTS) {
      setAttachmentNotice(`You can attach up to ${MAX_ATTACHMENTS} files per message.`);
      return false;
    }
    try {
      const response = await fetch(attachment.dataUrl);
      const blob = await response.blob();
      const file = new File([blob], attachment.name, { type: attachment.mimeType });
      const pending: PendingAttachment = {
        id: `${attachment.name}-${Date.now()}`,
        kind: attachment.kind,
        name: attachment.name,
        size: file.size,
        mimeType: attachment.mimeType,
        file,
        previewUrl: URL.createObjectURL(file),
      };
      setPendingAttachments((current) => [...current, pending]);
      setAttachmentNotice('Screenshot added to the message.');
      return true;
    } catch {
      setAttachmentNotice('Could not add the board screenshot.');
      return false;
    }
  }, [running]);
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

  const handleNewChat = () => {
    if (newChatLoading) return;
    setNewChatConfirmOpen(true);
  };

  const confirmNewChat = async () => {
    if (newChatLoading) return;
    setNewChatConfirmOpen(false);
    setNewChatLoading(true);
    try {
      await reset();
    } finally {
      setNewChatLoading(false);
    }
  };

  // Desktop resize: drag the left-edge handle to change the drawer width.
  // The drawer is anchored right, so width = viewport width - cursor x.
  const startResize = (event: React.MouseEvent) => {
    if (isExpanded) return; // expanded mode owns its own geometry
    event.preventDefault();
    resizingRef.current = true;
    const onMove = (moveEvent: MouseEvent) => {
      if (!resizingRef.current) return;
      const width = Math.min(Math.max(window.innerWidth - moveEvent.clientX, 360), Math.min(900, window.innerWidth - 16));
      if (drawerRef.current) drawerRef.current.style.width = `${width}px`;
    };
    const onUp = () => {
      resizingRef.current = false;
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
      // Persist the final width so it survives reloads.
      setDrawerWidth((current) => {
        if (current != null) {
          try { window.localStorage.setItem('mission-control-chat-width', String(current)); } catch { /* storage unavailable */ }
        }
        return current;
      });
    };
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';
  };

  const startCanvasResize = (event: React.MouseEvent) => {
    if (!isExpanded) return;
    event.preventDefault();
    canvasResizingRef.current = true;
    const onMove = (moveEvent: MouseEvent) => {
      if (!canvasResizingRef.current) return;
      const width = Math.min(Math.max(window.innerWidth - moveEvent.clientX, 420), Math.min(1000, window.innerWidth - 376));
      const chatWidth = Math.max(360, Math.min(720, Math.round(720 - (width - window.innerWidth * 0.44))));
      pendingCanvasWidthRef.current = width;
      document.documentElement.style.setProperty('--mission-control-tldraw-width', `${width}px`);
      document.documentElement.style.setProperty('--mission-control-expanded-chat-width', `${chatWidth}px`);
      document.documentElement.style.setProperty('--mission-control-expanded-chat-right', `${width}px`);
    };
    const onUp = () => {
      canvasResizingRef.current = false;
      if (pendingCanvasWidthRef.current != null) setCanvasWidth(pendingCanvasWidthRef.current);
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
      setCanvasWidth((current) => {
        if (current != null) {
          try { window.localStorage.setItem('mission-control-tldraw-width', String(current)); } catch { /* storage unavailable */ }
        }
        return current;
      });
    };
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';
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
    if (canvasAttachments.length > 0) {
      return addScreenshotAttachment(canvasAttachments[0]);
    }
    return submitPrompt(text);
  }, [addScreenshotAttachment, submitPrompt]);
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
  useEffect(() => {
    if (!isExpanded) {
      document.documentElement.style.removeProperty('--mission-control-tldraw-width');
      document.documentElement.style.removeProperty('--mission-control-expanded-chat-width');
      document.documentElement.style.removeProperty('--mission-control-expanded-chat-right');
      return;
    }
    const width = canvasWidth ?? Math.round(window.innerWidth * 0.44);
    const chatWidth = Math.max(360, Math.min(720, Math.round(720 - (width - window.innerWidth * 0.44))));
    document.documentElement.style.setProperty('--mission-control-tldraw-width', `${width}px`);
    document.documentElement.style.setProperty('--mission-control-expanded-chat-width', `${chatWidth}px`);
    document.documentElement.style.setProperty('--mission-control-expanded-chat-right', `${width}px`);
  }, [canvasWidth, isExpanded]);

  return (
    <>
      {open ? <button className="chat-backdrop is-open" type="button" aria-label={t('chatDrawer.close')} onClick={onClose} /> : null}
      <aside
        ref={drawerRef}
        className={`chat-drawer ${open ? 'is-open' : ''} ${isExpanded ? 'is-expanded' : ''}`}
        style={drawerWidth && !isExpanded ? { width: drawerWidth } : undefined}
        role="dialog"
        aria-modal="true"
        aria-label={t('chatDrawer.chatLabel')}
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
            aria-label={t('chatDrawer.resize')}
            title={t('chatDrawer.dragToResize')}
            onMouseDown={startResize}
          />
        ) : null}
        <header className="chat-drawer-head">
          <div className="chat-head-main">
            <div className="chat-head-identity">
              <span className="chat-mark" aria-hidden><Bot size={18} /></span>
              <div className="chat-head-copy">
                <p className="eyebrow">{t('chatDrawer.eyebrow')}</p>
                <h2 id="chat-drawer-title">{t('chat.button')}</h2>
                <span className="chat-session-title" title={headerSessionTitle}>{headerSessionTitle}</span>
              </div>
            </div>
            <div className="chat-head-actions">
              {submitting || running ? <Loader2 size={16} className="chat-header-loader chat-spin" aria-label={t('chatDrawer.working')} /> : null}
              <span
                className={`chat-led ${statusClass}`}
                title={`Gateway connection: ${statusText}`}
                aria-label={`Gateway connection: ${statusText}`}
              >
                <span className="chat-led-dot" />
              </span>
              <button className="chat-new-button" type="button" onClick={handleNewChat} disabled={newChatLoading} title={t('chatDrawer.startNew')} aria-label={t('chatDrawer.startNew')}>
                {newChatLoading ? <Loader2 size={15} className="chat-spin" /> : <SquarePen size={15} />}
                <span>{t('kanban.new')}</span>
              </button>
              <button className="chat-control chat-icon-button chat-tldraw-button" type="button" onPointerDown={openCanvas} onClick={openCanvas} title={t('chatDrawer.openTldraw')} aria-label={t('chatDrawer.openTldraw')}>
                {isCanvasLoading ? <Loader2 size={16} className="chat-spin" /> : <TldrawMark size={34} />}
              </button>
              <button className="chat-control chat-icon-button" type="button" onClick={onClose} title={t('chatDrawer.close')} aria-label={t('chatDrawer.close')}>
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

        <div ref={scrollRef} onScroll={handleTranscriptScroll} className={`chat-transcript ${previewMode ? 'is-preview' : ''} ${visibleTodoPlan ? 'has-todo-plan' : ''} ${isDragging ? 'is-dragging' : ''}`} aria-live="polite">
          {isDragging ? (
            <div className="chat-drop-hint"><Paperclip size={20} /><span>{t('chatDrawer.dropFiles')}</span></div>
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
              aria-label={t('chatDrawer.scrollLatest')}
              title={t('chatDrawer.scrollLatest')}
            >
              <ChevronDown size={18} />
            </button>
          ) : null}
        </div>

        {interaction ? (
          <section className={`chat-interaction chat-interaction-${interaction.kind}`} aria-label={interactionTitle(interaction)}>
            <div className="chat-interaction-heading">
              <span className="chat-interaction-icon">
                {interaction.kind === 'approval' ? <ShieldCheck size={16} /> : <KeyRound size={16} />}
              </span>
              <div>
                <strong>{interactionTitle(interaction)}</strong>
                <span>{t('interaction.unblocks')}</span>
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
                  <input value={interactionDraft} onChange={(event) => setInteractionDraft(event.target.value)} placeholder={t('interaction.typeAnswer')} aria-label={t('interaction.answerHermes')} />
                  <button type="button" className="chat-choice is-primary" disabled={!interactionDraft.trim() && (!multiSelect || selectedChoices.length === 0)} onClick={() => void respondInteraction(interactionDraft.trim() || selectedChoices.join(', '))}>{t('kanban.send')}</button>
                </div>
              </>
            ) : interaction.kind === 'terminal_read' ? (
              <>
                <p className="chat-interaction-copy">{interactionPrompt || 'Paste the requested terminal output.'}</p>
                <div className="chat-interaction-input-row">
                  <textarea value={interactionDraft} onChange={(event) => setInteractionDraft(event.target.value)} placeholder={t('interaction.pasteOutputPlaceholder')} aria-label={t('interaction.terminalOutputAria')} rows={3} />
                  <button type="button" className="chat-choice is-primary" disabled={!interactionDraft.trim()} onClick={() => void respondInteraction(interactionDraft.trim())}>{t('kanban.send')}</button>
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
                  <button type="button" className="chat-choice is-primary" disabled={!interactionDraft} onClick={() => void respondInteraction(interactionDraft)}>{t('kanban.send')}</button>
                </div>
              </>
            )}
          </section>
        ) : null}

        {error ? (
          <div className="chat-error" role="alert">
            <span>{error}</span>
            <button type="button" onClick={() => void connect()}>{t('chatDrawer.retry')}</button>
          </div>
        ) : null}

        <div className="chat-runtime-footer">
          <ChatTodoPlan plan={visibleTodoPlan} waitingForInput={Boolean(interaction)} />
          <div className="chat-status-line" role="status">
            <span className={`chat-status-line-verb ${running ? 'is-streaming' : statusLineLabel === 'Ready' ? 'is-ready' : ''}`}>
              {running ? (
                <span className="chat-status-line-kaomoji" aria-hidden>{streamingKaomoji}</span>
              ) : null}
              {statusLineLabel}
            </span>
            <span className="chat-status-line-separator">|</span>
            <span className="chat-status-line-model-group">
              <span className="chat-status-line-model" title={modelIdentity ? `${modelIdentity.model}${modelIdentity.provider ? ` via ${modelIdentity.provider}` : ''}` : 'Model not available'}>
                {modelIdentity?.model || 'Model unavailable'}
              </span>
              <span className="chat-status-line-separator">|</span>
              <span className="chat-status-line-reasoning">
                {modelIdentity?.reasoningEffort || '—'}
              </span>
            </span>
            <span className="chat-status-line-separator">|</span>
            <span className="chat-status-line-ctx" title={contextTokens == null ? 'Context usage not available yet' : `${contextTokens.toLocaleString()} / ${contextWindow.toLocaleString()} context tokens`}>
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
              title={contextTokens == null ? 'Context usage not available yet' : `${Math.round(contextPercent)}% of context window`}
            >
              <span className="chat-status-line-bar-fill" style={{ width: `${contextPercent}%` }} />
            </span>
            <span
              className="chat-status-line-ring"
              role="progressbar"
              aria-label={t('chatDrawer.contextWindowUsage')}
              aria-valuemin={0}
              aria-valuemax={100}
              aria-valuenow={Math.round(contextPercent)}
              style={{ '--chat-context-progress': `${contextPercent}%` } as CSSProperties}
              title={contextTokens == null ? 'Context usage not available yet' : `${Math.round(contextPercent)}% of context window`}
            />
            <span className="chat-status-line-percent">{contextTokens == null ? '—' : `${Math.round(contextPercent)}%`}</span>
          </div>
        </div>
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
        <TLDrawErrorBoundary onClose={canvasClose} onRetry={canvasRetry} width={canvasWidth}>
          <Suspense fallback={<TLDrawLoadingShell onClose={canvasClose} width={canvasWidth} />}>
            <LazyTLDrawCanvas sessionId={sessionId} sessionKey={sessionKey} sessionTitle={headerSessionTitle} storedToken={storedToken} onSendSelection={canvasSendSelection} onActionApplied={appendSystemMessage} onReady={canvasReady} loading={isCanvasLoading} expanded={isExpanded} width={canvasWidth} onClose={canvasClose} />
          </Suspense>
        </TLDrawErrorBoundary>
      ) : <TLDrawLoadingShell onClose={canvasClose} width={canvasWidth} />) : null}
      {open && isExpanded ? (
        <div
          className="tldraw-canvas-resize-handle"
          role="separator"
          aria-orientation="vertical"
          aria-label={t('ui.resizeCanvas')}
          title={t('chatDrawer.dragToResize')}
          style={{ right: canvasWidth ?? '44vw' }}
          onMouseDown={startCanvasResize}
        />
      ) : null}
      <Modal
        open={newChatConfirmOpen}
        className="new-chat-confirm-modal"
        title={t('chatDrawer.startNew')}
        subtitle={t('chatDrawer.startNewConfirm')}
        onClose={() => setNewChatConfirmOpen(false)}
        footer={(
          <>
            <Button variant="ghost" size="sm" type="button" onClick={() => setNewChatConfirmOpen(false)}>{t('kanban.cancel')}</Button>
            <Button size="sm" type="button" onClick={() => void confirmNewChat()} disabled={newChatLoading}>{t('chatDrawer.confirmStartNew')}</Button>
          </>
        )}
      >
        <p className="text-sm text-text-muted">{t('chatDrawer.startNewConfirm')}</p>
      </Modal>
    </>
  );
});
