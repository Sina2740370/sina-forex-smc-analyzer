/* ============================================================
   SMC Forex Analyzer - Service Worker (PWA)
   ============================================================ */

const CACHE_NAME = 'smc-forex-v2';
const STATIC_ASSETS = [
  '/',
  '/static/style.css',
  '/static/app.js',
  '/manifest.json',
];

/* ---- Install ---- */
self.addEventListener('install', (event) => {
  console.log('🔧 Service Worker: نصب');
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then((cache) => cache.addAll(STATIC_ASSETS))
      .then(() => self.skipWaiting())
  );
});

/* ---- Activate ---- */
self.addEventListener('activate', (event) => {
  console.log('✅ Service Worker: فعال');
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys.filter((k) => k !== CACHE_NAME)
            .map((k) => caches.delete(k))
      )
    ).then(() => self.clients.claim())
  );
});

/* ---- Fetch: Stale-While-Revalidate برای API + Cache-First برای static ---- */
self.addEventListener('fetch', (event) => {
  const { request } = event;
  const url = new URL(request.url);

  // فقط GET رو کش کن
  if (request.method !== 'GET') return;

  // API: همیشه از شبکه (بعد آپدیت کش)
  if (url.pathname.startsWith('/api/')) {
    event.respondWith(
      fetch(request)
        .then((response) => {
          const clone = response.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(request, clone));
          return response;
        })
        .catch(() => caches.match(request))
    );
    return;
  }

  // Static: اول کش، بعد شبکه
  event.respondWith(
    caches.match(request).then((cached) => {
      if (cached) return cached;
      return fetch(request).then((response) => {
        // فقط پاسخ‌های موفق رو کش کن
        if (response && response.status === 200 && response.type === 'basic') {
          const clone = response.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(request, clone));
        }
        return response;
      }).catch(() => {
        // اگه صفحه اصلی بود، آفلاین نشون بده
        if (request.mode === 'navigate') {
          return caches.match('/');
        }
      });
    })
  );
});

/* ---- Push Notifications (آماده برای آینده) ---- */
self.addEventListener('push', (event) => {
  if (!event.data) return;
  const data = event.data.json();
  event.waitUntil(
    self.registration.showNotification(data.title || 'سیگنال جدید', {
      body: data.body || 'سیگنال معاملاتی جدیدی صادر شد',
      icon: '/static/icon-192.png',
      badge: '/static/icon-192.png',
      dir: 'rtl',
      lang: 'fa',
      tag: 'signal',
      data: data.url || '/',
    })
  );
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  event.waitUntil(clients.openWindow(event.notification.data || '/'));
});
