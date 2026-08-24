import { useCallback, useEffect, useMemo, useRef, useState, memo } from 'react';
import { ArrowLeft, Camera, Loader2, RotateCcw, Send } from 'lucide-react';
import { Tldraw, createBindingId, createShapeId, getSnapshot, loadSnapshot, toRichText, type Editor, type TLStoreSnapshot } from 'tldraw';
import { collectBoardContext } from '../lib/tldraw-visual-context';
import { AGENT_MODES, modePromptFragment, type AgentMode } from '../lib/tldraw-agent-modes';
import { formatLints, lintBoard, type BoardLint } from '../lib/tldraw-lints';
import 'tldraw/tldraw.css';

export type CanvasScreenshotAttachment = {
  kind: 'image';
  name: string;
  mimeType: string;
  dataUrl: string;
};

type BridgeCommand = {
  id: string;
  type: string;
  text?: string;
  x?: number;
  y?: number;
  w?: number;
  h?: number;
  color?: string;
  shapeType?: string;
  props?: Record<string, unknown>;
  shapeId?: string;
  shapeIds?: string[];
  fromId?: string;
  toId?: string;
  bindingProps?: Record<string, unknown>;
  bindingIds?: string[];
  pageId?: string;
  direction?: string;
  padding?: number;
  angle?: number;
  style?: unknown;
  value?: unknown;
  format?: 'json' | 'svg' | 'png';
};

/** Commands this client build knows how to apply. Must stay in sync with applyBridgeCommand. */
const PNG_EXPORT_PADDING = 32;

const SUPPORTED_COMMANDS = new Set([
  'clear', 'create_text', 'create_line', 'create_box', 'create_frame', 'create_arrow', 'create_shape',
  'move_shape', 'update_shape', 'delete_shapes', 'duplicate', 'group', 'ungroup', 'bring_to_front',
  'send_to_back', 'zoom_to_fit', 'create_binding', 'delete_bindings', 'create_page', 'set_current_page',
  'rename_page', 'delete_page', 'move_shapes_to_page', 'align_shapes', 'distribute_shapes', 'pack_shapes',
  'flip_shapes', 'rotate_shapes', 'resize_shape', 'toggle_lock', 'set_style', 'set_opacity',
  'export_json', 'export_svg', 'export_png', 'import_mermaid',
]);

function isSupportedCommand(command: BridgeCommand): boolean {
  return SUPPORTED_COMMANDS.has(command.type);
}

type TLDrawCanvasProps = {
  sessionId: string | null;
  sessionKey: string | null;
  sessionTitle: string;
  storedToken: string;
  onSendSelection: (text: string, attachments?: CanvasScreenshotAttachment[]) => Promise<boolean>;
  onActionApplied?: (summary: string) => void;
  onReady: () => void;
  loading: boolean;
  onClose: () => void;
  expanded: boolean;
};

function storageKey(sessionId: string | null, sessionKey: string | null) {
  return `mission-control:whiteboard:v5:${sessionId || sessionKey || 'new'}`;
}

function sanitizeSnapshot(snapshot: TLStoreSnapshot): TLStoreSnapshot {
  const cleaned = JSON.parse(JSON.stringify(snapshot)) as TLStoreSnapshot;
  const store = ((cleaned as unknown as { document: { store: Record<string, { type?: string; x?: number; props?: Record<string, unknown> }> } }).document.store);
  for (const shape of Object.values(store)) {
    if (shape?.type !== 'geo' || !shape.props) continue;
    const width = typeof shape.props.w === 'number' ? shape.props.w : 1;
    const height = typeof shape.props.h === 'number' ? shape.props.h : 1;
    if (width <= 0) {
      shape.x = (shape.x ?? 0) + width;
      shape.props.w = Math.max(1, Math.abs(width));
    }
    if (height <= 0) shape.props.h = Math.max(1, Math.abs(height));
  }
  return cleaned;
}

function triggerDownload(href: string, filename: string) {
  const link = document.createElement('a');
  link.href = href;
  link.download = filename;
  link.click();
  URL.revokeObjectURL(href);
}

export function TldrawMark({ size = 16 }: { size?: number }) {
  return <span aria-hidden="true" style={{ fontSize: size * 1.25, fontWeight: 800, lineHeight: 1 }}>;</span>;
}

export const TLDrawCanvas = memo(function TLDrawCanvas({ sessionId, sessionKey, sessionTitle, storedToken, onSendSelection, onActionApplied, onReady, loading, onClose, expanded }: TLDrawCanvasProps) {
  const [editor, setEditor] = useState<Editor | null>(null);
  const [agentMode, setAgentMode] = useState<AgentMode>('');
  const [lints, setLints] = useState<BoardLint[]>([]);
  const [toastMessage, setToastMessage] = useState<string | null>(null);
  const canvasContainerRef = useRef<HTMLDivElement | null>(null);
  const key = useMemo(() => storageKey(sessionId, sessionKey), [sessionId, sessionKey]);
  const boardIdentity = useMemo(() => sessionId || sessionKey || '', [sessionId, sessionKey]);
  const remoteHydratedRef = useRef(false);
  const publishTimerRef = useRef<number | null>(null);

  useEffect(() => {
    if (!toastMessage) return;
    const timer = window.setTimeout(() => setToastMessage(null), 2800);
    return () => window.clearTimeout(timer);
  }, [toastMessage]);

  useEffect(() => {
    remoteHydratedRef.current = false;
    if (publishTimerRef.current !== null) {
      window.clearTimeout(publishTimerRef.current);
      publishTimerRef.current = null;
    }
  }, [boardIdentity]);
  const changeAgentMode = useCallback(async (mode: AgentMode) => {
    setAgentMode(mode);
    try {
      await fetch('/api/local/chat/whiteboard', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...(storedToken ? { Authorization: `Bearer ${storedToken}` } : {}) },
        body: JSON.stringify({ sessionId, sessionKey, action: 'mode', mode }),
      });
    } catch {
      // Mode persistence is best-effort; local UI state is already updated.
    }
  }, [sessionId, sessionKey, storedToken]);

  const handleMount = useCallback((nextEditor: Editor) => {
    setEditor(nextEditor);
    onReady();
    // For an identified session, the remote snapshot is authoritative. Do not
    // hydrate stale localStorage before the remote state arrives.
    if (sessionId) return;
    const raw = window.localStorage.getItem(key);
    if (!raw) {
      if (!sessionId) {
        nextEditor.createShape({
          id: createShapeId('session-note'),
          type: 'text',
          x: 160,
          y: 140,
          props: {
            richText: toRichText('Questa whiteboard è associata alla Chat corrente.'),
            color: 'black',
            size: 'l',
            font: 'sans',
            textAlign: 'start',
            autoSize: true,
          },
        });
      }
      return;
    }
    try {
      // loadSnapshot restores BOTH document and session sections of a full
      // editor snapshot (camera per page, current page, selection) on its own.
      loadSnapshot(nextEditor.store, sanitizeSnapshot(JSON.parse(raw) as TLStoreSnapshot));
    } catch {
      window.localStorage.removeItem(key);
    }
  }, [key, onReady, sessionId]);

  // Migrate a board drawn before the chat had an identity (storage key 'new')
  // to the stable sessionKey as soon as one becomes available.
  useEffect(() => {
    if (!sessionKey) return;
    const newKey = `mission-control:whiteboard:v5:new`;
    const target = storageKey(null, sessionKey);
    const orphan = window.localStorage.getItem(newKey);
    if (orphan && !window.localStorage.getItem(target)) {
      try {
        window.localStorage.setItem(target, orphan);
        window.localStorage.removeItem(newKey);
      } catch {
        // Best-effort migration; server snapshot remains authoritative anyway.
      }
      // The hydrated remote snapshot remains authoritative. If the server has
      // no snapshot, the normal hydration path will use this migrated local
      // fallback and publish it only after hydration completes.
    }
  }, [sessionKey, sessionId, storedToken]);

  useEffect(() => {
    if (!editor || window.localStorage.getItem(key)) return;
    if (editor.getCurrentPageShapes().length > 0) return;
    const noteId = createShapeId('session-note');
    if (editor.getShape(noteId)) return;
    editor.createShape({
      id: noteId,
      type: 'text',
      x: 160,
      y: 140,
      props: {
        richText: toRichText('Questa whiteboard è associata alla Chat corrente.'),
        color: 'black',
        size: 'l',
        font: 'sans',
        textAlign: 'start',
        autoSize: true,
      },
    });
  }, [editor, key]);

  useEffect(() => {
    if (!editor) return;
    const publishSnapshot = () => {
      if (sessionId && !remoteHydratedRef.current) return;
      if (publishTimerRef.current !== null) window.clearTimeout(publishTimerRef.current);
      publishTimerRef.current = window.setTimeout(() => {
        publishTimerRef.current = null;
        // Full editor snapshot: document + session (current page, per-page
        // cameras, selection). This is what makes save/resume pixel-identical.
        const snapshot = getSnapshot(editor.store);
        if (sessionId && !window.localStorage.getItem(key) && editor.getCurrentPageShapes().length === 0) return;
        try {
          window.localStorage.setItem(key, JSON.stringify(snapshot));
        } catch {
          // Persistence is best-effort; the board remains usable if storage is full.
        }
        if (sessionId) {
          void fetch('/api/local/chat/whiteboard', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', ...(storedToken ? { Authorization: `Bearer ${storedToken}` } : {}) },
            body: JSON.stringify({ sessionId, sessionKey, snapshot }),
          }).catch(() => {});
        }
      }, 300);
    };
    publishSnapshot();
    // Listen to BOTH scopes: 'document' covers shape changes, 'session'
    // covers page switches, camera moves, and selection so the whole
    // workspace state survives a reload.
    const disposeDocument = editor.store.listen(publishSnapshot, { scope: 'document' });
    const disposeSession = editor.store.listen(publishSnapshot, { scope: 'session' });
    return () => {
      if (publishTimerRef.current !== null) {
        window.clearTimeout(publishTimerRef.current);
        publishTimerRef.current = null;
      }
      // Flush the latest editor state synchronously before the drawer/canvas
      // is unmounted, so a fast close cannot lose the drawing.
      if (!sessionId || remoteHydratedRef.current) {
        try {
          window.localStorage.setItem(key, JSON.stringify(getSnapshot(editor.store)));
        } catch {
          // Best effort; the remote snapshot remains the secondary store.
        }
      }
      disposeDocument();
      disposeSession();
    };
  }, [editor, key, sessionId, sessionKey, storedToken]);

  useEffect(() => {
    if (!editor || !sessionId) return;
    let cancelled = false;
    const headers = storedToken ? { Authorization: `Bearer ${storedToken}` } : undefined;

    const applyRemoteState = async () => {
      if (!expanded) return;
      try {
        const response = await fetch(`/api/local/chat/whiteboard?sessionKey=${encodeURIComponent(sessionKey || '')}&sessionId=${encodeURIComponent(sessionId)}`, { headers });
        if (!response.ok || cancelled) return;
        const remote = await response.json() as { snapshot?: TLStoreSnapshot & { session?: unknown }; commands?: BridgeCommand[] };
        if (!remote.snapshot) {
          // Server snapshot wins for identified sessions. localStorage is only
          // a fallback when the server has no board yet.
          const raw = window.localStorage.getItem(key);
          if (raw && !remoteHydratedRef.current) {
            try {
              loadSnapshot(editor.store, sanitizeSnapshot(JSON.parse(raw) as TLStoreSnapshot));
            } catch {
              window.localStorage.removeItem(key);
            }
          }
          remoteHydratedRef.current = true;
        }
        if (remote.snapshot && !remoteHydratedRef.current) {
          remoteHydratedRef.current = true;
          loadSnapshot(editor.store, sanitizeSnapshot(remote.snapshot));
          // Only fit when the snapshot carries no session state (legacy boards).
          // Full snapshots already restore the saved per-page cameras.
          if (!remote.snapshot.session) {
            editor.zoomToFit({ animation: { duration: 250 } });
          }
        }
        const commands = remote.commands || [];
        const applied: string[] = [];
        for (const command of commands) {
          if (command.type === 'create_text' && command.text) {
            editor.createShape({
              id: createShapeId(`agent-text-${command.id}`),
              type: 'text',
              x: command.x ?? 160,
              y: command.y ?? 140,
              props: {
                richText: toRichText(command.text),
                color: command.color === 'black' ? 'black' : 'violet',
                size: 'l',
                font: 'sans',
                textAlign: 'start',
                autoSize: true,
              },
            });
          }
          if (command.type === 'create_line') {
            const width = command.w ?? 180;
            const lineWidth = Math.abs(width);
            editor.createShape({
              id: createShapeId(`agent-line-${command.id}`),
              type: 'geo',
              x: width < 0 ? (command.x ?? 180) - lineWidth : command.x ?? 180,
              y: command.y ?? 360,
              props: {
                geo: 'rectangle',
                w: lineWidth,
                h: command.h ?? 4,
                color: command.color === 'violet' ? 'violet' : 'black',
                fill: 'none',
                dash: 'solid',
                size: 'm',
              },
            });
          }
          if (command.type === 'create_box' && command.text) {
            editor.createShape({
              id: createShapeId(`agent-box-${command.id}`),
              type: 'geo',
              x: command.x ?? 160,
              y: command.y ?? 160,
              props: {
                geo: 'rectangle',
                w: Math.max(120, command.w ?? 220),
                h: Math.max(70, command.h ?? 90),
                richText: toRichText(command.text),
                color: command.color === 'violet' ? 'violet' : command.color === 'green' ? 'green' : 'blue',
                fill: 'semi',
                dash: 'solid',
                size: 'm',
                align: 'middle',
                verticalAlign: 'middle',
                font: 'sans',
              },
            });
          }
          if (command.type === 'create_frame') {
            editor.createShape({
              id: createShapeId(`agent-frame-${command.id}`),
              type: 'frame',
              x: command.x ?? 40,
              y: command.y ?? 40,
              props: {
                w: Math.max(300, command.w ?? 760),
                h: Math.max(180, command.h ?? 240),
                name: command.text || 'Layer',
                color: command.color === 'violet' ? 'violet' : command.color === 'green' ? 'green' : 'blue',
              },
            });
          }
          if (command.type === 'create_arrow') {
            editor.createShape({
              id: createShapeId(`agent-arrow-${command.id}`),
              type: 'arrow',
              x: command.x ?? 0,
              y: command.y ?? 0,
              props: {
                kind: 'arc',
                start: { x: 0, y: 0 },
                end: { x: command.w ?? 160, y: command.h ?? 0 },
                bend: 0,
                color: command.color === 'violet' ? 'violet' : 'black',
                fill: 'none',
                dash: 'solid',
                size: 'm',
                arrowheadStart: 'none',
                arrowheadEnd: 'arrow',
                font: 'draw',
                richText: toRichText(''),
                labelPosition: 0.5,
                labelColor: 'black',
                scale: 1,
                elbowMidPoint: 0.5,
              },
            });
          }
          if (command.type === 'create_shape' && command.shapeType) {
            const shapeProps = command.props || {};
            editor.createShape({
              id: createShapeId(`agent-shape-${command.id}`),
              type: command.shapeType as never,
              x: command.x ?? 160,
              y: command.y ?? 160,
              props: shapeProps as never,
            } as never);
          }
          if (command.type === 'move_shape' && command.shapeId && (command.x !== undefined || command.y !== undefined)) {
            const shape = editor.getShape(command.shapeId as never);
            if (shape) editor.updateShape({ id: shape.id, type: shape.type, x: command.x ?? shape.x, y: command.y ?? shape.y } as never);
          }
          if (command.type === 'update_shape' && command.shapeId && command.props) {
            editor.updateShape({
              id: command.shapeId as never,
              type: command.shapeType as never,
              props: command.props as never,
            } as never);
          }
          if (command.type === 'delete_shapes' && command.shapeIds?.length) {
            editor.deleteShapes(command.shapeIds as never);
          }
          if (command.type === 'duplicate' && command.shapeIds?.length) {
            editor.duplicateShapes(command.shapeIds as never);
          }
          if (command.type === 'group' && command.shapeIds?.length) {
            editor.groupShapes(command.shapeIds as never);
          }
          if (command.type === 'ungroup' && command.shapeIds?.length) {
            editor.ungroupShapes(command.shapeIds as never);
          }
          if (command.type === 'bring_to_front' && command.shapeIds?.length) {
            editor.bringToFront(command.shapeIds as never);
          }
          if (command.type === 'send_to_back' && command.shapeIds?.length) {
            editor.sendToBack(command.shapeIds as never);
          }
          if (command.type === 'zoom_to_fit') {
            editor.zoomToFit({ animation: { duration: 250 } });
          }
          if (command.type === 'delete_bindings' && command.bindingIds?.length) editor.deleteBindings(command.bindingIds as never);
          if (command.type === 'create_binding' && command.fromId && command.toId) {
            editor.createBinding({
              id: createBindingId(),
              type: 'arrow',
              fromId: command.fromId as never,
              toId: command.toId as never,
              props: command.bindingProps || {},
            } as never);
          }
          if (command.type === 'create_page') editor.createPage({ name: command.text || 'New page' });
          if (command.type === 'set_current_page' && command.pageId) editor.setCurrentPage(command.pageId as never);
          if (command.type === 'rename_page' && command.pageId && command.text) editor.renamePage(command.pageId as never, command.text);
          if (command.type === 'delete_page' && command.pageId) editor.deletePage(command.pageId as never);
          if (command.type === 'move_shapes_to_page' && command.shapeIds?.length && command.pageId) editor.moveShapesToPage(command.shapeIds as never, command.pageId as never);
          if (command.type === 'align_shapes' && command.shapeIds?.length && command.direction) editor.alignShapes(command.shapeIds as never, command.direction as never);
          if (command.type === 'distribute_shapes' && command.shapeIds?.length && command.direction) editor.distributeShapes(command.shapeIds as never, command.direction as never);
          if (command.type === 'pack_shapes' && command.shapeIds?.length) editor.packShapes(command.shapeIds as never, command.padding ?? 8);
          if (command.type === 'flip_shapes' && command.shapeIds?.length && command.direction) editor.flipShapes(command.shapeIds as never, command.direction as never);
          if (command.type === 'rotate_shapes' && command.shapeIds?.length) editor.rotateShapesBy(command.shapeIds as never, command.angle ?? 0);
          if (command.type === 'resize_shape' && command.shapeId && command.props) editor.resizeShape(command.shapeId as never, { x: Number(command.props.x ?? 1), y: Number(command.props.y ?? 1) } as never);
          if (command.type === 'toggle_lock' && command.shapeIds?.length) editor.toggleLock(command.shapeIds as never);
          if (command.type === 'set_style' && command.style && command.value) editor.setStyleForSelectedShapes(command.style as never, command.value as never);
          if (command.type === 'set_opacity' && typeof command.value === 'number') editor.setOpacityForSelectedShapes(command.value);
          if (command.type === 'export_json' || command.type === 'export_svg' || command.type === 'export_png') {
            const filename = `tldraw-${sessionId}-${Date.now()}`;
            if (command.type === 'export_json') {
              const blob = new Blob([JSON.stringify(getSnapshot(editor.store), null, 2)], { type: 'application/json' });
              const href = URL.createObjectURL(blob);
              const link = document.createElement('a');
              link.href = href;
              link.download = `${filename}.json`;
              link.click();
              URL.revokeObjectURL(href);
            } else if (command.type === 'export_svg') {
              const result = await editor.getSvgString(editor.getCurrentPageShapes(), { background: true });
              if (result) {
                const blob = new Blob([result.svg], { type: 'image/svg+xml' });
                const href = URL.createObjectURL(blob);
                const link = document.createElement('a');
                link.href = href;
                link.download = `${filename}.svg`;
                link.click();
                URL.revokeObjectURL(href);
              }
            } else {
              const result = await editor.toImage(editor.getCurrentPageShapes(), { format: 'png', pixelRatio: 2, padding: PNG_EXPORT_PADDING, background: true });
              const href = URL.createObjectURL(result.blob);
              const link = document.createElement('a');
              link.href = href;
              link.download = `${filename}.png`;
              link.click();
              URL.revokeObjectURL(href);
            }
          }

          if (command.type === 'clear') {
            editor.selectAll();
            editor.deleteShapes(editor.getSelectedShapeIds());
          }
          if (isSupportedCommand(command)) {
            applied.push(command.id);
          }
        }
        if (applied.length) {
          editor.zoomToFit({ animation: { duration: 250 } });
          const appliedCommands = commands.filter((c) => applied.includes(c.id));
          const summary = appliedCommands
            .map((c) => `${c.type}${c.text ? ` "${String(c.text).slice(0, 40)}"` : ''}`)
            .join(', ');
          onActionApplied?.(`Canvas actions applied: ${summary}`);
          setLints(lintBoard(editor));
          await fetch('/api/local/chat/whiteboard', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', ...(storedToken ? { Authorization: `Bearer ${storedToken}` } : {}) },
            body: JSON.stringify({ sessionId, sessionKey, action: 'ack', commandIds: applied }),
          });
        }
      } catch {
        // The local bridge is optional; local editor persistence remains primary.
      }
    };

    void applyRemoteState();
    const timer = window.setInterval(() => void applyRemoteState(), 1000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [editor, expanded, key, sessionId, sessionKey, storedToken, onActionApplied]);

  const sendSelection = async (withScreenshot = false) => {
    if (!editor) {
      setToastMessage('Board is still loading.');
      return;
    }
    setToastMessage(withScreenshot ? 'Capturing board…' : 'Sending board context…');
    try {
      const context = await collectBoardContext(editor, sessionKey, sessionId, { includeScreenshot: withScreenshot, container: canvasContainerRef.current });
      if (withScreenshot && !context.screenshot?.dataUrl) {
        throw new Error('Screenshot capture returned no image.');
      }
      const lines: string[] = [];
      const fragment = modePromptFragment(agentMode);
      if (fragment) lines.push(fragment);
      lines.push(
      `Whiteboard context from session ${sessionId || 'pending'} (sessionKey ${sessionKey || 'pending'}):`,
      `viewport: x=${context.viewport.x} y=${context.viewport.y} w=${context.viewport.w} h=${context.viewport.h} zoom=${context.viewport.zoom}`,
      `shapes on screen: ${context.visibleShapes.length}, offscreen: ${context.offscreenCount}`,
      `counts: ${JSON.stringify(context.shapeCounts)}`,
      `bindings: ${context.bindings.length}`,
    );
    if (context.selectedShapeIds.length) {
      lines.push(`selected: ${context.selectedShapeIds.join(', ')}`);
    } else {
      lines.push('No shapes are selected. Describe the current whiteboard and suggest the next step.');
    }
    for (const shape of context.visibleShapes.slice(0, 40)) {
      lines.push(JSON.stringify(shape));
    }
    lines.push(formatLints(lintBoard(editor)));
    const attachments: Array<{ kind: 'image'; name: string; mimeType: string; dataUrl: string }> = [];
    if (context.screenshot?.dataUrl) {
      attachments.push({
        kind: 'image',
        name: `tldraw-board-${Date.now()}.png`,
        mimeType: 'image/png',
        dataUrl: context.screenshot.dataUrl,
      });
    }
    await onSendSelection(lines.join('\n'), attachments);
      setToastMessage(withScreenshot ? 'Screenshot added to chat.' : 'Board context sent.');
    } catch (error) {
      setToastMessage(error instanceof Error ? 'Could not capture the board.' : 'Board action failed.');
    }
  };

  const clearBoard = () => {
    if (!editor) return;
    editor.selectAll();
    editor.deleteShapes(editor.getSelectedShapeIds());
    window.localStorage.removeItem(key);
  };

  const exportBoard = async (format: 'png' | 'svg' | 'json') => {
    if (!editor) return;
    const filename = `tldraw-${sessionId || sessionKey || 'board'}-${Date.now()}`;
    if (format === 'json') {
      const blob = new Blob([JSON.stringify(getSnapshot(editor.store), null, 2)], { type: 'application/json' });
      triggerDownload(URL.createObjectURL(blob), `${filename}.json`);
      return;
    }
    if (format === 'svg') {
      const result = await editor.getSvgString(editor.getCurrentPageShapes(), { background: true });
      if (result) triggerDownload(URL.createObjectURL(new Blob([result.svg], { type: 'image/svg+xml' })), `${filename}.svg`);
      return;
    }
    const image = await editor.toImage(editor.getCurrentPageShapes(), { format: 'png', pixelRatio: 2, padding: PNG_EXPORT_PADDING, background: true });
    triggerDownload(URL.createObjectURL(image.blob), `${filename}.png`);
  };

  return (
    <section className={`tldraw-canvas-panel ${expanded ? 'is-expanded' : ''}`} aria-label="TLDrawCanvas">
      <header className="tldraw-canvas-head">
        <div className="tldraw-canvas-title">
          <button type="button" className="chat-icon-button tldraw-canvas-back" onClick={onClose} title="Back to chat" aria-label="Back to chat">
            <ArrowLeft size={18} />
          </button>
          <div className="tldraw-canvas-heading">
            <span className="eyebrow">Session canvas</span>
            <h3>TLDrawCanvas</h3>
            <span className="tldraw-canvas-linked-session" title={sessionId || 'Session is still being created'}>
              {sessionTitle} · {sessionId ? sessionId.slice(0, 12) : 'pending'}
              {lints.length > 0 ? ` · ⚠ ${lints.length}` : ''}
            </span>
          </div>
        </div>
        <div className="tldraw-canvas-toolbar">
          <select
            className="tldraw-canvas-select"
            value={agentMode}
            onChange={(event) => void changeAgentMode(event.target.value as AgentMode)}
            title="Agent mode"
            aria-label="Agent mode"
          >
            <option value="">Free</option>
            {AGENT_MODES.map((mode) => (
              <option key={mode.id} value={mode.id}>{mode.label}</option>
            ))}
          </select>
          <select
            className="tldraw-canvas-select"
            value=""
            onChange={(event) => {
              const format = event.target.value as 'png' | 'svg' | 'json' | '';
              if (format) void exportBoard(format);
              event.target.value = '';
            }}
            title="Export board"
            aria-label="Export board"
          >
            <option value="">Export</option>
            <option value="png">PNG</option>
            <option value="svg">SVG</option>
            <option value="json">JSON</option>
          </select>
          <span className="tldraw-canvas-sep" />
          <button type="button" className="chat-icon-button" onClick={() => void sendSelection(true)} title="Screenshot to chat" aria-label="Screenshot to chat">
            <Camera size={16} />
          </button>
          <button type="button" className="chat-icon-button" onClick={() => void sendSelection(false)} title="Send selection to chat" aria-label="Send selection to chat">
            <Send size={16} />
          </button>
          {toastMessage ? <div className="tldraw-canvas-toast" role="status">{toastMessage}</div> : null}
          <button type="button" className="chat-icon-button" onClick={clearBoard} title="Clear board" aria-label="Clear board">
            <RotateCcw size={16} />
          </button>
        </div>
      </header>
      <div className="tldraw-canvas-canvas" ref={canvasContainerRef}>
        <Tldraw onMount={handleMount} />
        {loading ? (
          <div className="tldraw-canvas-loading" role="status">
            <Loader2 size={24} className="chat-spin" />
            <span>Loading TLDrawCanvas…</span>
          </div>
        ) : null}
      </div>
    </section>
  );
});
