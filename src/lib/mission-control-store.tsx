import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react';
import {
  getFallbackConfig,
  getFallbackKnowledge,
  getFallbackSkills,
  getFallbackSnapshot,
  getFallbackTools,
  loadMissionControlAlerts,
  loadMissionControlConfig,
  loadMissionControlKnowledge,
  loadMissionControlMachineStatus,
  loadMissionControlCron,
  loadMissionControlSessions,
  loadMissionControlSkills,
  loadMissionControlSnapshot,
  loadMissionControlTools,
  saveMissionControlConfig,
  MissionControlAuthError,
  MISSION_CONTROL_TOKEN_STORAGE_KEY,
  type MissionControlConfigSnapshot,
  type MissionControlKnowledgeSnapshot,
  type MissionControlSessionsSnapshot,
  type MissionControlSkillsSnapshot,
  type MissionControlSnapshot,
  type MissionControlToolsSnapshot,
} from './hermes-api';

export type ThemeMode = 'dark' | 'light' | 'system';
export type ResolvedTheme = 'dark' | 'light';

export type MissionControlGatewayAction = {
  id: 'refresh' | 'reload-config' | 'restart-gateway' | 'probe-health';
  label: string;
  hint: string;
  endpoint: string;
  method: 'GET' | 'POST';
};

type MissionControlActionResult = {
  label: string;
  endpoint: string;
  payload: string;
};

type MissionControlContextValue = {
  snapshot: MissionControlSnapshot;
  knowledge: MissionControlKnowledgeSnapshot;
  tools: MissionControlToolsSnapshot;
  skills: MissionControlSkillsSnapshot;
  config: MissionControlConfigSnapshot;
  loading: boolean;
  authRequired: boolean;
  authError: string | null;
  storedToken: string;
  tokenDraft: string;
  setTokenDraft: (value: string) => void;
  refreshAll: (token?: string, options?: { silent?: boolean }) => Promise<void>;
  unlock: (token: string) => Promise<void>;
  logout: () => void;
  actionResult: MissionControlActionResult | null;
  actionLoading: string | null;
  gatewayActions: MissionControlGatewayAction[];
  runGatewayAction: (action: MissionControlGatewayAction) => Promise<void>;
  reloadConfig: () => Promise<MissionControlConfigSnapshot>;
  saveConfig: (content: string, expectedHash?: string | null) => Promise<MissionControlConfigSnapshot>;
  linkStatus: string | null;
  setLinkStatus: (value: string | null) => void;
  lastUpdatedAt: string | null;
  theme: ThemeMode;
  setTheme: (theme: ThemeMode) => void;
  resolvedTheme: ResolvedTheme;
};

const MissionControlContext = createContext<MissionControlContextValue | null>(null);

function readStoredValue(key: string, fallback: string) {
  try {
    return window.localStorage.getItem(key) ?? fallback;
  } catch {
    return fallback;
  }
}

function persistStoredValue(key: string, value: string) {
  try {
    if (value) {
      window.localStorage.setItem(key, value);
    } else {
      window.localStorage.removeItem(key);
    }
  } catch {
    // Ignore storage failures; browsers are allowed to be dramatic.
  }
}

function getApiBaseUrl() {
  return import.meta.env.VITE_HERMES_API_BASE_URL || '/api';
}

function buildHeaders(token?: string) {
  const headers: Record<string, string> = { Accept: 'application/json' };
  if (token) {
    headers.Authorization = `Bearer ${token.trim()}`;
  }
  return headers;
}

function resolveTheme(theme: ThemeMode, systemTheme: ResolvedTheme): ResolvedTheme {
  return theme === 'system' ? systemTheme : theme;
}

function readJsonPayload<T>(payload: unknown): T {
  if (typeof payload === 'string') {
    return JSON.parse(payload) as T;
  }
  return payload as T;
}

export function MissionControlProvider({ children }: { children: ReactNode }) {
  const initialToken = typeof window === 'undefined' ? '' : readStoredValue(MISSION_CONTROL_TOKEN_STORAGE_KEY, '');
  const initialTheme = typeof window === 'undefined' ? 'system' : (readStoredValue('mission-control-theme', 'system') as ThemeMode);

  const [snapshot, setSnapshot] = useState<MissionControlSnapshot>(getFallbackSnapshot());
  const [knowledge, setKnowledge] = useState<MissionControlKnowledgeSnapshot>(getFallbackKnowledge());
  const [tools, setTools] = useState<MissionControlToolsSnapshot>(getFallbackTools());
  const [skills, setSkills] = useState<MissionControlSkillsSnapshot>(getFallbackSkills());
  const [config, setConfig] = useState<MissionControlConfigSnapshot>(getFallbackConfig());
  const [loading, setLoading] = useState(true);
  const [authRequired, setAuthRequired] = useState(false);
  const [authError, setAuthError] = useState<string | null>(null);
  const [storedToken, setStoredToken] = useState(initialToken);
  const [tokenDraft, setTokenDraft] = useState(initialToken);
  const [actionResult, setActionResult] = useState<MissionControlActionResult | null>(null);
  const [actionLoading, setActionLoading] = useState<string | null>(null);
  const [linkStatus, setLinkStatus] = useState<string | null>(null);
  const [lastUpdatedAt, setLastUpdatedAt] = useState<string | null>(null);
  const [theme, setThemeState] = useState<ThemeMode>(initialTheme);
  const [systemTheme, setSystemTheme] = useState<ResolvedTheme>('dark');

  const gatewayActions = useMemo<MissionControlGatewayAction[]>(
    () => [
      { id: 'refresh', label: 'Refresh snapshot', hint: 'Reload all live Mission Control data.', endpoint: '/api/mission-control', method: 'GET' },
      { id: 'reload-config', label: 'Reload config', hint: 'Re-read config.yaml from disk.', endpoint: '/api/mission-control/config', method: 'GET' },
      { id: 'probe-health', label: 'Probe gateway', hint: 'Hit /health to verify the gateway.', endpoint: '/health', method: 'GET' },
      { id: 'restart-gateway', label: 'Restart gateway', hint: 'Request a safe process restart.', endpoint: '/api/mission-control/restart', method: 'POST' },
    ],
    [],
  );

  useEffect(() => {
    if (typeof window === 'undefined') {
      return;
    }

    const media = window.matchMedia('(prefers-color-scheme: dark)');
    const apply = () => setSystemTheme(media.matches ? 'dark' : 'light');
    apply();
    media.addEventListener('change', apply);
    return () => media.removeEventListener('change', apply);
  }, []);

  useEffect(() => {
    if (typeof window === 'undefined') {
      return;
    }

    persistStoredValue('mission-control-theme', theme);
  }, [theme]);

  const resolvedTheme = useMemo(() => resolveTheme(theme, systemTheme), [systemTheme, theme]);

  useEffect(() => {
    if (typeof document === 'undefined') {
      return;
    }

    document.documentElement.dataset.theme = resolvedTheme;
    document.documentElement.style.colorScheme = resolvedTheme;
  }, [resolvedTheme]);

  const refreshConfig = useCallback(async (token?: string) => {
    const updated = await loadMissionControlConfig(token);
    setConfig(updated);
    return updated;
  }, []);

  const refreshAll = useCallback(async (token?: string, options?: { silent?: boolean; includeReference?: boolean }) => {
    const silent = options?.silent ?? false;
    const includeReference = options?.includeReference ?? true;
    if (!silent) {
      setLoading(true);
    }

    try {
      const coreRequests = [
        loadMissionControlSnapshot(token),
        loadMissionControlMachineStatus(token),
        loadMissionControlSessions(token),
        loadMissionControlCron(token),
        loadMissionControlAlerts(token),
      ];

      const referenceRequests = includeReference
        ? [loadMissionControlKnowledge(token), loadMissionControlTools(token), loadMissionControlSkills(token)]
        : [];

      const [dashboard, machine, sessions, cron, alerts, ...referenceData] = await Promise.all([
        ...coreRequests,
        ...referenceRequests,
      ]);

      setSnapshot({
        ...dashboard as MissionControlSnapshot,
        machine: machine as MissionControlSnapshot['machine'],
        sessions: sessions as MissionControlSnapshot['sessions'],
        cron: cron as MissionControlSnapshot['cron'],
        alerts: alerts as MissionControlSnapshot['alerts'],
      });

      if (includeReference && referenceData.length === 3) {
        const [knowledgeSnapshot, toolsSnapshot, skillsSnapshot] = referenceData;
        setKnowledge(knowledgeSnapshot as Awaited<ReturnType<typeof loadMissionControlKnowledge>>);
        setTools(toolsSnapshot as Awaited<ReturnType<typeof loadMissionControlTools>>);
        setSkills(skillsSnapshot as Awaited<ReturnType<typeof loadMissionControlSkills>>);
      }

      setAuthRequired(false);
      setAuthError(null);
      setLastUpdatedAt(new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }));
    } catch (error) {
      if (error instanceof MissionControlAuthError) {
        setAuthRequired(true);
        setAuthError('Access token required to enter the cockpit.');
      } else {
        setAuthRequired(false);
        setAuthError(null);
      }

      setSnapshot(getFallbackSnapshot());
      if (includeReference) {
        setKnowledge(getFallbackKnowledge());
        setTools(getFallbackTools());
        setSkills(getFallbackSkills());
      }
    } finally {
      if (!silent) {
        setLoading(false);
      }
    }
  }, []);

  useEffect(() => {
    void refreshAll(initialToken || undefined);
    void refreshConfig(initialToken || undefined);
    // Initial boot only.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (authRequired) {
      return;
    }

    const interval = window.setInterval(() => {
      void refreshAll(storedToken || undefined, { silent: true, includeReference: false });
    }, 15000);

    return () => window.clearInterval(interval);
  }, [authRequired, refreshAll, storedToken]);

  const unlock = useCallback(async (token: string) => {
    const nextToken = token.trim();
    setStoredToken(nextToken);
    setTokenDraft(nextToken);
    persistStoredValue(MISSION_CONTROL_TOKEN_STORAGE_KEY, nextToken);
    await Promise.all([
      refreshAll(nextToken || undefined),
      refreshConfig(nextToken || undefined),
    ]);
  }, [refreshAll, refreshConfig]);

  const logout = useCallback(() => {
    setTokenDraft('');
    setStoredToken('');
    persistStoredValue(MISSION_CONTROL_TOKEN_STORAGE_KEY, '');
    setAuthRequired(true);
    setAuthError('Logged out. Re-enter the access token to unlock the cockpit.');
    setSnapshot(getFallbackSnapshot());
    setKnowledge(getFallbackKnowledge());
    setTools(getFallbackTools());
    setSkills(getFallbackSkills());
    setConfig(getFallbackConfig());
    setActionResult(null);
    setLinkStatus(null);
  }, []);

  const reloadConfig = useCallback(async () => {
    const updated = await loadMissionControlConfig(storedToken || undefined);
    setConfig(updated);
    return updated;
  }, [storedToken]);

  const saveConfig = useCallback(async (content: string, expectedHash?: string | null) => {
    const updated = await saveMissionControlConfig(storedToken || undefined, content, expectedHash ?? config.hash ?? undefined);
    setConfig(updated);
    return updated;
  }, [config.hash, storedToken]);

  const runGatewayAction = useCallback(async (action: MissionControlGatewayAction) => {
    const token = storedToken.trim();
    const baseUrl = getApiBaseUrl().replace(/\/$/, '');

    setActionLoading(action.id);
    setActionResult(null);

    try {
      if (action.id === 'refresh') {
        await refreshAll(token || undefined);
        setActionResult({
          label: action.label,
          endpoint: action.endpoint,
          payload: 'Dashboard refreshed from live endpoints.',
        });
        return;
      }

      if (action.id === 'reload-config') {
        const updated = await loadMissionControlConfig(token || undefined);
        setConfig(updated);
        setActionResult({
          label: action.label,
          endpoint: action.endpoint,
          payload: `Reloaded config.yaml (${updated.path}).`,
        });
        return;
      }

      const response = await fetch(`${baseUrl}${action.endpoint}`, {
        method: action.method,
        headers: buildHeaders(token || undefined),
      });

      if (response.status === 401) {
        throw new MissionControlAuthError();
      }

      if (!response.ok) {
        throw new Error(`${action.endpoint} returned ${response.status}`);
      }

      const contentType = response.headers.get('content-type') ?? '';
      const payload = contentType.includes('application/json') ? await response.json() : await response.text();
      const result = readJsonPayload<unknown>(payload);
      setActionResult({
        label: action.label,
        endpoint: action.endpoint,
        payload: typeof result === 'string' ? result : JSON.stringify(result, null, 2),
      });
    } catch (error) {
      const message = error instanceof Error ? error.message : 'Unknown action failure';
      setActionResult({
        label: action.label,
        endpoint: action.endpoint,
        payload: message,
      });

      if (error instanceof MissionControlAuthError) {
        setAuthRequired(true);
        setAuthError('Access token required to keep using Mission Control.');
      }
    } finally {
      setActionLoading(null);
    }
  }, [refreshAll, storedToken]);

  const value = useMemo<MissionControlContextValue>(() => ({
    snapshot,
    knowledge,
    tools,
    skills,
    config,
    loading,
    authRequired,
    authError,
    storedToken,
    tokenDraft,
    setTokenDraft,
    refreshAll,
    unlock,
    logout,
    actionResult,
    actionLoading,
    gatewayActions,
    runGatewayAction,
    reloadConfig,
    saveConfig,
    linkStatus,
    setLinkStatus,
    lastUpdatedAt,
    theme,
    setTheme: setThemeState,
    resolvedTheme,
  }), [
    actionLoading,
    actionResult,
    authError,
    authRequired,
    config,
    gatewayActions,
    knowledge,
    lastUpdatedAt,
    linkStatus,
    loading,
    logout,
    refreshAll,
    reloadConfig,
    resolvedTheme,
    runGatewayAction,
    saveConfig,
    snapshot,
    skills,
    storedToken,
    theme,
    tokenDraft,
    tools,
    unlock,
  ]);

  return <MissionControlContext.Provider value={value}>{children}</MissionControlContext.Provider>;
}

export function useMissionControl() {
  const context = useContext(MissionControlContext);
  if (!context) {
    throw new Error('useMissionControl must be used inside MissionControlProvider');
  }
  return context;
}

export function useMissionControlSelection<T extends { id: string }>(items: T[], paramName: string) {
  const [selectedId, setSelectedId] = useState<string | null>(() => {
    if (typeof window === 'undefined') return null;
    return new URLSearchParams(window.location.search).get(paramName);
  });

  useEffect(() => {
    if (typeof window === 'undefined') {
      return;
    }

    const handlePopState = () => {
      setSelectedId(new URLSearchParams(window.location.search).get(paramName));
    };

    window.addEventListener('popstate', handlePopState);
    return () => window.removeEventListener('popstate', handlePopState);
  }, [paramName]);

  useEffect(() => {
    if (typeof window === 'undefined') {
      return;
    }

    const url = new URL(window.location.href);
    if (selectedId) {
      url.searchParams.set(paramName, selectedId);
    } else {
      url.searchParams.delete(paramName);
    }
    window.history.replaceState({}, '', `${url.pathname}${url.search}${url.hash}`);
  }, [paramName, selectedId]);

  useEffect(() => {
    if (selectedId && items.length > 0 && !items.some((item) => item.id === selectedId)) {
      setSelectedId(items[0]?.id ?? null);
    }
  }, [items, selectedId]);

  return {
    selectedId,
    setSelectedId,
    selectedItem: items.find((item) => item.id === selectedId) ?? items[0] ?? null,
  };
}
