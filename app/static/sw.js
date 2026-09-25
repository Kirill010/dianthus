// Service Worker для Диантуса — офлайн-кэш статики
// ⚠️ ВЕРСИЯ УВЕЛИЧЕНА ДО v6: при установке новый SW удалит старый кэш
// и заставит браузер скачать свежие CSS/JS.
const CACHE_NAME = 'dianthus-v9-2026-01';
const STATIC_ASSETS = [
  '/static/style.css',
  '/static/logo-icon.png',
  '/static/logo-full.png',
  '/static/favicon.ico',
  '/static/vendor/bootstrap.min.css',
  '/static/vendor/bootstrap.bundle.min.js',
  '/static/vendor/fontawesome/css/all.min.css',
];

self.addEventListener('install', (event) => {
  // Форсируем установку нового SW, не дожидаясь закрытия старых вкладок
  self.skipWaiting();
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(STATIC_ASSETS))
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k))
      )
    ).then(() => self.clients.claim())  // берём контроль над всеми вкладками
  );
});

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== 'GET') return;
  if (!url.pathname.startsWith('/static/')) return;

  const isCssOrJs = /\.(css|js)$/i.test(url.pathname);

  if (isCssOrJs) {
    event.respondWith(
      fetch(event.request).then((response) => {
        if (response && response.status === 200) {
          // fix #23: кладём в кэш без query, чтобы версии не плодились
          const cacheKey = url.origin + url.pathname;
          const clone = response.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(cacheKey, clone));
        }
        return response;
      }).catch(() => caches.match(event.request).then(
        (c) => c || caches.match(url.origin + url.pathname)
      ))
    );
    return;
  }

  event.respondWith(
    caches.match(event.request).then((cached) => {
      if (cached) return cached;
      return fetch(event.request).then((response) => {
        if (!response || response.status !== 200) return response;
        const clone = response.clone();
        caches.open(CACHE_NAME).then((cache) => cache.put(event.request, clone));
        return response;
      });
    })
  );
});