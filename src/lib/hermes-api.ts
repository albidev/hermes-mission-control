export type MissionControlMachineStatus = {
  health: 'healthy' | 'degraded' | 'critical' | 'offline';
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
  summary: string;
};

export type MissionControlSessionItem = {
  id: string;
  source: string;
  model: string;
  title: string;
  startedAt: number;
  endedAt: number | null;
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
  mode: 'live' | 'post';
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
};

export type MissionControlCronJob = {
  id: string;
  label: string;
  enabled: boolean;
  state: string;
  scheduleDisplay: string;
  nextRunAt: string | null;
  lastRunAt: string | null;
  pausedReason: string | null;
  lastStatus: string | null;
  lastError: string | null;
  prompt: string;
  model: string;
  provider: string | null;
  skill: string | null;
  skills: string[];
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

export const MISSION_CONTROL_TOKEN_STORAGE_KEY = 'mission-control-token';

export class MissionControlAuthError extends Error {
  constructor(message = 'Mission Control requires an access token.') {
    super(message);
    this.name = 'MissionControlAuthError';
  }
}

const fallbackMachine: MissionControlMachineStatus = {
  health: 'degraded',
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
      endpoint: '/api/mission-control',
    },
  ],
};

const fallbackKnowledge: MissionControlKnowledgeSnapshot = {
  available: false,
  vaultPath: '~/Documents/Hermes',
  title: 'Knowledge Sharing',
  path: 'Knowledge Sharing.md',
  updatedAt: null,
  excerpt: 'Create MEMORY.md, USER.md, IDENTITY.md, and AGENTS.md in the vault to surface them here.',
  highlights: [],
  primary: {
    id: 'knowledge-sharing',
    title: 'Knowledge Sharing',
    path: 'Knowledge Sharing.md',
    updatedAt: null,
    excerpt: 'Create MEMORY.md, USER.md, IDENTITY.md, and AGENTS.md in the vault to surface them here.',
    highlights: [],
  },
  items: [],
  sections: [
    {
      id: 'memory',
      title: 'MEMORY.md',
      items: [],
    },
    {
      id: 'user',
      title: 'USER.md',
      items: [],
    },
    {
      id: 'identity',
      title: 'IDENTITY.md',
      items: [],
    },
    {
      id: 'agents',
      title: 'AGENTS.md',
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
  mode: 'post',
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
};

const fallbackSnapshot: MissionControlSnapshot = {
  backendHealth: 'healthy',
  activeModel: 'gpt-5.4-mini',
  fallbackModel: 'minimax-m2.7 @ localhost:8787',
  gatewayStatus: 'online',
  activeAgents: 3,
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
  return import.meta.env.VITE_HERMES_API_BASE_URL || '/api';
}

function apiUrl(path: string) {
  return `${apiBaseUrl().replace(/\/$/, '')}${path}`;
}

function buildHeaders(accessToken?: string): Record<string, string> {
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
  return value.replace(/^\/Users\/[^/]+/, '~');
}

function normalizeMachineStatus(input: Partial<MissionControlMachineStatus> | undefined): MissionControlMachineStatus {
  if (!input) return fallbackMachine;

  return {
    health: input.health ?? fallbackMachine.health,
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
    summary: input.summary ?? fallbackMachine.summary,
  };
}

function normalizeSessionItem(input: Partial<MissionControlSessionItem> | undefined): MissionControlSessionItem {
  return {
    id: input?.id ?? 'unknown-session',
    source: input?.source ?? 'unknown',
    model: input?.model ?? 'unknown',
    title: input?.title ?? 'Untitled session',
    startedAt: Number(input?.startedAt ?? 0),
    endedAt: input?.endedAt === null || input?.endedAt === undefined ? null : Number(input.endedAt),
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

function normalizeAgentTrace(input: Partial<MissionControlAgentTraceSnapshot> | undefined): MissionControlAgentTraceSnapshot {
  if (!input) return fallbackAgentTrace;

  return {
    success: Boolean(input.success ?? true),
    mode: input.mode === 'live' ? 'live' : 'post',
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
  };
}

function normalizeCronJob(input: Partial<MissionControlCronJob> | undefined): MissionControlCronJob {
  return {
    id: input?.id ?? 'scheduled-job',
    label: input?.label ?? input?.id ?? 'Scheduled job',
    enabled: input?.enabled ?? true,
    state: input?.state ?? 'scheduled',
    scheduleDisplay: input?.scheduleDisplay ?? 'unspecified',
    nextRunAt: input?.nextRunAt ?? null,
    lastRunAt: input?.lastRunAt ?? null,
    pausedReason: input?.pausedReason ?? null,
    lastStatus: input?.lastStatus ?? null,
    lastError: input?.lastError ?? null,
    prompt: input?.prompt ?? '',
    model: input?.model ?? 'unknown',
    provider: input?.provider ?? null,
    skill: input?.skill ?? null,
    skills: input?.skills ?? [],
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

export async function loadMissionControlSnapshot(accessToken?: string): Promise<MissionControlSnapshot> {
  try {
    const response = await fetch(apiUrl('/mission-control'), {
      headers: buildHeaders(accessToken),
    });

    if (response.status === 401) {
      throw new MissionControlAuthError();
    }

    const data = await parseResponse<Partial<MissionControlSnapshot>>(response, 'mission-control');
    return normalizeSnapshot(data);
  } catch (error) {
    if (error instanceof MissionControlAuthError) {
      throw error;
    }

    return fallbackSnapshot;
  }
}

export async function loadMissionControlMachineStatus(accessToken?: string): Promise<MissionControlMachineStatus> {
  try {
    const response = await fetch(apiUrl('/mission-control/system'), {
      headers: buildHeaders(accessToken),
    });

    if (response.status === 401) {
      throw new MissionControlAuthError();
    }

    const data = await parseResponse<Partial<MissionControlMachineStatus>>(response, 'machine');
    return normalizeMachineStatus(data);
  } catch (error) {
    if (error instanceof MissionControlAuthError) {
      throw error;
    }

    return fallbackMachine;
  }
}

export async function loadMissionControlSessions(accessToken?: string): Promise<MissionControlSessionsSnapshot> {
  try {
    const response = await fetch(apiUrl('/mission-control/sessions'), {
      headers: buildHeaders(accessToken),
    });

    if (response.status === 401) {
      throw new MissionControlAuthError();
    }

    const data = await parseResponse<Partial<MissionControlSessionsSnapshot>>(response, 'sessions');
    return normalizeSessions(data);
  } catch (error) {
    if (error instanceof MissionControlAuthError) {
      throw error;
    }

    return fallbackSessions;
  }
}

export async function loadMissionControlAgentTrace(
  sessionId?: string,
  accessToken?: string,
  limit = 300,
  compact = false,
): Promise<MissionControlAgentTraceSnapshot> {
  try {
    const params = new URLSearchParams();
    if (sessionId) params.set('session_id', sessionId);
    if (Number.isFinite(limit)) {
      const normalized = Math.floor(limit);
      if (normalized <= 0) {
        params.set('limit', '0');
      } else {
        params.set('limit', String(Math.max(50, Math.min(1000, normalized))));
      }
    }
    if (compact) {
      params.set('compact', '1');
    }

    const response = await fetch(apiUrl(`/mission-control/agents/trace${params.toString() ? `?${params.toString()}` : ''}`), {
      headers: buildHeaders(accessToken),
      credentials: 'include',
    });

    const data = await parseResponse<Partial<MissionControlAgentTraceSnapshot>>(response, 'agents trace');
    return normalizeAgentTrace(data);
  } catch (error) {
    if (error instanceof MissionControlAuthError) {
      throw error;
    }
    return fallbackAgentTrace;
  }
}

export async function loadMissionControlCron(accessToken?: string): Promise<MissionControlCronSnapshot> {
  try {
    const response = await fetch(apiUrl('/mission-control/cron'), {
      headers: buildHeaders(accessToken),
    });

    if (response.status === 401) {
      throw new MissionControlAuthError();
    }

    const data = await parseResponse<Partial<MissionControlCronSnapshot>>(response, 'cron');
    return normalizeCron(data);
  } catch (error) {
    if (error instanceof MissionControlAuthError) {
      throw error;
    }

    return fallbackCron;
  }
}

export async function loadMissionControlAlerts(accessToken?: string): Promise<MissionControlAlertsSnapshot> {
  try {
    const response = await fetch(apiUrl('/mission-control/alerts'), {
      headers: buildHeaders(accessToken),
    });

    if (response.status === 401) {
      throw new MissionControlAuthError();
    }

    const data = await parseResponse<Partial<MissionControlAlertsSnapshot>>(response, 'alerts');
    return normalizeAlerts(data);
  } catch (error) {
    if (error instanceof MissionControlAuthError) {
      throw error;
    }

    return fallbackAlerts;
  }
}

export async function loadMissionControlKnowledge(accessToken?: string): Promise<MissionControlKnowledgeSnapshot> {
  try {
    const response = await fetch(apiUrl('/mission-control/knowledge'), {
      headers: buildHeaders(accessToken),
    });

    if (response.status === 401) {
      throw new MissionControlAuthError();
    }

    const data = await parseResponse<Partial<MissionControlKnowledgeSnapshot>>(response, 'knowledge');
    return normalizeKnowledge(data);
  } catch (error) {
    if (error instanceof MissionControlAuthError) {
      throw error;
    }

    return fallbackKnowledge;
  }
}

export async function loadMissionControlKnowledgeFile(
  sourcePath: string,
  accessToken?: string,
): Promise<MissionControlKnowledgeFilePayload> {
  const params = new URLSearchParams({ source_path: sourcePath });
  const response = await fetch(apiUrl(`/mission-control/knowledge/file?${params.toString()}`), {
    headers: buildHeaders(accessToken),
  });

  if (response.status === 401) {
    throw new MissionControlAuthError();
  }

  return await parseResponse<MissionControlKnowledgeFilePayload>(response, 'knowledge file');
}

export async function loadMissionControlTools(accessToken?: string): Promise<MissionControlToolsSnapshot> {
  try {
    const response = await fetch(apiUrl('/mission-control/tools'), {
      headers: buildHeaders(accessToken),
    });

    if (response.status === 401) {
      throw new MissionControlAuthError();
    }

    const data = await parseResponse<Partial<MissionControlToolsSnapshot>>(response, 'tools');
    return normalizeTools(data);
  } catch (error) {
    if (error instanceof MissionControlAuthError) {
      throw error;
    }

    return fallbackTools;
  }
}

export async function loadMissionControlSkills(accessToken?: string): Promise<MissionControlSkillsSnapshot> {
  try {
    const response = await fetch(apiUrl('/mission-control/skills'), {
      headers: buildHeaders(accessToken),
    });

    if (response.status === 401) {
      throw new MissionControlAuthError();
    }

    const data = await parseResponse<Partial<MissionControlSkillsSnapshot>>(response, 'skills');
    return normalizeSkills(data);
  } catch (error) {
    if (error instanceof MissionControlAuthError) {
      throw error;
    }

    return fallbackSkills;
  }
}

export async function loadMissionControlConfig(accessToken?: string): Promise<MissionControlConfigSnapshot> {
  try {
    const response = await fetch(apiUrl('/mission-control/config'), {
      headers: buildHeaders(accessToken),
    });

    if (response.status === 401) {
      throw new MissionControlAuthError();
    }

    const data = await parseResponse<Partial<MissionControlConfigSnapshot>>(response, 'config');
    return normalizeConfig(data);
  } catch (error) {
    if (error instanceof MissionControlAuthError) {
      throw error;
    }

    return fallbackConfig;
  }
}

export async function loadMissionControlLogs(
  accessToken?: string,
  options?: { maxFiles?: number; maxLines?: number },
): Promise<MissionControlLogsSnapshot> {
  const params = new URLSearchParams();
  if (options?.maxFiles) params.set('max_files', String(options.maxFiles));
  if (options?.maxLines) params.set('max_lines', String(options.maxLines));
  const suffix = params.toString() ? `?${params.toString()}` : '';

  try {
    const response = await fetch(apiUrl(`/mission-control/logs${suffix}`), {
      headers: buildHeaders(accessToken),
    });

    if (response.status === 401) {
      throw new MissionControlAuthError();
    }

    const data = await parseResponse<Partial<MissionControlLogsSnapshot>>(response, 'logs');
    return normalizeLogs(data);
  } catch (error) {
    if (error instanceof MissionControlAuthError) {
      throw error;
    }

    return fallbackLogs;
  }
}

export async function saveMissionControlConfig(accessToken: string | undefined, content: string, expectedHash?: string | null): Promise<MissionControlConfigSnapshot> {
  const response = await fetch(apiUrl('/mission-control/config'), {
    method: 'PUT',
    headers: {
      ...buildHeaders(accessToken),
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({ content, expectedHash: expectedHash ?? undefined }),
  });

  if (response.status === 401) {
    throw new MissionControlAuthError();
  }

  const data = await parseResponse<Partial<MissionControlConfigSnapshot>>(response, 'config save');
  return normalizeConfig(data);
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

export function getFallbackConfig(): MissionControlConfigSnapshot {
  return fallbackConfig;
}
