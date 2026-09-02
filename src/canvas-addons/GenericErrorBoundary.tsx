import { Component, type ReactNode } from 'react';

interface Props {
  onClose: () => void;
  onRetry: () => void;
  width?: number | null;
  children: ReactNode;
}

interface State {
  hasError: boolean;
}

export class GenericErrorBoundary extends Component<Props, State> {
  state: State = { hasError: false };

  static getDerivedStateFromError(): State {
    return { hasError: true };
  }

  componentDidCatch(error: Error, info: React.ErrorInfo) {
    console.error('[CanvasAddon] failed to load:', error, info);
  }

  render() {
    if (this.state.hasError) {
      return (
        <div className="canvas-addon-error" style={this.props.width ? { width: `${this.props.width}px` } : undefined}>
          <div className="canvas-addon-error-content">
            <p className="canvas-addon-error-title">Canvas addon crashed</p>
            <p className="canvas-addon-error-hint">The addon failed to render. Try reloading.</p>
            <div className="canvas-addon-error-actions">
              <button type="button" className="chat-icon-button" onClick={this.props.onClose} aria-label="Close addon">
                Close
              </button>
              <button type="button" className="chat-icon-button" onClick={this.props.onRetry} aria-label="Retry addon">
                Retry
              </button>
            </div>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}
