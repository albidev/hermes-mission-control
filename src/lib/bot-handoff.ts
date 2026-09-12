export type HandoffOrigin = {
  connectionId: string;
  profile: string;
  sessionId: string;
};

export type HandoffTarget = {
  profile: string;
  canonicalTitle: 'Bot Chat';
};

export type HandoffEnvelope = {
  handoffId: string;
  origin: HandoffOrigin;
  target: HandoffTarget;
  request: string;
  context: { mode: 'none'; messages: [] };
};

export type HandoffRpc = {
  resolveCanonical(profile: string): Promise<{ profile: string; registryId: string; openedId: string; created: boolean }>;
  submit(params: { session_id: string; text: string }): Promise<unknown>;
};

export type HandoffSubmitResult = {
  openedId: string;
  submitted: boolean;
};

/** Make the canonical Bot Chat treat every handoff as a fresh user request. */
export function formatHandoffPrompt(envelope: HandoffEnvelope): string {
  return [
    '[MISSION CONTROL HANDOFF — NEW REQUEST]',
    `handoff_id: ${envelope.handoffId}`,
    'Answer the CURRENT REQUEST directly.',
    'Do not say you already answered above and do not defer to a previous answer.',
    'Use previous conversation only as background context; it is not the answer to this request.',
    'Use your normal Hermes reasoning and tools. For specific/project/factual requests, use BDH as required by your SOUL.',
    '',
    'CURRENT REQUEST:',
    envelope.request,
  ].join('\n');
}

export function createHandoffEnvelope(
  origin: HandoffOrigin,
  target: HandoffTarget,
  request: string,
): HandoffEnvelope {
  return {
    handoffId: `mc-handoff-${crypto.randomUUID()}`,
    origin,
    target: { profile: target.profile, canonicalTitle: 'Bot Chat' },
    request,
    context: { mode: 'none', messages: [] },
  };
}

export function createHandoffDedupe() {
  const claimed = new Set<string>();
  return {
    tryClaim(handoffId: string): boolean {
      if (claimed.has(handoffId)) return false;
      claimed.add(handoffId);
      return true;
    },
    release(handoffId: string): void {
      claimed.delete(handoffId);
    },
  };
}

export async function submitHandoff(
  rpc: HandoffRpc,
  envelope: HandoffEnvelope,
  dedupe: ReturnType<typeof createHandoffDedupe> = createHandoffDedupe(),
): Promise<HandoffSubmitResult> {
  if (!dedupe.tryClaim(envelope.handoffId)) {
    return { openedId: '', submitted: false };
  }
  try {
    const canonical = await rpc.resolveCanonical(envelope.target.profile);
    await rpc.submit({ session_id: canonical.openedId, text: formatHandoffPrompt(envelope) });
    return { openedId: canonical.openedId, submitted: true };
  } catch (err) {
    dedupe.release(envelope.handoffId);
    throw err;
  }
}
