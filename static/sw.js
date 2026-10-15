// Service worker: receives push notifications while the app is closed, and
// opens the app when one is tapped.
//
// Served from /sw.js (not /static/sw.js) because a service worker only
// controls pages at or below the path it's served from, and the app is "/".
//
// No offline caching on purpose: the app is a live map — tiles, routes,
// delays — and a cached copy would show stale or empty data while looking
// current. Without a connection there's nothing useful to show.

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", event => event.waitUntil(self.clients.claim()));

self.addEventListener("push", event => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; }
  catch { data = { body: event.data ? event.data.text() : "" }; }
  event.waitUntil(self.registration.showNotification(data.title || "Bay Area GPS", {
    body: data.body || "",
    icon: "/static/icons/icon-192.png",
    badge: "/static/icons/badge-96.png",
    // Same tag = the new notification replaces the old one instead of stacking.
    tag: data.tag || undefined,
    data: { url: data.url || "/" },
  }));
});

self.addEventListener("notificationclick", event => {
  event.notification.close();
  const url = new URL((event.notification.data && event.notification.data.url) || "/", self.location.origin).href;
  event.waitUntil((async () => {
    // Bring an open window forward rather than opening a second one.
    const windows = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    for (const client of windows) {
      if (new URL(client.url).origin === self.location.origin && "focus" in client) return client.focus();
    }
    return self.clients.openWindow(url);
  })());
});
