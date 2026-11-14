import { FormEvent, Suspense, useCallback, useEffect, useRef, useState } from 'react';
import { NavLink, Outlet, useLocation, useNavigate } from 'react-router-dom';
import {
  ArrowUp,
  DollarSign,
  LayoutDashboard,
  LockKeyhole,
  Kanban,
  Menu,
  MessageSquare,
  PanelLeftClose,
  PanelLeftOpen,
  Search,
  ScrollText,
  Settings,
  Timer,
  Wrench,
} from 'lucide-react';
import { ThemeSelector } from './ThemeSelector';
import { LanguageSwitcher } from './LanguageSwitcher';
import { PushToggle } from './PushToggle';
import { useMissionControl } from '../lib/mission-control-store';
import { useI18n } from '../lib/i18n';
import { useToast } from './ui/ToastProvider';
import { ChatDrawer } from './ChatDrawer';
import { useChatPresence } from '../lib/chat-presence';
import { useLastRoutePersistence } from '../lib/last-route';
import { readLocalLastRoom, writeLocalLastRoom, claimLastRoomPointer, fetchServerLastRoom } from '../lib/room-persistence';
import { clearNewChatParams, selectChatSessionParams } from '../lib/chat-session-params';
import type { MissionControlAgentSessionItem } from '../lib/hermes-api';
import { recordReloadDiagnostic } from '../lib/reload-diagnostics';
import { getRouteScroller, handleRouteScrollShortcut, scrollRouteToTop } from '../lib/route-scroll-shortcuts';
import { Button } from './ui/Button';
import packageJson from '../../package.json';
import { PluginRegistry } from '../core/plugins/registry';
import { NavStatusIndicator } from './NavStatusIndicator';
import { GatewayRestartConfirmationDialog, useGatewayRestartAction } from './overview/QuickActions';
import type { MCPluginNavItem } from '../core/plugins/types';
import { resolveIcon } from '../lib/icons';
import type { CurrentViewActionContext } from '../lib/current-view-actions';
import type { NavigationPaletteItem } from '../lib/navigation-palette';
import { filterNavigationPaletteItems, isNavigationPaletteTextTarget, mergeNavigationItems, NAVIGATION_PALETTE_MOBILE_QUERY, paletteSelectionIndexAfterChange, resolveNavigationPaletteIndex, stepNavigationPaletteIndex } from '../lib/navigation-palette';

const APP_VERSION = packageJson.version;

type ShellProps = { registry: PluginRegistry | null; navItems?: MCPluginNavItem[] };

export function MissionControlShell({ registry, navItems: runtimeNavItems = [] }: ShellProps) {
  const location = useLocation();
  const navigate = useNavigate();
  useLastRoutePersistence();
  const presence = useChatPresence();
  const { t } = useI18n();
  const { showToast } = useToast();

  useEffect(() => {
    recordReloadDiagnostic('mission-control-shell-mounted');
    return () => recordReloadDiagnostic('mission-control-shell-unmounted');
  }, []);

  const chatButtonLabel = presence.phase === 'running'
    ? t('chat.working')
    : presence.phase === 'completed'
      ? t('chat.completed')
      : presence.phase === 'waiting'
        ? t('chat.needsYou')
        : t('chat.button');

  const {
    authRequired,
    authError,
    loading,
    storedToken,
    snapshot,
    tokenDraft,
    setTokenDraft,
    unlock,
    logout,
    gatewayActions,
    runGatewayAction,
    actionLoading,
  } = useMissionControl();

  // Default nav items (hardcoded)
  const defaultNavItems: MCPluginNavItem[] = [
    { to: '/', label: t('nav.overview'), icon: 'LayoutDashboard', order: 10 },
    { to: '/sessions', label: t('nav.sessions'), icon: 'MessageSquare', order: 20 },
    { to: '/kanban', label: t('nav.kanban'), icon: 'Kanban', order: 15 },
    { to: '/agents', label: t('nav.agents'), icon: 'Workflow', order: 30 },
    { to: '/bots', label: t('nav.bots'), icon: 'Bot', order: 35 },
    { to: '/usage', label: t('nav.usage'), icon: 'DollarSign', order: 40 },
    { to: '/tools', label: t('nav.tools'), icon: 'Wrench', order: 50 },
    { to: '/cron', label: t('nav.cron'), icon: 'Timer', order: 60 },
    { to: '/skills', label: t('nav.skills'), icon: 'Brain', order: 70 },
    { to: '/config', label: t('nav.config'), icon: 'Settings', order: 80 },
    { to: '/logs', label: t('nav.logs'), icon: 'ScrollText', order: 90 },
  ];

  // Plugin nav items (from registry)
  const registryNavItems = registry?.getNavItems() ?? [];
  // Merge prop navItems (from the runtime plugin loader) with registry nav items.
  // The loader wires the same plugin list through both surfaces, so the shared
  // helper dedupes by destination (see mergeNavigationItems).
  const navItems: MCPluginNavItem[] = mergeNavigationItems(defaultNavItems, registryNavItems, runtimeNavItems);

  const [sideOpen, setSideOpen] = useState(false);
  const [sideCollapsed, setSideCollapsed] = useState(false);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [paletteQuery, setPaletteQuery] = useState('');
  const [paletteIndex, setPaletteIndex] = useState(-1);
  const [refreshPending, setRefreshPending] = useState(false);
  const handleRestartResult = useCallback((result: 'success' | 'error' | 'unknown') => {
    const message = result === 'success'
      ? t('palette.restartAccepted')
      : result === 'unknown'
        ? t('palette.restartUnknown')
        : t('palette.actionFailed');
    showToast(message, { tone: result === 'success' ? 'success' : result === 'unknown' ? 'warning' : 'error' });
  }, [showToast, t]);
  const restartFlow = useGatewayRestartAction(gatewayActions, runGatewayAction, actionLoading, handleRestartResult);
  const [currentViewRefreshAvailable, setCurrentViewRefreshAvailable] = useState(false);
  const currentViewRefreshHandlersRef = useRef(new Map<string, () => Promise<unknown>>());
  const refreshInFlightRef = useRef(false);
  const registerCurrentViewRefresh = useCallback<CurrentViewActionContext['registerCurrentViewRefresh']>((id, refresh) => {
    currentViewRefreshHandlersRef.current.set(id, refresh);
    setCurrentViewRefreshAvailable(currentViewRefreshHandlersRef.current.size === 1);
    return () => {
      if (currentViewRefreshHandlersRef.current.get(id) !== refresh) return;
      currentViewRefreshHandlersRef.current.delete(id);
      setCurrentViewRefreshAvailable(currentViewRefreshHandlersRef.current.size === 1);
    };
  }, []);
  const [paletteMobile, setPaletteMobile] = useState(() =>
    typeof window !== 'undefined' && window.matchMedia(NAVIGATION_PALETTE_MOBILE_QUERY).matches);
  const paletteRef = useRef<HTMLElement | null>(null);
  const paletteInputRef = useRef<HTMLInputElement | null>(null);
  const paletteListRef = useRef<HTMLDivElement | null>(null);
  const paletteReturnFocusRef = useRef<HTMLElement | null>(null);
  const [showMobileScrollTop, setShowMobileScrollTop] = useState(false);
  const [chatOpen, setChatOpenState] = useState<boolean>(() => {
    try { return sessionStorage.getItem('mission-control-chat-open') === '1'; } catch { return false; }
  });
  const setChatOpen = useCallback((open: boolean) => {
    setChatOpenState(open);
    try { sessionStorage.setItem('mission-control-chat-open', open ? '1' : '0'); } catch { /* ignore */ }
  }, []);
  const chatSearchParams = new URLSearchParams(location.search);
  const chatRecoverySessionId = chatSearchParams.get('chatSession');
  const chatMode = chatSearchParams.get('chatMode') === 'canonical' || chatSearchParams.get('chatMode') === 'task' || chatSearchParams.get('chatMode') === 'room'
    ? chatSearchParams.get('chatMode') as 'canonical' | 'task' | 'room'
    : 'general';
  const chatBotProfile = chatSearchParams.get('botProfile');
  const freshBotChatId = chatMode === 'task' && chatBotProfile && location.state && typeof location.state === 'object' && 'freshBotChatId' in location.state
    && location.state.freshBotChatId === chatRecoverySessionId ? chatRecoverySessionId : null;
  const chatRoomId = chatSearchParams.get('roomId');
  const inviteLobbyId = chatSearchParams.get('lobbyId');
  const resumeRequestKey = location.state && typeof location.state === 'object' && 'sessionResumeRequest' in location.state
    && typeof location.state.sessionResumeRequest === 'string' ? location.state.sessionResumeRequest : null;
  const serverLastRoomRef = useRef<{ roomId: string; revision: number } | null>(null);
  const tokenInputRef = useRef<HTMLInputElement | null>(null);
  const chatButtonRef = useRef<HTMLButtonElement | null>(null);

  const openPalette = useCallback(() => {
    paletteReturnFocusRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    setPaletteQuery('');
    setPaletteIndex(-1);
    setPaletteOpen(true);
  }, []);
  const closePalette = useCallback((options?: { restoreFocus?: boolean }) => {
    setPaletteOpen(false);
    setPaletteIndex(-1);
    if (options?.restoreFocus === false) return;
    requestAnimationFrame(() => paletteReturnFocusRef.current?.focus());
  }, []);

  // Mobile gating: the palette is a desktop affordance. Below the CSS
  // breakpoint the sidebar trigger is hidden and the shortcut is inert so the
  // palette cannot be reached at all (it is not rendered either).
  useEffect(() => {
    const query = window.matchMedia(NAVIGATION_PALETTE_MOBILE_QUERY);
    const sync = () => {
      setPaletteMobile(query.matches);
      if (query.matches) setPaletteOpen(false);
    };
    sync();
    query.addEventListener('change', sync);
    return () => query.removeEventListener('change', sync);
  }, []);

  const restartAction = restartFlow.action;
  const paletteCatalog: NavigationPaletteItem[] = [
    ...navItems.map((item) => ({ kind: 'route' as const, label: item.label.includes('.') ? t(item.label) : item.label, to: item.to, icon: item.icon })),
    { kind: 'chat', label: t('chat.button'), to: '', icon: 'MessageSquare' },
    ...(currentViewRefreshAvailable ? [{ kind: 'action' as const, action: 'refresh-current-view' as const, label: t('palette.refreshData'), to: '', icon: 'RefreshCw', searchTerms: [t('palette.refreshCurrentView'), 'refresh', 'update'], disabled: refreshPending || restartFlow.pending || Boolean(actionLoading) }] : []),
    ...(restartAction && storedToken.trim() && !authRequired ? [{ kind: 'action' as const, action: 'restart-gateway' as const, label: t('palette.restartGateway'), to: '', icon: 'Rocket', searchTerms: ['restart', 'gateway'], disabled: restartFlow.pending || refreshPending || Boolean(actionLoading) }] : []),
  ];
  const paletteItems = filterNavigationPaletteItems(paletteCatalog, paletteQuery);

  // Enter, click and arrow selection all funnel through here: one navigation
  // path for keyboard and pointer.
  const activatePaletteItem = useCallback((item: (typeof paletteItems)[number] | undefined) => {
    if (!item) return;
    if (item.kind === 'action') {
      if (item.disabled) return;
      if (item.action === 'refresh-current-view') {
        if (refreshInFlightRef.current || restartFlow.pending || actionLoading) return;
        if (currentViewRefreshHandlersRef.current.size !== 1) return;
        const refresh = currentViewRefreshHandlersRef.current.values().next().value;
        if (!refresh) return;
        refreshInFlightRef.current = true;
        setRefreshPending(true);
        closePalette();
        void Promise.resolve().then(refresh).then((result) => {
          if (result !== false) showToast(t('palette.refreshFinished'), { tone: 'info' });
        }).catch(() => {
          showToast(t('palette.actionFailed'), { tone: 'error' });
        }).finally(() => {
          refreshInFlightRef.current = false;
          setRefreshPending(false);
        });
      } else if (item.action === 'restart-gateway' && restartAction && !restartFlow.pending && !refreshInFlightRef.current && !refreshPending && !actionLoading) {
        restartFlow.request();
        closePalette({ restoreFocus: false });
      }
      return;
    }
    if (item.kind === 'chat') setChatOpen(true);
    else navigate(item.to);
    closePalette();
  }, [actionLoading, closePalette, navigate, paletteItems, refreshPending, restartAction, restartFlow, setChatOpen, showToast, t]);

  const paletteGroups = [
    { id: 'navigation', label: t('palette.navigationGroup'), items: paletteItems.filter((item) => item.kind !== 'action') },
    { id: 'actions', label: t('palette.actionsGroup'), items: paletteItems.filter((item) => item.kind === 'action') },
  ].filter((group) => group.items.length > 0);

  // Keep the highlighted option valid for the list it belongs to. The selection
  // is positional, so a query change must clear it: the same index can point at a
  // different destination after filtering, and Enter would then activate something
  // the user never selected. A result-set change under the same query keeps a
  // still-in-range selection and clears one that fell out of range.
  const paletteQueryRef = useRef(paletteQuery);
  useEffect(() => {
    const previousQuery = paletteQueryRef.current;
    paletteQueryRef.current = paletteQuery;
    setPaletteIndex((current) => paletteSelectionIndexAfterChange(previousQuery, paletteQuery, current, paletteItems.length));
  }, [paletteQuery, paletteItems.length]);

  // Keep the highlighted option visible while arrowing through a long list.
  useEffect(() => {
    if (!paletteOpen || paletteIndex < 0) return;
    paletteListRef.current?.querySelectorAll<HTMLElement>('.navigation-palette-item')[paletteIndex]
      ?.scrollIntoView({ block: 'nearest' });
  }, [paletteIndex, paletteOpen]);

  // The dialog key handler is a single capture-phase listener whose identity
  // must not change on every keystroke, so it reads the live result set, the
  // current selection and the activator through refs.
  const paletteItemsRef = useRef(paletteItems);
  paletteItemsRef.current = paletteItems;
  const paletteIndexRef = useRef(paletteIndex);
  paletteIndexRef.current = paletteIndex;
  const activatePaletteItemRef = useRef(activatePaletteItem);
  activatePaletteItemRef.current = activatePaletteItem;

  useEffect(() => {
    const onShortcut = (event: KeyboardEvent) => {
      // Mobile: the palette is not reachable, so the shortcut must not open it.
      if (paletteMobile) return;
      if (!(event.metaKey || event.ctrlKey) || event.altKey || event.key.toLowerCase() !== 'k') return;
      // The palette's own search field is a legitimate text target, but it is not
      // an independent editor: with the palette open the shortcut must still
      // toggle it closed, or Cmd/Ctrl+K is dead exactly while the user is in it.
      const insideOpenPalette = paletteOpen && event.target instanceof Node && paletteRef.current?.contains(event.target);
      if (!insideOpenPalette && isNavigationPaletteTextTarget(event.target)) return;
      const otherModalOpen = document.querySelector('[role="alertdialog"][aria-modal="true"]');
      if (otherModalOpen && !paletteRef.current?.contains(otherModalOpen)) return;
      if (authRequired || sideOpen || restartFlow.confirmationOpen) return;
      event.preventDefault();
      if (paletteOpen) closePalette();
      else openPalette();
    };
    window.addEventListener('keydown', onShortcut);
    return () => window.removeEventListener('keydown', onShortcut);
  }, [authRequired, chatOpen, closePalette, openPalette, paletteMobile, paletteOpen, restartFlow.confirmationOpen, sideOpen]);

  useEffect(() => {
    if (!paletteOpen) return;
    paletteInputRef.current?.focus();
    const onDialogKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        // Capture phase: the palette is the topmost layer, so Escape must be
        // consumed here before the chat drawer / sidebar / route handlers see it.
        // A bubble listener would run after React's root-delegated drawer
        // handler and closing the palette would also close the drawer beneath.
        event.preventDefault();
        event.stopImmediatePropagation();
        closePalette();
        return;
      }
      // Arrow keys move the selection cyclically over the filtered options and
      // stay in the search field, so typing and navigating share one focus point.
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        const delta = event.key === 'ArrowDown' ? 1 : -1;
        event.preventDefault();
        event.stopImmediatePropagation();
        setPaletteIndex((current) => stepNavigationPaletteIndex(current, paletteItemsRef.current.length, delta));
        return;
      }
      // Enter navigates to the highlighted option. With no explicit selection the
      // first (single) match is activated directly.
      if (event.key === 'Enter') {
        const index = resolveNavigationPaletteIndex(paletteItemsRef.current.length, paletteIndexRef.current);
        if (index < 0) return;
        event.preventDefault();
        event.stopImmediatePropagation();
        activatePaletteItemRef.current(paletteItemsRef.current[index]);
        return;
      }
      if (event.key !== 'Tab' || !paletteRef.current) return;
      const focusable = [...paletteRef.current.querySelectorAll<HTMLElement>('button, input:not([disabled])')];
      if (focusable.length === 0) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    };
    window.addEventListener('keydown', onDialogKeyDown, true);
    return () => window.removeEventListener('keydown', onDialogKeyDown, true);
  }, [closePalette, paletteOpen]);

  const closeChat = useCallback(() => {
    setChatOpen(false);
    chatButtonRef.current?.focus();
    const params = new URLSearchParams(location.search);
    const hasChatParams = params.has('chatSession') || params.has('chatMode') || params.has('botProfile') || params.has('roomId');
    if (!hasChatParams) return;
    params.delete('chatSession');
    params.delete('chatMode');
    params.delete('botProfile');
    params.delete('roomId');
    const search = params.toString();
    navigate(`${location.pathname}${search ? `?${search}` : ''}`, { replace: true });
  }, [location.pathname, location.search, navigate]);

  const startNewChat = useCallback(() => {
    const search = clearNewChatParams(location.search);
    navigate(`${location.pathname}${search ? `?${search}` : ''}`, { replace: true });
  }, [location.pathname, location.search, navigate]);

  const changeRoom = useCallback((roomId: string | null, roomName?: string | null) => {
    const params = new URLSearchParams(location.search);
    if (roomId) {
      params.set('chatMode', 'room');
      params.set('roomId', roomId);
      params.delete('chatSession');
      // Local mirror (first paint) + shared cross-device pointer on the
      // telemetry server, revisioned exactly like the last chat. Whichever
      // device selects a room last wins for every device.
      writeLocalLastRoom(roomId);
      const previous = serverLastRoomRef.current;
      void claimLastRoomPointer(roomId, roomName ?? null, storedToken || '', previous?.roomId === roomId ? previous.revision : null)
        .then((result) => {
          if (result.lastRoom) serverLastRoomRef.current = { roomId: result.lastRoom.roomId, revision: result.lastRoom.revision };
        })
        .catch(() => {/* best effort, local mirror stays */});
    } else {
      // Leaving the room / switching to Chat must NOT clear the persisted
      // last-room key: it means "last room the user had open", so reopening
      // Rooms lands back on it. Removing it here is why the app "sometimes
      // loses the last room and picks another one" (the first list entry was
      // selected as fallback and then rewritten as the new last room).
      params.delete('roomId');
      params.delete('chatMode');
    }
    const search = params.toString();
    navigate(`${location.pathname}${search ? `?${search}` : ''}`, { replace: true });
  }, [location.pathname, location.search, navigate, storedToken]);

  const openRoomsMode = useCallback(() => {
    const params = new URLSearchParams(location.search);
    params.set('chatMode', 'room');
    // Restore the last room the user had open, so reopening Rooms after a
    // reload / drawer close lands back on the same room instead of the
    // bare 'select a room' state. Prefer the shared cross-device pointer
    // (server canonical); localStorage is the fast first-paint fallback.
    params.delete('roomId');
    const local = readLocalLastRoom();
    if (local) params.set('roomId', local);
    params.delete('chatSession');
    const search = params.toString();
    setChatOpen(true);
    navigate(`${location.pathname}${search ? `?${search}` : ''}`, { replace: true });
    if (storedToken) {
      void fetchServerLastRoom(storedToken).then((server) => {
        if (!server || !server.roomId) return;
        serverLastRoomRef.current = { roomId: server.roomId, revision: server.revision };
        if (server.roomId === local) return;
        // An async response must not navigate back to Rooms after the user left.
        if (new URLSearchParams(window.location.search).get('chatMode') !== 'room') return;
        const params2 = new URLSearchParams(window.location.search);
        params2.set('roomId', server.roomId);
        writeLocalLastRoom(server.roomId);
        navigate(`${window.location.pathname}?${params2}`, { replace: true });
      }).catch(() => {/* offline: local mirror stays */});
    }
  }, [location.pathname, location.search, navigate, storedToken]);

  const startTaskChat = useCallback(() => {
    const params = new URLSearchParams(location.search);
    params.delete('chatSession');
    params.delete('botProfile');
    params.delete('roomId');
    params.set('chatMode', 'task');
    const search = params.toString();
    navigate(`${location.pathname}${search ? `?${search}` : ''}`, { replace: true });
  }, [location.pathname, location.search, navigate]);

  const resumeSelectionRef = useRef(0);
  const resumePickedSession = useCallback((session: MissionControlAgentSessionItem) => {
    if (!session.isResumable) return;
    const search = selectChatSessionParams(location.search, session);
    setChatOpen(true);
    navigate(`${location.pathname}?${search}`, { replace: true, state: { sessionResumeRequest: String(++resumeSelectionRef.current) } });
  }, [location.pathname, location.search, navigate]);

  const activeNav = navItems.find((item) => (item.to === '/' ? location.pathname === '/' : location.pathname.startsWith(item.to)));
  const isOverviewRoute = activeNav?.to === '/';

  useEffect(() => {
    setSideOpen(false);
  }, [location.pathname]);

  useEffect(() => {
    // Scroll doesn't bubble; capture it from the route's nested scroll owner.
    setShowMobileScrollTop(false);
    const sync = () => {
      const scroller = getRouteScroller(document.querySelector('.route-stage'));
      setShowMobileScrollTop(Boolean(scroller && scroller.scrollTop > 240));
    };
    const frame = requestAnimationFrame(sync);
    window.addEventListener('scroll', sync, true);
    return () => {
      cancelAnimationFrame(frame);
      window.removeEventListener('scroll', sync, true);
    };
  }, [location.pathname]);

  useEffect(() => {
    const handleNotificationClick = (event: Event) => {
      const url = (event as CustomEvent<{ url?: string }>).detail?.url;
      if (typeof url !== 'string') return;
      const target = new URL(url, window.location.origin);
      navigate(`${target.pathname}${target.search}${target.hash}`);
    };
    window.addEventListener('mission-control:notification-click', handleNotificationClick);
    return () => window.removeEventListener('mission-control:notification-click', handleNotificationClick);
  }, [navigate]);

  useEffect(() => {
    const onRouteScrollKey = (event: KeyboardEvent) => {
      // ChatDrawer stays mounted with aria-modal="true" even while visually hidden.
      const modalOpen = Array.from(document.querySelectorAll('[aria-modal="true"]'))
        .some((modal) => getComputedStyle(modal).visibility === 'visible');
      const blocked = chatOpen || sideOpen || authRequired || modalOpen;
      handleRouteScrollShortcut(event, document.querySelector('.route-stage'), blocked);
    };
    window.addEventListener('keydown', onRouteScrollKey);
    return () => window.removeEventListener('keydown', onRouteScrollKey);
  }, [chatOpen, sideOpen, authRequired]);

  useEffect(() => {
    if (!sideOpen) return;
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setSideOpen(false);
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [sideOpen]);

  useEffect(() => {
    if (chatRecoverySessionId || chatRoomId) setChatOpen(true);
  }, [chatRecoverySessionId, chatRoomId]);

  useEffect(() => {
    if (!chatOpen) return;
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        closeChat();
      }
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [chatOpen, closeChat]);

  useEffect(() => {
    if (!authRequired) {
      return;
    }
    setSideOpen(false);
    const raf = window.requestAnimationFrame(() => {
      tokenInputRef.current?.focus();
      tokenInputRef.current?.select();
    });
    return () => window.cancelAnimationFrame(raf);
  }, [authRequired]);

  const toggleSidebar = useCallback(() => {
    if (typeof window !== 'undefined' && window.matchMedia('(min-width: 981px)').matches) {
      setSideCollapsed((value) => !value);
      return;
    }
    setSideOpen((value) => !value);
  }, []);

  useEffect(() => {
    const handler = () => toggleSidebar();
    window.addEventListener('mission-control:toggle-sidebar', handler);
    return () => window.removeEventListener('mission-control:toggle-sidebar', handler);
  }, [toggleSidebar]);

  const handleUnlock = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    await unlock(tokenDraft);
  };

  return (
    <main className="shell app-shell">
      <div className="ambient ambient-a" />
      <div className="ambient ambient-b" />

      <div className={`layout-frame ${sideOpen ? 'is-open' : ''} ${sideCollapsed ? 'is-collapsed' : ''} ${authRequired ? 'is-locked' : ''}`}>
        <aside className="card side-menu" aria-label={t('nav.aria')}>
          <div className="side-menu-head">
            <div className="side-menu-head-top">
              <strong className="sidebar-brand">{t('nav.missionControl')}</strong>
              <Button
                variant="ghost"
                size="md"
                icon={sideCollapsed ? <PanelLeftOpen size={17} /> : <PanelLeftClose size={17} />}
                iconOnly
                className="desktop-sidebar-toggle"
                type="button"
                aria-label={sideCollapsed ? t('nav.expandSidebar') : t('nav.collapseSidebar')}
                aria-expanded={!sideCollapsed}
                title={sideCollapsed ? t('nav.expandSidebar') : t('nav.collapseSidebar')}
                onClick={toggleSidebar}
              />
            </div>
          </div>

          <nav className="side-nav" aria-label={t('nav.routesAria')}>
            {defaultNavItems.map((item) => {
              const Icon = resolveIcon(item.icon) ?? ((props: any) => <span {...props} />);
              const label = item.label.includes('.') ? t(item.label) : item.label;
              return (
                <NavLink
                  key={item.to}
                  to={item.to}
                  end={item.to === '/'}
                  title={label}
                  className={({ isActive }) => `nav-link side-nav-link ${isActive ? 'nav-link-active is-active' : ''}`}
                >
                  <span className="side-nav-icon" aria-hidden>
                    <Icon size={16} strokeWidth={2} />
                  </span>
                  <span className="side-nav-label">{label}</span>
                  {item.indicator ? <NavStatusIndicator indicator={item.indicator} /> : null}
                </NavLink>
              );
            })}

            {registryNavItems.length > 0 ? (
              <>
                <div className="side-nav-section-label">PLUGINS</div>
                {registryNavItems.map((item) => {
                  const Icon = resolveIcon(item.icon) ?? ((props: any) => <span {...props} />);
                  const label = item.label.includes('.') ? t(item.label) : item.label;
                  return (
                    <NavLink
                      key={item.to}
                      to={item.to}
                      end={item.to === '/'}
                      title={label}
                      className={({ isActive }) => `nav-link side-nav-link ${isActive ? 'nav-link-active is-active' : ''}`}
                    >
                      <span className="side-nav-icon" aria-hidden>
                        <Icon size={16} strokeWidth={2} />
                      </span>
                      <span className="side-nav-label">{label}</span>
                      {item.indicator ? <NavStatusIndicator indicator={item.indicator} /> : null}
                    </NavLink>
                  );
                })}
              </>
            ) : null}
          </nav>

          <div className="side-menu-actions">
            {paletteMobile ? null : (
              <Button
                variant="secondary"
                size="md"
                icon={<Search size={16} />}
                className="pill pill-subtle side-action-button palette-open-button"
                type="button"
                onClick={openPalette}
                aria-label={t('palette.open')}
                title={t('palette.open')}
              >
                <span className="palette-open-label">{t('palette.open')}</span><kbd>{typeof navigator !== 'undefined' && /Mac|iPhone|iPad/.test(navigator.platform) ? '⌘K' : 'Ctrl+K'}</kbd>
              </Button>
            )}
            <div className="side-menu-preferences">
              <PushToggle />
              <ThemeSelector />
              <LanguageSwitcher />
            </div>
            <div className="side-menu-action-group">
              <Button
                variant="secondary"
                size="md"
                icon={<LockKeyhole size={16} />}
                className="side-action-button lock-button"
                type="button"
                onClick={logout}
                aria-label={t('auth.lockDashboard')}
                title={t('auth.lock')}
              >
                <span className="lock-label">{t('auth.lock')}</span>
              </Button>
            </div>
          </div>
          <footer className="side-menu-footer">
            <span className="sidebar-version" aria-label={t('nav.version', { version: APP_VERSION })} title={t('nav.version', { version: APP_VERSION })}>
              v{APP_VERSION}
            </span>
          </footer>
        </aside>

        {sideOpen ? (
          <button
            className="mobile-nav-backdrop"
            type="button"
            aria-label={t('nav.closeMenu')}
            onClick={() => setSideOpen(false)}
          />
        ) : null}

        <section className="workspace-column">
          <header className={`card workspace-bar ${isOverviewRoute ? 'is-overview' : ''}`}>
            <div className="workspace-title-wrap">
              <Button
                variant="secondary"
                size="md"
                icon={<Menu size={17} />}
                iconOnly
                className="sidebar-toggle mobile-sidebar-toggle"
                type="button"
                aria-label={t('nav.openMenu')}
                aria-expanded={sideOpen}
                onClick={toggleSidebar}
              />
              <div>
                <p className="eyebrow">{t('app.workspace')}</p>
                <h1>{activeNav ? (activeNav.label.includes('.') ? t(activeNav.label) : activeNav.label) : t('nav.overview')}</h1>
              </div>
            </div>
            <Button
              ref={chatButtonRef}
              variant="secondary"
              size="md"
              icon={<span className={`chat-presence-dot is-${presence.phase}`} aria-hidden><MessageSquare size={16} /></span>}
              className={`chat-open-button chat-presence-button is-${presence.phase}`}
              type="button"
              onClick={() => { setChatOpen(true); }}
              aria-label={presence.preview ? `${chatButtonLabel}: ${presence.preview}` : chatButtonLabel}
              aria-expanded={chatOpen}
              title={presence.preview || chatButtonLabel}
            >
              <span className="chat-presence-label-full">{chatButtonLabel}</span>
              <span className="chat-presence-label-compact" aria-hidden>
                {presence.phase === 'running' ? t('chat.workingCompact') : presence.phase === 'completed' ? t('chat.doneCompact') : presence.phase === 'waiting' ? t('chat.needsYouCompact') : t('chat.button')}
              </span>
              {presence.unreadCount > 0 ? <span className="chat-unread-badge">{presence.unreadCount > 9 ? '9+' : presence.unreadCount}</span> : null}
            </Button>
          </header>

          {paletteOpen && !paletteMobile ? (
            <div className="navigation-palette-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) closePalette(); }}>
              <section ref={paletteRef} className="navigation-palette" role="dialog" aria-modal="true" aria-label={t('palette.title')}>
                <div className="navigation-palette-search">
                  <Search size={18} aria-hidden="true" />
                  <input
                    ref={paletteInputRef}
                    type="search"
                    value={paletteQuery}
                    onChange={(event) => setPaletteQuery(event.target.value)}
                    placeholder={t('palette.search')}
                    aria-label={t('palette.search')}
                    role="combobox"
                    aria-expanded={paletteItems.length > 0}
                    aria-controls="navigation-palette-results"
                    aria-activedescendant={paletteIndex >= 0 ? `navigation-palette-option-${paletteIndex}` : undefined}
                  />
                  <button type="button" onClick={() => closePalette()} aria-label={t('palette.close')}>Esc</button>
                </div>
                <div className="navigation-palette-results" id="navigation-palette-results" role="listbox" ref={paletteListRef}>
                  {paletteItems.length ? paletteGroups.map((group) => (
                    <div key={group.id} role="group" aria-label={group.label} data-palette-group={group.id}>
                      <p aria-hidden="true" className="navigation-palette-group-label">{group.label}</p>
                      {group.items.map((item) => {
                        const index = paletteItems.indexOf(item);
                        const Icon = resolveIcon(item.icon) ?? ((props: any) => <span {...props} />);
                        return (
                          <div
                            id={`navigation-palette-option-${index}`}
                            className={`navigation-palette-item${index === paletteIndex ? ' is-selected' : ''}`}
                            key={`${item.kind}:${item.to || item.action || item.label}`}
                            role="option"
                            aria-selected={index === paletteIndex}
                            aria-disabled={item.disabled || undefined}
                            tabIndex={-1}
                            data-palette-action={item.action}
                            onClick={() => activatePaletteItem(item)}
                          >
                            <Icon size={17} aria-hidden="true" />
                            <span>{item.label}</span>
                            {item.kind === 'route' ? <kbd>{item.to}</kbd> : item.disabled ? <small>{t('palette.actionBusy')}</small> : null}
                          </div>
                        );
                      })}
                    </div>
                  )) : <p className="navigation-palette-empty">{t('palette.noResults')}</p>}
                </div>
              </section>
            </div>
          ) : null}

          <GatewayRestartConfirmationDialog
            open={restartFlow.confirmationOpen}
            onCancel={restartFlow.cancel}
            onConfirm={() => void restartFlow.confirm()}
            returnFocusTarget={paletteReturnFocusRef.current}
          />

          <section className={`route-stage ${isOverviewRoute ? 'is-overview' : ''}`}>
            <Suspense fallback={<div className="route-loading" role="status">Loading page…</div>}>
              <Outlet context={{ registerCurrentViewRefresh } satisfies CurrentViewActionContext} />
            </Suspense>
          </section>

          {showMobileScrollTop && !chatOpen && !sideOpen && !authRequired ? (
            <Button
              variant="primary"
              size="md"
              icon={<ArrowUp size={18} />}
              iconOnly
              className={`mobile-route-scroll-top ${location.pathname === '/config' ? 'is-config' : ''}`}
              type="button"
              aria-label={t('logs.scrollToTop')}
              title={t('logs.scrollToTop')}
              onClick={() => scrollRouteToTop(document.querySelector('.route-stage'))}
            />
          ) : null}

          {authRequired ? (
            <div className="auth-overlay" role="presentation">
              <section className="card auth-card auth-modal page-card" role="dialog" aria-modal="true" aria-labelledby="mission-control-auth-title">
                <div className="auth-modal-topbar">
                  <div className="auth-lock-mark" aria-hidden>
                    <svg viewBox="0 0 24 24" className="lock-icon auth-lock-icon">
                      <path d="M7.5 10V8.2A4.5 4.5 0 0 1 12 3.7a4.5 4.5 0 0 1 4.5 4.5V10" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
                      <rect x="5.5" y="10" width="13" height="10" rx="2.5" fill="none" stroke="currentColor" strokeWidth="1.8" />
                    </svg>
                  </div>
                  <ThemeSelector showLabel={false} className="auth-theme-toggle" />
                </div>

                <p className="eyebrow">{t('auth.accessRequired')}</p>
                <h2 id="mission-control-auth-title">{t('auth.lockedTitle')}</h2>
                <p className="lede">
                  {t('auth.lede')}
                </p>

                <form className="auth-form" onSubmit={handleUnlock}>
                  <label className="auth-label" htmlFor="mission-control-token">
                    {t('auth.tokenLabel')}
                  </label>
                  <input
                    ref={tokenInputRef}
                    id="mission-control-token"
                    className="auth-input"
                    type="password"
                    autoComplete="current-password"
                    inputMode="text"
                    value={tokenDraft}
                    onChange={(event) => setTokenDraft(event.target.value)}
                    placeholder={t('auth.tokenPlaceholder')}
                  />

                  {authError ? <p className="auth-error">{authError}</p> : null}

                  <div className="auth-actions">
                    <button className="auth-primary" type="submit" disabled={loading}>
                      {loading ? t('auth.unlocking') : t('auth.unlock')}
                    </button>
                  </div>

                  {storedToken ? (
                    <button className="auth-reset" type="button" onClick={logout}>
                      {t('auth.useDifferentToken')}
                    </button>
                  ) : null}
                </form>
              </section>
            </div>
          ) : null}
        </section>

        {!authRequired ? (
          <ChatDrawer
            open={chatOpen}
            storedToken={storedToken}
            initialSessionId={chatRecoverySessionId}
            freshSessionId={freshBotChatId}
            resumeRequestKey={resumeRequestKey}
            chatMode={chatMode}
            roomId={chatRoomId}
            inviteLobbyId={inviteLobbyId}
            botProfile={chatBotProfile}
            onClose={closeChat}
            onStartTaskChat={startTaskChat}
            onNewChat={startNewChat}
            onOpenRooms={openRoomsMode}
            onResumeSession={resumePickedSession}
            onRoomChange={changeRoom}
          />
        ) : null}
      </div>
    </main>
  );
}
