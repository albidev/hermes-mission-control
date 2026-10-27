import { useCallback, useEffect, useState } from 'react';
import { Bell, BellOff, Loader2 } from 'lucide-react';
import { useMissionControl } from '../lib/mission-control-store';
import { useI18n } from '../lib/i18n';
import { getPushState, pushSupported, subscribeToPush, unsubscribeFromPush, type PushSubscriptionState } from '../lib/push-client';

/**
 * Push notifications control. Expanded rows offer two options; the compact
 * sidebar uses a single icon toggle. Enabling requests permission and
 * registers the subscription with the backend so Hermes responses can arrive as
 * system notifications while the app is in the background or closed.
 */
export function PushToggle() {
  const { t } = useI18n();
  const { storedToken } = useMissionControl();
  const [state, setState] = useState<PushSubscriptionState | null>(null);
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(() => {
    void getPushState().then(setState);
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const unsupported = !pushSupported() || state?.status === 'unsupported';
  const subscribed = state?.status === 'subscribed';
  const disabled = busy || !state || unsupported || state.status === 'denied';

  const activate = async (next: boolean) => {
    if (disabled || next === subscribed) return;
    setBusy(true);
    try {
      const result = next
        ? await subscribeToPush(storedToken)
        : await unsubscribeFromPush(storedToken);
      setState(result);
    } finally {
      setBusy(false);
    }
  };

  const title = subscribed
    ? t('push.onTitle')
    : unsupported || state?.status === 'denied'
      ? t('push.blocked')
      : t('push.enable');

  return (
    <div className="push-toggle side-action-button" role="group" aria-label={title} title={title}>
      <button
        type="button"
        className={`sidebar-preference-compact ${subscribed ? 'is-active' : ''}`.trim()}
        onClick={() => void activate(!subscribed)}
        disabled={disabled}
        aria-pressed={subscribed}
        aria-label={title}
        title={title}
      >
        {busy ? <Loader2 size={16} className="chat-spin" aria-hidden />
          : subscribed ? <Bell size={16} aria-hidden /> : <BellOff size={16} aria-hidden />}
      </button>
      <span className="push-toggle-label">
        {busy ? (
          <Loader2 size={16} className="chat-spin" aria-hidden />
        ) : subscribed ? (
          <Bell size={16} className="sidebar-preference-icon" aria-hidden />
        ) : (
          <BellOff size={16} className="sidebar-preference-icon" aria-hidden />
        )}
        {t('push.label')}
      </span>
      <div className="push-toggle-track">
        <button
          type="button"
          className={`push-toggle-option ${subscribed ? '' : 'is-active'}`.trim()}
          onClick={() => void activate(false)}
          disabled={disabled}
          aria-pressed={!subscribed}
          aria-label={t('push.off')}
          title={t('push.off')}
        >
          {t('push.off')}
        </button>
        <button
          type="button"
          className={`push-toggle-option ${subscribed ? 'is-active' : ''}`.trim()}
          onClick={() => void activate(true)}
          disabled={disabled}
          aria-pressed={subscribed}
          aria-label={t('push.on')}
          title={t('push.on')}
        >
          {t('push.on')}
        </button>
      </div>
    </div>
  );
}
