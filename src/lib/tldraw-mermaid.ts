/**
 * Mermaid flowchart → tldraw bridge commands.
 * Parses a subset of Mermaid flowchart syntax (nodes, edges, direction)
 * and emits create_box/create_arrow/create_binding commands the agent
 * reducer already knows how to apply. Layout is computed as a simple
 * layered grid (columns per rank, rows per layer) — deterministic and
 * good enough for chat-driven diagrams.
 */

export interface BridgeCommandLike {
  type: string;
  text?: string;
  x?: number;
  y?: number;
  w?: number;
  h?: number;
  fromId?: string;
  toId?: string;
  shapeType?: string;
  props?: Record<string, unknown>;
}

const BOX_W = 200;
const BOX_H = 90;
const GAP_X = 120;
const GAP_Y = 100;

interface ParsedNode {
  id: string;
  label: string;
}

interface ParsedEdge {
  from: string;
  to: string;
  label?: string;
}

/** Strip Mermaid decorations: `id[Label]`, `id(Label)`, `id{Label}` → id + Label. */
function parseNodeToken(token: string): ParsedNode | null {
  const match = token.trim().match(/^([\w-]+)[\[\(\{](.*)[\]\)\}]$/);
  if (!match) return null;
  const label = match[2].replace(/^["']|["']$/g, '').trim() || match[1];
  return { id: match[1], label };
}

export function parseMermaidFlowchart(source: string): { direction: 'LR' | 'TD'; nodes: ParsedNode[]; edges: ParsedEdge[] } | null {
  const lines = source.split('\n').map((line) => line.trim()).filter(Boolean);
  if (!lines.length) return null;

  let direction: 'LR' | 'TD' = 'TD';
  const nodes = new Map<string, ParsedNode>();
  const edges: ParsedEdge[] = [];

  for (const line of lines) {
    if (/^flowchart\b/i.test(line) || /^graph\b/i.test(line)) {
      if (/\bLR\b/i.test(line)) direction = 'LR';
      continue;
    }
    if (/^(%%|classDef|class |style |subgraph|end$)/i.test(line)) continue;

    // Edge line: A[Label] --> B[Label2] or A -->|text| B
    const edgeMatch = line.match(/^(.+?)\s*-{1,2}>?\|([^|]*)\|\s*(.+)$/) || line.match(/^(.+?)\s*-->\s*(.+)$/);
    if (edgeMatch && !line.startsWith('%%')) {
      const left = edgeMatch[1].trim();
      const rightRaw = (edgeMatch[3] ?? edgeMatch[2]).trim();
      // Right side may chain: B --> C --> D — take first hop only for v1.
      const right = rightRaw.split(/\s*-->\s*/)[0].trim();
      const fromNode = parseNodeToken(left);
      const toNode = parseNodeToken(right);
      const fromId = fromNode?.id ?? left;
      const toId = toNode?.id ?? right;
      if (fromNode) nodes.set(fromNode.id, fromNode);
      if (toNode) nodes.set(toNode.id, toNode);
      if (!nodes.has(fromId)) nodes.set(fromId, { id: fromId, label: fromId });
      if (!nodes.has(toId)) nodes.set(toId, { id: toId, label: toId });
      const edgeLabel = edgeMatch[2] && edgeMatch[3] !== undefined ? edgeMatch[2].trim() : undefined;
      edges.push({ from: fromId, to: toId, label: edgeLabel || undefined });
      continue;
    }

    // Standalone node declaration.
    const node = parseNodeToken(line);
    if (node) nodes.set(node.id, node);
  }

  if (nodes.size === 0) return null;
  return { direction, nodes: [...nodes.values()], edges };
}

/** Compute layered layout: roots at rank 0, then BFS along edges. */
function computeRanks(nodes: ParsedNode[], edges: ParsedEdge[]): Map<string, number> {
  const ranks = new Map<string, number>();
  const outgoing = new Map<string, string[]>();
  const incoming = new Set<string>();
  for (const edge of edges) {
    outgoing.set(edge.from, [...(outgoing.get(edge.from) ?? []), edge.to]);
    incoming.add(edge.to);
  }
  const queue: string[] = nodes.filter((n) => !incoming.has(n.id)).map((n) => n.id);
  if (queue.length === 0 && nodes.length > 0) queue.push(nodes[0].id);
  for (const id of queue) ranks.set(id, 0);
  while (queue.length) {
    const current = queue.shift()!;
    const rank = ranks.get(current) ?? 0;
    for (const next of outgoing.get(current) ?? []) {
      if (!ranks.has(next) || ranks.get(next)! < rank + 1) {
        ranks.set(next, rank + 1);
        queue.push(next);
      }
    }
  }
  for (const node of nodes) if (!ranks.has(node.id)) ranks.set(node.id, 0);
  return ranks;
}

/** Convert parsed Mermaid into bridge commands with a layered layout. */
export function mermaidToCommands(source: string): BridgeCommandLike[] {
  const parsed = parseMermaidFlowchart(source);
  if (!parsed) return [];

  const ranks = computeRanks(parsed.nodes, parsed.edges);
  const horizontal = parsed.direction === 'LR';

  // Group nodes by rank; position each layer along the flow axis.
  const layers = new Map<number, ParsedNode[]>();
  for (const node of parsed.nodes) {
    const rank = ranks.get(node.id) ?? 0;
    layers.set(rank, [...(layers.get(rank) ?? []), node]);
  }

  const commands: BridgeCommandLike[] = [];
  const positionById = new Map<string, { x: number; y: number }>();

  const sortedRanks = [...layers.keys()].sort((a, b) => a - b);
  sortedRanks.forEach((rank, layerIndex) => {
    const layerNodes = layers.get(rank)!;
    layerNodes.forEach((node, indexInLayer) => {
      const along = layerIndex * (horizontal ? BOX_W + GAP_X : BOX_H + GAP_Y);
      const across = indexInLayer * (horizontal ? BOX_H + GAP_Y : BOX_W + GAP_X);
      const x = horizontal ? 120 + along : 120 + across;
      const y = horizontal ? 120 + across : 120 + along;
      positionById.set(node.id, { x, y });
      commands.push({ type: 'create_box', text: node.label, x, y, w: BOX_W, h: BOX_H });
    });
  });

  for (const edge of parsed.edges) {
    const from = positionById.get(edge.from);
    const to = positionById.get(edge.to);
    if (!from || !to) continue;
    // Arrow spans between box centers; the binding command anchors it.
    const startX = from.x + BOX_W;
    const startY = from.y + BOX_H / 2;
    const endX = to.x;
    const endY = to.y + BOX_H / 2;
    const width = endX - startX;
    const height = endY - startY;
    commands.push({
      type: 'create_arrow',
      x: width >= 0 ? startX : endX,
      y: startY,
      w: Math.abs(width) || 1,
      h: height,
    });
    commands.push({ type: 'create_binding', fromId: `agent-box-${edge.from}`, toId: `agent-box-${edge.to}` });
    // Note: binding fromId/toId reference deterministic agent-box ids derived
    // from the mermaid node id, matching createShapeId(`agent-box-${command.id}`)
    // only if command ids equal node ids. The server assigns ids, so bindings
    // for mermaid imports are best-effort until the reducer supports named ids.
  }

  return commands;
}
