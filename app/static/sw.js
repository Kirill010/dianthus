// Service Worker «Диантус». Отдаётся с корня сайта (/sw.js), поэтому работает на ВСЕХ
// страницах — раньше лежал в /static/ и на страницы сайта не влиял.
//
// Что делает:
//   1. Статика (CSS, JS, шрифты, логотип) грузится быстрее и открывается без сети.
//   2. Если пропал интернет — вместо ошибки браузера показывает страницу «Нет связи».
// Чего НЕ делает: не сохраняет страницы сайта (в них цены, корзина, токены защиты)
// и не трогает отправку форм — заказы всегда идут напрямую на сервер.
const CACHE_NAME = 'dianthus-v10-2026-10';
const OFFLINE_URL = '/static/offline.html';
const STATIC_ASSETS = [
  '/static/style.css',
  '/static/pwa.js',
  '/static/logo-icon.png',
  '/static/logo-full.png',
  '/static/favicon.ico',
  '/static/vendor/bootstrap.min.css',
  '/static/vendor/bootstrap.bundle.min.js',
  '/static/vendor/fontawesome/css/all.min.css',
  OFFLINE_URL,
];

self.addEventListener('install', (event) => {
  // Новая версия включается сразу, не дожидаясь закрытия старых вкладок
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
    ).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  // Страница сайта: всегда из сети; нет связи — страница-заглушка.
  if (req.mode === 'navigate') {
    event.respondWith(fetch(req).catch(() => caches.match(OFFLINE_URL)));
    return;
  }

  // Из кэша обслуживаем только «каркас»: файлы из списка и библиотеки.
  // Фото товаров и прочее — обычным путём браузера (иначе кэш разрастался бы).
  const path = url.pathname;
  const isShell = STATIC_ASSETS.includes(path) || path.startsWith('/static/vendor/');
  if (!isShell) return;

  if (/\.(css|js)$/i.test(path)) {
    // CSS/JS: сначала сеть (свежая версия), нет связи — из кэша
    event.respondWith(
      fetch(req).then((response) => {
        if (response && response.status === 200) {
          // кладём без query-строки, чтобы версии не плодились
          const clone = response.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(url.origin + path, clone));
        }
        return response;
      }).catch(() => caches.match(req).then(
        (c) => c || caches.match(url.origin + path)
      ))
    );
    return;
  }

  // Шрифты, картинки каркаса: сначала кэш
  event.respondWith(
    caches.match(req).then((cached) => {
      if (cached) return cached;
      return fetch(req).then((response) => {
        if (!response || response.status !== 200) return response;
        const clone = response.clone();
        caches.open(CACHE_NAME).then((cache) => cache.put(req, clone));
        return response;
      });
    })
  );
});
