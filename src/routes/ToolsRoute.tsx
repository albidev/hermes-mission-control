import { Blocks, CheckCircle2, Hammer, KeyRound } from 'lucide-react';
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

export function ToolsRoute() {
  const { tools } = useMissionControl();

  const readyCount = tools.availableToolsets.filter((toolset) => toolset.available).length;
  const blockedCount = tools.availableToolsets.filter((toolset) => !toolset.available).length;

  return (
    <div className="flex flex-col gap-6">
      <Card padding="none">
        <div className="px-4 pt-4 pb-3 border-b border-border-subtle flex items-center justify-between">
          <div className="flex flex-col gap-0.5">
            <span className="eyebrow">Tools</span>
            <h2 className="text-sm font-semibold text-text">Runtime tool inventory</h2>
          </div>
          <Badge variant={tools.available ? 'positive' : 'warning'}>{tools.available ? 'live' : 'fallback'}</Badge>
        </div>

        <div className="p-4 grid grid-cols-1 md:grid-cols-2 xl:grid-cols-4 gap-3">
          <MetricCard icon={Blocks} label="Toolsets" value={String(tools.availableToolsets.length)} hint="catalogued groups" />
          <MetricCard icon={CheckCircle2} label="Ready" value={String(readyCount)} hint="available now" />
          <MetricCard icon={KeyRound} label="Needs keys" value={String(blockedCount)} hint="waiting on env" />
          <MetricCard icon={Hammer} label="Tools" value={String(tools.toolCatalog.length)} hint="registered handlers" />
        </div>
      </Card>

      <Card padding="none">
        <div className="px-4 pt-4 pb-3 border-b border-border-subtle">
          <span className="eyebrow">Toolsets</span>
          <h3 className="text-sm font-semibold text-text mt-0.5">Grouped by availability</h3>
        </div>

        <div className="divide-y divide-border-subtle">
          {tools.availableToolsets.map((toolset) => (
            <div key={toolset.name} className="px-4 py-3 flex flex-col gap-2">
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <p className="text-sm font-medium text-text truncate">{toolset.name}</p>
                  <p className="text-xs text-text-muted line-clamp-2">{toolset.description || 'No description provided.'}</p>
                </div>
                <Badge variant={toolset.available ? 'positive' : 'warning'}>
                  {toolset.available ? 'available' : 'needs key'}
                </Badge>
              </div>

              <div className="text-xs text-text-subtle">
                {toolset.toolCount} tools · {toolset.isComposite ? 'composite' : 'direct'}
              </div>

              {toolset.resolvedTools.length > 0 ? (
                <div className="flex flex-wrap gap-2">
                  {toolset.resolvedTools.slice(0, 10).map((tool) => (
                    <Badge key={tool} variant="default">{tool}</Badge>
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
