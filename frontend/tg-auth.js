// به همه‌ی درخواست‌های /api هدر احراز هویت مینی‌اپ تلگرام را اضافه می‌کند.
// در مرورگر عادی (بدون initData) از هدر X-Admin-Key با کلید ذخیره‌شده در sessionStorage استفاده می‌کند.
(function () {
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

  var origFetch = window.fetch;
  var canPrompt = true;      // اجازه پرامپت در این بارگذاری صفحه
  var hasFailedOnce = false;  // فقط یک بار بعد از 401/403 اجازه پرامپت مجدد

  window.fetch = function (input, init) {
    var usedAdminKey = false;
    try {
      var url = typeof input === 'string' ? input : (input && input.url) || '';
      var u = new URL(url, location.origin);
      if (u.origin === location.origin && u.pathname.indexOf('/api/') === 0) {
        var data = getInitData();
        init = init || {};
        var headers = new Headers(init.headers || (typeof input !== 'string' && input.headers) || {});
        if (data) {
          // مسیر تلگرام: فقط اگر Authorization قبلاً ست نشده باشد
          if (!headers.has('Authorization')) {
            headers.set('Authorization', 'tma ' + data);
          }
        } else {
          // مسیر مرورگر عادی (بدون initData)
          var key = sessionStorage.getItem('sq_admin_key') || '';
          if (!key && canPrompt) {
            canPrompt = false;
            key = window.prompt('کلید ادمین را وارد کن') || '';
            if (key) {
              sessionStorage.setItem('sq_admin_key', key);
            }
            // کنسل = بدون هدر و بدون پرامپت بیشتر در این صفحه
          }
          if (key && !headers.has('Authorization') && !headers.has('X-Admin-Key')) {
            headers.set('X-Admin-Key', key);
            usedAdminKey = true;
          }
        }
        init.headers = headers;
      }
    } catch (e) {}

    var promise = origFetch.call(this, input, init);
    if (usedAdminKey) {
      return promise.then(function (res) {
        if (res.status === 401 || res.status === 403) {
          try { sessionStorage.removeItem('sq_admin_key'); } catch (e) {}
          // فقط یک پرامپت جدید بعد از شکست مجاز است (جلوگیری از حلقه)
          if (!hasFailedOnce) {
            canPrompt = true;
            hasFailedOnce = true;
          }
        }
        return res;
      });
    }
    return promise;
  };
})();
