import { useI18n } from '../lib/i18n';
import { useCallback, useEffect, useMemo, useRef, useState, type ElementType } from 'react';
import { Brain, Download, FolderTree, LibraryBig, Power, RefreshCw, Search, FileText, X } from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import remarkBreaks from 'remark-breaks';
import { Card } from '../components/ui/Card';
import { Badge } from '../components/ui/Badge';
import { Button } from '../components/ui/Button';
import { ToggleSwitch } from '../components/ui/ToggleSwitch';
import { Modal } from '../components/Modal';
import { PageHeader } from '../components/PageHeader';
import {
  loadMissionControlSkillsCatalog,
  loadMissionControlSkillFiles,
  installMissionControlSkill,
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
  color,
}: {
  icon: ElementType;
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
          <p className="mt-1 text-[11px] leading-relaxed text-text-subtle">{hint}</p>
        </div>
        <Icon className={`h-[18px] w-[18px] shrink-0 ${color}`} />
      </div>
    </div>
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
  accessToken,
  onClose,
  onToggle,
  isEnabled,
  isToggling,
}: {
  skillName: string;
  skillDescription?: string;
  accessToken?: string;
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

    loadMissionControlSkillFiles(skillName, accessToken)
      .then((result) => {
        if (cancelled) return;
        if (result) {
          setFiles(result);
          const mdFile = result.files.find((f) => f.name.toLowerCase().endsWith('.md'));
          if (mdFile) setActiveFile(mdFile);
        } else {
          setFilesError(t('skills.detail.loadFailed'));
        }
      })
      .catch(() => {
        if (!cancelled) setFilesError(t('skills.detail.loadFailed'));
      })
      .finally(() => {
        if (!cancelled) setFilesLoading(false);
      });

    return () => { cancelled = true; };
  }, [accessToken, skillName, t]);

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
              {isToggling ? t('skills.detail.updating') : isEnabled ? t('skills.detail.enabled') : t('skills.detail.disabled')}
            </span>
            <Button
              size="sm"
              variant={isEnabled ? 'secondary' : 'primary'}
              onClick={onToggle}
              disabled={isToggling}
            >
              {isEnabled ? t('skills.detail.disable') : t('skills.detail.enable')}
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
              <p className="eyebrow mb-2">{t('skills.detail.eyebrow')}</p>
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
                    <span className="text-xs text-text-muted shrink-0">{t('skills.detail.bytes', { count: activeFile.size })}</span>
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
  const [installingSkills, setInstallingSkills] = useState<Set<string>>(new Set());
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
        setCatalogError(nextCatalog.hint ?? t('skills.catalogUnavailable'));
      }
    } catch (error) {
      setCatalogError(error instanceof Error ? error.message : t('skills.catalogUnavailable'));
    } finally {
      setCatalogLoading(false);
    }
  };

  const handleInstall = useCallback(async (identifier: string, skillName: string) => {
    if (!window.confirm(t('skills.installConfirm', { name: skillName }))) return;
    setInstallingSkills((previous) => new Set(previous).add(identifier));
    setCatalogError(null);
    try {
      const result = await installMissionControlSkill(identifier, storedToken || undefined);
      if (!result?.success || !result.verified) {
        setCatalogError(t('skills.installFailed'));
        return;
      }
      await Promise.all([
        refreshAll(storedToken || undefined, { silent: true, includeReference: true }),
        refreshCatalog(),
      ]);
    } catch {
      setCatalogError(t('skills.installFailed'));
    } finally {
      setInstallingSkills((previous) => {
        const next = new Set(previous);
        next.delete(identifier);
        return next;
      });
    }
  }, [refreshAll, storedToken, t]);

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
    <div ref={containerRef} className="route-page-scroll flex h-full flex-col gap-5 overflow-y-auto sm:gap-6">
      <PullToReloadIndicator state={pullState} />
      <Card padding="none" className="!border-0">
        <PageHeader
          eyebrow={t('skills.eyebrow')}
          title={t('skills.title')}
          description={t('skills.description')}
          meta={(
            <div className="flex items-center justify-end">
              <span className="flex items-center gap-1.5 sm:hidden">
                <span className={`h-1.5 w-1.5 rounded-full ${skills.available && catalog?.available ? 'bg-positive' : 'bg-warning'}`} />
                {skills.available && catalog?.available ? t('tools.live') : t('tools.fallback')}
              </span>
              <div className="hidden items-center gap-1.5 sm:flex">
                <Badge variant={skills.available ? 'positive' : 'warning'}>
                  {skills.available ? t('skills.installedLive') : t('skills.installedFallback')}
                </Badge>
                <Badge variant={catalog?.available ? 'positive' : 'warning'}>
                  {catalog?.available ? t('skills.catalogLive') : t('skills.catalogFallback')}
                </Badge>
              </div>
            </div>
          )}
        />

        <div className="grid grid-cols-2 gap-2.5 p-3 sm:gap-3 sm:p-4 xl:grid-cols-4">
          <MetricCard icon={Brain} label={t('skills.installed')} value={String(skills.count)} hint={t('skills.localCapabilities', { count: catalog?.count ?? 0 })} color="text-sky-400" />
          <MetricCard icon={LibraryBig} label={t('skills.catalog')} value={String(catalog?.count ?? 0)} hint={t('skills.discoverable')} color="text-violet-400" />
          <MetricCard icon={Power} label={t('skills.enabled')} value={String(enabled)} hint={t('skills.currentlyActive')} color="text-emerald-400" />
          <MetricCard icon={FolderTree} label={t('skills.model')} value={snapshot.activeModel} hint={t('skills.currentPrimaryModel')} color="text-amber-400" />
        </div>
      </Card>

      <div role="tablist" aria-label={t('skills.title')} className="flex min-h-11 shrink-0 flex-nowrap gap-1.5 overflow-x-auto pb-1 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden sm:flex-wrap sm:overflow-visible sm:pb-0">
        <Button type="button" role="tab" aria-selected={activeTab === 'installed'} size="sm" variant={activeTab === 'installed' ? 'primary' : 'secondary'} className="shrink-0 whitespace-nowrap" onClick={() => setActiveTab('installed')}>
          {t('skills.installed')}
        </Button>
        <Button type="button" role="tab" aria-selected={activeTab === 'catalog'} size="sm" variant={activeTab === 'catalog' ? 'primary' : 'secondary'} className="shrink-0 whitespace-nowrap" onClick={() => setActiveTab('catalog')}>
          {t('skills.catalog')}
        </Button>
      </div>

      {activeTab === 'installed' ? (
        <Card padding="none" className="!border-0">
          <div className="flex flex-col gap-3 border-b border-border-subtle/60 px-4 pb-3 pt-4 lg:flex-row lg:items-center lg:justify-between">
            <div className="min-w-0">
              <span className="eyebrow">{t('skills.installed')}</span>
              <h3 className="mt-0.5 text-sm font-semibold text-text">{t('skills.everyInstalled')}</h3>
              <p className="mt-1 text-xs text-text-subtle">{t('skills.countSummary', { count: skills.skills.length, enabled, disabled })}</p>
            </div>
          </div>

          <div className="divide-y divide-border-subtle">
            {skills.skills.map((skill) => (
              <article
                key={skill.id}
                className="flex cursor-pointer flex-col gap-2 px-4 py-3 transition-colors hover:bg-surface-raised/40"
              >
                <div className="flex items-start justify-between gap-3">
                  <button
                    type="button"
                    className="min-w-0 flex-1 text-left"
                    onClick={() => setDetailSkill({ name: skill.name, description: skill.description, enabled: skill.enabled })}
                    aria-label={`${skill.name}: ${skill.description}`}
                  >
                    <p className="text-sm font-medium text-text truncate">{skill.name}</p>
                    <p className="text-xs text-text-muted line-clamp-2">{skill.description}</p>
                  </button>
                  <div className="flex shrink-0 items-center gap-3">
                    <ToggleSwitch
                      id={`toggle-${skill.id}`}
                      checked={skill.enabled}
                      disabled={togglingSkills.has(skill.name)}
                      onChange={() => handleToggle(skill.name, skill.enabled)}
                      label={skill.enabled ? t('skills.enabledLabel') : t('skills.disabledLabel')}
                    />
                  </div>
                </div>

                <div className="text-xs text-text-subtle">
                  {(skill.category || t('skills.uncategorized')) + (skill.model ? ` · ${skill.model}` : '')}
                </div>

                {skill.tags.length > 0 ? (
                  <div className="flex flex-wrap gap-2">
                    {skill.tags.map((tag) => (
                      <Badge key={tag} variant="default">{tag}</Badge>
                    ))}
                  </div>
                ) : null}
                </article>
                ))}
          </div>
        </Card>
      ) : (
        <Card padding="none" className="!border-0">
          <div className="flex flex-col gap-3 border-b border-border-subtle/60 px-4 pb-3 pt-4 lg:flex-row lg:items-center lg:justify-between">
            <div className="min-w-0">
              <span className="eyebrow">{t('skills.hub')}</span>
              <h3 className="mt-0.5 text-sm font-semibold text-text">{t('skills.discoverableCatalog')}</h3>
              <p className="mt-1 text-xs text-text-subtle">
                {t(catalogInstalledCount === 1 ? 'skills.matchesSummary' : 'skills.matchesSummaryPlural', { matches: filteredCatalogSkills.length, rendered: displayedCatalogSkills.length, installed: catalogInstalledCount })}
              </p>
            </div>
            <div className="flex min-w-0 flex-row items-center gap-2">
              <label className="relative min-w-0 flex-1 sm:flex-none">
                <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-text-subtle" />
                <input
                  value={query}
                  onChange={(event) => setQuery(event.target.value)}
                  placeholder={t('skills.search')}
                  aria-label={t('skills.search')}
                  className="h-11 w-full min-w-0 rounded-lg border border-border bg-surface px-9 text-sm text-text outline-none focus:border-accent sm:h-9 sm:w-64"
                />
              </label>
              <Button
                size="sm"
                variant="secondary"
                iconOnly
                loading={catalogLoading}
                icon={<RefreshCw className="h-4 w-4" />}
                onClick={() => void refreshCatalog()}
                aria-label={t('skills.refresh')}
                title={t('skills.refresh')}
                className="shrink-0 sm:w-auto sm:min-w-0 sm:px-2.5"
              ><span className="hidden sm:inline">{t('skills.refresh')}</span></Button>
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
                  {t('skills.timedOut', { source })}
                </Badge>
              ))}
            </div>
          ) : null}

          <div className="divide-y divide-border-subtle">
            {displayedCatalogSkills.map((skill) => {
              const installed = skill.installed || installedNames.has(skill.name.toLowerCase());
              return (
                <div key={`${skill.source}:${skill.identifier}`} className="flex flex-col gap-2 px-4 py-3">
                  <div className="flex items-start gap-3">
                    <div className="min-w-0 flex-1">
                      <p className="text-sm font-medium text-text truncate">{skill.name}</p>
                      <p className="text-xs text-text-muted line-clamp-2">{skill.description || t('skills.noDescription')}</p>
                    </div>
                  </div>

                  <div className="flex min-w-0 items-center justify-between gap-2">
                    <div className="min-w-0 truncate text-xs text-text-muted">
                      {skill.path || skill.repo || skill.source}
                    </div>
                    <Badge variant={statusVariant(skill.trustLevel)}>{skill.trustLevel}</Badge>
                  </div>

                  <div className="flex min-w-0 items-center justify-between gap-2">
                    <div className="flex min-w-0 flex-wrap gap-2">
                      {skill.tags.slice(0, 8).map((tag) => (
                        <Badge key={tag} variant="default">{tag}</Badge>
                      ))}
                    </div>
                    <div className="shrink-0">
                      {installed ? <Badge variant="positive">{t('skills.installedBadge')}</Badge> : (
                        <Button
                          size="sm"
                          variant="primary"
                          iconOnly
                          loading={installingSkills.has(skill.identifier)}
                          icon={<Download className="h-4 w-4" />}
                          onClick={() => void handleInstall(skill.identifier, skill.name)}
                          aria-label={`${t('skills.install')}: ${skill.name}`}
                          title={t('skills.install')}
                          className="skills-install-button sm:w-auto sm:min-w-0 sm:px-2.5"
                        ><span className="hidden sm:inline">{t('skills.install')}</span></Button>
                      )}
                    </div>
                  </div>
                </div>
              );
            })}

            {!catalogLoading && filteredCatalogSkills.length > displayedCatalogSkills.length ? (
              <div className="px-4 py-4 text-xs text-text-subtle">
                {t('skills.renderingFirst', { count: displayedCatalogSkills.length, total: filteredCatalogSkills.length })}
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
          accessToken={storedToken || undefined}
          onClose={() => setDetailSkill(null)}
          onToggle={detailSkill.enabled !== undefined ? () => handleToggle(detailSkill.name, detailSkill.enabled!) : undefined}
          isEnabled={detailSkill.enabled}
          isToggling={togglingSkills.has(detailSkill.name)}
        />
      ) : null}
    </div>
  );
}
