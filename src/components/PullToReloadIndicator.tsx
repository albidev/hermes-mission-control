import type { PullToReloadState } from '../hooks/usePullToReload';
import { Loader2, RefreshCw } from 'lucide-react';

type PullToReloadIndicatorProps = {
  state: PullToReloadState;
};

export function PullToReloadIndicator({ state }: PullToReloadIndicatorProps) {
  if (!state.pulling && !state.reloading) return null;

  const pullDistance = Math.min(state.progress * 72, 72);
  const released = state.progress >= 1;

  return (
    <div
      className="ptr-surface"
      style={{
        transform: `translate(-50%, ${pullDistance}px)`,
        opacity: state.reloading || state.progress > 0.04 ? 1 : 0,
      }}
      aria-live="polite"
      aria-busy={state.reloading}
    >
      <div className={`ptr-ring ${released ? 'ptr-ring-active' : ''}`}>
        {state.reloading ? (
          <Loader2 className="animate-spin" size={16} />
        ) : (
          <RefreshCw
            size={16}
            style={{ transform: `rotate(${state.progress * 220}deg)` }}
          />
        )}
      </div>
      <span className="ptr-label">
        {state.reloading ? 'Reloading…' : released ? 'Release to reload' : 'Pull to reload'}
      </span>
    </div>
  );
}
