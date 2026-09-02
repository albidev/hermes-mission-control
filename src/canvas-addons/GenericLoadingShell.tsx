import { Loader2 } from 'lucide-react';

interface Props {
  onClose: () => void;
  width?: number | null;
}

export function GenericLoadingShell({ onClose, width }: Props) {
  return (
    <div className="canvas-addon-loading" style={width ? { width: `${width}px` } : undefined}>
      <div className="canvas-addon-loading-content">
        <Loader2 size={24} className="chat-spin" />
        <span>Loading addon…</span>
      </div>
      <button type="button" className="chat-icon-button canvas-addon-loading-close" onClick={onClose} aria-label="Close addon">
        ✕
      </button>
    </div>
  );
}
