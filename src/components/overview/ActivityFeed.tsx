import { useI18n } from '../../lib/i18n';
import { useRef } from 'react';
import { Activity, Bot, User, Wrench } from 'lucide-react';
import type { MissionControlSnapshot } from '../../lib/hermes-api';
import { Card } from '../ui/Card';
import { formatRelativeTime } from '../../lib/format';

type Signal = MissionControlSnapshot['recentSignals'][number];

function signalIcon(label: string) {
  const l = label.toLowerCase();
  if (l.includes('agent') || l.includes('model') || l.includes('gateway')) return Bot;
  if (l.includes('user') || l.includes('session')) return User;
  if (l.includes('tool') || l.includes('skill')) return Wrench;
  return Activity;
}

function signalToneClass(tone: Signal['tone']) {
  return tone === 'good'
    ? 'border-l-positive'
    : tone === 'warn'
    ? 'border-l-warning'
    : 'border-l-negative';
}

export function ActivityFeed({ signals }: { signals: Signal[] }) {
  const { t } = useI18n();
  const listRef = useRef<HTMLDivElement>(null);

  return (
    <Card padding="none">
      <div className="flex items-center justify-between px-3 pt-3 pb-2 border-b border-border-subtle">
        <div className="flex flex-col gap-0.5">
          <span className="eyebrow">{t('activity.eyebrow')}</span>
          <h2 className="text-sm font-semibold text-text">{t('activity.title')}</h2>
        </div>
        <div className="flex items-center gap-1.5">
          <span className="h-2 w-2 rounded-full bg-positive animate-pulse" />
          <span className="text-xs text-text-muted">{t('activity.live')}</span>
        </div>
      </div>

      <div
        ref={listRef}
        className="flex flex-col divide-y divide-border-subtle max-h-56 overflow-y-auto"
      >
        {signals.length > 0 ? (
          signals.slice(0, 4).map((signal, i) => {
            const Icon = signalIcon(signal.label);
            const toneClass = signalToneClass(signal.tone);

            return (
              <div
                key={`${signal.label}-${i}`}
                className={`flex items-start gap-3 px-3 py-2.5 ${toneClass} border-l-2`}
              >
                <div className="mt-0.5 flex-shrink-0">
                  <Icon className="h-4 w-4 text-text-muted" />
                </div>
                <div className="flex-1 min-w-0">
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-sm font-medium text-text truncate">
                      {signal.label}
                    </span>
                  </div>
                  <p className="text-xs text-text-muted line-clamp-2 mt-0.5">
                    {signal.detail}
                  </p>
                </div>
              </div>
            );
          })
        ) : (
          <div className="flex items-center justify-center py-6">
            <p className="text-sm text-text-muted italic">{t('activity.noSignals')}</p>
          </div>
        )}
      </div>
    </Card>
  );
}
