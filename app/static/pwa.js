// Установка сайта на экран телефона + сервис-воркер.
// Подключается на всех страницах (base.html). Баннер есть только на главной и в
// каталоге; показывается на телефоне, если сайт ещё не установлен и его не закрывали.
(function () {
  'use strict';

  // ── Сервис-воркер: корневой (/sw.js), старый из /static/ убираем ──
  if ('serviceWorker' in navigator) {
    window.addEventListener('load', function () {
      navigator.serviceWorker.getRegistrations().then(function (regs) {
        regs.forEach(function (r) {
          if (new URL(r.scope).pathname === '/static/') r.unregister();
        });
      }).catch(function () {});
      navigator.serviceWorker.register('/sw.js', { scope: '/' }).catch(function () {});
    });
  }

  // ── Баннер «Установить» ──
  var banner = document.getElementById('pwaBanner');
  if (!banner) return;

  var KEY = 'pwa_banner_until';
  function snoozed() {
    try { return Number(localStorage.getItem(KEY) || 0) > Date.now(); } catch (e) { return false; }
  }
  function snooze(days) {
    try { localStorage.setItem(KEY, String(Date.now() + days * 864e5)); } catch (e) { /* без памяти — ничего страшного */ }
  }
  function mq(q) { return !!(window.matchMedia && window.matchMedia(q).matches); }

  var installed = mq('(display-mode: standalone)') || navigator.standalone === true;
  if (installed || snoozed() || !mq('(max-width: 768px)')) return;

  var installBtn = banner.querySelector('[data-pwa-install]');
  var iosHint = banner.querySelector('[data-pwa-ios]');
  var androidHint = banner.querySelector('[data-pwa-android]');
  var closeBtn = banner.querySelector('[data-pwa-close]');
  var deferred = null;

  function show() { banner.hidden = false; }
  function hide(days) { banner.hidden = true; snooze(days); }

  // Android / Chrome: браузер сам сообщает, что сайт можно установить
  window.addEventListener('beforeinstallprompt', function (e) {
    e.preventDefault();
    deferred = e;
    installBtn.hidden = false;
    show();
  });
  window.addEventListener('appinstalled', function () { hide(365); });

  installBtn.addEventListener('click', function () {
    if (!deferred) return;
    deferred.prompt();
    deferred.userChoice.then(function () { deferred = null; hide(30); });
  });
  closeBtn.addEventListener('click', function () { hide(30); });

  // iPhone/iPad (Safari): события нет — показываем подсказку, как добавить вручную
  var ua = navigator.userAgent || '';
  if (/iphone|ipad|ipod/i.test(ua) && !/crios|fxios|edgios/i.test(ua)) {
    iosHint.hidden = false;
    androidHint.hidden = true;
    show();
  }
})();
