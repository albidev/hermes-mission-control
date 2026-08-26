import { useI18n } from '../../lib/i18n';
import { Component, type ErrorInfo, type ReactNode } from 'react';
import { ArrowLeft, Loader2, XCircle } from 'lucide-react';

type ChatCanvasShellProps = {
  onClose: () => void;
  error?: boolean;
  onRetry?: () => void;
};

export function ChatCanvasLoadingShell({ onClose, error, onRetry }: ChatCanvasShellProps) {
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

export class ChatCanvasErrorBoundary extends Component<{
  children: ReactNode;
  onClose: () => void;
  onRetry: () => void;
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
    return <ChatCanvasLoadingShell onClose={this.props.onClose} error onRetry={this.props.onRetry} />;
  }
}
