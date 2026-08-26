import { useI18n } from '../lib/i18n';
import { useRef, useState } from 'react';
import { Blocks, CheckCircle2, Hammer, KeyRound, ChevronRight } from 'lucide-react';
import { Card } from '../components/ui/Card';
import { Badge } from '../components/ui/Badge';
import { useMissionControl } from '../lib/mission-control-store';
import { Modal } from '../components/Modal';
import { usePullToReload } from '../hooks/usePullToReload';
import { PullToReloadIndicator } from '../components/PullToReloadIndicator';

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

function ToolDetailPanel({
  toolsetName,
  onClose,
}: {
  toolsetName: string;
  onClose: () => void;
}) {
  const { t } = useI18n();
  const { tools } = useMissionControl();
  const toolset = tools.availableToolsets.find((ts) => ts.name === toolsetName);
  const toolCatalog = tools.toolCatalog.filter((tc) => tc.toolset === toolsetName);

  return (
    <Modal
      open
      title={toolsetName}
      subtitle={toolset ? `${toolset.toolCount} tools · ${toolset.isComposite ? 'composite' : 'direct'}` : 'Toolset detail'}
      onClose={onClose}
    >
      <div className="flex flex-col gap-4">
        {toolset ? (
          <>
            <div className="flex flex-wrap gap-2">
              <Badge variant={toolset.available ? 'positive' : 'warning'}>
                {toolset.available ? 'available' : 'needs key'}
              </Badge>
              {toolset.isComposite ? <Badge variant="accent">{t('tools.composite')}</Badge> : <Badge variant="default">{t('tools.direct')}</Badge>}
            </div>

            {toolset.description ? (
              <p className="text-sm text-text-muted">{toolset.description}</p>
            ) : null}

            {toolset.requirements.length > 0 ? (
              <div>
                <p className="eyebrow mb-2">{t('tools.requirements')}</p>
                <div className="flex flex-wrap gap-2">
                  {toolset.requirements.map((req) => (
                    <Badge key={req} variant="warning">{req}</Badge>
                  ))}
                </div>
              </div>
            ) : null}

            {toolCatalog.length > 0 ? (
              <div>
                <p className="eyebrow mb-2">Tools ({toolCatalog.length})</p>
                <div className="flex flex-wrap gap-2">
                  {toolCatalog.map((tool) => (
                    <Badge key={tool.name} variant={tool.available ? 'positive' : 'default'}>
                      {tool.name}
                    </Badge>
                  ))}
                </div>
              </div>
            ) : null}

            {toolset.directTools.length > 0 ? (
              <div>
                <p className="eyebrow mb-2">{t('tools.directTools')}</p>
                <div className="flex flex-wrap gap-2">
                  {toolset.directTools.map((tool) => (
                    <Badge key={tool} variant="default">{tool}</Badge>
                  ))}
                </div>
              </div>
            ) : null}

            {toolset.includes.length > 0 ? (
              <div>
                <p className="eyebrow mb-2">{t('tools.includes')}</p>
                <div className="flex flex-wrap gap-2">
                  {toolset.includes.map((inc) => (
                    <Badge key={inc} variant="accent">{inc}</Badge>
                  ))}
                </div>
              </div>
            ) : null}
          </>
        ) : (
          <p className="text-sm text-text-muted">{t('tools.notFound')}</p>
        )}
      </div>
    </Modal>
  );
}

export function ToolsRoute() {
  const { t } = useI18n();
  const { tools } = useMissionControl();
  const [detailTool, setDetailTool] = useState<string | null>(null);
  const containerRef = useRef<HTMLDivElement | null>(null);

  const { state: pullState } = usePullToReload({
    containerRef,
    onReload: async () => {
      // Tools data is refreshed via the store polling; manual reload triggers refreshAll through the action.
      // Left intentionally lightweight because tools are refreshed automatically.
    },
  });

  const readyCount = tools.availableToolsets.filter((toolset) => toolset.available).length;
  const blockedCount = tools.availableToolsets.filter((toolset) => !toolset.available).length;

  return (
    <div ref={containerRef} className="flex flex-col gap-6 h-full overflow-y-auto">
      <PullToReloadIndicator state={pullState} />
      <Card padding="none">
        <div className="px-4 pt-4 pb-3 border-b border-border-subtle flex items-center justify-between">
          <div className="flex flex-col gap-0.5">
            <span className="eyebrow">{t('nav.tools')}</span>
            <h2 className="text-sm font-semibold text-text">{t('tools.title')}</h2>
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
          <span className="eyebrow">{t('tools.toolsets')}</span>
          <h3 className="text-sm font-semibold text-text mt-0.5">{t('tools.groupedByAvailability')}</h3>
        </div>

        <div className="divide-y divide-border-subtle">
          {tools.availableToolsets.map((toolset) => (
            <div
              key={toolset.name}
              className="px-4 py-3 flex flex-col gap-2 cursor-pointer hover:bg-surface-raised/40 transition-colors"
              onClick={() => setDetailTool(toolset.name)}
              role="button"
              tabIndex={0}
              onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') setDetailTool(toolset.name); }}
            >
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <p className="text-sm font-medium text-text truncate">{toolset.name}</p>
                  <p className="text-xs text-text-muted line-clamp-2">{toolset.description || 'No description provided.'}</p>
                </div>
                <div className="flex items-center gap-2 shrink-0">
                  <Badge variant={toolset.available ? 'positive' : 'warning'}>
                    {toolset.available ? 'available' : 'needs key'}
                  </Badge>
                  <ChevronRight className="h-4 w-4 text-text-subtle" />
                </div>
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

      {detailTool ? (
        <ToolDetailPanel
          toolName={detailTool}
          onClose={() => setDetailTool(null)}
        />
      ) : null}
    </div>
  );
}
