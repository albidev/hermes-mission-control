import { localApiUrl, buildHeaders } from './hermes-api';

/**
 * Web Push for Mission Control.
 *
 * Requires HTTPS (served via Tailscale) and iOS Safari 16.4+ for home-screen
 * apps. The service worker receives push events while the app is in the
 * background or closed; a server-side process fires the push when Hermes
 * completes a response, because in the background this page's JS is
 * suspended and cannot poll.
 */

const SW_PATH = '/sw.js';

export type PushSubscriptionState =
  | { status: 'unsupported' }
  | { status: 'no-service-worker' }
  | { status: 'denied' }
  | { status: 'idle' }
  | { status: 'subscribing' }
  | { status: 'subscribed' }
  | { status: 'error'; error: string };

/**
 * Best-effort feature detection. On insecure origins (http) PushManager is
 * undefined, so this also naturally guards the Tailscale-IP http URL.
 */
export function pushSupported(): boolean {
  return typeof navigator !== 'undefined'
    && 'serviceWorker' in navigator
    && 'PushManager' in window
    && 'Notification' in window;
}

/**
 * Register the service worker (idempotent). The sw must be served from the
 * origin root so it has scope "/".
 */
export async function ensureServiceWorker(): Promise<ServiceWorkerRegistration | null> {
  if (!('serviceWorker' in navigator)) return null;
  try {
    return await navigator.serviceWorker.register(SW_PATH);
  } catch (err) {
    console.error('[push] service worker registration failed', err);
    return null;
  }
}

async function pushServerState(): Promise<PushSubscriptionState> {
  if (!pushSupported()) return { status: 'unsupported' };
  if (Notification.permission === 'denied') return { status: 'denied' };

  const registration = await ensureServiceWorker();
  if (!registration) return { status: 'no-service-worker' };

  const existing = await registration.pushManager.getSubscription();
  if (existing) return { status: 'subscribed' };

  return { status: Notification.permission === 'granted' ? 'idle' : 'idle' };
}

/**
 * Subscribe to push: request permission, subscribe with the server's VAPID
 * public key, and POST the subscription to the backend so it can deliver
 * pushes on our behalf.
 */
export async function subscribeToPush(accessToken?: string): Promise<PushSubscriptionState> {
  if (!pushSupported()) return { status: 'unsupported' };

  const registration = await ensureServiceWorker();
  if (!registration) return { status: 'no-service-worker' };

  const permission = await Notification.requestPermission();
  if (permission !== 'granted') return { status: 'denied' };

  try {
    const publicKey = await fetchVapidPublicKey(accessToken);
    const subscription = await registration.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: urlBase64ToUint8Array(publicKey),
    });

    // Persist the subscription server-side so the backend can send pushes.
    await persistSubscription(subscription, accessToken);
    return { status: 'subscribed' };
  } catch (err) {
    return { status: 'error', error: err instanceof Error ? err.message : String(err) };
  }
}

/** Unsubscribe: tell the browser and remove the server-side record. */
export async function unsubscribeFromPush(accessToken?: string): Promise<PushSubscriptionState> {
  if (!pushSupported()) return { status: 'unsupported' };
  const registration = await ensureServiceWorker();
  if (!registration) return { status: 'no-service-worker' };
  const subscription = await registration.pushManager.getSubscription();
  if (subscription) {
    const endpoint = subscription.endpoint;
    await subscription.unsubscribe();
    try {
      await fetch(localApiUrl('/push/subscriptions'), {
        method: 'DELETE',
        headers: buildHeaders(accessToken),
        body: JSON.stringify({ endpoint }),
      });
    } catch { /* best-effort */ }
  }
  return { status: 'idle' };
}

/** Load current state for the toggle UI. */
export async function getPushState(): Promise<PushSubscriptionState> {
  return pushServerState();
}

async function fetchVapidPublicKey(accessToken?: string): Promise<string> {
  const response = await fetch(localApiUrl('/push/vapid-public-key'), {
    headers: buildHeaders(accessToken),
  });
  if (!response.ok) throw new Error(`VAPID key request failed: ${response.status}`);
  const data = (await response.json()) as { publicKey?: string };
  if (!data.publicKey) throw new Error('VAPID public key missing from server response');
  return data.publicKey;
}

async function persistSubscription(subscription: PushSubscription, accessToken?: string): Promise<void> {
  const serialized = subscription.toJSON();
  const response = await fetch(localApiUrl('/push/subscriptions'), {
    method: 'POST',
    headers: { ...buildHeaders(accessToken), 'Content-Type': 'application/json' },
    body: JSON.stringify(serialized),
  });
  if (!response.ok) throw new Error(`Subscription persist failed: ${response.status}`);
}

/** Convert a base64url VAPID key into the Uint8Array PushManager wants. */
export function urlBase64ToUint8Array(base64String: string): Uint8Array<ArrayBuffer> {
  const padding = '='.repeat((4 - (base64String.length % 4)) % 4);
  const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
  const rawData = window.atob(base64);
  const outputArray = new Uint8Array(rawData.length);
  for (let i = 0; i < rawData.length; i += 1) {
    outputArray[i] = rawData.charCodeAt(i);
  }
  return outputArray;
}
