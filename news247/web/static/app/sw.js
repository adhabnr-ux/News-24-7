/* Foretape service worker: shows pushed alerts, opens the app on tap, keeps the app shell
   available offline, and re-subscribes if the browser rotates the push subscription. */
const SHELL = "foretape-shell-v9";
const CONF = "foretape-conf";
const SHELL_FILES = [
  "/app/", "/app/app.css", "/app/app.js", "/app/scene.js", "/app/launch.webp", "/app/depth.png", "/app/logo.svg", "/app/icon-192.png", "/app/badge-96.png",
  "/app/fonts/instrument-serif-normal.woff2", "/app/fonts/instrument-serif-italic.woff2", "/app/fonts/inter-normal.woff2", "/app/fonts/jetbrains-mono-normal.woff2",
];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(SHELL).then((c) => c.addAll(SHELL_FILES)).catch(() => {}).then(() => self.skipWaiting()));
});
self.addEventListener("activate", (e) => e.waitUntil(
  caches.keys().then((ks) => Promise.all(ks.filter((k) => k.startsWith("foretape-shell-") && k !== SHELL).map((k) => caches.delete(k))))
    .then(() => self.clients.claim())
));

// Network first for the app shell (always fresh when online), cache when offline.
self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin || !url.pathname.startsWith("/app/")) return;
  if (url.pathname.endsWith("sw.js") || url.pathname.endsWith(".webmanifest")) return;
  e.respondWith(
    fetch(e.request)
      .then((r) => { const copy = r.clone(); caches.open(SHELL).then((c) => c.put(e.request, copy)); return r; })
      .catch(() => caches.match(e.request, { ignoreSearch: true }))
  );
});

// The page hands us the access key so we can re-subscribe on our own if needed.
self.addEventListener("message", (e) => {
  if (e.data?.type === "token") caches.open(CONF).then((c) => c.put("/token", new Response(e.data.token)));
});

async function bumpBadge() {
  if (!self.navigator.setAppBadge) return;
  const c = await caches.open(CONF);
  const n = parseInt((await (await c.match("/badge"))?.text()) || "0", 10) + 1;
  await c.put("/badge", new Response(String(n)));
  const visible = (await self.clients.matchAll({ type: "window" })).some((w) => w.visibilityState === "visible");
  if (!visible) await self.navigator.setAppBadge(n);
}

self.addEventListener("push", (e) => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch (_) { d = { title: e.data?.text() || "Foretape" }; }
  const critical = d.severity === "CRITICAL";
  const title = d.title || "Foretape";
  const options = {
    body: d.body || "",
    tag: d.tag || d.id || "foretape",
    renotify: true,                       // a newer alert on the same story still buzzes
    requireInteraction: critical,         // critical alerts stay on screen (where supported)
    icon: "/app/icon-192.png",
    badge: "/app/badge-96.png",
    timestamp: d.ts ? d.ts * 1000 : Date.now(),
    vibrate: critical ? [120, 60, 120, 60, 240] : [80, 40, 80],
    data: { url: "/app/" + (d.id ? "#a=" + d.id : ""), source: d.url || "" },
    actions: d.url ? [{ action: "source", title: "Open source" }] : [],
  };
  e.waitUntil(Promise.all([
    self.registration.showNotification(title, options),  // iOS requires a visible notification for every push
    bumpBadge(),
    self.clients.matchAll({ type: "window" }).then((ws) => ws.forEach((w) => w.postMessage({ type: "push", payload: d }))),
  ]));
});

self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  const { url, source } = e.notification.data || {};
  const target = e.action === "source" && source ? source : url || "/app/";
  e.waitUntil((async () => {
    await caches.open(CONF).then((c) => c.put("/badge", new Response("0")));
    self.navigator.clearAppBadge?.();
    if (target.startsWith("http")) return self.clients.openWindow(target);
    const wins = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    const app = wins.find((w) => new URL(w.url).pathname.startsWith("/app"));
    if (app) { await app.focus(); return app.navigate(target).catch(() => {}); }
    return self.clients.openWindow(target);
  })());
});

// Browsers may rotate the subscription; register the new one with the server right away.
self.addEventListener("pushsubscriptionchange", (e) => {
  e.waitUntil((async () => {
    const token = await (await (await caches.open(CONF)).match("/token"))?.text();
    const key = (await (await fetch("/app/push-key")).text()).trim();
    const raw = atob(key.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - key.length % 4) % 4));
    const sub = await self.registration.pushManager.subscribe({
      userVisibleOnly: true, applicationServerKey: Uint8Array.from(raw, (c) => c.charCodeAt(0)),
    });
    if (token) {
      await fetch("/api/push/subscribe", {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: "Bearer " + token },
        body: JSON.stringify({ subscription: sub.toJSON(), label: "renewed" }),
      });
    }
  })());
});
