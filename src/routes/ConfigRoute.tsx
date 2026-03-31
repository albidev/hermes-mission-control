import { useEffect, useMemo, useState } from 'react';
import { FilePenLine, Hash, Server, Settings2 } from 'lucide-react';
import { Card } from '../components/ui/Card';
import { Badge } from '../components/ui/Badge';
import { Button } from '../components/ui/Button';
import { useMissionControl } from '../lib/mission-control-store';

function formatConfigValue(value: unknown) {
  if (typeof value === 'string') return value;
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  if (value === null || value === undefined) return 'n/a';
  try {
    return JSON.stringify(value);
  } catch {
    return 'n/a';
  }
}

function MetricCard({
  icon: Icon,
  label,
  value,
  hint,
}: {
  icon: React.ElementType;
  label: string;
  value: string;
  hint: string;
}) {
  return (
    <Card className="p-4">
      <div className="flex items-center justify-between">
        <span className="text-xs text-text-muted">{label}</span>
        <Icon className="h-4 w-4 text-text-subtle" />
      </div>
      <p className="text-sm font-semibold text-text mt-2 break-all">{value}</p>
      <p className="text-xs text-text-subtle mt-1">{hint}</p>
    </Card>
  );
}

export function ConfigRoute() {
  const { config, theme, resolvedTheme, snapshot, reloadConfig, saveConfig } = useMissionControl();
  const [draft, setDraft] = useState(config.content);
  const [status, setStatus] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const dirty = useMemo(() => draft !== config.content, [draft, config.content]);
  const configEntries = useMemo(() => Object.entries(config.config ?? {}), [config.config]);

  useEffect(() => {
    setDraft(config.content);
  }, [config.content]);

  const handleSave = async () => {
    setSaving(true);
    setStatus(null);
    try {
      const updated = await saveConfig(draft, config.hash);
      setDraft(updated.content);
      setStatus(`Saved ${updated.path}.`);
    } catch (error) {
      setStatus(error instanceof Error ? error.message : 'Failed to save config.');
    } finally {
      setSaving(false);
    }
  };

  const handleReload = async () => {
    setSaving(true);
    setStatus(null);
    try {
      const updated = await reloadConfig();
      setDraft(updated.content);
      setStatus(`Reloaded ${updated.path}.`);
    } catch (error) {
      setStatus(error instanceof Error ? error.message : 'Failed to reload config.');
    } finally {
      setSaving(false);
    }
  };

  const handleReset = () => {
    setDraft(config.content);
    setStatus('Reset draft to the last loaded file.');
  };

  return (
    <div className="flex flex-col gap-6">
      <Card padding="none">
        <div className="px-4 pt-4 pb-3 border-b border-border-subtle flex items-center justify-between">
          <div className="flex flex-col gap-0.5">
            <span className="eyebrow">Config</span>
            <h2 className="text-sm font-semibold text-text">config.yaml editor</h2>
          </div>
          <Badge variant={config.available ? 'positive' : 'warning'}>
            {config.available ? 'live endpoint' : 'fallback'}
          </Badge>
        </div>

        <div className="p-4 grid grid-cols-1 md:grid-cols-2 xl:grid-cols-4 gap-3">
          <MetricCard icon={FilePenLine} label="Path" value={config.path} hint={config.exists ? 'file exists' : 'missing'} />
          <MetricCard icon={Hash} label="Hash" value={config.hash || 'n/a'} hint="optimistic lock token" />
          <MetricCard icon={Settings2} label="Entries" value={String(configEntries.length)} hint="top-level keys" />
          <MetricCard icon={Server} label="Gateway" value={snapshot.gatewayStatus} hint={`theme ${theme} · ${resolvedTheme}`} />
        </div>
      </Card>

      <div className="grid grid-cols-1 xl:grid-cols-3 gap-6">
        <Card className="xl:col-span-2" padding="none">
          <div className="px-4 pt-4 pb-3 border-b border-border-subtle flex items-center justify-between">
            <div className="flex flex-col gap-0.5">
              <span className="eyebrow">Editor</span>
              <h3 className="text-sm font-semibold text-text">config.yaml</h3>
            </div>
            {dirty ? <Badge variant="warning">unsaved changes</Badge> : <Badge variant="default">in sync</Badge>}
          </div>

          <form className="p-4 flex flex-col gap-3" onSubmit={(event) => { event.preventDefault(); void handleSave(); }}>
            <textarea
              className="w-full min-h-[420px] rounded-lg border border-border bg-surface p-3 text-sm font-mono text-text resize-y"
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              spellCheck={false}
            />

            {status ? <p className="text-xs text-text-muted">{status}</p> : null}

            <div className="flex flex-wrap gap-2">
              <Button type="submit" variant="primary" disabled={saving || !dirty} loading={saving}>Save config</Button>
              <Button type="button" variant="secondary" onClick={() => void handleReload()} disabled={saving}>Reload</Button>
              <Button type="button" variant="ghost" onClick={handleReset} disabled={saving || !dirty}>Reset draft</Button>
            </div>
          </form>
        </Card>

        <Card padding="none">
          <div className="px-4 pt-4 pb-3 border-b border-border-subtle">
            <span className="eyebrow">Parsed view</span>
            <h3 className="text-sm font-semibold text-text mt-0.5">Current settings</h3>
          </div>

          <div className="divide-y divide-border-subtle max-h-[640px] overflow-y-auto">
            {configEntries.length > 0 ? (
              configEntries.map(([key, value]) => (
                <div key={key} className="px-4 py-3">
                  <p className="text-xs font-medium text-text">{key}</p>
                  <p className="text-xs text-text-muted mt-1 break-words">{formatConfigValue(value)}</p>
                </div>
              ))
            ) : (
              <div className="px-4 py-8 text-center text-sm text-text-muted italic">No parsed keys.</div>
            )}
          </div>
        </Card>
      </div>
    </div>
  );
}
