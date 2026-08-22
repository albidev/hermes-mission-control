/**
 * Contract tests for the Mermaid → bridge-commands converter.
 * Executed via node --experimental-strip-types from the Python runner.
 */
import { parseMermaidFlowchart, mermaidToCommands } from '../src/lib/tldraw-mermaid.ts';

function assert(condition: unknown, label: string): void {
  if (!condition) throw new Error(`FAIL: ${label}`);
  console.log(`ok - ${label}`);
}

const source = `
flowchart LR
  A[Chat] --> B[Hermes]
  B --> C{Memory}
  C --> D[Store]
`;

const parsed = parseMermaidFlowchart(source);
assert(parsed !== null, 'parses a simple flowchart');
assert(parsed!.direction === 'LR', 'detects LR direction');
assert(parsed!.nodes.length === 4, 'finds all four nodes');
assert(parsed!.nodes.some((n) => n.id === 'A' && n.label === 'Chat'), 'node labels are extracted');
assert(parsed!.edges.length === 3, 'finds all three edges');

const commands = mermaidToCommands(source);
const boxes = commands.filter((c) => c.type === 'create_box');
const arrows = commands.filter((c) => c.type === 'create_arrow');
assert(boxes.length === 4, 'emits one box per node');
assert(arrows.length === 3, 'emits one arrow per edge');
assert(boxes.every((b) => (b.w ?? 0) > 0 && (b.h ?? 0) > 0), 'all boxes have positive dimensions');
assert(boxes.every((b) => Number.isFinite(b.x) && Number.isFinite(b.y)), 'all coordinates are finite');

// Layout sanity: Chat is a root (rank 0), Store is downstream.
const chatBox = boxes.find((b) => b.text === 'Chat')!;
const storeBox = boxes.find((b) => b.text === 'Store')!;
assert(chatBox.x! < storeBox.x!, 'LR layout flows left to right by rank');

// Degenerate input is rejected, not half-parsed.
assert(parseMermaidFlowchart('hello world') === null, 'non-flowchart input returns null');
assert(mermaidToCommands('').length === 0, 'empty input emits no commands');

console.log('mermaid converter contract: OK');
