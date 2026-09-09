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
    await rpc.submit({ session_id: canonical.openedId, text: envelope.request });
    return { openedId: canonical.openedId, submitted: true };
  } catch (err) {
    dedupe.release(envelope.handoffId);
    throw err;
  }
}
