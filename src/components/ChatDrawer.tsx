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
  useMemo,
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
  Pen,
  Plus,
  ShieldCheck,
  SquarePen,
  X,
  XCircle,
  Users,
} from 'lucide-react';
import { ChatModelPicker } from './ChatModelPicker';
import { ChatComposer } from './ChatComposer';
import { ChatTodoPlan } from './chat/ChatTodoPlan';
import { Modal } from './Modal';
import { Button } from './ui/Button';
import type { ChatSlashPopoverHandle } from './ChatSlashPopover';
import type { CanvasAddonId } from '../canvas-addons/types';
import { CANVAS_ADDONS, ADDON_INDEX } from '../canvas-addons/manifest';
import { CanvasAddonHost } from '../canvas-addons/CanvasAddonHost';
import { CanvasAddonPicker } from '../canvas-addons/CanvasAddonPicker';
import type { CanvasAttachment } from '../canvas-addons/types';
import { ChatMessageCard } from './chat-messages';
import {
  MAX_ATTACHMENTS,
  MAX_ATTACHMENT_BYTES,
  classifyAttachment,
  interactionTitle,
  readFileAsDataUrl,
  useGatewayChat,
  type PendingAttachment,
} from '../lib/chat-gateway?mc=resume-v2';
import { markChatPresenceRead } from '../lib/chat-presence';
import { normalizeClarifyInteraction } from '../lib/chat-interactions';
import { previewText, type ChatAttachmentUpload, type ChatMessage, type GatewayInteractionRequest } from '../lib/chat-protocol';
import {
  loadMissionControlSessionPreview,
  type MissionControlAgentSessionItem,
  type MissionControlSessionPreviewMessage,
} from '../lib/hermes-api';
import { deriveTodoPlan, type TodoPlan } from '../lib/todo-plan';
import { loadBotProfiles } from '../lib/bot-gateway';
import type { BotMentionCandidate } from '../lib/bot-mentions';
import type { ChatMentionPopoverHandle } from './ChatMentionPopover';
import { createHandoffEnvelope, createHandoffDedupe, formatHandoffPrompt } from '../lib/bot-handoff';
import { addChatProfile } from '../lib/chat-session-params';
import { createHandoffObserver } from '../lib/bot-handoff-observer';
import { findHandoffCompletion } from '../lib/bot-handoff-recovery';
import { openHandoffClient } from '../lib/bot-handoff-client';
import { extractMentionRequest } from '../lib/bot-mentions';
import { canonicalChatCommand } from '../lib/bot-chat-policy';
import { classifyHandoffFailure } from '../lib/bot-handoff-reasons';
import { BotHandoffMessage } from './chat/BotHandoffMessage';
import { claimBotHandoff, loadPersistedBotHandoffs, persistBotHandoff, type PersistedBotHandoff } from '../lib/bot-handoff-persistence';
import { compareChatTimelineEntries } from '../lib/chat-timeline';
import { useGroupRoom } from '../lib/use-group-room';
import { GroupGatewayClient } from '../lib/group-gateway';
import type { GroupRoom } from '../lib/group-gateway';
import { CreateRoomForm, GroupRoomView } from './chat/GroupRoomView';
import { persistRoomVault } from '../lib/hermes-api';

type ChatDrawerProps = {
  open: boolean;
  storedToken: string;
  initialSessionId?: string | null;
  chatMode?: 'general' | 'canonical' | 'task' | 'room';
  roomId?: string | null;
  botProfile?: string | null;
  onClose: () => void;
  onStartTaskChat?: () => void;
  onOpenRooms?: () => void;
  onRoomChange?: (roomId: string | null) => void;
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

type CanonicalChatDrawerProps = Omit<ChatDrawerProps, 'chatMode'> & { chatMode?: 'general' | 'canonical' | 'task' };

function ChatModeTabs({ active, onSelect }: { active: 'chat' | 'rooms'; onSelect: (mode: 'chat' | 'rooms') => void }) {
  const { t } = useI18n();
  return (
    <div className="chat-mode-tabs" role="tablist" aria-label="Chat mode">
      <button type="button" className={`chat-mode-tab ${active === 'chat' ? 'is-active' : ''}`} role="tab" aria-selected={active === 'chat'} onClick={() => onSelect('chat')}>
        <MessageSquare size={14} />{t('chatDrawer.title')}
      </button>
      <button type="button" className={`chat-mode-tab ${active === 'rooms' ? 'is-active' : ''}`} role="tab" aria-selected={active === 'rooms'} onClick={() => onSelect('rooms')}>
        <Users size={14} />{t('rooms.title')}
      </button>
    </div>
  );
}

/**
 * Auto-hides the mode tab bar while the drawer content scrolls FAST and
 * re-shows it immediately when the user slows down, reaches the bottom
 * (auto-follow keeps it visible), or pauses for a while.
 *
 * Velocity-based: hiding only makes sense while the user is flying through
 * a long transcript; at low speed the rail stays, at the bottom it's always
 * visible because that's where the tab switch matters most. Listens in the
 * capture phase (scroll doesn't bubble); the first scroll on each element is
 * a baseline (mount-time auto-follow must not hide the rail).
 */
const TAB_SCROLL_RESUME_MS = 700;
const SCROLL_SPEED_HIDE_PX_MS = 0.2; // ≈ 200px/s — below this the rail stays
const BOTTOM_EPSILON_PX = 24;
function AutoHideModeTabs({ active, onSelect, containerRef }: { active: 'chat' | 'rooms'; onSelect: (mode: 'chat' | 'rooms') => void; containerRef: React.RefObject<HTMLElement | null> }) {
  const [hidden, setHidden] = useState(false);
  const hiddenRef = useRef(false);
  const shownAtRef = useRef(0);
  const seenRef = useRef(new WeakSet<HTMLElement>());
  const lastScrollRef = useRef(new Map<HTMLElement, { top: number; at: number }>());
  const setHiddenBoth = (next: boolean) => {
    hiddenRef.current = next;
    setHidden(next);
  };

  useEffect(() => {
    const node = containerRef.current;
    if (!node) return;
    const regionEls = () => node.querySelectorAll<HTMLElement>('.chat-transcript, [data-scroll-region]');
    const atBottom = (el: HTMLElement) => el.scrollHeight - el.scrollTop - el.clientHeight <= BOTTOM_EPSILON_PX;

    const onScroll = (event: Event) => {
      const target = event.target as HTMLElement | null;
      if (!target || typeof target.scrollTop !== 'number') return;
      const now = performance.now();
      const prev = lastScrollRef.current.get(target);
      lastScrollRef.current.set(target, { top: target.scrollTop, at: now });
      if (!seenRef.current.has(target)) {
        // first event for this element: auto-follow on mount, baseline only
        seenRef.current.add(target);
        return;
      }
      if (atBottom(target)) {
        setHiddenBoth(false);
        return;
      }
      if (!prev) return;
      const speed = Math.abs(target.scrollTop - prev.top) / Math.max(1, now - prev.at);
      if (speed >= SCROLL_SPEED_HIDE_PX_MS) {
        shownAtRef.current = now;
        if (!hiddenRef.current) setHiddenBoth(true);
      } else {
        setHiddenBoth(false);
      }
    };
    const resumeTimer = window.setInterval(() => {
      regionEls().forEach((el) => { if (atBottom(el)) setHiddenBoth(false); });
      if (shownAtRef.current !== 0 && performance.now() - shownAtRef.current >= TAB_SCROLL_RESUME_MS) {
        setHiddenBoth(false);
      }
    }, 200);
    node.addEventListener('scroll', onScroll, true);
    return () => {
      node.removeEventListener('scroll', onScroll, true);
      window.clearInterval(resumeTimer);
    };
  }, [containerRef]);

  return (
    <div className={`chat-mode-tabs-shell ${hidden ? 'is-hidden' : ''}`} aria-hidden={hidden}>
      <ChatModeTabs active={active} onSelect={onSelect} />
    </div>
  );
}

const CanonicalChatDrawer = memo(function CanonicalChatDrawer({ open, storedToken, initialSessionId, chatMode = 'general', botProfile, onClose, onStartTaskChat, onOpenRooms }: CanonicalChatDrawerProps) {
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
  const mentionPopoverRef = useRef<ChatMentionPopoverHandle | null>(null);
  const [botRoster, setBotRoster] = useState<BotMentionCandidate[]>([]);
  const [activeBotTarget, setActiveBotTarget] = useState<BotMentionCandidate | null>(null);
  const [handoffs, setHandoffs] = useState<PersistedBotHandoff[]>([]);
  const handoffDedupeRef = useRef(createHandoffDedupe());
  const handoffObserverRef = useRef<ReturnType<typeof createHandoffObserver> | null>(null);
  const handoffRecoveryRef = useRef(new Set<string>());
  const loadedHandoffIdsRef = useRef(new Set<string>());
  const titleRecoveryRef = useRef(new Set<string>());
  const pendingRef = useRef<PendingAttachment[]>([]);
  const [verbTick, setVerbTick] = useState(0);
  const [activeAddon, setActiveAddon] = useState<CanvasAddonId | null>(null);
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
    const stored = window.localStorage.getItem('mission-control-canvas-width');
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
    appendChatMessage,
    titleSession,
    ensureSession,
    claimLastChatPointer,
    appendSystemMessage,
    respondInteraction,
    interrupt,
    reset,
  } = useGatewayChat(storedToken, open, initialSessionId, botProfile);

  const activeTargetStorageKey = sessionId ? `mission-control-active-bot-target:${sessionId}` : null;
  const clearActiveBotTarget = useCallback(() => {
    setActiveBotTarget(null);
    if (!activeTargetStorageKey) return;
    try { window.localStorage.setItem(activeTargetStorageKey, 'cleared'); } catch { /* storage unavailable */ }
  }, [activeTargetStorageKey]);

  const rememberActiveBotTarget = useCallback((target: BotMentionCandidate) => {
    setActiveBotTarget(target);
    if (!activeTargetStorageKey) return;
    try { window.localStorage.removeItem(activeTargetStorageKey); } catch { /* storage unavailable */ }
  }, [activeTargetStorageKey]);

  useEffect(() => {
    if (!open || !sessionId) return;
    let cancelled = false;
    loadedHandoffIdsRef.current.clear();
    const references = [...new Set([sessionId, sessionKey].map((value) => value?.trim()).filter((value): value is string => Boolean(value)))];
    void Promise.all(references.map((reference) => loadPersistedBotHandoffs(storedToken, reference))).then((batches) => {
      const rows = [...new Map(batches.flat().map((row) => [row.id, row])).values()];
      if (cancelled) return;
      for (const row of rows) loadedHandoffIdsRef.current.add(row.id);
      setHandoffs(rows);
      let targetCleared = false;
      try { targetCleared = activeTargetStorageKey ? window.localStorage.getItem(activeTargetStorageKey) === 'cleared' : false; } catch { /* storage unavailable */ }
      const latest = [...rows].sort((left, right) => (right.updatedAt ?? 0) - (left.updatedAt ?? 0))[0];
      setActiveBotTarget(!targetCleared && latest ? {
        handle: latest.handle,
        displayName: latest.displayName,
        model: latest.model,
        provider: latest.provider,
      } : null);
    });
    return () => { cancelled = true; };
  }, [open, sessionId, sessionKey, storedToken, activeTargetStorageKey]);

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    loadBotProfiles(storedToken || undefined).then((result) => {
      if (cancelled) return;
      setBotRoster(result.profiles
        .filter((profile) => profile.is_bot === true)
        .map((profile) => ({
          handle: profile.name,
          displayName: profile.display_name || profile.name,
          description: profile.description || undefined,
          model: profile.model,
          provider: profile.provider,
        })));
    }).catch(() => {
      if (!cancelled) setBotRoster([]);
    });
    return () => { cancelled = true; };
  }, [open, storedToken]);

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
    if (!activeAddon) {
      setCanvasMountReady(false);
      return;
    }
    const frame = window.requestAnimationFrame(() => setCanvasMountReady(true));
    return () => window.cancelAnimationFrame(frame);
  }, [activeAddon]);

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
    loadMissionControlSessionPreview(token, initialSessionId, botProfile).then((item) => {
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
  }, [botProfile, initialSessionId, previewMode, storedToken]);

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

  const mentionHandles = useMemo(() => botRoster.map((bot) => bot.handle), [botRoster]);
  const handoffRequestIds = useMemo(
    () => new Set(handoffs.map((handoff) => `bot-request-${handoff.id}`)),
    [handoffs],
  );
  const existingMessageIds = useMemo(
    () => new Set(messages.map((message: ChatMessage) => message.id)),
    [messages],
  );
  const timelinedMessages = useMemo(() => [
    ...messages
      .filter((message: ChatMessage) => !handoffRequestIds.has(message.id))
      .map((message: ChatMessage) => ({ kind: 'message' as const, createdAt: message.createdAt ?? 0, order: 0, id: message.id, message })),
    ...handoffs.flatMap((handoff) => {
      const createdAt = handoff.createdAt ?? handoff.updatedAt;
      const requestMessage: ChatMessage = {
        id: `bot-request-${handoff.id}`,
        role: 'user',
        kind: 'user',
        source: 'live',
        text: `@${handoff.handle} ${handoff.request}`.trim(),
        status: 'complete',
        createdAt,
      };
      const replyMessage: ChatMessage | null = handoff.reply && !existingMessageIds.has(`bot-reply-${handoff.id}`)
        ? {
          id: `bot-reply-${handoff.id}`,
          role: 'assistant',
          kind: 'assistant',
          source: 'live',
          text: handoff.reply,
          status: 'complete',
          createdAt: handoff.updatedAt,
          attribution: {
            handle: handoff.handle,
            displayName: handoff.displayName,
            model: handoff.model,
            provider: handoff.provider,
          },
        }
        : null;
      return [
        { kind: 'message' as const, createdAt, order: 0, id: requestMessage.id, message: requestMessage },
        { kind: 'handoff' as const, createdAt, order: 1, id: handoff.id, handoff },
        ...(replyMessage ? [{ kind: 'message' as const, createdAt: handoff.updatedAt, order: 2, id: replyMessage.id, message: replyMessage }] : []),
      ];
    }),
  ].sort(compareChatTimelineEntries), [handoffs, handoffRequestIds, messages, existingMessageIds]);

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
        const previewMessages = (preview.recentMessages ?? []).filter((message) => !handoffs.some((handoff) => (
          handoff.reply?.trim() && handoff.reply.trim() === message.text.trim()
        )));
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
              {previewMessages.length > 0 ? previewMessages.map((msg, index) => (
                <ChatPreviewBubble key={`${msg.role}-${msg.timestamp ?? 'na'}-${index}`} message={msg} />
              )) : (
                <p className="chat-preview-fallback">{preview.preview || 'No recent messages available.'}</p>
              )}
              {handoffs.map((handoff) => (
                <BotHandoffMessage
                  key={`preview-${handoff.id}`}
                  handle={handoff.handle}
                  displayName={handoff.displayName}
                  model={handoff.model}
                  provider={handoff.provider}
                  request={handoff.request}
                  status={handoff.status}
                  reply={handoff.reply}
                  error={handoff.error}
                  reason={handoff.reason}
                />
              ))}
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
          {handoffs.length > 0 ? (
            <div className="chat-preview-body">
              {handoffs.map((handoff) => (
                <BotHandoffMessage
                  key={`preview-unavailable-${handoff.id}`}
                  handle={handoff.handle}
                  displayName={handoff.displayName}
                  model={handoff.model}
                  provider={handoff.provider}
                  request={handoff.request}
                  status={handoff.status}
                  reply={handoff.reply}
                  error={handoff.error}
                  reason={handoff.reason}
                />
              ))}
            </div>
          ) : null}
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

    if (messages.length === 0 && handoffs.length === 0) {
      return (
        <section className="chat-empty">
          <MessageSquare size={22} />
          <p>{t('chatDrawer.emptyTitle')}</p>
          <span>{t('chatDrawer.emptySubtitle')}</span>
        </section>
      );
    }

    const timeline = timelinedMessages;

    return (
      <>
        {timeline.map((entry) => entry.kind === 'message' ? (
          <ChatMessageCard key={entry.id} message={entry.message} mentionHandles={mentionHandles} />
        ) : (
          <BotHandoffMessage
            key={entry.id}
            handle={entry.handoff.handle}
            displayName={entry.handoff.displayName}
            model={botRoster.find((bot) => bot.handle === entry.handoff.handle)?.model}
            provider={botRoster.find((bot) => bot.handle === entry.handoff.handle)?.provider}
            request={entry.handoff.request}
            status={entry.handoff.status}
            reply={entry.handoff.reply}
            error={entry.handoff.error}
            reason={entry.handoff.reason}
            onRetry={entry.handoff.status === 'failed' && (entry.handoff.retryable !== false) ? () => {
              const handoff = entry.handoff;
              const handle = handoff.handle;
              rememberActiveBotTarget({
                handle,
                displayName: handoff.displayName,
                model: handoff.model,
                provider: handoff.provider,
              });
              if (!handoffDedupeRef.current.tryClaim(handle)) return;
              upsertHandoffState(handoff.id, { status: 'queued', error: null });
              const envelope = createHandoffEnvelope(
                { connectionId: 'local', profile: 'default', sessionId: sessionId ?? '' },
                { profile: handle, canonicalTitle: 'Bot Chat' },
                handoff.request,
              );
              const runRetry = async (): Promise<void> => {
                let client: Awaited<ReturnType<typeof openHandoffClient>> | null = null;
                try {
                  client = await openHandoffClient({ accessToken: storedToken || undefined });
                  const canonical = await client.resolveCanonical(handle);
                  upsertHandoffState(handoff.id, { targetSessionId: canonical.openedId });
                  const staleRuntimeId = await client.resume(handle, canonical.registryId);
                  await client.closeSession(staleRuntimeId);
                  upsertHandoffState(handoff.id, { status: 'running' });
                  const delivery = await client.deliver(handle, formatHandoffPrompt(envelope));
                  if (!delivery.deferred) {
                    handoffDedupeRef.current.release(handle);
                    client.close();
                    upsertHandoffState(handoff.id, { status: 'completed', reply: delivery.reply });
                    return;
                  }
                  const runtimeId = await client.resume(handle, canonical.registryId);
                  const baseline = await client.eventsSince(0);
                  const initialLastSeen = Math.max(
                    baseline.latest_seq ?? 0,
                    ...(baseline.events ?? []).map((event) => event.seq ?? 0),
                  );
                  const completedReply = findHandoffCompletion(baseline.events ?? [], envelope.handoffId);
                  if (completedReply) {
                    handoffDedupeRef.current.release(handle);
                    client.close();
                    upsertHandoffState(handoff.id, { status: 'completed', reply: completedReply });
                    return;
                  }
                  handoffObserverRef.current = createHandoffObserver(
                    { eventsSince: (params) => client!.eventsSince(params.last_seen) },
                    {
                      sessionId: runtimeId,
                      initialLastSeen,
                      intervalMs: 1200,
                      onEvent: () => {},
                      onComplete: (reply) => {
                        handoffDedupeRef.current.release(handle);
                        client?.close();
                        upsertHandoffState(handoff.id, { status: 'completed', reply });
                      },
                      onError: (message) => {
                        handoffDedupeRef.current.release(handle);
                        client?.close();
                        const failure = classifyHandoffFailure(message);
                        upsertHandoffState(handoff.id, { status: 'failed', error: message, reason: failure.reason, retryable: failure.retryable });
                      },
                    },
                  );
                  void handoffObserverRef.current.start();
                } catch (err) {
                  handoffDedupeRef.current.release(handle);
                  client?.close();
                  const message = err instanceof Error ? err.message : 'Handoff failed.';
                  const failure = classifyHandoffFailure(err);
                  setHandoffs((current) => current.map((item) => item.id === handoff.id ? {
                    ...item,
                    status: 'failed',
                    error: message,
                    reason: failure.reason,
                    retryable: failure.retryable,
                  } : item));
                }
              };
              void runRetry();
            } : undefined}
          />
        ))}
      </>
    );
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
  const headerSessionTitle = sessionTitle || preview?.title || t('chatDrawer.untitledSession');
  const contextEyebrow = chatMode === 'canonical'
    ? t('chatDrawer.primaryChat')
    : chatMode === 'task'
      ? t('chatDrawer.freshTaskChat')
      : t('chatDrawer.eyebrow');
  const contextTitle = botProfile?.trim() || t('chat.button');
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
  const [todoPlanVisible, setTodoPlanVisible] = useState(true);
  const showTodoPlan = todoPlanVisible ? visibleTodoPlan : null;
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

  const addScreenshotAttachment = useCallback(async (attachment: CanvasAttachment): Promise<boolean> => {
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

  const upsertHandoffState = useCallback((id: string, patch: Partial<PersistedBotHandoff>, originSessionId = sessionId ?? '') => {
    setHandoffs((current) => {
      const existing = current.find((item) => item.id === id);
      if (!existing) return current;
      const updated: PersistedBotHandoff = { ...existing, ...patch, updatedAt: Date.now() };
      void persistBotHandoff(storedToken, originSessionId, updated, sessionKey);
      return current.map((item) => item.id === id ? updated : item);
    });
  }, [sessionId, sessionKey, storedToken]);

  useEffect(() => {
    if (!open || !sessionId || !storedToken.trim()) return;
    const loaded = handoffs.filter((handoff) => loadedHandoffIdsRef.current.has(handoff.id));
    const pending = loaded.filter((handoff) => handoff.status === 'queued' || handoff.status === 'running');
    if (loaded.length === 0) return;
    let cancelled = false;

    const recover = async (): Promise<void> => {
      const titleHandoff = loaded[0];
      if (titleHandoff && !titleRecoveryRef.current.has(titleHandoff.id) && (!sessionTitle?.trim() || sessionTitle === 'Untitled session')) {
        titleRecoveryRef.current.add(titleHandoff.id);
        await titleSession(sessionId, titleHandoff.request);
      }
      for (const handoff of pending) {
        if (cancelled || handoffRecoveryRef.current.has(handoff.id)) continue;
        handoffRecoveryRef.current.add(handoff.id);
        let client: Awaited<ReturnType<typeof openHandoffClient>> | null = null;
        try {
          client = await openHandoffClient({ accessToken: storedToken });
          const canonical = await client.resolveCanonical(handoff.handle, handoff.targetSessionId);
          await client.resume(handoff.handle, canonical.registryId);
          const snapshot = await client.eventsSince(0);
          const reply = findHandoffCompletion(snapshot.events ?? [], handoff.id);
          if (!cancelled && reply) {
            upsertHandoffState(handoff.id, {
              targetSessionId: canonical.openedId,
              status: 'completed',
              reply,
              error: null,
            }, sessionId);
          }
        } catch {
          // The Bot may still be working or the gateway may be unavailable.
          // Leave the durable handoff state untouched; the next reopen retries.
        } finally {
          client?.close();
          handoffRecoveryRef.current.delete(handoff.id);
        }
      }
    };
    void recover();
    return () => { cancelled = true; };
  }, [handoffs, open, sessionId, sessionTitle, storedToken, titleSession, upsertHandoffState]);

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const text = draft;
    if (!text.trim() && pendingAttachments.length === 0) return;
    if (running && pendingAttachments.length > 0) {
      setAttachmentNotice('Attachments are available after the current response finishes. Send steer text only.');
      return;
    }
    setAttachmentNotice(null);

    // Bot handoff: a mention to a roster-marked Bot routes the request into the
    // target canonical Bot Chat. The origin transcript stays untouched; the
    // attributed reply is rendered as a dedicated handoff card.
    const mention = extractMentionRequest(text, botRoster);
    const trimmedText = text.trim();
    const followUp = !mention
      && activeBotTarget
      && pendingAttachments.length === 0
      && Boolean(trimmedText)
      && !trimmedText.startsWith('/')
      && !trimmedText.startsWith('@');
    const handoffMention = mention ?? (followUp ? {
      mention: `@${activeBotTarget.handle}`,
      request: trimmedText,
    } : null);
    if (handoffMention) {
      let originSessionId = await ensureSession();
      originSessionId = await claimLastChatPointer('submit', originSessionId);
      if (originSessionId !== sessionId) {
        originSessionId = await ensureSession(originSessionId);
      }
      const handle = handoffMention.mention.slice(1);
      const candidate = botRoster.find((item) => item.handle === handle) ?? (activeBotTarget?.handle === handle ? activeBotTarget : undefined);
      // Dedupe: a second submit for the same Bot while one is in flight is
      // ignored (double click, StrictMode, reconnect). The claim is released
      // when the handoff settles so an explicit Retry can re-run it.
      if (!handoffDedupeRef.current.tryClaim(handle)) {
        setAttachmentNotice('A request to this Bot is already in flight.');
        return;
      }
      const envelope = createHandoffEnvelope(
        { connectionId: 'local', profile: 'default', sessionId: originSessionId },
        { profile: handle, canonicalTitle: 'Bot Chat' },
        handoffMention.request,
      );
      const handoffId = envelope.handoffId;
      await titleSession(originSessionId, handoffMention.request || `@${handle}`);
      appendChatMessage({
        id: `bot-request-${handoffId}`,
        role: 'user',
        kind: 'user',
        source: 'live',
        text: `${handoffMention.mention} ${handoffMention.request}`.trim(),
        status: 'complete',
        createdAt: Date.now(),
      }, 'user_message');
      const initialHandoff: PersistedBotHandoff = {
        id: handoffId,
        handle,
        displayName: candidate?.displayName,
        model: candidate?.model,
        provider: candidate?.provider,
        request: handoffMention.request,
        status: 'queued',
        createdAt: Date.now(),
        updatedAt: Date.now(),
      };
      const claimResult = await claimBotHandoff(storedToken, originSessionId, initialHandoff);
      if (claimResult === false) {
        handoffDedupeRef.current.release(handle);
        setAttachmentNotice('This handoff was already claimed by another Mission Control client.');
        return;
      }
      rememberActiveBotTarget({
        handle,
        displayName: candidate?.displayName,
        model: candidate?.model,
        provider: candidate?.provider,
      });
      setHandoffs((current) => [...current, initialHandoff]);
      void persistBotHandoff(storedToken, originSessionId, initialHandoff, sessionKey);
      setDraft('');
      const runHandoff = async (): Promise<void> => {
        let client: Awaited<ReturnType<typeof openHandoffClient>> | null = null;
        try {
          client = await openHandoffClient({ accessToken: storedToken || undefined });
          const canonical = await client.resolveCanonical(handle);
          upsertHandoffState(handoffId, { targetSessionId: canonical.openedId }, originSessionId);
          // A dashboard-local Bot Chat may still be live with a stale MCP snapshot.
          // Close only that runtime; the canonical transcript/profile remains intact.
          const staleRuntimeId = await client.resume(handle, canonical.registryId);
          await client.closeSession(staleRuntimeId);
          upsertHandoffState(handoffId, { status: 'running' }, originSessionId);
          const delivery = await client.deliver(handle, formatHandoffPrompt(envelope));
          if (!delivery.deferred) {
            handoffDedupeRef.current.release(handle);
            client.close();
            const attributedReply: ChatMessage = {
              id: `bot-reply-${handoffId}`,
              role: 'assistant',
              kind: 'assistant',
              source: 'live',
              text: delivery.reply,
              status: 'complete',
              createdAt: Date.now(),
              attribution: {
                handle,
                displayName: candidate?.displayName,
                model: candidate?.model,
                provider: candidate?.provider,
              },
            };
            appendChatMessage(attributedReply, 'assistant_message');
            upsertHandoffState(handoffId, { status: 'completed', reply: delivery.reply }, originSessionId);
            return;
          }

          // The target Bot Chat is already live in this gateway. The relay has queued
          // the request there; attach an observer so the UI keeps the working state
          // and receives the actual Bot reply instead of the transport acknowledgement.
          const runtimeId = await client.resume(handle, canonical.registryId);
          const baseline = await client.eventsSince(0);
          const initialLastSeen = Math.max(
            baseline.latest_seq ?? 0,
            ...(baseline.events ?? []).map((event) => event.seq ?? 0),
          );
          const completedReply = findHandoffCompletion(baseline.events ?? [], handoffId);
          if (completedReply) {
            handoffDedupeRef.current.release(handle);
            client.close();
            appendChatMessage({
              id: `bot-reply-${handoffId}`,
              role: 'assistant',
              kind: 'assistant',
              source: 'live',
              text: completedReply,
              status: 'complete',
              createdAt: Date.now(),
              attribution: {
                handle,
                displayName: candidate?.displayName,
                model: candidate?.model,
                provider: candidate?.provider,
              },
            }, 'assistant_message');
            upsertHandoffState(handoffId, { status: 'completed', reply: completedReply }, originSessionId);
            return;
          }
          handoffObserverRef.current = createHandoffObserver(
            { eventsSince: (params) => client!.eventsSince(params.last_seen) },
            {
              sessionId: runtimeId,
              initialLastSeen,
              intervalMs: 1200,
              onEvent: () => {},
              onComplete: (reply) => {
                handoffDedupeRef.current.release(handle);
                client?.close();
                const attributedReply: ChatMessage = {
                  id: `bot-reply-${handoffId}`,
                  role: 'assistant',
                  kind: 'assistant',
                  source: 'live',
                  text: reply,
                  status: 'complete',
                  createdAt: Date.now(),
                  attribution: {
                    handle,
                    displayName: candidate?.displayName,
                    model: candidate?.model,
                    provider: candidate?.provider,
                  },
                };
                appendChatMessage(attributedReply, 'assistant_message');
                upsertHandoffState(handoffId, { status: 'completed', reply }, originSessionId);
              },
              onError: (message) => {
                handoffDedupeRef.current.release(handle);
                client?.close();
                const failure = classifyHandoffFailure(message);
                upsertHandoffState(handoffId, { status: 'failed', error: message, reason: failure.reason, retryable: failure.retryable }, originSessionId);
              },
            },
          );
          void handoffObserverRef.current.start();
        } catch (err) {
          handoffDedupeRef.current.release(handle);
          client?.close();
          const message = err instanceof Error ? err.message : 'Handoff failed.';
          const failure = classifyHandoffFailure(err);
          setHandoffs((current) => current.map((item) => item.id === handoffId ? {
            ...item,
            status: 'failed',
            error: message,
            reason: failure.reason,
            retryable: failure.retryable,
          } : item));
        }
      };
      void runHandoff();
      return;
    }

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
      const sent = await submitPrompt(running && !uploads.length
        ? `/steer ${text}`
        : text.trim().startsWith('/')
          ? canonicalChatCommand(chatMode, text.trim().slice(1))
          : text, uploads);
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
    if (mentionPopoverRef.current?.handleKey(event)) return;
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      event.currentTarget.form?.requestSubmit();
    }
  };

  const handleDrawerKeyDown = (event: KeyboardEvent<HTMLElement>) => {
    if (event.key === 'Escape') {
      if (activeAddon) {
        closeAddon();
      } else {
        onClose();
      }
      return;
    }
    // Non intercettare frecce quando il focus è su un campo di testo
    // (il cursore deve muoversi, non ridimensionare il pannello).
    const target = event.target as HTMLElement | null;
    if (target && (target.tagName === 'TEXTAREA' || target.tagName === 'INPUT')) return;
    if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
      event.preventDefault();
      setDrawerWidth(prev => 
        event.key === 'ArrowLeft' 
          ? Math.max(360, (prev ?? 480) - 20)
          : Math.min(900, (prev ?? 480) + 20)
      );
      return;
    }
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
    clearActiveBotTarget();
    try {
      await reset();
      if (chatMode === 'canonical') onStartTaskChat?.();
    } finally {
      setNewChatLoading(false);
    }
  };

  // Desktop resize: drag the left-edge handle to change the drawer width.
  // The drawer is anchored right, so width = viewport width - cursor x.
  const startResize = (event: React.MouseEvent) => {
    if (activeAddon) return; // expanded mode owns its own geometry
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
      const finalWidth = drawerRef.current ? Math.min(Math.max(parseInt(drawerRef.current.style.width, 10) || 360, 360), 900) : 360;
      setDrawerWidth(finalWidth);
      try { window.localStorage.setItem('mission-control-chat-width', String(finalWidth)); } catch { /* storage unavailable */ }
    };
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';
  };

  const startCanvasResize = (event: React.MouseEvent) => {
    if (!activeAddon) return;
    event.preventDefault();
    canvasResizingRef.current = true;
    const onMove = (moveEvent: MouseEvent) => {
      if (!canvasResizingRef.current) return;
      const viewportWidth = window.innerWidth;
      const maxCanvasWidth = Math.max(420, Math.min(1000, viewportWidth - 376));
      const width = Math.min(Math.max(viewportWidth - moveEvent.clientX, 420), maxCanvasWidth);
      const chatWidth = Math.max(360, Math.min(720, Math.round(720 - (width - window.innerWidth * 0.44))));
      pendingCanvasWidthRef.current = width;
      document.documentElement.style.setProperty('--mission-control-canvas-width', `${width}px`);
      document.documentElement.style.setProperty('--mission-control-expanded-chat-width', `${chatWidth}px`);
      document.documentElement.style.setProperty('--mission-control-expanded-chat-right', `${width}px`);
    };
    const onUp = () => {
      canvasResizingRef.current = false;
      const finalWidth = pendingCanvasWidthRef.current;
      if (finalWidth != null) {
        setCanvasWidth(finalWidth);
        try { window.localStorage.setItem('mission-control-canvas-width', String(finalWidth)); } catch { /* storage unavailable */ }
      }
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
    };
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';
  };

  const interactionPayload = interaction?.payload ?? {};
  const clarifyContent = interaction?.kind === 'clarify' ? normalizeClarifyInteraction(interactionPayload) : null;
  const interactionChoices = clarifyContent?.choices ?? (Array.isArray(interactionPayload.choices)
    ? interactionPayload.choices.filter((choice): choice is string => typeof choice === 'string' && choice.trim().length > 0)
    : []);
  const multiSelect = clarifyContent?.multiSelect ?? (interactionPayload.multi_select === true);
  const interactionQuestion = clarifyContent?.question ?? (typeof interactionPayload.question === 'string' ? interactionPayload.question : '');
  const hasLongInteractionChoice = interactionChoices.some((choice) => choice.length > 42);
  const interactionPrompt = typeof interactionPayload.prompt === 'string' ? interactionPayload.prompt : '';
  const secretEnvVar = typeof interactionPayload.env_var === 'string' ? interactionPayload.env_var : '';
  const approvalCommand = typeof interactionPayload.command === 'string' ? interactionPayload.command : '';
  const approvalDescription = typeof interactionPayload.description === 'string' ? interactionPayload.description : '';
  const canvasSendSelection = useCallback(async (text: string, canvasAttachments: CanvasAttachment[] = []) => {
    if (canvasAttachments.length > 0) {
      return addScreenshotAttachment(canvasAttachments[0]);
    }
    return submitPrompt(text);
  }, [addScreenshotAttachment, submitPrompt]);
  const canvasReady = useCallback(() => setIsCanvasLoading(false), []);
  const canvasRetry = useCallback(() => window.location.reload(), []);
  const closeAddon = useCallback(() => {
    setActiveAddon(null);
    setIsCanvasLoading(false);
    setCanvasMountReady(false);
  }, []);
  const openAddon = useCallback((id: CanvasAddonId) => {
    setCanvasMountReady(false);
    setIsCanvasLoading(true);
    setActiveAddon(id);
  }, []);

    useEffect(() => {
    if (!activeAddon) {
      document.documentElement.style.removeProperty('--mission-control-canvas-width');
      document.documentElement.style.removeProperty('--mission-control-expanded-chat-width');
      document.documentElement.style.removeProperty('--mission-control-expanded-chat-right');
      return;
    }
    const width = canvasWidth ?? Math.round(window.innerWidth * 0.44);
    const chatWidth = Math.max(360, Math.min(720, Math.round(720 - (width - window.innerWidth * 0.44))));
    document.documentElement.style.setProperty('--mission-control-canvas-width', `${width}px`);
    document.documentElement.style.setProperty('--mission-control-expanded-chat-width', `${chatWidth}px`);
    document.documentElement.style.setProperty('--mission-control-expanded-chat-right', `${width}px`);
  }, [canvasWidth, activeAddon]);

  return (
    <>
      {open ? <button className="chat-backdrop is-open" type="button" aria-label={t('chatDrawer.close')} onClick={onClose} /> : null}
      <aside
        ref={drawerRef}
        className={`chat-drawer ${open ? 'is-open' : ''} ${activeAddon ? 'is-expanded' : ''}`}
        style={drawerWidth && !activeAddon ? { width: drawerWidth } : undefined}
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
        {!activeAddon ? (
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
                <p className="eyebrow">{contextEyebrow}</p>
                <h2 id="chat-drawer-title">{contextTitle}</h2>
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
              <CanvasAddonPicker
                addons={CANVAS_ADDONS}
                activeAddon={activeAddon}
                onOpen={openAddon}
                onClose={closeAddon}
              />
              <button className="chat-control chat-icon-button" type="button" onClick={onClose} title={t('chatDrawer.close')} aria-label={t('chatDrawer.close')}>
                <X size={18} />
              </button>
            </div>
          </div>
        </header>
        {onOpenRooms ? <AutoHideModeTabs active="chat" onSelect={(mode) => { if (mode === 'rooms') onOpenRooms(); }} containerRef={drawerRef} /> : null}

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

        <div ref={scrollRef} onScroll={handleTranscriptScroll} className={`chat-transcript ${previewMode ? 'is-preview' : ''} ${showTodoPlan ? 'has-todo-plan' : ''} ${isDragging ? 'is-dragging' : ''}`} aria-live="polite">
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

        <div className={`chat-runtime-footer ${interaction ? 'has-interaction' : ''}`}>
          {error ? (
            <div className="chat-error" role="alert">
              <span>{error}</span>
              <button type="button" onClick={() => void connect()}>{t('chatDrawer.retry')}</button>
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
                    <div className={`chat-choice-row ${hasLongInteractionChoice ? 'has-long-choice' : ''}`}>
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
          <ChatTodoPlan plan={showTodoPlan} waitingForInput={Boolean(interaction)} />
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
          mentionPopoverRef={mentionPopoverRef}
          botRoster={botRoster}
          activeBotTarget={activeBotTarget}
          onClearBotTarget={clearActiveBotTarget}
          running={running}
          submitting={submitting}
          disabled={connectionState !== 'connected'}
          todoVisible={todoPlanVisible}
          onToggleTodo={() => setTodoPlanVisible((v) => !v)}
        />
      </aside>
      {open && activeAddon && ADDON_INDEX[activeAddon] ? (canvasMountReady ? (
        <CanvasAddonHost
          addon={ADDON_INDEX[activeAddon]}
          sessionId={sessionId}
          sessionKey={sessionKey}
          sessionTitle={headerSessionTitle}
          storedToken={storedToken}
          onSendPayload={canvasSendSelection}
          onActionApplied={appendSystemMessage}
          onReady={canvasReady}
          loading={isCanvasLoading}
          expanded={true}
          width={canvasWidth}
          onClose={closeAddon}
        />
      ) : null) : null}
      {open && activeAddon ? (
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

function GroupChatDrawer({ open, roomId, storedToken, onClose, onRoomChange }: ChatDrawerProps) {
  const { t } = useI18n();
  const state = useGroupRoom({ enabled: open, initialRoomId: roomId ?? null });
  const canUseRooms = state.capabilities?.driver === true && state.driverAvailable;
  const [creating, setCreating] = useState(false);
  const [roomPickerOpen, setRoomPickerOpen] = useState(false);
  const [editingName, setEditingName] = useState(false);
  const [nameDraft, setNameDraft] = useState('');
  const [botCandidates, setBotCandidates] = useState<BotMentionCandidate[]>([]);
  const drawerRef = useRef<HTMLElement>(null);
  const resizingRef = useRef(false);

  // Share the canonical drawer width so Rooms and Chat keep the same size:
  // read the same localStorage key the chat drawer persists through its resize handle.
  useEffect(() => {
    if (!open) return;
    try {
      const stored = parseInt(window.localStorage.getItem('mission-control-chat-width') || '', 10);
      if (Number.isFinite(stored) && stored >= 360 && stored <= 900 && drawerRef.current) {
        drawerRef.current.style.width = `${stored}px`;
      }
    } catch { /* storage unavailable */ }
  }, [open]);

  const startResize = (event: React.MouseEvent) => {
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
      const finalWidth = drawerRef.current ? Math.min(Math.max(parseInt(drawerRef.current.style.width, 10) || 540, 360), 900) : 540;
      try { window.localStorage.setItem('mission-control-chat-width', String(finalWidth)); } catch { /* storage unavailable */ }
    };
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';
  };
  const mentionRoster = useMemo(() => (state.room?.members ?? [])
    .filter((member) => member.handle.trim())
    .map((member) => ({
      handle: member.handle,
      displayName: member.displayName || member.profile || member.handle,
      description: member.profile ? `profile: ${member.profile}` : undefined,
    })), [state.room?.members]);

  useEffect(() => {
    if (!open || botCandidates.length > 0) return;
    let cancelled = false;
    void loadBotProfiles()
      .then((payload) => {
        if (cancelled) return;
        setBotCandidates(payload.profiles
          .filter((profile) => (profile.is_bot || profile.is_default) && profile.name.trim())
          .map((profile) => ({
            handle: profile.name,
            displayName: profile.display_name || profile.name,
            description: profile.model ? `model: ${profile.model}` : undefined,
          })));
      })
      .catch(() => { /* roster is best-effort for creation UI */ });
    return () => { cancelled = true; };
  }, [botCandidates.length, open]);

  useEffect(() => {
    if (!open || !state.selectedRoomId || state.selectedRoomId === roomId) return;
    onRoomChange?.(state.selectedRoomId);
  }, [onRoomChange, open, roomId, state.selectedRoomId]);

  const selectRoom = useCallback((nextRoomId: string | null) => {
    onRoomChange?.(nextRoomId);
    return state.selectRoom(nextRoomId);
  }, [onRoomChange, state.selectRoom]);

  const createRoomFromDrawer = useCallback(async (name: string, handles: string[], vaultId?: string) => {
    const client = new GroupGatewayClient();
    const roster = handles.map((handle, index) => ({
      id: `member-${handle}-${index}`,
      profile: handle === 'default' ? 'default' : handle,
      handle,
      displayName: (botCandidates.find((m) => m.handle === handle) ?? mentionRoster.find((m) => m.handle === handle))?.displayName,
    }));
    const room = await client.create({ roomId: `mc-${Date.now()}`.slice(0, 48), name, roster });
    if (vaultId) {
      void persistRoomVault(room.id, vaultId, storedToken?.trim() || undefined).catch(() => undefined);
    }
    setCreating(false);
    await state.refresh();
    await state.selectRoom(room.id);
  }, [botCandidates, mentionRoster, state, storedToken]);

  return (
    <>
      {open ? <button className="chat-backdrop is-open" type="button" aria-label={t('rooms.close')} onClick={onClose} /> : null}
      <aside ref={drawerRef} className={`chat-drawer ${open ? 'is-open' : ''}`} role="dialog" aria-modal="true" aria-label={t('rooms.eyebrow')} aria-hidden={!open} inert={!open ? true : undefined}>
        <div className="chat-drawer-resize-handle" role="separator" aria-orientation="vertical" aria-label={t('chatDrawer.resize')} title={t('chatDrawer.dragToResize')} onMouseDown={startResize} />
        <header className="chat-drawer-head">
          <div className="chat-head-main">
            <div className="chat-head-identity">
              <span className="chat-mark" aria-hidden><Users size={18} /></span>
              <div className="chat-head-copy">
                <p className="eyebrow">{t('rooms.eyebrow')}</p>
                {editingName && state.room ? <form onSubmit={(event) => { event.preventDefault(); if (nameDraft.trim() && nameDraft.trim() !== state.room?.name) void state.renameRoom(nameDraft); setEditingName(false); }} className="flex min-w-0 items-center gap-1"><input autoFocus value={nameDraft} onChange={(event) => setNameDraft(event.target.value)} onBlur={() => setEditingName(false)} onKeyDown={(event) => { if (event.key === 'Escape') setEditingName(false); }} aria-label={t('rooms.name')} className="mc-input h-7 min-w-0 flex-1 text-xs" /><button type="submit" aria-label={t('rooms.saveName')} title={t('rooms.saveName')} className="chat-control chat-icon-button !h-7 !w-7"><Check size={14} /></button></form> : <h2 title={state.rooms.length > 0 ? t('rooms.selectRoom') : undefined} className={state.rooms.length > 0 ? 'inline-flex cursor-pointer items-center gap-1 hover:text-accent' : ''} onClick={state.rooms.length > 0 ? () => setRoomPickerOpen((open) => !open) : undefined}>{state.room?.name || t('rooms.title')}{state.rooms.length > 0 ? <ChevronDown size={13} className={`shrink-0 transition-transform ${roomPickerOpen ? 'rotate-180' : ''}`} /> : null}</h2>}
                {state.room && !state.disbanded ? <span className="chat-session-title">{t('rooms.membersCount', { count: state.room.members.length })} · {t('rooms.messagesCount', { count: state.events.length })}</span> : <span className="chat-session-title">{t('rooms.selectRoom')}</span>}
              </div>
            </div>
            <div className="chat-head-actions">
              {state.refreshing ? <Loader2 size={16} className="chat-spin chat-header-loader" aria-label={t('rooms.refresh')} /> : null}
              <span className={`chat-led ${state.disbanded || state.serviceUnavailable ? 'is-offline' : state.pendingActions.length > 0 || state.blocked ? 'is-pending' : 'is-online'}`} title={state.disbanded || state.serviceUnavailable ? t('rooms.ledOffline') : state.pendingActions.length > 0 || state.blocked ? t('rooms.ledPending') : t('rooms.ledOnline')} aria-label={state.disbanded || state.serviceUnavailable ? t('rooms.ledOffline') : state.pendingActions.length > 0 || state.blocked ? t('rooms.ledPending') : t('rooms.ledOnline')}><span className="chat-led-dot" /></span>
              <button className="chat-control chat-icon-button" type="button" onClick={() => { if (state.room) { setNameDraft(state.room.name ?? ''); setEditingName(true); } }} title={t('rooms.renameRoom')} aria-label={t('rooms.renameRoom')} disabled={!state.room || state.disbanded}><Pen size={15} /></button>
              <button className="chat-control chat-icon-button" type="button" onClick={() => setCreating((current) => !current)} title={creating ? t('rooms.close') : t('rooms.create')} aria-label={creating ? t('rooms.close') : t('rooms.create')}>{creating ? <X size={16} /> : <Plus size={16} />}</button>
              <button className="chat-control chat-icon-button" type="button" onClick={onClose} title={t('rooms.close')} aria-label={t('rooms.close')}><X size={18} /></button>
            </div>
          </div>
          {roomPickerOpen && state.rooms.length > 0 ? (
            <div className="absolute left-3 right-3 top-full z-20 mt-1 flex max-h-56 flex-col overflow-y-auto rounded-lg border border-border-subtle bg-surface shadow-lg" role="listbox">
              {state.rooms.map((room: GroupRoom) => <button key={room.id} type="button" role="option" aria-selected={room.id === state.selectedRoomId} onClick={() => { void selectRoom(room.id); setRoomPickerOpen(false); }} className={`px-2.5 py-1.5 text-left text-xs ${room.id === state.selectedRoomId ? 'bg-accent-subtle text-accent' : 'text-text hover:bg-surface-sunken'}`}>{room.name || room.id}</button>)}
            </div>
          ) : null}
        </header>
        <AutoHideModeTabs active="rooms" onSelect={(mode) => { if (mode === 'chat') onRoomChange ? onRoomChange(null) : onClose(); }} containerRef={drawerRef} />
        <div className="flex min-h-0 flex-1 flex-col">
          {!canUseRooms && !state.loading ? <div className="chat-error m-4" role="status">{t('rooms.driverUnavailable')}</div> : null}
          {creating ? <div className="chat-transcript rooms-empty"><CreateRoomForm members={botCandidates.length > 0 ? botCandidates : mentionRoster} onCancel={() => setCreating(false)} onCreate={createRoomFromDrawer} /></div> : state.room && canUseRooms ? <GroupRoomView state={state} mentionRoster={botCandidates.length > 0 ? botCandidates : mentionRoster} onSend={(text) => state.send(text, `room:${state.room?.id ?? state.selectedRoomId}`)} /> : canUseRooms ? <div className="chat-transcript rooms-empty"><p className="chat-empty">{t('rooms.chooseRoom')}</p></div> : null}
          {state.error && !state.serviceUnavailable ? <p className="chat-error" role="alert">{state.error.message}</p> : null}
        </div>
      </aside>
    </>
  );
}

export const ChatDrawer = memo(function ChatDrawer(props: ChatDrawerProps) {
  if (props.chatMode === 'room') return <GroupChatDrawer {...props} />;
  const { chatMode, roomId: _roomId, onRoomChange: _onRoomChange, ...canonicalProps } = props;
  return <CanonicalChatDrawer {...canonicalProps} chatMode={chatMode} />;
});
