import { useI18n } from '../lib/i18n';
import { useEffect, useMemo, useRef, useState } from 'react';
import { FilePenLine, Hash, Search, Server, Settings2 } from 'lucide-react';
import { parse as parseYaml, parseDocument, stringify as stringifyYaml } from 'yaml';
import { Card } from '../components/ui/Card';
import { Badge } from '../components/ui/Badge';
import { Button } from '../components/ui/Button';
import { PageHeader } from '../components/PageHeader';
import { useMissionControl } from '../lib/mission-control-store';
import { usePullToReload } from '../hooks/usePullToReload';
import { PullToReloadIndicator } from '../components/PullToReloadIndicator';

type PathSegment = string | number;
type PendingEdit = { path: PathSegment[]; value: unknown };

function formatConfigValue(value: unknown) {
  if (typeof value === 'string') return value;
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  if (value === null || value === undefined) return '—';
  try {
    return JSON.stringify(value);
  } catch {
    return '—';
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
  return key
    .replace(/[_-]+/g, ' ')
    .trim()
    .replace(/\b\w/g, (character) => character.toUpperCase());
}

function pathKey(path: PathSegment[]) {
  return path.map((segment) => String(segment)).join('.');
}

function configValueMatchesQuery(path: PathSegment[], value: unknown, query: string): boolean {
  if (!query) return true;
  const haystack = `${pathKey(path)} ${pathLabel(path)} ${formatConfigValue(value)}`.toLowerCase();
  if (haystack.includes(query)) return true;
  if (isPlainObject(value)) {
    return Object.entries(value).some(([childKey, childValue]) => configValueMatchesQuery([...path, childKey], childValue, query));
  }
  if (Array.isArray(value)) {
    return value.some((childValue, index) => configValueMatchesQuery([...path, index], childValue, query));
  }
  return false;
}

function MetricCard({
  icon: Icon,
  label,
  value,
  hint,
  color,
}: {
  icon: React.ElementType;
  label: string;
  value: string;
  hint: string;
  color: string;
}) {
  return (
    <div className="min-w-0 rounded-lg bg-surface-sunken/35 p-3 transition-colors hover:bg-surface-sunken/50 sm:p-4">
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

export function ConfigRoute() {
  const { t } = useI18n();
  const { config, snapshot, reloadConfig, saveConfig } = useMissionControl();
  const [draft, setDraft] = useState(config.content);
  const [status, setStatus] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [yamlError, setYamlError] = useState<string | null>(null);
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
        setStatus(t('config.reloaded', { path: updated.path }));
      } catch (error) {
        setStatus(error instanceof Error ? error.message : t('config.failedReload'));
      } finally {
        setSaving(false);
      }
    },
  });

  const dirty = useMemo(() => draft !== config.content || Object.keys(pendingEdits).length > 0 || Object.keys(complexDrafts).length > 0, [draft, config.content, pendingEdits, complexDrafts]);
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
  const visibleFormSections = useMemo(
    () => formSections.filter(([sectionKey, sectionValue]) => {
      if (showChangedOnly) return (changedSectionCounts[sectionKey] ?? 0) > 0;
      return !searchLower || configValueMatchesQuery([sectionKey], sectionValue, searchLower);
    }),
    [changedSectionCounts, formSections, searchLower, showChangedOnly],
  );

  useEffect(() => {
    setDraft(config.content);
    setFormState(isPlainObject(config.config) ? (config.config as Record<string, unknown>) : {});
    setComplexDrafts({});
    setPendingEdits({});
    setYamlError(null);
    setSearchQuery('');
    setShowChangedOnly(false);
  }, [config]);

  useEffect(() => {
    setSectionOpen((previous) => {
      const next: Record<string, boolean> = {};
      for (const [section] of formSections) {
        next[section] = previous[section] ?? section === formSections[0]?.[0];
      }
      return next;
    });
  }, [formSections]);

  useEffect(() => {
    if (!searchLower) return;
    setSectionOpen((previous) => {
      const next = { ...previous };
      for (const [sectionKey, sectionValue] of formSections) {
        if (configValueMatchesQuery([sectionKey], sectionValue, searchLower)) {
          next[sectionKey] = true;
        }
      }
      return next;
    });
  }, [formSections, searchLower]);

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
      const document = parseDocument(nextYaml);
      if (document.errors.length > 0) {
        setYamlError(document.errors[0]?.message ?? t('config.yamlInvalid'));
        return;
      }
      setYamlError(null);
      const parsed = document.toJSON();
      if (isPlainObject(parsed)) {
        setFormState(parsed);
        setPendingEdits({});
      }
    } catch (error) {
      setYamlError(error instanceof Error ? error.message : t('config.yamlInvalid'));
    }
  };

  const handleSave = async () => {
    setSaving(true);
    setStatus(null);
    try {
      let payloadYaml = draft;

      if (editorMode === 'yaml' && yamlError) {
        throw new Error(t('config.yamlParseErrors'));
      }

      if (editorMode === 'form' && Object.keys(complexDrafts).length > 0) {
        for (const text of Object.values(complexDrafts)) {
          const parsed = parseYaml(text);
          if (!Array.isArray(parsed)) {
            throw new Error(t('config.fixArrayBeforeSave'));
          }
        }
      }

      if (editorMode === 'form' && Object.keys(pendingEdits).length > 0) {
        const doc = parseDocument(draft);
        if (doc.errors.length > 0) {
          throw new Error(t('config.yamlParseErrors'));
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
      setStatus(t('config.saved', { path: updated.path }));
    } catch (error) {
      setStatus(error instanceof Error ? error.message : t('config.failedSave'));
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
      setYamlError(null);
      setStatus(t('config.reloaded', { path: updated.path }));
    } catch (error) {
      setStatus(error instanceof Error ? error.message : t('config.failedReload'));
    } finally {
      setSaving(false);
    }
  };

  const handleReset = () => {
    setDraft(config.content);
    setFormState(isPlainObject(config.config) ? (config.config as Record<string, unknown>) : {});
    setComplexDrafts({});
    setPendingEdits({});
    setYamlError(null);
    setStatus(t('config.resetNote'));
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
              try {
                const parsed = parseYaml(nextText);
                if (Array.isArray(parsed)) {
                  updateFormValue(path, parsed);
                }
              } catch {
                // Keep the text draft visible until the YAML becomes valid.
              }
            }}
            onBlur={(event) => {
              const nextText = event.target.value;
              try {
                const parsed = parseYaml(nextText);
                if (!Array.isArray(parsed)) {
                  throw new Error(t('config.expectedArray'));
                }
                updateFormValue(path, parsed);
                setComplexDrafts((previous) => {
                  const next = { ...previous };
                  delete next[key];
                  return next;
                });
                setStatus(null);
              } catch (error) {
                setStatus(error instanceof Error ? t('config.invalidArray', { label, error: error.message }) : t('config.invalidArray', { label, error: t('config.expectedArray') }));
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
            {value ? t('config.enabled') : t('config.disabled')}
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
    <div ref={containerRef} className="route-page-scroll flex h-full min-w-0 flex-col gap-5 overflow-y-auto sm:gap-6">
      <PullToReloadIndicator state={pullState} />

      <PageHeader
        eyebrow={t('config.eyebrow')}
        title={t('config.title')}
        description={t('config.description')}
        meta={(
          <div className="flex items-center gap-2">
            <Badge variant={config.available ? 'positive' : 'warning'}>
              {config.available ? t('config.liveEndpoint') : t('config.fallback')}
            </Badge>
            <span className="hidden text-xs text-text-subtle sm:inline">{snapshot.gatewayStatus}</span>
          </div>
        )}
      />

      <Card padding="none" className="!border-0">
        <div className="grid grid-cols-2 gap-2.5 p-3 sm:gap-3 sm:p-4 xl:grid-cols-4">
          <MetricCard
            icon={FilePenLine}
            label={t('config.file')}
            value={config.path}
            hint={config.exists ? t('config.fileExists') : t('config.missing')}
            color="text-sky-400"
          />
          <MetricCard
            icon={Hash}
            label={t('config.hash')}
            value={config.hash ? config.hash.slice(0, 12) : '—'}
            hint={t('config.lockToken')}
            color="text-violet-400"
          />
          <MetricCard
            icon={Settings2}
            label={t('config.sections')}
            value={String(formSections.length)}
            hint={t('config.topLevelKeys')}
            color="text-amber-400"
          />
          <MetricCard
            icon={Server}
            label={t('config.changes')}
            value={String(Object.keys(pendingEdits).length)}
            hint={dirty ? t('config.unsavedChanges') : t('config.inSync')}
            color={dirty ? 'text-amber-400' : 'text-emerald-400'}
          />
        </div>
      </Card>

      <div className="config-editor-mode-bar sticky top-0 z-20 -mx-1 px-2 py-2 sm:-mx-0 sm:px-3">
        <div className="flex min-w-0 flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
          <div role="tablist" aria-label={t('config.editor')} className="flex min-h-11 shrink-0 flex-nowrap gap-1.5 overflow-x-auto pb-1 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden sm:flex-wrap sm:overflow-visible sm:pb-0">
            <Button
              className="shrink-0 whitespace-nowrap"
              type="button"
              role="tab"
              aria-selected={editorMode === 'form'}
              size="sm"
              variant={editorMode === 'form' ? 'primary' : 'ghost'}
              onClick={() => setEditorMode('form')}
            >
              {t('config.formMode')}
            </Button>
            <Button
              className="shrink-0 whitespace-nowrap"
              type="button"
              role="tab"
              aria-selected={editorMode === 'yaml'}
              size="sm"
              variant={editorMode === 'yaml' ? 'primary' : 'ghost'}
              onClick={() => setEditorMode('yaml')}
            >
              {t('config.yamlMode')}
            </Button>
          </div>
        </div>
      </div>

      {editorMode === 'yaml' ? (
        <Card padding="none" className="!border-0">
          <div className="flex flex-col gap-2 border-b border-border px-4 pb-3 pt-4 sm:flex-row sm:items-center sm:justify-between">
            <div className="min-w-0">
              <span className="eyebrow">{t('config.editor')}</span>
              <h3 className="mt-0.5 truncate text-sm font-semibold text-text">config.yaml</h3>
              <p className="mt-1 truncate text-xs text-text-subtle">{t('config.hash')}: {config.hash || '—'}</p>
            </div>
            <Badge variant={yamlError ? 'warning' : dirty ? 'warning' : 'default'}>
              {yamlError ? t('config.yamlInvalid') : dirty ? t('config.unsavedChanges') : t('config.inSync')}
            </Badge>
          </div>

          <div className="flex flex-col gap-2 p-3 sm:p-4">
            <textarea
              aria-label={t('config.rawYamlEditor')}
              className="mobile-config-editor min-h-[420px] w-full resize-y rounded-lg border border-border bg-surface p-3 text-sm font-mono text-text outline-none focus:border-accent sm:min-h-[620px]"
              value={draft}
              onChange={(event) => handleYamlChange(event.target.value)}
              spellCheck={false}
            />
            <p className={`text-xs ${yamlError ? 'text-warning' : 'text-text-subtle'}`}>
              {yamlError ? `${t('config.yamlInvalid')}: ${yamlError}` : t('config.yamlValid')}
            </p>
          </div>
        </Card>
      ) : (
        <Card padding="none" className="!border-0">
          <div className="flex flex-col gap-2 border-b border-border px-4 pb-3 pt-4 sm:flex-row sm:items-center sm:justify-between">
            <div className="min-w-0">
              <span className="eyebrow">{t('config.schemaDrivenForm')}</span>
              <h3 className="mt-0.5 text-sm font-semibold text-text">{t('config.allSections')}</h3>
              <p className="mt-1 text-xs leading-relaxed text-text-subtle">{t('config.formPatched')}</p>
            </div>
          </div>

          <div className="flex flex-col gap-2 border-b border-border px-3 py-3 sm:flex-row sm:items-center sm:px-4">
            <label className="relative min-w-0 flex-1">
              <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-text-subtle" />
              <input
                type="text"
                value={searchQuery}
                onChange={(event) => setSearchQuery(event.target.value)}
                placeholder={t('config.searchKeys')}
                aria-label={t('config.searchKeys')}
                className="h-10 w-full rounded-lg border border-border bg-surface pl-9 pr-3 text-sm text-text outline-none focus:border-accent"
              />
            </label>
            <div className="flex shrink-0 items-center gap-2 overflow-x-auto">
              <Button type="button" size="sm" variant={showChangedOnly ? 'primary' : 'secondary'} onClick={() => setShowChangedOnly((previous) => !previous)}>
                {showChangedOnly ? t('config.showAllFields') : t('config.showChangedOnly')}
              </Button>
              <Button type="button" size="sm" variant="ghost" onClick={() => setAllSectionsOpen(true)}>{t('config.expandAll')}</Button>
              <Button type="button" size="sm" variant="ghost" onClick={() => setAllSectionsOpen(false)}>{t('config.collapseAll')}</Button>
            </div>
          </div>

          <div className="grid min-w-0 grid-cols-1 gap-4 p-3 sm:p-4 xl:grid-cols-[240px_minmax(0,1fr)]">
            <Card className="h-fit min-w-0 !border-0 xl:sticky xl:top-16" padding="none">
              <div className="border-b border-border px-3 py-2 text-xs font-semibold text-text">{t('config.sectionsNav')}</div>
              <div className="flex max-w-full flex-nowrap gap-1 overflow-x-auto p-2 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden xl:flex-col xl:overflow-visible">
                {visibleFormSections.map(([sectionKey]) => {
                  const changes = changedSectionCounts[sectionKey] ?? 0;
                  const active = sectionOpen[sectionKey] !== false;
                  return (
                    <button
                      key={`nav-${sectionKey}`}
                      type="button"
                      className={`flex shrink-0 items-center justify-between gap-2 rounded-md border px-2 py-2 text-left text-xs transition xl:w-full ${active ? 'border-border bg-surface-elevated text-text' : 'border-border-subtle text-text-muted hover:text-text'}`}
                      onClick={() => jumpToSection(sectionKey)}
                    >
                      <span className="truncate">{pathLabel([sectionKey])}</span>
                      {changes > 0 ? <Badge variant="warning">{changes}</Badge> : null}
                    </button>
                  );
                })}
              </div>
            </Card>

            <div className="flex min-w-0 flex-col gap-3">
              {visibleFormSections.length === 0 ? (
                <div className="rounded-lg border border-dashed border-border-subtle px-4 py-8 text-center text-sm text-text-muted">
                  {t('config.noMatches')}
                </div>
              ) : null}
              {visibleFormSections.map(([sectionKey, sectionValue]) => {
                const content = renderField([sectionKey], sectionValue, 0);
                if (!content) return null;
                const changes = changedSectionCounts[sectionKey] ?? 0;
                const open = sectionOpen[sectionKey] !== false;
                return (
                  <section id={sectionDomId(sectionKey)} key={sectionKey} className="min-w-0 rounded-lg border border-border-subtle bg-surface-elevated/10">
                    <button
                      type="button"
                      aria-expanded={open}
                      className="flex min-h-11 w-full items-center justify-between gap-3 px-3 py-3 text-left transition-colors hover:bg-surface-elevated/30 sm:px-4"
                      onClick={() => toggleSection(sectionKey)}
                    >
                      <span className="min-w-0 truncate text-xs font-semibold uppercase tracking-wide text-text">{pathLabel([sectionKey])}</span>
                      <span className="flex shrink-0 items-center gap-2">
                        {changes > 0 ? <Badge variant="warning">{t('config.changedCount', { count: changes })}</Badge> : null}
                        <span className="text-[11px] text-text-subtle">{open ? t('config.collapse') : t('config.expand')}</span>
                      </span>
                    </button>
                    {open ? <div className="flex min-w-0 flex-col gap-3 border-t border-border p-3 sm:p-4">{content}</div> : null}
                  </section>
                );
              })}
            </div>
          </div>
        </Card>
      )}

      <div className="config-action-bar sticky bottom-3 z-30 flex min-w-0 shrink-0 flex-col gap-2 overflow-hidden rounded-xl border border-border-subtle bg-surface/95 p-3 backdrop-blur sm:flex-row sm:items-center sm:justify-between">
        <p className="min-w-0 truncate text-xs text-text-subtle">
          {status ?? (dirty ? t('config.unsavedChanges') : t('config.inSync'))}
        </p>
        <div className="config-action-bar-buttons -mx-1 flex min-w-0 max-w-[calc(100%+0.5rem)] shrink-0 flex-nowrap gap-2 overflow-x-auto px-1 pb-1 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden sm:mx-0 sm:max-w-none sm:flex-wrap sm:overflow-visible sm:px-0 sm:pb-0">
          <Button className="shrink-0 whitespace-nowrap" type="button" variant="primary" disabled={saving || !dirty || Boolean(yamlError)} loading={saving} onClick={() => void handleSave()}>
            {t('config.save')}
          </Button>
          <Button className="shrink-0 whitespace-nowrap" type="button" variant="secondary" onClick={() => void handleReload()} disabled={saving}>
            {t('config.reload')}
          </Button>
          <Button className="shrink-0 whitespace-nowrap" type="button" variant="ghost" onClick={handleReset} disabled={saving || !dirty}>
            {t('config.resetDraft')}
          </Button>
        </div>
      </div>
    </div>
  );
}
