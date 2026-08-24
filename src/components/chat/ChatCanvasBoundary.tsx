import { Component, type ErrorInfo, type ReactNode } from 'react';
import { ArrowLeft, Loader2, XCircle } from 'lucide-react';

type ChatCanvasShellProps = {
  onClose: () => void;
  error?: boolean;
  onRetry?: () => void;
};

export function ChatCanvasLoadingShell({ onClose, error, onRetry }: ChatCanvasShellProps) {
  return (
    <section className="tldraw-canvas-panel is-expanded" aria-label={error ? 'TLDrawCanvas unavailable' : 'TLDrawCanvas loading'}>
      <header className="tldraw-canvas-head">
        <div className="tldraw-canvas-title">
          <button type="button" className="chat-icon-button tldraw-canvas-back" onClick={onClose} title="Back to chat" aria-label="Back to chat">
            <ArrowLeft size={18} />
          </button>
          <div>
            <span className="eyebrow">Session canvas</span>
            <h3>TLDrawCanvas</h3>
            <span className="tldraw-canvas-linked-session">{error ? 'Canvas unavailable' : 'Preparing workspace…'}</span>
          </div>
        </div>
      </header>
      <div className="tldraw-canvas-loading" role={error ? 'alert' : 'status'}>
        {error ? <XCircle size={24} aria-hidden /> : <Loader2 size={24} className="chat-spin" />}
        <strong>{error ? 'Unable to load TLDrawCanvas' : 'Opening TLDrawCanvas…'}</strong>
        <span>{error ? 'Check the connection and try again.' : 'Preparing the editable workspace.'}</span>
        {error && onRetry ? <button type="button" className="chat-control" onClick={onRetry}>Retry</button> : null}
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
