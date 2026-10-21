// Service worker : permet d'ouvrir l'appli et de voir les photos même quand le Mac est éteint.
// Les données des recettes sont gardées côté page (localStorage), voir index.html.
const SHELL = "shell-v1";
const THUMBS = "thumbs-v1";
const SHELL_FILES = ["/", "/manifest.json", "/icon.svg", "/icon-180.png"];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(SHELL).then(c => c.addAll(SHELL_FILES)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", e => {
  e.waitUntil(caches.keys()
    .then(keys => Promise.all(keys.filter(k => ![SHELL, THUMBS].includes(k)).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});

// Quand le Mac est éteint, la requête peut rester bloquée : on abandonne vite.
function fetchWithTimeout(req, ms = 3000) {
  return Promise.race([fetch(req), new Promise((_, rej) => setTimeout(() => rej(new Error("timeout")), ms))]);
}

self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin) return;

  // Miniatures : cache d'abord (elles ne changent jamais)
  if (url.pathname.startsWith("/thumbs/")) {
    e.respondWith(caches.open(THUMBS).then(async c => {
      const hit = await c.match(e.request);
      if (hit) return hit;
      const res = await fetch(e.request);
      if (res.ok) c.put(e.request, res.clone());
      return res;
    }));
    return;
  }

  // Coquille de l'appli : réseau d'abord (pour les mises à jour), cache si le Mac est injoignable
  if (e.request.mode === "navigate" || SHELL_FILES.includes(url.pathname)) {
    const key = e.request.mode === "navigate" ? "/" : url.pathname;
    e.respondWith(fetchWithTimeout(e.request)
      .then(res => {
        if (res.ok) caches.open(SHELL).then(c => c.put(key, res.clone()));
        return res;
      })
      .catch(() => caches.match(key)));
  }
});
