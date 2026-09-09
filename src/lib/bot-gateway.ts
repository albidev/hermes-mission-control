import {
  createRpcRequest,
  getRpcErrorMessage,
  isResponseFor,
  parseGatewayFrame,
} from './chat-protocol';
import { getWebSocketUrl, mintWsCredential, RPC_TIMEOUT_MS } from './chat-transport';
import { createBotChatResolver, type BotChatResult } from './bot-chat-routing';

export type BotCanonicalSession = {
  id?: string;
  resolved_id?: string;
  title?: string;
  message_count?: number;
  last_active?: number;
};

export type BotProfileSummary = {
  name: string;
  path?: string;
  is_default?: boolean;
  model?: string;
  provider?: string;
  description?: string;
  display_name?: string;
  skill_count?: number;
  canonical_session?: BotCanonicalSession | null;
  ui_meta?: Record<string, unknown>;
  is_bot?: boolean;
};

export type BotProfileDetails = {
  name: string;
  description: string;
  soul: string;
  model: { provider: string; default: string };
  skills: Array<{ name: string; enabled: boolean }>;
  toolsets: Array<{ name: string; label?: string; description?: string; enabled: boolean; tool_count?: number }>;
  toolsets_pinned?: boolean;
  mcp_servers: Array<{ name: string; enabled: boolean; transport?: string }>;
};

export type BotProfilesPayload = {
  profiles: BotProfileSummary[];
  bot_mode_protocol?: boolean;
};

export type BotModelProviderOption = {
  slug: string;
  name: string;
  models: string[];
  is_current?: boolean;
};

export type CreateBotProfileInput = {
  name: string;
  description?: string;
  soul?: string;
  model?: string;
  provider?: string;
  noSkills?: boolean;
  shareAuth?: boolean;
  botRoster?: boolean;
};

export type ConfigureBotProfileInput = {
  name: string;
  description: string;
  soul: string;
  model?: string;
  provider?: string;
  enabledToolsets?: string[];
  disabledSkills?: string[];
  botRoster: boolean;
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

function rpcError(value: unknown): Error {
  if (isRecord(value) && typeof value.message === 'string') return new Error(value.message);
  return new Error(getRpcErrorMessage(value));
}

function normalizeProfiles(value: unknown): BotProfilesPayload {
  if (!isRecord(value) || !Array.isArray(value.profiles)) return { profiles: [] };
  return {
    profiles: value.profiles.filter(isRecord).map((profile) => ({
      name: typeof profile.name === 'string' ? profile.name : '',
      path: typeof profile.path === 'string' ? profile.path : undefined,
      is_default: profile.is_default === true,
      model: typeof profile.model === 'string' ? profile.model : undefined,
      provider: typeof profile.provider === 'string' ? profile.provider : undefined,
      description: typeof profile.description === 'string' ? profile.description : '',
      display_name: typeof profile.display_name === 'string' ? profile.display_name : undefined,
      skill_count: typeof profile.skill_count === 'number' ? profile.skill_count : 0,
      canonical_session: isRecord(profile.canonical_session)
        ? profile.canonical_session as BotCanonicalSession
        : null,
      ui_meta: isRecord(profile.ui_meta) ? profile.ui_meta : undefined,
      is_bot: isRecord(profile.ui_meta)
        && isRecord(profile.ui_meta.mission_control)
        && profile.ui_meta.mission_control.bot === true,
    })).filter((profile) => profile.name),
    bot_mode_protocol: value.bot_mode_protocol === true,
  };
}

async function requestBotRpc<T>(method: string, params: Record<string, unknown>, accessToken?: string): Promise<T> {
  const credential = await mintWsCredential(accessToken ?? '');
  const socket = new WebSocket(getWebSocketUrl(credential));
  const requestId = `mc-bots-${Date.now()}-${Math.random().toString(36).slice(2)}`;

  return await new Promise<T>((resolve, reject) => {
    let settled = false;
    const timeout = window.setTimeout(() => {
      if (settled) return;
      settled = true;
      socket.close();
      reject(new Error(`Gateway request timed out: ${method}`));
    }, RPC_TIMEOUT_MS);

    const finish = (callback: () => void) => {
      if (settled) return;
      settled = true;
      window.clearTimeout(timeout);
      socket.close();
      callback();
    };

    socket.onerror = () => finish(() => reject(new Error(`Could not connect to the gateway for ${method}.`)));
    socket.onclose = () => {
      if (!settled) finish(() => reject(new Error(`Gateway connection closed during ${method}.`)));
    };
    socket.onmessage = (event) => {
      const frame = parseGatewayFrame(event.data);
      if (frame.kind !== 'response' || !isResponseFor(frame.response, requestId)) return;
      if (frame.response.error) {
        finish(() => reject(rpcError(frame.response.error)));
        return;
      }
      finish(() => resolve(frame.response.result as T));
    };
    socket.onopen = () => {
      socket.send(JSON.stringify(createRpcRequest(requestId, method, params)));
    };
  });
}

export async function openBotCanonicalChat(
  profile: string,
  knownCanonicalId?: string | null,
  accessToken?: string,
): Promise<BotChatResult> {
  const normalizedProfile = profile.trim();
  if (!normalizedProfile) throw new Error('A Bot profile is required to open Bot Chat.');
  const resolver = createBotChatResolver({
    list: (params) => requestBotRpc('session.list', params, accessToken),
    create: (params) => requestBotRpc('session.create', params, accessToken),
    title: (params) => requestBotRpc('session.title', params, accessToken),
  });
  return resolver.resolve(normalizedProfile, knownCanonicalId?.trim() || undefined);
}

export async function submitBotPrompt(
  profile: string,
  text: string,
  accessToken?: string,
): Promise<{ openedId: string; submitted: boolean }> {
  const canonical = await openBotCanonicalChat(profile, undefined, accessToken);
  // The registry row may point to a closed runtime session (e.g. the drawer
  // closed it on unmount). Resume the canonical session so the runtime id is
  // live before submitting, exactly like the Desktop canonical contract.
  // Every RPC here MUST carry the explicit profile: the gateway is
  // profile-scoped and would otherwise look in the default profile's DB.
  let runtimeId = canonical.openedId;
  try {
    const resumed = await requestBotRpc<unknown>('session.resume', {
      session_id: canonical.registryId,
      profile,
      cols: 80,
      eager_build: true,
      source: 'mission-control',
    }, accessToken);
    if (isRecord(resumed) && typeof resumed.session_id === 'string' && resumed.session_id.trim()) {
      runtimeId = resumed.session_id.trim();
    }
  } catch (err) {
    // Resume failure is fatal here: prompt.submit resolves sessions only from
    // the gateway's in-memory registry, so a dead runtime id can never be
    // submitted. Surface the real error instead of masking it.
    throw new Error(`Could not resume the canonical Bot Chat for ${profile}: ${err instanceof Error ? err.message : String(err)}`);
  }
  await requestBotRpc('prompt.submit', { session_id: runtimeId, text, profile }, accessToken);
  return { openedId: runtimeId, submitted: true };
}

export async function botEventsSince(
  sessionId: string,
  lastSeen: number,
  accessToken?: string,
  profile?: string,
): Promise<{ events?: Array<{ type: string; seq?: number; payload?: Record<string, unknown> }>; truncated?: boolean; epoch?: string | null }> {
  return requestBotRpc('session.events.since', {
    session_id: sessionId,
    last_seen: lastSeen,
    ...(profile ? { profile } : {}),
  }, accessToken);
}

export async function loadBotProfiles(accessToken?: string): Promise<BotProfilesPayload> {
  return normalizeProfiles(await requestBotRpc<unknown>('profiles.list', { include_sessions: true }, accessToken));
}

export async function loadBotModelOptions(accessToken?: string): Promise<BotModelProviderOption[]> {
  const value = await requestBotRpc<unknown>('model.options', {
    explicit_only: false,
    include_unconfigured: false,
    refresh: false,
  }, accessToken);
  if (!isRecord(value) || !Array.isArray(value.providers)) return [];
  return value.providers.filter(isRecord).map((provider) => ({
    slug: typeof provider.slug === 'string' ? provider.slug : '',
    name: typeof provider.name === 'string' ? provider.name : String(provider.slug || ''),
    models: Array.isArray(provider.models)
      ? provider.models.filter((model): model is string => typeof model === 'string' && model.trim().length > 0)
      : [],
    is_current: provider.is_current === true,
  })).filter((provider) => provider.slug);
}

export async function installBotSkill(profile: string, identifier: string, accessToken?: string): Promise<void> {
  await requestBotRpc('skills.manage', {
    action: 'install',
    query: identifier,
    profile,
  }, accessToken);
}

export async function configureBotToolsets(name: string, enabledToolsets: string[], accessToken?: string): Promise<void> {
  await requestBotRpc('profiles.configure', {
    name,
    enabled_toolsets: enabledToolsets,
  }, accessToken);
}

export async function loadBotProfile(name: string, accessToken?: string): Promise<BotProfileDetails> {
  const value = await requestBotRpc<unknown>('profiles.describe', { name }, accessToken);
  if (!isRecord(value)) throw new Error('Gateway returned an invalid Bot profile.');
  return {
    name: typeof value.name === 'string' ? value.name : name,
    description: typeof value.description === 'string' ? value.description : '',
    soul: typeof value.soul === 'string' ? value.soul : '',
    model: isRecord(value.model)
      ? {
        provider: typeof value.model.provider === 'string' ? value.model.provider : '',
        default: typeof value.model.default === 'string' ? value.model.default : '',
      }
      : { provider: '', default: '' },
    skills: Array.isArray(value.skills)
      ? value.skills.filter(isRecord).map((skill) => ({
        name: typeof skill.name === 'string' ? skill.name : '',
        enabled: skill.enabled !== false,
      })).filter((skill) => skill.name)
      : [],
    toolsets: Array.isArray(value.toolsets)
      ? value.toolsets.filter(isRecord).map((toolset) => ({
        name: typeof toolset.name === 'string' ? toolset.name : '',
        label: typeof toolset.label === 'string' ? toolset.label : undefined,
        description: typeof toolset.description === 'string' ? toolset.description : undefined,
        enabled: toolset.enabled === true,
        tool_count: typeof toolset.tool_count === 'number' ? toolset.tool_count : undefined,
      })).filter((toolset) => toolset.name)
      : [],
    toolsets_pinned: value.toolsets_pinned === true,
    mcp_servers: Array.isArray(value.mcp_servers)
      ? value.mcp_servers.filter(isRecord).map((server) => ({
        name: typeof server.name === 'string' ? server.name : '',
        enabled: server.enabled === true,
        transport: typeof server.transport === 'string' ? server.transport : undefined,
      })).filter((server) => server.name)
      : [],
  };
}

export async function createBotProfile(input: CreateBotProfileInput, accessToken?: string): Promise<void> {
  await requestBotRpc('profiles.create', {
    name: input.name.trim(),
    description: input.description?.trim() || undefined,
    soul: input.soul?.trim() || undefined,
    model: input.model?.trim() || undefined,
    provider: input.provider?.trim() || undefined,
    no_skills: input.noSkills !== false,
    share_auth: input.shareAuth !== false,
    mirror_credentials: true,
    ui_meta: { mission_control: { bot: input.botRoster !== false } },
  }, accessToken);
}

export async function configureBotProfile(input: ConfigureBotProfileInput, accessToken?: string): Promise<void> {
  await requestBotRpc('profiles.configure', {
    name: input.name,
    description: input.description,
    soul: input.soul,
    model: input.model?.trim() || undefined,
    provider: input.provider?.trim() || undefined,
    ...(input.enabledToolsets ? { enabled_toolsets: input.enabledToolsets } : {}),
    ...(input.disabledSkills ? { disabled_skills: input.disabledSkills } : {}),
    ui_meta: { mission_control: { bot: input.botRoster } },
  }, accessToken);
}
