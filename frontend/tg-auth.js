// ============================================================
//  tg-auth.js — احراز هویت مینی‌اپ تلگرام برای همه‌ی درخواست‌های /api
//
//  - داخل تلگرام: هدر «Authorization: tma <initData>» (با انتظار تا ۳ ثانیه
//    برای آماده‌شدن WebApp/initData؛ در صورت نبود، پیام خطا در UI نشان داده
//    می‌شود و هرگز window.prompt داخل تلگرام صدا زده نمی‌شود).
//  - خارج از تلگرام: کلید ادمین از localStorage با کلید یکسان «sina_admin_key»
//    (مهاجرت خودکار از مقادیر قدیمی sessionStorage/localStorage با نام
//    «sq_admin_key») و در نبود کلید، یک‌بار prompt (فقط مرورگر عادی).
//  - گلوبال window.TgAuth در دسترس اسکریپت اصلی اپ است.
// ============================================================
(function () {
  var KEY_NAME = 'sina_admin_key';      // کلید یکسان‌شده (localStorage)
  var LEGACY_KEY = 'sq_admin_key';      // کلید قدیمی (sessionStorage قدیمی + localStorage ژورنال)
  var TG_UA = /Telegram/i.test(navigator.userAgent || '');

  function inTelegram() {
    try { if (window.Telegram && window.Telegram.WebApp) return true; } catch (e) {}
    return TG_UA;
  }

  function readKey() {
    try { return localStorage.getItem(KEY_NAME) || ''; } catch (e) { return ''; }
  }

  function writeKey(v) {
    try {
      if (v) localStorage.setItem(KEY_NAME, v);
      else localStorage.removeItem(KEY_NAME);
    } catch (e) {}
  }

  // --- مهاجرت دوطرفه به کلید یکسان‌شده (idempotent) ---
  (function migrate() {
    try {
      if (!localStorage.getItem(KEY_NAME)) {
        var ss = sessionStorage.getItem(LEGACY_KEY) || '';
        var ls = localStorage.getItem(LEGACY_KEY) || '';
        var v = ss || ls;
        if (v) localStorage.setItem(KEY_NAME, v);
      }
      // پاک‌کردن نسخه‌های قدیمی بعد از انتقال
      if (sessionStorage.getItem(LEGACY_KEY)) sessionStorage.removeItem(LEGACY_KEY);
      if (localStorage.getItem(LEGACY_KEY)) localStorage.removeItem(LEGACY_KEY);
    } catch (e) {}
  })();

  function getInitData() {
    try {
      var w = window.Telegram && window.Telegram.WebApp;
      if (w && w.initData) return w.initData;
    } catch (e) {}
    try {
      var h = new URLSearchParams(location.hash.slice(1)).get('tgWebAppData');
      if (h) { sessionStorage.setItem('sq_tg_init', h); return h; }
      return sessionStorage.getItem('sq_tg_init') || '';
    } catch (e) { return ''; }
  }

  // انتظار برای آماده‌شدن WebApp/initData (حداکثر timeoutMs)
  function waitForInitData(timeoutMs) {
    return new Promise(function (resolve) {
      var t0 = Date.now();
      (function tick() {
        var d = getInitData();
        if (d) return resolve(d);
        if (Date.now() - t0 >= timeoutMs) return resolve('');
        setTimeout(tick, 120);
      })();
    });
  }

  // پیام خطای واضح در UI (به‌جای prompt بی‌اثر داخل WebView تلگرام)
  function showTgError(msg) {
    try {
      var el = document.getElementById('tg-auth-error');
      if (!el) {
        el = document.createElement('div');
        el.id = 'tg-auth-error';
        el.setAttribute('role', 'alert');
        el.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:99999;' +
          'background:#ef4444;color:#fff;padding:10px 14px;font:13px/1.6 Tahoma,Arial,sans-serif;' +
          'text-align:center;direction:rtl';
        (document.body || document.documentElement).appendChild(el);
      }
      el.textContent = msg;
    } catch (e) {}
  }

  var canPrompt = true;      // فقط مرورگر عادی؛ یک‌بار در هر بارگذاری صفحه
  var hasFailedOnce = false; // بعد از 401/403 فقط یک‌بار اجازه‌ی پرامپت مجدد

  function promptKey() {
    if (!canPrompt) return '';
    canPrompt = false;
    var k = '';
    try { k = window.prompt('کلید ادمین را وارد کن') || ''; } catch (e) {}
    if (k) writeKey(k);
    return k;
  }

  // --- رهگیری fetch برای درخواست‌های /api/ ---
  var origFetch = window.fetch;

  window.fetch = function (input, init) {
    return (async function () {
      var usedKey = false;
      try {
        var url = typeof input === 'string' ? input : (input && input.url) || '';
        var u = new URL(url, location.origin);
        if (u.origin === location.origin && u.pathname.indexOf('/api/') === 0) {
          init = init || {};
          var headers = new Headers(init.headers || (typeof input !== 'string' && input.headers) || {});
          if (!headers.has('Authorization') && !headers.has('X-Admin-Key')) {
            var data = getInitData();
            if (data) {
              // مسیر تلگرام
              headers.set('Authorization', 'tma ' + data);
            } else if (inTelegram()) {
              // داخل تلگرام ولی initData هنوز نیامده: منتظر می‌مانیم
              data = await waitForInitData(3000);
              if (data) {
                headers.set('Authorization', 'tma ' + data);
              } else {
                showTgError('اتصال به تلگرام برقرار نشد (initData در دسترس نیست). لطفاً مینی‌اپ را ببند و دوباره باز کن.');
              }
            } else {
              // مرورگر عادی: کلید ذخیره‌شده یا یک‌بار prompt
              var key = readKey();
              if (!key) key = promptKey();
              if (key) { headers.set('X-Admin-Key', key); usedKey = true; }
            }
          }
          init.headers = headers;
        }
      } catch (e) {}

      var res = await origFetch.call(this, input, init);
      if (usedKey && (res.status === 401 || res.status === 403)) {
        writeKey('');
        if (!hasFailedOnce) {
          canPrompt = true;
          hasFailedOnce = true;
        }
      }
      return res;
    }).call(this);
  };

  // --- گلوبال برای اسکریپت اصلی اپ (index.html) ---
  window.TgAuth = {
    isTelegram: inTelegram,
    getKey: readKey,
    setKey: writeKey,
    // async: هدر Authorization را برمی‌گرداند یا رشته‌ی خالی (بدون پرامپت داخل تلگرام)
    getAuthHeader: async function () {
      var k = readKey();
      return k ? ('Bearer ' + k) : '';
    }
  };
})();
