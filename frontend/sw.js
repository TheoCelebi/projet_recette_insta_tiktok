// Service worker : permet d'ouvrir l'appli et de voir les photos même quand le Mac est éteint.
// Les données des recettes sont gardées côté page (localStorage), voir index.html.
const SHELL = "shell-v9";
const THUMBS = "thumbs-v1";
const SHELL_FILES = ["/", "/manifest.json", "/icon.svg", "/icon-180.png",
  "/fonts/fraunces.woff2", "/fonts/fraunces-italic.woff2", "/fonts/dmsans.woff2"];

self.addEventListener("install", e => {
  // cache: "reload" : on prend les fichiers sur le serveur, jamais dans le cache HTTP du navigateur
  e.waitUntil(caches.open(SHELL)
    .then(c => c.addAll(SHELL_FILES.map(u => new Request(u, { cache: "reload" }))))
    .then(() => self.skipWaiting()));
});

self.addEventListener("activate", e => {
  e.waitUntil(caches.keys()
    .then(keys => Promise.all(keys.filter(k => ![SHELL, THUMBS].includes(k)).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});

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

  // Partage (/share) : normalement traité par le Mac ; s'il est injoignable, on sert l'appli,
  // qui garde le lien en file d'attente et l'enverra plus tard.
  if (e.request.mode === "navigate" && url.pathname === "/share") {
    e.respondWith(fetch(e.request).catch(() => caches.match("/")));
    return;
  }

  // Coquille de l'appli : réponse immédiate depuis le cache, mise à jour en arrière-plan
  // (la nouvelle version s'affiche à l'ouverture suivante). Sans cache : réseau.
  // (/share et l'API passent toujours par le Mac : seule la page d'accueil est concernée)
  if (SHELL_FILES.includes(url.pathname)) {
    const key = url.pathname;
    const update = fetch(e.request, { cache: "no-cache" }).then(res => {  // revalidation (304 si inchangé)
      if (res.ok) { const copy = res.clone(); caches.open(SHELL).then(c => c.put(key, copy)); }
      return res;
    });
    e.waitUntil(update.then(() => {}, () => {}));
    e.respondWith(caches.match(key).then(hit => hit || update));
  }
});
