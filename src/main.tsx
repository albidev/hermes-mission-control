import React, { Component, type ErrorInfo, type ReactNode } from 'react';
import ReactDOM from 'react-dom/client';
import App from './App';
import { I18nProvider, useI18n } from './lib/i18n';
import { ensureServiceWorker } from './lib/push-client';
import './styles.css';

class AppErrorBoundary extends Component<{ children: ReactNode; copy: { title: string; message: string; reload: string } }, { hasError: boolean }> {
  state = { hasError: false };

  static getDerivedStateFromError() {
    return { hasError: true };
  }

  componentDidCatch(error: Error, _info: ErrorInfo) {
    console.error('[MissionControl] application render failed:', error);
  }

  render() {
    if (!this.state.hasError) return this.props.children;
    return (
      <main className="app-error-screen" role="alert">
        <div className="app-error-card">
          <strong>{this.props.copy.title}</strong>
          <span>{this.props.copy.message}</span>
          <button type="button" onClick={() => window.location.reload()}>{this.props.copy.reload}</button>
        </div>
      </main>
    );
  }
}

function LocalizedErrorBoundary({ children }: { children: ReactNode }) {
  const { t } = useI18n();
  return <AppErrorBoundary copy={{ title: t('error.reloadTitle'), message: t('error.reloadMessage'), reload: t('error.reloadAction') }}>{children}</AppErrorBoundary>;
}

const root = document.getElementById('root') as HTMLElement;
ReactDOM.createRoot(root).render(
  <React.StrictMode>
    <I18nProvider>
      <LocalizedErrorBoundary>
        <App />
      </LocalizedErrorBoundary>
    </I18nProvider>
  </React.StrictMode>,
);

window.addEventListener('pageshow', () => {
  // The store already refreshes on visibilitychange; do not force a full reload
  // here because iOS paints a white navigation surface during notification resume.
});

navigator.serviceWorker?.addEventListener('message', (event: MessageEvent<{ type?: string; url?: string }>) => {
  if (event.data?.type !== 'mission-control:notification-click' || typeof event.data.url !== 'string') return;
  window.dispatchEvent(new CustomEvent('mission-control:notification-click', { detail: { url: event.data.url } }));
});

// Register the push service worker once, non-blocking. Guarded for HTTPS:
// service worker registration fails silently on insecure origins.
ensureServiceWorker();
