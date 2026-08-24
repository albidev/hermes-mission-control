import type { GatewayInteractionRequest } from './chat-protocol';

export function interactionTitle(interaction: GatewayInteractionRequest): string {
  if (interaction.kind === 'approval') return 'Hermes needs permission';
  if (interaction.kind === 'clarify') return 'Hermes needs your answer';
  if (interaction.kind === 'sudo') return 'Hermes needs elevated access';
  if (interaction.kind === 'terminal_read') return 'Hermes needs terminal output';
  return 'Hermes needs a secret';
}
