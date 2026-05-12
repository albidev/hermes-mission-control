import { useEffect, useMemo, useState, type ElementType } from 'react';
import { Brain, FolderTree, LibraryBig, Power, RefreshCw, Search } from 'lucide-react';
import { Card } from '../components/ui/Card';
import { Badge } from '../components/ui/Badge';
import { Button } from '../components/ui/Button';
import { loadMissionControlSkillsCatalog, type MissionControlSkillsCatalogSnapshot } from '../lib/hermes-api';
import { useMissionControl } from '../lib/mission-control-store';

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

export function SkillsRoute() {
  const { skills, snapshot, storedToken } = useMissionControl();
  const [activeTab, setActiveTab] = useState<'installed' | 'catalog'>('installed');
  const [catalog, setCatalog] = useState<MissionControlSkillsCatalogSnapshot | null>(null);
  const [catalogLoading, setCatalogLoading] = useState(false);
  const [catalogError, setCatalogError] = useState<string | null>(null);
  const [query, setQuery] = useState('');

  const enabled = skills.skills.filter((skill) => skill.enabled).length;
  const disabled = skills.skills.length - enabled;
  const installedNames = useMemo(() => new Set(skills.skills.map((skill) => skill.name.toLowerCase())), [skills.skills]);

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
    <div className="flex flex-col gap-6">
      <Card padding="none">
        <div className="px-4 pt-4 pb-3 border-b border-border-subtle flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
          <div className="flex flex-col gap-0.5">
            <span className="eyebrow">Skills</span>
            <h2 className="text-sm font-semibold text-text">Installed skills and Skills Hub catalog</h2>
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
        <>
          <Card padding="none">
            <div className="px-4 pt-4 pb-3 border-b border-border-subtle flex items-center justify-between">
              <div className="flex flex-col gap-0.5">
                <span className="eyebrow">Categories</span>
                <h3 className="text-sm font-semibold text-text">Skill families</h3>
              </div>
              <span className="text-xs text-text-subtle">{skills.categories.length} categories</span>
            </div>

            <div className="p-4 flex flex-wrap gap-2">
              {skills.categories.map((category) => (
                <Badge key={category.name} variant="default">
                  {category.name} · {category.count}
                </Badge>
              ))}
            </div>
          </Card>

          <Card padding="none">
            <div className="px-4 pt-4 pb-3 border-b border-border-subtle flex items-center justify-between">
              <div>
                <span className="eyebrow">Installed</span>
                <h3 className="text-sm font-semibold text-text mt-0.5">Every installed skill</h3>
              </div>
              <Badge variant="default">{disabled} disabled</Badge>
            </div>

            <div className="divide-y divide-border-subtle">
              {skills.skills.map((skill) => (
                <div key={skill.id} className="px-4 py-3 flex flex-col gap-2">
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0">
                      <p className="text-sm font-medium text-text truncate">{skill.name}</p>
                      <p className="text-xs text-text-muted line-clamp-2">{skill.description}</p>
                    </div>
                    <Badge variant={skill.enabled ? 'positive' : 'warning'}>
                      {skill.enabled ? 'enabled' : 'disabled'}
                    </Badge>
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
        </>
      ) : (
        <Card padding="none">
          <div className="px-4 pt-4 pb-3 border-b border-border-subtle flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
            <div>
              <span className="eyebrow">Skills Hub</span>
              <h3 className="text-sm font-semibold text-text mt-0.5">Discoverable skill catalog</h3>
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
                  placeholder="Search catalog"
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
                      {installed ? <Badge variant="positive">installed</Badge> : <Badge variant="default">available</Badge>}
                      <Badge variant={statusVariant(skill.trustLevel)}>{skill.trustLevel}</Badge>
                    </div>
                  </div>

                  <div className="text-xs text-text-subtle truncate">
                    {skill.source} · {skill.identifier}{skill.repo ? ` · ${skill.repo}` : ''}{skill.path ? ` · ${skill.path}` : ''}
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
              <div className="px-4 py-8 text-sm text-text-muted">No catalog skills match this filter.</div>
            ) : null}
          </div>
        </Card>
      )}
    </div>
  );
}
