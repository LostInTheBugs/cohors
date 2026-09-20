/* Cohors — service worker (PWA) : cache des ressources statiques + page hors-ligne. */
const CACHE = "cohors-v2026.09.149";
const CORE = ["/static/logo.png", "/static/icon-192.png", "/static/icon-512.png",
  "/static/esc.js", "/static/nav.js", "/static/i18n.js", "/static/theme.css", "/static/bg-texture.png",
  "/static/fonts/cinzel.woff2", "/offline.html"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(CORE)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys()
    .then((ks) => Promise.all(ks.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});
self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== location.origin) return;
  if (url.pathname.startsWith("/api/") || url.pathname === "/sw.js" || url.pathname === "/manifest.webmanifest") return;
  if (req.mode === "navigate") {
    e.respondWith(fetch(req).catch(() => caches.match("/offline.html")));
    return;
  }
  if (url.pathname.startsWith("/static/")) {
    e.respondWith(fetch(req).then((r) => {
      if (r.ok) caches.open(CACHE).then((c) => c.put(req, r.clone()));
      return r;
    }).catch(() => caches.match(req)));
  }
});
