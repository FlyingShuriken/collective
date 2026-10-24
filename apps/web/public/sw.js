const CACHE = "orbyte-public-v1";
const ASSETS = ["/offline.html", "/icons/icon-192.png", "/icons/icon-512.png"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(ASSETS)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(caches.keys().then((keys) => Promise.all(keys
    .filter((key) => key.startsWith("orbyte-public-") && key !== CACHE)
    .map((key) => caches.delete(key)))).then(() => self.clients.claim()));
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  const url = new URL(request.url);
  if (request.method !== "GET" || url.origin !== self.location.origin || url.pathname.startsWith("/api/")) return;
  if (ASSETS.includes(url.pathname) && !url.search) {
    event.respondWith(caches.match(url.pathname).then((saved) => saved || fetch(request)));
  } else if (request.mode === "navigate") {
    event.respondWith(fetch(request).catch(async () => (await caches.match("/offline.html")) || Response.error()));
  }
});
