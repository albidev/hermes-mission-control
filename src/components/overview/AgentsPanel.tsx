import { Sparkles } from 'lucide-react';
import type { MissionControlSessionsSnapshot } from '../../lib/hermes-api';
import { Card } from '../ui/Card';
import { Badge } from '../ui/Badge';
import { formatRelativeTime } from '../../lib/format';

export function AgentsPanel({
  activeAgents,
  sessions,
}: {
  activeAgents: number;
  sessions: MissionControlSessionsSnapshot;
}) {
  const liveSessions = sessions.items.filter((session) => session.endedAt === null);
  const modelCount = new Map<string, number>();

  for (const session of liveSessions) {
    const model = session.model || 'unknown';
    modelCount.set(model, (modelCount.get(model) ?? 0) + 1);
  }

  const topModels = [...modelCount.entries()]
    .sort((a, b) => b[1] - a[1])
    .slice(0, 3);

  return (
    <Card padding="none">
      <div className="flex items-center justify-between px-4 pt-4 pb-3 border-b border-border-subtle">
        <div className="flex flex-col gap-0.5">
          <span className="eyebrow">Agents</span>
          <h2 className="text-sm font-semibold text-text">Runtime footprint</h2>
        </div>
        <Badge variant={activeAgents > 0 ? 'positive' : 'default'} dot>
          {activeAgents} active
        </Badge>
      </div>

      <div className="p-3 flex flex-col gap-2">
        <div className="flex items-center justify-between text-sm">
          <span className="text-text-muted">Live sessions</span>
          <span className="font-medium text-text">{liveSessions.length}</span>
        </div>

        <div className="flex items-center justify-between text-sm">
          <span className="text-text-muted">Tracked sessions</span>
          <span className="font-medium text-text">{sessions.totalSessions}</span>
        </div>

        <div className="pt-2 border-t border-border-subtle flex flex-col gap-2">
          <div className="flex items-center gap-1.5 text-xs text-text-muted">
            <Sparkles className="h-3.5 w-3.5" />
            Top models
          </div>

          {topModels.length > 0 ? (
            <div className="flex flex-wrap gap-2">
              {topModels.map(([model, count]) => (
                <Badge key={model} variant="default">
                  {model} · {count}
                </Badge>
              ))}
            </div>
          ) : (
            <p className="text-xs text-text-subtle italic">No live model usage right now.</p>
          )}
        </div>
      </div>
    </Card>
  );
}
