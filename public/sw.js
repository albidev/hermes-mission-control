/* Mission Control Web Push service worker.
 *
 * Receives Web Push notifications while the app is in the background or
 * closed, and shows them as system notifications. Also acts as a basic
 * offline cache host so the shell loads fast from the home-screen app.
 */
self.addEventListener('install', (event) => {
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(self.clients.claim());
});

// Push event: show the notification. The payload is JSON with {title, body}.
self.addEventListener('push', (event) => {
  let title = 'Hermes Mission Control';
  let body = 'A new Hermes response is ready.';
  let tag = 'mission-control';
  let data = {};

  try {
    const payload = event.data ? event.data.json() : null;
    if (payload) {
      if (payload.title) title = payload.title;
      if (payload.body) body = payload.body;
      if (payload.tag) tag = payload.tag;
      data = payload.data || {};
    }
  } catch {
    // Non-JSON payload: use the raw text as the body.
    if (event.data) body = event.data.text();
  }

  event.waitUntil(
    self.registration.showNotification(title, {
      body,
      tag,
      icon: '/favicon.svg',
      badge: '/favicon.svg',
      data,
      vibrate: [80, 40, 80],
    }),
  );
});

// Notification click: focus the app and route to the chat.
self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const target = event.notification.data?.url || '/';
  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(async (clients) => {
      for (const client of clients) {
        if ('focus' in client) {
          await client.focus();
          client.postMessage({ type: 'mission-control:notification-click', url: target });
          return;
        }
      }
      return self.clients.openWindow(target);
    }),
  );
});
