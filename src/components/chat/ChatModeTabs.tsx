import { useCallback, useEffect, useRef, useState, type RefObject } from 'react';
import { ChevronDown, History, MessageSquare, Users } from 'lucide-react';
import { useI18n } from '../../lib/i18n';
import { loadMissionControlAgentSessions, type MissionControlAgentSessionItem } from '../../lib/hermes-api';
import { SessionPicker } from './SessionPicker';

function TabLed({ state }: { state: 'none' | 'done' | 'help' }) {
  if (state === 'none') return null;
  return <span className={`tab-led ${state === 'done' ? 'is-done' : 'is-help'}`} aria-hidden />;
}

interface ChatModeTabsProps {
  active: 'chat' | 'rooms';
  onSelect: (mode: 'chat' | 'rooms') => void;
  chatLed?: 'none' | 'done' | 'help';
  roomsLed?: 'none' | 'done' | 'help';
  sessionsOpen: boolean;
  sessionsLed: 'none' | 'done' | 'help';
  sessionsTitle: string;
  sessionsTriggerRef: RefObject<HTMLButtonElement | null>;
  onToggleSessions?: () => void;
}

function ChatModeTabs({ active, onSelect, chatLed = 'none', roomsLed = 'none', sessionsOpen, sessionsLed, sessionsTitle, sessionsTriggerRef, onToggleSessions }: ChatModeTabsProps) {
  const { t } = useI18n();
  return (
    <div className="chat-mode-tabs" role="group" aria-label="Chat mode">
      <div className="chat-mode-choices" role="tablist" aria-label="Chat mode">
      <button type="button" className={`chat-mode-tab ${active === 'chat' ? 'is-active' : ''}`} role="tab" aria-selected={active === 'chat'} onClick={() => onSelect('chat')}>
        <MessageSquare size={14} />{t('chatDrawer.title')}<TabLed state={chatLed} />
      </button>
      <button type="button" className={`chat-mode-tab ${active === 'rooms' ? 'is-active' : ''}`} role="tab" aria-selected={active === 'rooms'} onClick={() => onSelect('rooms')}>
        <Users size={14} />{t('rooms.title')}<TabLed state={roomsLed} />
      </button>
      </div>
      {onToggleSessions ? <button ref={sessionsTriggerRef} type="button" data-session-picker-trigger className={`chat-mode-tab chat-sessions-trigger ${sessionsOpen ? 'is-active' : ''}`} aria-haspopup="dialog" aria-expanded={sessionsOpen} aria-controls={sessionsOpen ? 'chat-session-picker' : undefined} title={sessionsTitle} onClick={onToggleSessions}>
        <History size={14} />{t('sessionPicker.title')}<TabLed state={sessionsLed} /><ChevronDown size={12} />
      </button> : null}
    </div>
  );
}

/** Attention state for a mode tab. `done` = finished/new content below the
 *  fold (green), `help` = blocked/approval pending (amber). The dot clears
 *  when the user scrolls to the bottom, resolves the action, or switches
 *  into the mode. */
export type TabAttention = 'none' | 'done' | 'help';

export function useTabAttention({ needsAction, atBottom, contentCount }: {
  needsAction: boolean;
  atBottom: boolean;
  contentCount: number;
}): TabAttention {
  const [state, setState] = useState<TabAttention>('none');
  const stateRef = useRef<TabAttention>('none');
  const prevCountRef = useRef(contentCount);
  const grownRef = useRef(false);
  useEffect(() => {
    if (atBottom) {
      // Reading the content clears the attention dot immediately.
      grownRef.current = false;
      if (stateRef.current !== 'none') { stateRef.current = 'none'; setState('none'); }
      return;
    }
    if (contentCount !== prevCountRef.current) {
      grownRef.current = contentCount > prevCountRef.current || grownRef.current;
      if (contentCount < prevCountRef.current) grownRef.current = false;
      prevCountRef.current = contentCount;
    }
    const target: TabAttention = needsAction ? 'help' : grownRef.current ? 'done' : 'none';
    if (target !== stateRef.current) { stateRef.current = target; setState(target); }
  }, [atBottom, contentCount, needsAction]);
  return state;
}

/**
 * Auto-hides the mode tab bar while the drawer content scrolls FAST and
 * re-shows it when the user slows down, reaches the bottom (auto-follow
 * keeps it visible), or pauses for a while.
 *
 * Velocity-based with HYSTERESIS to avoid flicker: hide only above
 * HIDE_SPEED, re-show only below SHOW_SPEED (or at the bottom). Between the
 * two thresholds the rail keeps its current state, so the natural speed
 * decay of a flicked scroll (which oscillates around a single threshold)
 * can't flip the rail hide/show/hide. Speed is averaged over a small rolling
 * window of scroll samples to smooth trackpad bursts. Capture-phase listen
 * because scroll doesn't bubble; first scroll per element is baseline.
 */
const TAB_SCROLL_RESUME_MS = 700;
const SCROLL_SPEED_HIDE_PX_MS = 0.3; // ≥ 300px/s hides
const SCROLL_SPEED_SHOW_PX_MS = 0.1; // ≤ 100px/s shows (dead zone in between)
const SPEED_WINDOW_SAMPLES = 4;
const BOTTOM_EPSILON_PX = 24;
interface AutoHideModeTabsProps extends Pick<ChatModeTabsProps, 'active' | 'onSelect' | 'chatLed' | 'roomsLed'> {
  containerRef: RefObject<HTMLElement | null>;
  storedToken: string;
  enabled: boolean;
  blocked?: boolean;
  currentSessionId?: string | null;
  currentProfile?: string | null;
  onResumeSession?: (session: MissionControlAgentSessionItem) => void;
}

export function AutoHideModeTabs({ active, onSelect, containerRef, chatLed = 'none', roomsLed = 'none', storedToken, enabled, blocked = false, currentSessionId, currentProfile, onResumeSession }: AutoHideModeTabsProps) {
  const { t } = useI18n();
  const [sessionsOpen, setSessionsOpen] = useState(false);
  const [liveCount, setLiveCount] = useState<number | null>(null);
  const [activityError, setActivityError] = useState(false);
  const sessionsTriggerRef = useRef<HTMLButtonElement>(null);
  const closeSessions = useCallback(() => {
    setSessionsOpen(false);
    sessionsTriggerRef.current?.focus({ preventScroll: true });
  }, []);
  useEffect(() => { if (!enabled || blocked) setSessionsOpen(false); }, [enabled, blocked]);
  useEffect(() => {
    if (!enabled || !onResumeSession) return;
    const controller = new AbortController();
    let timer: number | undefined;
    let pending = false;
    const poll = async () => {
      if (pending || controller.signal.aborted) return;
      window.clearTimeout(timer);
      if (document.visibilityState === 'hidden') { timer = window.setTimeout(() => void poll(), 5000); return; }
      pending = true;
      try {
        const snapshot = await loadMissionControlAgentSessions(storedToken, 1, 0, undefined, undefined, { includeRecentMessages: false, signal: controller.signal });
        if (!controller.signal.aborted) { setLiveCount(snapshot.stats.liveSessions); setActivityError(false); }
      } catch {
        if (!controller.signal.aborted) setActivityError(true);
      } finally {
        pending = false;
        if (!controller.signal.aborted) timer = window.setTimeout(() => void poll(), 5000);
      }
    };
    const refresh = () => void poll();
    void poll();
    document.addEventListener('visibilitychange', refresh);
    return () => { controller.abort(); window.clearTimeout(timer); document.removeEventListener('visibilitychange', refresh); };
  }, [enabled, onResumeSession, storedToken]);
  const [hidden, setHidden] = useState(false);
  const hiddenRef = useRef(false);
  const shownAtRef = useRef(0);
  const seenRef = useRef(new WeakSet<HTMLElement>());
  const samplesRef = useRef(new Map<HTMLElement, number[]>());
  const prevTopRef = useRef(new Map<HTMLElement, { top: number; at: number }>());
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
      if (target?.closest('[data-session-picker]')) return;
      if (!target || typeof target.scrollTop !== 'number') return;
      const now = performance.now();
      const prev = prevTopRef.current.get(target);
      prevTopRef.current.set(target, { top: target.scrollTop, at: now });
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
      const windowed = samplesRef.current.get(target) ?? [];
      windowed.push(speed);
      if (windowed.length > SPEED_WINDOW_SAMPLES) windowed.shift();
      samplesRef.current.set(target, windowed);
      const avg = windowed.reduce((a, b) => a + b, 0) / windowed.length;

      if (hiddenRef.current) {
        if (avg <= SCROLL_SPEED_SHOW_PX_MS) setHiddenBoth(false);
      } else if (avg >= SCROLL_SPEED_HIDE_PX_MS) {
        shownAtRef.current = now;
        setHiddenBoth(true);
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
    <div className={`chat-mode-tabs-shell ${hidden && !sessionsOpen ? 'is-hidden' : ''}`} aria-hidden={blocked || (hidden && !sessionsOpen)} inert={blocked || (hidden && !sessionsOpen) ? true : undefined}>
      <ChatModeTabs active={active} onSelect={(mode) => { setSessionsOpen(false); onSelect(mode); }} chatLed={chatLed} roomsLed={roomsLed} sessionsOpen={sessionsOpen} sessionsLed={activityError ? 'help' : liveCount ? 'done' : 'none'} sessionsTitle={activityError ? t('sessions.unableToLoad') : t('sessionPicker.activeCount', { count: liveCount ?? 0 })} sessionsTriggerRef={sessionsTriggerRef} onToggleSessions={onResumeSession ? () => setSessionsOpen((current) => !current) : undefined} />
      {onResumeSession ? <SessionPicker key={storedToken} open={sessionsOpen && enabled} storedToken={storedToken} currentSessionId={currentSessionId} currentProfile={currentProfile} onClose={closeSessions} onSelect={(session) => { setSessionsOpen(false); onResumeSession(session); }} /> : null}
    </div>
  );
}
