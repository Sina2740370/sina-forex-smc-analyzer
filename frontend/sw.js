// ============================================================
//  Service Worker v6 - App Shell Precache (cache-first) + Pass-Through
//  - شِل اپ ('./', './index.html', './manifest.json') در install کش می‌شود
//    و cache-first با fallback شبکه سرو می‌شود.
//  - همه‌ی درخواست‌های دیگر (از جمله /api/) بدون respondWith مستقیم به شبکه
//    می‌روند.
// ============================================================

const CACHE = 'sinaquant-shell-v6';
const SHELL = ['./', './index.html', './manifest.json'];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE)
      .then((cache) => cache.addAll(SHELL))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

// فقط درخواست‌های شِل اپ را هندل می‌کنیم؛ خروجی null یعنی pass-through
function shellCacheKey(url) {
  if (url.origin !== self.location.origin) return null;
  let scopePath;
  try { scopePath = new URL(self.registration.scope).pathname; } catch (e) { return null; }
  if (!url.pathname.startsWith(scopePath)) return null;
  const rel = url.pathname.slice(scopePath.length);
  if (rel === '' || rel === 'index.html' || rel === 'manifest.json') {
    return './' + (rel === '' ? 'index.html' : rel);
  }
  return null;
}

self.addEventListener('fetch', (event) => {
  if (event.request.method !== 'GET') return;

  let url;
  try { url = new URL(event.request.url); } catch (e) { return; }

  // /api/ و هر چیز غیر از شِل: همیشه مستقیم به شبکه (بدون respondWith)
  if (!shellCacheKey(url)) return;

  event.respondWith((async () => {
    const cache = await caches.open(CACHE);
    // cache-first برای شِل اپ
    const hit = (await cache.match(event.request)) || (await cache.match('./index.html'));
    if (hit) return hit;
    // fallback شبکه (+ به‌روزرسانی کش در صورت موفقیت)
    try {
      const res = await fetch(event.request);
      if (res && res.ok && res.type === 'basic') {
        cache.put(event.request, res.clone()).catch(() => {});
      }
      return res;
    } catch (e) {
      return new Response('offline', { status: 503, statusText: 'Offline' });
    }
  })());
});
