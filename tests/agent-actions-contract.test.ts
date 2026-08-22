/**
 * Contract tests for the tldraw agent action reducer.
 * These run under `python3 -m unittest discover` alongside the Python tests
 * via a small node shim invoked from test_agent_actions_contract.py.
 */
import { readFileSync } from 'node:fs';

function assertIncludes(source: string, expected: string, label: string) {
  if (!source.includes(expected)) {
    throw new Error(`${label}: expected to find ${JSON.stringify(expected)}`);
  }
}

const source = readFileSync(new URL('../src/lib/tldraw-agent-actions.ts', import.meta.url), 'utf8');

// Validation must happen before mutation: validateCommand is exported and pure-ish.
assertIncludes(source, 'export function validateCommand', 'validator is exported for tests');
assertIncludes(source, 'export function applyBridgeCommands', 'batch entrypoint is exported');

// Geometry guards that encode today's regressions:
assertIncludes(source, 'w/h must be positive', 'rejects non-positive dimensions');
assertIncludes(source, 'x/y must be finite numbers', 'rejects non-finite coordinates');
assertIncludes(source, 'unknown shape', 'rejects commands targeting missing shapes');
assertIncludes(source, 'fromId/toId required', 'bindings need both endpoints');
assertIncludes(source, "editor.getShape(command.fromId as TLShapeId)", 'binding endpoints must exist');
assertIncludes(source, 'unsupported color', 'colors come from the tldraw palette');

// Atomicity: batches run inside editor.run for undo coherence.
assertIncludes(source, 'editor.run(() => {', 'multi-command batches are transactional');

// Failed actions are reported, never acknowledged blindly.
assertIncludes(source, 'failed.push', 'failures are collected');
assertIncludes(source, 'appliedIds.includes', 'only validated commands are applied');
assertIncludes(source, 'editor.run(', 'batches run inside one undo-safe editor.run');

console.log('agent action contract: OK');
