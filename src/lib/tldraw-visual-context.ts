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

/** Capture the visible canvas as rendered in the DOM, via SVG foreignObject. */
export async function captureScreenshot(
  editor: Editor,
  maxWidthPx = 1280,
  container?: HTMLElement | null,
): Promise<BoardContext['screenshot'] | undefined> {
  // Preferred path: composite the real <canvas> elements inside the container.
  // html-to-image cannot copy canvas pixel data during DOM serialization, so we
  // draw each canvas onto an offscreen canvas ourselves — this is a true
  // screenshot of what the user sees (shapes + camera + overlays).
  if (container) {
    try {
      const width = container.clientWidth;
      const height = container.clientHeight;
      if (width > 0 && height > 0) {
        const scale = Math.min(1, maxWidthPx / width);
        const out = document.createElement('canvas');
        out.width = Math.round(width * scale);
        out.height = Math.round(height * scale);
        const ctx = out.getContext('2d');
        if (ctx) {
          ctx.fillStyle = '#ffffff';
          ctx.fillRect(0, 0, out.width, out.height);
          let drew = false;
          for (const canvas of Array.from(container.querySelectorAll('canvas'))) {
            const rect = canvas.getBoundingClientRect();
            const box = container.getBoundingClientRect();
            // Skip canvases that are not actually visible in layout flow.
            if (rect.width < 2 || rect.height < 2) continue;
            try {
              ctx.drawImage(
                canvas,
                (rect.left - box.left) * scale,
                (rect.top - box.top) * scale,
                rect.width * scale,
                rect.height * scale,
              );
              drew = true;
            } catch {
              // Tainted canvas — skip this layer.
            }
          }
          if (drew) {
            return { dataUrl: out.toDataURL('image/png'), width: out.width, height: out.height };
          }
        }
      }
    } catch {
      // Fall through to the tldraw-native renderer.
    }
  }
  // Fallback: render page shapes with tldraw's own exporter.
  try {
    const bounds = editor.getViewportScreenBounds();
    const scale = Math.min(1, maxWidthPx / Math.max(bounds.w, 1));
    const result = await editor.toImage([...editor.getCurrentPageShapeIds()], {
      format: 'png',
      background: true,
      padding: 0,
      scale,
    } as never);
    if (!result) return undefined;
    if (result instanceof Blob) {
      const dataUrl = await new Promise<string>((resolve) => {
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result));
        reader.readAsDataURL(result);
      });
      return { dataUrl, width: Math.round(bounds.w * scale), height: Math.round(bounds.h * scale) };
    }
    const src = (result as unknown as { src?: string }).src;
    if (typeof src === 'string') {
      return { dataUrl: src, width: Math.round(bounds.w * scale), height: Math.round(bounds.h * scale) };
    }
    return undefined;
  } catch {
    return undefined;
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
