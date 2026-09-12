export type BotChatIdentity = { profile: string; title: string };
export type BotChatResult = {
  profile: string;
  registryId: string;
  openedId: string;
  created: boolean;
};

export type SessionRpc = {
  list(params: Record<string, unknown>): Promise<{ sessions?: Array<{ id?: string; resolved_id?: string; title?: string; root_title?: string }> }>;
  create(params: Record<string, unknown>): Promise<{ session_id?: string; stored_session_id?: string }>;
  title(params: Record<string, unknown>): Promise<{ title?: string; pending?: boolean }>;
};

export function createBotChatResolver(rpc: SessionRpc) {
  const inFlight = new Map<string, Promise<BotChatResult>>();

  function key(profile: string) { return profile; }

  async function resolve(profile: string, knownCanonicalId?: string): Promise<BotChatResult> {
    const k = key(profile);
    if (inFlight.has(k)) return inFlight.get(k)!;
    const p = doResolve(profile, knownCanonicalId).finally(() => { inFlight.delete(k); });
    inFlight.set(k, p);
    return p;
  }

  async function doResolve(profile: string, knownCanonicalId?: string): Promise<BotChatResult> {
    const isCanonical = (s: { root_title?: string; title?: string }) => {
      const root = String(s.root_title || '').trim();
      const title = String(s.title || '').trim();
      return root === 'Bot Chat' || (!root && title === 'Bot Chat');
    };
    let listResp: { sessions?: Array<{ id?: string; resolved_id?: string; title?: string; root_title?: string }> };
    try {
      listResp = await rpc.list({ profile, title: 'Bot Chat', include_hidden: true, limit: 200 });
    } catch (e) {
      throw new Error('session.list failed: ' + (e instanceof Error ? e.message : String(e)));
    }
    const sessions = (listResp && Array.isArray(listResp.sessions)) ? listResp.sessions : [];
    const exact = sessions.find(isCanonical);

    if (exact) {
      const id = exact.id || '';
      if (!id) throw new Error('fail-closed: missing registry id');
      return { profile, registryId: id, openedId: exact.resolved_id || id, created: false };
    }

    if (sessions.length === 0 && knownCanonicalId) {
      throw new Error('fail-closed: knownCanonicalId ' + knownCanonicalId + ' but list empty');
    }

    let createResp: { session_id?: string; stored_session_id?: string };
    try {
      createResp = await rpc.create({ profile, title: 'Bot Chat', hidden: true, follow_profile_config: true });
    } catch (e) {
      throw new Error('session.create failed: ' + (e instanceof Error ? e.message : String(e)));
    }
    const runtimeId = createResp.session_id || '';

    try {
      await rpc.title({ session_id: runtimeId, title: 'Bot Chat', profile });
    } catch (e) {
      const msg = (e instanceof Error ? e.message : String(e));
      if (/already in use/i.test(msg)) {
        let relist: { sessions?: Array<{ id?: string; resolved_id?: string; title?: string; root_title?: string }> };
        try {
          relist = await rpc.list({ profile, title: 'Bot Chat', include_hidden: true, limit: 200 });
        } catch (e2) {
          throw new Error('re-list after already-in-use failed: ' + (e2 instanceof Error ? e2.message : String(e2)));
        }
        const relSessions = (relist && Array.isArray(relist.sessions)) ? relist.sessions : [];
        const winner = relSessions.find(isCanonical);
        if (!winner) throw new Error('already-in-use adoption found no winner');
        const winId = winner.id || '';
        if (!winId) throw new Error('fail-closed: missing registry id');
        return { profile, registryId: winId, openedId: winner.resolved_id || winId, created: false };
      }
      throw new Error('session.title failed: ' + msg);
    }

    let relist2: { sessions?: Array<{ id?: string; resolved_id?: string; title?: string; root_title?: string }> };
    try {
      relist2 = await rpc.list({ profile, title: 'Bot Chat', include_hidden: true, limit: 200 });
    } catch (e) {
      throw new Error('post-title re-list failed: ' + (e instanceof Error ? e.message : String(e)));
    }
    const rel2 = (relist2 && Array.isArray(relist2.sessions)) ? relist2.sessions : [];
    const adopted = rel2.find(isCanonical);
    if (!adopted) throw new Error('missing post-title registry row');
    const adoptedId = adopted.id || '';
    if (!adoptedId) throw new Error('fail-closed: missing registry id');
    return {
      profile,
      registryId: adoptedId,
      openedId: adopted.resolved_id || adoptedId,
      created: true,
    };
  }

  return { resolve };
}
