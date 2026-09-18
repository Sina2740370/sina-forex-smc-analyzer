// ============================================================
//  Service Worker v4 - Kill Switch
//  این نسخه خودش رو پاک می‌کنه و cacheها رو پاک می‌کنه
//  بعد از فعال شدن، دیگه SW نداریم
// ============================================================

self.addEventListener('install', (event) => {
  console.log('🔧 SW v4 نصب شد - Kill Switch Mode');
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  console.log('🗑 SW v4 - شروع پاک‌سازی');

  event.waitUntil(
    (async () => {
      try {
        // ۱. پاک کردن همه cacheها
        const cacheNames = await caches.keys();
        console.log('🗑 پاک کردن ' + cacheNames.length + ' cache');
        await Promise.all(cacheNames.map((name) => caches.delete(name)));

        // ۲. Unregister خودم
        console.log('🗑 Unregister خودم');
        await self.registration.unregister();

        // ۳. Reload همه tabها
        const clients = await self.clients.matchAll({ type: 'window' });
        console.log('🔄 Reload ' + clients.length + ' tab');
        for (const client of clients) {
          if ('navigate' in client) {
            client.navigate(client.url);
          }
        }
      } catch (e) {
        console.error('خطا در self-destruct:', e);
      }
    })()
  );
});

// هیچ fetch handler نداریم - همه چیز از شبکه میاد
