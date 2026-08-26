import { FormEvent, useCallback, useEffect, useRef, useState } from 'react';
import { NavLink, Outlet, useLocation, useNavigate } from 'react-router-dom';
import {
  Bot,
  Brain,
  BookOpen,
  ClipboardCheck,
  DollarSign,
  LayoutDashboard,
  LockKeyhole,
  Kanban,
  Menu,
  MessageSquare,
  PanelLeftClose,
  PanelLeftOpen,
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
import { ChatDrawer } from './ChatDrawer';
import { useChatPresence } from '../lib/chat-presence';
import { useLastRoutePersistence } from '../lib/last-route';
import { recordReloadDiagnostic } from '../lib/reload-diagnostics';
import { Button } from './ui/Button';

export function MissionControlShell() {
  const location = useLocation();
  const navigate = useNavigate();
  useLastRoutePersistence();
  const presence = useChatPresence();
  const { t } = useI18n();

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
  } = useMissionControl();

  const navItems = [
    { to: '/', label: t('nav.overview'), icon: LayoutDashboard },
    { to: '/sessions', label: t('nav.sessions'), icon: MessageSquare },
    { to: '/kanban', label: t('nav.kanban'), icon: Kanban },
    { to: '/agents', label: t('nav.agents'), icon: Bot },
    { to: '/usage', label: t('nav.usage'), icon: DollarSign },
    { to: '/knowledge', label: t('nav.knowledge'), icon: BookOpen },
    { to: '/tools', label: t('nav.tools'), icon: Wrench },
    { to: '/cron', label: t('nav.cron'), icon: Timer },
    { to: '/skills', label: t('nav.skills'), icon: Brain },
    { to: '/config', label: t('nav.config'), icon: Settings },
    { to: '/logs', label: t('nav.logs'), icon: ScrollText },
    ...(snapshot.candidatesEnabled
      ? [{ to: '/curate', label: t('nav.curate'), icon: ClipboardCheck }]
      : []),
  ];
  const [sideOpen, setSideOpen] = useState(false);
  const [sideCollapsed, setSideCollapsed] = useState(false);
  // Safari suspends the tab in the background and cold-reloads it on return,
  // wiping React state. Persist the chat-open flag so the drawer comes back
  // open exactly as the user left it after the app reloads.
  const [chatOpen, setChatOpenState] = useState<boolean>(() => {
    try { return sessionStorage.getItem('mission-control-chat-open') === '1'; } catch { return false; }
  });
  const setChatOpen = useCallback((open: boolean) => {
    setChatOpenState(open);
    try { sessionStorage.setItem('mission-control-chat-open', open ? '1' : '0'); } catch { /* ignore */ }
  }, []);
  const chatRecoverySessionId = new URLSearchParams(location.search).get('chatSession');
  const tokenInputRef = useRef<HTMLInputElement | null>(null);
  const chatButtonRef = useRef<HTMLButtonElement | null>(null);

  const closeChat = useCallback(() => {
    setChatOpen(false);
    chatButtonRef.current?.focus();
    if (!chatRecoverySessionId) return;
    const params = new URLSearchParams(location.search);
    params.delete('chatSession');
    const search = params.toString();
    navigate(`${location.pathname}${search ? `?${search}` : ''}`, { replace: true });
  }, [chatRecoverySessionId, location.pathname, location.search, navigate]);

  const activeNav = navItems.find((item) => (item.to === '/' ? location.pathname === '/' : location.pathname.startsWith(item.to)));
  const isOverviewRoute = activeNav?.to === '/';

  useEffect(() => {
    setSideOpen(false);
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
    if (!sideOpen) return;
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setSideOpen(false);
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [sideOpen]);

  useEffect(() => {
    if (chatRecoverySessionId) setChatOpen(true);
  }, [chatRecoverySessionId]);

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
              <p className="eyebrow">{t('nav.missionControl')}</p>
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
            <strong>{t('nav.operatorPanel')}</strong>
            <span className="mini-note">{t('nav.miniNote')}</span>
          </div>

          <nav className="side-nav" aria-label={t('nav.routesAria')}>
            {navItems.map((item) => {
              const Icon = item.icon;
              return (
                <NavLink
                  key={item.to}
                  to={item.to}
                  end={item.to === '/'}
                  title={item.label}
                  className={({ isActive }) => `nav-link side-nav-link ${isActive ? 'nav-link-active is-active' : ''}`}
                >
                  <span className="side-nav-icon" aria-hidden>
                    <Icon size={16} strokeWidth={2} />
                  </span>
                  <span className="side-nav-label">{item.label}</span>
                </NavLink>
              );
            })}
          </nav>

          <div className="side-menu-actions">
            <PushToggle />
            <ThemeSelector />
            <LanguageSwitcher />
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
                <h1>{activeNav?.label ?? t('nav.overview')}</h1>
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

          <section className={`route-stage ${isOverviewRoute ? 'is-overview' : ''}`}>
            <Outlet />
          </section>

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
            onClose={closeChat}
          />
        ) : null}
      </div>
    </main>
  );
}
