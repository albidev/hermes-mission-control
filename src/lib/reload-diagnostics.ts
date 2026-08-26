const DIAGNOSTICS_ENDPOINT = '/api/local/client-diagnostics';
const STORAGE_KEY = 'mission-control-reload-diagnostics';
const MAX_BREADCRUMBS = 40;
const MAX_TEXT = 4000;

type DiagnosticPayload = Record<string, unknown>;

type NavigationEntry = PerformanceNavigationTiming & { type?: string };

function getToken(): string {
  try {
    return window.localStorage.getItem('mission-control-token')?.trim() || '';
  } catch {
    return '';
  }
}

function navigationType(): string | undefined {
  try {
    const entry = performance.getEntriesByType('navigation')[0] as NavigationEntry | undefined;
    return entry?.type;
  } catch {
    return undefined;
  }
}

function truncate(value: unknown): unknown {
  if (typeof value !== 'string') return value;
  return value.length > MAX_TEXT ? `${value.slice(0, MAX_TEXT)}…` : value;
}

function storage(): Storage | null {
  try {
    return window.sessionStorage;
  } catch {
    return null;
  }
}

function persistBreadcrumb(event: DiagnosticPayload): void {
  const target = storage();
  if (!target) return;
  try {
    const previous = JSON.parse(target.getItem(STORAGE_KEY) || '[]');
    const breadcrumbs = Array.isArray(previous) ? previous.slice(-(MAX_BREADCRUMBS - 1)) : [];
    target.setItem(STORAGE_KEY, JSON.stringify([...breadcrumbs, event]));
  } catch {
    // Diagnostics must never interfere with the application.
  }
}

function postDiagnostic(event: DiagnosticPayload, unload = false): void {
  const payload = JSON.stringify({ ...event, _accessToken: getToken() });
  if (unload && typeof navigator.sendBeacon === 'function') {
    try {
      if (navigator.sendBeacon(DIAGNOSTICS_ENDPOINT, new Blob([payload], { type: 'application/json' }))) return;
    } catch {
      // Fall through to keepalive fetch.
    }
  }
  try {
    void fetch(DIAGNOSTICS_ENDPOINT, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: payload,
      keepalive: true,
      credentials: 'include',
    }).catch(() => {});
  } catch {
    // Diagnostics must never interfere with the application.
  }
}

export function recordReloadDiagnostic(type: string, details: DiagnosticPayload = {}, unload = false): void {
  if (typeof window === 'undefined') return;
  const event: DiagnosticPayload = {
    timestamp: new Date().toISOString(),
    type,
    href: window.location.href,
    visibility: document.visibilityState,
    navigationType: navigationType(),
    ...Object.fromEntries(Object.entries(details).map(([key, value]) => [key, truncate(value)])),
  };
  persistBreadcrumb(event);
  try {
    console.warn('[MissionControl reload-diagnostic]', event);
  } catch {
    // Ignore console failures in restricted WebViews.
  }
  postDiagnostic(event, unload);
}

function previousBreadcrumbs(): unknown[] {
  const target = storage();
  if (!target) return [];
  try {
    const parsed = JSON.parse(target.getItem(STORAGE_KEY) || '[]');
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

export function installReloadDiagnostics(): () => void {
  if (typeof window === 'undefined') return () => {};

  const previous = previousBreadcrumbs();
  recordReloadDiagnostic('boot', {
    previousBreadcrumbs: previous,
    previousBreadcrumbCount: previous.length,
    wasDiscarded: Boolean((document as Document & { wasDiscarded?: boolean }).wasDiscarded),
  });

  const onError = (event: ErrorEvent) => {
    recordReloadDiagnostic('window.error', {
      message: event.message,
      source: event.filename,
      line: event.lineno,
      column: event.colno,
      stack: event.error?.stack,
    });
  };
  const onUnhandledRejection = (event: PromiseRejectionEvent) => {
    const reason = event.reason;
    recordReloadDiagnostic('unhandledrejection', {
      message: reason instanceof Error ? reason.message : String(reason),
      stack: reason instanceof Error ? reason.stack : undefined,
    });
  };
  const onBeforeUnload = () => {
    recordReloadDiagnostic('beforeunload', {
      stack: new Error('Mission Control beforeunload').stack,
      persisted: false,
    }, true);
  };
  const onPageHide = (event: PageTransitionEvent) => {
    recordReloadDiagnostic('pagehide', { persisted: event.persisted }, true);
  };
  const onPageShow = (event: PageTransitionEvent) => {
    recordReloadDiagnostic('pageshow', { persisted: event.persisted });
  };
  const onVisibility = () => recordReloadDiagnostic('visibilitychange');
  const onOnline = () => recordReloadDiagnostic('online');
  const onOffline = () => recordReloadDiagnostic('offline');
  const onFreeze = () => recordReloadDiagnostic('freeze');
  const onResume = () => recordReloadDiagnostic('resume');

  window.addEventListener('error', onError);
  window.addEventListener('unhandledrejection', onUnhandledRejection);
  window.addEventListener('beforeunload', onBeforeUnload);
  window.addEventListener('pagehide', onPageHide);
  window.addEventListener('pageshow', onPageShow);
  document.addEventListener('visibilitychange', onVisibility);
  window.addEventListener('online', onOnline);
  window.addEventListener('offline', onOffline);
  window.addEventListener('freeze', onFreeze);
  window.addEventListener('resume', onResume);

  if (import.meta.hot) {
    import.meta.hot.on('vite:beforeFullReload', (details) => recordReloadDiagnostic('vite:beforeFullReload', { details }));
    import.meta.hot.on('vite:beforeUpdate', (details) => recordReloadDiagnostic('vite:beforeUpdate', { details }));
    import.meta.hot.on('vite:afterUpdate', (details) => recordReloadDiagnostic('vite:afterUpdate', { details }));
    import.meta.hot.on('vite:error', (details) => recordReloadDiagnostic('vite:error', { details }));
  }

  return () => {
    window.removeEventListener('error', onError);
    window.removeEventListener('unhandledrejection', onUnhandledRejection);
    window.removeEventListener('beforeunload', onBeforeUnload);
    window.removeEventListener('pagehide', onPageHide);
    window.removeEventListener('pageshow', onPageShow);
    document.removeEventListener('visibilitychange', onVisibility);
    window.removeEventListener('online', onOnline);
    window.removeEventListener('offline', onOffline);
    window.removeEventListener('freeze', onFreeze);
    window.removeEventListener('resume', onResume);
  };
}
