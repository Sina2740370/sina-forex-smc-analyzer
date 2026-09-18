// ============================================================
//  Service Worker v4 - SELF DESTRUCT MODE
//  این SW خودش رو پاک می‌کنه و همه چیز رو ری‌ست می‌کنه
// ============================================================

self.addEventListener('install', (event) => {
  console.log('🔧 SW v4 نصب شد - Self Destruct Mode');
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  console.log('🗑 SW v4 - شروع پاک‌سازی کامل');
  event.waitUntil(
    (async () => {
      try {
        // ۱. پاک کردن همه cacheها
        const cacheNames = await caches.keys();
        console.log('🗑 پاک کردن ' + cacheNames.length + ' cache');
        await Promise.all(cacheNames.map(name => caches.delete(name)));

        // ۲. Unregister خودم
        console.log('🗑 Unregister خودم');
        await self.registration.unregister();

        // ۳. Reload همه tabها
        const clients = await self.clients.matchAll({ type: 'window' });
        console.log('🔄 Reload ' + clients.length + ' tab');
        for (const client of clients) {
          if ('navigate' in client) {
            await client.navigate(client.url);
          }
        }
      } catch (e) {
        console.error('خطا در self-destruct:', e);
      }
    })()
  );
});

// هیچ fetch handler نداریم - همه چیز از شبکه
