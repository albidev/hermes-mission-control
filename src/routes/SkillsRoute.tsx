import { Brain, FolderTree, Power, PowerOff } from 'lucide-react';
import { Card } from '../components/ui/Card';
import { Badge } from '../components/ui/Badge';
import { useMissionControl } from '../lib/mission-control-store';

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
      <p className="text-lg font-semibold text-text mt-2">{value}</p>
      <p className="text-xs text-text-subtle mt-1">{hint}</p>
    </Card>
  );
}

export function SkillsRoute() {
  const { skills, snapshot } = useMissionControl();
  const enabled = skills.skills.filter((skill) => skill.enabled).length;
  const disabled = skills.skills.length - enabled;

  return (
    <div className="flex flex-col gap-6">
      <Card padding="none">
        <div className="px-4 pt-4 pb-3 border-b border-border-subtle flex items-center justify-between">
          <div className="flex flex-col gap-0.5">
            <span className="eyebrow">Skills</span>
            <h2 className="text-sm font-semibold text-text">Installed skill catalog</h2>
          </div>
          <Badge variant={skills.available ? 'positive' : 'warning'}>{skills.available ? 'live' : 'fallback'}</Badge>
        </div>

        <div className="p-4 grid grid-cols-1 md:grid-cols-2 xl:grid-cols-4 gap-3">
          <MetricCard icon={Brain} label="Skills" value={String(skills.count)} hint="configured abilities" />
          <MetricCard icon={Power} label="Enabled" value={String(enabled)} hint="currently active" />
          <MetricCard icon={PowerOff} label="Disabled" value={String(disabled)} hint="currently inactive" />
          <MetricCard icon={FolderTree} label="Model" value={snapshot.activeModel} hint="current primary model" />
        </div>
      </Card>

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
        <div className="px-4 pt-4 pb-3 border-b border-border-subtle">
          <span className="eyebrow">Catalog</span>
          <h3 className="text-sm font-semibold text-text mt-0.5">Every installed skill</h3>
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
    </div>
  );
}
