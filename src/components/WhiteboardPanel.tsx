import { useCallback, useEffect, useMemo, useState } from 'react';
import { Maximize2, Minimize2, RotateCcw, X } from 'lucide-react';
import { Tldraw, getSnapshot, loadSnapshot, type Editor, type TLStoreSnapshot } from 'tldraw';
import 'tldraw/tldraw.css';

type WhiteboardPanelProps = {
  sessionId: string | null;
  sessionTitle: string;
  onClose: () => void;
  expanded: boolean;
};

function storageKey(sessionId: string | null) {
  return `mission-control:whiteboard:v2:${sessionId || 'new'}`;
}

export function WhiteboardPanel({ sessionId, sessionTitle, onClose, expanded }: WhiteboardPanelProps) {
  const [editor, setEditor] = useState<Editor | null>(null);
  const key = useMemo(() => storageKey(sessionId), [sessionId]);

  const handleMount = useCallback((nextEditor: Editor) => {
    setEditor(nextEditor);
    const raw = window.localStorage.getItem(key);
    if (!raw) return;
    try {
      loadSnapshot(nextEditor.store, JSON.parse(raw) as TLStoreSnapshot);
    } catch {
      window.localStorage.removeItem(key);
    }
  }, [key]);

  useEffect(() => {
    if (!editor) return;
    const dispose = editor.store.listen(() => {
      try {
        window.localStorage.setItem(key, JSON.stringify(getSnapshot(editor.store)));
      } catch {
        // Persistence is best-effort; the board remains usable if storage is full.
      }
    }, { scope: 'document', source: 'user' });
    return dispose;
  }, [editor, key]);

  const clearBoard = () => {
    if (!editor) return;
    editor.selectAll();
    editor.deleteShapes(editor.getSelectedShapeIds());
    window.localStorage.removeItem(key);
  };

  return (
    <section className={`whiteboard-panel ${expanded ? 'is-expanded' : ''}`} aria-label="Session whiteboard">
      <header className="whiteboard-head">
        <div>
          <span className="eyebrow">Session canvas</span>
          <h3>Whiteboard</h3>
          <span className="whiteboard-linked-session" title={sessionId || 'Session is still being created'}>
            Linked chat: {sessionTitle} · {sessionId ? sessionId.slice(0, 12) : 'pending'}
          </span>
        </div>
        <div className="whiteboard-actions">
          <button type="button" className="chat-icon-button" onClick={clearBoard} title="Clear whiteboard" aria-label="Clear whiteboard">
            <RotateCcw size={15} />
          </button>
          <button type="button" className="chat-icon-button" onClick={onClose} title="Close whiteboard" aria-label="Close whiteboard">
            {expanded ? <Minimize2 size={16} /> : <X size={17} />}
          </button>
        </div>
      </header>
      <div className="whiteboard-canvas">
        <Tldraw onMount={handleMount} />
      </div>
    </section>
  );
}
