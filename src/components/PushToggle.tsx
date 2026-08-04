import { useCallback, useEffect, useState } from 'react';
import { Bell, BellOff, Loader2 } from 'lucide-react';
import { useMissionControl } from '../lib/mission-control-store';
import { getPushState, pushSupported, subscribeToPush, unsubscribeFromPush, type PushSubscriptionState } from '../lib/push-client';

/**
 * Push notifications toggle. Shows an on/off switch for Web Push on this
 * device. When enabled, requests permission and registers the subscription
 * with the backend so Hermes responses can arrive as system notifications
 * while the app is in the background or closed.
 */
export function PushToggle() {
  const { storedToken } = useMissionControl();
  const [state, setState] = useState<PushSubscriptionState | null>(null);
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(() => {
    void getPushState().then(setState);
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  if (!pushSupported()) return null;
  if (!state) return null;

  const subscribed = state.status === 'subscribed';

  const toggle = async () => {
    if (busy) return;
    setBusy(true);
    try {
      const next = subscribed
        ? await unsubscribeFromPush(storedToken)
        : await subscribeToPush(storedToken);
      setState(next);
    } finally {
      setBusy(false);
    }
  };

  const title = subscribed
    ? 'Push notifications are on'
    : state.status === 'denied'
      ? 'Notifications blocked in browser settings'
      : 'Enable push notifications for Hermes responses';

  return (
    <button
      className="pill pill-subtle pill-button side-action-button push-toggle"
      type="button"
      onClick={() => void toggle()}
      disabled={busy || state.status === 'denied'}
      aria-label={title}
      title={title}
      aria-pressed={subscribed}
    >
      {busy ? (
        <Loader2 size={15} className="chat-spin" />
      ) : subscribed ? (
        <Bell size={15} />
      ) : (
        <BellOff size={15} />
      )}
      <span className="push-toggle-label">{subscribed ? 'Push: On' : 'Push: Off'}</span>
    </button>
  );
}
