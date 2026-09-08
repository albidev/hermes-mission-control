import React from 'react';
import { useI18n } from '../../lib/i18n';
import { Link } from 'react-router-dom';
import { AlertCircle, CheckCircle, Clock, PanelLeftClose } from 'lucide-react';
import type { MissionControlAlert } from '../../lib/hermes-api';
import { Card } from '../ui/Card';
import { Badge } from '../ui/Badge';
import type { MCPluginAttentionContributor } from '../../core/plugins/types';

type AttentionNeededProps = {
  alerts: MissionControlAlert[];
  pluginContributors?: MCPluginAttentionContributor[];
};

function SectionLink({ to, label, children }: { to: string; label: string; children: React.ReactNode }) {
  return (
    <Link to={to} className="inline-flex h-11 w-11 items-center justify-center rounded-[var(--control-radius)] text-text-muted transition-colors hover:bg-surface-sunken hover:text-accent sm:h-auto sm:w-auto sm:justify-start sm:gap-1 sm:rounded-none sm:bg-transparent" aria-label={label} title={label}>
      <span aria-hidden="true">{children}</span>
      <span className="hidden sm:inline">{label}</span>
    </Link>
  );
}

export function AttentionNeeded({ alerts, pluginContributors = [] }: AttentionNeededProps) {
  const { t } = useI18n();
  const errorAlerts = alerts.filter((a) => a.tone === 'bad');
  const warnAlerts = alerts.filter((a) => a.tone === 'warn');
  const goodAlerts = alerts.filter((a) => a.tone === 'good');
  const [pluginCounts, setPluginCounts] = React.useState<Record<string, number>>({});
  const totalPluginAttention = Object.values(pluginCounts).reduce((sum, count) => sum + count, 0);
  const totalCount = errorAlerts.length + warnAlerts.length + totalPluginAttention;
  const hasAttention = totalCount > 0;

  return (
    <Card padding="none">
      <div className="flex items-center justify-between px-3 pt-3 pb-2 border-b border-border-subtle">
        <div className="flex flex-col gap-0.5"><span className="eyebrow">{t('attention.eyebrow')}</span><h2 className="text-sm font-semibold text-text">{t('attention.title')}</h2></div>
        {hasAttention ? <Badge variant={errorAlerts.length > 0 ? 'negative' : 'warning'}>{totalCount}</Badge> : <Badge variant="positive"><CheckCircle className="h-3 w-3" />All clear</Badge>}
      </div>
      <div className="p-3 flex flex-col gap-2.5">
        {pluginContributors.map((contributor) => {
          const Component = contributor.component;
          return <Component key={contributor.id} onActiveChange={(count) => setPluginCounts((current) => current[contributor.id] === count ? current : { ...current, [contributor.id]: count })} />;
        })}
        {errorAlerts.slice(0, 2).map((alert) => <div key={alert.id} className="flex items-start gap-2"><AlertCircle className="h-4 w-4 text-negative mt-0.5 flex-shrink-0" /><div className="flex-1 min-w-0"><p className="text-sm font-medium text-text truncate">{alert.title}</p><p className="text-xs text-text-muted line-clamp-1 mt-0.5">{alert.detail}</p></div><Badge variant="negative" className="flex-shrink-0">{alert.category}</Badge></div>)}
        {warnAlerts.slice(0, 2).map((alert) => <div key={alert.id} className="flex items-start gap-2"><Clock className="h-4 w-4 text-warning mt-0.5 flex-shrink-0" /><div className="flex-1 min-w-0"><p className="text-sm font-medium text-text truncate">{alert.title}</p><p className="text-xs text-text-muted line-clamp-1 mt-0.5">{alert.detail}</p></div><Badge variant="warning" className="flex-shrink-0">{alert.category}</Badge></div>)}
        {!hasAttention && <div className="flex flex-col items-center gap-2 py-3 text-center"><CheckCircle className="h-7 w-7 text-positive" /><p className="text-sm text-text-muted">{t('attention.noIssues')}</p></div>}
        <div className="flex items-center justify-between pt-2 border-t border-border-subtle"><SectionLink to="/sessions" label={t('attention.viewAll')}><PanelLeftClose className="h-4 w-4" /></SectionLink><span className="text-xs text-text-subtle">{goodAlerts.length > 0 ? `${goodAlerts.length} info` : ''}</span></div>
      </div>
    </Card>
  );
}
