import { useEffect, useState } from 'react';
import { useI18n } from '../../lib/i18n';
import { useMissionControl } from '../../lib/mission-control-store';
import { loadPrimaryProfileDisplayName } from '../../lib/bot-gateway';
import { Badge } from '../ui/Badge';
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
  const { snapshot, storedToken, authRequired } = useMissionControl();
  const [identity, setIdentity] = useState<{ token: string; name: string | null } | null>(null);
  const displayName = !authRequired && identity?.token === storedToken ? identity.name : null;

  useEffect(() => {
    if (authRequired || !storedToken.trim()) {
      setIdentity(null);
      return;
    }
    let cancelled = false;
    let requestSequence = 0;
    const refreshIdentity = () => {
      const requestId = ++requestSequence;
      void loadPrimaryProfileDisplayName(storedToken).then((name) => {
        if (!cancelled && requestId === requestSequence) setIdentity({ token: storedToken, name });
      }).catch(() => { /* Identity is optional; keep the last name on transient failures. */ });
    };
    refreshIdentity();
    window.addEventListener('focus', refreshIdentity);
    return () => {
      cancelled = true;
      window.removeEventListener('focus', refreshIdentity);
    };
  }, [authRequired, storedToken]);

  // Infer agent status from gateway + session activity
  // For now, use gateway status as proxy; real status would come from backend
  const isActive = snapshot.gatewayStatus === 'online' && snapshot.activeAgents > 0;
  const status = isActive ? 'thinking' : 'idle';
  const variant = STATUS_VARIANTS[status] ?? 'default';

  return (
    <div className="border-b border-border bg-surface px-4 sm:px-6 py-2.5">
      <div className="agent-status-layout">
        {/* Agent identity */}
        <div className="agent-status-identity flex min-w-0 max-w-full items-center gap-2">
          <Zap className="h-4 w-4 shrink-0 text-accent" />
          <span className="truncate max-w-[16rem] text-sm font-semibold text-text" title={displayName || t('chatDrawer.eyebrow')}>{displayName || t('chatDrawer.eyebrow')}</span>
        </div>

        <div className="agent-status-separator h-4 w-px bg-border" aria-hidden="true" />

        {/* Model */}
        <div className="agent-status-model flex items-center gap-1.5 min-w-0 max-w-full">
          <span className="sr-only min-[640px]:not-sr-only text-xs text-text-muted shrink-0">{t('statusbar.model')}</span>
          <code
            className="min-w-0 flex-1 text-xs font-mono bg-surface-raised px-1.5 py-0.5 rounded text-text truncate min-[640px]:flex-none min-[640px]:max-w-[260px] md:max-w-none"
            title={snapshot.activeModel}
          >
            {snapshot.activeModel}
          </code>
        </div>

        <div className="agent-status-separator h-4 w-px bg-border" aria-hidden="true" />

        {/* Status */}
        <div className="agent-status-state flex items-center gap-1.5 shrink-0">
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

        {/* Active agents count */}
        <div className="agent-status-count flex items-center gap-1.5 shrink-0">
          <span className="text-xs text-text-subtle">
            {snapshot.activeAgents} agent{snapshot.activeAgents !== 1 ? 's' : ''}
          </span>
        </div>
      </div>
    </div>
  );
}
