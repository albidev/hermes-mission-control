import { FormEvent, useCallback, useEffect, useRef, useState } from 'react';
import { NavLink, Outlet, useLocation, useNavigate } from 'react-router-dom';
import {
  Bot,
  Brain,
  BookOpen,
  ClipboardCheck,
  DollarSign,
  LayoutDashboard,
  MessageSquare,
  ScrollText,
  Settings,
  Wrench,
} from 'lucide-react';
import { ThemeSelector } from './ThemeSelector';
import { PushToggle } from './PushToggle';
import { useMissionControl } from '../lib/mission-control-store';
import { ChatDrawer } from './ChatDrawer';
import { useLastRoutePersistence } from '../lib/last-route';

export function MissionControlShell() {
  const location = useLocation();
  const navigate = useNavigate();
  useLastRoutePersistence();
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
    lastUpdatedAt,
    resolvedTheme,
  } = useMissionControl();

  const navItems = [
    { to: '/', label: 'Overview', icon: LayoutDashboard },
    { to: '/sessions', label: 'Sessions', icon: MessageSquare },
    { to: '/agents', label: 'Agents', icon: Bot },
    { to: '/usage', label: 'Usage', icon: DollarSign },
    { to: '/knowledge', label: 'Knowledge', icon: BookOpen },
    { to: '/tools', label: 'Tools', icon: Wrench },
    { to: '/skills', label: 'Skills', icon: Brain },
    { to: '/config', label: 'Config', icon: Settings },
    { to: '/logs', label: 'Logs', icon: ScrollText },
    ...(snapshot.candidatesEnabled
      ? [{ to: '/curate', label: 'Curate', icon: ClipboardCheck }]
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
    if (typeof window !== 'undefined' && window.matchMedia('(min-width: 980px)').matches) {
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
        <aside className="card side-menu" aria-label="Mission Control navigation">
          <div className="side-menu-head">
            <div className="side-menu-head-top">
              <p className="eyebrow">Hermes Mission Control</p>
              <button className="pill pill-subtle pill-button side-close" type="button" onClick={toggleSidebar} aria-label="Close navigation">
                ✕
              </button>
            </div>
            <strong>Operator panel</strong>
            <span className="mini-note">Gateway, sessions, agents, usage, tools, skills, config, logs, curate</span>
          </div>

          <nav className="side-nav" aria-label="Mission Control routes">
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
            <button
              className="pill pill-subtle pill-button side-action-button lock-button"
              type="button"
              onClick={logout}
              aria-label="Lock dashboard"
              title="Lock"
            >
              <svg viewBox="0 0 24 24" className="lock-icon" aria-hidden>
                <path d="M7.5 10V8.2A4.5 4.5 0 0 1 12 3.7a4.5 4.5 0 0 1 4.5 4.5V10" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
                <rect x="5.5" y="10" width="13" height="10" rx="2.5" fill="none" stroke="currentColor" strokeWidth="1.8" />
              </svg>
              <span className="lock-label">Lock</span>
            </button>
          </div>
        </aside>

        {sideOpen ? (
          <button
            className="mobile-nav-backdrop"
            type="button"
            aria-label="Close navigation"
            onClick={() => setSideOpen(false)}
          />
        ) : null}

        <section className="workspace-column">
          <header className={`card workspace-bar ${isOverviewRoute ? 'is-overview' : ''}`}>
            <div className="workspace-title-wrap">
              <button
                className="pill pill-subtle pill-button sidebar-toggle"
                type="button"
                aria-label="Toggle navigation menu"
                aria-expanded={sideOpen}
                onClick={toggleSidebar}
              >
                ☰
              </button>
              <div>
                <p className="eyebrow">Workspace</p>
                <h1>{activeNav?.label ?? 'Overview'}</h1>
              </div>
            </div>
            <div className="workspace-meta">
              <span className="mini-note">theme: {resolvedTheme}</span>
              {lastUpdatedAt ? <span className="mini-note">Last synced {lastUpdatedAt}</span> : null}
              <button
                ref={chatButtonRef}
                className="pill pill-subtle pill-button chat-open-button"
                type="button"
                onClick={() => setChatOpen(true)}
                aria-label="Open Hermes chat"
                aria-expanded={chatOpen}
              >
                <MessageSquare size={16} aria-hidden />
                <span>Chat</span>
              </button>
            </div>
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

                <p className="eyebrow">Access required</p>
                <h2 id="mission-control-auth-title">Locked cockpit.</h2>
                <p className="lede">
                  Mission Control is visible but frozen until you enter the bearer token. Nice dashboard, shame if someone actually got in.
                </p>

                <form className="auth-form" onSubmit={handleUnlock}>
                  <label className="auth-label" htmlFor="mission-control-token">
                    Access token
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
                    placeholder="Paste bearer token"
                  />

                  {authError ? <p className="auth-error">{authError}</p> : null}

                  <div className="auth-actions">
                    <button className="auth-primary" type="submit" disabled={loading}>
                      {loading ? 'Unlocking…' : 'Unlock cockpit'}
                    </button>
                  </div>

                  {storedToken ? (
                    <button className="auth-reset" type="button" onClick={logout}>
                      Use a different token
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
