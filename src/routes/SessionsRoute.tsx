import { useMemo } from 'react';
import { Activity, Bot, Clock3, MessagesSquare } from 'lucide-react';
import { Card } from '../components/ui/Card';
import { Badge } from '../components/ui/Badge';
import { formatRelativeTime, formatTimestamp } from '../lib/format';
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

export function SessionsRoute() {
  const { snapshot } = useMissionControl();

  const sortedSessions = useMemo(
    () => [...snapshot.sessions.items].sort((a, b) => b.lastActive - a.lastActive),
    [snapshot.sessions.items],
  );

  return (
    <div className="flex flex-col gap-6">
      <Card padding="none">
        <div className="px-4 pt-4 pb-3 border-b border-border-subtle flex items-center justify-between">
          <div className="flex flex-col gap-0.5">
            <span className="eyebrow">Sessions</span>
            <h2 className="text-sm font-semibold text-text">Conversation runtime archive</h2>
          </div>
          <Badge variant="default">{snapshot.sessions.totalSessions} tracked</Badge>
        </div>

        <div className="p-4 grid grid-cols-1 md:grid-cols-2 xl:grid-cols-4 gap-3">
          <MetricCard
            icon={MessagesSquare}
            label="Tracked sessions"
            value={String(snapshot.sessions.totalSessions)}
            hint="total in storage"
          />
          <MetricCard
            icon={Activity}
            label="Messages"
            value={String(snapshot.sessions.totalMessages)}
            hint="conversation history"
          />
          <MetricCard
            icon={Bot}
            label="Active agents"
            value={String(snapshot.sessions.activeAgents)}
            hint="current snapshot"
          />
          <MetricCard
            icon={Clock3}
            label="Tool calls"
            value={String(snapshot.sessions.toolCallsToday)}
            hint="today"
          />
        </div>
      </Card>

      <Card padding="none">
        <div className="px-4 pt-4 pb-3 border-b border-border-subtle flex items-center justify-between">
          <div className="flex flex-col gap-0.5">
            <span className="eyebrow">Timeline</span>
            <h3 className="text-sm font-semibold text-text">Latest sessions</h3>
          </div>
          <span className="text-xs text-text-subtle">sorted by last activity</span>
        </div>

        <div className="divide-y divide-border-subtle">
          {sortedSessions.length > 0 ? (
            sortedSessions.map((session) => (
              <div key={session.id} className="px-4 py-3 flex flex-col gap-2">
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <p className="text-sm font-medium text-text truncate">{session.title}</p>
                    <p className="text-xs text-text-muted truncate">{session.source} · {session.model}</p>
                  </div>
                  <Badge variant="default">{session.messageCount} msgs</Badge>
                </div>

                <p className="text-xs text-text-muted line-clamp-2">
                  {session.preview || 'No preview available.'}
                </p>

                <div className="flex items-center justify-between text-xs text-text-subtle">
                  <span>Started {formatTimestamp(session.startedAt)}</span>
                  <span>Active {formatRelativeTime(session.lastActive)}</span>
                </div>
              </div>
            ))
          ) : (
            <div className="px-4 py-8 text-center text-sm text-text-muted italic">No sessions yet.</div>
          )}
        </div>
      </Card>
    </div>
  );
}
