/**
 * Geometric and semantic lints for the tldraw board.
 * Detects the recurring visual defects the agent can introduce
 * (detached arrows, overlaps, offscreen content, tiny shapes)
 * so review mode can report and fix them.
 */
import type { Editor } from 'tldraw';
import { collectBindings } from './tldraw-visual-context';

export type BoardLintSeverity = 'error' | 'warning';

export interface BoardLint {
  id: string;
  severity: BoardLintSeverity;
  rule: string;
  message: string;
  shapeIds: string[];
}

const MIN_SHAPE_SIZE = 8;
const OVERLAP_EPSILON = 4;

function rectOverlap(a: { x: number; y: number; w: number; h: number }, b: { x: number; y: number; w: number; h: number }): boolean {
  return (
    a.x + OVERLAP_EPSILON < b.x + b.w &&
    b.x + OVERLAP_EPSILON < a.x + a.w &&
    a.y + OVERLAP_EPSILON < b.y + b.h &&
    b.y + OVERLAP_EPSILON < a.y + a.h
  );
}

export function lintBoard(editor: Editor): BoardLint[] {
  const lints: BoardLint[] = [];
  const shapes = editor.getCurrentPageShapes();
  const boundsById = new Map<string, { x: number; y: number; w: number; h: number }>();

  for (const shape of shapes) {
    const bounds = editor.getShapePageBounds(shape.id);
    if (bounds) boundsById.set(String(shape.id), { x: bounds.x, y: bounds.y, w: bounds.w, h: bounds.h });
  }

  // Rule 1: arrows/lines without bindings (detached connectors).
  const boundFromIds = new Set<string>();
  const boundToIds = new Set<string>();
  for (const binding of collectBindings(editor)) {
    boundFromIds.add(binding.fromId);
    boundToIds.add(binding.toId);
  }
  const connectors = shapes.filter((s) => s.type === 'arrow' || s.type === 'line');
  for (const connector of connectors) {
    const id = String(connector.id);
    const hasBinding = boundFromIds.has(id) || boundToIds.has(id);
    if (!hasBinding) {
      lints.push({
        id: `detached-${id}`,
        severity: 'error',
        rule: 'detached_connector',
        message: 'A connector is not bound to any shape and will drift out of position on move.',
        shapeIds: [id],
      });
    }
  }

  // Rule 2: overlapping non-connector shapes. Freehand draw strokes overlap
  // naturally (that's how drawing works), so they are exempt.
  const nodes = shapes.filter((s) => s.type !== 'arrow' && s.type !== 'line' && s.type !== 'draw' && s.type !== 'highlight');
  for (let i = 0; i < nodes.length; i += 1) {
    for (let j = i + 1; j < nodes.length; j += 1) {
      const a = boundsById.get(String(nodes[i].id));
      const b = boundsById.get(String(nodes[j].id));
      if (!a || !b) continue;
      // Skip containment (frames legitimately contain shapes).
      const aContainsB = a.x <= b.x && a.y <= b.y && a.x + a.w >= b.x + b.w && a.y + a.h >= b.y + b.h;
      const bContainsA = b.x <= a.x && b.y <= a.y && b.x + b.w >= a.x + a.w && b.y + b.h >= a.y + a.h;
      if (aContainsB || bContainsA) continue;
      if (rectOverlap(a, b)) {
        lints.push({
          id: `overlap-${nodes[i].id}-${nodes[j].id}`,
          severity: 'warning',
          rule: 'overlapping_shapes',
          message: `Two shapes overlap: ${String(nodes[i].type)} at (${Math.round(a.x)}, ${Math.round(a.y)}) and ${String(nodes[j].type)} at (${Math.round(b.x)}, ${Math.round(b.y)}).`,
          shapeIds: [String(nodes[i].id), String(nodes[j].id)],
        });
      }
    }
  }

  // Rule 3: degenerate tiny shapes.
  for (const shape of shapes) {
    const bounds = boundsById.get(String(shape.id));
    if (!bounds) continue;
    if (shape.type === 'arrow' || shape.type === 'line' || shape.type === 'draw' || shape.type === 'highlight') continue; // connectors and freehand strokes may legitimately be thin
    if (bounds.w < MIN_SHAPE_SIZE || bounds.h < MIN_SHAPE_SIZE) {
      lints.push({
        id: `tiny-${shape.id}`,
        severity: 'error',
        rule: 'degenerate_shape',
        message: `A ${shape.type} shape is degenerate (${Math.round(bounds.w)}×${Math.round(bounds.h)}). This usually means invalid persisted geometry.`,
        shapeIds: [String(shape.id)],
      });
    }
  }

  // Rule 4: content outside the viewport (only when the board is non-empty).
  if (shapes.length > 0) {
    const viewport = editor.getViewportPageBounds();
    const offscreen = shapes.filter((s) => {
      const b = boundsById.get(String(s.id));
      if (!b) return false;
      return b.x + b.w < viewport.x || b.x > viewport.x + viewport.w || b.y + b.h < viewport.y || b.y > viewport.y + viewport.h;
    });
    if (offscreen.length > 0) {
      lints.push({
        id: 'offscreen-content',
        severity: 'warning',
        rule: 'offscreen_content',
        message: `${offscreen.length} shape(s) are outside the current viewport. Run zoom_to_fit to bring them back.`,
        shapeIds: offscreen.map((s) => String(s.id)),
      });
    }
  }

  return lints;
}

/** Compact text rendering of lints for chat context. */
export function formatLints(lints: BoardLint[]): string {
  if (lints.length === 0) return 'Board lints: none. The board is clean.';
  const lines = lints.map((lint) => `[${lint.severity.toUpperCase()}] ${lint.rule}: ${lint.message}`);
  return `Board lints (${lints.length}):\n${lines.join('\n')}`;
}
