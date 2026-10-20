/* Foretape service worker: shows pushed alerts, opens the app on tap, keeps the app shell
   available offline, and re-subscribes if the browser rotates the push subscription. */
const SHELL = "foretape-shell-v79";
const CONF = "foretape-conf";
const SHELL_FILES = [
  "/app/", "/app/app.css", "/app/app.js", "/app/cmd.js", "/app/scene.js", "/app/launch.webp", "/app/depth.png", "/app/logo.svg", "/app/icon-192.png", "/app/badge-96.png",
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
  // the tape itself: kept from the last good answer, served (marked) when the network is gone
  if (e.request.method === "GET" && url.origin === location.origin && url.pathname === "/api/app") {
    e.respondWith(fetch(e.request).then((r) => {
      if (r.ok) { const copy = r.clone(); caches.open(CONF).then(async (c) => c.put("/offline/app", new Response(await copy.blob(), { headers: { "Content-Type": "application/json", "X-Foretape-Offline": String(Math.floor(Date.now() / 1000)) } }))); }
      return r;
    }).catch(async () => (await (await caches.open(CONF)).match("/offline/app")) || Response.error()));
    return;
  }
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
  if (e.data?.type === "device") caches.open(CONF).then((c) => c.put("/device", new Response(e.data.device)));
  if (e.data?.type === "onestock") caches.open(CONF).then((c) => c.put("/onestock", new Response(e.data.on ? "1" : "")));
  if (e.data?.type === "quietcall") caches.open(CONF).then((c) => c.put("/quietcall", new Response(e.data.on ? "1" : "0")));
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
    data: { url: d.open || "/app/" + (d.id ? "#a=" + d.id : ""), source: d.url || "", mute: d.mute || "", track: d.track || "" },
    // buttons where the platform shows them (Android, desktop; iOS doesn't): the source, and muting the stock here
    actions: [...(d.track ? [{ action: "track", title: "Track (paper)" }] : []), ...(d.url ? [{ action: "source", title: "Open source" }] : []), ...(d.mute ? [{ action: "mute", title: `Mute ${d.mute} 4h` }] : [])].slice(0, 2),
  };
  e.waitUntil(Promise.all([
    // one notification per stock (a Desk setting): a newer alert on the same stock replaces the older one
    caches.open(CONF).then(async (c) => {
      const one = await (await c.match("/onestock"))?.text().catch(() => "");
      if (one === "1" && d.tickers?.[0] && d.kind !== "system") options.tag = "sym-" + d.tickers[0];
      // sound by the call (on unless switched off): watch and skip calls arrive silently; CRITICAL always rings
      const quiet = (await (await c.match("/quietcall"))?.text().catch(() => "")) ?? "";
      if (quiet !== "0" && !critical && (d.call === "watch" || d.call === "skip")) {
        options.silent = true;
        options.renotify = false;
        delete options.vibrate;
      }
      return self.registration.showNotification(title, options);  // iOS requires a visible notification for every push
    }),
    bumpBadge(),
    self.clients.matchAll({ type: "window" }).then((ws) => ws.forEach((w) => w.postMessage({ type: "push", payload: d }))),
  ]));
});

async function deskAction(body) {
  const c = await caches.open(CONF);
  const token = await (await c.match("/token"))?.text();
  const device = (await (await c.match("/device"))?.text()) || "";
  if (!token || !device) return;
  await fetch("/api/desk", { method: "POST", headers: { "Content-Type": "application/json", Authorization: "Bearer " + token, "X-Device": device }, body: JSON.stringify(body) });
}

async function trackAlert(id) {
  const c = await caches.open(CONF);
  const token = await (await c.match("/token"))?.text();
  const device = (await (await c.match("/device"))?.text()) || "";
  if (!token || !device) return;
  const r = await fetch("/api/track-alert", { method: "POST", headers: { "Content-Type": "application/json", Authorization: "Bearer " + token, "X-Device": device }, body: JSON.stringify({ alert_id: id }) });
  const out = r.ok ? await r.json() : null;
  await self.registration.showNotification(out ? `📌 Tracking ${out.symbol} at $${(+out.entry).toFixed(2)}` : "Couldn't track it", { body: out ? "A paper call with the alert's plan: stop and target set." : await r.text(), tag: "track-" + id, icon: "/app/icon-192.png", badge: "/app/badge-96.png" });
}

self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  const { url, source, mute, track } = e.notification.data || {};
  if (e.action === "mute" && mute) {  // handled in the background: no app window
    e.waitUntil(deskAction({ action: "mute", symbol: mute, hours: 4 }).catch(() => {}));
    return;
  }
  if (e.action === "track" && track) {  // a paper call with the alert's plan, from the lock screen
    e.waitUntil(trackAlert(track).catch(() => {}));
    return;
  }
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
    const device = (await (await (await caches.open(CONF)).match("/device"))?.text()) || "";
    const key = (await (await fetch("/app/push-key")).text()).trim();
    const raw = atob(key.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - key.length % 4) % 4));
    const sub = await self.registration.pushManager.subscribe({
      userVisibleOnly: true, applicationServerKey: Uint8Array.from(raw, (c) => c.charCodeAt(0)),
    });
    if (token) {
      await fetch("/api/push/subscribe", {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: "Bearer " + token, "X-Device": device },
        body: JSON.stringify({ subscription: sub.toJSON(), label: "renewed" }),
      });
    }
  })());
});
