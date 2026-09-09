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

export type BotMcpTool = {
  name: string;
  description?: string;
};

export type BotMcpServer = {
  name: string;
  enabled: boolean;
  transport?: string;
  configured?: boolean;
  tools?: { include?: string[]; exclude?: string[] };
};

export type BotProfileDetails = {
  name: string;
  description: string;
  soul: string;
  model: { provider: string; default: string };
  skills: Array<{ name: string; enabled: boolean }>;
  toolsets: Array<{ name: string; label?: string; description?: string; enabled: boolean; tool_count?: number }>;
  toolsets_pinned?: boolean;
  mcp_servers: BotMcpServer[];
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
  enabledMcpServers?: string[];
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

export async function openBotTaskChat(profile: string, accessToken?: string): Promise<{ profile: string; openedId: string }> {
  const normalizedProfile = profile.trim();
  if (!normalizedProfile) throw new Error('A Bot profile is required to start a new chat.');
  let result: { session_id?: string; stored_session_id?: string };
  try {
    result = await requestBotRpc('session.create', {
      profile: normalizedProfile,
      hidden: false,
      follow_profile_config: true,
    }, accessToken);
  } catch (cause) {
    throw new Error('session.create failed: ' + (cause instanceof Error ? cause.message : String(cause)));
  }
  const openedId = result.stored_session_id || result.session_id || '';
  if (!openedId.trim()) throw new Error('session.create returned no session id.');
  return { profile: normalizedProfile, openedId };
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
  if (identifier.startsWith('local:')) {
    const response = await fetch('/api/local/profile/skills/install', {
      method: 'POST',
      headers: { Authorization: accessToken ? `Bearer ${accessToken}` : '', 'Content-Type': 'application/json' },
      body: JSON.stringify({ profile, identifier }),
    });
    if (!response.ok) {
      let detail = `Local skill installation failed (${response.status}).`;
      try {
        const payload = await response.json() as { detail?: string };
        if (payload.detail) detail = payload.detail;
      } catch {
        // Keep the HTTP status when the sidecar did not return JSON.
      }
      throw new Error(detail);
    }
    return;
  }
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

export async function configureBotMcpTools(
  sessionId: string,
  action: 'enable' | 'disable',
  names: string[],
  accessToken?: string,
): Promise<void> {
  if (!sessionId || names.length === 0) return;
  const result = await requestBotRpc<unknown>('tools.configure', {
    session_id: sessionId,
    action,
    names,
  }, accessToken);
  if (isRecord(result) && Array.isArray(result.unknown) && result.unknown.length > 0) {
    throw new Error(`Unknown MCP tools: ${result.unknown.join(', ')}`);
  }
  if (isRecord(result) && Array.isArray(result.missing_servers) && result.missing_servers.length > 0) {
    throw new Error(`MCP servers unavailable: ${result.missing_servers.join(', ')}`);
  }
}

export async function loadBotMcpTools(
  server: string,
  profile: string | undefined,
  accessToken?: string,
): Promise<{ ok: boolean; tools: BotMcpTool[]; error?: string }> {
  const value = await requestBotRpc<unknown>('mcp.servers.test', {
    name: server,
    ...(profile ? { profile } : {}),
  }, accessToken);
  if (!isRecord(value)) return { ok: false, tools: [], error: 'Invalid MCP response.' };
  return {
    ok: value.ok === true,
    tools: Array.isArray(value.tools)
      ? value.tools.filter(isRecord).map((tool) => ({
        name: typeof tool.name === 'string' ? tool.name : '',
        description: typeof tool.description === 'string' ? tool.description : undefined,
      })).filter((tool) => tool.name)
      : [],
    error: typeof value.error === 'string' ? value.error : undefined,
  };
}

export async function loadBotProfile(name: string, accessToken?: string): Promise<BotProfileDetails> {
  const [value, mcpValue, launchMcpValue] = await Promise.all([
    requestBotRpc<unknown>('profiles.describe', { name }, accessToken),
    requestBotRpc<unknown>('mcp.servers.list', { profile: name }, accessToken).catch(() => null),
    requestBotRpc<unknown>('mcp.servers.list', {}, accessToken).catch(() => null),
  ]);
  if (!isRecord(value)) throw new Error('Gateway returned an invalid Bot profile.');
  const describedMcp = Array.isArray(value.mcp_servers) ? value.mcp_servers.filter(isRecord) : [];
  const listedMcp = isRecord(mcpValue) && Array.isArray(mcpValue.servers) ? mcpValue.servers.filter(isRecord) : [];
  const launchMcp = isRecord(launchMcpValue) && Array.isArray(launchMcpValue.servers) ? launchMcpValue.servers.filter(isRecord) : [];
  const mcpByName = new Map<string, Record<string, unknown>>(
    listedMcp.filter((server) => typeof server.name === 'string')
      .map((server) => [server.name as string, server]),
  );
  const launchMcpByName = new Map<string, Record<string, unknown>>(
    launchMcp.filter((server) => typeof server.name === 'string')
      .map((server) => [server.name as string, server]),
  );
  const describedByName = new Map<string, Record<string, unknown>>(
    describedMcp.filter((server) => typeof server.name === 'string')
      .map((server) => [server.name as string, server]),
  );
  const mcpNames = [...new Set([
    ...describedByName.keys(),
    ...launchMcpByName.keys(),
  ])];
  const mcpServers = mcpNames.map((nameValue) => {
    const described = describedByName.get(nameValue);
    const listed = mcpByName.get(nameValue) ?? launchMcpByName.get(nameValue);
    const filters = listed && isRecord(listed.tools) ? listed.tools : undefined;
    return {
      name: nameValue,
      enabled: described?.enabled === true,
      configured: Boolean(described),
      transport: typeof described?.transport === 'string'
        ? described.transport
        : (typeof listed?.transport === 'string' ? listed.transport : undefined),
      tools: filters ? {
        include: Array.isArray(filters.include) ? filters.include.filter((tool): tool is string => typeof tool === 'string') : undefined,
        exclude: Array.isArray(filters.exclude) ? filters.exclude.filter((tool): tool is string => typeof tool === 'string') : undefined,
      } : undefined,
    };
  });
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
    mcp_servers: mcpServers,
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
    ...(input.enabledMcpServers ? { enabled_mcp_servers: input.enabledMcpServers } : {}),
    ...(input.disabledSkills ? { disabled_skills: input.disabledSkills } : {}),
    ui_meta: { mission_control: { bot: input.botRoster } },
  }, accessToken);
}
