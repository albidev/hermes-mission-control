import { useEffect, useState } from 'react';
import { buildHeaders, localApiUrl } from '../lib/hermes-api';
import { useMissionControl } from '../lib/mission-control-store';
import type { MCPluginNavIndicator } from '../core/plugins/types';

type IndicatorPayload = {
  active?: boolean;
  count?: number;
  tone?: MCPluginNavIndicator['tones'][number];
  label?: string;
};

const toneClass: Record<MCPluginNavIndicator['tones'][number], string> = {
  neutral: 'bg-text-subtle',
  info: 'bg-accent',
  success: 'bg-positive',
  warning: 'bg-warning',
  error: 'bg-negative',
};

export function NavStatusIndicator({ indicator }: { indicator: MCPluginNavIndicator }) {
  const { storedToken } = useMissionControl();
  const [payload, setPayload] = useState<IndicatorPayload | null>(null);

  useEffect(() => {
    let cancelled = false;
    const pollMs = Math.max(10_000, indicator.pollMs ?? 30_000);
    const refresh = async () => {
      try {
        const response = await fetch(localApiUrl(indicator.endpoint), {
          headers: buildHeaders(storedToken || undefined),
          cache: 'no-store',
        });
        if (!response.ok) return;
        const next = await response.json() as IndicatorPayload;
        if (!cancelled) setPayload(next);
      } catch {
        // Optional plugin status must never break the host navigation.
      }
    };
    void refresh();
    const timer = window.setInterval(() => void refresh(), pollMs);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [indicator.endpoint, indicator.pollMs, storedToken]);

  if (!payload?.active) return null;
  const tone = payload.tone && indicator.tones.includes(payload.tone) ? payload.tone : 'info';
  const label = payload.label || (typeof payload.count === 'number' ? `${payload.count} items` : 'Plugin status requires attention');
  return (
    <span
      aria-label={label}
      className={`ml-auto h-2 w-2 shrink-0 rounded-full ${toneClass[tone]}`}
      title={label}
    />
  );
}
