/**
 * Visual context capture for the tldraw agent.
 * Collects viewport, selection, shape metadata, bindings, and a screenshot
 * so the agent can "see" the board like the official Agent Starter Kit does.
 */
import type { Editor, TLShape, TLShapeId } from 'tldraw';

export interface ShapeSummary {
  id: string;
  type: string;
  x: number;
  y: number;
  w?: number;
  h?: number;
  text?: string;
  color?: string;
}

export interface BoardContext {
  sessionKey: string | null;
  sessionId: string | null;
  viewport: { x: number; y: number; w: number; h: number; zoom: number };
  selectedShapeIds: string[];
  visibleShapes: ShapeSummary[];
  offscreenCount: number;
  shapeCounts: Record<string, number>;
  bindings: Array<{ id: string; fromId: string; toId: string }>;
  screenshot?: { dataUrl: string; width: number; height: number };
  capturedAt: number;
}

function shapeText(editor: Editor, shape: TLShape): string | undefined {
  const props = (shape as unknown as { props?: Record<string, unknown> }).props ?? {};
  const rich = props.richText as { toString?: () => string } | undefined;
  if (rich && typeof rich.toString === 'function') {
    const text = rich.toString().trim();
    return text || undefined;
  }
  if (typeof props.name === 'string' && props.name.trim()) return props.name.trim();
  if (typeof props.text === 'string' && props.text.trim()) return props.text.trim();
  return undefined;
}

export function summarizeShapes(editor: Editor): {
  visible: ShapeSummary[];
  offscreenCount: number;
  counts: Record<string, number>;
} {
  const viewport = editor.getViewportPageBounds();
  const all = editor.getCurrentPageShapes();
  const visible: ShapeSummary[] = [];
  let offscreenCount = 0;
  const counts: Record<string, number> = {};

  for (const shape of all) {
    counts[shape.type] = (counts[shape.type] ?? 0) + 1;
    const bounds = editor.getShapePageBounds(shape.id);
    const onScreen =
      bounds &&
      bounds.x < viewport.x + viewport.w &&
      bounds.x + bounds.w > viewport.x &&
      bounds.y < viewport.y + viewport.h &&
      bounds.y + bounds.h > viewport.y;

    if (!onScreen) {
      offscreenCount += 1;
      continue;
    }

    visible.push({
      id: shape.id,
      type: shape.type,
      x: Math.round(shape.x),
      y: Math.round(shape.y),
      w: bounds ? Math.round(bounds.w) : undefined,
      h: bounds ? Math.round(bounds.h) : undefined,
      text: shapeText(editor, shape),
      color: (shape as unknown as { props?: { color?: string } }).props?.color,
    });
  }

  return { visible, offscreenCount, counts };
}

export function collectBindings(editor: Editor): Array<{ id: string; fromId: string; toId: string }> {
  // Bindings live in the store as records of type 'binding'; read them directly
  // so we get every arrow relationship regardless of which shape anchors it.
  const bindings: Array<{ id: string; fromId: string; toId: string }> = [];
  for (const record of Object.values(editor.store.allRecords())) {
    const rec = record as { typeName?: string; id?: string; fromId?: string; toId?: string };
    if (rec.typeName === 'binding' && rec.id && rec.fromId && rec.toId) {
      bindings.push({ id: rec.id, fromId: rec.fromId, toId: rec.toId });
    }
  }
  return bindings;
}

async function blobToDataUrl(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => typeof reader.result === 'string' ? resolve(reader.result) : reject(new Error('PNG data URL conversion failed'));
    reader.onerror = () => reject(reader.error ?? new Error('PNG data URL conversion failed'));
    reader.readAsDataURL(blob);
  });
}

/** Screenshot using tldraw's native PNG exporter, with SVG rasterization as fallback. */
export async function captureScreenshot(
  editor: Editor,
  maxWidthPx = 1280,
  container?: HTMLElement | null,
): Promise<BoardContext['screenshot'] | undefined> {
  try {
    const shapes = editor.getCurrentPageShapes();
    if (!shapes.length) return undefined;

    const native = await editor.toImage(shapes, {
      format: 'png',
      pixelRatio: 2,
      padding: 32,
      background: true,
    });
    const scale = Math.min(1, maxWidthPx / Math.max(native.width, 1));
    if (scale === 1) {
      return { dataUrl: await blobToDataUrl(native.blob), width: native.width, height: native.height };
    }

    const url = URL.createObjectURL(native.blob);
    try {
      const image = await new Promise<HTMLImageElement>((resolve, reject) => {
        const img = new Image();
        img.onload = () => resolve(img);
        img.onerror = () => reject(new Error('Native PNG could not be resized'));
        img.src = url;
      });
      const canvas = document.createElement('canvas');
      canvas.width = Math.max(1, Math.round(native.width * scale));
      canvas.height = Math.max(1, Math.round(native.height * scale));
      const ctx = canvas.getContext('2d');
      if (!ctx) throw new Error('Screenshot canvas is unavailable');
      ctx.drawImage(image, 0, 0, canvas.width, canvas.height);
      return { dataUrl: canvas.toDataURL('image/png'), width: canvas.width, height: canvas.height };
    } finally {
      URL.revokeObjectURL(url);
    }
  } catch (nativeError) {
    // Keep a browser-compatible fallback for older tldraw/browser combinations.
    try {
      const result = await editor.getSvgString(editor.getCurrentPageShapes(), { background: true, padding: 32 });
      if (!result?.svg) return undefined;
      const url = URL.createObjectURL(new Blob([result.svg], { type: 'image/svg+xml;charset=utf-8' }));
      try {
        const image = await new Promise<HTMLImageElement>((resolve, reject) => {
          const img = new Image();
          img.onload = () => resolve(img);
          img.onerror = () => reject(nativeError);
          img.src = url;
        });
        const width = Number(result.width) || editor.getViewportScreenBounds().w;
        const height = Number(result.height) || editor.getViewportScreenBounds().h;
        const scale = Math.min(1, maxWidthPx / Math.max(width, 1));
        const canvas = document.createElement('canvas');
        canvas.width = Math.max(1, Math.round(width * scale));
        canvas.height = Math.max(1, Math.round(height * scale));
        const ctx = canvas.getContext('2d');
        if (!ctx) return undefined;
        ctx.drawImage(image, 0, 0, canvas.width, canvas.height);
        return { dataUrl: canvas.toDataURL('image/png'), width: canvas.width, height: canvas.height };
      } finally {
        URL.revokeObjectURL(url);
      }
    } catch {
      return undefined;
    }
  }
}

/** Full board context snapshot for the agent. */
export async function collectBoardContext(
  editor: Editor,
  sessionKey: string | null,
  sessionId: string | null,
  options: { includeScreenshot?: boolean; container?: HTMLElement | null } = {},
): Promise<BoardContext> {
  const vp = editor.getViewportPageBounds();
  const camera = editor.getCamera();
  const { visible, offscreenCount, counts } = summarizeShapes(editor);
  const selected = [...editor.getSelectedShapeIds()].map((id: TLShapeId) => String(id));

  let screenshot: BoardContext['screenshot'];
  if (options.includeScreenshot !== false) {
    screenshot = await captureScreenshot(editor, 1280, options.container);
  }

  return {
    sessionKey,
    sessionId,
    viewport: { x: Math.round(vp.x), y: Math.round(vp.y), w: Math.round(vp.w), h: Math.round(vp.h), zoom: Number(camera.z.toFixed(3)) },
    selectedShapeIds: selected,
    visibleShapes: visible,
    offscreenCount,
    shapeCounts: counts,
    bindings: collectBindings(editor),
    screenshot,
    capturedAt: Date.now(),
  };
}
