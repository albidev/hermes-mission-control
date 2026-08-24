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
  refreshAll: (token?: string, options?: { silent?: boolean; includeReference?: boolean; includeSnapshot?: boolean; includeSessions?: boolean }) => Promise<void>;
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
  return import.meta.env.VITE_MISSION_CONTROL_LOCAL_API_BASE_URL || '/api/local';
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
  const envToken = (typeof import.meta.env !== 'undefined' && import.meta.env.VITE_MISSION_CONTROL_TOKEN) || '';
  const initialToken = typeof window === 'undefined' ? '' : readStoredValue(MISSION_CONTROL_TOKEN_STORAGE_KEY, envToken);
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
      { id: 'refresh', label: 'Refresh snapshot', hint: 'Reload all live Mission Control data.', endpoint: '/status', method: 'GET' },
      { id: 'reload-config', label: 'Reload config', hint: 'Re-read config.yaml from disk.', endpoint: '/config', method: 'GET' },
      { id: 'probe-health', label: 'Probe gateway', hint: 'Hit /api/local/status to verify the telemetry backend.', endpoint: '/status', method: 'GET' },
      { id: 'restart-gateway', label: 'Restart gateway', hint: 'Request a safe process restart.', endpoint: '/gateway/restart', method: 'POST' },
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

  const refreshReferenceData = useCallback(async (token?: string) => {
    const [knowledgeRes, toolsRes, skillsRes] = await Promise.allSettled([
      loadMissionControlKnowledge(token),
      loadMissionControlTools(token),
      loadMissionControlSkills(token),
    ]);

    if (knowledgeRes.status === 'fulfilled') {
      const nextKnowledge = knowledgeRes.value;
      setKnowledge((previous) => (nextKnowledge.available ? nextKnowledge : previous));
    }

    if (toolsRes.status === 'fulfilled') {
      const nextTools = toolsRes.value;
      setTools((previous) => (nextTools.available ? nextTools : previous));
    }

    if (skillsRes.status === 'fulfilled') {
      const nextSkills = skillsRes.value;
      setSkills((previous) => (nextSkills.available ? nextSkills : previous));
    }
  }, []);

  const refreshAll = useCallback(async (token?: string, options?: { silent?: boolean; includeReference?: boolean; includeSnapshot?: boolean; includeSessions?: boolean }) => {
    const silent = options?.silent ?? false;
    const includeReference = options?.includeReference ?? true;
    const includeSnapshot = options?.includeSnapshot ?? !silent;
    const includeSessions = options?.includeSessions ?? !silent;
    if (!silent) {
      setLoading(true);
    }

    if (includeReference) {
      void refreshReferenceData(token);
    }

    try {
      const updateMachine = loadMissionControlMachineStatus(token).then((machine) => {
        setSnapshot((previous) => {
          const nextMachine = machine as MissionControlSnapshot['machine'];
          const machineValue = nextMachine.source === 'fallback' && previous.machine.source !== 'fallback' ? previous.machine : nextMachine;
          return { ...previous, machine: machineValue };
        });
      });

      const updateSessions = includeSessions
        ? loadMissionControlSessions(token).then((sessions) => {
            setSnapshot((previous) => {
              const nextSessions = sessions as MissionControlSnapshot['sessions'];
              const sessionsValue = nextSessions.totalSessions === 0 && previous.sessions.totalSessions > 0 ? previous.sessions : nextSessions;
              return {
                ...previous,
                sessions: sessionsValue,
                activeAgents: sessionsValue.activeAgents,
              };
            });
          })
        : Promise.resolve();

      const updateCron = loadMissionControlCron(token).then((cron) => {
        setSnapshot((previous) => {
          const nextCron = cron as MissionControlSnapshot['cron'];
          const cronValue = nextCron.items.length === 0 && previous.cron.items.length > 0 ? previous.cron : nextCron;
          return {
            ...previous,
            cron: cronValue,
            queuedJobs: cronValue.queuedJobs,
          };
        });
      });

      const updateAlerts = loadMissionControlAlerts(token).then((alerts) => {
        setSnapshot((previous) => {
          const nextAlerts = alerts as MissionControlSnapshot['alerts'];
          const alertsValue =
            nextAlerts.items.length === 1 && nextAlerts.items[0]?.id === 'fallback-gateway' && previous.alerts.items.length > 0
              ? previous.alerts
              : nextAlerts;
          return { ...previous, alerts: alertsValue };
        });
      });

      const liveResults = await Promise.allSettled([updateMachine, updateSessions, updateCron, updateAlerts]);
      const authFailure = liveResults.find(
        (result): result is PromiseRejectedResult => result.status === 'rejected' && result.reason instanceof MissionControlAuthError,
      );
      if (authFailure) {
        throw authFailure.reason;
      }

      if (includeSnapshot) {
        try {
          const dashboard = await loadMissionControlSnapshot(token);
          setSnapshot((previous) => ({
            ...previous,
            backendHealth: dashboard.backendHealth,
            activeModel: dashboard.activeModel,
            fallbackModel: dashboard.fallbackModel,
            gatewayStatus: dashboard.gatewayStatus,
            activeAgents: dashboard.activeAgents,
            candidatesEnabled: dashboard.candidatesEnabled,
            queuedJobs: dashboard.queuedJobs,
            toolCallsToday: dashboard.toolCallsToday,
            recentSignals: dashboard.recentSignals,
            knowledgeSharing: dashboard.knowledgeSharing,
          }));
        } catch (error) {
          if (error instanceof MissionControlAuthError) {
            throw error;
          }
        }
      }

      setAuthRequired(false);
      setAuthError(null);
      setLastUpdatedAt(new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }));
    } catch (error) {
      if (error instanceof MissionControlAuthError) {
        setAuthRequired(true);
        setAuthError('Access token required to enter the cockpit.');

        // Auth failures should lock the UI and scrub live state.
        setSnapshot(getFallbackSnapshot());
        if (includeReference) {
          setKnowledge(getFallbackKnowledge());
          setTools(getFallbackTools());
          setSkills(getFallbackSkills());
        }
      } else {
        // Transient network/backend hiccups should NOT clobber already-live data.
        setAuthRequired(false);
        setAuthError(null);
      }
    } finally {
      if (!silent) {
        setLoading(false);
      }
    }
  }, [refreshReferenceData]);

  useEffect(() => {
    void refreshAll(initialToken || undefined);
    void refreshConfig(initialToken || undefined).catch((error) => {
      if (error instanceof MissionControlAuthError) {
        // Config is optional. Keep the cockpit live even if the config surface rejects auth.
        setAuthRequired(false);
        setAuthError(null);
        setConfig(getFallbackConfig());
      }
    });
    // Initial boot only.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (authRequired) {
      return;
    }

    let ticks = 0;
    const interval = window.setInterval(() => {
      if (document.visibilityState !== 'visible') return;
      ticks += 1;
      const includeReference = ticks % 4 === 0 || !tools.available || !skills.available || !knowledge.available;
      const includeSnapshot = ticks % 4 === 0 || snapshot.activeModel === 'gpt-5.4-mini';
      const includeConfig = ticks % 4 === 0 || !config.available;
      void refreshAll(storedToken || undefined, { silent: true, includeReference, includeSnapshot });
      if (includeConfig) {
        void refreshConfig(storedToken || undefined).catch(() => {});
      }
    }, 15000);

    return () => window.clearInterval(interval);
  }, [authRequired, config.available, knowledge.available, refreshAll, refreshConfig, skills.available, snapshot.activeModel, storedToken, tools.available]);

  useEffect(() => {
    if (authRequired || typeof document === 'undefined' || typeof window === 'undefined') {
      return;
    }

    const refreshAfterWake = () => {
      if (document.visibilityState !== 'visible') {
        return;
      }
      void refreshAll(storedToken || undefined, { silent: true, includeReference: true, includeSnapshot: true });
    };

    const refreshAfterOnline = () => {
      void refreshAll(storedToken || undefined, { silent: true, includeReference: true, includeSnapshot: true });
    };

    document.addEventListener('visibilitychange', refreshAfterWake);
    window.addEventListener('online', refreshAfterOnline);

    return () => {
      document.removeEventListener('visibilitychange', refreshAfterWake);
      window.removeEventListener('online', refreshAfterOnline);
    };
  }, [authRequired, refreshAll, storedToken]);

  const unlock = useCallback(async (token: string) => {
    const nextToken = token.trim();
    setStoredToken(nextToken);
    setTokenDraft(nextToken);
    persistStoredValue(MISSION_CONTROL_TOKEN_STORAGE_KEY, nextToken);
    await refreshAll(nextToken || undefined);
    await refreshConfig(nextToken || undefined).catch((error) => {
      if (error instanceof MissionControlAuthError) {
        setConfig(getFallbackConfig());
        return;
      }
      throw error;
    });
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
