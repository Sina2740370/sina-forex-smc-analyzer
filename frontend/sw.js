/* ============================================================
   Sina Quant - Service Worker v3
   استراتژی: Network-First برای فایل‌های اصلی
   ============================================================ */

const CACHE_NAME = 'sina-quant-v3';
const STATIC_ASSETS = [
  '/',
  '/static/style.css',
  '/static/app.js',
  '/manifest.json',
];

/* ---- Install ---- */
self.addEventListener('install', (event) => {
  console.log('🔧 SW نصب شد (v3)');
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then((cache) => cache.addAll(STATIC_ASSETS).catch(() => {}))
      .then(() => self.skipWaiting())
  );
});

/* ---- Activate: پاک کردن کش‌های قدیمی ---- */
self.addEventListener('activate', (event) => {
  console.log('✅ SW فعال شد (v3)');
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys
          .filter((k) => k !== CACHE_NAME)
          .map((k) => {
            console.log('🗑 پاک کردن کش:', k);
            return caches.delete(k);
          })
      )
    ).then(() => self.clients.claim())
  );
});

/* ---- Fetch ---- */
self.addEventListener('fetch', (event) => {
  const { request } = event;
  const url = new URL(request.url);

  // فقط GET
  if (request.method !== 'GET') return;

  // API: همیشه از شبکه (بدون کش)
  if (url.pathname.startsWith('/api/')) {
    return; // به مرورگر اجازه بده طبیعی درخواست بزنه
  }

  // HTML و JS و CSS: Network-First (همیشه آخرین نسخه)
  if (
    url.pathname === '/' ||
    url.pathname === '/manifest.json' ||
    url.pathname === '/sw.js' ||
    url.pathname.endsWith('.js') ||
    url.pathname.endsWith('.css') ||
    url.pathname.endsWith('.html')
  ) {
    event.respondWith(
      fetch(request)
        .then((response) => {
          // ذخیره در کش برای بار بعد
          if (response && response.status === 200) {
            const clone = response.clone();
            caches.open(CACHE_NAME).then((cache) => cache.put(request, clone));
          }
          return response;
        })
        .catch(() => {
          // آفلاین: از کش استفاده کن
          return caches.match(request).then((cached) => {
            if (cached) return cached;
            if (request.mode === 'navigate') {
              return caches.match('/');
            }
          });
        })
    );
    return;
  }

  // بقیه: Cache-First
  event.respondWith(
    caches.match(request).then((cached) => {
      if (cached) return cached;
      return fetch(request).then((response) => {
        if (response && response.status === 200 && response.type === 'basic') {
          const clone = response.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(request, clone));
        }
        return response;
      }).catch(() => caches.match('/'));
    })
  );
});