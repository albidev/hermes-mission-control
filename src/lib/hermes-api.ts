export type MissionControlMachineStatus = {
  health: 'healthy' | 'degraded' | 'critical' | 'offline';
  source?: 'local-psutil' | 'core-api' | 'fallback';
  host: string;
  platform: string;
  platformVersion: string;
  cpuCores: number;
  cpuUsagePercent: number | null;
  ramUsage: {
    usedPercent: number | null;
    usedGb: number | null;
    availableGb: number | null;
    totalGb: number | null;
  };
  loadAverage: {
    one: number | null;
    five: number | null;
    fifteen: number | null;
    perCore: number | null;
  };
  diskUsage: {
    path: string;
    usedPercent: number | null;
    freeGb: number;
    totalGb: number;
  };
  processMemoryMb: number | null;
  thermal: {
    fanRpm: number | null;
    fanCount: number | null;
    thermalPressure: number | null;
    thermalLevel: string | null;
    levelSource: string | null;
    source: string | null;
    error: string | null;
  };
  summary: string;
};

export type MissionControlSessionItem = {
  id: string;
  source: string;
  model: string;
  title: string;
  startedAt: number;
  endedAt: number | null;
  status: MissionControlAgentSessionStatus;
  messageCount: number;
  preview: string;
  lastActive: number;
};

export type MissionControlSessionsSnapshot = {
  totalSessions: number;
  totalMessages: number;
  activeAgents: number;
  toolCallsToday: number;
  items: MissionControlSessionItem[];
};

export type MissionControlAgentTraceEvent = {
  id: string;
  type: string;
  label: string;
  detail: string;
  tone: 'good' | 'warn' | 'bad';
  status?: string;
  timestamp: number;
  sessionId: string;
  turnId: number;
  parentEventId?: string;
  toolName?: string;
  callId?: string;
  skillName?: string;
  request?: string;
  response?: string;
};

export type MissionControlAgentTraceNode = {
  id: string;
  kind: string;
  label: string;
  status: string;
  turnId: number;
  timestamp: number;
};

export type MissionControlAgentTraceEdge = {
  from: string;
  to: string;
  kind: string;
};

export type MissionControlAgentTraceSnapshot = {
  success: boolean;
  schemaVersion?: string;
  available?: boolean;
  mode: 'live' | 'post';
  traceMode?: MissionControlTraceMode;
  session: MissionControlSessionItem | null;
  events: MissionControlAgentTraceEvent[];
  nodes: MissionControlAgentTraceNode[];
  edges: MissionControlAgentTraceEdge[];
  stats: {
    turns: number;
    toolCalls: number;
    skills: number;
    thoughts: number;
    errors: number;
    durationSeconds: number;
  };
  warnings?: string[];
};

export type MissionControlCronExecution = {
  status: string | null;
  startedAt: string | null;
  finishedAt: string | null;
  error: string | null;
};

export type MissionControlCronJob = {
  id: string;
  label: string;
  enabled: boolean;
  state: string;
  scheduleDisplay: string;
  scheduleKind?: string;
  scheduleExpr?: string | null;
  nextRunAt: string | null;
  lastRunAt: string | null;
  createdAt?: string | null;
  pausedReason: string | null;
  lastStatus: string | null;
  lastError: string | null;
  prompt: string;
  model: string;
  provider: string | null;
  skill: string | null;
  skills: string[];
  repeat?: { times?: number | null; completed?: number | null } | number | null;
  deliver?: string | null;
  script?: string | null;
  noAgent?: boolean;
  contextFrom?: string[];
  enabledToolsets?: string[];
  workdir?: string | null;
  monitorScript?: string | null;
  monitorUrl?: string | null;
  attachToSession?: boolean | null;
  reasoningEffort?: string | null;
  lastOutput?: string | null;
  latestExecution?: MissionControlCronExecution | null;
};

export type MissionControlCronSnapshot = {
  queuedJobs: number;
  items: MissionControlCronJob[];
};

export type MissionControlAlert = {
  id: string;
  category: 'gateway' | 'sessions' | 'cron' | 'knowledge' | 'machine';
  tone: 'good' | 'warn' | 'bad';
  title: string;
  detail: string;
  endpoint?: string;
};

export type MissionControlAlertsSnapshot = {
  items: MissionControlAlert[];
};

export type MissionControlKnowledgeItem = {
  id: string;
  title: string;
  path: string;
  sourcePath?: string;
  updatedAt: string | null;
  excerpt: string;
  highlights: string[];
  contentPreview?: string;
};

export type MissionControlKnowledgeSection = {
  id: string;
  title: string;
  items: MissionControlKnowledgeItem[];
};

export type MissionControlKnowledgeSnapshot = {
  available: boolean;
  vaultPath: string;
  title: string;
  path: string;
  updatedAt: string | null;
  excerpt: string;
  highlights: string[];
  primary: MissionControlKnowledgeItem;
  items: MissionControlKnowledgeItem[];
  sections: MissionControlKnowledgeSection[];
};

export type MissionControlKnowledgeFilePayload = {
  success: boolean;
  title: string;
  path: string;
  sourcePath: string;
  updatedAt: string | null;
  excerpt: string;
  highlights: string[];
  content: string;
  contentLength: number;
};

export type MissionControlToolsetItem = {
  name: string;
  description: string;
  directTools: string[];
  includes: string[];
  resolvedTools: string[];
  toolCount: number;
  isComposite: boolean;
  available: boolean;
  requirements: string[];
};

export type MissionControlToolCatalogItem = {
  name: string;
  toolset: string;
  available: boolean;
  sourcePath?: string | null;
};

export type MissionControlToolsSnapshot = {
  available: boolean;
  count: number;
  toolCount: number;
  toolsets: MissionControlToolsetItem[];
  availableToolsets: MissionControlToolsetItem[];
  toolCatalog: MissionControlToolCatalogItem[];
  resolvedTools: string[];
};

export type MissionControlSkillItem = {
  id: string;
  name: string;
  description: string;
  enabled: boolean;
  model: string;
  tags: string[];
  category?: string;
  filePath?: string;
};

export type MissionControlSkillCategory = {
  name: string;
  description?: string;
  count: number;
  skills: string[];
};

export type MissionControlSkillsSnapshot = {
  available: boolean;
  count: number;
  hint: string | null;
  skills: MissionControlSkillItem[];
  categories: MissionControlSkillCategory[];
};

export type MissionControlSkillCatalogItem = {
  id: string;
  name: string;
  description: string;
  source: string;
  identifier: string;
  trustLevel: 'builtin' | 'trusted' | 'community' | string;
  repo?: string | null;
  path?: string | null;
  tags: string[];
  installed: boolean;
};

export type MissionControlSkillsCatalogSnapshot = {
  available: boolean;
  count: number;
  hint: string | null;
  skills: MissionControlSkillCatalogItem[];
  sources: Record<string, number>;
  timedOut: string[];
};

export type MissionControlSkillFile = {
  name: string;
  path: string;
  size: number;
  content: string;
};

export type MissionControlSkillFilesPayload = {
  skill: string;
  path: string;
  files: MissionControlSkillFile[];
};

export type MissionControlConfigSnapshot = {
  available: boolean;
  path: string;
  exists: boolean;
  content: string;
  hash: string;
  updatedAt: string | null;
  config: Record<string, unknown>;
};

export type MissionControlLogEntry = {
  lineNumber: number;
  level: 'info' | 'warn' | 'error';
  text: string;
};

export type MissionControlLogFile = {
  name: string;
  path: string;
  updatedAt: string | null;
  sizeBytes: number;
  entryCount: number;
  entries: MissionControlLogEntry[];
};

export type MissionControlLogsSnapshot = {
  available: boolean;
  path: string;
  fileCount: number;
  totalEntries: number;
  generatedAt: string | null;
  files: MissionControlLogFile[];
};

export type MissionControlSnapshot = {
  backendHealth: 'healthy' | 'degraded' | 'offline';
  activeModel: string;
  fallbackModel: string;
  gatewayStatus: string;
  activeAgents: number;
  candidatesEnabled: boolean;
  queuedJobs: number;
  toolCallsToday: number;
  recentSignals: Array<{
    label: string;
    detail: string;
    tone: 'good' | 'warn' | 'bad';
  }>;
  knowledgeSharing: MissionControlKnowledgeSnapshot;
  machine: MissionControlMachineStatus;
  sessions: MissionControlSessionsSnapshot;
  cron: MissionControlCronSnapshot;
  alerts: MissionControlAlertsSnapshot;
};

export type MissionControlCapabilities = {
  schemaVersion: string;
  trace: {
    stream: boolean;
    compact: boolean;
    namedSseTraceEvent: boolean;
  };
  traceModes: MissionControlTraceMode[];
};

export type MissionControlTraceMode = 'native' | 'transcript' | 'unavailable';
export type MissionControlAgentSessionStatus = 'live' | 'idle' | 'ended';

export type MissionControlSessionPreviewMessage = {
  role: 'user' | 'assistant';
  text: string;
  timestamp: number | null;
};

export type MissionControlAgentSessionItem = {
  sessionId: string;
  agentId: string;
  title: string;
  source: string;
  platform: string;
  chatType: string;
  displayName: string;
  model: string;
  startedAt: number | null;
  lastActiveAt: number | null;
  endedAt: number | null;
  status: MissionControlAgentSessionStatus;
  category: 'conversation' | 'automation' | 'system' | 'unknown';
  originLabel: string;
  isResumable: boolean;
  messageCount: number;
  traceMode: MissionControlTraceMode;
  preview: string;
  recentMessages: MissionControlSessionPreviewMessage[];
  inputTokens: number;
  outputTokens: number;
  cacheReadTokens: number;
  cacheWriteTokens: number;
  reasoningTokens: number;
  estimatedCostUsd: number;
  costStatus: string;
};

export type MissionControlAgentsSessionsSnapshot = {
  success: boolean;
  schemaVersion: string;
  available: boolean;
  items: MissionControlAgentSessionItem[];
  stats: {
    totalSessions: number;
    liveSessions: number;
    activeAgents: number;
  };
  facets: {
    status: Record<MissionControlAgentSessionStatus, number>;
    category: Record<'conversation' | 'automation' | 'system' | 'unknown', number>;
    origin: Record<string, number>;
    model: Record<string, number>;
  };
  tabCounts: Record<'all' | 'live' | 'conversation' | 'automation' | 'system', number>;
  pagination: {
    total: number;
    offset: number;
    limit: number;
    hasMore: boolean;
  };
};

export const MISSION_CONTROL_TOKEN_STORAGE_KEY = 'mission-control-token';

export class MissionControlAuthError extends Error {
  constructor(message = 'Mission Control requires an access token.') {
    super(message);
    this.name = 'MissionControlAuthError';
  }
}

const fallbackMachine: MissionControlMachineStatus = {
  health: 'degraded',
  source: 'fallback',
  host: 'local-machine',
  platform: 'unknown',
  platformVersion: '',
  cpuCores: 1,
  cpuUsagePercent: null,
  ramUsage: {
    usedPercent: null,
    usedGb: null,
    availableGb: null,
    totalGb: null,
  },
  loadAverage: {
    one: null,
    five: null,
    fifteen: null,
    perCore: null,
  },
  diskUsage: {
    path: '/',
    usedPercent: null,
    freeGb: 0,
    totalGb: 0,
  },
  processMemoryMb: null,
  thermal: {
    fanRpm: null,
    fanCount: null,
    thermalPressure: null,
    thermalLevel: null,
    levelSource: null,
    source: null,
    error: null,
  },
  summary: 'System metrics unavailable',
};

const fallbackSessions: MissionControlSessionsSnapshot = {
  totalSessions: 0,
  totalMessages: 0,
  activeAgents: 0,
  toolCallsToday: 0,
  items: [],
};

const fallbackCron: MissionControlCronSnapshot = {
  queuedJobs: 0,
  items: [],
};

const fallbackAlerts: MissionControlAlertsSnapshot = {
  items: [
    {
      id: 'fallback-gateway',
      category: 'gateway',
      tone: 'warn',
      title: 'Dashboard is using fallback data',
      detail: 'Real backend endpoints were not reachable, so the cockpit is showing cached defaults.',
      endpoint: '/api/status',
    },
  ],
};

const fallbackKnowledge: MissionControlKnowledgeSnapshot = {
  available: false,
  // Platform-neutral placeholder: the real vault path always comes from the
  // telemetry server (MISSION_CONTROL_VAULT_PATH or the platform default).
  // Do not fabricate a macOS-style path here.
  vaultPath: '~/wiki',
  title: 'Knowledge Sharing',
  path: 'Knowledge Sharing.md',
  updatedAt: null,
  excerpt: 'Create ~/.hermes/SOUL.md, ~/.hermes/USER.md, ~/.hermes/AGENTS.md and vault notes to surface them here.',
  highlights: [],
  primary: {
    id: 'knowledge-sharing',
    title: 'Knowledge Sharing',
    path: 'Knowledge Sharing.md',
    updatedAt: null,
    excerpt: 'Create ~/.hermes/SOUL.md, ~/.hermes/USER.md, ~/.hermes/AGENTS.md and vault notes to surface them here.',
    highlights: [],
  },
  items: [],
  sections: [
    {
      id: 'soul',
      title: '~/.hermes/SOUL.md',
      items: [],
    },
    {
      id: 'user',
      title: '~/.hermes/USER.md',
      items: [],
    },
    {
      id: 'agents',
      title: '~/.hermes/AGENTS.md',
      items: [],
    },
    {
      id: 'vault-notes',
      title: 'Vault knowledge',
      items: [],
    },
  ],
};

const fallbackTools: MissionControlToolsSnapshot = {
  available: false,
  count: 4,
  toolCount: 4,
  toolsets: [
    {
      name: 'dashboard',
      description: 'Mission Control dashboard and refresh helpers.',
      directTools: ['refresh-cockpit'],
      includes: [],
      resolvedTools: ['refresh-cockpit'],
      toolCount: 1,
      isComposite: false,
      available: true,
      requirements: [],
    },
    {
      name: 'gateway',
      description: 'Gateway health and operator control surfaces.',
      directTools: ['gateway-health'],
      includes: [],
      resolvedTools: ['gateway-health'],
      toolCount: 1,
      isComposite: false,
      available: true,
      requirements: [],
    },
    {
      name: 'system',
      description: 'Local machine inspection helpers.',
      directTools: ['machine-usage'],
      includes: [],
      resolvedTools: ['machine-usage'],
      toolCount: 1,
      isComposite: false,
      available: true,
      requirements: [],
    },
    {
      name: 'models',
      description: 'Model registry and inference selection tools.',
      directTools: ['model-registry'],
      includes: [],
      resolvedTools: ['model-registry'],
      toolCount: 1,
      isComposite: false,
      available: true,
      requirements: [],
    },
  ],
  availableToolsets: [],
  toolCatalog: [
    { name: 'refresh-cockpit', toolset: 'dashboard', available: true },
    { name: 'gateway-health', toolset: 'gateway', available: true },
    { name: 'machine-usage', toolset: 'system', available: true },
    { name: 'model-registry', toolset: 'models', available: true },
  ],
  resolvedTools: ['gateway-health', 'machine-usage', 'model-registry', 'refresh-cockpit'],
};

const fallbackSkills: MissionControlSkillsSnapshot = {
  available: false,
  count: 3,
  hint: 'Add more skills to the vault to extend Hermes.',
  skills: [
    {
      id: 'session-recorder',
      name: 'Session Recorder',
      description: 'Tracks active conversations and stores compact histories.',
      enabled: true,
      model: 'gpt-5.4-mini',
      tags: ['sessions', 'history'],
      category: 'Sessions',
      filePath: 'skills/session-recorder.md',
    },
    {
      id: 'cron-orchestrator',
      name: 'Cron Orchestrator',
      description: 'Turns scheduled jobs into worker prompts and status updates.',
      enabled: true,
      model: 'gpt-5.4-mini',
      tags: ['cron', 'automation'],
      category: 'Automation',
      filePath: 'skills/cron-orchestrator.md',
    },
    {
      id: 'knowledge-synthesizer',
      name: 'Knowledge Synthesizer',
      description: 'Summarises shared notes into the Mission Control knowledge lane.',
      enabled: false,
      model: 'gpt-5.4-mini',
      tags: ['knowledge', 'notes'],
      category: 'Knowledge',
      filePath: 'skills/knowledge-synthesizer.md',
    },
  ],
  categories: [
    { name: 'Sessions', count: 1, skills: ['session-recorder'] },
    { name: 'Automation', count: 1, skills: ['cron-orchestrator'] },
    { name: 'Knowledge', count: 1, skills: ['knowledge-synthesizer'] },
  ],
};

const fallbackSkillsCatalog: MissionControlSkillsCatalogSnapshot = {
  available: false,
  count: 0,
  hint: 'Skills Hub catalog unavailable.',
  skills: [],
  sources: {},
  timedOut: [],
};

const fallbackConfig: MissionControlConfigSnapshot = {
  available: false,
  path: '~/.hermes/config.yaml',
  exists: true,
  content: `# Mission Control config\n# Edit this file directly from Mission Control.\n`,
  hash: 'fallback',
  updatedAt: null,
  config: {},
};

const fallbackLogs: MissionControlLogsSnapshot = {
  available: false,
  path: '~/.hermes/logs',
  fileCount: 0,
  totalEntries: 0,
  generatedAt: null,
  files: [],
};

const fallbackAgentTrace: MissionControlAgentTraceSnapshot = {
  success: true,
  schemaVersion: 'core-safe-v1',
  available: false,
  mode: 'post',
  traceMode: 'unavailable',
  session: null,
  events: [],
  nodes: [],
  edges: [],
  stats: {
    turns: 0,
    toolCalls: 0,
    skills: 0,
    thoughts: 0,
    errors: 0,
    durationSeconds: 0,
  },
  warnings: ['Agent trace unavailable.'],
};

const fallbackCapabilities: MissionControlCapabilities = {
  schemaVersion: 'core-safe-v1',
  trace: {
    stream: false,
    compact: false,
    namedSseTraceEvent: false,
  },
  traceModes: ['native', 'transcript', 'unavailable'],
};

const fallbackSnapshot: MissionControlSnapshot = {
  backendHealth: 'healthy',
  activeModel: 'gpt-5.4-mini',
  fallbackModel: 'minimax-m2.7 @ localhost:8787',
  gatewayStatus: 'online',
  activeAgents: 3,
  candidatesEnabled: false,
  queuedJobs: 1,
  toolCallsToday: 42,
  recentSignals: [
    { label: 'Gateway', detail: 'Discord + DM intake is live and ready.', tone: 'good' },
    { label: 'Fallback', detail: 'Local MiniMax standby is configured for primary failure.', tone: 'good' },
    { label: 'Vision', detail: 'Mobile vision reads are available for screenshot triage.', tone: 'good' },
    { label: 'Alerts', detail: 'No critical incidents. Just the usual existential drift.', tone: 'warn' },
    { label: 'Machine', detail: fallbackMachine.summary, tone: 'warn' },
  ],
  knowledgeSharing: fallbackKnowledge,
  machine: fallbackMachine,
  sessions: fallbackSessions,
  cron: fallbackCron,
  alerts: fallbackAlerts,
};


function apiBaseUrl() {
  return localApiBaseUrl();
}

function localApiBaseUrl() {
  return import.meta.env.VITE_MISSION_CONTROL_LOCAL_API_BASE_URL || '/api/local';
}

function apiUrl(path: string) {
  return `${apiBaseUrl().replace(/\/$/, '')}${path}`;
}

export function localApiUrl(path: string) {
  return apiUrl(path);
}

export function buildHeaders(accessToken?: string): Record<string, string> {
  const headers: Record<string, string> = {
    Accept: 'application/json',
  };

  if (accessToken) {
    headers.Authorization = `Bearer ${accessToken.trim()}`;
  }

  return headers;
}

function redactHomePath(value: string | undefined | null): string | undefined | null {
  if (!value) return value;
  // macOS: /Users/<name>, Linux: /home/<name>
  return value.replace(/^\/(Users|home)\/[^/]+/, '~');
}

function normalizeMachineStatus(input: Partial<MissionControlMachineStatus> | undefined): MissionControlMachineStatus {
  if (!input) return fallbackMachine;

  return {
    health: input.health ?? fallbackMachine.health,
    source: input.source ?? fallbackMachine.source,
    host: input.host ?? fallbackMachine.host,
    platform: input.platform ?? fallbackMachine.platform,
    platformVersion: input.platformVersion ?? fallbackMachine.platformVersion,
    cpuCores: input.cpuCores ?? fallbackMachine.cpuCores,
    cpuUsagePercent: input.cpuUsagePercent ?? fallbackMachine.cpuUsagePercent,
    ramUsage: {
      usedPercent: input.ramUsage?.usedPercent ?? fallbackMachine.ramUsage.usedPercent,
      usedGb: input.ramUsage?.usedGb ?? fallbackMachine.ramUsage.usedGb,
      availableGb: input.ramUsage?.availableGb ?? fallbackMachine.ramUsage.availableGb,
      totalGb: input.ramUsage?.totalGb ?? fallbackMachine.ramUsage.totalGb,
    },
    loadAverage: {
      one: input.loadAverage?.one ?? fallbackMachine.loadAverage.one,
      five: input.loadAverage?.five ?? fallbackMachine.loadAverage.five,
      fifteen: input.loadAverage?.fifteen ?? fallbackMachine.loadAverage.fifteen,
      perCore: input.loadAverage?.perCore ?? fallbackMachine.loadAverage.perCore,
    },
    diskUsage: {
      path: redactHomePath(input.diskUsage?.path ?? fallbackMachine.diskUsage.path) ?? fallbackMachine.diskUsage.path,
      usedPercent: input.diskUsage?.usedPercent ?? fallbackMachine.diskUsage.usedPercent,
      freeGb: input.diskUsage?.freeGb ?? fallbackMachine.diskUsage.freeGb,
      totalGb: input.diskUsage?.totalGb ?? fallbackMachine.diskUsage.totalGb,
    },
    processMemoryMb: input.processMemoryMb ?? fallbackMachine.processMemoryMb,
    thermal: {
      fanRpm: input.thermal?.fanRpm ?? fallbackMachine.thermal.fanRpm,
      fanCount: input.thermal?.fanCount ?? fallbackMachine.thermal.fanCount,
      thermalPressure: input.thermal?.thermalPressure ?? fallbackMachine.thermal.thermalPressure,
      thermalLevel: input.thermal?.thermalLevel ?? fallbackMachine.thermal.thermalLevel,
      levelSource: input.thermal?.levelSource ?? fallbackMachine.thermal.levelSource,
      source: input.thermal?.source ?? fallbackMachine.thermal.source,
      error: input.thermal?.error ?? fallbackMachine.thermal.error,
    },
    summary: input.summary ?? fallbackMachine.summary,
  };
}

function normalizeSessionItem(input: Partial<MissionControlSessionItem> | undefined): MissionControlSessionItem {
  const status = input?.status === 'live' || input?.status === 'idle' || input?.status === 'ended'
    ? input.status
    : input?.endedAt === null || input?.endedAt === undefined ? 'idle' : 'ended';
  return {
    id: input?.id ?? 'unknown-session',
    source: input?.source ?? 'unknown',
    model: input?.model ?? 'unknown',
    title: input?.title ?? 'Untitled session',
    startedAt: Number(input?.startedAt ?? 0),
    endedAt: input?.endedAt === null || input?.endedAt === undefined ? null : Number(input.endedAt),
    status,
    messageCount: Number(input?.messageCount ?? 0),
    preview: input?.preview ?? '',
    lastActive: Number(input?.lastActive ?? 0),
  };
}

function normalizeSessions(input: Partial<MissionControlSessionsSnapshot> | undefined): MissionControlSessionsSnapshot {
  return {
    totalSessions: Number(input?.totalSessions ?? fallbackSessions.totalSessions),
    totalMessages: Number(input?.totalMessages ?? fallbackSessions.totalMessages),
    activeAgents: Number(input?.activeAgents ?? fallbackSessions.activeAgents),
    toolCallsToday: Number(input?.toolCallsToday ?? fallbackSessions.toolCallsToday),
    items: (input?.items ?? fallbackSessions.items).map((item) => normalizeSessionItem(item)),
  };
}

function pickTraceCandidate(input: unknown): Partial<MissionControlAgentTraceSnapshot> | undefined {
  if (!input || typeof input !== 'object') return undefined;
  const candidate = input as Record<string, unknown>;

  if (Array.isArray(candidate.events) || Array.isArray(candidate.nodes) || Array.isArray(candidate.edges)) {
    return candidate as Partial<MissionControlAgentTraceSnapshot>;
  }

  const nested = ['trace', 'data', 'payload']
    .map((key) => candidate[key])
    .find((value) => value && typeof value === 'object') as Record<string, unknown> | undefined;

  if (!nested) return undefined;
  if (Array.isArray(nested.events) || Array.isArray(nested.nodes) || Array.isArray(nested.edges)) {
    return nested as Partial<MissionControlAgentTraceSnapshot>;
  }

  return undefined;
}

function normalizeCapabilities(input: unknown): MissionControlCapabilities {
  if (!input || typeof input !== 'object') return fallbackCapabilities;
  const candidate = input as Record<string, unknown>;
  const rawTrace = candidate.trace;
  const trace = (rawTrace && typeof rawTrace === 'object' ? rawTrace : {}) as Record<string, unknown>;
  const traceEnabled = typeof rawTrace === 'boolean' ? rawTrace : null;
  const traceModes = Array.isArray(candidate.traceModes)
    ? candidate.traceModes.map((mode) => normalizeTraceMode(mode))
    : fallbackCapabilities.traceModes;

  return {
    schemaVersion: typeof candidate.schemaVersion === 'string' && candidate.schemaVersion ? candidate.schemaVersion : fallbackCapabilities.schemaVersion,
    trace: {
      stream: typeof trace.stream === 'boolean' ? trace.stream : (traceEnabled ?? fallbackCapabilities.trace.stream),
      compact: typeof trace.compact === 'boolean' ? trace.compact : (traceEnabled ?? fallbackCapabilities.trace.compact),
      namedSseTraceEvent:
        typeof trace.namedSseTraceEvent === 'boolean'
          ? trace.namedSseTraceEvent
          : (traceEnabled ?? fallbackCapabilities.trace.namedSseTraceEvent),
    },
    traceModes,
  };
}

function getAgentKey(source?: string, model?: string): string {
  return `${source || 'unknown'}::${model || 'unknown'}`;
}

function normalizeTraceMode(value: unknown): MissionControlTraceMode {
  return value === 'native' || value === 'transcript' || value === 'unavailable' ? value : 'unavailable';
}

function normalizeSessionPreviewMessages(input: unknown): MissionControlSessionPreviewMessage[] {
  if (!Array.isArray(input)) return [];
  return input
    .filter(isRecord)
    .map((message) => ({
      role: message.role === 'user' ? 'user' as const : 'assistant' as const,
      text: readString(message.text),
      timestamp: message.timestamp === null || message.timestamp === undefined ? null : readNumber(message.timestamp, 0),
    }))
    .filter((message) => message.text.trim().length > 0);
}

function normalizeAgentSessionItem(input: Record<string, unknown> | undefined): MissionControlAgentSessionItem {
  const source = readString(input?.source, 'unknown');
  const model = readString(input?.model, 'unknown');
  const rawCategory = input?.category;
  const category = rawCategory === 'conversation' || rawCategory === 'automation' || rawCategory === 'system' || rawCategory === 'unknown'
    ? rawCategory
    : ['tui', 'discord', 'telegram', 'mission-control'].includes(source)
      ? 'conversation'
      : ['cron', 'kanban'].includes(source)
        ? 'automation'
        : 'unknown';
  return {
    sessionId: readString(input?.sessionId, readString(input?.id, 'unknown-session')),
    agentId: readString(input?.agentId, getAgentKey(source, model)),
    title: readString(input?.title, 'Untitled session'),
    source,
    platform: readString(input?.platform, source),
    chatType: readString(input?.chatType, 'unknown'),
    displayName: readString(input?.displayName, 'Unknown session'),
    model,
    startedAt: input?.startedAt === null || input?.startedAt === undefined ? null : readNumber(input?.startedAt, 0),
    lastActiveAt: input?.lastActiveAt === null || input?.lastActiveAt === undefined ? null : readNumber(input?.lastActiveAt, 0),
    endedAt: input?.endedAt === null || input?.endedAt === undefined ? null : readNumber(input?.endedAt, 0),
    status: input?.status === 'live' || input?.status === 'idle' || input?.status === 'ended' ? input.status : 'idle',
    category,
    originLabel: readString(input?.originLabel, source === 'unknown' ? 'Unknown' : source),
    isResumable: readBoolean(input?.isResumable, category === 'conversation'),
    messageCount: readNumber(input?.messageCount, 0),
    traceMode: normalizeTraceMode(input?.traceMode),
    preview: readString(input?.preview),
    recentMessages: normalizeSessionPreviewMessages(input?.recentMessages),
    inputTokens: readNumber(input?.inputTokens, 0),
    outputTokens: readNumber(input?.outputTokens, 0),
    cacheReadTokens: readNumber(input?.cacheReadTokens, 0),
    cacheWriteTokens: readNumber(input?.cacheWriteTokens, 0),
    reasoningTokens: readNumber(input?.reasoningTokens, 0),
    estimatedCostUsd: readNumber(input?.estimatedCostUsd, 0),
    costStatus: readString(input?.costStatus),
  };
}

function normalizeFacetCounts(input: Record<string, unknown> | undefined, keys: string[], fallbackItems: MissionControlAgentSessionItem[], field: 'status' | 'category' | 'source' | 'model'): Record<string, number> {
  if (input) {
    return Object.fromEntries(Object.entries(input).map(([key, value]) => [key, readNumber(value, 0)]));
  }
  const counts: Record<string, number> = Object.fromEntries(keys.map((key) => [key, 0]));
  for (const item of fallbackItems) {
    const key = field === 'status' ? item.status : field === 'category' ? item.category : field === 'model' ? item.model : item.source;
    counts[key] = (counts[key] ?? 0) + 1;
  }
  return counts;
}

function normalizeAgentSessionsSnapshot(input: OfficialMissionControlAgentSessionsPayload | null | undefined): MissionControlAgentsSessionsSnapshot {
  const items = Array.isArray(input?.items) ? input.items.filter(isRecord).map((item) => normalizeAgentSessionItem(item)) : [];
  return {
    success: input?.success ?? true,
    schemaVersion: readString(input?.schemaVersion, '1'),
    available: input?.available ?? true,
    items,
    stats: {
      totalSessions: readNumber(input?.stats?.totalSessions, items.length),
      liveSessions: readNumber(input?.stats?.liveSessions, items.filter((item) => item.status === 'live').length),
      activeAgents: readNumber(input?.stats?.activeAgents, new Set(items.filter((item) => item.status === 'live').map((item) => item.agentId)).size),
    },
    facets: {
      status: normalizeFacetCounts(input?.facets?.status, ['live', 'idle', 'ended'], items, 'status') as Record<MissionControlAgentSessionStatus, number>,
      category: normalizeFacetCounts(input?.facets?.category, ['conversation', 'automation', 'system', 'unknown'], items, 'category') as Record<'conversation' | 'automation' | 'system' | 'unknown', number>,
      origin: normalizeFacetCounts(input?.facets?.origin, [], items, 'source'),
      model: normalizeFacetCounts(input?.facets?.model, [], items, 'model'),
    },
    tabCounts: {
      all: readNumber(input?.tabCounts?.all, readNumber(input?.stats?.totalSessions, items.length)),
      live: readNumber(input?.tabCounts?.live, readNumber(input?.stats?.liveSessions, items.filter((item) => item.status === 'live').length)),
      conversation: readNumber(input?.tabCounts?.conversation, readNumber(input?.facets?.category?.conversation, items.filter((item) => item.category === 'conversation').length)),
      automation: readNumber(input?.tabCounts?.automation, readNumber(input?.facets?.category?.automation, items.filter((item) => item.category === 'automation').length)),
      system: readNumber(input?.tabCounts?.system, readNumber(input?.facets?.category?.system, items.filter((item) => item.category === 'system').length)),
    },
    pagination: {
      total: readNumber(input?.pagination?.total, items.length),
      offset: readNumber(input?.pagination?.offset, 0),
      limit: readNumber(input?.pagination?.limit, items.length),
      hasMore: Boolean(input?.pagination?.hasMore ?? false),
    },
  };
}

function normalizeTraceSessionRef(input: Record<string, unknown> | null | undefined): MissionControlAgentTraceSnapshot['session'] {
  if (!input) return null;
  return {
    id: readString(input.sessionId, readString(input.id, 'unknown-session')),
    source: readString(input.source, 'unknown'),
    model: readString(input.model, 'unknown'),
    title: readString(input.title, 'Untitled session'),
    startedAt: input.startedAt === null || input.startedAt === undefined ? 0 : readNumber(input.startedAt, 0),
    endedAt: null,
    status: 'idle',
    messageCount: 0,
    preview: '',
    lastActive: input.lastActiveAt === null || input.lastActiveAt === undefined ? 0 : readNumber(input.lastActiveAt, 0),
  };
}

function normalizeAgentTracePayload(input: Partial<MissionControlAgentTraceSnapshot> | undefined): MissionControlAgentTraceSnapshot {
  const normalized = normalizeAgentTrace(input);
  const record = (input && typeof input === 'object' ? input : {}) as Record<string, unknown>;
  return {
    ...normalized,
    session: normalizeTraceSessionRef(record.session as Record<string, unknown> | null | undefined),
    traceMode: normalizeTraceMode(record.traceMode),
    warnings: Array.isArray(record.warnings) ? record.warnings.filter((item): item is string => typeof item === 'string') : [],
  };
}

function normalizeAgentTrace(input: Partial<MissionControlAgentTraceSnapshot> | undefined): MissionControlAgentTraceSnapshot {
  if (!input) return fallbackAgentTrace;

  return {
    success: Boolean(input.success ?? true),
    schemaVersion: input.schemaVersion ?? fallbackAgentTrace.schemaVersion,
    available: input.available ?? fallbackAgentTrace.available,
    mode: input.mode === 'live' ? 'live' : 'post',
    traceMode: normalizeTraceMode(input.traceMode),
    session: input.session ? normalizeSessionItem(input.session as Partial<MissionControlSessionItem>) : null,
    events: (input.events ?? fallbackAgentTrace.events).map((event, index) => ({
      id: event?.id ?? `event-${index + 1}`,
      type: event?.type ?? 'event',
      label: event?.label ?? 'Event',
      detail: event?.detail ?? '',
      tone: event?.tone === 'bad' ? 'bad' : event?.tone === 'warn' ? 'warn' : 'good',
      status: event?.status ?? undefined,
      timestamp: Number(event?.timestamp ?? 0),
      sessionId: event?.sessionId ?? (input.session as Partial<MissionControlSessionItem> | undefined)?.id ?? 'session',
      turnId: Number(event?.turnId ?? 0),
      parentEventId: event?.parentEventId ?? undefined,
      toolName: event?.toolName ?? undefined,
      callId: event?.callId ?? undefined,
      skillName: event?.skillName ?? undefined,
      request: event?.request ?? undefined,
      response: event?.response ?? undefined,
    })),
    nodes: (input.nodes ?? fallbackAgentTrace.nodes).map((node, index) => ({
      id: node?.id ?? `node-${index + 1}`,
      kind: node?.kind ?? 'event',
      label: node?.label ?? 'Node',
      status: node?.status ?? 'ok',
      turnId: Number(node?.turnId ?? 0),
      timestamp: Number(node?.timestamp ?? 0),
    })),
    edges: (input.edges ?? fallbackAgentTrace.edges).map((edge) => ({
      from: edge?.from ?? '',
      to: edge?.to ?? '',
      kind: edge?.kind ?? 'sequence',
    })).filter((edge) => edge.from && edge.to),
    stats: {
      turns: Number(input.stats?.turns ?? 0),
      toolCalls: Number(input.stats?.toolCalls ?? 0),
      skills: Number(input.stats?.skills ?? 0),
      thoughts: Number(input.stats?.thoughts ?? 0),
      errors: Number(input.stats?.errors ?? 0),
      durationSeconds: Number(input.stats?.durationSeconds ?? 0),
    },
    warnings: input.warnings ?? fallbackAgentTrace.warnings,
  };
}

function normalizeCronJob(input: Partial<MissionControlCronJob> | undefined): MissionControlCronJob {
  return {
    id: input?.id ?? 'scheduled-job',
    label: input?.label ?? input?.id ?? 'Scheduled job',
    enabled: input?.enabled ?? true,
    state: input?.state ?? 'scheduled',
    scheduleDisplay: input?.scheduleDisplay ?? 'unspecified',
    scheduleKind: input?.scheduleKind,
    scheduleExpr: input?.scheduleExpr ?? null,
    nextRunAt: input?.nextRunAt ?? null,
    lastRunAt: input?.lastRunAt ?? null,
    createdAt: input?.createdAt ?? null,
    pausedReason: input?.pausedReason ?? null,
    lastStatus: input?.lastStatus ?? null,
    lastError: input?.lastError ?? null,
    prompt: input?.prompt ?? '',
    model: input?.model ?? 'unknown',
    provider: input?.provider ?? null,
    skill: input?.skill ?? null,
    skills: input?.skills ?? [],
    repeat: input?.repeat ?? null,
    deliver: input?.deliver ?? null,
    script: input?.script ?? null,
    noAgent: input?.noAgent ?? false,
    contextFrom: input?.contextFrom ?? [],
    enabledToolsets: input?.enabledToolsets ?? [],
    workdir: input?.workdir ?? null,
    monitorScript: input?.monitorScript ?? null,
    monitorUrl: input?.monitorUrl ?? null,
    attachToSession: input?.attachToSession ?? null,
    reasoningEffort: input?.reasoningEffort ?? null,
    lastOutput: input?.lastOutput ?? null,
    latestExecution: input?.latestExecution ?? null,
  };
}

function normalizeCron(input: Partial<MissionControlCronSnapshot> | undefined): MissionControlCronSnapshot {
  return {
    queuedJobs: Number(input?.queuedJobs ?? fallbackCron.queuedJobs),
    items: (input?.items ?? fallbackCron.items).map((item) => normalizeCronJob(item)),
  };
}

function normalizeAlert(input: Partial<MissionControlAlert> | undefined): MissionControlAlert {
  return {
    id: input?.id ?? 'unknown-alert',
    category: input?.category ?? 'gateway',
    tone: input?.tone ?? 'warn',
    title: input?.title ?? 'Attention required',
    detail: input?.detail ?? '',
    endpoint: input?.endpoint,
  };
}

function normalizeAlerts(input: Partial<MissionControlAlertsSnapshot> | undefined): MissionControlAlertsSnapshot {
  return {
    items: (input?.items ?? fallbackAlerts.items).map((item) => normalizeAlert(item)),
  };
}

function normalizeKnowledgeItem(input: Partial<MissionControlKnowledgeItem> | undefined): MissionControlKnowledgeItem {
  return {
    id: input?.id ?? input?.title ?? 'knowledge-item',
    title: input?.title ?? 'Untitled note',
    path: redactHomePath(input?.path ?? 'Knowledge Sharing.md') ?? 'Knowledge Sharing.md',
    sourcePath: redactHomePath(input?.sourcePath ?? undefined) ?? undefined,
    updatedAt: input?.updatedAt ?? null,
    excerpt: input?.excerpt ?? '',
    highlights: input?.highlights ?? [],
    contentPreview: input?.contentPreview ?? '',
  };
}

function normalizeKnowledgeSection(input: Partial<MissionControlKnowledgeSection> | undefined): MissionControlKnowledgeSection {
  return {
    id: input?.id ?? 'section',
    title: input?.title ?? 'Section',
    items: (input?.items ?? []).map((item) => normalizeKnowledgeItem(item)),
  };
}

function normalizeKnowledge(input: Partial<MissionControlKnowledgeSnapshot> | undefined): MissionControlKnowledgeSnapshot {
  const primary = normalizeKnowledgeItem(input?.primary ?? input?.items?.[0] ?? fallbackKnowledge.primary);
  const items = (input?.items ?? fallbackKnowledge.items).map((item) => normalizeKnowledgeItem(item));
  const sections = (input?.sections ?? fallbackKnowledge.sections).map((section) => normalizeKnowledgeSection(section));

  return {
    available: input?.available ?? fallbackKnowledge.available,
    vaultPath: redactHomePath(input?.vaultPath ?? fallbackKnowledge.vaultPath) ?? fallbackKnowledge.vaultPath,
    title: input?.title ?? primary.title ?? fallbackKnowledge.title,
    path: redactHomePath(input?.path ?? primary.path ?? fallbackKnowledge.path) ?? fallbackKnowledge.path,
    updatedAt: input?.updatedAt ?? primary.updatedAt ?? fallbackKnowledge.updatedAt,
    excerpt: input?.excerpt ?? primary.excerpt ?? fallbackKnowledge.excerpt,
    highlights: input?.highlights ?? primary.highlights ?? fallbackKnowledge.highlights,
    primary,
    items,
    sections,
  };
}

function normalizeToolset(input: Partial<MissionControlToolsetItem> | undefined): MissionControlToolsetItem {
  return {
    name: input?.name ?? 'toolset',
    description: input?.description ?? '',
    directTools: input?.directTools ?? [],
    includes: input?.includes ?? [],
    resolvedTools: input?.resolvedTools ?? [],
    toolCount: Number(input?.toolCount ?? (input?.resolvedTools?.length ?? 0)),
    isComposite: input?.isComposite ?? false,
    available: input?.available ?? true,
    requirements: input?.requirements ?? [],
  };
}

function normalizeTools(input: Partial<MissionControlToolsSnapshot> | undefined): MissionControlToolsSnapshot {
  const toolsets = (input?.toolsets ?? fallbackTools.toolsets).map((item) => normalizeToolset(item));
  const availableToolsets = (input?.availableToolsets ?? toolsets).map((item) => normalizeToolset(item));
  const toolCatalog = (input?.toolCatalog ?? fallbackTools.toolCatalog).map((item) => ({
    name: item?.name ?? 'tool',
    toolset: item?.toolset ?? 'general',
    available: item?.available ?? true,
    sourcePath: redactHomePath(item?.sourcePath ?? null) ?? null,
  }));

  return {
    available: input?.available ?? fallbackTools.available,
    count: Number(input?.count ?? toolsets.length),
    toolCount: Number(input?.toolCount ?? toolCatalog.length),
    toolsets,
    availableToolsets,
    toolCatalog,
    resolvedTools: input?.resolvedTools ?? toolCatalog.map((item) => item.name),
  };
}

function normalizeSkillItem(input: Partial<MissionControlSkillItem> | undefined): MissionControlSkillItem {
  const filePath = input?.filePath ?? (input as { file_path?: string } | undefined)?.file_path;
  return {
    id: input?.id ?? input?.name ?? 'skill-item',
    name: input?.name ?? 'Unnamed skill',
    description: input?.description ?? '',
    enabled: input?.enabled ?? true,
    model: input?.model ?? 'unknown',
    tags: input?.tags ?? [],
    category: input?.category,
    filePath: redactHomePath(filePath) ?? undefined,
  };
}

function normalizeSkillCategory(input: Partial<MissionControlSkillCategory> | undefined): MissionControlSkillCategory {
  return {
    name: input?.name ?? 'Category',
    description: input?.description,
    count: Number(input?.count ?? (input?.skills?.length ?? 0)),
    skills: input?.skills ?? [],
  };
}

function normalizeSkills(input: Partial<MissionControlSkillsSnapshot> | undefined): MissionControlSkillsSnapshot {
  const skills = (input?.skills ?? fallbackSkills.skills).map((item) => normalizeSkillItem(item));
  const categories = (input?.categories ?? fallbackSkills.categories).map((item) => normalizeSkillCategory(item));
  return {
    available: input?.available ?? fallbackSkills.available,
    count: Number(input?.count ?? skills.length),
    hint: input?.hint ?? fallbackSkills.hint,
    skills,
    categories,
  };
}

function normalizeSkillsCatalog(input: Partial<MissionControlSkillsCatalogSnapshot> | undefined): MissionControlSkillsCatalogSnapshot {
  const rawSkills = input?.skills ?? fallbackSkillsCatalog.skills;
  const skills = rawSkills.map((item) => ({
    id: item.id ?? item.identifier ?? item.name ?? 'skill-catalog-item',
    name: item.name ?? 'Unnamed skill',
    description: item.description ?? '',
    source: item.source ?? 'unknown',
    identifier: item.identifier ?? item.id ?? item.name ?? 'unknown',
    trustLevel: item.trustLevel ?? 'community',
    repo: item.repo ?? null,
    path: item.path ?? null,
    tags: item.tags ?? [],
    installed: Boolean(item.installed),
  }));

  return {
    available: input?.available ?? fallbackSkillsCatalog.available,
    count: Number(input?.count ?? skills.length),
    hint: input?.hint ?? fallbackSkillsCatalog.hint,
    skills,
    sources: input?.sources ?? fallbackSkillsCatalog.sources,
    timedOut: input?.timedOut ?? fallbackSkillsCatalog.timedOut,
  };
}

function normalizeConfig(input: Partial<MissionControlConfigSnapshot> | undefined): MissionControlConfigSnapshot {
  return {
    available: input?.available ?? fallbackConfig.available,
    path: redactHomePath(input?.path ?? fallbackConfig.path) ?? fallbackConfig.path,
    exists: input?.exists ?? fallbackConfig.exists,
    content: input?.content ?? fallbackConfig.content,
    hash: input?.hash ?? fallbackConfig.hash,
    updatedAt: input?.updatedAt ?? fallbackConfig.updatedAt,
    config: (input?.config ?? fallbackConfig.config) as Record<string, unknown>,
  };
}

function normalizeLogs(input: Partial<MissionControlLogsSnapshot> | undefined): MissionControlLogsSnapshot {
  const files: MissionControlLogFile[] = (input?.files ?? fallbackLogs.files).map((file) => {
    const entries: MissionControlLogEntry[] = (file.entries ?? []).map((entry) => ({
      lineNumber: Number(entry.lineNumber ?? 0),
      level: entry.level === 'error' ? 'error' : entry.level === 'warn' ? 'warn' : 'info',
      text: String(entry.text ?? ''),
    }));

    return {
      name: file.name ?? 'unknown.log',
      path: redactHomePath(file.path ?? '~/.hermes/logs') ?? '~/.hermes/logs',
      updatedAt: file.updatedAt ?? null,
      sizeBytes: Number(file.sizeBytes ?? 0),
      entryCount: Number(file.entryCount ?? entries.length),
      entries,
    };
  });

  return {
    available: input?.available ?? fallbackLogs.available,
    path: redactHomePath(input?.path ?? fallbackLogs.path) ?? fallbackLogs.path,
    fileCount: Number(input?.fileCount ?? files.length),
    totalEntries: Number(input?.totalEntries ?? files.reduce((acc, file) => acc + file.entries.length, 0)),
    generatedAt: input?.generatedAt ?? null,
    files,
  };
}

function normalizeSnapshot(input: Partial<MissionControlSnapshot>): MissionControlSnapshot {
  return {
    backendHealth: input.backendHealth ?? fallbackSnapshot.backendHealth,
    activeModel: input.activeModel ?? fallbackSnapshot.activeModel,
    fallbackModel: input.fallbackModel ?? fallbackSnapshot.fallbackModel,
    gatewayStatus: input.gatewayStatus ?? fallbackSnapshot.gatewayStatus,
    activeAgents: input.activeAgents ?? fallbackSnapshot.activeAgents,
    candidatesEnabled: input.candidatesEnabled ?? fallbackSnapshot.candidatesEnabled,
    queuedJobs: input.queuedJobs ?? fallbackSnapshot.queuedJobs,
    toolCallsToday: input.toolCallsToday ?? fallbackSnapshot.toolCallsToday,
    recentSignals: input.recentSignals ?? fallbackSnapshot.recentSignals,
    knowledgeSharing: normalizeKnowledge(input.knowledgeSharing),
    machine: normalizeMachineStatus(input.machine),
    sessions: normalizeSessions(input.sessions),
    cron: normalizeCron(input.cron),
    alerts: normalizeAlerts(input.alerts),
  };
}

async function parseResponse<T>(response: Response, fallbackName: string): Promise<T> {
  if (!response.ok) {
    throw new Error(`Mission Control ${fallbackName} API returned ${response.status}`);
  }

  return (await response.json()) as T;
}

async function loadResource<T>(path: string, accessToken: string | undefined, fallback: T, fallbackName: string): Promise<T> {
  try {
    const response = await fetch(apiUrl(path), {
      headers: buildHeaders(accessToken),
      cache: 'no-store',
    });

    if (response.status === 401) {
      throw new MissionControlAuthError();
    }

    const data = await parseResponse<Partial<T>>(response, fallbackName);
    return data as T;
  } catch (error) {
    if (error instanceof MissionControlAuthError) {
      throw error;
    }

    return fallback;
  }
}


type OfficialStatusPayload = {
  gateway_running?: boolean;
  gateway_state?: string | null;
  active_sessions?: number;
  candidates_enabled?: boolean;
  config_path?: string;
  gateway_updated_at?: string | null;
};

type OfficialModelInfoPayload = {
  model?: string;
  provider?: string;
  auto_context_length?: number;
  config_context_length?: number;
  effective_context_length?: number;
  capabilities?: Record<string, unknown>;
};

type OfficialSessionsPayload = {
  sessions?: Array<Record<string, unknown>>;
  total?: number;
};

type OfficialMissionControlAgentsPayload = {
  success?: boolean;
  schemaVersion?: string;
  available?: boolean;
  capabilities?: {
    trace?: boolean | {
      stream?: boolean;
      compact?: boolean;
      namedSseTraceEvent?: boolean;
    };
    traceModes?: string[];
  };
};

type OfficialMissionControlAgentSessionsPayload = {
  success?: boolean;
  schemaVersion?: string;
  available?: boolean;
  items?: Array<Record<string, unknown>>;
  offset?: number;
  pagination?: {
    total?: number;
    offset?: number;
    limit?: number;
    hasMore?: boolean;
  };
  stats?: {
    totalSessions?: number;
    liveSessions?: number;
    activeAgents?: number;
  };
  facets?: {
    status?: Record<string, unknown>;
    category?: Record<string, unknown>;
    origin?: Record<string, unknown>;
    model?: Record<string, unknown>;
  };
  tabCounts?: Record<string, unknown>;
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

function readString(value: unknown, fallback = ''): string {
  return typeof value === 'string' ? value : fallback;
}

function readNullableString(value: unknown): string | null {
  return typeof value === 'string' && value ? value : null;
}

function readNumber(value: unknown, fallback = 0): number {
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  if (typeof value === 'string' && value.trim()) {
    const parsed = Number(value);
    if (Number.isFinite(parsed)) return parsed;
  }
  return fallback;
}

function readBoolean(value: unknown, fallback = false): boolean {
  return typeof value === 'boolean' ? value : fallback;
}

function readStringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string' && item.length > 0) : [];
}

function hashText(value: string): string {
  let hash = 2166136261;
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return `fnv1a-${(hash >>> 0).toString(16).padStart(8, '0')}`;
}

function trimTrailingSlash(value: string): string {
  return value.replace(/\/$/, '');
}

async function fetchOfficialJson<T>(path: string, accessToken?: string): Promise<T> {
  const response = await fetch(apiUrl(path), {
    headers: buildHeaders(accessToken),
    cache: 'no-store',
  });

  if (response.status === 401) {
    throw new MissionControlAuthError();
  }

  return await parseResponse<T>(response, path.replace(/^\//, ''));
}

async function maybeFetchLocalJson<T>(
  path: string,
  accessToken?: string,
): Promise<{ payload: T | null; response: Response | null }> {
  try {
    const response = await fetch(localApiUrl(path), {
      headers: buildHeaders(accessToken),
      cache: 'no-store',
    });

    if (response.status === 401) {
      throw new MissionControlAuthError();
    }

    if (!response.ok) {
      return { payload: null, response };
    }

    return { payload: await response.json() as T, response };
  } catch (error) {
    if (error instanceof MissionControlAuthError) throw error;
    return { payload: null, response: null };
  }
}

async function maybeFetchOfficialJson<T>(path: string, accessToken?: string): Promise<T | null> {
  try {
    return await fetchOfficialJson<T>(path, accessToken);
  } catch (error) {
    if (error instanceof MissionControlAuthError) throw error;
    return null;
  }
}

async function fetchOfficialStatus(accessToken?: string): Promise<OfficialStatusPayload | null> {
  return await maybeFetchOfficialJson<OfficialStatusPayload>('/status', accessToken);
}

async function fetchOfficialModelInfo(accessToken?: string): Promise<OfficialModelInfoPayload | null> {
  return await maybeFetchOfficialJson<OfficialModelInfoPayload>('/model/info', accessToken);
}

async function fetchOfficialSessions(accessToken?: string, limit = 50): Promise<OfficialSessionsPayload | null> {
  return await maybeFetchOfficialJson<OfficialSessionsPayload>(`/sessions?limit=${limit}&offset=0`, accessToken);
}

async function fetchMissionControlAgents(accessToken?: string): Promise<OfficialMissionControlAgentsPayload | null> {
  const { payload: local } = await maybeFetchLocalJson<OfficialMissionControlAgentsPayload>('/mission-control/agents', accessToken);
  if (local) return local;
  return await maybeFetchOfficialJson<OfficialMissionControlAgentsPayload>('/mission-control/agents', accessToken);
}

export type MissionControlAgentSessionFilters = {
  query?: string;
  status?: string;
  category?: string;
  origin?: string;
  model?: string;
  tab?: string;
};

async function fetchMissionControlAgentSessions(
  accessToken?: string,
  limit = 100,
  offset = 0,
  sessionId?: string | null,
  filters?: MissionControlAgentSessionFilters,
): Promise<OfficialMissionControlAgentSessionsPayload | null> {
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  if (sessionId) params.set('session_id', sessionId);
  for (const [key, value] of Object.entries(filters ?? {})) {
    if (value && value !== 'all') params.set(key, value);
  }
  const query = `/mission-control/sessions?${params.toString()}`;
  const { payload: local } = await maybeFetchLocalJson<OfficialMissionControlAgentSessionsPayload>(query, accessToken);
  return local ?? null;
}

export type MissionControlSessionsUsageSnapshot = {
  success: boolean;
  available: boolean;
  totals: {
    inputTokens: number;
    outputTokens: number;
    cacheReadTokens: number;
    cacheWriteTokens: number;
    reasoningTokens: number;
    totalTokens: number;
    estimatedCostUsd: number;
    sessionCount: number;
    pricedSessionCount: number;
  };
  byModel: Array<{
    model: string;
    inputTokens: number;
    outputTokens: number;
    cacheReadTokens: number;
    cacheWriteTokens: number;
    reasoningTokens: number;
    totalTokens: number;
    estimatedCostUsd: number;
    sessionCount: number;
    pricedSessionCount: number;
  }>;
};

const fallbackUsage: MissionControlSessionsUsageSnapshot = {
  success: false,
  available: false,
  totals: { inputTokens: 0, outputTokens: 0, cacheReadTokens: 0, cacheWriteTokens: 0, reasoningTokens: 0, totalTokens: 0, estimatedCostUsd: 0, sessionCount: 0, pricedSessionCount: 0 },
  byModel: [],
};

export async function loadSessionsUsage(accessToken?: string): Promise<MissionControlSessionsUsageSnapshot> {
  try {
    const { payload: local } = await maybeFetchLocalJson<MissionControlSessionsUsageSnapshot>('/sessions/usage', accessToken);
    if (local && local.success) return local;
  } catch { /* ignore */ }
  return fallbackUsage;
}

export type MissionControlProviderUsageWindow = {
  id: string;
  label: string;
  usedPercent?: number;
  resetsAt?: string;
  windowMinutes?: number;
  remaining?: number;
  total?: number;
  unit?: string;
  featured?: boolean;
};

export type MissionControlProviderUsageBalance = {
  id: string;
  label: string;
  value?: number;
  currency?: string;
  unit?: string;
  featured?: boolean;
};

export type MissionControlProviderUsageMetric = {
  id: string;
  label: string;
  value?: number | string | boolean | null;
  unit?: string;
  featured?: boolean;
};

export type MissionControlProviderUsage = {
  provider: string;
  available: boolean;
  source?: string;
  updatedAt?: string | null;
  stale?: boolean;
  error?: string;
  plan?: string | null;
  status?: string;
  renewsAt?: string | null;
  windows: MissionControlProviderUsageWindow[];
  balances: MissionControlProviderUsageBalance[];
  metrics: MissionControlProviderUsageMetric[];
  pace?: Record<string, unknown> | null;
};

export type MissionControlProviderUsageSnapshot = {
  schemaVersion?: number;
  success: boolean;
  available: boolean;
  updatedAt?: string;
  providers: MissionControlProviderUsage[];
};

const fallbackProviderUsage: MissionControlProviderUsageSnapshot = {
  success: false,
  available: false,
  providers: [],
};

export async function loadProviderUsage(accessToken?: string): Promise<MissionControlProviderUsageSnapshot> {
  try {
    const { payload: local } = await maybeFetchLocalJson<MissionControlProviderUsageSnapshot>('/provider-usage', accessToken);
    if (local && local.available) return local;
  } catch { /* provider usage is an optional overview panel */ }
  return fallbackProviderUsage;
}

async function fetchMissionControlAgentTrace(
  sessionId?: string,
  accessToken?: string,
  limit = 300,
  compact = false,
): Promise<Partial<MissionControlAgentTraceSnapshot> | null> {
  const params = new URLSearchParams();
  if (sessionId) params.set('session_id', sessionId);
  params.set('limit', String(limit));
  if (compact) params.set('compact', '1');
  const { payload: local } = await maybeFetchLocalJson<Partial<MissionControlAgentTraceSnapshot>>(
    `/mission-control/agents/trace?${params.toString()}`,
    accessToken,
  );
  if (local) return local;
  return await maybeFetchOfficialJson<Partial<MissionControlAgentTraceSnapshot>>(
    `/mission-control/agents/trace?${params.toString()}`,
    accessToken,
  );
}

function formatModelRef(model: string | null | undefined, provider?: string | null, baseUrl?: string | null): string {
  const modelName = (model ?? '').trim();
  if (!modelName) return 'unknown';
  const providerName = (provider ?? '').trim();
  if (providerName) return `${modelName} @ ${providerName}`;
  const url = (baseUrl ?? '').trim();
  if (url) {
    try {
      return `${modelName} @ ${new URL(url).host}`;
    } catch {
      return `${modelName} @ ${url}`;
    }
  }
  return modelName;
}

function deriveFallbackModel(config: Record<string, unknown> | null | undefined): string {
  if (!config) return fallbackSnapshot.fallbackModel;

  const chain = config.fallback_providers;
  if (Array.isArray(chain) && chain.length > 0 && isRecord(chain[0])) {
    const first = chain[0];
    return formatModelRef(readString(first.model), readString(first.provider), readString(first.base_url));
  }

  const single = config.fallback_model;
  if (isRecord(single)) {
    return formatModelRef(readString(single.model), readString(single.provider), readString(single.base_url));
  }

  return fallbackSnapshot.fallbackModel;
}

function deriveGatewayStatus(status: OfficialStatusPayload | null): string {
  if (!status) return fallbackSnapshot.gatewayStatus;
  const state = readString(status.gateway_state).trim();
  if (state) {
    if (state === 'running') return 'online';
    if (state === 'stopped') return 'offline';
    return state.replace(/_/g, ' ');
  }
  return readBoolean(status.gateway_running, false) ? 'online' : 'offline';
}

function deriveBackendHealth(status: OfficialStatusPayload | null, machine: MissionControlMachineStatus): MissionControlSnapshot['backendHealth'] {
  if (!status) return 'offline';
  if (!readBoolean(status.gateway_running, false)) return 'offline';
  if (machine.health === 'critical' || machine.health === 'offline') return 'degraded';
  return 'healthy';
}

function normalizeOfficialSessionItem(input: Record<string, unknown>): MissionControlSessionItem {
  return normalizeSessionItem({
    id: readString(input.id, 'unknown-session'),
    source: readString(input.source, 'unknown'),
    model: readString(input.model, 'unknown'),
    title: readString(input.title, 'Untitled session'),
    startedAt: readNumber(input.started_at),
    endedAt: input.ended_at === null ? null : readNumber(input.ended_at),
    status: readBoolean(input.is_active, false) ? 'live' : undefined,
    messageCount: readNumber(input.message_count),
    preview: readString(input.preview),
    lastActive: readNumber(input.last_active, readNumber(input.started_at)),
  });
}

function deriveSessionsSnapshot(payload: OfficialSessionsPayload | null, status: OfficialStatusPayload | null): MissionControlSessionsSnapshot {
  if (!payload?.sessions) {
    return normalizeSessions({
      ...fallbackSessions,
      activeAgents: readNumber(status?.active_sessions, fallbackSessions.activeAgents),
    });
  }

  const items = payload.sessions.filter(isRecord).map((item) => normalizeOfficialSessionItem(item));
  const activeAgents = payload.sessions.filter((item) => isRecord(item) && readBoolean(item.is_active)).length || readNumber(status?.active_sessions, 0);
  const totalMessages = payload.sessions.reduce((total, item) => total + (isRecord(item) ? readNumber(item.message_count) : 0), 0);
  const todayStart = new Date();
  todayStart.setHours(0, 0, 0, 0);
  const todayEpoch = todayStart.getTime() / 1000;
  const toolCallsToday = payload.sessions.reduce((total, item) => {
    if (!isRecord(item)) return total;
    return readNumber(item.started_at) >= todayEpoch ? total + readNumber(item.tool_call_count) : total;
  }, 0);

  return normalizeSessions({
    totalSessions: readNumber(payload.total, items.length),
    totalMessages,
    activeAgents,
    toolCallsToday,
    items,
  });
}

function normalizeOfficialCronJob(input: Record<string, unknown>): MissionControlCronJob {
  const schedule = isRecord(input.schedule) ? input.schedule : {};
  const scheduleDisplay = readString(input.schedule_display)
    || readString(schedule.display)
    || readString(schedule.expr)
    || 'unspecified';
  const enabled = readBoolean(input.enabled, true);
  const state = readString(input.state) || (enabled ? 'scheduled' : 'paused');
  const repeat = isRecord(input.repeat)
    ? { times: readNumber(input.repeat.times, 0), completed: readNumber(input.repeat.completed, 0) }
    : typeof input.repeat === 'number' ? input.repeat : null;
  const latestExecution = isRecord(input.latest_execution)
    ? {
        status: readNullableString(input.latest_execution.status),
        startedAt: readNullableString(input.latest_execution.started_at),
        finishedAt: readNullableString(input.latest_execution.finished_at),
        error: readNullableString(input.latest_execution.error),
      }
    : null;
  return normalizeCronJob({
    id: readString(input.id, 'scheduled-job'),
    label: readString(input.name) || readString(input.label) || readString(input.id, 'Scheduled job'),
    enabled,
    state,
    scheduleDisplay,
    scheduleKind: readString(schedule.kind),
    scheduleExpr: readNullableString(schedule.expr),
    nextRunAt: readNullableString(input.next_run_at),
    lastRunAt: readNullableString(input.last_run_at),
    createdAt: readNullableString(input.created_at),
    pausedReason: readNullableString(input.paused_reason),
    lastStatus: readNullableString(input.last_status),
    lastError: readNullableString(input.last_error) ?? readNullableString(input.last_delivery_error),
    prompt: readString(input.prompt),
    model: readString(input.model, 'unknown'),
    provider: readNullableString(input.provider),
    skill: readNullableString(input.skill),
    skills: readStringArray(input.skills),
    repeat,
    deliver: readNullableString(input.deliver),
    script: readNullableString(input.script),
    noAgent: readBoolean(input.no_agent),
    contextFrom: readStringArray(input.context_from),
    enabledToolsets: readStringArray(input.enabled_toolsets),
    workdir: readNullableString(input.workdir),
    monitorScript: readNullableString(input.monitor_script),
    monitorUrl: readNullableString(input.monitor_url),
    attachToSession: typeof input.attach_to_session === 'boolean' ? input.attach_to_session : null,
    reasoningEffort: readNullableString(input.reasoning_effort),
    lastOutput: readNullableString(input.last_output),
    latestExecution,
  });
}

function deriveAlerts(
  status: OfficialStatusPayload | null,
  machine: MissionControlMachineStatus,
  sessions: MissionControlSessionsSnapshot,
  cron: MissionControlCronSnapshot,
): MissionControlAlertsSnapshot {
  const items: MissionControlAlert[] = [];
  const gatewayRunning = readBoolean(status?.gateway_running, false);

  if (!gatewayRunning) {
    items.push({
      id: 'gateway-offline',
      category: 'gateway',
      tone: 'bad',
      title: 'Gateway offline',
      detail: 'Hermes gateway is not reporting as running.',
      endpoint: '/api/status',
    });
  }

  if (machine.health === 'critical' || machine.health === 'offline') {
    items.push({
      id: 'machine-health',
      category: 'machine',
      tone: 'bad',
      title: 'Machine telemetry degraded',
      detail: machine.summary,
      endpoint: '/api/local/system',
    });
  } else if (machine.source === 'fallback') {
    items.push({
      id: 'machine-fallback',
      category: 'machine',
      tone: 'warn',
      title: 'Machine telemetry unavailable',
      detail: 'Local telemetry did not answer, so Mission Control is flying half-blind.',
      endpoint: '/api/local/system',
    });
  }

  const failedJobs = cron.items.filter((job) => job.lastStatus === 'failed' || Boolean(job.lastError));
  if (failedJobs.length > 0) {
    items.push({
      id: 'cron-failures',
      category: 'cron',
      tone: 'warn',
      title: 'Cron jobs need attention',
      detail: `${failedJobs.length} scheduled job${failedJobs.length === 1 ? '' : 's'} reported failures or delivery errors.`,
      endpoint: '/api/cron/jobs',
    });
  }

  if (sessions.activeAgents === 0 && gatewayRunning) {
    items.push({
      id: 'sessions-idle',
      category: 'sessions',
      tone: 'good',
      title: 'No active sessions',
      detail: 'Gateway is up, but nothing is actively running right now.',
      endpoint: '/api/sessions',
    });
  }

  return normalizeAlerts({ items: items.length > 0 ? items : [] });
}

function deriveRecentSignals(
  status: OfficialStatusPayload | null,
  modelInfo: OfficialModelInfoPayload | null,
  sessions: MissionControlSessionsSnapshot,
  cron: MissionControlCronSnapshot,
  alerts: MissionControlAlertsSnapshot,
  machine: MissionControlMachineStatus,
): MissionControlSnapshot['recentSignals'] {
  const signals: MissionControlSnapshot['recentSignals'] = [
    {
      label: 'Gateway',
      detail: readBoolean(status?.gateway_running, false)
        ? `Gateway ${deriveGatewayStatus(status)} with ${sessions.activeAgents} active session${sessions.activeAgents === 1 ? '' : 's'}.`
        : 'Gateway is offline or not reachable from the dashboard.',
      tone: readBoolean(status?.gateway_running, false) ? 'good' : 'bad',
    },
    {
      label: 'Model',
      detail: formatModelRef(readString(modelInfo?.model), readString(modelInfo?.provider)),
      tone: readString(modelInfo?.model) ? 'good' : 'warn',
    },
    {
      label: 'Cron',
      detail: cron.items.length > 0 ? `${cron.queuedJobs} job${cron.queuedJobs === 1 ? '' : 's'} queued across ${cron.items.length} schedule${cron.items.length === 1 ? '' : 's'}.` : 'No cron jobs configured.',
      tone: cron.items.some((job) => job.lastStatus === 'failed' || Boolean(job.lastError)) ? 'warn' : 'good',
    },
    {
      label: 'Machine',
      detail: machine.summary,
      tone: machine.health === 'healthy' ? 'good' : machine.health === 'degraded' ? 'warn' : 'bad',
    },
  ];

  if (alerts.items.length > 0) {
    const primary = alerts.items[0];
    signals.push({ label: 'Alert', detail: `${primary.title}: ${primary.detail}`, tone: primary.tone });
  }

  return signals;
}

export async function loadMissionControlSnapshot(accessToken?: string): Promise<MissionControlSnapshot> {
  try {
    const [status, modelInfo, configRaw, machine, cron] = await Promise.all([
      maybeFetchLocalJson<OfficialStatusPayload>('/status', accessToken).then((r) => r.payload),
      maybeFetchLocalJson<OfficialModelInfoPayload>('/model/info', accessToken).then((r) => r.payload),
      maybeFetchLocalJson<Record<string, unknown>>('/config', accessToken).then((r) => r.payload),
      loadLocalMissionControlMachineStatus(accessToken),
      loadMissionControlCron(accessToken),
    ]);

    const sessions = fallbackSessions;
    const knowledgeSharing = fallbackKnowledge;
    const alerts = deriveAlerts(status, machine, sessions, cron);
    return normalizeSnapshot({
      backendHealth: deriveBackendHealth(status, machine),
      activeModel: formatModelRef(readString(modelInfo?.model), readString(modelInfo?.provider)),
      fallbackModel: deriveFallbackModel(configRaw),
      gatewayStatus: deriveGatewayStatus(status),
      activeAgents: sessions.activeAgents,
      candidatesEnabled: readBoolean(status?.candidates_enabled, false),
      queuedJobs: cron.queuedJobs,
      toolCallsToday: sessions.toolCallsToday,
      recentSignals: deriveRecentSignals(status, modelInfo, sessions, cron, alerts, machine),
      knowledgeSharing,
      machine,
      sessions,
      cron,
      alerts,
    });
  } catch (error) {
    if (error instanceof MissionControlAuthError) {
      throw error;
    }

    return fallbackSnapshot;
  }
}

async function loadLocalMissionControlMachineStatus(accessToken?: string): Promise<MissionControlMachineStatus | null> {
  try {
    const response = await fetch(localApiUrl('/system'), {
      headers: buildHeaders(accessToken),
      cache: 'no-store',
    });

    if (response.status === 401) {
      throw new MissionControlAuthError();
    }

    if (!response.ok) {
      return null;
    }

    const data = await response.json() as Partial<MissionControlMachineStatus>;
    return normalizeMachineStatus({ ...data, source: 'local-psutil' });
  } catch (error) {
    if (error instanceof MissionControlAuthError) {
      throw error;
    }
    return null;
  }
}

export async function loadMissionControlMachineStatus(accessToken?: string): Promise<MissionControlMachineStatus> {
  const local = await loadLocalMissionControlMachineStatus(accessToken);
  if (local) {
    return local;
  }

  try {
    // No official backend available; return local-only degraded status
    return normalizeMachineStatus({
      ...fallbackMachine,
      source: 'local-psutil',
      health: 'degraded',
      summary: 'Local telemetry active; no official backend connected.',
    });
  } catch (error) {
    if (error instanceof MissionControlAuthError) {
      throw error;
    }

    return fallbackMachine;
  }
}

export async function loadMissionControlAgentSessions(
  accessToken?: string,
  limit = 100,
  offset = 0,
  filters?: MissionControlAgentSessionFilters,
): Promise<MissionControlAgentsSessionsSnapshot> {
  const payload = await fetchMissionControlAgentSessions(accessToken, limit, offset, null, filters);
  return normalizeAgentSessionsSnapshot(payload);
}

export async function loadMissionControlSessionPreview(accessToken?: string, sessionId?: string | null): Promise<MissionControlAgentSessionItem | null> {
  if (!sessionId) return null;
  const payload = await fetchMissionControlAgentSessions(accessToken, 1, 0, sessionId);
  if (!payload?.items?.length) return null;
  const normalized = normalizeAgentSessionsSnapshot(payload);
  return normalized.items[0] ?? null;
}

export async function loadMissionControlSessions(accessToken?: string): Promise<MissionControlSessionsSnapshot> {
  try {
    const payload = await fetchMissionControlAgentSessions(accessToken, 200);
    if (payload) {
      const normalized = normalizeAgentSessionsSnapshot(payload);
      const items = normalized.items.map((item) =>
        normalizeSessionItem({
          id: item.sessionId,
          source: item.source,
          model: item.model,
          title: item.title,
          startedAt: item.startedAt ?? 0,
          endedAt: item.endedAt,
          status: item.status,
          messageCount: item.messageCount,
          preview: item.preview,
          lastActive: item.lastActiveAt ?? item.startedAt ?? 0,
        }),
      );
      const totalMessages = items.reduce((total, item) => total + item.messageCount, 0);
      return normalizeSessions({
        totalSessions: normalized.stats.totalSessions,
        totalMessages,
        activeAgents: normalized.stats.activeAgents,
        toolCallsToday: fallbackSessions.toolCallsToday,
        items,
      });
    }

    const [legacyPayload, status] = await Promise.all([
      fetchMissionControlAgentSessions(accessToken, 50),
      maybeFetchLocalJson<OfficialStatusPayload>('/status', accessToken).then((r) => r.payload),
    ]);
    return deriveSessionsSnapshot(legacyPayload, status);
  } catch (error) {
    if (error instanceof MissionControlAuthError) {
      throw error;
    }

    return fallbackSessions;
  }
}

export async function loadMissionControlCapabilities(accessToken?: string): Promise<MissionControlCapabilities> {
  try {
    const payload = await fetchMissionControlAgents(accessToken);
    if (payload) {
      return normalizeCapabilities({
        schemaVersion: payload.schemaVersion ?? fallbackCapabilities.schemaVersion,
        trace: payload.capabilities?.trace,
        traceModes: payload.capabilities?.traceModes ?? fallbackCapabilities.traceModes,
      });
    }
  } catch (error) {
    if (error instanceof MissionControlAuthError) {
      throw error;
    }
  }

  return fallbackCapabilities;
}

export async function loadMissionControlAgentTrace(
  sessionId?: string,
  accessToken?: string,
  limit = 300,
  compact = false,
): Promise<MissionControlAgentTraceSnapshot> {
  try {
    const payload = await fetchMissionControlAgentTrace(sessionId, accessToken, limit, compact);
    if (payload) {
      return normalizeAgentTracePayload(payload);
    }
    return normalizeAgentTracePayload(fallbackAgentTrace);
  } catch (error) {
    if (error instanceof MissionControlAuthError) {
      throw error;
    }

    return normalizeAgentTracePayload(fallbackAgentTrace);
  }
}

export async function loadMissionControlCron(accessToken?: string): Promise<MissionControlCronSnapshot> {
  try {
    const { payload } = await maybeFetchLocalJson<Array<Record<string, unknown>> | { jobs?: Array<Record<string, unknown>> }>('/cron/jobs', accessToken);
    const rawJobs = Array.isArray(payload) ? payload : payload?.jobs;
    const items = Array.isArray(rawJobs) ? rawJobs.filter(isRecord).map((job) => normalizeOfficialCronJob(job)) : [];
    const queuedJobs = items.filter((job) => job.enabled && job.state !== 'paused' && Boolean(job.nextRunAt)).length;
    return normalizeCron({ queuedJobs, items });
  } catch (error) {
    if (error instanceof MissionControlAuthError) {
      throw error;
    }

    return fallbackCron;
  }
}

async function requestCronJson<T>(
  path: string,
  method: 'POST' | 'PATCH' | 'DELETE',
  accessToken?: string,
  body?: Record<string, unknown>,
): Promise<T> {
  const response = await fetch(localApiUrl(path), {
    method,
    headers: { ...buildHeaders(accessToken), 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : '{}',
    cache: 'no-store',
  });
  if (response.status === 401) throw new MissionControlAuthError();
  if (!response.ok) {
    let detail = `Cron API returned ${response.status}`;
    try {
      const payload = await response.json() as { detail?: string };
      if (payload.detail) detail = payload.detail;
    } catch {
      // Preserve the HTTP error if the backend did not return JSON.
    }
    throw new Error(detail);
  }
  return await response.json() as T;
}

function normalizeCronMutationResult(payload: unknown): MissionControlCronJob {
  if (isRecord(payload) && isRecord(payload.job)) return normalizeOfficialCronJob(payload.job);
  return normalizeOfficialCronJob(isRecord(payload) ? payload : {});
}

export async function loadMissionControlCronJobs(accessToken?: string): Promise<MissionControlCronJob[]> {
  const snapshot = await loadMissionControlCron(accessToken);
  return snapshot.items;
}

export async function loadMissionControlCronJob(jobId: string, accessToken?: string): Promise<MissionControlCronJob> {
  const { payload } = await maybeFetchLocalJson<Record<string, unknown>>(`/cron/jobs/${encodeURIComponent(jobId)}`, accessToken);
  if (!payload) throw new Error('Cron job not found.');
  return normalizeOfficialCronJob(isRecord(payload.job) ? payload.job : payload);
}

export async function createMissionControlCronJob(
  payload: Record<string, unknown>,
  accessToken?: string,
): Promise<MissionControlCronJob> {
  return normalizeCronMutationResult(await requestCronJson('/cron/jobs', 'POST', accessToken, payload));
}

export async function updateMissionControlCronJob(
  jobId: string,
  payload: Record<string, unknown>,
  accessToken?: string,
): Promise<MissionControlCronJob> {
  return normalizeCronMutationResult(await requestCronJson(`/cron/jobs/${encodeURIComponent(jobId)}`, 'PATCH', accessToken, payload));
}

export async function pauseMissionControlCronJob(jobId: string, accessToken?: string): Promise<MissionControlCronJob> {
  return normalizeCronMutationResult(await requestCronJson(`/cron/jobs/${encodeURIComponent(jobId)}/pause`, 'POST', accessToken));
}

export async function resumeMissionControlCronJob(jobId: string, accessToken?: string): Promise<MissionControlCronJob> {
  return normalizeCronMutationResult(await requestCronJson(`/cron/jobs/${encodeURIComponent(jobId)}/resume`, 'POST', accessToken));
}

export async function runMissionControlCronJob(jobId: string, accessToken?: string): Promise<MissionControlCronJob> {
  return normalizeCronMutationResult(await requestCronJson(`/cron/jobs/${encodeURIComponent(jobId)}/run`, 'POST', accessToken));
}

export async function deleteMissionControlCronJob(jobId: string, accessToken?: string): Promise<void> {
  await requestCronJson(`/cron/jobs/${encodeURIComponent(jobId)}`, 'DELETE', accessToken);
}

export async function loadMissionControlAlerts(accessToken?: string): Promise<MissionControlAlertsSnapshot> {
  try {
    const [status, machine, cron] = await Promise.all([
      maybeFetchLocalJson<OfficialStatusPayload>('/status', accessToken).then((r) => r.payload),
      loadMissionControlMachineStatus(accessToken),
      loadMissionControlCron(accessToken),
    ]);
    return deriveAlerts(status ?? null, machine, fallbackSessions, cron);
  } catch (error) {
    if (error instanceof MissionControlAuthError) {
      throw error;
    }

    return fallbackAlerts;
  }
}

export async function loadMissionControlKnowledge(accessToken?: string): Promise<MissionControlKnowledgeSnapshot> {
  try {
    const { payload, response } = await maybeFetchLocalJson<Partial<MissionControlKnowledgeSnapshot>>('/knowledge', accessToken);
    if (payload) {
      return normalizeKnowledge(payload);
    }
    if (response && response.status < 500 && response.status !== 404) {
      return normalizeKnowledge(fallbackKnowledge);
    }

    // No official backend available; return fallback knowledge
    return normalizeKnowledge(fallbackKnowledge);
  } catch (error) {
    if (error instanceof MissionControlAuthError) {
      throw error;
    }

    return normalizeKnowledge(fallbackKnowledge);
  }
}

export async function loadMissionControlKnowledgeFile(
  sourcePath: string,
  accessToken?: string,
): Promise<MissionControlKnowledgeFilePayload> {
  try {
    const { payload, response } = await maybeFetchLocalJson<MissionControlKnowledgeFilePayload>(
      `/knowledge/file?path=${encodeURIComponent(sourcePath)}`,
      accessToken,
    );

    if (payload) {
      return {
        ...payload,
        path: redactHomePath(payload.path) ?? payload.path,
        sourcePath: redactHomePath(payload.sourcePath) ?? payload.sourcePath,
      };
    }

    if (response) {
      if (response.status === 403) {
        throw new Error('Knowledge file API returned 403');
      }
      if (response.status < 500 && response.status !== 404) {
        throw new Error(`Knowledge file API returned ${response.status}`);
      }
    }
  } catch (error) {
    if (error instanceof MissionControlAuthError) {
      throw error;
    }
    if (error instanceof Error && /Knowledge file API returned/.test(error.message)) {
      throw error;
    }
  }

  const response = await fetch(apiUrl(`/knowledge/file?path=${encodeURIComponent(sourcePath)}`), {
    headers: buildHeaders(accessToken),
    cache: 'no-store',
  });

  if (response.status === 401) {
    throw new MissionControlAuthError();
  }

  if (!response.ok) {
    throw new Error(`Knowledge file API returned ${response.status}`);
  }

  const payload = await response.json() as MissionControlKnowledgeFilePayload;
  return {
    ...payload,
    path: redactHomePath(payload.path) ?? payload.path,
    sourcePath: redactHomePath(payload.sourcePath) ?? payload.sourcePath,
  };
}

export async function loadMissionControlTools(accessToken?: string): Promise<MissionControlToolsSnapshot> {
  const { payload } = await maybeFetchLocalJson<MissionControlToolsSnapshot>('/tools', accessToken);
  if (payload) return normalizeTools(payload);
  return fallbackTools;
}

export async function loadMissionControlSkills(accessToken?: string): Promise<MissionControlSkillsSnapshot> {
  const { payload } = await maybeFetchLocalJson<MissionControlSkillsSnapshot>('/skills', accessToken);
  if (payload) return normalizeSkills(payload);
  return fallbackSkills;
}

export async function loadMissionControlSkillsCatalog(
  accessToken?: string,
  options?: { query?: string; source?: string; limit?: number },
): Promise<MissionControlSkillsCatalogSnapshot> {
  const params = new URLSearchParams();
  if (options?.query) params.set('query', options.query);
  if (options?.source && options.source !== 'all') params.set('source', options.source);
  if (options?.limit != null) params.set('limit', String(options.limit));
  const query = params.toString();
  const { payload } = await maybeFetchLocalJson<MissionControlSkillsCatalogSnapshot>(`/skills/catalog${query ? '?' + query : ''}`, accessToken);
  return normalizeSkillsCatalog(payload ?? undefined);
}

export type InstallSkillResult = {
  success: boolean;
  skillName: string;
  identifier: string;
  installed: boolean;
  verified: boolean;
};

export async function installMissionControlSkill(
  identifier: string,
  accessToken?: string,
): Promise<InstallSkillResult | null> {
  try {
    const response = await fetch(localApiUrl('/skills/install'), {
      method: 'POST',
      headers: { ...buildHeaders(accessToken), 'Content-Type': 'application/json' },
      body: JSON.stringify({ identifier }),
    });
    if (response.status === 401) throw new MissionControlAuthError();
    if (!response.ok) return null;
    return await response.json() as InstallSkillResult;
  } catch (error) {
    if (error instanceof MissionControlAuthError) throw error;
    return null;
  }
}

export type ToggleSkillResult = {
  success: boolean;
  skillName: string;
  enabled: boolean;
  detail: string;
};

export async function toggleMissionControlSkill(
  skillName: string,
  enabled: boolean,
  accessToken?: string,
): Promise<ToggleSkillResult | null> {
  try {
    const response = await fetch(localApiUrl('/skills/toggle'), {
      method: 'POST',
      headers: { ...buildHeaders(accessToken), 'Content-Type': 'application/json' },
      body: JSON.stringify({ skillName, enabled }),
    });
    if (response.status === 401) throw new MissionControlAuthError();
    if (!response.ok) return null;
    return await response.json() as ToggleSkillResult;
  } catch (error) {
    if (error instanceof MissionControlAuthError) throw error;
    return null;
  }
}

export async function loadMissionControlSkillFiles(
  skillName: string,
  accessToken?: string,
): Promise<MissionControlSkillFilesPayload | null> {
  try {
    const { payload } = await maybeFetchLocalJson<MissionControlSkillFilesPayload>(
      `/skills/files?skill=${encodeURIComponent(skillName)}`,
      accessToken,
    );
    return payload;
  } catch {
    return null;
  }
}

export async function loadMissionControlConfig(accessToken?: string): Promise<MissionControlConfigSnapshot> {
  try {
    const { payload: configPayload } = await maybeFetchLocalJson<{ content?: string; hash?: string; path?: string; config?: Record<string, unknown> }>('/config', accessToken);

    if (!configPayload) {
      return fallbackConfig;
    }

    const content = typeof configPayload.content === 'string' ? configPayload.content : '';
    const configPath = configPayload.path ?? fallbackConfig.path;
    const parsedConfig = isRecord(configPayload.config) ? configPayload.config : {};

    return normalizeConfig({
      available: true,
      path: configPath,
      exists: Boolean(content.trim()) || Boolean(configPath),
      content,
      hash: typeof configPayload.hash === 'string' ? configPayload.hash : hashText(content),
      updatedAt: null,
      config: parsedConfig,
    });
  } catch (_error) {
    // Config is optional reference data. If the official dashboard auth drifts
    // after a restart, degrade here instead of locking the whole cockpit.
    return fallbackConfig;
  }
}

function inferLogLevel(line: string): MissionControlLogEntry['level'] {
  if (/(ERROR|CRITICAL)/.test(line)) return 'error';
  if (/WARNING/.test(line)) return 'warn';
  return 'info';
}

function buildLogFilePath(name: string): string {
  return `~/.hermes/logs/${name}`;
}

export async function loadMissionControlLogs(
  accessToken?: string,
  options?: { maxFiles?: number; maxLines?: number },
): Promise<MissionControlLogsSnapshot> {
  const params = new URLSearchParams();
  if (options?.maxFiles != null) {
    params.set('maxFiles', String(options.maxFiles));
  }
  if (options?.maxLines != null) {
    params.set('maxLines', String(options.maxLines));
  }
  const query = params.toString();
  const path = `/logs${query ? '?' + query : ''}`;
  const { payload } = await maybeFetchLocalJson<MissionControlLogsSnapshot>(path, accessToken);
  return normalizeLogs(payload);
}

export async function saveMissionControlConfig(accessToken: string | undefined, content: string, expectedHash?: string | null): Promise<MissionControlConfigSnapshot> {
  const response = await fetch(apiUrl('/config'), {
    method: 'PUT',
    headers: {
      ...buildHeaders(accessToken),
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({ content, hash: expectedHash ?? null }),
  });

  if (response.status === 401) {
    throw new MissionControlAuthError();
  }

  await parseResponse<Record<string, unknown>>(response, 'config save');
  return await loadMissionControlConfig(accessToken);
}

export function getFallbackSnapshot(): MissionControlSnapshot {
  return fallbackSnapshot;
}

export function getFallbackKnowledge(): MissionControlKnowledgeSnapshot {
  return fallbackKnowledge;
}

export function getFallbackTools(): MissionControlToolsSnapshot {
  return fallbackTools;
}

export function getFallbackSkills(): MissionControlSkillsSnapshot {
  return fallbackSkills;
}

export function getFallbackSkillsCatalog(): MissionControlSkillsCatalogSnapshot {
  return fallbackSkillsCatalog;
}

export function getFallbackConfig(): MissionControlConfigSnapshot {
  return fallbackConfig;
}

export function getFallbackCapabilities(): MissionControlCapabilities {
  return fallbackCapabilities;
}

// ---------- Nightly brain candidates ----------

export interface MissionControlCandidate {
  id: string;
  type: string;
  title: string;
  status: string;
  created: string;
  approved_at: string | null;
  rejected_at: string | null;
  rejection_reason: string;
  quarantine_until: string | null;
  body: string;
  _filename: string;
}

export interface MissionControlCandidatesSnapshot {
  candidates: MissionControlCandidate[];
  count: number;
}

export interface MissionControlVaultInfo {
  id: string;
  label: string;
  candidates_dir: string;
}

export async function loadMissionControlVaults(
  accessToken?: string,
): Promise<MissionControlVaultInfo[]> {
  const { payload } = await maybeFetchLocalJson<{ vaults: MissionControlVaultInfo[] }>(
    '/candidates/vaults',
    accessToken,
  );
  return payload?.vaults ?? [];
}

export async function loadMissionControlCandidates(
  accessToken?: string,
  status?: string,
  vault?: string,
): Promise<MissionControlCandidatesSnapshot> {
  const params = new URLSearchParams();
  if (status) params.set('status', status);
  if (vault) params.set('vault', vault);
  const qs = params.toString();
  const path = qs ? `/candidates?${qs}` : '/candidates';
  const { payload } = await maybeFetchLocalJson<MissionControlCandidatesSnapshot>(path, accessToken);
  return payload ?? { candidates: [], count: 0 };
}

export async function approveCandidate(
  accessToken: string | undefined,
  id: string,
  vault?: string,
  filename?: string,
): Promise<MissionControlCandidate | null> {
  const response = await fetch(apiUrl('/candidates/approve'), {
    method: 'POST',
    headers: { ...buildHeaders(accessToken), 'Content-Type': 'application/json' },
    body: JSON.stringify({ id, ...(vault ? { vault } : {}), ...(filename ? { filename } : {}) }),
  });
  if (response.status === 401) throw new MissionControlAuthError();
  if (!response.ok) {
    let detail = '';
    try {
      const payload = await response.json();
      detail = payload?.detail || payload?.error || '';
    } catch {
      // Keep the HTTP status as the useful fallback.
    }
    throw new Error(`Approve failed (${response.status})${detail ? `: ${detail}` : ''}`);
  }
  const data = await response.json();
  return data?.candidate ?? null;
}

export async function rejectCandidate(
  accessToken: string | undefined,
  id: string,
  reason: string,
  vault?: string,
  filename?: string,
): Promise<MissionControlCandidate | null> {
  const response = await fetch(apiUrl('/candidates/reject'), {
    method: 'POST',
    headers: { ...buildHeaders(accessToken), 'Content-Type': 'application/json' },
    body: JSON.stringify({ id, reason, ...(vault ? { vault } : {}), ...(filename ? { filename } : {}) }),
  });
  if (response.status === 401) throw new MissionControlAuthError();
  if (!response.ok) {
    let detail = '';
    try {
      const payload = await response.json();
      detail = payload?.detail || payload?.error || '';
    } catch {
      // Keep the HTTP status as the useful fallback.
    }
    throw new Error(`Reject failed (${response.status})${detail ? `: ${detail}` : ''}`);
  }
  const data = await response.json();
  return data?.candidate ?? null;
}

// ---------------------------------------------------------------------------
// Kanban (Mission Control → sidecar /api/local/kanban/* → core kanban_db)
// ---------------------------------------------------------------------------

export type MissionControlKanbanTask = {
  id: string;
  title: string;
  body?: string | null;
  status?: string;
  priority: number;
  assignee?: string | null;
  created_at?: number | null;
  comment_count?: number;
  parents?: string[];
  children?: string[];
  progress?: { done: number; total: number } | null;
};

export type MissionControlKanbanColumn = {
  name: string;
  tasks: MissionControlKanbanTask[];
};

export type MissionControlKanbanBoard = {
  columns: MissionControlKanbanColumn[];
  tenants: string[];
  assignees: string[];
  latestEventId: number;
  now: number;
};

export type MissionControlKanbanComment = {
  id: number;
  task_id: string;
  author: string;
  body: string;
  created_at: number;
};

export type MissionControlKanbanRun = {
  id: number;
  profile?: string | null;
  status: string;
  outcome?: string | null;
  summary?: string | null;
  error?: string | null;
  started_at: number;
  ended_at?: number | null;
};

export type MissionControlKanbanEventEntry = {
  id: number;
  kind: string;
  payload: Record<string, unknown> | null;
  created_at: number;
};

export type MissionControlKanbanTaskLog = {
  task_id: string;
  path: string;
  exists: boolean;
  size_bytes: number;
  content: string;
  truncated: boolean;
};

export type MissionControlKanbanTaskDetail = MissionControlKanbanTask & {
  body?: string | null;
  result?: string | null;
  latest_summary?: string | null;
  comments: MissionControlKanbanComment[];
  runs: MissionControlKanbanRun[];
  events: MissionControlKanbanEventEntry[];
};

export type MissionControlKanbanBoardMeta = {
  slug: string;
  name?: string | null;
  is_current?: boolean;
  total?: number;
  counts?: Record<string, number>;
  archived?: boolean;
};

export async function loadKanbanBoard(accessToken?: string, board?: string): Promise<MissionControlKanbanBoard> {
  const query = board ? `?board=${encodeURIComponent(board)}` : '';
  const { payload } = await maybeFetchLocalJson<MissionControlKanbanBoard>(`/kanban/board${query}`, accessToken);
  if (!payload) throw new Error('Kanban board unavailable.');
  return payload;
}

export async function loadKanbanBoards(accessToken?: string): Promise<{ boards: MissionControlKanbanBoardMeta[]; current: string }> {
  const { payload } = await maybeFetchLocalJson<{ boards: MissionControlKanbanBoardMeta[]; current: string }>('/kanban/boards', accessToken);
  if (!payload) throw new Error('Kanban boards unavailable.');
  return payload;
}

export async function loadKanbanTaskLog(accessToken?: string, taskId?: string, board?: string): Promise<MissionControlKanbanTaskLog> {
  if (!taskId) throw new Error('taskId is required');
  const params = new URLSearchParams({ tail: '100000' });
  if (board) params.set('board', board);
  const { payload } = await maybeFetchLocalJson<MissionControlKanbanTaskLog>(`/kanban/tasks/${encodeURIComponent(taskId)}/log?${params.toString()}`, accessToken);
  if (!payload) throw new Error('Worker log unavailable.');
  return payload;
}

export async function loadKanbanTaskDetail(accessToken?: string, taskId?: string, board?: string): Promise<MissionControlKanbanTaskDetail> {
  if (!taskId) throw new Error('taskId is required');
  const params = new URLSearchParams();
  if (board) params.set('board', board);
  const qs = params.toString() ? `?${params.toString()}` : '';
  const { payload } = await maybeFetchLocalJson<MissionControlKanbanTaskDetail>(`/kanban/tasks/${encodeURIComponent(taskId)}${qs}`, accessToken);
  if (!payload) throw new Error('Task unavailable.');
  return payload;
}

async function kanbanPost<T>(path: string, body: Record<string, unknown>, accessToken?: string): Promise<T> {
  const response = await fetch(localApiUrl(path), {
    method: 'POST',
    headers: { ...buildHeaders(accessToken), 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    cache: 'no-store',
  });
  if (!response.ok) {
    let detail = `HTTP ${response.status}`;
    try {
      const err = (await response.json()) as { detail?: string };
      if (err?.detail) detail = err.detail;
    } catch { /* ignore */ }
    throw new Error(detail);
  }
  return (await response.json()) as T;
}

export function moveKanbanTask(
  accessToken: string | undefined,
  taskId: string,
  status: string,
  board?: string,
): Promise<{ task: MissionControlKanbanTask | null }> {
  const qs = board ? `?board=${encodeURIComponent(board)}` : '';
  return kanbanPost(`/kanban/tasks/${encodeURIComponent(taskId)}${qs}`, { status }, accessToken);
}

export function archiveKanbanTask(
  accessToken: string | undefined,
  taskId: string,
  board?: string,
): Promise<{ ok: boolean }> {
  const qs = board ? `?board=${encodeURIComponent(board)}` : '';
  return kanbanPost(`/kanban/tasks/${encodeURIComponent(taskId)}/archive${qs}`, {}, accessToken);
}

export function patchKanbanTask(
  accessToken: string | undefined,
  taskId: string,
  input: { assignee?: string | null; priority?: number; title?: string; body?: string },
  board?: string,
): Promise<{ task: MissionControlKanbanTask | null }> {
  const qs = board ? `?board=${encodeURIComponent(board)}` : '';
  return kanbanPost(`/kanban/tasks/${encodeURIComponent(taskId)}${qs}`, input, accessToken);
}

export function linkKanbanTask(
  accessToken: string | undefined,
  taskId: string,
  parentId: string,
  remove = false,
  board?: string,
): Promise<{ ok: boolean }> {
  const qs = board ? `?board=${encodeURIComponent(board)}` : '';
  return kanbanPost(`/kanban/tasks/${encodeURIComponent(taskId)}/links${qs}`, { parent_id: parentId, remove }, accessToken);
}

export function createKanbanTask(
  accessToken: string | undefined,
  input: {
    title: string;
    body?: string;
    priority?: number;
    status?: string;
    assignee?: string;
    tenant?: string;
    skills?: string;
    parents?: string;
    workspace_kind?: string;
    workspace_path?: string;
    goal_mode?: boolean;
  },
  board?: string,
): Promise<{ id: string }> {
  const qs = board ? `?board=${encodeURIComponent(board)}` : '';
  return kanbanPost(`/kanban/tasks${qs}`, input, accessToken);
}

export function addKanbanComment(
  accessToken: string | undefined,
  taskId: string,
  body: string,
  board?: string,
): Promise<{ ok: boolean }> {
  const qs = board ? `?board=${encodeURIComponent(board)}` : '';
  return kanbanPost(`/kanban/tasks/${encodeURIComponent(taskId)}/comments${qs}`, { body }, accessToken);
}

export function createKanbanBoard(
  accessToken: string | undefined,
  input: { slug?: string; name?: string; description?: string; icon?: string; default_workdir?: string; switch?: boolean },
): Promise<{ board: MissionControlKanbanBoardMeta; current: string }> {
  return kanbanPost('/kanban/boards', input, accessToken);
}

export function deleteKanbanBoard(
  accessToken: string | undefined,
  slug: string,
  hard = false,
): Promise<{ result: Record<string, unknown>; current: string }> {
  return kanbanPost(`/kanban/boards/${encodeURIComponent(slug)}/delete`, { hard }, accessToken);
}

