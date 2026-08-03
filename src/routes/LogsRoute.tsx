import { useEffect, useMemo, useRef, useState } from 'react';
import { AlertTriangle, FileText, RefreshCw, TerminalSquare } from 'lucide-react';
import { Card } from '../components/ui/Card';
import { Badge } from '../components/ui/Badge';
import { Button } from '../components/ui/Button';
import {
  MissionControlAuthError,
  loadMissionControlLogs,
  type MissionControlLogsSnapshot,
} from '../lib/hermes-api';
import { useMissionControl } from '../lib/mission-control-store';
import { usePullToReload } from '../hooks/usePullToReload';
import { PullToReloadIndicator } from '../components/PullToReloadIndicator';

function levelVariant(level: 'info' | 'warn' | 'error'): 'default' | 'warning' | 'negative' {
  if (level === 'error') return 'negative';
  if (level === 'warn') return 'warning';
  return 'default';
}

function formatTimestamp(value: string | null): string {
  if (!value) return 'updated unknown';
  const parsed = Date.parse(value);
  if (Number.isNaN(parsed)) return 'updated unknown';
  return `updated ${new Date(parsed).toLocaleString()}`;
}

export function LogsRoute() {
  const { storedToken } = useMissionControl();
  const [logs, setLogs] = useState<MissionControlLogsSnapshot | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedFileName, setSelectedFileName] = useState<string | null>(null);
  const containerRef = useRef<HTMLDivElement | null>(null);

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
          setError('Access token required to read log files.');
        } else {
          setError(err instanceof Error ? err.message : 'Failed to load logs.');
        }
      }
    },
  });

  useEffect(() => {
    let cancelled = false;

    async function refreshLogs() {
      try {
        const payload = await loadMissionControlLogs(storedToken || undefined, {
          maxFiles: 10,
          maxLines: 160,
        });
        if (cancelled) return;
        setLogs(payload);
        setError(null);
      } catch (err) {
        if (cancelled) return;
        if (err instanceof MissionControlAuthError) {
          setError('Access token required to read log files.');
        } else {
          setError(err instanceof Error ? err.message : 'Failed to load logs.');
        }
      } finally {
        if (!cancelled) {
          setLoading(false);
        }
      }
    }

    void refreshLogs();
    const timer = window.setInterval(() => void refreshLogs(), 5000);

    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [storedToken]);

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

  const visibleFiles = useMemo(() => sortedFiles.slice(0, 10), [sortedFiles]);

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

  return (
    <div ref={containerRef} className="flex flex-col gap-6 h-full overflow-y-auto">
      <PullToReloadIndicator state={pullState} />
      <Card padding="none">
        <div className="px-4 pt-4 pb-3 border-b border-border-subtle flex items-center justify-between">
          <div className="flex flex-col gap-0.5">
            <span className="eyebrow">Logs</span>
            <h2 className="text-sm font-semibold text-text">Live tail from ~/.hermes/logs</h2>
          </div>
          <Badge variant={logs?.available ? 'positive' : 'warning'}>
            {loading ? 'loading' : logs?.available ? 'live' : 'unavailable'}
          </Badge>
        </div>

        <div className="p-4 grid grid-cols-1 md:grid-cols-3 gap-3">
          <Card className="p-4">
            <div className="flex items-center justify-between">
              <span className="text-xs text-text-muted">Errors</span>
              <AlertTriangle className="h-4 w-4 text-negative" />
            </div>
            <p className="text-lg font-semibold text-text mt-2">{counters.errors}</p>
            <p className="text-xs text-text-subtle mt-1">in current tail window</p>
          </Card>

          <Card className="p-4">
            <div className="flex items-center justify-between">
              <span className="text-xs text-text-muted">Warnings</span>
              <FileText className="h-4 w-4 text-warning" />
            </div>
            <p className="text-lg font-semibold text-text mt-2">{counters.warnings}</p>
            <p className="text-xs text-text-subtle mt-1">in current tail window</p>
          </Card>

          <Card className="p-4">
            <div className="flex items-center justify-between">
              <span className="text-xs text-text-muted">Files</span>
              <RefreshCw className="h-4 w-4 text-text-subtle" />
            </div>
            <p className="text-lg font-semibold text-text mt-2">{logs?.fileCount ?? 0}</p>
            <p className="text-xs text-text-subtle mt-1">{logs?.path ?? '~/.hermes/logs'}</p>
          </Card>
        </div>
      </Card>

      {error ? (
        <Card className="p-4">
          <p className="text-sm text-negative">{error}</p>
        </Card>
      ) : null}

      <Card padding="none">
        <div className="px-4 pt-4 pb-3 border-b border-border-subtle flex items-center justify-between">
          <div className="flex flex-col gap-0.5">
            <span className="eyebrow">Filters</span>
            <h3 className="text-sm font-semibold text-text">Single full-width stream with log switcher</h3>
          </div>
          <span className="text-xs text-text-subtle">auto-refresh 5s</span>
        </div>

        {visibleFiles.length > 0 && activeFile ? (
          <div className="p-4 flex flex-col gap-4">
            <div className="flex flex-wrap gap-2">
              {visibleFiles.map((file) => {
                const selected = file.name === activeFile.name;
                return (
                  <Button
                    key={file.name}
                    size="sm"
                    variant={selected ? 'primary' : 'secondary'}
                    onClick={() => setSelectedFileName(file.name)}
                  >
                    {file.name}
                  </Button>
                );
              })}
            </div>

            <Card variant="sunken" padding="none" className="min-w-0 overflow-hidden">
              <div className="px-4 py-3 border-b border-border-subtle bg-surface-raised/70">
                <div className="flex items-center justify-between gap-2">
                  <p className="text-sm font-medium text-text truncate">{activeFile.name}</p>
                  <Badge variant="default" className="shrink-0">{activeFile.entryCount} lines</Badge>
                </div>
                <div className="mt-1 flex items-center justify-between gap-2">
                  <p className="text-xs text-text-subtle truncate min-w-0">{activeFile.path}</p>
                  <span className="text-[11px] text-text-subtle shrink-0">{formatTimestamp(activeFile.updatedAt)}</span>
                </div>
              </div>

              <div className="mobile-log-stream max-h-[700px] overflow-y-auto divide-y divide-border-subtle">
                {activeFile.entries.length > 0 ? (
                  [...activeFile.entries]
                    .sort((a, b) => b.lineNumber - a.lineNumber)
                    .slice(0, 160)
                    .map((entry) => (
                      <div key={`${activeFile.name}-${entry.lineNumber}`} className="px-4 py-2.5 flex items-start gap-3">
                        <TerminalSquare className="h-4 w-4 mt-0.5 text-text-muted flex-shrink-0" />
                        <div className="min-w-0 flex-1">
                          <div className="flex items-center justify-between gap-2 mb-1">
                            <span className="text-xs text-text-subtle">line {entry.lineNumber}</span>
                            <Badge variant={levelVariant(entry.level)}>{entry.level}</Badge>
                          </div>
                          <p className="text-xs text-text-muted whitespace-pre-wrap break-words font-mono leading-relaxed">
                            {entry.text}
                          </p>
                        </div>
                      </div>
                    ))
                ) : (
                  <div className="px-4 py-6 text-sm text-text-muted italic">No lines captured for this file.</div>
                )}
              </div>
            </Card>
          </div>
        ) : (
          <div className="px-4 py-8 text-center text-sm text-text-muted italic">No log files found.</div>
        )}
      </Card>
    </div>
  );
}
