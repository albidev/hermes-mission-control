import { useI18n } from '../lib/i18n';
import { useCallback, useEffect, useMemo, useRef, useState, type ElementType } from 'react';
import { Brain, FolderTree, LibraryBig, Power, RefreshCw, Search, FileText, X } from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import remarkBreaks from 'remark-breaks';
import { Card } from '../components/ui/Card';
import { Badge } from '../components/ui/Badge';
import { Button } from '../components/ui/Button';
import { ToggleSwitch } from '../components/ui/ToggleSwitch';
import { Modal } from '../components/Modal';
import {
  loadMissionControlSkillsCatalog,
  loadMissionControlSkillFiles,
  toggleMissionControlSkill,
  type MissionControlSkillsCatalogSnapshot,
  type MissionControlSkillFilesPayload,
  type MissionControlSkillFile,
} from '../lib/hermes-api';
import { useMissionControl } from '../lib/mission-control-store';
import { usePullToReload } from '../hooks/usePullToReload';
import { PullToReloadIndicator } from '../components/PullToReloadIndicator';

function MetricCard({
  icon: Icon,
  label,
  value,
  hint,
}: {
  icon: ElementType;
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
      <p className="text-lg font-semibold text-text mt-2">{value}</p>
      <p className="text-xs text-text-subtle mt-1">{hint}</p>
    </Card>
  );
}

function statusVariant(status: string): 'positive' | 'accent' | 'warning' | 'default' {
  if (status === 'builtin') return 'positive';
  if (status === 'trusted') return 'accent';
  if (status === 'community') return 'warning';
  return 'default';
}

function SkillDetailPanel({
  skillName,
  skillDescription,
  onClose,
  onToggle,
  isEnabled,
  isToggling,
}: {
  skillName: string;
  skillDescription?: string;
  onClose: () => void;
  onToggle?: () => void;
  isEnabled?: boolean;
  isToggling?: boolean;
}) {
  const { t } = useI18n();
  const [files, setFiles] = useState<MissionControlSkillFilesPayload | null>(null);
  const [filesLoading, setFilesLoading] = useState(true);
  const [filesError, setFilesError] = useState<string | null>(null);
  const [activeFile, setActiveFile] = useState<MissionControlSkillFile | null>(null);

  useEffect(() => {
    let cancelled = false;
    setFilesLoading(true);
    setFilesError(null);
    setActiveFile(null);

    loadMissionControlSkillFiles(skillName)
      .then((result) => {
        if (cancelled) return;
        if (result) {
          setFiles(result);
          const mdFile = result.files.find((f) => f.name.toLowerCase().endsWith('.md'));
          if (mdFile) setActiveFile(mdFile);
        } else {
          setFilesError('Could not load skill files.');
        }
      })
      .catch(() => {
        if (!cancelled) setFilesError('Could not load skill files.');
      })
      .finally(() => {
        if (!cancelled) setFilesLoading(false);
      });

    return () => { cancelled = true; };
  }, [skillName]);

  const fileTree = files?.files ?? [];

  return (
    <Modal
      open
      title={skillName}
      subtitle={skillDescription}
      onClose={onClose}
      footer={
        onToggle ? (
          <div className="flex items-center justify-between gap-3">
            <span className="text-xs text-text-subtle">
              {isToggling ? 'Updating…' : isEnabled ? 'Skill is enabled' : 'Skill is disabled'}
            </span>
            <Button
              size="sm"
              variant={isEnabled ? 'secondary' : 'primary'}
              onClick={onToggle}
              disabled={isToggling}
            >
              {isEnabled ? 'Disable' : 'Enable'}
            </Button>
          </div>
        ) : null
      }
    >
      <div className="flex flex-col gap-4">
        {filesLoading ? (
          <p className="text-sm text-text-muted">{t('skills.detail.loading')}</p>
        ) : filesError ? (
          <p className="text-sm text-warning">{filesError}</p>
        ) : fileTree.length === 0 ? (
          <p className="text-sm text-text-muted">{t('skills.detail.noFiles')}</p>
        ) : (
          <div className="flex flex-col lg:flex-row gap-4 min-h-[400px]">
            {/* File tree sidebar */}
            <div className="lg:w-56 shrink-0 border-b lg:border-b-0 lg:border-r border-border-subtle pb-3 lg:pb-0 lg:pr-3">
              <p className="eyebrow mb-2">{t('knowledge.files')}</p>
              <div className="flex flex-row lg:flex-col gap-1 overflow-x-auto lg:overflow-x-visible">
                {fileTree.map((file) => (
                  <button
                    key={file.path}
                    type="button"
                    onClick={() => setActiveFile(file)}
                    className={[
                      'flex items-center gap-1.5 px-2 py-1 rounded-md text-xs whitespace-nowrap transition-colors text-left',
                      activeFile?.path === file.path
                        ? 'bg-accent/10 text-accent'
                        : 'text-text-muted hover:text-text hover:bg-surface-raised',
                    ].join(' ')}
                  >
                    <FileText className="h-3 w-3 shrink-0" />
                    <span className="truncate max-w-[180px]">{file.name}</span>
                  </button>
                ))}
              </div>
            </div>

            {/* File content */}
            <div className="flex-1 min-w-0">
              {activeFile ? (
                <>
                  <div className="flex items-center justify-between gap-2 mb-3">
                    <p className="text-xs font-medium text-text-subtle truncate">{activeFile.path}</p>
                    <span className="text-xs text-text-muted shrink-0">{activeFile.size} bytes</span>
                  </div>
                  <div className="rounded-lg border border-border-subtle bg-surface-raised p-4 overflow-x-auto">
                    {activeFile.name.toLowerCase().endsWith('.md') ? (
                      <div className="prose prose-sm prose-invert max-w-none prose-headings:text-text prose-p:text-text-muted prose-a:text-accent prose-code:text-accent prose-pre:bg-surface prose-pre:border prose-pre:border-border-subtle">
                        <ReactMarkdown remarkPlugins={[remarkGfm, remarkBreaks]}>
                          {activeFile.content}
                        </ReactMarkdown>
                      </div>
                    ) : (
                      <pre className="text-xs text-text-muted whitespace-pre-wrap break-words font-mono leading-relaxed">
                        {activeFile.content}
                      </pre>
                    )}
                  </div>
                </>
              ) : (
                <p className="text-sm text-text-muted">{t('skills.detail.selectFile')}</p>
              )}
            </div>
          </div>
        )}
      </div>
    </Modal>
  );
}

export function SkillsRoute() {
  const { t } = useI18n();
  const { skills, snapshot, storedToken, refreshAll } = useMissionControl();
  const [activeTab, setActiveTab] = useState<'installed' | 'catalog'>('installed');
  const [catalog, setCatalog] = useState<MissionControlSkillsCatalogSnapshot | null>(null);
  const [catalogLoading, setCatalogLoading] = useState(false);
  const [catalogError, setCatalogError] = useState<string | null>(null);
  const [query, setQuery] = useState('');
  const [togglingSkills, setTogglingSkills] = useState<Set<string>>(new Set());
  const [detailSkill, setDetailSkill] = useState<{ name: string; description: string; enabled?: boolean } | null>(null);
  const containerRef = useRef<HTMLDivElement | null>(null);

  const enabled = skills.skills.filter((skill) => skill.enabled).length;
  const disabled = skills.skills.length - enabled;
  const installedNames = useMemo(() => new Set(skills.skills.map((skill) => skill.name.toLowerCase())), [skills.skills]);

  const { state: pullState } = usePullToReload({
    containerRef,
    onReload: async () => {
      await refreshAll(storedToken || undefined, { silent: true, includeReference: true });
      await refreshCatalog();
    },
  });

  const handleToggle = useCallback(async (skillName: string, currentlyEnabled: boolean) => {
    setTogglingSkills((prev) => new Set(prev).add(skillName));
    try {
      const result = await toggleMissionControlSkill(skillName, !currentlyEnabled, storedToken || undefined);
      if (result?.success) {
        // Refresh skills list to pick up new disabled state from backend
        await refreshAll(storedToken || undefined, { silent: true, includeReference: true });
      }
    } catch {
      // Silently fail — state stays unchanged
    } finally {
      setTogglingSkills((prev) => {
        const next = new Set(prev);
        next.delete(skillName);
        return next;
      });
    }
  }, [storedToken, refreshAll]);

  const refreshCatalog = async () => {
    setCatalogLoading(true);
    setCatalogError(null);
    try {
      const nextCatalog = await loadMissionControlSkillsCatalog(storedToken || undefined, { limit: 5000 });
      setCatalog(nextCatalog);
      if (!nextCatalog.available) {
        setCatalogError(nextCatalog.hint ?? 'Skills catalog unavailable.');
      }
    } catch (error) {
      setCatalogError(error instanceof Error ? error.message : 'Skills catalog unavailable.');
    } finally {
      setCatalogLoading(false);
    }
  };

  useEffect(() => {
    void refreshCatalog();
    // Load once per token change. Manual refresh handles everything else.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [storedToken]);

  const filteredCatalogSkills = useMemo(() => {
    const normalizedQuery = query.trim().toLowerCase();
    const items = catalog?.skills ?? [];
    if (!normalizedQuery) return items;
    return items.filter((skill) => {
      const haystack = [skill.name, skill.description, skill.source, skill.identifier, skill.repo ?? '', skill.path ?? '', ...skill.tags]
        .join(' ')
        .toLowerCase();
      return haystack.includes(normalizedQuery);
    });
  }, [catalog?.skills, query]);

  const catalogInstalledCount = filteredCatalogSkills.filter((skill) => skill.installed || installedNames.has(skill.name.toLowerCase())).length;
  const displayedCatalogSkills = filteredCatalogSkills.slice(0, 250);

  return (
    <div ref={containerRef} className="flex flex-col gap-6 h-full overflow-y-auto">
      <PullToReloadIndicator state={pullState} />
      <Card padding="none">
        <div className="px-4 pt-4 pb-3 border-b border-border-subtle flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
          <div className="flex flex-col gap-0.5">
            <span className="eyebrow">{t('nav.skills')}</span>
            <h2 className="text-sm font-semibold text-text">{t('skills.title')}</h2>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Badge variant={skills.available ? 'positive' : 'warning'}>{skills.available ? 'installed live' : 'installed fallback'}</Badge>
            <Badge variant={catalog?.available ? 'positive' : 'warning'}>{catalog?.available ? 'catalog live' : 'catalog fallback'}</Badge>
          </div>
        </div>

        <div className="p-4 grid grid-cols-1 md:grid-cols-2 xl:grid-cols-4 gap-3">
          <MetricCard icon={Brain} label="Installed" value={String(skills.count)} hint={`local capabilities · ${catalog?.count ?? 0} in catalog`} />
          <MetricCard icon={LibraryBig} label="Catalog" value={String(catalog?.count ?? 0)} hint="discoverable skills" />
          <MetricCard icon={Power} label="Enabled" value={String(enabled)} hint="currently active" />
          <MetricCard icon={FolderTree} label="Model" value={snapshot.activeModel} hint="current primary model" />
        </div>
      </Card>

      <div className="flex flex-wrap items-center gap-2">
        <Button
          variant={activeTab === 'installed' ? 'primary' : 'secondary'}
          size="sm"
          onClick={() => setActiveTab('installed')}
        >
          Installed
        </Button>
        <Button
          variant={activeTab === 'catalog' ? 'primary' : 'secondary'}
          size="sm"
          onClick={() => setActiveTab('catalog')}
        >
          Catalog
        </Button>
      </div>

      {activeTab === 'installed' ? (
        <Card padding="none">
          <div className="px-4 pt-4 pb-3 border-b border-border-subtle flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
            <div>
              <span className="eyebrow">{t('skills.installed')}</span>
              <h3 className="text-sm font-semibold text-text mt-0.5">{t('skills.everyInstalled')}</h3>
              <p className="text-xs text-text-subtle mt-1">
                {skills.skills.length} skills · {enabled} enabled · {disabled} disabled
              </p>
            </div>
          </div>

          <div className="divide-y divide-border-subtle">
            {skills.skills.map((skill) => (
              <div
                key={skill.id}
                className="px-4 py-3 flex flex-col gap-2 cursor-pointer hover:bg-surface-raised/40 transition-colors"
                onClick={() => setDetailSkill({ name: skill.name, description: skill.description, enabled: skill.enabled })}
                role="button"
                tabIndex={0}
                onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') setDetailSkill({ name: skill.name, description: skill.description, enabled: skill.enabled }); }}
              >
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <p className="text-sm font-medium text-text truncate">{skill.name}</p>
                    <p className="text-xs text-text-muted line-clamp-2">{skill.description}</p>
                  </div>
                  <div className="flex items-center gap-3 shrink-0" onClick={(e) => e.stopPropagation()}>
                    <ToggleSwitch
                      id={`toggle-${skill.id}`}
                      checked={skill.enabled}
                      disabled={togglingSkills.has(skill.name)}
                      onChange={() => handleToggle(skill.name, skill.enabled)}
                      label={skill.enabled ? 'enabled' : 'disabled'}
                    />
                  </div>
                </div>

                <div className="text-xs text-text-subtle">
                  {(skill.category || 'uncategorized') + (skill.model ? ` · ${skill.model}` : '')}
                </div>

                {skill.tags.length > 0 ? (
                  <div className="flex flex-wrap gap-2">
                    {skill.tags.map((tag) => (
                      <Badge key={tag} variant="default">{tag}</Badge>
                    ))}
                  </div>
                ) : null}
              </div>
            ))}
          </div>
        </Card>
      ) : (
        <Card padding="none">
          <div className="px-4 pt-4 pb-3 border-b border-border-subtle flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
            <div>
              <span className="eyebrow">{t('skills.hub')}</span>
              <h3 className="text-sm font-semibold text-text mt-0.5">{t('skills.discoverableCatalog')}</h3>
              <p className="text-xs text-text-subtle mt-1">
                {filteredCatalogSkills.length} matches · {displayedCatalogSkills.length} rendered · {catalogInstalledCount} installed match{catalogInstalledCount === 1 ? '' : 'es'}
              </p>
            </div>
            <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
              <div className="relative">
                <Search className="h-4 w-4 absolute left-3 top-1/2 -translate-y-1/2 text-text-subtle" />
                <input
                  value={query}
                  onChange={(event) => setQuery(event.target.value)}
                  placeholder={t('skills.search')}
                  className="h-9 w-full sm:w-64 rounded-lg bg-surface border border-border px-9 text-sm text-text outline-none focus:border-accent"
                />
              </div>
              <Button
                size="sm"
                variant="secondary"
                loading={catalogLoading}
                icon={<RefreshCw className="h-4 w-4" />}
                onClick={() => void refreshCatalog()}
              >
                Refresh
              </Button>
            </div>
          </div>

          {catalogError ? (
            <div className="px-4 py-3 border-b border-border-subtle text-xs text-warning">{catalogError}</div>
          ) : null}

          {catalog?.sources ? (
            <div className="px-4 py-3 border-b border-border-subtle flex flex-wrap gap-2">
              {Object.entries(catalog.sources).map(([source, count]) => (
                <Badge key={source} variant="default">
                  {source} · {count}
                </Badge>
              ))}
              {catalog.timedOut.map((source) => (
                <Badge key={`timeout-${source}`} variant="warning">
                  {source} timed out
                </Badge>
              ))}
            </div>
          ) : null}

          <div className="divide-y divide-border-subtle">
            {displayedCatalogSkills.map((skill) => {
              const installed = skill.installed || installedNames.has(skill.name.toLowerCase());
              return (
                <div key={`${skill.source}:${skill.identifier}`} className="px-4 py-3 flex flex-col gap-2">
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0">
                      <p className="text-sm font-medium text-text truncate">{skill.name}</p>
                      <p className="text-xs text-text-muted line-clamp-2">{skill.description || 'No description provided.'}</p>
                    </div>
                    <div className="flex flex-wrap justify-end gap-2">
                      {installed ? <Badge variant="positive">{t('skills.installedBadge')}</Badge> : <Badge variant="default">{t('skills.availableBadge')}</Badge>}
                      <Badge variant={statusVariant(skill.trustLevel)}>{skill.trustLevel}</Badge>
                    </div>
                  </div>

                      <div className="text-xs text-text-muted truncate">
                        {(skill.category || 'uncategorized') + (skill.model ? ` · ${skill.model}` : '')}
                      </div>

                  {skill.tags.length > 0 ? (
                    <div className="flex flex-wrap gap-2">
                      {skill.tags.slice(0, 8).map((tag) => (
                        <Badge key={tag} variant="default">{tag}</Badge>
                      ))}
                    </div>
                  ) : null}
                </div>
              );
            })}

            {!catalogLoading && filteredCatalogSkills.length > displayedCatalogSkills.length ? (
              <div className="px-4 py-4 text-xs text-text-subtle">
                Rendering the first {displayedCatalogSkills.length} matches. Use search to narrow the full {filteredCatalogSkills.length}-skill result set.
              </div>
            ) : null}

            {!catalogLoading && filteredCatalogSkills.length === 0 ? (
              <div className="px-4 py-8 text-sm text-text-muted">{t('skills.noMatch')}</div>
            ) : null}
          </div>
        </Card>
      )}

      {detailSkill ? (
        <SkillDetailPanel
          skillName={detailSkill.name}
          skillDescription={detailSkill.description}
          onClose={() => setDetailSkill(null)}
          onToggle={detailSkill.enabled !== undefined ? () => handleToggle(detailSkill.name, detailSkill.enabled!) : undefined}
          isEnabled={detailSkill.enabled}
          isToggling={togglingSkills.has(detailSkill.name)}
        />
      ) : null}
    </div>
  );
}
