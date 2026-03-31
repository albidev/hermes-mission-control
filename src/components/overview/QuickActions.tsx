import { RefreshCw, Rocket, Settings } from 'lucide-react';
import type { MissionControlGatewayAction } from '../../lib/mission-control-store';
import { Card } from '../ui/Card';
import { Button } from '../ui/Button';

type QuickActionsProps = {
  gatewayActions: MissionControlGatewayAction[];
  runGatewayAction: (action: MissionControlGatewayAction) => Promise<void>;
  actionLoading: string | null;
};

export function QuickActions({
  gatewayActions,
  runGatewayAction,
  actionLoading,
}: QuickActionsProps) {
  const refreshAction = gatewayActions.find((a) => a.id === 'refresh');
  const restartAction = gatewayActions.find((a) => a.id === 'restart-gateway');

  return (
    <Card padding="none">
      <div className="flex items-center justify-between px-4 pt-4 pb-3 border-b border-border-subtle">
        <div className="flex flex-col gap-0.5">
          <span className="eyebrow">Controls</span>
          <h2 className="text-sm font-semibold text-text">Quick actions</h2>
        </div>
      </div>

      <div className="p-4 flex flex-wrap items-center gap-2">
        {refreshAction && (
          <Button
            variant="secondary"
            size="sm"
            icon={<RefreshCw className="h-3.5 w-3.5" />}
            loading={actionLoading === 'refresh'}
            onClick={() => void runGatewayAction(refreshAction)}
          >
            Refresh
          </Button>
        )}

        {restartAction && (
          <Button
            variant="secondary"
            size="sm"
            icon={<Rocket className="h-3.5 w-3.5" />}
            loading={actionLoading === 'restart-gateway'}
            onClick={() => void runGatewayAction(restartAction)}
          >
            Restart gateway
          </Button>
        )}

        <div className="flex-1" />

        <Button
          variant="ghost"
          size="sm"
          icon={<Settings className="h-3.5 w-3.5" />}
          onClick={() => {
            window.location.hash = '#settings';
          }}
        >
          Settings
        </Button>
      </div>
    </Card>
  );
}
