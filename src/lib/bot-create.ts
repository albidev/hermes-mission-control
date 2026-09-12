import type { CreateBotProfileInput } from './bot-gateway';

export interface BotCreateDraftInput {
  name: string;
  description: string;
  soul: string;
  model: string;
  provider: string;
  noSkills: boolean;
  botRoster: boolean;
  cloneFrom: string | null;
}

export function buildBotCreateInput(draft: BotCreateDraftInput): CreateBotProfileInput {
  return {
    name: draft.name,
    description: draft.description,
    soul: draft.soul,
    model: draft.model,
    provider: draft.provider,
    noSkills: draft.noSkills,
    botRoster: draft.botRoster,
    cloneFrom: draft.cloneFrom?.trim() || undefined,
  };
}
