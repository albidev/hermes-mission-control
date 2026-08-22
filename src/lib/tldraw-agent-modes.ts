/**
 * Agent modes for the tldraw canvas.
 * Each mode shapes how the agent behaves on the board and how the UI presents it.
 */
export type AgentMode = 'draw' | 'review' | 'arrange' | 'explain' | '';

export const AGENT_MODES: Array<{ id: Exclude<AgentMode, ''>; label: string; hint: string }> = [
  { id: 'draw', label: 'Draw', hint: 'Agent creates and edits shapes freely' },
  { id: 'review', label: 'Review', hint: 'Agent inspects the board and reports issues' },
  { id: 'arrange', label: 'Arrange', hint: 'Agent tidies layout without changing content' },
  { id: 'explain', label: 'Explain', hint: 'Agent annotates the board with explanations' },
];

export function isAgentMode(value: unknown): value is AgentMode {
  return value === 'draw' || value === 'review' || value === 'arrange' || value === 'explain' || value === '';
}

/** System-prompt fragment the chat sends when a mode is active. */
export function modePromptFragment(mode: AgentMode): string {
  switch (mode) {
    case 'draw':
      return 'Agent mode DRAW: create new diagram elements. Use create_box/create_text/create_arrow with bindings; keep all arrows black; zoom_to_fit at the end.';
    case 'review':
      return 'Agent mode REVIEW: do not modify the board. Read the visual context, report overlaps, detached arrows, offscreen shapes, and suggest fixes as text.';
    case 'arrange':
      return 'Agent mode ARRANGE: reorganize existing shapes only (move_shape, align_shapes, distribute_shapes, pack_shapes). Never delete or add content.';
    case 'explain':
      return 'Agent mode EXPLAIN: annotate the board. Add text labels near clusters, keep arrows black, never delete existing shapes.';
    default:
      return '';
  }
}
