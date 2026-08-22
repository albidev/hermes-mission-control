/**
 * Validated, transactional application of agent commands to a tldraw Editor.
 *
 * The bridge sends JSON commands; this module is the ONLY place that turns
 * them into editor mutations. Validation happens BEFORE any mutation so an
 * invalid command can never leave the store half-mutated.
 */
import {
  createBindingId,
  createShapeId,
  toRichText,
  type Editor,
  type TLShapeId,
} from 'tldraw';
import { mermaidToCommands } from './tldraw-mermaid';

export interface BridgeCommand {
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
}

export interface ActionOutcome {
  appliedIds: string[];
  failed: Array<{ id: string; reason: string }>;
  affectedShapeIds: TLShapeId[];
}

const GEO_COLORS = new Set(['black', 'grey', 'light-violet', 'violet', 'blue', 'light-blue', 'yellow', 'orange', 'green', 'light-green', 'light-red', 'red', 'white']);

function finite(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value);
}

function positiveOrUndefined(value: unknown): boolean {
  return value === undefined || (finite(value) && (value as number) > 0);
}

/** Validate a single command against current editor state. Returns error reason or null. */
export function validateCommand(editor: Editor, command: BridgeCommand): string | null {
  const t = command.type;

  if (t === 'create_box' || t === 'create_frame') {
    if (!command.text) return `${t}: missing label`;
    if (!finite(command.x) || !finite(command.y)) return `${t}: x/y must be finite numbers`;
    if (!positiveOrUndefined(command.w) || !positiveOrUndefined(command.h)) return `${t}: w/h must be positive`;
    return null;
  }

  if (t === 'create_arrow' || t === 'create_line') {
    if (!finite(command.x) || !finite(command.y)) return `${t}: x/y must be finite numbers`;
    if (!positiveOrUndefined(command.w) && !(t === 'create_arrow' && finite(command.h))) return `${t}: needs positive w or h`;
    if (command.color && !GEO_COLORS.has(command.color)) return `${t}: unsupported color ${String(command.color)}`;
    return null;
  }

  if (t === 'create_text') {
    if (!command.text) return 'create_text: missing text';
    return null;
  }

  if (t === 'move_shape') {
    if (!command.shapeId) return 'move_shape: missing shapeId';
    if (!editor.getShape(command.shapeId as TLShapeId)) return `move_shape: unknown shape ${command.shapeId}`;
    if (command.x !== undefined && !finite(command.x)) return 'move_shape: x not finite';
    if (command.y !== undefined && !finite(command.y)) return 'move_shape: y not finite';
    return null;
  }

  if (t === 'update_shape') {
    if (!command.shapeId) return 'update_shape: missing shapeId';
    if (!editor.getShape(command.shapeId as TLShapeId)) return `update_shape: unknown shape ${command.shapeId}`;
    if (!command.props || typeof command.props !== 'object') return 'update_shape: missing props';
    return null;
  }

  if (t === 'delete_shapes' || t === 'duplicate' || t === 'group' || t === 'bring_to_front' || t === 'send_to_back') {
    if (!command.shapeIds?.length) return `${t}: missing shapeIds`;
    for (const id of command.shapeIds) {
      if (t !== 'group' && !editor.getShape(id as TLShapeId)) return `${t}: unknown shape ${id}`;
    }
    return null;
  }

  if (t === 'create_binding') {
    if (!command.fromId || !command.toId) return 'create_binding: fromId/toId required';
    if (!editor.getShape(command.fromId as TLShapeId)) return `create_binding: unknown from ${command.fromId}`;
    if (!editor.getShape(command.toId as TLShapeId)) return `create_binding: unknown to ${command.toId}`;
    return null;
  }

  if (t === 'delete_bindings') {
    if (!command.bindingIds?.length) return 'delete_bindings: missing bindingIds';
    return null;
  }

  if (t === 'align_shapes' || t === 'distribute_shapes' || t === 'flip_shapes') {
    if (!command.shapeIds?.length) return `${t}: missing shapeIds`;
    if (!command.direction) return `${t}: missing direction`;
    return null;
  }

  if (t === 'pack_shapes') {
    if (!command.shapeIds?.length) return 'pack_shapes: missing shapeIds';
    if (command.padding !== undefined && (!finite(command.padding) || (command.padding as number) < 0)) return 'pack_shapes: invalid padding';
    return null;
  }

  if (t === 'rotate_shapes') {
    if (!command.shapeIds?.length) return 'rotate_shapes: missing shapeIds';
    if (!finite(command.angle)) return 'rotate_shapes: angle must be finite radians';
    return null;
  }

  if (t === 'resize_shape') {
    if (!command.shapeId) return 'resize_shape: missing shapeId';
    if (!editor.getShape(command.shapeId as TLShapeId)) return `resize_shape: unknown shape ${command.shapeId}`;
    const sx = command.props?.x;
    const sy = command.props?.y;
    if (!finite(sx) || !finite(sy) || (sx as number) <= 0 || (sy as number) <= 0) return 'resize_shape: scale factors must be positive';
    return null;
  }

  // clear, zoom_to_fit, ungroup, pages ops, toggle_lock, set_style, set_opacity, exports:
  // no additional validation needed beyond SUPPORTED_COMMANDS membership.
  return null;
}

/**
 * Apply one validated command. Assumes validateCommand returned null.
 * Returns affected shape ids when derivable.
 */
function applyValidated(editor: Editor, command: BridgeCommand): TLShapeId[] {
  const t = command.type;
  switch (t) {
    case 'clear': {
      const ids = editor.getCurrentPageShapeIds();
      editor.deleteShapes([...ids]);
      return [...ids];
    }
    case 'create_text':
      editor.createShape({ id: createShapeId(`agent-text-${command.id}`), type: 'text', x: command.x ?? 160, y: command.y ?? 140, props: { richText: toRichText(command.text!), color: command.color === 'black' ? 'black' : 'violet', autoSize: true } } as never);
      return [createShapeId(`agent-text-${command.id}`)];
    case 'create_box':
      editor.createShape({ id: createShapeId(`agent-box-${command.id}`), type: 'geo', x: command.x!, y: command.y!, props: { geo: 'rectangle', w: command.w ?? 200, h: command.h ?? 90, color: command.color ?? 'black', richText: toRichText(command.text!) } } as never);
      return [createShapeId(`agent-box-${command.id}`)];
    case 'create_frame':
      editor.createShape({ id: createShapeId(`agent-frame-${command.id}`), type: 'frame', x: command.x!, y: command.y!, props: { name: command.text!, w: command.w ?? 800, h: command.h ?? 500 } } as never);
      return [createShapeId(`agent-frame-${command.id}`)];
    case 'create_arrow':
    case 'create_line': {
      const width = Math.abs(command.w ?? 0);
      const height = Math.abs(command.h ?? 0);
      const end = { x: width, y: height };
      editor.createShape({
        id: createShapeId(`agent-arrow-${command.id}`),
        type: t === 'create_arrow' ? 'arrow' : 'line',
        x: command.x!,
        y: command.y!,
        props: t === 'create_arrow'
          ? { color: command.color ?? 'black', arrowheadStart: 'none', arrowheadEnd: 'arrow', start: { x: 0, y: 0 }, end }
          : { color: command.color ?? 'black', start: { x: 0, y: 0 }, end },
      } as never);
      return [createShapeId(`agent-arrow-${command.id}`)];
    }
    case 'create_shape':
      editor.createShape({ id: createShapeId(`agent-shape-${command.id}`), type: command.shapeType as never, x: command.x ?? 160, y: command.y ?? 160, props: command.props ?? {} } as never);
      return [createShapeId(`agent-shape-${command.id}`)];
    case 'move_shape': {
      const shape = editor.getShape(command.shapeId as TLShapeId)!;
      editor.updateShape({ id: shape.id, type: shape.type, x: command.x ?? shape.x, y: command.y ?? shape.y } as never);
      return [shape.id];
    }
    case 'update_shape': {
      const shape = editor.getShape(command.shapeId as TLShapeId)!;
      editor.updateShape({ id: shape.id, type: shape.type, props: { ...shape.props, ...command.props } } as never);
      return [shape.id];
    }
    case 'delete_shapes':
      editor.deleteShapes(command.shapeIds as never);
      return [];
    case 'duplicate':
      editor.duplicateShapes(command.shapeIds as never, { x: 24, y: 24 });
      return [];
    case 'group':
      editor.groupShapes(command.shapeIds as never);
      return [];
    case 'bring_to_front':
      editor.bringToFront(command.shapeIds as never);
      return [];
    case 'send_to_back':
      editor.sendToBack(command.shapeIds as never);
      return [];
    case 'zoom_to_fit':
      editor.zoomToFit({ animation: { duration: 250 } });
      return [];
    case 'create_binding':
      editor.createBinding({ id: createBindingId(), type: 'arrow', fromId: command.fromId as never, toId: command.toId as never, props: command.bindingProps ?? {} } as never);
      return [];
    case 'delete_bindings':
      editor.deleteBindings(command.bindingIds as never);
      return [];
    case 'align_shapes':
      editor.alignShapes(command.shapeIds as never, command.direction as never);
      return [];
    case 'distribute_shapes':
      editor.distributeShapes(command.shapeIds as never, command.direction as never);
      return [];
    case 'pack_shapes':
      editor.packShapes(command.shapeIds as never, command.padding ?? 8);
      return [];
    case 'flip_shapes':
      editor.flipShapes(command.shapeIds as never, command.direction as never);
      return [];
    case 'rotate_shapes':
      editor.rotateShapesBy(command.shapeIds as never, command.angle ?? 0);
      return [];
    case 'resize_shape': {
      const sx = Number((command.props as Record<string, unknown>).x ?? 1);
      const sy = Number((command.props as Record<string, unknown>).y ?? 1);
      editor.resizeShape(command.shapeId as never, { x: sx, y: sy } as never);
      return [command.shapeId as TLShapeId];
    }
    case 'toggle_lock':
      editor.toggleLock(command.shapeIds as never);
      return [];
    case 'set_style':
      editor.setStyleForSelectedShapes(command.style as never, command.value as never);
      return [];
    case 'set_opacity':
      editor.setOpacityForSelectedShapes(command.value as number);
      return [];
    default:
      return [];
  }
}

/**
 * Apply a batch of commands atomically where possible.
 * Commands are validated first; invalid ones are reported and skipped.
 * Valid ones are applied inside one undo-safe batch.
 */
export function applyBridgeCommands(editor: Editor, commands: BridgeCommand[]): ActionOutcome {
  const appliedIds: string[] = [];
  const failed: ActionOutcome['failed'] = [];

  // Expand import_mermaid into its sub-commands before validation so the
  // whole diagram is validated and applied as one unit.
  const expanded: BridgeCommand[] = [];
  for (const command of commands) {
    if (command.type === 'import_mermaid' && command.text) {
      const generated = mermaidToCommands(command.text).map((cmd, index) => ({
        ...cmd,
        id: `${command.id}-m${index}`,
      })) as BridgeCommand[];
      if (generated.length === 0) {
        failed.push({ id: command.id, reason: 'import_mermaid: could not parse flowchart' });
      } else {
        expanded.push(...generated);
      }
    } else {
      expanded.push(command);
    }
  }
  commands = expanded;

  for (const command of commands) {
    const error = validateCommand(editor, command);
    if (error) failed.push({ id: command.id, reason: error });
    else appliedIds.push(command.id);
  }

  const validCommands = commands.filter((c) => appliedIds.includes(c.id));
  const affected: TLShapeId[] = [];

  if (validCommands.length > 0) {
    editor.run(() => {
      for (const command of validCommands) affected.push(...applyValidated(editor, command));
    });
  }

  return { appliedIds, failed, affectedShapeIds: affected };
}
