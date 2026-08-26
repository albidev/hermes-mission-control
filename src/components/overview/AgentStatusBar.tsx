import { useI18n } from '../../lib/i18n';
import { useMissionControl } from '../../lib/mission-control-store';
import { Badge } from '../ui/Badge';
import { Card } from '../ui/Card';
import { Zap } from 'lucide-react';

const STATUS_LABELS: Record<string, string> = {
  idle: 'Idle',
  thinking: 'Thinking',
  'tool-use': 'Using tools',
  error: 'Error',
};

const STATUS_VARIANTS: Record<string, 'default' | 'positive' | 'warning' | 'negative'> = {
  idle: 'default',
  thinking: 'positive',
  'tool-use': 'warning',
  error: 'negative',
};

export function AgentStatusBar() {
  const { t } = useI18n();
  const { snapshot } = useMissionControl();

  // Infer agent status from gateway + session activity
  // For now, use gateway status as proxy; real status would come from backend
  const isActive = snapshot.gatewayStatus === 'online' && snapshot.activeAgents > 0;
  const status = isActive ? 'thinking' : 'idle';
  const variant = STATUS_VARIANTS[status] ?? 'default';

  return (
    <div className="border-b border-border bg-surface px-4 sm:px-6 py-2.5">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2 min-w-0">
        {/* Agent identity */}
        <div className="flex items-center gap-2 shrink-0">
          <Zap className="h-4 w-4 text-accent" />
          <span className="text-sm font-semibold text-text">{t('chatDrawer.eyebrow')}</span>
        </div>

        <div className="hidden sm:block h-4 w-px bg-border" />

        {/* Model */}
        <div className="flex items-center gap-1.5 min-w-0 max-w-full">
          <span className="text-xs text-text-muted shrink-0">{t('statusbar.model')}</span>
          <code
            className="text-xs font-mono bg-surface-raised px-1.5 py-0.5 rounded text-text truncate max-w-[170px] sm:max-w-[260px] md:max-w-none"
            title={snapshot.activeModel}
          >
            {snapshot.activeModel}
          </code>
        </div>

        <div className="hidden sm:block h-4 w-px bg-border" />

        {/* Status */}
        <div className="flex items-center gap-1.5 shrink-0">
          <Badge variant={variant} dot>
            {STATUS_LABELS[status] ?? status}
          </Badge>
        </div>

        {/* Fallback model */}
        <>
          <div className="hidden lg:block h-4 w-px bg-border" />
          <div className="hidden lg:flex items-center gap-1.5 min-w-0">
            <span className="text-xs text-text-subtle shrink-0">{t('statusbar.fallback')}</span>
            <code className="text-xs font-mono text-text-muted truncate max-w-[220px]" title={snapshot.fallbackModel}>
              {snapshot.fallbackModel}
            </code>
          </div>
        </>

        {/* Spacer */}
        <div className="hidden sm:block flex-1" />

        {/* Active agents count */}
        <div className="sm:ml-auto flex items-center gap-1.5 shrink-0">
          <span className="text-xs text-text-subtle">
            {snapshot.activeAgents} agent{snapshot.activeAgents !== 1 ? 's' : ''}
          </span>
        </div>
      </div>
    </div>
  );
}
