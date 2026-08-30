import { useI18n } from '../lib/i18n';
import { useEffect, useMemo, useRef, useState } from 'react';
import { AlertTriangle, ArrowUp, FileText, Loader2, RefreshCw, TerminalSquare } from 'lucide-react';
import { Card } from '../components/ui/Card';
import { Badge } from '../components/ui/Badge';
import { Button } from '../components/ui/Button';
import { PageHeader } from '../components/PageHeader';
import {
  MissionControlAuthError,
  loadMissionControlLogs,
  type MissionControlLogsSnapshot,
} from '../lib/hermes-api';
import { useMissionControl } from '../lib/mission-control-store';
import { usePullToReload } from '../hooks/usePullToReload';
import { PullToReloadIndicator } from '../components/PullToReloadIndicator';

const LOGS_REFRESH_INTERVALS = [1000, 2000, 5000] as const;
type LogsRefreshInterval = (typeof LOGS_REFRESH_INTERVALS)[number];
const LOGS_REFRESH_STORAGE_KEY = 'mission-control-logs-refresh-interval';

function readRefreshInterval(): LogsRefreshInterval {
  try {
    const stored = Number(window.localStorage.getItem(LOGS_REFRESH_STORAGE_KEY));
    return LOGS_REFRESH_INTERVALS.includes(stored as LogsRefreshInterval) ? stored as LogsRefreshInterval : 5000;
  } catch {
    return 5000;
  }
}

function levelVariant(level: 'info' | 'warn' | 'error'): 'default' | 'warning' | 'negative' {
  if (level === 'error') return 'negative';
  if (level === 'warn') return 'warning';
  return 'default';
}

function formatTimestamp(value: string | null, unknownLabel: string, formatLabel: (time: string) => string): string {
  if (!value) return unknownLabel;
  const parsed = Date.parse(value);
  if (Number.isNaN(parsed)) return unknownLabel;
  return formatLabel(new Date(parsed).toLocaleString());
}

function MetricCard({
  icon: Icon,
  label,
  value,
  hint,
  color,
  className = '',
}: {
  icon: React.ElementType;
  label: string;
  value: string;
  hint: string;
  color: string;
  className?: string;
}) {
  return (
    <div className={`min-w-0 rounded-lg bg-surface-sunken/35 p-3 transition-colors hover:bg-surface-sunken/50 sm:p-4 ${className}`}>
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <span className="text-[11px] font-medium uppercase tracking-wide text-text-muted">{label}</span>
          <p className="mt-1 truncate text-xl font-semibold text-text tabular-nums">{value}</p>
          <p className="mt-1 truncate text-[11px] leading-relaxed text-text-subtle">{hint}</p>
        </div>
        <Icon className={`h-[18px] w-[18px] shrink-0 ${color}`} />
      </div>
    </div>
  );
}

export function LogsRoute() {
  const { t } = useI18n();
  const { storedToken } = useMissionControl();
  const [logs, setLogs] = useState<MissionControlLogsSnapshot | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedFileName, setSelectedFileName] = useState<string | null>(null);
  const [showScrollTop, setShowScrollTop] = useState(false);
  const [refreshInterval, setRefreshInterval] = useState<LogsRefreshInterval>(readRefreshInterval);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const selectedFileNameRef = useRef<string | null>(null);
  const refreshInFlightRef = useRef(false);

  useEffect(() => {
    selectedFileNameRef.current = selectedFileName;
  }, [selectedFileName]);

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    const updateScrollTopVisibility = () => {
      setShowScrollTop(container.scrollTop > Math.max(240, container.clientHeight * 0.35));
    };

    updateScrollTopVisibility();
    container.addEventListener('scroll', updateScrollTopVisibility, { passive: true });
    return () => container.removeEventListener('scroll', updateScrollTopVisibility);
  }, []);

  const { state: pullState } = usePullToReload({
    containerRef,
    onReload: async () => {
      try {
        const payload = await loadMissionControlLogs(storedToken || undefined, {
          maxFiles: 10,
          maxLines: 160,
        });
        setLogs(payload);
        setError(null);
      } catch (err) {
        if (err instanceof MissionControlAuthError) {
          setError(t('logs.authRequired'));
        } else {
          setError(err instanceof Error ? err.message : t('logs.failedLoad'));
        }
      }
    },
  });

  useEffect(() => {
    let cancelled = false;

    async function refreshLogs() {
      if (refreshInFlightRef.current) return;
      refreshInFlightRef.current = true;
      try {
        const payload = await loadMissionControlLogs(storedToken || undefined, {
          maxFiles: 10,
          maxLines: 160,
        });
        if (cancelled) return;
        const activeName = selectedFileNameRef.current;
        setLogs((current) => {
          if (!current || !activeName) return payload;
          const activeFile = payload.files.find((file) => file.name === activeName);
          if (!activeFile) return payload;
          return {
            ...payload,
            files: current.files.map((file) => file.name === activeName ? activeFile : file),
          };
        });
        setError(null);
      } catch (err) {
        if (cancelled) return;
        if (err instanceof MissionControlAuthError) {
          setError(t('logs.authRequired'));
        } else {
          setError(err instanceof Error ? err.message : t('logs.failedLoad'));
        }
      } finally {
        refreshInFlightRef.current = false;
        if (!cancelled) {
          setLoading(false);
        }
      }
    }

    void refreshLogs();
    const timer = window.setInterval(() => {
      if (document.visibilityState === 'visible') void refreshLogs();
    }, refreshInterval);

    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [refreshInterval, storedToken]);

  const files = logs?.files ?? [];

  const counters = useMemo(() => {
    let errors = 0;
    let warnings = 0;
    for (const file of files) {
      for (const entry of file.entries) {
        if (entry.level === 'error') errors += 1;
        if (entry.level === 'warn') warnings += 1;
      }
    }
    return { errors, warnings };
  }, [files]);

  const sortedFiles = useMemo(() => {
    return [...files].sort((a, b) => {
      const aTime = a.updatedAt ? Date.parse(a.updatedAt) : 0;
      const bTime = b.updatedAt ? Date.parse(b.updatedAt) : 0;
      return bTime - aTime;
    });
  }, [files]);

  const visibleFiles = useMemo(
    () => [...sortedFiles].sort((a, b) => a.name.localeCompare(b.name, undefined, { numeric: true })).slice(0, 10),
    [sortedFiles],
  );

  useEffect(() => {
    if (visibleFiles.length === 0) {
      setSelectedFileName(null);
      return;
    }

    if (!selectedFileName || !visibleFiles.some((file) => file.name === selectedFileName)) {
      setSelectedFileName(visibleFiles[0].name);
    }
  }, [visibleFiles, selectedFileName]);

  const activeFile = useMemo(
    () => visibleFiles.find((file) => file.name === selectedFileName) ?? visibleFiles[0] ?? null,
    [visibleFiles, selectedFileName],
  );

  const changeRefreshInterval = (next: LogsRefreshInterval) => {
    setRefreshInterval(next);
    try {
      window.localStorage.setItem(LOGS_REFRESH_STORAGE_KEY, String(next));
    } catch {
      // Storage can be unavailable in private browsing; the setting still applies in memory.
    }
  };

  return (
    <div ref={containerRef} className="logs-page route-page-scroll flex min-w-0 flex-col gap-5 h-full overflow-y-auto sm:gap-6">
      <PullToReloadIndicator state={pullState} />
      <PageHeader
        eyebrow={t('logs.eyebrow')}
        title={t('logs.title')}
        description={t('logs.description')}
        meta={(
          <Badge variant={logs?.available ? 'positive' : 'warning'}>
            {loading ? t('logs.loading') : logs?.available ? t('logs.live') : t('logs.unavailable')}
          </Badge>
        )}
      />

        <Card padding="none" className="!border-0">
        <div className="grid grid-cols-2 gap-2 p-3 sm:grid-cols-3 sm:gap-3 sm:p-4">
          <MetricCard
            icon={AlertTriangle}
            label={t('logs.errors')}
            value={String(counters.errors)}
            hint={t('logs.inCurrentTail')}
            color="text-negative"
          />
          <MetricCard
            icon={FileText}
            label={t('logs.warnings')}
            value={String(counters.warnings)}
            hint={t('logs.inCurrentTail')}
            color="text-warning"
          />
          <MetricCard
            icon={RefreshCw}
            label={t('logs.files')}
            value={String(logs?.fileCount ?? 0)}
            hint={logs?.path ?? t('logs.path')}
            color="text-text-subtle"
            className="hidden sm:block"
          />
        </div>
        </Card>

        {error ? (
        <Card className="min-w-0 border-negative/30 bg-negative/5 p-4">
          <p className="text-sm text-negative">{error}</p>
        </Card>
        ) : null}

        <Card padding="none" className="min-w-0 overflow-hidden">
        <div className="flex flex-col gap-2 border-b border-border px-4 pb-3 pt-4 sm:flex-row sm:items-center sm:justify-between">
          <div className="flex flex-col gap-0.5">
            <span className="eyebrow">{t('logs.filters')}</span>
            <h3 className="text-sm font-semibold text-text">{t('logs.streamTitle')}</h3>
          </div>
          <div className="logs-refresh-control">
            <label htmlFor="logs-auto-refresh" className="sr-only">{t('logs.autoRefreshLabel')}</label>
            <select
              id="logs-auto-refresh"
              value={refreshInterval}
              onChange={(event) => {
                const next = Number(event.target.value);
                if (!LOGS_REFRESH_INTERVALS.includes(next as LogsRefreshInterval)) return;
                changeRefreshInterval(next as LogsRefreshInterval);
              }}
              className="logs-refresh-select"
            >
              <option value={1000}>{t('logs.autoRefresh1')}</option>
              <option value={2000}>{t('logs.autoRefresh2')}</option>
              <option value={5000}>{t('logs.autoRefresh5')}</option>
            </select>
          </div>
        </div>

        {loading && !logs ? (
          <div className="flex items-center justify-center gap-2 px-4 py-10 text-sm text-text-muted" role="status" aria-live="polite" aria-busy="true">
            <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
            <span>{t('logs.loading')}</span>
          </div>
        ) : visibleFiles.length > 0 && activeFile ? (
          <div className="flex min-w-0 flex-col gap-4 p-3 sm:p-4">
            <div className="flex min-w-0 flex-nowrap gap-1.5 overflow-x-auto pb-1 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden sm:flex-wrap sm:overflow-visible sm:pb-0">
              {visibleFiles.map((file) => {
                const selected = file.name === activeFile.name;
                return (
                  <Button
                    key={file.name}
                    size="sm"
                    variant={selected ? 'primary' : 'secondary'}
                    className="shrink-0 whitespace-nowrap"
                    onClick={() => setSelectedFileName(file.name)}
                  >
                    {file.name}
                  </Button>
                );
              })}
            </div>

            <Card variant="sunken" padding="none" className="min-w-0 overflow-hidden">
              <div className="border-b border-border bg-surface-raised/70 px-3 py-3 sm:px-4">
                <div className="flex items-center justify-between gap-2">
                  <p className="text-sm font-medium text-text truncate">{activeFile.name}</p>
                  <Badge variant="default" className="shrink-0">{t('logs.lines', { count: activeFile.entryCount })}</Badge>
                </div>
                <div className="mt-1 flex min-w-0 items-center justify-between gap-2">
                  <p className="text-xs text-text-subtle truncate min-w-0">{activeFile.path}</p>
                  <span className="shrink-0 text-[11px] text-text-subtle">{formatTimestamp(activeFile.updatedAt, t('logs.updatedUnknown'), (time) => t('logs.updated', { time }))}</span>
                </div>
              </div>

              <div className="divide-y divide-border">
                {activeFile.entries.length > 0 ? (
                  [...activeFile.entries]
                    .sort((a, b) => b.lineNumber - a.lineNumber)
                    .slice(0, 160)
                    .map((entry) => (
                      <div key={`${activeFile.name}-${entry.lineNumber}`} className="flex items-start gap-3 px-3 py-2.5 sm:px-4">
                        <TerminalSquare className="mt-0.5 h-4 w-4 shrink-0 text-text-muted" />
                        <div className="min-w-0 flex-1">
                          <div className="mb-1 flex items-center justify-between gap-2">
                            <span className="text-xs text-text-subtle">{t('logs.line', { number: entry.lineNumber })}</span>
                            <Badge variant={levelVariant(entry.level)}>{entry.level}</Badge>
                          </div>
                          <p className="text-xs text-text-muted whitespace-pre-wrap break-words font-mono leading-relaxed">
                            {entry.text}
                          </p>
                        </div>
                      </div>
                    ))
                ) : (
                  <div className="px-4 py-6 text-sm text-text-muted italic">{t('logs.noLines')}</div>
                )}
              </div>
            </Card>
          </div>
        ) : (
          <div className="px-4 py-8 text-center text-sm text-text-muted italic">{t('logs.noFiles')}</div>
        )}
        </Card>

      {showScrollTop ? (
        <Button
          variant="primary"
          size="md"
          icon={<ArrowUp className="h-5 w-5" />}
          iconOnly
          className="logs-scroll-top shadow-lg"
          type="button"
          aria-label={t('logs.scrollToTop')}
          title={t('logs.scrollToTop')}
          onClick={() => containerRef.current?.scrollTo({ top: 0, behavior: 'smooth' })}
        />
      ) : null}
    </div>
  );
}
