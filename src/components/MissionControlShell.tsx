import { FormEvent, useCallback, useEffect, useState } from 'react';
import { NavLink, Outlet, useLocation } from 'react-router-dom';
import {
  Bot,
  Brain,
  BookOpen,
  LayoutDashboard,
  MessageSquare,
  ScrollText,
  Settings,
  Wrench,
  type LucideIcon,
} from 'lucide-react';
import { ThemeSelector } from './ThemeSelector';
import { useMissionControl } from '../lib/mission-control-store';

const navItems: Array<{ to: string; label: string; icon: LucideIcon }> = [
  { to: '/', label: 'Overview', icon: LayoutDashboard },
  { to: '/sessions', label: 'Sessions', icon: MessageSquare },
  { to: '/agents', label: 'Agents', icon: Bot },
  { to: '/knowledge', label: 'Knowledge', icon: BookOpen },
  { to: '/tools', label: 'Tools', icon: Wrench },
  { to: '/skills', label: 'Skills', icon: Brain },
  { to: '/config', label: 'Config', icon: Settings },
  { to: '/logs', label: 'Logs', icon: ScrollText },
];

export function MissionControlShell() {
  const location = useLocation();
  const {
    authRequired,
    authError,
    loading,
    tokenDraft,
    setTokenDraft,
    unlock,
    logout,
    lastUpdatedAt,
    resolvedTheme,
  } = useMissionControl();

  const [sideOpen, setSideOpen] = useState(false);
  const [sideCollapsed, setSideCollapsed] = useState(false);

  const activeNav = navItems.find((item) => (item.to === '/' ? location.pathname === '/' : location.pathname.startsWith(item.to)));
  const isOverviewRoute = activeNav?.to === '/';

  useEffect(() => {
    setSideOpen(false);
  }, [location.pathname]);

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

      <div className={`layout-frame ${sideOpen ? 'is-open' : ''} ${sideCollapsed ? 'is-collapsed' : ''}`}>
        <aside className="card side-menu" aria-label="Mission Control navigation">
          <div className="side-menu-head">
            <div className="side-menu-head-top">
              <p className="eyebrow">Hermes Mission Control</p>
              <button className="pill pill-subtle pill-button side-close" type="button" onClick={toggleSidebar} aria-label="Close navigation">
                ✕
              </button>
            </div>
            <strong>Operator panel</strong>
            <span className="mini-note">Gateway, sessions, agents, tools, skills, config, logs</span>
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
            </div>
          </header>

          {authRequired ? (
            <section className="card auth-card page-card">
              <p className="eyebrow">Access required</p>
              <h2>Locked cockpit.</h2>
              <p className="lede">
                This dashboard is gated behind a bearer token. Enter it once and the route shell will stay unlocked until you log out.
              </p>

              <div className="auth-status">
                <span className="pill status offline">locked</span>
                <span className="mini-note">{loading ? 'checking access' : 'awaiting token'}</span>
                <span className="mini-note">theme: {resolvedTheme}</span>
              </div>

              <form className="auth-form" onSubmit={handleUnlock}>
                <label className="auth-label" htmlFor="mission-control-token">
                  Access token
                </label>
                <input
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
                  <button className="auth-secondary" type="button" onClick={logout}>
                    Clear stored token
                  </button>
                </div>
              </form>
            </section>
          ) : (
            <section className={`route-stage ${isOverviewRoute ? 'is-overview' : ''}`}>
              <Outlet />
            </section>
          )}
        </section>
      </div>
    </main>
  );
}
