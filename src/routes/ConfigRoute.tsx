import { useI18n } from '../lib/i18n';
import { useEffect, useMemo, useRef, useState } from 'react';
import { FilePenLine, Hash, Server, Settings2 } from 'lucide-react';
import { parse as parseYaml, parseDocument, stringify as stringifyYaml } from 'yaml';
import { Card } from '../components/ui/Card';
import { Badge } from '../components/ui/Badge';
import { Button } from '../components/ui/Button';
import { useMissionControl } from '../lib/mission-control-store';
import { usePullToReload } from '../hooks/usePullToReload';
import { PullToReloadIndicator } from '../components/PullToReloadIndicator';

type PathSegment = string | number;
type PendingEdit = { path: PathSegment[]; value: unknown };

function formatConfigValue(value: unknown) {
  const { t } = useI18n();
  if (typeof value === 'string') return value;
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  if (value === null || value === undefined) return 'n/a';
  try {
    return JSON.stringify(value);
  } catch {
    return 'n/a';
  }
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function cloneValue<T>(value: T): T {
  try {
    return structuredClone(value);
  } catch {
    return JSON.parse(JSON.stringify(value)) as T;
  }
}

function setAtPath(root: Record<string, unknown>, path: PathSegment[], nextValue: unknown): Record<string, unknown> {
  const nextRoot = cloneValue(root);
  let cursor: Record<string, unknown> | unknown[] = nextRoot;

  for (let index = 0; index < path.length - 1; index += 1) {
    const key = path[index];
    if (Array.isArray(cursor)) {
      const arrayIndex = Number(key);
      const current = cursor[arrayIndex];
      if (!isPlainObject(current) && !Array.isArray(current)) {
        cursor[arrayIndex] = typeof path[index + 1] === 'number' ? [] : {};
      }
      cursor = cursor[arrayIndex] as Record<string, unknown> | unknown[];
      continue;
    }

    const current = cursor[key as string];
    if (!isPlainObject(current) && !Array.isArray(current)) {
      cursor[key as string] = typeof path[index + 1] === 'number' ? [] : {};
    }
    cursor = cursor[key as string] as Record<string, unknown> | unknown[];
  }

  const leaf = path[path.length - 1];
  if (Array.isArray(cursor)) {
    cursor[Number(leaf)] = nextValue;
  } else {
    cursor[String(leaf)] = nextValue;
  }

  return nextRoot;
}

function pathLabel(path: PathSegment[]) {
  const key = String(path[path.length - 1] ?? 'field');
  return key.replace(/[_-]+/g, ' ').trim();
}

function pathKey(path: PathSegment[]) {
  return path.map((segment) => String(segment)).join('.');
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
  const { t } = useI18n();
  const { config, theme, resolvedTheme, snapshot, reloadConfig, saveConfig } = useMissionControl();
  const [draft, setDraft] = useState(config.content);
  const [status, setStatus] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [editorMode, setEditorMode] = useState<'yaml' | 'form'>('form');
  const [formState, setFormState] = useState<Record<string, unknown>>(config.config ?? {});
  const [complexDrafts, setComplexDrafts] = useState<Record<string, string>>({});
  const [pendingEdits, setPendingEdits] = useState<Record<string, PendingEdit>>({});
  const [searchQuery, setSearchQuery] = useState('');
  const [showChangedOnly, setShowChangedOnly] = useState(false);
  const [sectionOpen, setSectionOpen] = useState<Record<string, boolean>>({});
  const containerRef = useRef<HTMLDivElement | null>(null);

  const { state: pullState } = usePullToReload({
    containerRef,
    onReload: async () => {
      setSaving(true);
      setStatus(null);
      try {
        const updated = await reloadConfig();
        setDraft(updated.content);
        setFormState(isPlainObject(updated.config) ? (updated.config as Record<string, unknown>) : {});
        setComplexDrafts({});
        setPendingEdits({});
        setStatus(`Reloaded ${updated.path}.`);
      } catch (error) {
        setStatus(error instanceof Error ? error.message : 'Failed to reload config.');
      } finally {
        setSaving(false);
      }
    },
  });

  const dirty = useMemo(() => draft !== config.content || Object.keys(pendingEdits).length > 0, [draft, config.content, pendingEdits]);
  const formSections = useMemo(() => Object.entries(formState ?? {}), [formState]);
  const changedPathKeys = useMemo(() => new Set(Object.keys(pendingEdits)), [pendingEdits]);
  const searchLower = useMemo(() => searchQuery.trim().toLowerCase(), [searchQuery]);
  const changedSectionCounts = useMemo(() => {
    const counts: Record<string, number> = {};
    for (const path of Object.keys(pendingEdits)) {
      const section = path.split('.')[0] ?? 'unknown';
      counts[section] = (counts[section] ?? 0) + 1;
    }
    return counts;
  }, [pendingEdits]);

  useEffect(() => {
    setDraft(config.content);
    setFormState(isPlainObject(config.config) ? (config.config as Record<string, unknown>) : {});
    setComplexDrafts({});
    setPendingEdits({});
    setSearchQuery('');
    setShowChangedOnly(false);
  }, [config]);

  useEffect(() => {
    setSectionOpen((previous) => {
      const next: Record<string, boolean> = {};
      for (const [section] of formSections) {
        next[section] = previous[section] ?? true;
      }
      return next;
    });
  }, [formSections]);

  const updateFormValue = (path: PathSegment[], nextValue: unknown) => {
    const editKey = pathKey(path);
    setFormState((previous) => setAtPath(previous, path, nextValue));
    setPendingEdits((previous) => ({
      ...previous,
      [editKey]: { path, value: nextValue },
    }));
  };

  const handleYamlChange = (nextYaml: string) => {
    setDraft(nextYaml);
    try {
      const parsed = parseYaml(nextYaml);
      if (isPlainObject(parsed)) {
        setFormState(parsed);
        setPendingEdits({});
      }
    } catch {
      // Keep the current schema form state while the user types invalid YAML.
    }
  };

  const handleSave = async () => {
    setSaving(true);
    setStatus(null);
    try {
      let payloadYaml = draft;

      if (editorMode === 'form' && Object.keys(pendingEdits).length > 0) {
        const doc = parseDocument(draft);
        if (doc.errors.length > 0) {
          throw new Error('Current YAML has parse errors. Fix YAML mode first, then save form changes.');
        }

        for (const edit of Object.values(pendingEdits)) {
          doc.setIn(edit.path, edit.value);
        }

        payloadYaml = String(doc);
      }

      const updated = await saveConfig(payloadYaml, config.hash);
      setDraft(updated.content);
      setFormState(isPlainObject(updated.config) ? (updated.config as Record<string, unknown>) : {});
      setComplexDrafts({});
      setPendingEdits({});
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
      setFormState(isPlainObject(updated.config) ? (updated.config as Record<string, unknown>) : {});
      setComplexDrafts({});
      setPendingEdits({});
      setStatus(`Reloaded ${updated.path}.`);
    } catch (error) {
      setStatus(error instanceof Error ? error.message : 'Failed to reload config.');
    } finally {
      setSaving(false);
    }
  };

  const handleReset = () => {
    setDraft(config.content);
    setFormState(isPlainObject(config.config) ? (config.config as Record<string, unknown>) : {});
    setComplexDrafts({});
    setPendingEdits({});
    setStatus('Reset draft to the last loaded file.');
  };

  const sectionDomId = (section: string) => `config-section-${section.replace(/[^a-zA-Z0-9_-]/g, '-')}`;

  const isPathChanged = (path: PathSegment[]) => {
    const key = pathKey(path);
    if (changedPathKeys.has(key)) return true;
    const prefix = `${key}.`;
    for (const changed of changedPathKeys) {
      if (changed.startsWith(prefix)) return true;
    }
    return false;
  };

  const toggleSection = (section: string) => {
    setSectionOpen((previous) => ({ ...previous, [section]: !previous[section] }));
  };

  const setAllSectionsOpen = (open: boolean) => {
    const next: Record<string, boolean> = {};
    for (const [section] of formSections) {
      next[section] = open;
    }
    setSectionOpen(next);
  };

  const jumpToSection = (section: string) => {
    const node = document.getElementById(sectionDomId(section));
    if (node) {
      node.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
    setSectionOpen((previous) => ({ ...previous, [section]: true }));
  };

  const renderField = (path: PathSegment[], value: unknown, depth = 0): React.ReactNode | null => {
    const key = pathKey(path);
    const label = pathLabel(path);
    const changed = isPathChanged(path);
    const valuePreview = formatConfigValue(value).toLowerCase();
    const matchesQuery =
      searchLower.length === 0 ||
      key.toLowerCase().includes(searchLower) ||
      label.toLowerCase().includes(searchLower) ||
      valuePreview.includes(searchLower);

    if (isPlainObject(value)) {
      const entries = Object.entries(value)
        .map(([childKey, childValue]) => renderField([...path, childKey], childValue, depth + 1))
        .filter(Boolean);

      if (entries.length === 0 && (showChangedOnly || searchLower.length > 0) && !changed && !matchesQuery) {
        return null;
      }

      if (depth === 0) {
        return <>{entries}</>;
      }

      return (
        <div key={key} className={`rounded-lg border border-border-subtle bg-surface-elevated/30 ${depth > 0 ? 'p-3' : 'p-4'}`}>
          <div className="flex items-center justify-between gap-2">
            <p className="text-xs font-semibold text-text uppercase tracking-wide">{label}</p>
            {changed ? <Badge variant="warning">{t('config.changed')}</Badge> : null}
          </div>
          <div className="mt-3 flex flex-col gap-3">
            {entries.length > 0 ? entries : <p className="text-xs text-text-muted italic">{t('config.emptyObject')}</p>}
          </div>
        </div>
      );
    }

    if (Array.isArray(value)) {
      if ((showChangedOnly && !changed) || (searchLower.length > 0 && !matchesQuery && !changed)) {
        return null;
      }

      const staged = complexDrafts[key] ?? stringifyYaml(value).trim();
      return (
        <div key={key} className="rounded-lg border border-border-subtle bg-surface-elevated/20 p-3">
          <div className="flex items-center justify-between gap-2">
            <label className="text-xs font-medium text-text">{label}</label>
            {changed ? <Badge variant="warning">{t('config.changed')}</Badge> : null}
          </div>
          <p className="text-[11px] text-text-subtle mt-1">{t('config.arrayEditor')}</p>
          <textarea
            className="mt-2 w-full min-h-[120px] rounded-md border border-border bg-surface p-2 text-xs font-mono text-text resize-y"
            value={staged}
            spellCheck={false}
            onChange={(event) => {
              const nextText = event.target.value;
              setComplexDrafts((previous) => ({ ...previous, [key]: nextText }));
            }}
            onBlur={(event) => {
              const nextText = event.target.value;
              try {
                const parsed = parseYaml(nextText);
                if (!Array.isArray(parsed)) {
                  throw new Error('Expected an array.');
                }
                updateFormValue(path, parsed);
                setComplexDrafts((previous) => {
                  const next = { ...previous };
                  delete next[key];
                  return next;
                });
                setStatus(null);
              } catch (error) {
                setStatus(error instanceof Error ? `${label}: ${error.message}` : `${label}: invalid YAML array.`);
              }
            }}
          />
        </div>
      );
    }

    if (typeof value === 'boolean') {
      if ((showChangedOnly && !changed) || (searchLower.length > 0 && !matchesQuery && !changed)) {
        return null;
      }

      return (
        <div key={key} className="rounded-lg border border-border-subtle bg-surface-elevated/20 p-3 flex items-center justify-between gap-3">
          <div>
            <div className="flex items-center gap-2">
              <p className="text-xs font-medium text-text">{label}</p>
              {changed ? <Badge variant="warning">{t('config.changed')}</Badge> : null}
            </div>
            <p className="text-[11px] text-text-subtle">{t('config.boolean')}</p>
          </div>
          <label className="inline-flex items-center gap-2 text-xs text-text-muted">
            <input
              type="checkbox"
              checked={value}
              onChange={(event) => updateFormValue(path, event.target.checked)}
            />
            {value ? 'enabled' : 'disabled'}
          </label>
        </div>
      );
    }

    if (typeof value === 'number') {
      if ((showChangedOnly && !changed) || (searchLower.length > 0 && !matchesQuery && !changed)) {
        return null;
      }

      return (
        <div key={key} className="rounded-lg border border-border-subtle bg-surface-elevated/20 p-3">
          <div className="flex items-center gap-2">
            <label className="text-xs font-medium text-text" htmlFor={key}>{label}</label>
            {changed ? <Badge variant="warning">{t('config.changed')}</Badge> : null}
          </div>
          <input
            id={key}
            type="number"
            className="mt-2 w-full rounded-md border border-border bg-surface p-2 text-xs text-text"
            value={Number.isFinite(value) ? String(value) : ''}
            onChange={(event) => {
              const nextRaw = event.target.value;
              const nextNumber = Number(nextRaw);
              if (!Number.isNaN(nextNumber)) {
                updateFormValue(path, nextNumber);
              }
            }}
          />
        </div>
      );
    }

    if (value === null || value === undefined) {
      if ((showChangedOnly && !changed) || (searchLower.length > 0 && !matchesQuery && !changed)) {
        return null;
      }

      return (
        <div key={key} className="rounded-lg border border-border-subtle bg-surface-elevated/20 p-3">
          <div className="flex items-center gap-2">
            <label className="text-xs font-medium text-text" htmlFor={key}>{label}</label>
            {changed ? <Badge variant="warning">{t('config.changed')}</Badge> : null}
          </div>
          <input
            id={key}
            type="text"
            className="mt-2 w-full rounded-md border border-border bg-surface p-2 text-xs text-text"
            value=""
            placeholder="null"
            onChange={(event) => updateFormValue(path, event.target.value)}
          />
        </div>
      );
    }

    const textValue = String(value);
    const multiline = textValue.includes('\n') || textValue.length > 100;
    if ((showChangedOnly && !changed) || (searchLower.length > 0 && !matchesQuery && !changed)) {
      return null;
    }

    return (
      <div key={key} className="rounded-lg border border-border-subtle bg-surface-elevated/20 p-3">
        <div className="flex items-center gap-2">
          <label className="text-xs font-medium text-text" htmlFor={key}>{label}</label>
          {changed ? <Badge variant="warning">{t('config.changed')}</Badge> : null}
        </div>
        {multiline ? (
          <textarea
            id={key}
            className="mt-2 w-full min-h-[84px] rounded-md border border-border bg-surface p-2 text-xs text-text resize-y"
            value={textValue}
            onChange={(event) => updateFormValue(path, event.target.value)}
          />
        ) : (
          <input
            id={key}
            type="text"
            className="mt-2 w-full rounded-md border border-border bg-surface p-2 text-xs text-text"
            value={textValue}
            onChange={(event) => updateFormValue(path, event.target.value)}
          />
        )}
      </div>
    );
  };

  return (
    <div ref={containerRef} className="flex flex-col gap-6 h-full overflow-y-auto">
      <PullToReloadIndicator state={pullState} />
      <Card padding="none">
        <div className="px-4 pt-4 pb-3 border-b border-border-subtle flex items-center justify-between">
          <div className="flex flex-col gap-0.5">
            <span className="eyebrow">{t('nav.config')}</span>
            <h2 className="text-sm font-semibold text-text">{t('config.title')}</h2>
          </div>
          <Badge variant={config.available ? 'positive' : 'warning'}>
            {config.available ? 'live endpoint' : 'fallback'}
          </Badge>
        </div>

        <div className="p-4 grid grid-cols-1 md:grid-cols-2 xl:grid-cols-4 gap-3">
          <MetricCard icon={FilePenLine} label="Path" value={config.path} hint={config.exists ? 'file exists' : 'missing'} />
          <MetricCard icon={Hash} label="Hash" value={config.hash || 'n/a'} hint="optimistic lock token" />
          <MetricCard icon={Settings2} label="Sections" value={String(formSections.length)} hint="top-level keys" />
          <MetricCard icon={Server} label="Gateway" value={snapshot.gatewayStatus} hint={`theme ${theme} · ${resolvedTheme}`} />
        </div>
      </Card>

      <div className="flex items-center justify-between gap-3">
        <div className="inline-flex items-center rounded-lg border border-border-subtle bg-surface p-1">
          <button
            type="button"
            className={`px-3 py-1.5 text-xs rounded-md transition ${editorMode === 'form' ? 'bg-surface-elevated text-text' : 'text-text-muted hover:text-text'}`}
            onClick={() => setEditorMode('form')}
          >
            Form mode
          </button>
          <button
            type="button"
            className={`px-3 py-1.5 text-xs rounded-md transition ${editorMode === 'yaml' ? 'bg-surface-elevated text-text' : 'text-text-muted hover:text-text'}`}
            onClick={() => setEditorMode('yaml')}
          >
            YAML mode
          </button>
        </div>
        <span className="text-xs text-text-subtle">Showing {editorMode === 'yaml' ? 'raw YAML editor' : 'schema-driven form'}</span>
      </div>

      {editorMode === 'yaml' ? (
        <Card padding="none">
          <div className="px-4 pt-4 pb-3 border-b border-border-subtle flex items-center justify-between">
            <div className="flex flex-col gap-0.5">
              <span className="eyebrow">{t('config.editor')}</span>
              <h3 className="text-sm font-semibold text-text">config.yaml</h3>
            </div>
            {dirty ? <Badge variant="warning">{t('config.unsavedChanges')}</Badge> : <Badge variant="default">{t('config.inSync')}</Badge>}
          </div>

          <form className="p-4 flex flex-col gap-3" onSubmit={(event) => { event.preventDefault(); void handleSave(); }}>
            <textarea
              className="w-full mobile-config-editor min-h-[620px] rounded-lg border border-border bg-surface p-3 text-sm font-mono text-text resize-y"
              value={draft}
              onChange={(event) => handleYamlChange(event.target.value)}
              spellCheck={false}
            />

            {status ? <p className="text-xs text-text-muted">{status}</p> : null}

            <div className="flex flex-wrap gap-2">
              <Button type="submit" variant="primary" disabled={saving || !dirty} loading={saving}>{t('config.save')}</Button>
              <Button type="button" variant="secondary" onClick={() => void handleReload()} disabled={saving}>{t('config.reload')}</Button>
              <Button type="button" variant="ghost" onClick={handleReset} disabled={saving || !dirty}>{t('config.resetDraft')}</Button>
            </div>
          </form>
        </Card>
      ) : (
        <Card padding="none">
          <div className="px-4 pt-4 pb-3 border-b border-border-subtle flex items-center justify-between">
            <div>
              <span className="eyebrow">{t('config.schemaDrivenForm')}</span>
              <h3 className="text-sm font-semibold text-text mt-0.5">{t('config.allSections')}</h3>
            </div>
            {dirty ? <Badge variant="warning">{t('config.unsavedChanges')}</Badge> : <Badge variant="default">{t('config.inSync')}</Badge>}
          </div>

          <div className="px-4 pt-2 text-xs text-text-subtle">
            Form edits are patched onto the original YAML on save to preserve existing spacing/comments as much as possible.
            {Object.keys(pendingEdits).length > 0 ? ` Pending changes: ${Object.keys(pendingEdits).length}.` : ''}
          </div>

          <div className="px-4 pt-3 pb-2 border-b border-border-subtle flex flex-wrap items-center gap-2">
            <input
              type="text"
              value={searchQuery}
              onChange={(event) => setSearchQuery(event.target.value)}
              placeholder={t('config.searchKeys')}
              className="min-w-[220px] flex-1 rounded-md border border-border bg-surface px-3 py-2 text-xs text-text"
            />
            <Button type="button" variant={showChangedOnly ? 'primary' : 'secondary'} onClick={() => setShowChangedOnly((previous) => !previous)}>
              {showChangedOnly ? 'Show all fields' : 'Show changed only'}
            </Button>
            <Button type="button" variant="ghost" onClick={() => setAllSectionsOpen(true)}>{t('config.expandAll')}</Button>
            <Button type="button" variant="ghost" onClick={() => setAllSectionsOpen(false)}>{t('config.collapseAll')}</Button>
          </div>

          <div className="grid grid-cols-1 xl:grid-cols-[260px_minmax(0,1fr)] gap-4 p-4">
            <Card className="xl:sticky xl:top-4 h-fit" padding="none">
              <div className="px-3 py-2 border-b border-border-subtle text-xs font-semibold text-text">{t('knowledge.sections')}</div>
              <div className="max-h-[520px] overflow-y-auto p-2 flex flex-col gap-1">
                {formSections.map(([sectionKey, sectionValue]) => {
                  const content = renderField([sectionKey], sectionValue, 0);
                  if (!content) return null;
                  const changes = changedSectionCounts[sectionKey] ?? 0;
                  const active = sectionOpen[sectionKey] !== false;
                  return (
                    <button
                      key={`nav-${sectionKey}`}
                      type="button"
                      className={`w-full text-left rounded-md border px-2 py-2 text-xs transition ${active ? 'border-border bg-surface-elevated text-text' : 'border-border-subtle text-text-muted hover:text-text'}`}
                      onClick={() => jumpToSection(sectionKey)}
                    >
                      <div className="flex items-center justify-between gap-2">
                        <span className="truncate">{pathLabel([sectionKey])}</span>
                        {changes > 0 ? <Badge variant="warning">{changes}</Badge> : null}
                      </div>
                    </button>
                  );
                })}
              </div>
            </Card>

            <div className="max-h-[760px] overflow-y-auto flex flex-col gap-3 pr-1">
              {formSections.map(([sectionKey, sectionValue]) => {
                const content = renderField([sectionKey], sectionValue, 0);
                if (!content) return null;
                const changes = changedSectionCounts[sectionKey] ?? 0;
                const open = sectionOpen[sectionKey] !== false;
                return (
                  <section id={sectionDomId(sectionKey)} key={sectionKey} className="rounded-lg border border-border-subtle bg-surface-elevated/10">
                    <button
                      type="button"
                      className="w-full px-4 py-3 border-b border-border-subtle flex items-center justify-between text-left"
                      onClick={() => toggleSection(sectionKey)}
                    >
                      <span className="text-xs font-semibold uppercase tracking-wide text-text">{pathLabel([sectionKey])}</span>
                      <span className="flex items-center gap-2">
                        {changes > 0 ? <Badge variant="warning">{changes} changed</Badge> : null}
                        <span className="text-[11px] text-text-subtle">{open ? 'collapse' : 'expand'}</span>
                      </span>
                    </button>
                    {open ? <div className="p-3 flex flex-col gap-3">{content}</div> : null}
                  </section>
                );
              })}
            </div>
          </div>

          <div className="px-4 pb-4 pt-2 border-t border-border-subtle">
            {status ? <p className="text-xs text-text-muted mb-3">{status}</p> : null}
            <div className="flex flex-wrap gap-2">
              <Button type="button" variant="primary" disabled={saving || !dirty} loading={saving} onClick={() => void handleSave()}>{t('config.save')}</Button>
              <Button type="button" variant="secondary" onClick={() => void handleReload()} disabled={saving}>{t('config.reload')}</Button>
              <Button type="button" variant="ghost" onClick={handleReset} disabled={saving || !dirty}>{t('config.resetDraft')}</Button>
            </div>
          </div>
        </Card>
      )}
    </div>
  );
}
