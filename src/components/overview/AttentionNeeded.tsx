import { Link } from 'react-router-dom';
import { AlertCircle, Bell, CheckCircle, Clock, PanelLeftClose } from 'lucide-react';
import type { MissionControlAlert } from '../../lib/hermes-api';
import { Card } from '../ui/Card';
import { Badge } from '../ui/Badge';

type AttentionNeededProps = {
  alerts: MissionControlAlert[];
};

function SectionLink({
  to,
  children,
}: {
  to: string;
  children: React.ReactNode;
}) {
  return (
    <Link
      to={to}
      className="inline-flex items-center gap-1 text-xs text-text-muted hover:text-accent transition-colors"
    >
      {children}
    </Link>
  );
}

export function AttentionNeeded({ alerts }: AttentionNeededProps) {
  const errorAlerts = alerts.filter((a) => a.tone === 'bad');
  const warnAlerts = alerts.filter((a) => a.tone === 'warn');
  const goodAlerts = alerts.filter((a) => a.tone === 'good');

  const totalCount = errorAlerts.length + warnAlerts.length;
  const hasAttention = totalCount > 0;

  return (
    <Card padding="none">
      <div className="flex items-center justify-between px-4 pt-4 pb-3 border-b border-border-subtle">
        <div className="flex flex-col gap-0.5">
          <span className="eyebrow">Attention</span>
          <h2 className="text-sm font-semibold text-text">Needs review</h2>
        </div>
        {hasAttention ? (
          <Badge variant={errorAlerts.length > 0 ? 'negative' : 'warning'}>
            {totalCount}
          </Badge>
        ) : (
          <Badge variant="positive">
            <CheckCircle className="h-3 w-3" />
            All clear
          </Badge>
        )}
      </div>

      <div className="p-4 flex flex-col gap-3">
        {hasAttention ? (
          <>
            {/* Errors first */}
            {errorAlerts.slice(0, 2).map((alert) => (
              <div key={alert.id} className="flex items-start gap-2">
                <AlertCircle className="h-4 w-4 text-negative mt-0.5 flex-shrink-0" />
                <div className="flex-1 min-w-0">
                  <p className="text-sm font-medium text-text truncate">{alert.title}</p>
                  <p className="text-xs text-text-muted line-clamp-1 mt-0.5">
                    {alert.detail}
                  </p>
                </div>
                <Badge variant="negative" className="flex-shrink-0">
                  {alert.category}
                </Badge>
              </div>
            ))}

            {/* Warnings */}
            {warnAlerts.slice(0, 2).map((alert) => (
              <div key={alert.id} className="flex items-start gap-2">
                <Clock className="h-4 w-4 text-warning mt-0.5 flex-shrink-0" />
                <div className="flex-1 min-w-0">
                  <p className="text-sm font-medium text-text truncate">{alert.title}</p>
                  <p className="text-xs text-text-muted line-clamp-1 mt-0.5">
                    {alert.detail}
                  </p>
                </div>
                <Badge variant="warning" className="flex-shrink-0">
                  {alert.category}
                </Badge>
              </div>
            ))}
          </>
        ) : (
          <div className="flex flex-col items-center gap-2 py-4 text-center">
            <CheckCircle className="h-8 w-8 text-positive" />
            <p className="text-sm text-text-muted">No issues detected.</p>
          </div>
        )}

        {/* Footer link */}
        <div className="flex items-center justify-between pt-2 border-t border-border-subtle">
          <SectionLink to="/sessions">
            <PanelLeftClose className="h-3 w-3" />
            <span>View all</span>
          </SectionLink>
          <span className="text-xs text-text-subtle">
            {goodAlerts.length > 0 ? `${goodAlerts.length} info` : ''}
          </span>
        </div>
      </div>
    </Card>
  );
}
