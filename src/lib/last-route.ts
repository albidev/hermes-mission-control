import { useEffect } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';

/**
 * PWA-style last-route persistence.
 *
 * Safari from the home screen always cold-launches the app at its start URL
 * (the root "/"), so the browser can't do the "back to where I was" job for
 * us. This hook saves the current route on every navigation and restores it
 * on a fresh launch — so reopening Mission Control lands you on the page you
 * left, like a real installed app.
 *
 * Explicit deep links (e.g. opening /sessions directly) are never overridden:
 * we only restore when the app booted on the bare root.
 */
const STORAGE_KEY = 'mission-control-last-route';

function isRestorableRoute(route: string | null): route is string {
  return typeof route === 'string' && route.length > 0 && route !== '/';
}

export function useLastRoutePersistence() {
  const location = useLocation();
  const navigate = useNavigate();

  // Restore on cold launch. Guarded so it only fires once per mount.
  useEffect(() => {
    if (location.pathname !== '/') return; // explicit deep link: never override
    let saved: string | null = null;
    try {
      saved = sessionStorage.getItem(STORAGE_KEY);
      if (!isRestorableRoute(saved)) saved = localStorage.getItem(STORAGE_KEY);
    } catch {
      /* storage unavailable — behave as a plain SPA */
    }
    if (!isRestorableRoute(saved)) return;
    navigate(saved, { replace: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Persist the current route whenever it changes.
  useEffect(() => {
    const route = `${location.pathname}${location.search}`;
    if (route === '/') return; // don't clobber history with the boot root
    try {
      sessionStorage.setItem(STORAGE_KEY, route);
      localStorage.setItem(STORAGE_KEY, route);
    } catch {
      /* storage unavailable — ignore */
    }
  }, [location.pathname, location.search]);
}
