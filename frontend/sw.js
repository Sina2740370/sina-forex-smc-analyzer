// ============================================================
//  Service Worker v5 - Pass-Through Mode
//  فقط برای PWA installability - هیچ caching نمی‌کنه
// ============================================================

self.addEventListener('install', (event) => {
  console.log('🔧 SW v5 نصب شد');
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  console.log('✅ SW v5 فعال شد');
  event.waitUntil(self.clients.claim());
});

// Pass-through: همه چیز از شبکه (هیچ caching)
self.addEventListener('fetch', (event) => {
  if (event.request.method !== 'GET') return;

  const url = new URL(event.request.url);
  if (url.pathname.startsWith('/api/')) return;

  event.respondWith(
    fetch(event.request).catch(() => caches.match(event.request))
  );
});
